# Golfbot

Automated tee time booking for courses that use [ForeUp](https://foreupsoftware.com) software. Supports both direct API booking and Playwright browser-automation fallback for courses with anti-bot protections.

## Setup

```bash
pip install -r requirements.txt

# Only needed if using Playwright fallback for anti-bot courses
playwright install chromium
```

Copy `.env.example` to `.env` and fill in your ForeUp credentials:

```bash
cp .env.example .env
```

## Finding your course's booking URL

Go to the course's online booking page — it'll look like:
```
https://foreupsoftware.com/index.php/booking/19765/2431#teetimes
```

The two numbers are:
- **Schedule ID**: `19765`
- **Course ID**: `2431`

You can pass the full URL directly — the bot will extract both IDs automatically.

## CLI Usage

```bash
# Dry run — discover tee times without booking
python3 golfbot_cli.py \
  --foreup-url "https://foreupsoftware.com/index.php/booking/19765/2431#teetimes" \
  --play-date 2026-05-10 \
  --players 2 \
  --time-start 07:00 \
  --time-end 09:00 \
  --username your@email.com \
  --password yourpassword \
  --dry-run

# Real booking (will prompt for confirmation)
python3 golfbot_cli.py \
  --foreup-url "https://foreupsoftware.com/index.php/booking/19765/2431#teetimes" \
  --play-date 2026-05-10 \
  --players 2 \
  --time-start 07:00 \
  --time-end 09:00

# Using env vars instead of CLI flags
export FOREUP_USERNAME=your@email.com
export FOREUP_PASSWORD=yourpassword
python3 golfbot_cli.py \
  --schedule-id 19765 \
  --play-date 2026-05-10 \
  --players 2 \
  --time-start 07:00 \
  --time-end 09:00 \
  --dry-run
```

### CLI Options

| Flag | Description |
|------|-------------|
| `--foreup-url` | Full ForeUp booking URL (extracts base URL, schedule ID, course ID) |
| `--schedule-id` | ForeUp schedule ID (alternative to URL) |
| `--course-id` | ForeUp course ID |
| `--play-date` | Target date (YYYY-MM-DD) |
| `--players` | Number of players (1-4) |
| `--time-start` | Earliest acceptable tee time (HH:MM, 24h) |
| `--time-end` | Latest acceptable tee time (HH:MM, 24h) |
| `--holes` | 9 or 18 (default: 18) |
| `--username` | ForeUp login email (or `FOREUP_USERNAME` env var) |
| `--password` | ForeUp password (or `FOREUP_PASSWORD` env var) |
| `--dry-run` | Discover and select tee times without booking |

## Web UI

```bash
uvicorn src.web.app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. The web UI lets you:
- Enter ForeUp credentials
- Paste a booking URL (auto-populates schedule/course IDs)
- Set date, time window, player count
- Run dry-run or real booking jobs
- Watch job progress in real-time

## How it works

1. **Auth** — Logs into ForeUp with your credentials via their booking API
2. **Discovery** — Fetches available tee times for the target date/schedule
3. **Filtering** — Matches slots to your time window and player count
4. **Booking** — Validates slot availability, then submits a reservation (with idempotency guard to prevent double-bookings)
5. **Anti-bot fallback** — If a course has reCAPTCHA or other protections, automatically falls back to Playwright browser automation

## Logs

Every CLI run writes structured JSON to `logs/run_<id>.json` including decision traces, latency breakdowns, and success/failure state.

```bash
python3 scripts/analyze_runs.py
```

### Sniper race telemetry

Sniper races additionally write one classified record each, answering *why* a
race was lost rather than just that it was:

| Outcome | Meaning |
|---------|---------|
| `won` | Booked. |
| `late_start` | First poll fired after the release. The arming path overran it — poll speed is irrelevant until this is fixed. |
| `blocked` | ForeUp served anti-bot responses. Polls were rejected, not empty. |
| `lost_at_submit` | We saw the slot and lost it between detection and confirmation. |
| `never_saw_slot` | Polling worked and was not blocked, but nothing matching appeared. |
| `found_not_booked` | Slot found with auto-book off. |
| `aborted` / `error` | Stopped deliberately, or an unhandled failure. |

The headline metric is `first_poll_offset_ms` — negative means we were already
polling when the release landed, which is what you want.

Records go to stdout as a `GOLFBOT_RACE` line, to `logs/races.jsonl`, and to
`RACE_LOG_WEBHOOK_URL` if set. **Only the webhook is durable** — Render reclaims
the container filesystem on every deploy, restart, and free-plan spin-down, and
its log stream ages out, so correlating races across weeks needs a real sink.
See `.env.example`.

```bash
# local
python3 scripts/analyze_runs.py

# from Render's log stream
render logs --tail 1000 | grep GOLFBOT_RACE | python3 scripts/analyze_runs.py -
```

### Durable storage (Supabase)

1. Create a project at [supabase.com](https://supabase.com) (free tier is ample —
   one race record is ~1-2KB).
2. Run [`supabase/schema.sql`](supabase/schema.sql) in Dashboard → SQL Editor.
   It creates the `races` table, indexes, and leaves RLS enabled with no
   policies.
3. Copy the **`service_role`** key from Project Settings → API. Not the `anon`
   key — RLS is on, and only `service_role` bypasses it.
4. Set these wherever the bot runs (Render → Environment, or `.env` locally):

   ```bash
   RACE_LOG_WEBHOOK_URL=https://<project>.supabase.co/rest/v1/races
   RACE_LOG_WEBHOOK_HEADERS={"apikey":"SERVICE_KEY","Authorization":"Bearer SERVICE_KEY","Prefer":"return=minimal"}
   ```

5. Confirm the wiring before trusting it with a real race:

   ```bash
   python3 scripts/verify_race_sink.py
   ```

   It sends one synthetic record through the real code path and, on failure,
   names the likely cause (wrong key, missing table, bad URL).

The `service_role` key bypasses RLS entirely — keep it in environment variables
and out of the repo. `.env` is gitignored.

`supabase/schema.sql` also carries a commented query cookbook for the questions
worth asking: where races are being lost, whether first-poll offsets are still
landing before the release, and whether anti-bot rejections are climbing.

## Architecture

```
src/
  auth/          — Session token/cookie management interfaces
  booking/       — ForeUp booking client, executor, Playwright fallback
  discovery/     — ForeUp tee time discovery client
  scheduler/     — High-frequency polling scheduler
  telemetry/     — Latency measurement and metrics
  web/           — FastAPI web server with inline UI
  config.py      — YAML config loading, secret redaction
config/
  courses.yaml   — Per-course settings (schedule IDs, time windows, retry profiles)
```
