-- Golfbot race telemetry table.
--
-- Run this once in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
--
-- Shape note: the sink POSTs the race record straight to PostgREST, which maps
-- each top-level JSON key to a column of the same name and errors on any key it
-- cannot place. So every top-level field of the record needs a column here, even
-- the ones you never query.

create table if not exists races (
    -- Identity. run_id is generated per race and is the natural primary key.
    run_id           text primary key,
    job_id           text,
    kind             text,
    mode             text,          -- 'sniper' (timed release) or 'snipe_now'

    -- When the bot says the race happened. created_at is when we received it,
    -- which is the one to trust if a container clock ever drifts.
    recorded_at      timestamptz,
    created_at       timestamptz not null default now(),

    -- What was being raced for. release_at and play_date stay text: the bot can
    -- emit an empty string for them, which would fail a timestamptz cast and
    -- reject the whole insert. Cast in queries when you need to.
    course_ids       jsonb,
    play_date        text,
    release_at       text,

    -- The verdict.
    outcome          text,          -- won | late_start | blocked | lost_at_submit
                                    -- | never_saw_slot | found_not_booked
                                    -- | aborted | error
    summary          text,          -- one-line human-readable version
    success          boolean,

    -- Detail. jsonb so the schema does not have to track every field we add.
    timing           jsonb,         -- first_poll_offset_ms lives here
    polls            jsonb,
    attempts         jsonb,
    captcha          jsonb,

    aborted_reason   text,
    error            text
);

-- The two axes you actually slice on: what happened, and when.
create index if not exists races_outcome_idx    on races (outcome);
create index if not exists races_recorded_idx   on races (recorded_at desc);
create index if not exists races_created_idx    on races (created_at desc);

-- Leave RLS on with no policies. The service_role key used by the bot bypasses
-- RLS entirely, so writes keep working, while an anon key leaked from anywhere
-- else can neither read nor write this table.
alter table races enable row level security;


-- ---------------------------------------------------------------------------
-- Per-course tee-grid registry (src/telemetry/grid_store.py)
-- ---------------------------------------------------------------------------
--
-- Future Sight needs to know which exact tee times a course offers before it
-- can pre-stage holds against them. That's learned from a teesheet that is
-- already visible — one row per (course_id, day_of_week), so a Saturday sheet
-- doesn't get overwritten by a Tuesday read of the same course. The sink
-- reuses RACE_LOG_WEBHOOK_URL, swapping /races for /course_grids, so no extra
-- env var is needed once the races table above is wired up.

create table if not exists course_grids (
    course_id         text not null,   -- ForeUp teesheet id (the URL's second number)
    day_of_week       int  not null,   -- Python date.weekday(): Monday=0 .. Sunday=6

    interval_minutes  int,             -- inferred tee interval, e.g. 8 or 10
    anchor_phase      int,             -- (any observed time) mod interval_minutes
    first_time        text,
    last_time         text,
    observed_count    int,
    confidence        text,            -- none | low | medium | high

    -- One real slot's raw ForeUp payload, for shaping a pre-staged hold's body.
    template          jsonb,

    -- The date this grid was read from, and when. source_date is a display
    -- field (kept text, like races.play_date, for the same reason: never fail
    -- the insert over a cast). updated_at is what tells you the row is stale.
    source_date       text,
    updated_at        timestamptz not null default now(),

    primary key (course_id, day_of_week)
);

-- upsert_grid() POSTs with ?on_conflict=course_id,day_of_week — the primary
-- key above is exactly what PostgREST needs to resolve that.

alter table course_grids enable row level security;

-- upsert_grid() never sends updated_at, so the column default only fires on
-- the very first insert — a later re-learn of the same course/day would
-- otherwise leave updated_at frozen at that first write, making every row
-- look equally stale forever. This keeps it honest on every write.
create or replace function course_grids_touch_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists course_grids_set_updated_at on course_grids;
create trigger course_grids_set_updated_at
    before insert or update on course_grids
    for each row execute function course_grids_touch_updated_at();


-- ---------------------------------------------------------------------------
-- Query cookbook
-- ---------------------------------------------------------------------------

-- Where are we losing, over the last 30 days?
--
--   select outcome, count(*)
--   from races
--   where created_at > now() - interval '30 days'
--   group by outcome
--   order by count(*) desc;

-- Did the late-start fix hold? Negative means we were already polling when the
-- release landed, which is what we want.
--
--   select run_id,
--          recorded_at,
--          outcome,
--          (timing->>'first_poll_offset_ms')::numeric as first_poll_offset_ms,
--          (timing->>'rewarm_ms')::numeric            as rewarm_ms,
--          timing->>'rewarm_overran_release'          as overran
--   from races
--   where mode = 'sniper'
--   order by recorded_at desc
--   limit 25;

-- Are we being blocked without noticing? Any non-zero count here means polls
-- were rejected rather than coming back empty.
--
--   select date_trunc('day', created_at) as day,
--          sum((polls->>'antibot')::int) as antibot_polls,
--          sum((polls->>'count')::int)   as total_polls
--   from races
--   group by 1 order by 1 desc;

-- When we reach submit, how long does it take, and does it correlate with
-- losing? This is the question to ask once late_start stops showing up.
--
--   select outcome,
--          count(*),
--          round(avg((timing->>'submit_to_confirm_ms')::numeric)) as avg_submit_ms
--   from races
--   where timing->>'submit_to_confirm_ms' is not null
--   group by outcome;

-- Which slots did we try and lose? Unnests the per-attempt detail.
--
--   select r.run_id, r.recorded_at, a->>'slot_time' as slot, a->>'error' as err
--   from races r, jsonb_array_elements(r.attempts) a
--   where a->>'result' = 'failed'
--   order by r.recorded_at desc;

-- Races armed but never recorded cannot be found here by definition — that is
-- the free-plan spin-down signature. Compare the count below against how many
-- times you armed the sniper.
--
--   select date_trunc('day', created_at) as day, count(*)
--   from races group by 1 order by 1 desc;

-- Future Sight post-mortem: what happened to each pre-staged fire in the last
-- release, in firing order, with the offset from the release instant.
--
--   select r.run_id,
--          p->>'time'   as target,
--          p->>'result' as result,
--          (p->>'latency_ms')::numeric as hold_ms,
--          round(extract(epoch from
--              (p->>'at')::timestamptz - (r.release_at)::timestamptz
--          ) * 1000) as fired_at_offset_ms,
--          left(p->>'error', 80) as error
--   from races r, jsonb_array_elements(r.prestage) p
--   where jsonb_array_length(r.prestage) > 0
--   order by r.created_at desc, (p->>'at')::timestamptz
--   limit 40;

-- Did pre-staging beat polling? Compare how races end with and without it.
--
--   select (jsonb_array_length(coalesce(prestage,'[]')) > 0) as prestaged,
--          outcome, count(*)
--   from races group by 1, 2 order by 1 desc, 3 desc;

-- Grid coverage: which course/weekday pairs are learned, and how stale.
-- scripts/harvest_grids.py should keep this at 0 missing across every day.
--
--   select course_id, day_of_week, interval_minutes, confidence,
--          source_date, now() - updated_at as age
--   from course_grids
--   order by course_id, day_of_week;
