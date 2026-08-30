#!/usr/bin/env python3
"""Fail if the race record has a top-level key the races table cannot store.

PostgREST maps each top-level JSON key to a column and rejects the WHOLE insert
on any key it cannot place. So a single new field on the record silently ends
all telemetry — every race, not just the ones using the new field. That is how
`two_factor` (v0.68) and `prestage` (v0.73) went in without columns and took
the history down with them.

    python3 scripts/check_schema_drift.py

Run it after touching RaceRecorder.as_dict or supabase/schema.sql.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.telemetry.race_log import RaceRecorder  # noqa: E402

SCHEMA = ROOT / "supabase" / "schema.sql"

# What grid_store.upsert_grid POSTs. Kept here rather than imported because the
# real call builds the row inside a function; this is the contract it must meet.
GRID_ROW_KEYS = {
    "course_id", "day_of_week", "interval_minutes", "anchor_phase", "first_time",
    "last_time", "observed_count", "confidence", "template", "source_date",
}


def schema_columns(table: str) -> set[str]:
    """Column names from a CREATE TABLE, ignoring the comment cookbook."""
    sql = SCHEMA.read_text(encoding="utf-8")
    match = re.search(
        rf"create table (?:if not exists )?{table}\s*\(\s*(.*?)\n\);", sql, re.S | re.I
    )
    if not match:
        raise SystemExit(f"Could not find the {table} CREATE TABLE in {SCHEMA}")

    columns: set[str] = set()
    for line in match.group(1).splitlines():
        line = line.strip()
        # Skip blanks, comment-only lines, and table constraints.
        if not line or line.startswith("--"):
            continue
        name = line.split()[0]
        if name.lower() in {"primary", "unique", "constraint", "foreign", "check"}:
            continue
        columns.add(name)
    return columns


def record_keys() -> set[str]:
    """Top-level keys a real race actually POSTs."""
    return set(RaceRecorder(run_id="drift-check").as_dict())


def check(table: str, keys: set[str], ignore: set[str], kind: str) -> int:
    columns = schema_columns(table)

    missing = sorted(keys - columns)
    if missing:
        print(f"SCHEMA DRIFT in {table} — these {kind} fields have no column, so")
        print("PostgREST will reject every write, not only the ones using them:\n")
        for name in missing:
            print(f"  - {name}")
        print(f"\nAdd them to supabase/schema.sql and run against the live table:\n")
        for name in missing:
            print(f"  alter table {table} add column if not exists {name} jsonb;")
        return 1

    unused = sorted(columns - keys - ignore)
    if unused:
        # Not fatal: a column nothing writes is harmless, but it is usually a
        # rename that lost data, so say so.
        print(f"note: {table} columns with no matching field: {', '.join(unused)}")

    print(f"OK — all {len(keys)} {kind} fields have columns in {table}.")
    return 0


def main() -> int:
    failures = check("races", record_keys(), {"created_at"}, "race record")
    # course_grids existed only in grid_store.py for its whole life: the table
    # was never in schema.sql, so Future Sight's fallback could never fire.
    failures += check("course_grids", GRID_ROW_KEYS, {"updated_at"}, "grid row")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
