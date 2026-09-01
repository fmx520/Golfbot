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

## Supported courses

The booking links for these ship with the app, so the web UI just asks you to
pick one — there is nothing to copy and paste:

| Course | Booking URL |
|--------|-------------|
| Bethpage State Park | `https://foreupsoftware.com/index.php/booking/19765/2431#/teetimes` |
| Montauk Downs State Park | `https://foreupsoftware.com/index.php/booking/19756/2436#/teetimes` |
| Sunken Meadow State Park | `https://foreupsoftware.com/index.php/booking/19766/2437#/teetimes` |
| Crab Meadow Golf Course | `https://foreupsoftware.com/index.php/booking/21593/8314#/teetimes` |

They live in [`src/courses.py`](src/courses.py), which is the single source of
truth: every schedule and course ID the app uses is parsed out of these URLs, so
fixing a link there fixes it in the picker, in jobs, in the sniper, and in the
grid probe at once. (`config/courses.yaml` is a separate, CLI-oriented file for
per-course polling and retry defaults — it does not feed the picker.)

### The link precheck

A stale booking link looks exactly like a good one right up until the bot
searches an empty tee sheet, or races a release on the wrong course. So before
you connect, Golfbot loads each course's booking page straight from ForeUp,
unauthenticated, and confirms ForeUp still recognises the schedule and course
IDs in the link — then shows you the course name ForeUp reports back.

The Connect page shows one banner with the verdict, and a per-course detail
block with the link, the IDs, and what the check found. The check runs once at
app startup, and results are cached for 15 minutes; **Re-check** forces a fresh
round.

Three outcomes, deliberately distinguished:

| Verdict | Meaning | Effect |
|---------|---------|--------|
| **Verified** | ForeUp resolved the link and named the course. | Connect normally. |
| **Wrong link** | The page loaded, but ForeUp has no such course on that schedule. | Connect is blocked (with a "use it anyway" escape hatch) — this is real evidence the link is wrong. |
| **Unconfirmed** | ForeUp did not answer, or served a bot check. | A warning only. Nothing says the link is bad, so you can still connect. |

Run the same check from a terminal — useful as a deploy gate or a cron job:

```bash
python3 scripts/verify_course_urls.py          # table
python3 scripts/verify_course_urls.py --json   # raw report
# exit 0 = all verified, 1 = a link is wrong, 2 = could not confirm
```

Tune it with `COURSE_PREFLIGHT_TTL_SECONDS` (cache lifetime, default 900),
`COURSE_PREFLIGHT_TIMEOUT_SECONDS` (per-request timeout, default 10), and
`GOLFBOT_PREFLIGHT_ON_STARTUP=0` to skip the boot-time warm.

## Using a course that isn't listed

Pick **Other — paste a ForeUp URL** in the picker (or use the CLI). Booking URLs
look like:

```
https://foreupsoftware.com/index.php/booking/19765/2431#/teetimes
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
- Pick a supported course, with its ForeUp link prechecked (see above)
- Enter ForeUp credentials
- Paste a booking URL for any other ForeUp course
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
   It creates the `races` table and the `course_grids` table (Future Sight's
   learned tee-grid registry — see below), with indexes, and leaves RLS
   enabled with no policies.
3. Copy the **`service_role`** key from Project Settings → API. Not the `anon`
   key — RLS is on, and only `service_role` bypasses it.
4. Set these wherever the bot runs (Render → Environment, or `.env` locally):

   ```bash
   RACE_LOG_WEBHOOK_URL=https://<project>.supabase.co/rest/v1/races
   RACE_LOG_WEBHOOK_TOKEN=<the secret key>
   ```

   The key is sent as both the `apikey` and bearer headers, so there is no JSON
   to type. Use `RACE_LOG_WEBHOOK_HEADERS` (a JSON object) only for a backend
   that needs some other header set.

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

### Learning every course's tee grid (Future Sight)

Future Sight pre-stages a hold at an exact tee time, so it has to know the
times a course actually offers before it can fire — that's the `course_grids`
table above, one row per `(course_id, day_of_week)`. Normally a row is only
learned as a side effect of arming a real job on that weekday, which makes it
look like getting full coverage means running the bot on seven different
calendar days.

It doesn't. These courses release on a rolling window (many NY state courses
publish a new day each night at 7pm, always keeping ~7 days visible), so the
booking window visible **right now** already contains one date for every day
of the week — today, tomorrow, +2 ... +6 are seven consecutive dates, which
span Monday through Sunday exactly once. `scripts/harvest_grids.py` reads
those dates in a single run, for every course and every sibling teesheet on a
multi-course facility (Bethpage's Black/Red/Blue/Green/Yellow), and upserts
whatever it learns:

```bash
python3 scripts/harvest_grids.py
python3 scripts/harvest_grids.py --course bethpage --horizon-days 13
python3 scripts/harvest_grids.py --dry-run --json   # preview without writing
```

Needs `FOREUP_USERNAME`/`FOREUP_PASSWORD` and `RACE_LOG_WEBHOOK_URL` (the
Supabase sink above) — without a sink it reports what it would have learned
but writes nothing. It's safe to re-run: a date that came back booked solid
just gets another shot next time, and a shifted seasonal grid overwrites the
old row rather than duplicating it. Since a booking window rolls forward on
its own, running this weekly — a cron job, or by hand — is what keeps the
registry current; nothing about it requires the bot to be armed on any
particular day.

Check coverage and staleness with the `course_grids` query in the schema's
cookbook, or `python3 scripts/harvest_grids.py --dry-run` against a course to
see what's currently reachable without writing anything.

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
  courses.py     — Supported-course catalog + the ForeUp link precheck
config/
  courses.yaml   — Per-course settings (schedule IDs, time windows, retry profiles)
```
