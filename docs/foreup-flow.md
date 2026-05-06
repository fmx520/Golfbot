# ForeUp Booking Request Flow

This document defines the exact request sequence for a ForeUp tee-time booking lifecycle and the reliability guarantees enforced by the clients in `src/discovery/foreup_client.py` and `src/booking/foreup_client.py`.

## 1) Session/auth bootstrap

1. `GET /api/bootstrap` (or facility-specific bootstrap endpoint)
   - Establishes cookies/session material.
   - Returns CSRF or anti-forgery token where applicable.
2. `POST /api/auth/login`
   - Sends credential payload.
   - Stores auth cookies + bearer/session token.
3. `GET /api/auth/me`
   - Verifies effective identity and role permissions.

**Client guarantees**
- Connection reuse uses one pooled HTTP client per process.
- Strict request timeouts are applied to every request.
- Logging emits `request_id`, stage name, start/end timestamps, latency, and outcome.

## 2) Tee sheet / availability lookup

1. `GET /api/teesheet?course_id=...&date=...`
   - Pulls the tee-sheet for the requested date.
2. `GET /api/availability?course_id=...&date=...&players=...`
   - Returns open slots and pricing/rate metadata.

**Client guarantees**
- Retry with bounded exponential backoff + jitter is applied **only** to retry-safe calls (GET/HEAD).
- `429` and `5xx` responses are retried if safe and retry budget remains.
- Each retry attempt is individually logged with attempt number and delay.

## 3) Slot validation and hold

1. `POST /api/slots/validate`
   - Confirms requested slot still matches party-size/rate constraints.
2. `POST /api/slots/hold`
   - Attempts temporary hold before payment/confirmation.

**Client guarantees**
- Non-idempotent calls are not automatically retried.
- `409`, `410`, and `422` are treated as stale-slot race outcomes and surfaced explicitly.
- `429/5xx` on non-safe calls return a typed error for caller-managed recovery.

## 4) Final booking confirmation

1. `POST /api/bookings/confirm`
   - Finalizes reservation and returns confirmation number.

**Client guarantees**
- Idempotency guard prevents duplicate booking attempts for the same key.
- Caller supplies deterministic idempotency key (e.g. `{facility}:{course}:{date}:{time}:{player_hash}`).
- Duplicate in-flight attempts with same key fail fast with a deterministic error.
- Completed keys remain in-memory to block accidental immediate duplicates.

## Telemetry and latency measurement

Each stage logs:
- `request_id`: UUID per end-to-end booking flow.
- `stage`: semantic stage (`bootstrap`, `availability_lookup`, `slot_validation`, `slot_hold`, `final_booking`).
- `started_at` / `ended_at`: UTC ISO timestamps.
- `latency_ms`: measured duration.
- `attempt`: retry attempt index.
- `http_status` and normalized `result` (`ok`, `retry`, `error`, `stale_slot`, `duplicate_blocked`).

The booking client can aggregate stage durations to produce end-to-end latency for SLO tracking.
