# Where Golfbot actually stands

Operational state as of **2026-09-22**, written for whoever picks this up next.
`CHANGELOG.md` says what shipped; this says what is true, what is open, and what
has already been ruled out so you don't re-litigate it.

---

## It works now. Here is the run that proves it.

**2026-09-19, Montauk — two real bookings**, and the first time Future Sight has
ever fired at a live release.

```
19:00:00.000  burst fires 09:03 09:12 09:21 09:30 09:39 09:48 — all at once
19:00:01.016  FIVE OF SIX HOLDS ACCEPTED (~1.0s each)
19:00:03.861  poll #1 finally returns (3.9s)
19:00:14.874  09:39 confirmed — TTID_09191900145e71r, 4 players
```

`first_poll_offset_ms: 1.12` — the first hold left **1 millisecond** after the
release, with no teesheet read in front of it. That is the whole design.

For contrast, the three days we lost before this (`races` on 08-30/08-31): the
first hold went out at **T+801ms** and **T+1189ms**, into a race ForeUp had
already decided. We were losing on the time spent asking what opened.

Run `732229020613` won on a grid the code itself labelled **an educated guess** —
the only reference sheet available ran 13:06–17:00 against a 09:00–12:45 window.
Five of six accepts says the guess was right. Before v1.04 that same situation
would have disarmed Future Sight entirely and left us polling.

---

## How a race actually runs

Two things happen **concurrently** from T+0. This is the single most important
thing to understand before changing anything in `_sniper_worker_main`.

```
T+0 ──┬── BURST THREAD: fires every pre-staged target simultaneously.
      │   Pre-built payloads from the stored grid — no discovery round trip.
      │   Only ACQUIRES holds. First accept wins; the rest are released via
      │   DELETE pending_reservation/{id}.
      │
      └── MAIN THREAD: polls, and completes the booking.
          Completion (2FA, payment) lives here and ONLY here, so there is
          exactly one booking in flight and no way to double-book.
```

Rules that are load-bearing, each bought with a lost race:

- **The burst must never block polling.** It ran to completion first until
  v1.00; that forced a gate which disarmed Future Sight on 08-31 and cost us
  the race. On its own thread, a wrong grid costs the fallback nothing — which
  is why v1.04 fires even on an untrusted grid rather than standing down.
- **Targets fire in parallel.** Sequentially (pre-v1.00) they went out at
  T+0.000, T+0.568, T+2.443, T+4.204 — only the first was ever really in the race.
- **Check for a won hold before the poll path submits** (v1.07). On 09-19 the
  burst won 09:39 at T+1.0s but the loop only looked at the top of the
  iteration, so the poll path spent its submit on 09:03 — a slot *our own burst
  had just released* — and lost it. We raced ourselves.
- **Never call `_cancel_requested()` off the main thread.** It pops from
  `cmd_queue` and pushes non-cancel messages back, so it can steal the 2FA code
  mid-booking.

---

## Open items

**1. The weekly grid harvest has never once succeeded.** *(needs a human)*

`.github/workflows/harvest-grids.yml` fires Sundays 09:00 UTC. It ran 09-06 and
09-13 and **failed both times in ~15 seconds**: all four repo secrets are empty.

```
FOREUP_USERNAME:        (empty)
FOREUP_PASSWORD:        (empty)
RACE_LOG_WEBHOOK_URL:   (empty)
RACE_LOG_WEBHOOK_TOKEN: (empty)
harvest_grids.py: error: ForeUp credentials required
```

That is why nearly every `course_grids` row still reads `updated_at 09-01 09:57`
— one manual run, three weeks ago. Set them at
`github.com/fmx520/Golfbot/settings/secrets/actions`; the same values are
already Render env vars.

Even with the secrets set, the workflow would have kept failing until v1.10:
Actions runs `run:` steps under `bash -e`, so the harvester's normal "partial
coverage" exit 1 ended the step before `code=$?` ran. Fixed (`|| code=$?`). As
of 10-11 the only fresh grids are the ones races learn (2432 Sat, 2436 Sat);
everything else dates from 08-30–09-01.

**Rotate the Supabase service key before copying it there** — it was pasted into
a chat transcript and is considered exposed. The DB password from that same
transcript also still needs rotating.

**2. Undecided: halve the burst request volume.**

Parallel fires made each pass ~2.3× faster while `burst_seconds` (5) and the
inter-pass gap (0.25s) stayed put, so roughly twice as many passes fit: ~44
requests per burst where the sequential version sent ~20. This was a side effect
of v1.00, not a decision.

Widening the gap to ~0.75s restores the old volume and keeps the full 5s
late-publish hedge. **Nothing has pushed back on us** — no rate limits, no
anti-bot, five simultaneous holds accepted cleanly. This is precaution, not a
fix. Note also that in both production Future Sight runs the re-fire passes have
**never** demonstrably helped: 09-19 won on pass 1; 08-30 failed on all passes
for an unrelated reason (wrong grid).

---

## Already ruled out — don't re-propose without new evidence

**Render CPU upgrade.** Tempting, and I argued for it before the user correctly
pushed back. The data splits:

- *The median is ForeUp.* p50 goes ~250ms (empty sheet) → ~400–540ms (full sheet
  at the release). That is their server under load. More local CPU cannot help.
- *The tail is us.* Multi-second outliers appear at 23:17 and 08:48 on runs with
  **100% empty responses and zero competition** — 1306ms and 3433ms. Nothing
  upstream explains a 3.4s reply to an empty query at 8:48am. That is CFS
  throttling on 0.1 vCPU.

The discriminator run is `5bde9d0084d2` (08-29 **19:01**, peak load, 55% empty
polls): **p50 of 104ms** — the fastest median in the table, at the busiest
moment. ForeUp answers cheap queries fast even at peak.

But the upgrade's value collapsed anyway: Future Sight fires pre-built payloads
with almost no parsing, and v1.07 means a slow poll no longer costs the win.
On 09-19 the poll took 3.9s and we won regardless. **Do not spend money here**
unless the poll path becomes load-bearing again.

**Throttle instrumentation — built in v1.09.** The question came back once the
cost that matters moved from the poll to the T+0 holds (0.9–2.2s each on 10-10,
against 247–309ms for the same race's requests at T+4–8s). Render's CPU metrics
for both Future Sight releases peak at ~7–9% of the 0.15 limit, but they are
30s averages and can hide a sub-second burst; the kernel counters cannot.

Every race now records `timing.cpu_throttle`, from a baseline read just before
the release: `release` (to T+5s, where races are decided) and `race` (to the
end). `throttled_periods` counts 100ms windows in which we hit the quota and
waited; `throttled_ms` is the total wait. **Zero in `release` across a few races
means CPU is not slowing the holds — stop spending thought on a bigger plan.**
Non-zero there is the first real evidence for one. `periods: 0` means no quota
was enforced at all. Logging only: reads happen before T+0 and on a timer thread.

```sql
select left(run_id,12) run, timing->'cpu_throttle'->'release' release_window
from races where timing ? 'cpu_throttle' order by recorded_at desc;
```

**Detection evasion** (proxy/IP rotation, TLS fingerprint spoofing,
human-mimicking timing) is out of scope and was declined. Not revisitable.

---

## Things that will mislead you

Written because each one cost real time this session.

- **There are two ways to win a hold, and both must be finished the same way.**
  Polling wins it in `book_slot`; Future Sight wins it in the burst and finishes
  it in `complete_prestaged_hold`. Until v1.08 the booking-fee recovery lived
  only inside `book_slot`, so it had only ever been exercised through polling
  (the three 08-31 `awaiting_payment` runs). On **10-10, Bethpage 2432**, Future
  Sight won 08:36, 08:45 and 08:27 at T+2s, met "A booking fee is required",
  had no recovery, and gave the race away — recorded as `lost_at_submit`, which
  it was not. Both paths now go through `_finish_hold` in the client and
  `_enter_awaiting_payment` in the worker. **Anything that changes what happens
  after a hold is won goes there, not into one path.**
- **A course's rhythm does not say where its bookable sheet starts (v1.11).**
  Targets used to be the earliest six grid times in the user's window. On
  **10-17** the window opened at 07:00: Future Sight fired 48 holds at Red
  07:06–07:51 and Black 07:00–07:50, every one "Time not available" (some in
  ~220ms, so not slowness), while the fullest sheet seen at that release
  started at **08:18 (Red) and 09:00 (Black)**. 10-10 agrees: Red 08:00/08:09
  refused, 08:27–08:45 won. Whether earlier times are not bookable online for
  the class or are gone before any hold lands, aiming there wins nothing. Now
  `grid_store.release_floor` takes the earliest time seen open on a sheet read
  *at a release* (play date ≥7 days after the read, in New York time; ≥20 times
  seen — a 4-time Black Sunday read once said 16:20) for the same weekday, and
  targets start there; a `prestage_grid` decision says so. Polling still covers
  the whole window. Trade-off: if a release read caught the sheet after its
  first times went, the floor is late — it costs earliness, not the win, and a
  wider read at a later release replaces it.
- **Firing six holds at once over one HTTP/2 connection is not fully
  thread-safe.** 10-17, Red: one first-round shot (07:15) never left —
  `deque mutated during iteration` from HPACK header encoding in h2, two burst
  threads encoding headers on the shared connection at once. First occurrence
  in any race; cost nothing that night (the time was unbookable) but can cost a
  real shot. Not changed — the connection layer is load-bearing and the user
  has asked not to touch it without evidence. This is the first such evidence.
- **First CPU-throttle numbers (v1.09 logging), 10-17:** in the 15s release
  window, 3 throttled 100ms periods totalling ~290ms, with two races running at
  once. Not zero — the first evidence CPU can add delay at the release — but the
  window is 15s wide, so it does not show whether those landed at T+0. Both
  races report identical numbers because the counters are container-wide.
- **Grid rows older than 45 days can now be overwritten by thin reads.** That is
  the designed staleness rule; the 08-30–09-01 rows crossed it 10-15. Rows that
  races refresh (the Saturdays) stay protected. The harvest secrets (open item
  1) would keep the rest fresh. `test_grid_no_downgrade.py` hard-coded an 08-31
  fixture and started failing on 10-15 for the same reason — its dates are now
  relative to today.
- **Grid choice never looks at age — so disagreeing grids now split the shots
  (v1.10).** Arming ranks the live reference read and the stored grid by
  window coverage, then trustworthiness, then times seen. Tonight's live read
  is usually a thin evening sample, so a weeks-old stored grid that covers the
  morning beats it every time. If a course changes its sheet, all six holds
  would go to times that no longer exist (polling still runs, so this costs
  the head start, not the race). When a second grid with ≥5 observations
  would aim at different times, targets are now interleaved between the two
  and a `prestage_grid` decision says so. A coarser view of the same sheet (an
  18-minute read of a 9-minute grid) does not count as disagreeing.
- **`grid.py` used to read two intervals meeting as one fake grid.** A
  10-minute day into a 15-minute twilight has GCD 5 and was reported as a
  *consistent* 5-minute sheet. A divisor under 7 is now believed only if two
  open times are actually that far apart. 9/10-minute sheets are unaffected.
- **"Bethpage Red runs 10 minutes in the morning" (in `_sample_covers`'
  docstring) is contradicted by October data**: holds accepted at 08:27–08:45
  on 10-10 and the 10-17 release sheet itself both put Red mornings on the
  same 9-minute @ :03 grid as its afternoons. Either the sheet changed with the
  season or the original diagnosis was the booking-class problem v0.97 fixed.
  Sheets are not fixed facts — which is why the split above exists.
- **The live reference read is stored under the play date's weekday**, even
  when it came from tomorrow's sheet (1 row today: 2436 "Monday" learned from
  Tuesday 09-01). Harmless for targeting — interval and phase are per-course —
  but that row's template (class, fee) is the wrong day's. Not yet fixed.
- **The payment-hold keepalive never ran before v1.08.** It was a daemon thread
  started just before the worker returned, and a `Process` exits when its target
  returns, killing daemon threads — zero refreshes were ever sent while a user
  paid. The hold was only kept alive during the 2FA code wait. The worker now
  stays up refreshing until refreshes stop being accepted (paid or gone), a stop
  request, or `PAYMENT_HOLD_SECONDS` (600).
  **First live run, 10-17 (Red 12:12, $20): booking paid and confirmed by the
  user.** The keepalive sent 74 refreshes, all accepted, and ended on the time
  limit — it never saw a refresh fail. Either ForeUp keeps accepting refreshes
  after the fee is paid, or the payment landed late in the 10 minutes; the user
  did not say when they paid. Do not rely on "refresh fails once paid" to end
  the loop early. Harmless either way: the loop never releases, it only holds.

- **`prestage` empty does NOT mean Future Sight wasn't selected.** It means no
  holds *fired*. On 08-31 it was armed and disarmed itself at arm time, and the
  reason lived only in the browser event log. That is now persisted to
  `races.prestage_decision` — **read it before concluding anything** about why a
  race went the way it did.
- **`mode` only ever holds `sniper` / `snipe_now`.** It does not record whether
  Future Sight was on. Don't infer strategy from it.
- **Don't compare mid-day runs to release runs** to isolate a variable. They
  differ in payload size *and* upstream load simultaneously. Find a run that
  separates them (see `5bde9d0084d2` above).
- **A booking fee is not knowable from `booking_fee_required`.** Bethpage
  charges $5 and reports the flag; Montauk's slots report `false` throughout,
  and its one clean booking read *"Original amount due at course: $69.00"*. The
  payment path is therefore **reactive on ForeUp's error text**, not on the
  flag. Keep it that way.
- **PostgREST rejects the ENTIRE insert on one unmapped key.** Run
  `python3 scripts/check_schema_drift.py` after touching `RaceRecorder.as_dict`
  or `supabase/schema.sql`. The sink now drops the offending column and retries
  (logging `GOLFBOT_RACE_SINK_DRIFT`), but that is a safety net, not a licence.
- **A trigger in `schema.sql` is a file, not an applied migration.** The
  `course_grids.updated_at` trigger sat in the file for weeks while the live DB
  had `trigger_count: 0`, so `updated_at` recorded insert time and never
  changed. Applied 09-01. Verify live state; don't trust the file.

---

## Checking on things

Data lives in Supabase project `mdbxvrxzilxkasznhzqv`, tables `races` and
`course_grids`. Render service `srv-d8176p3rjlhs73au3sc0` (`Golfbot-2`,
free tier), deploys from `main` on commit. `/health` reports `APP_VERSION`.

```sql
-- how did the last few races go, and did Future Sight fire?
select left(run_id,12) run,
       to_char(created_at at time zone 'America/New_York','MM-DD HH24:MI') et,
       mode, outcome,
       timing->>'first_poll_offset_ms' first_ms,
       jsonb_array_length(coalesce(prestage,'[]'::jsonb)) fires,
       jsonb_array_length(coalesce(prestage_decision,'[]'::jsonb)) decisions
from races order by created_at desc limit 10;

-- WHY did Future Sight arm or stand down on a given run?
select d->>'stage', left(d->>'detail',300)
from races r, lateral jsonb_array_elements(r.prestage_decision) d
where left(r.run_id,12) = '<run>';

-- which course/day pairs can Future Sight aim at with real observations?
-- (trustworthy = covers the window, OR seen >= 20 and span >= 240 min)
select course_id, day_of_week, interval_minutes, anchor_phase,
       first_time, last_time, observed_count, source_date, updated_at
from course_grids order by course_id, day_of_week;
```

Unobserved (course, weekday) pairs do not block a race: since v1.06 the grid is
inferred from the same course's other days, because interval and phase belong to
the **course**, not the day — true across all 27 rows, zero counterexamples.
Note 2437 runs phase `:06` where its neighbours run `:03`, so inference is
per-course and never global.

---

## Running the tests

No pytest; each file is standalone and self-reporting. A fresh container needs
`pip install -r requirements.txt` first.

```bash
python3 tests/test_prestage_concurrency.py   # ~3 min: aligns to minute boundaries
python3 tests/test_fee_recovery_paths.py   # both ways of winning a hold reach the payment link
python3 tests/test_cpu_throttle.py        # CPU-throttle logging reads cgroup v1/v2, never raises
python3 tests/test_grid_targeting.py      # no fake grids from two intervals; disagreeing grids both get shots
python3 tests/test_grid_inference.py
python3 tests/test_grid_no_downgrade.py
python3 tests/test_sink_drift.py
python3 scripts/check_schema_drift.py
```

`test_prestage_concurrency.py` drives the real `_sniper_worker_main` with fake
ForeUp clients and covers the four properties that each cost a race: targets
fire together, polling isn't delayed by the burst, extra holds are released, and
a hold won mid-poll is taken before the poll path can submit against it.

**Verify a regression test actually fails without its fix.** The v1.07 test was
confirmed that way — remove the check and the prestaged hold never gets booked
at all.
