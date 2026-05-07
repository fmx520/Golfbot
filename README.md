# Golfbot

Golf bot testing CLI with dry-run support and structured run logs.

## Usage

From the repo root (`/workspace/Golfbot`), run:

```bash
python3 golfbot_cli.py \
  --course-id pebble_beach \
  --play-date 2026-05-10 \
  --players 2 \
  --time-start 07:00 \
  --time-end 09:00 \
  --dry-run
```

Real run (will prompt for final confirmation):

```bash
python3 golfbot_cli.py \
  --course-id pebble_beach \
  --play-date 2026-05-10 \
  --players 2 \
  --time-start 07:00 \
  --time-end 09:00
```

### `--dry-run`

`--dry-run` runs discovery and interactive slot selection, then stops before the irreversible confirmation request.

## Web UI (Option A: separate worker process)

Run the web app:

```bash
cd /workspace/Golfbot
uvicorn src.web.app:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` and submit a job.

Design note: the web server starts a separate worker **process** per job, so UI rendering/request handling stays off the timing-sensitive booking loop path.

## Logs

Every run writes structured JSON to `logs/run_<id>.json` including:

- run metadata
- decision trace
- race loss points (latency by phase)
- success/confirmation state

## Analyze runs

```bash
python3 scripts/analyze_runs.py
```

The analyzer reports:

- attempt counts
- success rate
- average race loss points (where latency was spent)
