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


def schema_columns() -> set[str]:
    """Column names from the races CREATE TABLE, ignoring the comment cookbook."""
    sql = SCHEMA.read_text(encoding="utf-8")
    match = re.search(r"create table[^(]*\(\s*(.*?)\n\);", sql, re.S | re.I)
    if not match:
        raise SystemExit(f"Could not find the races CREATE TABLE in {SCHEMA}")

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


def main() -> int:
    columns, keys = schema_columns(), record_keys()

    missing = sorted(keys - columns)
    if missing:
        print("SCHEMA DRIFT — these record fields have no column, so PostgREST")
        print("will reject every race record, not only the ones using them:\n")
        for name in missing:
            print(f"  - {name}")
        print("\nAdd them to supabase/schema.sql and run this against the live table:\n")
        for name in missing:
            print(f"  alter table races add column if not exists {name} jsonb;")
        return 1

    unused = sorted(columns - keys - {"created_at"})
    if unused:
        # Not fatal: a column the record stopped sending is harmless, but it is
        # usually a rename that lost data, so say so.
        print(f"note: columns with no matching record field: {', '.join(unused)}")

    print(f"OK — all {len(keys)} record fields have columns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
