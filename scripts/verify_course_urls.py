#!/usr/bin/env python3
"""Check every built-in course URL against ForeUp and report what it found.

Same check the web UI runs on the Connect page, but from a terminal and with
exit codes, so it can gate a deploy or run on a schedule:

    python3 scripts/verify_course_urls.py
    python3 scripts/verify_course_urls.py --json

Exit codes:
    0  every link verified
    1  ForeUp actively rejected a link — the catalog in src/courses.py is wrong
    2  a link could not be confirmed (ForeUp unreachable, or a bot check)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.courses import CONCLUSIVELY_BAD, STATUS_OK, run_preflight  # noqa: E402

MARKS = {"ok": "PASS", "mismatch": "FAIL", "malformed": "FAIL"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="emit the raw report instead of a table")
    args = parser.parse_args()

    report = run_preflight(force=True)

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(f"Checking {len(report.checks)} course links against ForeUp\n")
        for check in report.checks:
            mark = MARKS.get(check.status, "WARN")
            print(f"  [{mark}] {check.name}")
            print(f"         {check.booking_url}")
            print(f"         schedule {check.schedule_id} · course {check.course_id} · {check.latency_ms:.0f}ms")
            print(f"         {check.detail}")
            if check.status != STATUS_OK and check.teesheets:
                for sheet in check.teesheets:
                    print(f"           - {sheet['name']} ({sheet['id']})")
            print()
        print(report.summary)

    if any(c.status in CONCLUSIVELY_BAD for c in report.checks):
        return 1
    return 0 if report.all_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
