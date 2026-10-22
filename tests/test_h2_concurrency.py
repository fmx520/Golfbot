"""Many threads, one HTTP/2 connection: requests must never be scrambled.

Future Sight fires every pre-staged hold at once from a thread pool, with
polling on another thread, all over one shared HTTP/2 connection. httpcore
does not make that safe (see src/discovery/h2_guard.py), and it cost us twice:

* 2026-10-17: "deque mutated during iteration" -- a hold never left;
* 2026-10-21: ConnectionTerminated(error_code=1) at T+1.35s, six in-flight
  holds dead with "Broken pipe" -- the winning hold had landed 0.8s earlier.

This runs the bot's real HTTP stack (httpx, same flags as the shared client)
against a local, strict HTTP/2 server -- the h2 library on the server side, so
it rejects exactly what a real server rejects. Each request carries an id in
two headers; if header compression is scrambled, they arrive mismatched.

  1. With the guard removed, the harness must reproduce the failure. If it
     does not, the test is not exercising the race and says so.
  2. With the guard, GETs and hold-like form POSTs from 12 threads: zero
     connection kills, zero garbled requests, zero client errors.
  3. With the guard, requests still run in parallel: six requests against a
     server that takes 300ms each finish in about 300ms, not 1.8s.

No network: everything is on 127.0.0.1.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GOLFBOT_LOG_DIR", "/tmp/golfbot-test-logs")

import h2.config  # noqa: E402
import h2.connection  # noqa: E402
import h2.events  # noqa: E402
import h2.exceptions  # noqa: E402
import httpx  # noqa: E402

import src.discovery.foreup_client  # noqa: E402,F401  -- installs the guard, as in production
from src.discovery import h2_guard  # noqa: E402

FAILURES = 0


def _fail(msg: str) -> None:
    global FAILURES
    FAILURES += 1
    print(f"  FAIL: {msg}")


class StrictH2Server:
    """Minimal HTTP/2 server. Kills the connection on any protocol error, as a
    real server does, and checks each request's id headers match."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.stats = {"protocol_errors": 0, "garbled": 0, "requests": 0}
        self._lock = threading.Lock()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/"
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.sock.close()

    def _accept(self) -> None:
        while True:
            try:
                s, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(s,), daemon=True).start()

    def _handle(self, s: socket.socket) -> None:
        conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
        send_lock = threading.Lock()   # server side: one writer at a time
        conn.initiate_connection()
        s.sendall(conn.data_to_send())
        pending: dict[int, dict] = {}

        def respond(stream_id: int) -> None:
            with send_lock:
                try:
                    conn.send_headers(stream_id, [(":status", "200")], end_stream=True)
                    s.sendall(conn.data_to_send())
                except (h2.exceptions.StreamClosedError, h2.exceptions.ProtocolError, OSError):
                    pass

        try:
            while True:
                data = s.recv(65535)
                if not data:
                    return
                with send_lock:
                    try:
                        events = conn.receive_data(data)
                    except h2.exceptions.ProtocolError:
                        with self._lock:
                            self.stats["protocol_errors"] += 1
                        s.sendall(conn.data_to_send())
                        s.close()
                        return
                    s.sendall(conn.data_to_send())
                for ev in events:
                    if isinstance(ev, h2.events.RequestReceived):
                        hdrs = dict(ev.headers)
                        with self._lock:
                            self.stats["requests"] += 1
                            if hdrs.get(b"x-req") != hdrs.get(b"x-req-check"):
                                self.stats["garbled"] += 1
                        pending[ev.stream_id] = hdrs
                    elif isinstance(ev, h2.events.DataReceived):
                        with send_lock:
                            conn.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
                            s.sendall(conn.data_to_send())
                    elif isinstance(ev, h2.events.StreamEnded):
                        if self.delay:
                            threading.Timer(self.delay, respond, args=(ev.stream_id,)).start()
                        else:
                            respond(ev.stream_id)
        except OSError:
            return


def _burst(rounds: int, threads: int, delay: float = 0.0) -> dict:
    """Fire `threads` requests at once, `rounds` times, each round on one fresh
    HTTP/2 connection -- the shape of a Future Sight burst plus polling."""
    server = StrictH2Server(delay=delay)
    errors: dict[str, int] = {}
    ok = [0]
    lock = threading.Lock()
    old_switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # interleave the threads as hard as the GIL allows
    try:
        for r in range(rounds):
            client = httpx.Client(http1=False, http2=True, timeout=10)
            client.get(server.url)  # open the connection, like the pre-release re-warm
            barrier = threading.Barrier(threads)

            def fire(i: int) -> None:
                v = f"{r}-{i}-" + "x" * (i * 7)
                headers = {"x-req": v, "x-req-check": v, f"x-extra-{i}": v}
                barrier.wait()
                try:
                    if i % 2:
                        # a hold is a form-encoded POST: exercises the body path too
                        client.post(server.url, headers=headers,
                                    data={"time": f"2026-10-28 07:{i:02d}", "players": "2"})
                    else:
                        client.get(server.url, headers=headers)
                    with lock:
                        ok[0] += 1
                except Exception as exc:  # noqa: BLE001
                    key = f"{type(exc).__name__}: {str(exc)[:60]}"
                    with lock:
                        errors[key] = errors.get(key, 0) + 1

            ts = [threading.Thread(target=fire, args=(i,)) for i in range(threads)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            client.close()
    finally:
        sys.setswitchinterval(old_switch)
        server.close()
    return {"ok": ok[0], "errors": errors, **server.stats}


def test_guard_is_installed_on_import() -> None:
    if not h2_guard.installed():
        return _fail("importing the ForeUp client did not install the HTTP/2 guard")
    print("  ok: guard installed by importing src.discovery.foreup_client")


def test_without_guard_the_race_reproduces() -> None:
    h2_guard._uninstall_for_tests()
    try:
        res = _burst(rounds=10, threads=12)
    finally:
        h2_guard.install()
    broken = res["protocol_errors"] + res["garbled"] + sum(res["errors"].values())
    if broken == 0:
        return _fail("without the guard nothing broke -- this harness is not exercising "
                     "the race, so a pass below would prove nothing")
    top = sorted(res["errors"].items(), key=lambda kv: -kv[1])[:3]
    print(f"  ok: unguarded, {res['ok']}/{10 * 12} requests survived; server killed "
          f"{res['protocol_errors']} connections, {res['garbled']} arrived garbled; "
          f"top client errors {top}")


def test_with_guard_nothing_is_scrambled() -> None:
    res = _burst(rounds=30, threads=12)
    total = 30 * 12
    if res["protocol_errors"] or res["garbled"] or res["errors"] or res["ok"] != total:
        return _fail(f"guarded burst: {res['ok']}/{total} ok, {res['protocol_errors']} "
                     f"connection kills, {res['garbled']} garbled, errors {res['errors']}")
    print(f"  ok: guarded, {res['ok']}/{total} GETs and form POSTs from 12 threads on one "
          f"connection -- 0 connection kills, 0 garbled")


def test_with_guard_requests_still_run_in_parallel() -> None:
    delay, n = 0.3, 6
    started = time.perf_counter()
    res = _burst(rounds=1, threads=n, delay=delay)
    elapsed = time.perf_counter() - started
    if res["errors"] or res["ok"] != n:
        return _fail(f"parallel check had errors: {res}")
    # one warm-up request + one parallel round ~= 2 x delay; serialised would be (1+n) x delay
    if elapsed > delay * 3:
        return _fail(f"{n} requests at {delay}s each took {elapsed:.2f}s -- they were "
                     f"serialised, not sent in parallel")
    print(f"  ok: {n} requests against a {delay}s server finished in {elapsed:.2f}s "
          f"(serialised would be ~{delay * (n + 1):.1f}s)")


if __name__ == "__main__":
    for fn in (test_guard_is_installed_on_import, test_without_guard_the_race_reproduces,
               test_with_guard_nothing_is_scrambled, test_with_guard_requests_still_run_in_parallel):
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'PASS' if FAILURES == 0 else str(FAILURES) + ' FAILURE(S)'}")
    sys.exit(1 if FAILURES else 0)
