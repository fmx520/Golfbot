"""Make httpcore's HTTP/2 connection safe to share between threads.

Future Sight fires every pre-staged hold at once from a thread pool, and polling
runs on another thread, all over ONE HTTP/2 connection (the shared httpx client).
httpcore's sync HTTP/2 code is not safe for that. Reading
httpcore/_sync/http2.py (1.0.9) shows three unguarded races on the shared
h2.connection.H2Connection, which does no locking of its own:

1. Header compression. `send_headers` HPACK-encodes into a table both ends must
   keep in lockstep. Two threads encoding at once corrupt it.
   10-17: "deque mutated during iteration" -- one hold never left.
2. Every other state change (body frames, end-of-stream, flow-control acks,
   incoming frames) is likewise unguarded; only the socket read and the socket
   write are locked.
3. Stream ids. `get_next_available_stream_id()` does not reserve the id -- h2
   only does that when headers are sent -- so two threads can take the same id,
   and the second set of headers lands on an open stream as "trailers". The
   server calls that a protocol error and kills the connection.
   10-21: ConnectionTerminated(error_code=1) at T+1.35s, six in-flight holds
   dead with "Broken pipe". Polling and the booking share that connection.

A local reproduction (12 threads, one connection, a strict h2 server) hits all
three -- including requests that ARRIVED carrying another request's headers.

The guard wraps each connection's H2Connection so that:
  * every method call on it runs under one per-connection lock. h2 does no I/O,
    so these are microsecond in-memory steps and nothing waits on the network
    while holding the lock -- the socket reads and writes stay outside it;
  * taking a stream id, sending that stream's headers and opening its flow
    window is one uninterrupted step, so ids are unique, opened in order, and
    a stream cannot be answered and closed by the server before its own setup
    has finished (a fourth race the reproduction turned up).

Nothing about what goes on the wire changes: same connection, same protocol,
same requests, same parallelism. Frames just never get scrambled.

It patches httpcore internals, so it checks they are the shape it was written
against and reports (rather than silently skipping) when they are not. The
versions it was verified with are pinned in requirements.txt.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False


class _SerialisedH2:
    """Proxy for an h2 H2Connection: every call holds the connection's lock."""

    __slots__ = ("_golfbot_inner", "_golfbot_lock", "_golfbot_local")

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_golfbot_inner", inner)
        object.__setattr__(self, "_golfbot_lock", threading.RLock())
        object.__setattr__(self, "_golfbot_local", threading.local())

    # -- stream-id allocation: hold the lock from "take an id" to "send its headers"
    def get_next_available_stream_id(self) -> int:
        self._golfbot_lock.acquire()
        try:
            stream_id = self._golfbot_inner.get_next_available_stream_id()
        except BaseException:
            self._golfbot_lock.release()
            raise
        self._golfbot_local.held = getattr(self._golfbot_local, "held", 0) + 1
        return stream_id

    def _golfbot_release_allocation(self) -> None:
        held = getattr(self._golfbot_local, "held", 0)
        if held:
            self._golfbot_local.held = held - 1
            self._golfbot_lock.release()

    def increment_flow_control_window(self, *args: Any, **kwargs: Any) -> Any:
        # httpcore's _send_request_headers is: send_headers(stream) ->
        # increment_flow_control_window(stream) -> write. If the allocation were
        # released after send_headers, ANOTHER thread's write could flush this
        # stream's headers, the server could answer and close it, and the
        # reader would drop it before this call -- KeyError on the stream id.
        # So the hold runs through the per-stream window update and ends here,
        # still before any socket I/O.
        try:
            with self._golfbot_lock:
                return self._golfbot_inner.increment_flow_control_window(*args, **kwargs)
        finally:
            if kwargs.get("stream_id") is not None:
                self._golfbot_release_allocation()

    # -- everything else: one call at a time
    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._golfbot_inner, name)
        if not callable(attr):
            return attr
        lock = self._golfbot_lock

        def _call(*args: Any, **kwargs: Any) -> Any:
            with lock:
                return attr(*args, **kwargs)

        return _call

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._golfbot_inner, name, value)


def install() -> bool:
    """Install the guard on httpcore's sync HTTP/2 connection. Idempotent.

    Returns True when installed. Never raises: if httpcore's internals are not
    the shape this was written against, it logs an error naming what is
    missing and the bot runs exactly as it did before.
    """
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from httpcore._sync.http2 import HTTP2Connection
    except Exception as exc:  # noqa: BLE001
        logger.error("h2_guard_not_installed", extra={"reason": f"import failed: {exc}"})
        return False

    missing = [n for n in ("__init__", "_send_request_headers", "handle_request")
               if not hasattr(HTTP2Connection, n)]
    if missing:
        logger.error("h2_guard_not_installed", extra={"reason": f"httpcore lacks {missing}"})
        return False

    original_init = HTTP2Connection.__init__
    original_send_headers = HTTP2Connection._send_request_headers

    def guarded_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        state = getattr(self, "_h2_state", None)
        if state is None:
            logger.error("h2_guard_not_installed",
                         extra={"reason": "HTTP2Connection has no _h2_state after __init__"})
            return
        if not isinstance(state, _SerialisedH2):
            self._h2_state = _SerialisedH2(state)

    def guarded_send_request_headers(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return original_send_headers(self, *args, **kwargs)
        finally:
            # If building the headers failed before send_headers ran, the id
            # allocation is still held -- release it so the connection lives.
            state = getattr(self, "_h2_state", None)
            if isinstance(state, _SerialisedH2):
                state._golfbot_release_allocation()

    _ORIGINALS["__init__"] = original_init
    _ORIGINALS["_send_request_headers"] = original_send_headers
    HTTP2Connection.__init__ = guarded_init
    HTTP2Connection._send_request_headers = guarded_send_request_headers
    _INSTALLED = True
    logger.info("h2_guard_installed")
    return True


def installed() -> bool:
    return _INSTALLED


_ORIGINALS: dict[str, Any] = {}


def _uninstall_for_tests() -> None:
    """Put httpcore back as shipped. Tests only: proves the race reproduces."""
    global _INSTALLED
    if not _INSTALLED:
        return
    from httpcore._sync.http2 import HTTP2Connection
    for name, fn in _ORIGINALS.items():
        setattr(HTTP2Connection, name, fn)
    _INSTALLED = False
