"""The sink must never lose a whole race to one unmapped column.

PostgREST rejects the ENTIRE insert on any unmapped key, so shipping a new
telemetry field before its migration lands loses every race record until
someone notices. It has happened to this project before.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.telemetry.race_log as rl  # noqa: E402

FAILURES = 0


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


def _pgrst(col: str) -> _Resp:
    return _Resp(400, '{"code":"PGRST204","message":"Could not find the '
                      f"'{col}'" + ' column of \'races\' in the schema cache"}')


def _run(reject: list[str]) -> tuple[dict, list[dict]]:
    """Fake a PostgREST that rejects each column in `reject`, one at a time."""
    sent: list[dict] = []

    class FakeHttpx:
        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            sent.append(dict(json))
            for col in reject:
                if col in json:
                    return _pgrst(col)
            return _Resp(201)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def fake_import(name, *a, **kw):
        if name == "httpx":
            return FakeHttpx
        return real_import(name, *a, **kw)

    import builtins
    builtins.__import__ = fake_import
    try:
        os.environ["RACE_LOG_WEBHOOK_URL"] = "https://example.invalid/races"
        status = rl._post_to_sink({"run_id": "r1", "outcome": "lost",
                                   "prestage_decision": [{"stage": "x"}],
                                   "peers": {"count": 1}})
    finally:
        builtins.__import__ = real_import
    return status, sent


def check(name, cond, detail=""):
    global FAILURES
    if cond:
        print(f"  ok: {name}")
    else:
        FAILURES += 1
        print(f"FAIL: {name} {detail}")


def test_missing_column_is_dropped_and_record_saved():
    status, sent = _run(reject=["prestage_decision"])
    check("record still saved", status.get("ok") is True, status)
    check("offending column dropped", status.get("dropped_columns") == ["prestage_decision"], status)
    check("retry omitted the column", "prestage_decision" not in sent[-1], sent[-1])
    check("everything else survived",
          sent[-1].get("run_id") == "r1" and "peers" in sent[-1], sent[-1])


def test_two_missing_columns():
    status, sent = _run(reject=["prestage_decision", "peers"])
    check("both dropped", set(status.get("dropped_columns") or []) == {"prestage_decision", "peers"}, status)
    check("record still saved", status.get("ok") is True, status)


def test_non_drift_error_is_not_retried_forever():
    def _boom(url, json=None, headers=None, timeout=None):
        return _Resp(500, "upstream exploded")
    import builtins
    real = builtins.__import__

    class FakeHttpx:
        post = staticmethod(_boom)

    builtins.__import__ = lambda n, *a, **k: FakeHttpx if n == "httpx" else real(n, *a, **k)
    try:
        os.environ["RACE_LOG_WEBHOOK_URL"] = "https://example.invalid/races"
        status = rl._post_to_sink({"run_id": "r1"})
    finally:
        builtins.__import__ = real
    check("non-drift failure reported, not looped", status.get("ok") is False
          and status.get("status_code") == 500, status)


if __name__ == "__main__":
    for fn in (test_missing_column_is_dropped_and_record_saved,
               test_two_missing_columns,
               test_non_drift_error_is_not_retried_forever):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
