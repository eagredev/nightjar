"""Tests for daemon/status_report.py and the related state.py accessors.

Pure-Python tests: no IMAP, no SMTP, no Claude. The IMAP walk is
abstracted behind a callable; tests pass a fake walker that returns
canned headers.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from daemon import status_report
from daemon.config import Config, DaemonConfig, InboxConfig, Contact
from daemon.state import State
from daemon.status_report import (
    DEFAULT_EXPIRY_WINDOW_SECONDS,
    IN_FLIGHT_GRACE_SECONDS,
    OOB_BEYOND_DAEMON,
    OOB_PRE_NIGHTJAR,
    OOB_WITHIN_WINDOW,
    InboxWalkResult,
    OutOfBandMessage,
    StatusReport,
    build_awaiting_section,
    build_expiring_section,
    build_health_block,
    build_in_flight_section,
    build_out_of_band_section,
    build_recent_outbound_section,
    build_status_report,
    classify_out_of_band,
    render_status_report,
)


# ---- Fixtures -------------------------------------------------------------


def _state_dir(tmp_path: Path) -> State:
    """Fresh state-db rooted at tmp_path."""
    db_path = tmp_path / "state.db"
    return State(db_path=db_path)


def _config(tmp_path: Path, *, inboxes: dict[str, InboxConfig] | None = None) -> Config:
    daemon = DaemonConfig(state_dir=tmp_path / "state", log_dir=tmp_path / "logs")
    daemon.state_dir.mkdir(parents=True, exist_ok=True)
    daemon.log_dir.mkdir(parents=True, exist_ok=True)
    if inboxes is None:
        inboxes = {
            "primary": InboxConfig(
                name="primary",
                enabled=True,
                imap_host="localhost",
                imap_port=993,
                imap_user="user@example.com",
                imap_password="secret",
                allowed_contacts=("principal", "alice"),
                trusted_authserv="mx.test",
                catchup_window_days=7,
                status_walk_count=200,
            ),
        }
    return Config(
        daemon=daemon,
        contacts={
            "principal": Contact(
                contact_id="principal",
                addresses=("me@example.com",),
                display_name="Principal",
                relationship="me",
                daily_limit=-1,
                is_principal=True,
            ),
            "alice": Contact(
                contact_id="alice",
                addresses=("alice@example.com",),
                display_name="Alice",
                relationship="collaborator",
                daily_limit=3,
                is_principal=False,
            ),
        },
        inboxes=inboxes,
    )


def _ok_walker(headers: list[dict]):
    """Returns an async walker that yields the given headers verbatim."""
    async def w(inbox_name: str, walk_count: int) -> InboxWalkResult:
        return InboxWalkResult(
            inbox=inbox_name,
            walked_count=len(headers),
            headers=tuple(headers),
            error=None,
        )
    return w


def _failing_walker(error: str = "imap down"):
    async def w(inbox_name: str, walk_count: int) -> InboxWalkResult:
        return InboxWalkResult(
            inbox=inbox_name, walked_count=0,
            headers=(), error=error,
        )
    return w


# ---- State accessors (Step 6g additions) ----------------------------------


def test_in_flight_messages_returns_only_specified_states(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    state.record_message(
        inbox="primary", message_id="<a@x>",
        from_addr="alice@example.com", contact_id="alice",
        subject="hello", received_at=100, state="RECEIVED",
    )
    state.record_message(
        inbox="primary", message_id="<b@x>",
        from_addr="alice@example.com", contact_id="alice",
        subject="resolved", received_at=100, state="RESPONDED",
    )
    rows = state.in_flight_messages(now=200)
    ids = {r["id"] for r in rows}
    assert ids == {"<a@x>"}


def test_in_flight_messages_age_filter(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    # Fresh message: received_at = now - 30s = 970. Within the grace
    # window — should NOT be returned.
    state.record_message(
        inbox="primary", message_id="<recent@x>",
        from_addr="alice@example.com", contact_id="alice",
        subject="recent", received_at=970, state="RECEIVED",
    )
    # Stale message: received_at = now - 20min = -200. Older than the
    # grace window — should be returned.
    state.record_message(
        inbox="primary", message_id="<stale@x>",
        from_addr="alice@example.com", contact_id="alice",
        subject="stale", received_at=1000 - 20 * 60, state="RECEIVED",
    )
    rows = state.in_flight_messages(
        older_than_seconds=10 * 60, now=1000,
    )
    ids = {r["id"] for r in rows}
    # Only the stale one survives the 10-min age filter.
    assert ids == {"<stale@x>"}


def test_expiring_approvals_window(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    state.queue_approval(
        token="aaa1", message_id="<a@x>", verb="block",
        args={"contact": "alice"}, tier=2,
        window_seconds=12 * 3600, at=1000,
    )
    state.queue_approval(
        token="bbb2", message_id="<b@x>", verb="block",
        args={"contact": "bob"}, tier=2,
        window_seconds=72 * 3600, at=1000,
    )
    rows = state.expiring_approvals(within_seconds=24 * 3600, now=1000)
    tokens = {r["token"] for r in rows}
    # Only the 12h-TTL row falls in the next 24h.
    assert tokens == {"aaa1"}


def test_list_message_ids_in_db_filters_by_inbox(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    state.record_message(
        inbox="primary", message_id="<p1@x>",
        from_addr="a@example.com", contact_id=None,
        subject="x", received_at=100, state="RECEIVED",
    )
    state.record_message(
        inbox="secondary", message_id="<s1@x>",
        from_addr="a@example.com", contact_id=None,
        subject="x", received_at=100, state="RECEIVED",
    )
    primary_ids = state.list_message_ids_in_db(inbox="primary")
    assert primary_ids == {"<p1@x>"}
    all_ids = state.list_message_ids_in_db()
    assert all_ids == {"<p1@x>", "<s1@x>"}


def test_first_message_received_at_min_or_none(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    assert state.first_message_received_at() is None
    state.record_message(
        inbox="primary", message_id="<z@x>",
        from_addr="a@example.com", contact_id=None,
        subject="x", received_at=500, state="RECEIVED",
    )
    state.record_message(
        inbox="primary", message_id="<y@x>",
        from_addr="a@example.com", contact_id=None,
        subject="x", received_at=200, state="RECEIVED",
    )
    assert state.first_message_received_at() == 200


def test_last_successful_claude_invocation(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    assert state.last_successful_claude_invocation_at() is None
    state.record_claude_invocation(
        purpose="triage", contact_id="alice",
        model="claude-haiku-4-5", input_tokens=100, output_tokens=20,
        ok=True, ts=500,
    )
    state.record_claude_invocation(
        purpose="triage", contact_id="alice",
        model="claude-haiku-4-5", input_tokens=100, output_tokens=20,
        ok=False, error_reason="x", ts=600,
    )
    # Only the ok=1 row counts.
    assert state.last_successful_claude_invocation_at() == 500


def test_last_outbound_sent_at(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    assert state.last_outbound_sent_at() is None
    state.record_outbound(
        ts=500, channel="reply", to_addr="a@example.com",
        subject="x", body="y", smtp_message_id="<x@x>",
        related_message_id=None, ok=True,
    )
    state.record_outbound(
        ts=600, channel="reply", to_addr="a@example.com",
        subject="x", body="y", smtp_message_id=None,
        related_message_id=None, ok=False, error="boom",
    )
    # ok=False filtered out, so the latest ok=1 row wins.
    assert state.last_outbound_sent_at() == 500


# ---- Section builders -----------------------------------------------------


def test_build_health_block_empty_state(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    cfg = _config(tmp_path)
    h = build_health_block(state=state, config=cfg)
    assert h["heartbeat_iso"] == "(never)"
    assert h["last_claude_call_iso"] == "(never)"
    assert h["last_sent_iso"] == "(never)"
    assert h["panicked"] is False
    assert h["last_catchup_iso_per_inbox"] == {"primary": "(never)"}


def test_build_health_block_with_data(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    cfg = _config(tmp_path)
    state.heartbeat(ts=1000)
    state.set_last_catchup_at("primary", 900)
    h = build_health_block(state=state, config=cfg)
    assert "1970" in h["heartbeat_iso"]
    assert "1970" in h["last_catchup_iso_per_inbox"]["primary"]


def test_build_awaiting_section(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    state.queue_approval(
        token="t1", message_id="<m@x>", verb="block",
        args={"contact": "alice"}, tier=2,
        window_seconds=7 * 86400, at=1000,
    )
    rows = build_awaiting_section(state=state, now=1100)
    assert len(rows) == 1
    assert rows[0]["age_seconds"] == 100


def test_build_expiring_section_excludes_far_future(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    state.queue_approval(
        token="soon", message_id="<m@x>", verb="block",
        args={"contact": "alice"}, tier=2,
        window_seconds=10 * 3600, at=1000,
    )
    state.queue_approval(
        token="later", message_id="<m@x>", verb="block",
        args={"contact": "alice"}, tier=2,
        window_seconds=72 * 3600, at=1000,
    )
    rows = build_expiring_section(state=state, now=1000)
    tokens = {r["token"] for r in rows}
    assert tokens == {"soon"}


def test_build_in_flight_section_uses_grace_window(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    # Recent message — should NOT show up.
    state.record_message(
        inbox="primary", message_id="<recent@x>",
        from_addr="a@example.com", contact_id=None,
        subject="x", received_at=995, state="RECEIVED",
    )
    rows = build_in_flight_section(state=state, now=1000)
    assert rows == []


def test_build_recent_outbound_section_returns_recent_first(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    state.record_outbound(
        ts=500, channel="reply", to_addr="a@x",
        subject="first", body="x", smtp_message_id="<a>",
        related_message_id=None, ok=True,
    )
    state.record_outbound(
        ts=600, channel="reply", to_addr="b@x",
        subject="second", body="y", smtp_message_id="<b>",
        related_message_id=None, ok=True,
    )
    rows = build_recent_outbound_section(state=state)
    # Newest first.
    assert rows[0]["subject"] == "second"
    assert rows[1]["subject"] == "first"


# ---- Out-of-band classifier ----------------------------------------------


def test_classify_oob_within_window() -> None:
    headers = [
        {"uid": "1", "message_id": "<new@x>", "from_addr": "a@x",
         "subject": "hi", "received_at": 950},
    ]
    out = classify_out_of_band(
        headers=tuple(headers),
        state_message_ids=set(),
        catchup_window_seconds=7 * 86400,
        daemon_first_seen_at=100,
        inbox="primary",
        now=1000,
    )
    assert len(out) == 1
    assert out[0].category == OOB_WITHIN_WINDOW


def test_classify_oob_pre_nightjar() -> None:
    headers = [
        {"uid": "1", "message_id": "<old@x>", "from_addr": "a@x",
         "subject": "hi", "received_at": 1000 - 10 * 86400},
    ]
    out = classify_out_of_band(
        headers=tuple(headers),
        state_message_ids=set(),
        catchup_window_seconds=7 * 86400,
        daemon_first_seen_at=100,
        inbox="primary",
        now=1000,
    )
    assert out[0].category == OOB_PRE_NIGHTJAR


def test_classify_oob_beyond_daemon() -> None:
    """A message older than the daemon's first-recorded message is
    classified beyond_daemon, regardless of how it relates to the
    catchup window."""
    headers = [
        {"uid": "1", "message_id": "<ancient@x>", "from_addr": "a@x",
         "subject": "hi", "received_at": 50},
    ]
    out = classify_out_of_band(
        headers=tuple(headers),
        state_message_ids=set(),
        catchup_window_seconds=7 * 86400,
        daemon_first_seen_at=100,
        inbox="primary",
        now=1000,
    )
    assert out[0].category == OOB_BEYOND_DAEMON


def test_classify_oob_skips_known_message_ids() -> None:
    headers = [
        {"uid": "1", "message_id": "<known@x>", "from_addr": "a@x",
         "subject": "hi", "received_at": 950},
        {"uid": "2", "message_id": "<unknown@x>", "from_addr": "a@x",
         "subject": "hi", "received_at": 950},
    ]
    out = classify_out_of_band(
        headers=tuple(headers),
        state_message_ids={"<known@x>"},
        catchup_window_seconds=7 * 86400,
        daemon_first_seen_at=100,
        inbox="primary",
        now=1000,
    )
    assert {m.message_id for m in out} == {"<unknown@x>"}


def test_classify_oob_skips_messages_without_message_id() -> None:
    headers = [
        {"uid": "1", "message_id": "", "from_addr": "a@x",
         "subject": "hi", "received_at": 950},
    ]
    out = classify_out_of_band(
        headers=tuple(headers),
        state_message_ids=set(),
        catchup_window_seconds=7 * 86400,
        daemon_first_seen_at=None,
        inbox="primary",
        now=1000,
    )
    assert out == []


# ---- build_out_of_band_section -------------------------------------------


def test_oob_section_handles_failing_walker(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    cfg = _config(tmp_path)
    out = asyncio.run(build_out_of_band_section(
        state=state, config=cfg, walker=_failing_walker(), now=1000,
    ))
    assert out == {"primary": []}


def test_oob_section_classifies_per_inbox(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    state.record_message(
        inbox="primary", message_id="<known@x>",
        from_addr="a@example.com", contact_id=None,
        subject="x", received_at=900, state="RECEIVED",
    )
    cfg = _config(tmp_path)
    walker = _ok_walker([
        {"uid": "1", "message_id": "<known@x>", "from_addr": "a@x",
         "subject": "x", "received_at": 900},
        {"uid": "2", "message_id": "<unknown@x>", "from_addr": "b@x",
         "subject": "y", "received_at": 950},
    ])
    out = asyncio.run(build_out_of_band_section(
        state=state, config=cfg, walker=walker, now=1000,
    ))
    assert {m.message_id for m in out["primary"]} == {"<unknown@x>"}


# ---- build_status_report end-to-end --------------------------------------


def test_build_status_report_minimal(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    cfg = _config(tmp_path)
    report = asyncio.run(build_status_report(
        state=state, config=cfg, walker=_ok_walker([]), now=1000,
    ))
    assert isinstance(report, StatusReport)
    assert report.generated_at == 1000
    assert report.awaiting == []
    assert report.expiring == []
    assert report.in_flight == []
    assert report.out_of_band == {"primary": []}
    assert report.recent_outbound == []


def test_build_status_report_with_data(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    state.queue_approval(
        token="t1", message_id="<m@x>", verb="block",
        args={"contact": "alice"}, tier=2,
        window_seconds=12 * 3600, at=1000,
    )
    state.heartbeat(ts=1000)
    cfg = _config(tmp_path)
    report = asyncio.run(build_status_report(
        state=state, config=cfg, walker=_ok_walker([]), now=1100,
    ))
    assert len(report.awaiting) == 1
    assert len(report.expiring) == 1  # 12h ttl is within 24h window
    assert "1970" in report.health["heartbeat_iso"]


# ---- Renderer ------------------------------------------------------------


def test_render_status_report_basic_shape(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    cfg = _config(tmp_path)
    report = asyncio.run(build_status_report(
        state=state, config=cfg, walker=_ok_walker([]), now=1000,
    ))
    rendered = render_status_report(report)
    assert "DAEMON HEALTH" in rendered
    assert "AWAITING YOUR ACTION" in rendered
    assert "APPROACHING EXPIRY" in rendered
    assert "IN FLIGHT" in rendered
    assert "OUT-OF-BAND MAIL" in rendered
    assert "RECENTLY SENT" in rendered
    assert "(none)" in rendered  # for empty sections


def test_render_status_report_includes_pickup_pointer_when_oob(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    cfg = _config(tmp_path)
    walker = _ok_walker([
        {"uid": "1", "message_id": "<unknown@x>", "from_addr": "alice@x",
         "subject": "test", "received_at": 950},
    ])
    report = asyncio.run(build_status_report(
        state=state, config=cfg, walker=walker, now=1000,
    ))
    rendered = render_status_report(report)
    assert "<unknown@x>" in rendered
    assert "pickup" in rendered.lower()


def test_render_status_report_panic_banner_when_panicked(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    state.trip_panic(reason="dead-mans-switch", at=900)
    cfg = _config(tmp_path)
    report = asyncio.run(build_status_report(
        state=state, config=cfg, walker=_ok_walker([]), now=1000,
    ))
    rendered = render_status_report(report)
    assert "PANICKED" in rendered
