"""Step 6e (receipt reliability) catchup loop tests.

These tests exercise the new SINCE+state-db dedup logic in
`InboxWatcher._catch_up`. The watcher's IMAP client is replaced with
a tiny stub that records the searches it received and replies with
canned UIDs/headers — that's enough surface to verify the dedup,
watermark, and first-run summary behaviour without standing up a
real IMAP server or full asyncio event loop.

Coverage:
    - Date formatting helper
    - SINCE search uses the right window in steady state vs. first run
    - Messages already in state-db are dedup-skipped
    - Messages \\Seen-flagged externally but absent from state-db are
      still processed (the bug Step 6e fixes)
    - Watermark advances on success
    - First-run summary fires once and only once
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Iterable
from unittest.mock import MagicMock

import pytest

from daemon.config import (
    ClaudeConfig,
    Config,
    Contact,
    DaemonConfig,
    InboxConfig,
    SmtpConfig,
)
from daemon.inbox_watcher import InboxWatcher
from daemon.log import JSONLLogger
from daemon.state import State


# ---------------------------------------------------------------------------
# Fixtures: build a minimum InboxWatcher wired to a tmp State.
# ---------------------------------------------------------------------------


def _make_logger(tmp_path: Path) -> JSONLLogger:
    return JSONLLogger(tmp_path / "test.log")


def _make_principal() -> Contact:
    return Contact(
        contact_id="principal",
        addresses=("me@example.com",),
        display_name="Me",
        relationship="Administrator",
        daily_limit=-1,
        is_principal=True,
        inboxes=("nightjar",),
    )


def _make_inbox(name: str = "nightjar", window: int = 7) -> InboxConfig:
    return InboxConfig(
        name=name,
        enabled=True,
        imap_host="imap.example.com",
        imap_port=993,
        imap_user="nightjar@example.com",
        imap_password="x",
        allowed_contacts=("principal",),
        trusted_authserv="mx.google.com",
        catchup_window_days=window,
    )


def _make_config(tmp_path: Path, *, window: int = 7) -> Config:
    principal = _make_principal()
    inbox = _make_inbox(window=window)
    return Config(
        daemon=DaemonConfig(
            state_dir=tmp_path / "state",
            log_dir=tmp_path / "logs",
            contacts_dir=tmp_path / "contacts",
        ),
        inboxes={"nightjar": inbox},
        contacts={"principal": principal},
        address_index={"me@example.com": "principal"},
        smtp=None,           # disables first-run summary delivery
        claude=None,         # disables triage
        security=None,       # principal mail will fail auth, which is fine
    )


def _make_watcher(tmp_path: Path, *, window: int = 7) -> tuple[InboxWatcher, State]:
    state = State(db_path=tmp_path / "state.db")
    watcher = InboxWatcher(
        inbox=_make_inbox(window=window),
        config=_make_config(tmp_path, window=window),
        state=state,
        logger=_make_logger(tmp_path),
    )
    return watcher, state


# ---------------------------------------------------------------------------
# Stub IMAP client.
# ---------------------------------------------------------------------------


class _StubResponse:
    """aioimaplib returns objects with .result and .lines; we don't use
    .lines but a couple of code paths reach for .result on select().
    """
    def __init__(self, result: str = "OK"):
        self.result = result
        self.lines: list[bytes] = []


class _StubIMAP:
    """A minimum surface to drive `_catch_up`.

    `search_replies` is a queue of (result, [uid_bytes, ...]) tuples,
    one per `uid_search` call. `headers_by_uid` maps UID strings to
    raw RFC822 header blobs the stub returns from `uid("fetch", ...)`.
    """
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
        # aioimaplib returns a list with one space-joined bytes blob
        return (result, [b" ".join(uids)] if uids else [b""])

    async def uid(self, verb: str, uid: str, spec: str):
        assert verb == "fetch"
        self.fetched_uids.append(uid)
        if uid not in self._headers_by_uid:
            return ("NO", [])
        blob = self._headers_by_uid[uid]
        # Match the shape `_extract_literal` parses.
        return ("OK", [
            f"1 FETCH (UID {uid} BODY[HEADER] {{{len(blob)}}}".encode("ascii"),
            bytearray(blob),
            b")",
            b"Success",
        ])


def _header_blob(message_id: str, *, from_addr: str = "me@example.com",
                 subject: str = "ping",
                 dmarc_pass: bool = True) -> bytes:
    """Build a minimal RFC822 header blob the watcher will accept.

    DMARC: the watcher requires Authentication-Results from the
    trusted authserv (mx.google.com) with dmarc=pass and a matching
    From: domain. Without this, every message is DROPPED at the
    DMARC gate, but for catchup-loop testing that's also fine — the
    state-db row still gets inserted, so dedup still works.
    """
    auth_results = (
        f"Authentication-Results: mx.google.com; dmarc=pass header.from={from_addr.split('@')[1]}\r\n"
        if dmarc_pass else ""
    )
    return (
        f"{auth_results}"
        f"Message-ID: {message_id}\r\n"
        f"From: {from_addr}\r\n"
        f"Subject: {subject}\r\n"
        f"\r\n"
    ).encode("ascii")


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_imap_since_date_formats_correctly() -> None:
    """Verify the SINCE date format the watcher emits to IMAP servers."""
    # 2026-01-15 12:00:00 UTC = calendar.timegm
    import calendar
    ts = calendar.timegm((2026, 1, 15, 12, 0, 0, 0, 0, 0))
    assert InboxWatcher._imap_since_date(ts) == "15-Jan-2026"
    # Check zero-padding of single-digit days.
    ts2 = calendar.timegm((2026, 3, 4, 0, 0, 0, 0, 0, 0))
    assert InboxWatcher._imap_since_date(ts2) == "04-Mar-2026"


def test_first_run_uses_30_day_window(tmp_path: Path) -> None:
    """Watermark NULL → wider 30-day window even when configured tighter."""
    watcher, _ = _make_watcher(tmp_path, window=7)
    stub = _StubIMAP(
        search_replies=[("OK", [])],
        headers_by_uid={},
    )
    asyncio.run(watcher._catch_up(stub))
    assert len(stub.searches) == 1
    # The search should reach back ~30 days regardless of the configured
    # 7-day window. Easy proxy: the SINCE date should be older than
    # (now - 8 days) and within (now - 31 days).
    since = stub.searches[0].removeprefix("SINCE ").strip()
    parsed_ts = time.mktime(time.strptime(since, "%d-%b-%Y"))
    now = time.time()
    assert (now - 31 * 86400) <= parsed_ts <= (now - 8 * 86400)


def test_steady_state_uses_configured_window(tmp_path: Path) -> None:
    """With a watermark set, the search uses the configured window
    (or watermark - 1d, whichever is older)."""
    watcher, state = _make_watcher(tmp_path, window=7)
    # Pretend we already ran a catchup an hour ago.
    state.set_last_catchup_at("nightjar", int(time.time()) - 3600)
    stub = _StubIMAP(
        search_replies=[("OK", [])],
        headers_by_uid={},
    )
    asyncio.run(watcher._catch_up(stub))
    since = stub.searches[0].removeprefix("SINCE ").strip()
    parsed_ts = time.mktime(time.strptime(since, "%d-%b-%Y"))
    now = time.time()
    # window = 7 days, so SINCE should land in the (now - 8d, now - 6d) range.
    assert (now - 8 * 86400) <= parsed_ts <= (now - 6 * 86400)


def test_dedup_skips_known_messages(tmp_path: Path) -> None:
    """A UID whose Message-ID is already in state-db is skipped silently."""
    watcher, state = _make_watcher(tmp_path)
    # Pre-populate state-db with a message we'll pretend IMAP also
    # returned.
    state.record_message(
        message_id="<known@example.com>",
        inbox="nightjar",
        from_addr="me@example.com",
        subject="ping",
        contact_id="principal",
        state="RECEIVED",
    )
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)
    stub = _StubIMAP(
        search_replies=[("OK", [b"1"])],
        headers_by_uid={"1": _header_blob("<known@example.com>")},
    )
    asyncio.run(watcher._catch_up(stub))
    # The fetch happens (we need headers to discover the Message-ID),
    # but no second insert is attempted. The state-db should still
    # have exactly the one row.
    assert state.message_exists("<known@example.com>")
    # No new approvals or transitions should have been added.
    assert stub.fetched_uids == ["1"]


def test_externally_seen_message_still_processed(tmp_path: Path) -> None:
    """The bug Step 6e fixes: a message marked \\Seen by Gmail web /
    a phone client / a daemon crash is NOT in state-db, and SINCE
    catches it regardless of the \\Seen flag.

    We don't model \\Seen at all here — that's the point. The new
    catchup doesn't filter on \\Seen, so a message that the old
    UNSEEN-based logic would have skipped is now picked up by virtue
    of being absent from state-db.
    """
    watcher, state = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)
    stub = _StubIMAP(
        search_replies=[("OK", [b"42"])],
        headers_by_uid={"42": _header_blob("<silently-dropped@example.com>")},
    )
    asyncio.run(watcher._catch_up(stub))
    assert state.message_exists("<silently-dropped@example.com>")


def test_watermark_advances_on_success(tmp_path: Path) -> None:
    watcher, state = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", 1_700_000_000)
    stub = _StubIMAP(
        search_replies=[("OK", [])],
        headers_by_uid={},
    )
    before = int(time.time())
    asyncio.run(watcher._catch_up(stub))
    after = int(time.time())
    new_watermark = state.get_last_catchup_at("nightjar")
    assert new_watermark is not None
    assert before <= new_watermark <= after


def test_watermark_advances_even_with_only_skipped_candidates(tmp_path: Path) -> None:
    """If every UID dedup-skips, we still verified the window is clear,
    so the watermark must advance — otherwise we'd re-walk the same
    range forever."""
    watcher, state = _make_watcher(tmp_path)
    state.record_message(
        message_id="<a@example.com>",
        inbox="nightjar",
        from_addr="me@example.com",
        subject="ping",
        contact_id="principal",
        state="RECEIVED",
    )
    state.set_last_catchup_at("nightjar", 1_700_000_000)
    stub = _StubIMAP(
        search_replies=[("OK", [b"1"])],
        headers_by_uid={"1": _header_blob("<a@example.com>")},
    )
    asyncio.run(watcher._catch_up(stub))
    new_watermark = state.get_last_catchup_at("nightjar")
    assert new_watermark is not None
    assert new_watermark > 1_700_000_000


def test_catchup_search_failed_does_not_advance_watermark(tmp_path: Path) -> None:
    """If IMAP refuses the search, we did NOT verify anything; the
    watermark must not move forward (we'd lose mail in the gap)."""
    watcher, state = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", 1_700_000_000)
    stub = _StubIMAP(
        search_replies=[("NO", [])],
        headers_by_uid={},
    )
    asyncio.run(watcher._catch_up(stub))
    assert state.get_last_catchup_at("nightjar") == 1_700_000_000


def test_first_run_summary_fires_only_when_processed_gt_zero(tmp_path: Path) -> None:
    """First-run with new mail → summary attempted. First-run with
    only skipped/empty → no summary (would be noise on a clean
    install)."""
    watcher, state = _make_watcher(tmp_path)
    sent: list[str] = []
    # Patch the helper to record calls without sending. SMTP is None
    # in the fixture, so the helper short-circuits, but this gives
    # us a positive-side check too.
    watcher._send_first_run_recon_summary = MagicMock(
        side_effect=lambda **kw: sent.append("called")
    )
    stub = _StubIMAP(
        search_replies=[("OK", [b"1"])],
        headers_by_uid={"1": _header_blob("<new-msg@example.com>")},
    )
    asyncio.run(watcher._catch_up(stub))
    assert sent == ["called"]


def test_first_run_summary_does_not_fire_with_zero_processed(tmp_path: Path) -> None:
    watcher, state = _make_watcher(tmp_path)
    sent: list[str] = []
    watcher._send_first_run_recon_summary = MagicMock(
        side_effect=lambda **kw: sent.append("called")
    )
    stub = _StubIMAP(
        search_replies=[("OK", [])],
        headers_by_uid={},
    )
    asyncio.run(watcher._catch_up(stub))
    assert sent == []


def test_first_run_summary_does_not_fire_on_subsequent_runs(tmp_path: Path) -> None:
    """The summary is one-shot. Once the watermark is set, a later
    catchup (still finding new mail) must NOT re-fire it."""
    watcher, state = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)
    sent: list[str] = []
    watcher._send_first_run_recon_summary = MagicMock(
        side_effect=lambda **kw: sent.append("called")
    )
    stub = _StubIMAP(
        search_replies=[("OK", [b"1"])],
        headers_by_uid={"1": _header_blob("<new@example.com>")},
    )
    asyncio.run(watcher._catch_up(stub))
    assert sent == []


def test_no_messageid_falls_back_to_synthetic_id(tmp_path: Path) -> None:
    """A message without a Message-ID header gets a synthetic ID so
    we still dedup against state-db on subsequent passes."""
    watcher, state = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)
    # Hand-craft a header blob with no Message-ID.
    blob = (
        b"Authentication-Results: mx.google.com; dmarc=pass header.from=example.com\r\n"
        b"From: me@example.com\r\n"
        b"Subject: anonymous\r\n"
        b"\r\n"
    )
    stub = _StubIMAP(
        search_replies=[("OK", [b"99"])],
        headers_by_uid={"99": blob},
    )
    asyncio.run(watcher._catch_up(stub))
    assert state.message_exists("<no-msgid-nightjar-uid99>")
