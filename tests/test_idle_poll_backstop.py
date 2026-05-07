"""Tests for the IDLE belt-and-braces periodic poll.

Without the poll, an IMAP IDLE missed-push (observed in production
on Gmail) waits up to 27 minutes for the next IDLE refresh to
catch the message. With the poll, the worst-case latency is the
poll interval (default 60s).

What's tested here:

- `InboxConfig.poll_interval_seconds` defaults to 60 and accepts 0
  (disable).
- `_catch_up` accepts a `wake_reason` keyword and emits the
  `poll_caught_missed_push` warn-level event when a poll-driven
  catchup actually finds new mail (i.e. IDLE silently dropped it).
- The same wake_reason="poll" path does NOT emit the event when
  there's nothing new to process — emitting on every poll would
  be noise.
- A non-poll wake_reason ("activity", "refresh", "startup") never
  emits the warn-level event, even if catchup processed mail.

The IDLE race itself is not unit-tested here — that's an asyncio
race over a real-shaped IMAP client and would require a
substantially heavier fake. The race semantics are covered
separately in production (the empirical evidence is the
`idle_poll` and `poll_caught_missed_push` events showing up in
the daemon's JSONL log when the poll fires).
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from daemon.config import (
    Config, Contact, DaemonConfig, InboxConfig,
)
from daemon.inbox_watcher import InboxWatcher
from daemon.log import JSONLLogger
from daemon.state import State


# ---- Reused stub IMAP from test_catchup_dedup ----------------------------


class _StubResponse:
    def __init__(self, result: str = "OK"):
        self.result = result
        self.lines: list[bytes] = []


class _StubIMAP:
    def __init__(
        self,
        *,
        search_replies: list[tuple[str, list[bytes]]],
        headers_by_uid: dict[str, bytes],
    ) -> None:
        self.searches: list[str] = []
        self._search_replies = list(search_replies)
        self._headers_by_uid = headers_by_uid
        self.fetched_uids: list[str] = []

    async def uid_search(self, query: str):
        self.searches.append(query)
        if not self._search_replies:
            return ("OK", [b""])
        result, uids = self._search_replies.pop(0)
        return (result, [b" ".join(uids)] if uids else [b""])

    async def uid(self, verb: str, uid: str, spec: str):
        assert verb == "fetch"
        self.fetched_uids.append(uid)
        if uid not in self._headers_by_uid:
            return ("NO", [])
        blob = self._headers_by_uid[uid]
        return ("OK", [
            f"1 FETCH (UID {uid} BODY[HEADER] {{{len(blob)}}}".encode("ascii"),
            bytearray(blob),
            b")",
            b"Success",
        ])


def _header_blob(message_id: str, *, from_addr: str = "me@example.com") -> bytes:
    return (
        f"Authentication-Results: mx.google.com; dmarc=pass header.from={from_addr.split('@')[1]}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"From: {from_addr}\r\n"
        f"Subject: ping\r\n"
        f"\r\n"
    ).encode("ascii")


def _make_watcher(tmp_path: Path) -> tuple[InboxWatcher, State, Path]:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    state = State(db_path=tmp_path / "state.db")
    principal = Contact(
        contact_id="principal",
        addresses=("me@example.com",),
        display_name="Me",
        relationship="Administrator",
        daily_limit=-1,
        is_principal=True,
        inboxes=("nightjar",),
    )
    inbox = InboxConfig(
        name="nightjar",
        enabled=True,
        imap_host="imap.example.com",
        imap_port=993,
        imap_user="nightjar@example.com",
        imap_password="x",
        allowed_contacts=("principal",),
        trusted_authserv="mx.google.com",
    )
    config = Config(
        daemon=DaemonConfig(
            state_dir=tmp_path / "state",
            log_dir=log_dir,
            contacts_dir=tmp_path / "contacts",
        ),
        inboxes={"nightjar": inbox},
        contacts={"principal": principal},
        address_index={"me@example.com": "principal"},
        smtp=None, claude=None, security=None,
    )
    # JSONLLogger writes per-day files into the given directory.
    logger = JSONLLogger(log_dir)
    watcher = InboxWatcher(
        inbox=inbox, config=config, state=state, logger=logger,
    )
    return watcher, state, log_dir


def _read_log_events(log_dir: Path) -> list[dict]:
    """Read all JSONL events from JSONLLogger's per-day output files."""
    events: list[dict] = []
    for p in sorted(log_dir.glob("*.jsonl")):
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


# ---- Config tests ---------------------------------------------------------


def test_inbox_config_default_poll_interval() -> None:
    inbox = InboxConfig(
        name="x", enabled=True, imap_host="h", imap_port=993,
        imap_user="u", imap_password="p", allowed_contacts=(),
        trusted_authserv="mx.google.com",
    )
    assert inbox.poll_interval_seconds == 60


def test_inbox_config_poll_disabled_zero() -> None:
    """Setting poll_interval_seconds=0 must be accepted (disables
    the poll, restores IDLE-only behaviour)."""
    inbox = InboxConfig(
        name="x", enabled=True, imap_host="h", imap_port=993,
        imap_user="u", imap_password="p", allowed_contacts=(),
        trusted_authserv="mx.google.com",
        poll_interval_seconds=0,
    )
    assert inbox.poll_interval_seconds == 0


# ---- Catchup wake_reason behaviour ----------------------------------------


def test_catchup_emits_poll_caught_event_when_poll_finds_mail(
    tmp_path: Path,
) -> None:
    """The headline test: a poll-driven catchup that actually
    processes new mail must emit poll_caught_missed_push so the
    operator can see IDLE is missing pushes."""
    watcher, state, log_path = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)
    stub = _StubIMAP(
        search_replies=[("OK", [b"42"])],
        headers_by_uid={"42": _header_blob("<missed-push@example.com>")},
    )

    asyncio.run(watcher._catch_up(stub, wake_reason="poll"))

    events = _read_log_events(log_path)
    poll_events = [e for e in events if e.get("event") == "poll_caught_missed_push"]
    assert len(poll_events) == 1
    e = poll_events[0]
    assert e["processed"] >= 1
    assert e["level"] == "warn"
    assert "IDLE" in e.get("detail", "")


def test_catchup_does_not_emit_poll_event_when_no_new_mail(
    tmp_path: Path,
) -> None:
    """Poll-driven catchup with nothing to process is the EXPECTED
    case (most polls don't find anything because IDLE is doing its
    job). Logging it would be noise."""
    watcher, state, log_path = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)
    stub = _StubIMAP(
        search_replies=[("OK", [])],  # empty — nothing in the window
        headers_by_uid={},
    )

    asyncio.run(watcher._catch_up(stub, wake_reason="poll"))

    events = _read_log_events(log_path)
    assert not [e for e in events if e.get("event") == "poll_caught_missed_push"]


def test_catchup_does_not_emit_poll_event_on_activity_wake(
    tmp_path: Path,
) -> None:
    """Activity-driven catchup that finds mail is the NORMAL path —
    IDLE pushed it, the watcher caught it. No warn-level event."""
    watcher, state, log_path = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)
    stub = _StubIMAP(
        search_replies=[("OK", [b"42"])],
        headers_by_uid={"42": _header_blob("<via-idle@example.com>")},
    )

    asyncio.run(watcher._catch_up(stub, wake_reason="activity"))

    events = _read_log_events(log_path)
    assert not [e for e in events if e.get("event") == "poll_caught_missed_push"]


def test_catchup_does_not_emit_poll_event_on_refresh_wake(
    tmp_path: Path,
) -> None:
    """The long IDLE refresh (~27 min) finding mail is normal
    too — Gmail rotates IDLE connections. Not a missed push."""
    watcher, state, log_path = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)
    stub = _StubIMAP(
        search_replies=[("OK", [b"42"])],
        headers_by_uid={"42": _header_blob("<via-refresh@example.com>")},
    )

    asyncio.run(watcher._catch_up(stub, wake_reason="refresh"))

    events = _read_log_events(log_path)
    assert not [e for e in events if e.get("event") == "poll_caught_missed_push"]


# ---- Socket-health probe (sleep/wake recovery) ---------------------------


class _NoopStubResp:
    def __init__(self, result: str = "OK") -> None:
        self.result = result
        self.lines: list[bytes] = []


class _NoopStubIMAP:
    """Minimum surface for _probe_socket_alive: just a noop() coroutine."""
    def __init__(
        self,
        *,
        noop_result: str = "OK",
        noop_delay_seconds: float = 0.0,
        noop_raises: BaseException | None = None,
    ) -> None:
        self._noop_result = noop_result
        self._noop_delay = noop_delay_seconds
        self._noop_raises = noop_raises
        self.noop_call_count = 0

    async def noop(self):
        self.noop_call_count += 1
        if self._noop_raises is not None:
            raise self._noop_raises
        if self._noop_delay > 0:
            await asyncio.sleep(self._noop_delay)
        return _NoopStubResp(self._noop_result)


def test_probe_socket_alive_returns_silently_on_ok(tmp_path: Path) -> None:
    """Healthy socket: NOOP returns OK promptly, probe returns. No
    exception, no log event."""
    watcher, _, log_dir = _make_watcher(tmp_path)
    stub = _NoopStubIMAP(noop_result="OK", noop_delay_seconds=0.0)

    asyncio.run(watcher._probe_socket_alive(stub))

    assert stub.noop_call_count == 1
    events = _read_log_events(log_dir)
    # No warning-level events from this path.
    assert not [e for e in events if e.get("event") in (
        "imap_socket_dead", "imap_noop_failed",
    )]


def test_probe_socket_alive_raises_on_timeout(tmp_path: Path) -> None:
    """Dead-socket simulation: NOOP coroutine sleeps past the
    timeout. Probe should raise RuntimeError and emit
    imap_socket_dead. Outer run() loop's exception handler will
    then reconnect."""
    watcher, _, log_dir = _make_watcher(tmp_path)
    # Sleep longer than NOOP_HEALTH_TIMEOUT_SECONDS by patching the
    # constant to a tiny value so the test runs fast.
    import daemon.inbox_watcher as iw
    orig_timeout = iw.NOOP_HEALTH_TIMEOUT_SECONDS
    iw.NOOP_HEALTH_TIMEOUT_SECONDS = 0.05
    try:
        stub = _NoopStubIMAP(noop_delay_seconds=2.0)
        with pytest.raises(RuntimeError, match="imap noop probe timed out"):
            asyncio.run(watcher._probe_socket_alive(stub))
    finally:
        iw.NOOP_HEALTH_TIMEOUT_SECONDS = orig_timeout

    events = _read_log_events(log_dir)
    dead_events = [e for e in events if e.get("event") == "imap_socket_dead"]
    assert len(dead_events) == 1
    assert dead_events[0]["level"] == "warn"
    assert "Reconnecting" in dead_events[0]["detail"]


def test_probe_socket_alive_raises_on_noop_non_ok(tmp_path: Path) -> None:
    """NOOP returns NO/BAD: server is talking but unhappy. Treat
    this as dead-equivalent and reconnect."""
    watcher, _, log_dir = _make_watcher(tmp_path)
    stub = _NoopStubIMAP(noop_result="NO")

    with pytest.raises(RuntimeError, match="imap noop returned NO"):
        asyncio.run(watcher._probe_socket_alive(stub))

    events = _read_log_events(log_dir)
    fail_events = [e for e in events if e.get("event") == "imap_noop_failed"]
    assert len(fail_events) == 1
    assert fail_events[0]["result"] == "NO"


def test_probe_socket_alive_raises_on_underlying_exception(
    tmp_path: Path,
) -> None:
    """If the IMAP client itself raises (e.g. ConnectionResetError
    from the socket layer), the probe should propagate so the outer
    loop can reconnect. The asyncio.wait_for wrapper passes through
    non-TimeoutError exceptions."""
    watcher, _, _ = _make_watcher(tmp_path)
    stub = _NoopStubIMAP(noop_raises=ConnectionResetError("peer closed"))

    with pytest.raises(ConnectionResetError):
        asyncio.run(watcher._probe_socket_alive(stub))


# ---- In-IDLE socket-death detection (Issue #42) -------------------------


class _PushStubClient:
    """Minimum surface for _wait_for_activity testing.

    Models wait_server_push() with a configurable behaviour:
    - 'push': returns a non-empty push immediately.
    - 'empty': returns None (server-pushed empty), one shot.
    - 'timeout': raises asyncio.TimeoutError after sleeping the
      requested timeout — simulates "nothing arrived in the slice."
    - 'block': sleeps forever (used to verify slice timeouts trip).
    """
    def __init__(
        self,
        *,
        behaviour: str,
        transport_dead_after_n_calls: int | None = None,
    ) -> None:
        self._behaviour = behaviour
        self._transport_dead_after = transport_dead_after_n_calls
        self.call_count = 0
        # _transport_is_dead() walks client.protocol.transport.is_closing()
        # — model that surface.
        self.protocol = _StubProtocol()

    async def wait_server_push(self, timeout: float = 0):
        self.call_count += 1
        if (
            self._transport_dead_after is not None
            and self.call_count > self._transport_dead_after
        ):
            self.protocol.transport.set_closing(True)
        if self._behaviour == "push":
            return [b"* 42 EXISTS"]
        if self._behaviour == "empty":
            # First call returns empty so the loop continues; the
            # second call decides what happens (transport state or
            # behaviour swap by test).
            if self.call_count == 1:
                return None
            return [b"* 42 EXISTS"]
        if self._behaviour == "timeout":
            # Honour the slice timeout so the test runs fast.
            await asyncio.sleep(0)
            raise asyncio.TimeoutError()
        if self._behaviour == "block":
            await asyncio.sleep(timeout)
            raise asyncio.TimeoutError()
        raise AssertionError(f"unknown behaviour: {self._behaviour}")


class _StubProtocol:
    def __init__(self) -> None:
        self.transport = _StubTransport()


class _StubTransport:
    def __init__(self) -> None:
        self._closing = False

    def set_closing(self, value: bool) -> None:
        self._closing = value

    def is_closing(self) -> bool:
        return self._closing

    def get_extra_info(self, key: str):
        # Used by _enable_tcp_keepalive — return None so the helper
        # returns False without setting any sockopts (that path is
        # tested separately with a real socket).
        return None


def test_wait_for_activity_returns_on_push(tmp_path: Path) -> None:
    """Healthy path: a single push wakes _wait_for_activity."""
    watcher, _, _ = _make_watcher(tmp_path)
    stub = _PushStubClient(behaviour="push")

    asyncio.run(watcher._wait_for_activity(stub))

    assert stub.call_count == 1


def test_wait_for_activity_raises_on_dead_transport(tmp_path: Path) -> None:
    """When a slice expires AND transport is closing, raise so the
    outer loop reconnects. This is the Issue #42 fix: without it,
    a half-open socket leaves wait_server_push blocked indefinitely.

    The test patches IDLE_PUSH_SLICE_SECONDS to a tiny value so the
    timeout fires fast.
    """
    watcher, _, log_dir = _make_watcher(tmp_path)
    import daemon.inbox_watcher as iw
    orig_slice = iw.IDLE_PUSH_SLICE_SECONDS
    iw.IDLE_PUSH_SLICE_SECONDS = 0.01
    try:
        # Transport reports dead after the very first wait_server_push
        # call — simulates TCP keepalive having torn the socket down.
        stub = _PushStubClient(
            behaviour="timeout", transport_dead_after_n_calls=0,
        )
        with pytest.raises(RuntimeError, match="imap transport dead during idle"):
            asyncio.run(watcher._wait_for_activity(stub))
    finally:
        iw.IDLE_PUSH_SLICE_SECONDS = orig_slice

    events = _read_log_events(log_dir)
    dead = [e for e in events if e.get("event") == "imap_socket_dead_in_idle"]
    assert len(dead) == 1
    assert dead[0]["level"] == "warn"
    assert "Reconnecting" in dead[0]["detail"]


def test_wait_for_activity_continues_on_timeout_with_live_transport(
    tmp_path: Path,
) -> None:
    """Slice expires but transport is still healthy → loop keeps
    waiting. The push_count rising past 1 proves the loop went
    around at least once.
    """
    watcher, _, log_dir = _make_watcher(tmp_path)
    import daemon.inbox_watcher as iw
    orig_slice = iw.IDLE_PUSH_SLICE_SECONDS
    iw.IDLE_PUSH_SLICE_SECONDS = 0.01

    # Custom client: first call times out (live transport), second
    # call returns a push.
    class _TwoPhaseStub:
        def __init__(self):
            self.call_count = 0
            self.protocol = _StubProtocol()
        async def wait_server_push(self, timeout: float = 0):
            self.call_count += 1
            if self.call_count == 1:
                await asyncio.sleep(0)
                raise asyncio.TimeoutError()
            return [b"* 42 EXISTS"]

    try:
        stub = _TwoPhaseStub()
        asyncio.run(watcher._wait_for_activity(stub))
    finally:
        iw.IDLE_PUSH_SLICE_SECONDS = orig_slice

    assert stub.call_count == 2
    # No dead-socket event when transport stayed healthy.
    events = _read_log_events(log_dir)
    assert not [e for e in events if e.get("event") == "imap_socket_dead_in_idle"]


def test_wait_for_activity_stop_event_breaks_loop(tmp_path: Path) -> None:
    """If _stop_event is set while looping, the function returns
    without raising — clean shutdown path."""
    watcher, _, _ = _make_watcher(tmp_path)
    import daemon.inbox_watcher as iw
    orig_slice = iw.IDLE_PUSH_SLICE_SECONDS
    iw.IDLE_PUSH_SLICE_SECONDS = 0.01

    # Empty pushes loop forever until stop_event is set; we set it
    # before entering, so the first iteration's loop guard exits
    # immediately.
    watcher._stop_event.set()
    stub = _PushStubClient(behaviour="empty")

    try:
        asyncio.run(watcher._wait_for_activity(stub))
    finally:
        iw.IDLE_PUSH_SLICE_SECONDS = orig_slice

    # Loop guard checked before the first wait_server_push — never
    # called.
    assert stub.call_count == 0


# ---- TCP keepalive helpers ------------------------------------------------


def test_enable_tcp_keepalive_returns_false_when_no_socket(tmp_path: Path) -> None:
    """When the client/transport hasn't connected yet (or
    aioimaplib's internals don't expose what we expect), the helper
    must return False rather than crash."""
    from daemon.inbox_watcher import _enable_tcp_keepalive

    class _NoProtocolClient:
        protocol = None

    assert _enable_tcp_keepalive(_NoProtocolClient()) is False


def test_get_underlying_socket_accepts_duck_typed_socket() -> None:
    """Regression: asyncio's SSL transport returns
    asyncio.TransportSocket, NOT a socket.socket subclass. The
    helper must accept any object that exposes setsockopt and
    getsockopt — strict isinstance(sock, socket.socket) silently
    drops keepalive on the SSL path, which is the production case.
    """
    from daemon.inbox_watcher import _get_underlying_socket

    class _DuckSocket:
        def setsockopt(self, *a, **kw): pass
        def getsockopt(self, *a, **kw): return 1

    duck = _DuckSocket()

    class _T:
        def get_extra_info(self, key):
            return duck if key == "socket" else None

    class _P:
        transport = _T()

    class _C:
        protocol = _P()

    assert _get_underlying_socket(_C()) is duck


def test_get_underlying_socket_rejects_object_without_sockopt() -> None:
    """Defence-in-depth: if the object has no setsockopt, it isn't
    a socket — return None so the caller doesn't try to set
    keepalive on something that can't honour it."""
    from daemon.inbox_watcher import _get_underlying_socket

    class _NotASocket:
        pass  # no setsockopt, no getsockopt

    not_sock = _NotASocket()

    class _T:
        def get_extra_info(self, key):
            return not_sock if key == "socket" else None

    class _P:
        transport = _T()

    class _C:
        protocol = _P()

    assert _get_underlying_socket(_C()) is None


def test_enable_tcp_keepalive_sets_sockopt_when_socket_present() -> None:
    """When a real socket is present, keepalive is enabled. We use
    a real AF_INET TCP socket (no connection needed) to verify the
    sockopt round-trips.
    """
    import socket as _socket
    from daemon.inbox_watcher import (
        _enable_tcp_keepalive, TCP_KEEPALIVE_IDLE_SECONDS,
    )

    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        class _StubTransportRealSock:
            def __init__(self, s):
                self._sock = s
            def get_extra_info(self, key):
                return self._sock if key == "socket" else None

        class _StubProtoRealSock:
            def __init__(self, s):
                self.transport = _StubTransportRealSock(s)

        class _StubClientRealSock:
            def __init__(self, s):
                self.protocol = _StubProtoRealSock(s)

        result = _enable_tcp_keepalive(_StubClientRealSock(sock))
        assert result is True
        assert sock.getsockopt(
            _socket.SOL_SOCKET, _socket.SO_KEEPALIVE,
        ) == 1
        if hasattr(_socket, "TCP_KEEPIDLE"):
            assert sock.getsockopt(
                _socket.IPPROTO_TCP, _socket.TCP_KEEPIDLE,
            ) == TCP_KEEPALIVE_IDLE_SECONDS
    finally:
        sock.close()


def test_transport_is_dead_returns_true_when_no_protocol(tmp_path: Path) -> None:
    from daemon.inbox_watcher import _transport_is_dead

    class _NoProto:
        protocol = None

    assert _transport_is_dead(_NoProto()) is True


def test_transport_is_dead_returns_true_when_closing() -> None:
    from daemon.inbox_watcher import _transport_is_dead

    transport = _StubTransport()
    transport.set_closing(True)

    class _Proto:
        pass

    proto = _Proto()
    proto.transport = transport

    class _Client:
        pass

    client = _Client()
    client.protocol = proto

    assert _transport_is_dead(client) is True


def test_transport_is_dead_returns_false_when_healthy() -> None:
    from daemon.inbox_watcher import _transport_is_dead

    transport = _StubTransport()  # closing defaults to False

    class _Proto:
        pass

    proto = _Proto()
    proto.transport = transport

    class _Client:
        pass

    client = _Client()
    client.protocol = proto

    assert _transport_is_dead(client) is False


def test_catchup_default_wake_reason_is_startup(tmp_path: Path) -> None:
    """The first catchup at daemon startup is called without a
    wake_reason kwarg. That path must not emit the missed-push
    event regardless of how much mail it processes."""
    watcher, state, log_path = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)
    stub = _StubIMAP(
        search_replies=[("OK", [b"42"])],
        headers_by_uid={"42": _header_blob("<at-startup@example.com>")},
    )

    asyncio.run(watcher._catch_up(stub))  # no kwarg

    events = _read_log_events(log_path)
    assert not [e for e in events if e.get("event") == "poll_caught_missed_push"]
