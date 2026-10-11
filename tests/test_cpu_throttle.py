"""CPU-throttle logging: reads both cgroup layouts, never breaks a race.

This is measurement only. It exists to settle, with a number, whether the
0.15-CPU free plan slows us down at the release instant (see STATE.md). The
properties that matter: it reads what Render might give us (cgroup v1 or v2),
it degrades to None rather than raising where the counters are missing, and
the result rides inside `timing` so the races table needs no new column.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.telemetry import race_log  # noqa: E402
from src.telemetry.race_log import RaceRecorder, cpu_throttle_delta, read_cpu_throttle  # noqa: E402

FAILURES = 0


def _fail(msg: str) -> None:
    global FAILURES
    FAILURES += 1
    print(f"  FAIL: {msg}")


def _write(dirpath: Path, name: str, text: str) -> str:
    p = dirpath / name
    p.write_text(text)
    return str(p)


def test_reads_cgroup_v2() -> None:
    with tempfile.TemporaryDirectory() as d:
        stat = _write(Path(d), "cpu.stat",
                      "usage_usec 5000000\nuser_usec 4000000\nsystem_usec 1000000\n"
                      "nr_periods 1200\nnr_throttled 7\nthrottled_usec 350000\n")
        snap = read_cpu_throttle(v2_stat=stat, v1_stats=(), v1_usage=())
    want = {"periods": 1200, "throttled_periods": 7, "throttled_usec": 350000, "usage_usec": 5000000}
    if snap != want:
        return _fail(f"v2 read {snap}, wanted {want}")
    print("  ok: cgroup v2 counters read")


def test_reads_cgroup_v1() -> None:
    with tempfile.TemporaryDirectory() as d:
        stat = _write(Path(d), "cpu.stat",
                      "nr_periods 900\nnr_throttled 3\nthrottled_time 120000000\n")
        usage = _write(Path(d), "cpuacct.usage", "8000000000\n")
        snap = read_cpu_throttle(v2_stat=str(Path(d) / "absent"), v1_stats=(stat,), v1_usage=(usage,))
    # v1 reports nanoseconds; we store microseconds.
    want = {"periods": 900, "throttled_periods": 3, "throttled_usec": 120000, "usage_usec": 8000000}
    if snap != want:
        return _fail(f"v1 read {snap}, wanted {want}")
    print("  ok: cgroup v1 counters read (ns converted)")


def test_missing_counters_is_none_not_an_error() -> None:
    snap = read_cpu_throttle(v2_stat="/nonexistent/cpu.stat", v1_stats=("/nonexistent/a",), v1_usage=())
    if snap is not None:
        return _fail(f"expected None with no counters, got {snap}")
    if cpu_throttle_delta(None, {"periods": 1}, 1.0) is not None:
        return _fail("a delta against a missing baseline must be None")
    print("  ok: no counters -> None, no exception")


def test_delta_in_readable_units() -> None:
    before = {"periods": 100, "throttled_periods": 2, "throttled_usec": 10000, "usage_usec": 1000000}
    after = {"periods": 160, "throttled_periods": 9, "throttled_usec": 95500, "usage_usec": 1450000}
    got = cpu_throttle_delta(before, after, 6.004)
    want = {"seconds": 6.0, "periods": 60, "throttled_periods": 7, "throttled_ms": 85.5, "cpu_used_ms": 450.0}
    if got != want:
        return _fail(f"delta {got}, wanted {want}")
    print("  ok: delta -> 7 throttled periods, 85.5ms throttled, 450ms CPU over 6s")


def test_rides_inside_timing() -> None:
    """Recorded windows appear under timing.cpu_throttle, not a new column."""
    snaps = iter([
        {"periods": 10, "throttled_periods": 0, "throttled_usec": 0},
        {"periods": 60, "throttled_periods": 4, "throttled_usec": 40000},
    ])
    real = race_log.read_cpu_throttle
    race_log.read_cpu_throttle = lambda *a, **k: next(snaps)
    try:
        race = RaceRecorder(run_id="t")
        race.mark_cpu_baseline()
        race.record_cpu_window("release")
    finally:
        race_log.read_cpu_throttle = real
    record = race.as_dict()
    if "cpu_throttle" in record:
        return _fail("cpu_throttle leaked to a top-level key -- that would need a column")
    window = (record["timing"].get("cpu_throttle") or {}).get("release")
    if not window or window["throttled_periods"] != 4 or window["throttled_ms"] != 40.0:
        return _fail(f"timing.cpu_throttle.release wrong: {record['timing'].get('cpu_throttle')}")
    print("  ok: timing.cpu_throttle.release recorded inside timing")


def test_never_raises_into_the_race() -> None:
    real = race_log.read_cpu_throttle

    def boom(*a, **k):
        raise OSError("cgroup gone")
    race = RaceRecorder(run_id="t")
    race._cpu_baseline = {"periods": 0, "throttled_periods": 0, "throttled_usec": 0}
    race_log.read_cpu_throttle = boom
    try:
        race.record_cpu_window("release")   # must not raise
    except Exception as exc:  # noqa: BLE001
        return _fail(f"record_cpu_window raised: {exc}")
    finally:
        race_log.read_cpu_throttle = real
    print("  ok: a failing read is swallowed")


if __name__ == "__main__":
    for fn in (test_reads_cgroup_v2, test_reads_cgroup_v1, test_missing_counters_is_none_not_an_error,
               test_delta_in_readable_units, test_rides_inside_timing, test_never_raises_into_the_race):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
