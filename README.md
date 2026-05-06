# Golfbot

Golf bot testing CLI with dry-run support and structured run logs.

## Usage

```bash
python3 golfbot_cli.py --dry-run
python3 golfbot_cli.py
```

### `--dry-run`

`--dry-run` runs full discovery and booking preparation, emits timing/decision trace,
and stops before the irreversible confirmation request.

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
