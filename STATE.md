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

## A failed read is not an absent row (2026-11-04)

The whole race was lost to one 5-second timeout. Bethpage Black, play date
Wednesday 2026-11-11:

```
23:56:18  GOLFBOT_GRID_STORE fetch error ReadTimeout: The read operation timed out
23:56:20  "no teesheet has ever been read for this course on a Wednesday"
          -> inferred rhythm from Mon/Sat/Sun; floor borrowed from Monday's
             2026-09-07 release: 08:30
00:00:00  24 holds fired at 08:30-09:20  ->  all "Time not available."
00:00:43  "kept existing grid for course 2431 dow 2: stored 09:30-16:00, 29 slots"
```

The Wednesday row was in the table the whole time saying the sheet starts at
**09:30**. `fetch_grid` returned `None` for a read failure exactly as it does
for a row that does not exist, so the caller concluded we had never seen a
Wednesday, inferred from other weekdays, and borrowed a floor from a read two
months stale. Every hold then fired an hour below the real sheet. Exactly one
such timeout appears in 30 days of logs — it just landed at arming.

Fixed in v1.14: `fetch_grid_result()` retries three times and returns
`(row, ok)`, and arming will no longer infer a grid *or* borrow a sibling floor
when `ok` is False — it says so and runs as Sniper instead. `timing.cpu_throttle`
read `throttled_ms: 0` for this race, so CPU had nothing to do with it.

**The general rule: when a lookup can fail, "I could not read it" and "there is
nothing there" must not return the same value.** Anywhere else that pattern
exists here, treat it as a bug.

## Races flag themselves now (v1.15)

Every failure this bot has had was visible in the data at the time and nobody
saw it. So a compromised race says so in its own `summary`, at the front, where
the UI and any Supabase query already look:

```
!! BORROWED_FLOOR !! AIMED_OFF_SHEET — No Wednesday release has been read for
this course, so holds start from Monday's floor (08:30, read 2026-09-07,
58 days ago). || ABORTED: ... [95 polls | pre-staged 24 (0 accepted)]
```

`races.warnings` carries them as rows. Current codes:

| code | means |
|---|---|
| `aimed_off_sheet` | every pre-staged hold refused as "Time not available" — fired at times the sheet does not offer. **Derived at flush**, so it needs no foresight |
| `grid_unreadable` | the registry did not answer at arming; Future Sight stood down |
| `stale_grid` | aiming with a grid older than `GRID_STALE_WARN_DAYS` (21) |
| `borrowed_floor` | this weekday has no release read; the floor came from another day |
| `inferred_grid` | no teesheet ever read for this weekday; rhythm inferred |

**The distinction that makes `aimed_off_sheet` work:** ForeUp says
*"Time not available."* when a time is not on the sheet, and *"...no longer
available."* when somebody took it. Losing to a competitor is a race; firing at
times that do not exist is a targeting failure no amount of speed fixes. Only
the second, with zero accepts, raises the flag. Keep that distinction.

## The open question: refusals on times the sheet advertised (v1.16)

On 2026-11-04 the availability API listed 09:50, 11:00 and 11:10 as open and
`pending_reservation` refused all three with **"Time not available."** — the
same string it uses for times that are not on the sheet at all. Only 09:30 came
back "no longer available" (genuinely taken).

**This is unexplained and may be the common thread under several lost weeks.**
It is NOT a targeting failure: those were real, on-grid, currently-listed times.

It also means `aimed_off_sheet` (v1.15) is a heuristic, not a proof. "Time not
available" has now been observed meaning both "this time does not exist" and
something else on a time that plainly did.

v1.16 stops the guessing. Every failed hold now records, inside `attempts`:

- `slot_polled` — the slot AS ADVERTISED (spots, booking class, side id,
  schedule/course id, group sizes, minimum players)
- `payload_sent` — what we actually POSTed
- `mismatch` — the disagreements, named: wrong booking class, wrong side id,
  more players than spots, a group size the slot does not allow, below
  `minimum_players`

The burst records `payload_sent` too; it builds from a stored template rather
than a live slot, so what we sent is the only evidence there is.

**Next time this happens, read `attempts[].mismatch` first.** If it is empty and
ForeUp still refused, the disagreement is in a field not yet captured — widen
`SLOT_FORENSIC_KEYS` / `PAYLOAD_FORENSIC_KEYS` rather than theorising.

```sql
select a->>'slot_time', a->>'error', a->'mismatch',
       a->'slot_polled', a->'payload_sent'
from races r, lateral jsonb_array_elements(r.attempts) a
where left(r.run_id,12) = '<run>';
```

## Why Bethpage kept losing, and what fixed it (v1.17)

Across seven Bethpage Future Sight races the burst fired **186 pre-staged holds
and 3 were accepted**. Montauk, same code, same night: 5 of 6. It was never a
Black-only problem and it was never speed.

Run `9621586f744b` (2026-10-21, Black) is the clean proof:

```
BURST fired:     07:00, 07:10, 07:20, 07:30, 07:40, 07:50   (66 holds, 0 accepted)
POLL attempted:  09:30                                       -> WON
```

The sheet started at 09:30. The burst spent the whole window two and a half
hours below it. Tonight (11-04) the same thing an hour below: 08:30-09:20
against a 09:30 sheet.

**The chain:** Bethpage sells out faster than the harvester can ever read it
(Black shows 0-1 open slots on every day of the visible window), so there is no
trustworthy release floor. Without a floor, targets start at the user's
`time_start` — often 07:00. The burst then re-fires those dead times for the
full five seconds while the poll, within one second, is looking at the real
sheet.

**v1.17:** the poll publishes every in-window time it sees into `observed_times`,
and each burst pass after the first re-aims at them. Guesses at T+0, reality as
soon as reality is known. A re-aim records the `reaimed_from_poll` warning, which
also marks that course/weekday's stored floor as wrong.

This is the first change in this file that should convert races rather than
explain losses. If Bethpage still loses after it, compare what the burst fired
against what the poll saw — that query is two rows and settles it:

```sql
select 'burst' src, string_agg(distinct p->>'time', ', ' order by p->>'time') times
from races r, lateral jsonb_array_elements(r.prestage) p where left(r.run_id,12)='<run>'
union all
select 'poll', string_agg(a->>'slot_time', ', ' order by a->>'slot_time')
from races r, lateral jsonb_array_elements(r.attempts) a where left(r.run_id,12)='<run>';
```

## Audit fixes (v1.19)

- An unused pre-staged hold is released however the race ended. It used to be
  released only on a loss, so a poll-path win could leave a second hold on the sheet.
- The arming reference read is banked under the weekday it was read for. The
  fallback reference is "tomorrow"; it used to be filed under the play date's weekday.
- `observed_times` (the burst's re-aim feed) only takes times from the burst's own
  course. `polls.observed` is recorded on every poll, burst or not; other courses in
  a rotation are keyed `"<teesheet> HH:MM"`.
- `upsert_grid` skips the write when it cannot read the stored row to compare.
- v1.20 reverted v1.19's per-worker HTTP client: no race ever showed the shared
  connection failing, so the proven behaviour stays until one does.
- Gone: `src/auth`, `src/scheduler`, `src/config.py`, `config/courses.yaml`,
  `/api/courses`, the `interfaces.py`/`service.py`/`executor.py` stubs. Nothing imported them.

## What a lost race records (v1.18)

A race now leaves behind enough to diagnose it without another week of waiting:

| where | what |
|---|---|
| `summary` | leads with `!! CODE — detail` when anything compromised the race |
| `warnings` | `aimed_off_sheet`, `grid_unreadable`, `stale_grid`, `borrowed_floor`, `inferred_grid`, `reaimed_from_poll` |
| `prestage[]` | every hold fired: time, result, latency, error, `payload_sent` |
| `attempts[]` | every submit: `slot_polled` (as advertised), `payload_sent`, `mismatch` |
| `polls.observed[]` | **every in-window time the sheet actually showed, with ms from release** |
| `prestage_decision[]` | the whole arming story, keyed to the run |
| `timing.cpu_throttle` | whether the CPU quota stalled us (so far: never) |

`polls.observed` is the one that answers the question that took a month to ask:
**did we aim at times that existed?** Counts could not. Compare it against what
the burst fired:

```sql
select
  (select string_agg(distinct p->>'time', ', ' order by p->>'time')
     from jsonb_array_elements(r.prestage) p)            as burst_fired,
  (select string_agg(o->>'time', ', ' order by o->>'time')
     from jsonb_array_elements(r.polls->'observed') o)   as sheet_showed,
  r.warnings
from races r where left(r.run_id,12) = '<run>';
```

If `burst_fired` and `sheet_showed` do not overlap, the release floor for that
course/weekday is wrong — that is the Bethpage failure, and it is now visible
in one row instead of inferred from an error string.

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
  **v1.13 — a weekday with no good read borrows one.** 10-25, Black Sunday:
  the only Sunday row was the 4-time 08-30 read, so `release_floor` (rightly)
  gave nothing and Future Sight fired 30 holds at 08:00–08:50, all refused.
  Polling first saw 09:30 at T+1.14s, its hold took 2.07s, and it lost; the
  window then sold out (255 of 283 polls empty). `sibling_release_floor` now
  lends the same course's floor from days of the same type (weekend↔weekend,
  weekday↔weekday), earliest wins — replayed, that night fires 09:00–09:50.
  That race also banked a real Black Sunday read (09:30, 20 seen).
- **Arming after the release time silently targets tomorrow.** 10-25: a Red job
  armed at 19:01:34 for a 19:00 release was scheduled for 10-26 19:00 (the
  worker rolls a passed release to the next day). It was cancelled and a
  snipe-now at 19:01:56 found nothing in 07:00–10:00 for 90s. Arm before 7pm.
- **First live race with the HTTP/2 guard (v1.12), 10-25:** all 30 burst holds
  got clean ForeUp answers — no Broken pipe, no ConnectionTerminated, no deque
  error. CPU throttling in the release window: zero.
- **One HTTP/2 connection shared by the burst threads was not thread-safe —
  guarded since v1.12.** The burst and polling share one connection, and
  httpcore's sync HTTP/2 code leaves the shared `H2Connection` unguarded (only
  socket reads/writes are locked). Two production hits:
  **10-17** `deque mutated during iteration` (HPACK table) — a hold never left;
  **10-21** `ConnectionTerminated(error_code=1)` at T+1.35s — ForeUp killed the
  connection and six in-flight holds died "Broken pipe". The winning poll hold
  had landed at T+0.55s; had the kill come first, it would have died too.
  A local reproduction (`tests/test_h2_concurrency.py`: 12 threads, one
  connection, a strict h2 server) found four races — header compression,
  unguarded state changes, two threads taking the same stream id ("pseudo-header
  in trailer"), and a stream answered and closed by the server before its own
  setup finished — plus requests that *arrived* carrying another request's
  headers. Unguarded: ~10% of requests survive. `src/discovery/h2_guard.py`
  (installed when `foreup_client` imports) puts every call on the shared h2
  state under one per-connection lock and makes "take stream id → send headers
  → open its window" one step; socket I/O stays outside the lock. Guarded:
  360/360 GETs and hold-style POSTs, zero kills, zero garbled; six 300ms
  requests still finish in ~0.7s, so parallelism is intact. Same connection,
  same protocol, same requests on the wire. It patches httpcore internals, so
  the HTTP stack is **pinned** in `requirements.txt` (httpx 0.28.1, httpcore
  1.0.9, h2 4.4.1 — what Render was already installing) and `/health` reports
  `"h2_guard": true`. If it ever reads false, the installed httpcore no longer
  matches and the burst is unsafe again.
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
dependencies installed first — and **use `python3 -m pip`, not `pip`**: in this
image `pip` belongs to Python 3.13 while `python3` is 3.11, so plain `pip
install` reports success and leaves `import fastapi` failing.

```bash
python3 -m pip install -r requirements.txt
```

If a test fails right after you edited a module and the edit looks correct, drop
stale bytecode before believing it: `find . -name __pycache__ -exec rm -rf {} +`.

```bash
python3 tests/test_prestage_concurrency.py   # ~3 min: aligns to minute boundaries
python3 tests/test_fee_recovery_paths.py   # both ways of winning a hold reach the payment link
python3 tests/test_cpu_throttle.py        # CPU-throttle logging reads cgroup v1/v2, never raises
python3 tests/test_grid_targeting.py      # no fake grids from two intervals; disagreeing grids both get shots
python3 tests/test_h2_concurrency.py      # ~1 min: many threads on one HTTP/2 connection never scramble requests
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
