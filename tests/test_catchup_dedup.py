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
import calendar
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
        # Batch dedup pre-filter: comma-separated UID list. Reply with
        # a multi-message FETCH response shaped the way real Gmail
        # delivers it (see _batch_dedup_known_uids parser).
        if "," in uid:
            self.fetched_uids.append(uid)  # record the batch as one event
            chunks: list[bytes | bytearray] = []
            for u in uid.split(","):
                blob = self._headers_by_uid.get(u)
                if blob is None:
                    continue
                # Synthesize a Message-ID-only blob from the full header
                # blob the test fixture provides — _batch_dedup_known_uids
                # only reads the Message-ID line so we can pass the
                # whole header through.
                chunks.append(
                    f"1 FETCH (UID {u} BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(blob)}}}".encode("ascii")
                )
                chunks.append(bytearray(blob))
                chunks.append(b")")
            chunks.append(b"Success")
            return ("OK", chunks)
        # Single-UID slow path.
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
    # the configured 7-day window but no older than 31 days.
    #
    # Both the watcher's _imap_since_date and our parse here use UTC
    # (calendar.timegm). Mixing time.mktime (local time) with
    # time.gmtime caused intermittent failures across DST boundaries.
    since = stub.searches[0].removeprefix("SINCE ").strip()
    parsed_ts = calendar.timegm(time.strptime(since, "%d-%b-%Y"))
    now = time.time()
    # IMAP SINCE is day-granular and the watcher rounds DOWN to
    # midnight UTC, so parsed_ts can land up to ~24h earlier than
    # `now - window_days * 86400`. The 31-day upper bound on the
    # "how old" axis absorbs that.
    assert (now - 31 * 86400) <= parsed_ts <= (now - 7 * 86400)


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
    # Parse symmetrically with the watcher's UTC formatting.
    parsed_ts = calendar.timegm(time.strptime(since, "%d-%b-%Y"))
    now = time.time()
    # window = 7 days; the watcher's _imap_since_date rounds DOWN to
    # midnight UTC, so parsed_ts lands somewhere in [now - 8d, now - 7d]
    # depending on what time of day the test runs. Allow a small slop
    # on each side for runtime drift between the watcher's `now` and
    # the test's.
    assert (now - 8 * 86400 - 60) <= parsed_ts <= (now - 7 * 86400 + 60)


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


# ---------------------------------------------------------------------------
# Catchup error-rate threshold (silent-wedge incident 2026-05-07).
# When a half-open SSL transport makes most fetches fail with
# CommandTimeout, _catch_up must abort, leave the watermark unchanged,
# and raise so run() reconnects rather than blasting through the rest
# of the window for 100+ more 10s timeouts.
# ---------------------------------------------------------------------------


class _FetchRaisingIMAP:
    """Stub whose uid_search returns a configurable list of UIDs but
    whose uid('fetch', ...) call raises a configurable exception
    every time. Models the production failure: SINCE search succeeds
    (server is talking), then per-UID fetches all time out (transport
    is degraded).

    `batch_attempts` counts comma-separated UID fetches (the dedup
    pre-filter). `fetch_attempts` counts single-UID fetches (the
    slow per-UID path). Both raise the same exception so the catchup
    abort threshold tests behave the same with or without the batch
    pre-filter — the pre-filter swallows its own failure, falls back,
    and the slow path then trips the threshold as before."""
    def __init__(
        self,
        *,
        uids: list[bytes],
        fetch_raises: BaseException,
    ) -> None:
        self._uids = list(uids)
        self._fetch_raises = fetch_raises
        self.searches: list[str] = []
        self.fetch_attempts: int = 0
        self.batch_attempts: int = 0

    async def uid_search(self, query: str):
        self.searches.append(query)
        return ("OK", [b" ".join(self._uids)] if self._uids else [b""])

    async def uid(self, verb: str, uid: str, spec: str):  # noqa: ARG002
        assert verb == "fetch"
        if "," in uid:
            self.batch_attempts += 1
        else:
            self.fetch_attempts += 1
        raise self._fetch_raises


def _read_jsonl_events(log_dir: Path) -> list[dict]:
    """JSONL reader (separate from _read_log_events in the sibling
    file because tests in this module write into the JSONLLogger's
    file-mode rather than directory-mode)."""
    import json
    events: list[dict] = []
    if log_dir.is_file():
        log_files = [log_dir]
    else:
        log_files = sorted(log_dir.glob("*.jsonl"))
    for p in log_files:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def test_catchup_aborts_on_high_error_rate(tmp_path: Path) -> None:
    """Threshold trip: 1/4 of the candidates failing with raises is
    enough to abort. The function raises RuntimeError, the watermark
    does NOT advance, and a catchup_aborted_high_error_rate event is
    logged at warn level."""
    watcher, state = _make_watcher(tmp_path)
    initial_watermark = int(time.time()) - 600
    state.set_last_catchup_at("nightjar", initial_watermark)

    # 8 UIDs; CATCHUP_ABORT_MIN_ERRORS=4 will trip after the 4th raise.
    stub = _FetchRaisingIMAP(
        uids=[b"1", b"2", b"3", b"4", b"5", b"6", b"7", b"8"],
        fetch_raises=RuntimeError("simulated CommandTimeout"),
    )

    with pytest.raises(RuntimeError, match="catchup aborted: 4 fetch errors"):
        asyncio.run(watcher._catch_up(stub))

    # Threshold of max(4, 8//4) = 4. Should bail after the 4th attempt
    # (not push through all 8).
    assert stub.fetch_attempts == 4

    # Watermark must NOT have advanced — the next pass should re-search
    # this window, not skip past it.
    assert state.get_last_catchup_at("nightjar") == initial_watermark


def test_catchup_aborts_logs_warn_event(tmp_path: Path) -> None:
    """The abort path logs a single catchup_aborted_high_error_rate
    event at warn level with the failure totals so the operator can
    see it in the JSONL log."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = State(db_path=tmp_path / "state.db")
    inbox = _make_inbox()
    config = _make_config(tmp_path)
    logger = JSONLLogger(log_dir)
    watcher = InboxWatcher(
        inbox=inbox, config=config, state=state, logger=logger,
    )
    state.set_last_catchup_at("nightjar", int(time.time()) - 600)

    stub = _FetchRaisingIMAP(
        uids=[b"1", b"2", b"3", b"4", b"5", b"6"],
        fetch_raises=RuntimeError("simulated CommandTimeout"),
    )

    with pytest.raises(RuntimeError):
        asyncio.run(watcher._catch_up(stub))

    events = _read_jsonl_events(log_dir)
    abort_events = [
        e for e in events if e.get("event") == "catchup_aborted_high_error_rate"
    ]
    assert len(abort_events) == 1
    e = abort_events[0]
    assert e["level"] == "warn"
    assert e["errors"] == 4
    assert e["candidates"] == 6
    assert e["threshold"] == 4
    # No catchup_complete on the abort path — that event would mislead
    # the operator into thinking the pass succeeded.
    assert not [
        e for e in events if e.get("event") == "catchup_complete"
    ]


def test_catchup_does_not_abort_on_low_error_count(tmp_path: Path) -> None:
    """Below the absolute floor (CATCHUP_ABORT_MIN_ERRORS=4), a
    handful of fetch failures are normal — fold them into the totals
    and let catchup complete. Otherwise a single transient failure on
    a small window would loop the watcher indefinitely."""
    watcher, state = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 600)

    # 3 candidates, all fail. errors = 3, threshold = max(4, 3//4) = 4.
    # 3 < 4 → no trip.
    stub = _FetchRaisingIMAP(
        uids=[b"1", b"2", b"3"],
        fetch_raises=RuntimeError("transient"),
    )

    # Should NOT raise.
    asyncio.run(watcher._catch_up(stub))
    assert stub.fetch_attempts == 3
    # Watermark advances — we did finish the window, even with errors.
    assert state.get_last_catchup_at("nightjar") > int(time.time()) - 60


def test_catchup_threshold_scales_with_window(tmp_path: Path) -> None:
    """For larger windows, the trip threshold scales as 1/4 of
    candidates. With 100 UIDs the threshold is 25 — we should fail
    fast at 25, not blast through all 100."""
    watcher, state = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 600)

    uids = [str(i).encode("ascii") for i in range(1, 101)]
    stub = _FetchRaisingIMAP(
        uids=uids,
        fetch_raises=RuntimeError("simulated CommandTimeout"),
    )

    with pytest.raises(RuntimeError, match="catchup aborted: 25 fetch errors"):
        asyncio.run(watcher._catch_up(stub))

    assert stub.fetch_attempts == 25  # not 100
    # The batch-dedup pre-filter also tried (and raised); it counts
    # as a separate, swallowed attempt that doesn't change the trip
    # behaviour but tells us the pre-filter was reached.
    assert stub.batch_attempts == 1


# ---------------------------------------------------------------------------
# Batch dedup pre-filter tests (2026-05-08 fix for slow catchup).
#
# Prior behaviour: catchup did a per-UID `UID FETCH BODY[HEADER]` round-trip
# for every candidate before checking state-db dedup. With ~100 known
# candidates and a Gmail RTT around 3s, that produced 5-8 minute catchup
# cycles even when nothing was new. Fix: one batch fetch of MESSAGE-ID
# for all candidates, drop the known ones locally, then the slow per-UID
# fetch only runs for genuinely-new UIDs.
# ---------------------------------------------------------------------------


def test_batch_dedup_filters_known_uids_before_per_uid_fetch(tmp_path: Path) -> None:
    """When state-db already knows N of the M candidates, the slow
    per-UID fetch loop should run M-N times, not M times."""
    watcher, state = _make_watcher(tmp_path)
    # Seed state-db with 9 of 10 candidates as already-known.
    headers: dict[str, bytes] = {}
    for i in range(1, 10):  # UIDs 1-9 are known
        msgid = f"<known-{i}@example.com>"
        state.record_message(
            message_id=msgid, inbox="nightjar",
            from_addr="me@example.com", subject="ping",
            contact_id="principal", state="RECEIVED",
        )
        headers[str(i)] = _header_blob(msgid)
    # UID 10 is genuinely new.
    headers["10"] = _header_blob("<new@example.com>")
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)

    stub = _StubIMAP(
        search_replies=[("OK", [str(i).encode("ascii") for i in range(1, 11)])],
        headers_by_uid=headers,
    )
    asyncio.run(watcher._catch_up(stub))

    # First fetched_uids entry is the batch dedup (comma-separated).
    # The slow path runs only for UID 10 (multiple fetches against
    # UID 10 are fine — _fetch_and_record may itself fetch the body
    # via the principal-agent path).
    assert "," in stub.fetched_uids[0]  # batch
    slow_path_uids = {u for u in stub.fetched_uids if "," not in u}
    assert slow_path_uids == {"10"}


def test_batch_dedup_skipped_below_threshold(tmp_path: Path) -> None:
    """With fewer than CATCHUP_BATCH_DEDUP_MIN_CANDIDATES UIDs, the
    pre-filter is bypassed (parse overhead isn't worth the win on
    tiny windows). The slow path runs as before."""
    watcher, state = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)

    headers = {str(i): _header_blob(f"<msg-{i}@example.com>") for i in range(1, 4)}
    stub = _StubIMAP(
        search_replies=[("OK", [b"1", b"2", b"3"])],
        headers_by_uid=headers,
    )
    asyncio.run(watcher._catch_up(stub))

    # No comma in any fetched_uids entry → no batch fetch happened.
    assert all("," not in u for u in stub.fetched_uids)
    # All three UIDs were touched by the slow path (multiple fetches
    # per UID is fine — _fetch_and_record may also pull the body).
    assert {"1", "2", "3"} <= set(stub.fetched_uids)


def test_batch_dedup_failure_falls_back_to_slow_path(tmp_path: Path) -> None:
    """A failing batch dedup fetch must not break catchup — the slow
    per-UID path should still run for every candidate. This is the
    safety net that lets us ship the optimisation without making it
    load-bearing."""
    watcher, state = _make_watcher(tmp_path)
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)

    # 8 candidates, none known to state-db. Stub will return "NO" on
    # the batch dedup attempt (simulating a transient IMAP error or
    # an unsupported FETCH spec on this server). Slow path must still
    # process all 8.
    class _BatchFailingStub(_StubIMAP):
        async def uid(self, verb: str, uid: str, spec: str):
            if "," in uid:
                return ("NO", [])  # batch failure
            return await super().uid(verb, uid, spec)

    headers = {str(i): _header_blob(f"<msg-{i}@example.com>") for i in range(1, 9)}
    stub = _BatchFailingStub(
        search_replies=[("OK", [str(i).encode("ascii") for i in range(1, 9)])],
        headers_by_uid=headers,
    )
    asyncio.run(watcher._catch_up(stub))

    # All 8 messages should have been processed via the slow path.
    for i in range(1, 9):
        assert state.message_exists(f"<msg-{i}@example.com>")


def test_batch_dedup_logs_skip_events_per_uid(tmp_path: Path) -> None:
    """Telemetry parity: the new batch path should still emit one
    catchup_skipped_existing event per known UID so existing log
    diffing/alerting tools keep working. Plus a single
    catchup_batch_dedup summary event."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = State(db_path=tmp_path / "state.db")
    inbox = _make_inbox()
    config = _make_config(tmp_path)
    logger = JSONLLogger(log_dir)
    watcher = InboxWatcher(
        inbox=inbox, config=config, state=state, logger=logger,
    )
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)

    # 10 candidates, 8 known.
    headers: dict[str, bytes] = {}
    for i in range(1, 9):
        msgid = f"<known-{i}@example.com>"
        state.record_message(
            message_id=msgid, inbox="nightjar",
            from_addr="me@example.com", subject="ping",
            contact_id="principal", state="RECEIVED",
        )
        headers[str(i)] = _header_blob(msgid)
    for i in (9, 10):
        headers[str(i)] = _header_blob(f"<new-{i}@example.com>")

    stub = _StubIMAP(
        search_replies=[("OK", [str(i).encode("ascii") for i in range(1, 11)])],
        headers_by_uid=headers,
    )
    asyncio.run(watcher._catch_up(stub))

    events = _read_jsonl_events(log_dir)
    # 8 per-UID skip events, all marked detail=batch_dedup.
    skip_events = [e for e in events if e.get("event") == "catchup_skipped_existing"
                   and e.get("detail") == "batch_dedup"]
    assert len(skip_events) == 8
    # One summary event with the right totals.
    summary = [e for e in events if e.get("event") == "catchup_batch_dedup"]
    assert len(summary) == 1
    assert summary[0]["candidates"] == 10
    assert summary[0]["pre_filtered"] == 8
    assert summary[0]["remaining"] == 2


def test_batch_dedup_skipped_count_in_catchup_complete(tmp_path: Path) -> None:
    """The catchup_complete event's `skipped` total must include
    batch-dedup skips, not just per-UID dedup skips. Otherwise an
    operator looking at the telemetry would see candidates=N,
    processed=0, skipped=0 and wonder where the work went."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state = State(db_path=tmp_path / "state.db")
    inbox = _make_inbox()
    config = _make_config(tmp_path)
    logger = JSONLLogger(log_dir)
    watcher = InboxWatcher(
        inbox=inbox, config=config, state=state, logger=logger,
    )
    state.set_last_catchup_at("nightjar", int(time.time()) - 60)

    # 10 known, 0 new.
    headers: dict[str, bytes] = {}
    for i in range(1, 11):
        msgid = f"<known-{i}@example.com>"
        state.record_message(
            message_id=msgid, inbox="nightjar",
            from_addr="me@example.com", subject="ping",
            contact_id="principal", state="RECEIVED",
        )
        headers[str(i)] = _header_blob(msgid)

    stub = _StubIMAP(
        search_replies=[("OK", [str(i).encode("ascii") for i in range(1, 11)])],
        headers_by_uid=headers,
    )
    asyncio.run(watcher._catch_up(stub))

    events = _read_jsonl_events(log_dir)
    complete = [e for e in events if e.get("event") == "catchup_complete"]
    assert len(complete) == 1
    assert complete[0]["candidates"] == 10
    assert complete[0]["processed"] == 0
    assert complete[0]["skipped"] == 10  # all 10 came via batch dedup
    assert complete[0]["errors"] == 0
