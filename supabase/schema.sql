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
    prestage         jsonb,         -- Future Sight: one entry per hold fired
    two_factor       jsonb,         -- booking-code path + hold keepalive
    peers            jsonb,         -- our own concurrent jobs on this race

    aborted_reason   text,
    error            text
);


-- ---------------------------------------------------------------------------
-- Migration for a table created before v0.78
-- ---------------------------------------------------------------------------
--
-- prestage (v0.73), two_factor (v0.68) and peers (v0.78) were added to the
-- record without columns. PostgREST rejects the WHOLE insert on any unmapped
-- key, so a table missing these accepts NO races at all -- the symptom is a
-- GOLFBOT_RACE_SINK_ERROR 400 line per run and an empty history. Safe to run
-- more than once.

alter table races add column if not exists prestage   jsonb;
alter table races add column if not exists two_factor jsonb;
alter table races add column if not exists peers      jsonb;

-- Guard against this recurring: scripts/check_schema_drift.py compares the
-- record's top-level keys against the columns above and fails on any gap.

-- The two axes you actually slice on: what happened, and when.
create index if not exists races_outcome_idx    on races (outcome);
create index if not exists races_recorded_idx   on races (recorded_at desc);
create index if not exists races_created_idx    on races (created_at desc);

-- Leave RLS on with no policies. The service_role key used by the bot bypasses
-- RLS entirely, so writes keep working, while an anon key leaked from anywhere
-- else can neither read nor write this table.
alter table races enable row level security;


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
