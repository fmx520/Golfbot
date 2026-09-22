# Golfbot — notes for Claude Code sessions

See `README.md` for setup, architecture, and how the bot works. This file is
about working in this specific repo, where multiple Claude Code sessions
operate independently and push straight to `main` without coordinating first.

## Start with STATE.md

`STATE.md` is the operational handoff: what currently works and the run that
proves it, how a race actually executes (the burst and the poll run
CONCURRENTLY — get this wrong and you will reintroduce a lost race), what is
open, what has already been ruled out and why, and the specific things in this
codebase that will mislead you if you reason from them naively. Read it before
diagnosing a losing race or proposing an optimisation — several conclusions in
there were reached the expensive way.

`CHANGELOG.md` says what shipped. `STATE.md` says what is true.

## Check state before assuming it

Don't assume the version, a table, or an endpoint you're about to add doesn't
already exist — another session may have shipped it earlier today. Before
starting real work:

```bash
git fetch origin main && git log --oneline -20 origin/main
cat CHANGELOG.md | head -30
```

`course_grids` (`supabase/schema.sql`) was independently fixed by two sessions
within the same day because neither checked first — see CHANGELOG.md's
"Notable gaps" section for the full story. Don't repeat it.

## Keep CHANGELOG.md current

`src/web/app.py` carries one `APP_VERSION` string. When you bump it, add one
row to `CHANGELOG.md` (newest on top): version, date, a one-line summary, the
short commit hash. That file is the fast path for the next session (or a
human) to see what recently shipped without replaying `git log` — it only
works if every session that bumps the version actually adds the row.

## Before pushing to `main`

- Re-fetch `origin/main` immediately before merging — it moves fast in this
  repo, sometimes within the same hour.
- Run `python3 scripts/check_schema_drift.py` after touching
  `RaceRecorder.as_dict` or `supabase/schema.sql` — a race/grid record with a
  field the table has no column for makes PostgREST reject the *entire*
  write, not just that field.
- Prefer a plain merge/fast-forward over rewriting history — several sessions
  may have work in flight on `main` at once.
