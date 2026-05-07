"""Regression tests for the agent-MVP continuation routing path.

Production bug surfaced 2026-05-06: after an init dispatch, the
agent_sessions row's last_message_id was not being advanced to the
OUTBOUND reply's Message-ID. The principal's continuation reply (whose
In-Reply-To references the daemon's outbound) then failed to match in
agent_session_lookup_by_last_message, fell through to subject auth,
and tripped the dead-man's-switch.

These tests pin the dispatch round-trip:

    1. _dispatch_agent_request, on init, must call agent_session_advance
       with the OUTBOUND Message-ID returned by _send_agent_reply.

    2. _classify_agent_mail, on a follow-up reply with
       In-Reply-To = the OUTBOUND id, must classify as
       AGENT_CONTINUATION and return the original session_id.

    3. The classifier's session-status filter must permit 'completed'
       sessions (Claude --resume continues a completed session — that's
       the entire point).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from daemon import agent_router, inbox_watcher, principal_agent
from daemon.config import (
    Config, Contact, DaemonConfig, InboxConfig, SecurityConfig, SmtpConfig,
)
from daemon.inbox_watcher import InboxWatcher
from daemon.log import JSONLLogger
from daemon.notifier import SendResult
from daemon.state import State


def _make_config(tmp_path: Path) -> tuple[Config, InboxConfig]:
    daemon = DaemonConfig(
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        notes_dir=tmp_path / "notes",
    )
    daemon.state_dir.mkdir(parents=True)
    daemon.log_dir.mkdir(parents=True)
    daemon.notes_dir.mkdir(parents=True)
    contacts = {
        "principal": Contact(
            contact_id="principal",
            addresses=("me@example.com",),
            display_name="Operator",
            relationship="self",
            daily_limit=-1,
            is_principal=True,
            inboxes=("nightjar",),
        ),
    }
    inbox = InboxConfig(
        name="nightjar",
        enabled=True,
        imap_host="imap.example.com",
        imap_port=993,
        imap_user="bot@example.com",
        imap_password="x",
        allowed_contacts=("principal",),
        trusted_authserv="mx.google.com",
    )
    security = SecurityConfig(
        totp_secret="JBSWY3DPEHPK3PXP",
        dead_mans_switch_window_minutes=60,
        dead_mans_switch_threshold=3,
    )
    smtp = SmtpConfig(
        host="smtp.example.com",
        port=587,
        user="bot@example.com",
        password="smtp-secret",
        from_name="Nightjar",
        from_addr="bot@example.com",
    )
    return (
        Config(
            daemon=daemon,
            contacts=contacts,
            inboxes={"nightjar": inbox},
            security=security,
            smtp=smtp,
            address_index={"me@example.com": "principal"},
        ),
        inbox,
    )


def _make_watcher(tmp_path: Path) -> InboxWatcher:
    config, inbox = _make_config(tmp_path)
    state = State(db_path=config.daemon.state_dir / "state.db")
    logger = JSONLLogger(config.daemon.log_dir)
    return InboxWatcher(
        inbox=inbox, config=config, state=state, logger=logger,
    )


def _stub_send_result(message_id: str) -> SendResult:
    return SendResult(
        primary_message_id=message_id,
        primary_sent=True,
        audit_sent=True,
        audit_queued=False,
        audit_id=None,
        error=None,
    )


def test_init_dispatch_advances_last_message_to_outbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: an init dispatch must leave agent_sessions.last_message_id
    pointing at the OUTBOUND reply's Message-ID, not the inbound."""
    watcher = _make_watcher(tmp_path)

    inbound_id = "<inbound-init@principal.example.com>"
    outbound_id = "<outbound-reply@bot.example.com>"
    session_id = "deadbeef-0000-0000-0000-000000000001"

    # Pre-record the inbound message in state so transition() can move it.
    watcher.state.record_message(
        message_id=inbound_id,
        inbox="nightjar",
        from_addr="me@example.com",
        subject="(body-only)",
        contact_id="principal",
        state="RECEIVED",
        received_at=1000,
    )

    async def fake_execute(**kwargs):
        return principal_agent.AgentResult(
            session_id=session_id,
            status="completed",
            final_text="Done.",
            audit_log_path=tmp_path / "audit.jsonl",
            started_at=1000,
            completed_at=1010,
        )

    monkeypatch.setattr(principal_agent, "execute", fake_execute)

    def fake_notify_principal(**kwargs):
        # The dispatch flow's outbound Message-ID for the agent reply.
        # We assert below that this id is what gets persisted into
        # agent_sessions.last_message_id.
        return _stub_send_result(outbound_id)

    monkeypatch.setattr(
        inbox_watcher.notifier, "notify_principal", fake_notify_principal,
    )

    classification = agent_router.AgentClassification(
        kind=agent_router.CLASS_AGENT_INIT,
        primary_code="123456",
        secondary_code="654321",
        session_id=None,
        request_body="Write polo.txt to my desktop.",
    )

    asyncio.run(
        watcher._dispatch_agent_request(
            message_id=inbound_id,
            from_addr="me@example.com",
            classification=classification,
        )
    )

    row = watcher.state.agent_session_get(session_id)
    assert row is not None, "agent_session row should have been created"
    assert row["originating_message_id"] == inbound_id
    assert row["last_message_id"] == outbound_id, (
        "regression: last_message_id was not advanced to the outbound "
        "Message-ID after _send_agent_reply succeeded"
    )
    assert row["status"] == "completed"


def test_continuation_lookup_resolves_via_outbound_id(
    tmp_path: Path,
) -> None:
    """The classifier helper must find the session when the principal's
    reply In-Reply-To matches the OUTBOUND id we just persisted."""
    watcher = _make_watcher(tmp_path)

    outbound_id = "<outbound-reply@bot.example.com>"
    session_id = "deadbeef-0000-0000-0000-000000000002"

    watcher.state.agent_session_create(
        session_id=session_id,
        originating_message_id="<orig-inbound@principal.example.com>",
        started_at=1000,
    )
    # Simulate the post-fix dispatch path: advance to outbound id.
    watcher.state.agent_session_advance(
        session_id=session_id, last_message_id=outbound_id,
    )
    watcher.state.agent_session_complete(
        session_id=session_id, status="completed", completed_at=1010,
    )

    # Drive the classifier directly through the watcher's lookup
    # closure. We pass body via the classify path, but only the lookup
    # logic matters here.
    body = "237294\n\nKeep going."
    classification = agent_router.classify(
        body=body,
        in_reply_to=outbound_id,
        active_session_lookup=lambda mid: (
            str(watcher.state.agent_session_lookup_by_last_message(mid)["session_id"])
            if watcher.state.agent_session_lookup_by_last_message(mid)
            and watcher.state.agent_session_lookup_by_last_message(mid).get("status")
                not in ("killed", "errored")
            else None
        ),
    )
    assert classification.kind == agent_router.CLASS_AGENT_CONTINUATION
    assert classification.session_id == session_id


def test_classifier_permits_completed_sessions(tmp_path: Path) -> None:
    """A 'completed' session must remain routable for continuation —
    Claude's --resume continues a completed session by design. Only
    'killed'/'errored' sessions should fall through to standard auth."""
    watcher = _make_watcher(tmp_path)
    outbound_id = "<outbound@bot.example.com>"

    watcher.state.agent_session_create(
        session_id="sess-completed",
        originating_message_id="<a@b>",
        started_at=1000,
    )
    watcher.state.agent_session_advance(
        session_id="sess-completed", last_message_id=outbound_id,
    )
    watcher.state.agent_session_complete(
        session_id="sess-completed", status="completed", completed_at=1010,
    )

    # The actual classifier filter (mirrors the helper in
    # _classify_agent_mail).
    def lookup(message_id: str) -> str | None:
        row = watcher.state.agent_session_lookup_by_last_message(message_id)
        if row is None:
            return None
        if row.get("status") in ("killed", "errored"):
            return None
        return str(row["session_id"])

    assert lookup(outbound_id) == "sess-completed"


def test_classifier_rejects_killed_and_errored_sessions(tmp_path: Path) -> None:
    """Killed and errored sessions cannot be resumed. The principal's
    next reply for those should fall through to standard subject auth."""
    watcher = _make_watcher(tmp_path)

    for status, suffix in (("killed", "k"), ("errored", "e")):
        sess = f"sess-{status}"
        outbound = f"<outbound-{suffix}@bot.example.com>"
        watcher.state.agent_session_create(
            session_id=sess,
            originating_message_id=f"<inbound-{suffix}@x>",
            started_at=1000,
        )
        watcher.state.agent_session_advance(
            session_id=sess, last_message_id=outbound,
        )
        watcher.state.agent_session_complete(
            session_id=sess, status=status, completed_at=1010,
        )

        def lookup(message_id: str) -> str | None:
            row = watcher.state.agent_session_lookup_by_last_message(message_id)
            if row is None:
                return None
            if row.get("status") in ("killed", "errored"):
                return None
            return str(row["session_id"])

        assert lookup(outbound) is None, (
            f"{status} session must NOT be resumable"
        )


def test_dispatch_failed_outbound_logs_unanchored_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the outbound reply fails to send, we cannot anchor the
    session. The watcher must log an explicit warning so the operator
    can see why a follow-up reply will not route."""
    watcher = _make_watcher(tmp_path)

    inbound_id = "<inbound-init-fail@principal.example.com>"
    session_id = "deadbeef-0000-0000-0000-000000000003"

    watcher.state.record_message(
        message_id=inbound_id,
        inbox="nightjar",
        from_addr="me@example.com",
        subject="(body-only)",
        contact_id="principal",
        state="RECEIVED",
        received_at=1000,
    )

    async def fake_execute(**kwargs):
        return principal_agent.AgentResult(
            session_id=session_id,
            status="completed",
            final_text="Done.",
            audit_log_path=tmp_path / "audit.jsonl",
            started_at=1000,
            completed_at=1010,
        )

    monkeypatch.setattr(principal_agent, "execute", fake_execute)

    def fake_notify_principal(**kwargs):
        return SendResult(
            primary_message_id="<would-have-been@bot>",
            primary_sent=False,  # send failed
            audit_sent=False,
            audit_queued=False,
            audit_id=None,
            error="connection refused",
        )

    monkeypatch.setattr(
        inbox_watcher.notifier, "notify_principal", fake_notify_principal,
    )

    events: list[dict] = []
    monkeypatch.setattr(
        watcher.logger, "event",
        lambda name, **kwargs: events.append({"name": name, **kwargs}),
    )

    classification = agent_router.AgentClassification(
        kind=agent_router.CLASS_AGENT_INIT,
        primary_code="123456",
        secondary_code="654321",
        session_id=None,
        request_body="Do something.",
    )

    asyncio.run(
        watcher._dispatch_agent_request(
            message_id=inbound_id,
            from_addr="me@example.com",
            classification=classification,
        )
    )

    names = [e["name"] for e in events]
    assert "agent_session_unanchored" in names, (
        f"expected agent_session_unanchored warning, got events: {names}"
    )

    # The session row should still exist but pointed at the inbound
    # (the create-time default), since we never advanced it.
    row = watcher.state.agent_session_get(session_id)
    assert row is not None
    assert row["last_message_id"] == inbound_id, (
        "On send failure, last_message_id should remain the create-time "
        "default (inbound). Subsequent continuations cannot route — by design."
    )
