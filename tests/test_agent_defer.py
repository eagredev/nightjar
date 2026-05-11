"""Integration tests for agent-dispatch deferral on busy systems.

Plan: ~/nightjar/docs/agent-defer-when-busy.md.

The deferral path:
  1. Inbound agent mail arrives. _dispatch_agent_request consults
     `system_load.is_system_busy(self.config.agent.dispatch)`.
  2. If busy: persist QUEUED_DEFERRED with payload, send a one-shot
     "queued" reply, and return without spawning the executor.
  3. After every IDLE catchup, _drain_deferred_if_free re-checks
     busy. If free: walks deferred rows oldest-first, atomically
     claims each (RECEIVED→QUEUED_DEFERRED→AGENT_RUNNING), and
     re-enters _dispatch_agent_request with a reconstructed
     classification.

These tests pin the round-trip: defer when busy, drain when free,
no double-dispatch.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from daemon import (
    agent_router,
    inbox_watcher,
    notifier,
    principal_agent,
    system_load,
)
from daemon.config import (
    AgentConfig,
    Config,
    Contact,
    DaemonConfig,
    InboxConfig,
    SecurityConfig,
    SmtpConfig,
)
from daemon.inbox_watcher import InboxWatcher
from daemon.log import JSONLLogger
from daemon.notifier import SendResult
from daemon.state import State


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_config(
    tmp_path: Path,
    *,
    dispatch_policy: system_load.DispatchPolicy | None = None,
) -> tuple[Config, InboxConfig]:
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
    agent_cfg = AgentConfig(
        dispatch=dispatch_policy or system_load.DispatchPolicy(
            defer_when_gaming_mode=True,
        ),
    )
    return (
        Config(
            daemon=daemon,
            contacts=contacts,
            inboxes={"nightjar": inbox},
            security=security,
            smtp=smtp,
            agent=agent_cfg,
            address_index={"me@example.com": "principal"},
        ),
        inbox,
    )


def _make_watcher(
    tmp_path: Path,
    *,
    dispatch_policy: system_load.DispatchPolicy | None = None,
) -> InboxWatcher:
    config, inbox = _make_config(tmp_path, dispatch_policy=dispatch_policy)
    state = State(db_path=config.daemon.state_dir / "state.db")
    logger = JSONLLogger(config.daemon.log_dir)
    return InboxWatcher(
        inbox=inbox, config=config, state=state, logger=logger,
    )


def _send_result_ok() -> SendResult:
    return SendResult(
        primary_message_id="<outbound@bot.example.com>",
        primary_sent=True,
        audit_sent=True,
        audit_queued=False,
        audit_id=None,
        error=None,
    )


def _record_received(state: State, message_id: str) -> None:
    state.record_message(
        message_id=message_id,
        inbox="nightjar",
        from_addr="me@example.com",
        subject="Re: agent",
        contact_id="principal",
        state="RECEIVED",
    )


# ---- Defer path ----------------------------------------------------------


def test_busy_system_defers_dispatch_and_persists_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _system_is_busy returns True, the message lands in
    QUEUED_DEFERRED with its request body stashed on the row,
    and the executor is NOT spawned."""
    watcher = _make_watcher(tmp_path)
    inbound_id = "<test-defer-1@example.com>"
    _record_received(watcher.state, inbound_id)

    monkeypatch.setattr(
        system_load, "is_system_busy",
        lambda policy: (True, "principal is in gaming mode (gamescope)"),
    )
    # Notifier should be called once with the queued-reply body.
    notify_calls: list[dict] = []
    monkeypatch.setattr(
        notifier, "notify_principal",
        lambda **kw: (notify_calls.append(kw), _send_result_ok())[1],
    )
    # Executor must NOT be reached.
    execute_called = MagicMock()
    monkeypatch.setattr(principal_agent, "execute", execute_called)

    classification = agent_router.AgentClassification(
        kind=agent_router.CLASS_AGENT_INIT,
        request_body="run the report please",
    )
    _run(watcher._dispatch_agent_request(
        message_id=inbound_id,
        from_addr="me@example.com",
        classification=classification,
    ))

    # Executor never spawned.
    execute_called.assert_not_called()
    # Exactly one notifier call, and it carried the queued-reply subject.
    assert len(notify_calls) == 1
    assert notify_calls[0]["subject"] == "Nightjar: queued"
    assert "queued" in notify_calls[0]["body"].lower()
    assert "gaming mode" in notify_calls[0]["body"].lower()
    # Row is QUEUED_DEFERRED and payload is intact.
    deferred = watcher.state.select_deferred_messages()
    assert len(deferred) == 1
    assert deferred[0]["message_id"] == inbound_id
    assert deferred[0]["payload"]["request_body"] == "run the report please"
    assert deferred[0]["payload"]["kind"] == agent_router.CLASS_AGENT_INIT
    assert deferred[0]["payload"]["session_id"] is None


def test_busy_continuation_preserves_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = _make_watcher(tmp_path)
    inbound_id = "<test-defer-cont@example.com>"
    _record_received(watcher.state, inbound_id)

    monkeypatch.setattr(
        system_load, "is_system_busy",
        lambda policy: (True, "principal is in gaming mode (gamescope)"),
    )
    monkeypatch.setattr(
        notifier, "notify_principal", lambda **kw: _send_result_ok(),
    )
    classification = agent_router.AgentClassification(
        kind=agent_router.CLASS_AGENT_CONTINUATION,
        session_id="sess-deferred-uuid",
        request_body="follow up",
    )
    _run(watcher._dispatch_agent_request(
        message_id=inbound_id,
        from_addr="me@example.com",
        classification=classification,
    ))
    deferred = watcher.state.select_deferred_messages()
    assert len(deferred) == 1
    assert deferred[0]["payload"]["session_id"] == "sess-deferred-uuid"
    assert deferred[0]["payload"]["kind"] == agent_router.CLASS_AGENT_CONTINUATION


def test_free_system_dispatches_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_system_busy False → existing dispatch path runs (executor
    is invoked). No QUEUED_DEFERRED row written."""
    watcher = _make_watcher(tmp_path)
    inbound_id = "<test-free@example.com>"
    _record_received(watcher.state, inbound_id)

    monkeypatch.setattr(
        system_load, "is_system_busy", lambda policy: (False, "system free"),
    )
    # Stub the executor so it doesn't actually try to spawn claude.
    fake_result = principal_agent.AgentResult(
        session_id="sess-fresh",
        status="completed",
        final_text="ok",
        audit_log_path=tmp_path / "audit.jsonl",
        started_at=1, completed_at=2,
    )

    async def fake_execute(**kw):
        return fake_result

    monkeypatch.setattr(principal_agent, "execute", fake_execute)
    monkeypatch.setattr(
        principal_agent, "ensure_agent_workspace", lambda cwd: None,
    )
    # The dispatch path will also try to send the reply — stub the notifier.
    monkeypatch.setattr(
        notifier, "notify_principal", lambda **kw: _send_result_ok(),
    )

    classification = agent_router.AgentClassification(
        kind=agent_router.CLASS_AGENT_INIT,
        request_body="hello",
    )
    _run(watcher._dispatch_agent_request(
        message_id=inbound_id,
        from_addr="me@example.com",
        classification=classification,
    ))
    # Nothing landed in QUEUED_DEFERRED.
    assert watcher.state.select_deferred_messages() == []


# ---- Drain path ----------------------------------------------------------


def test_drain_when_free_redispatches_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two deferred messages, system becomes free, drain re-dispatches
    in received_at order. Executor sees both."""
    watcher = _make_watcher(tmp_path)
    # Two deferred rows, late one first to confirm ordering.
    watcher.state.record_message(
        message_id="<m-late@example.com>",
        inbox="nightjar", from_addr="me@example.com",
        subject=None, contact_id="principal", state="RECEIVED",
        received_at=200,
    )
    watcher.state.record_message(
        message_id="<m-early@example.com>",
        inbox="nightjar", from_addr="me@example.com",
        subject=None, contact_id="principal", state="RECEIVED",
        received_at=100,
    )
    for mid in ("<m-late@example.com>", "<m-early@example.com>"):
        watcher.state.mark_deferred(
            message_id=mid,
            from_state="RECEIVED",
            deferred_payload={
                "request_body": f"req for {mid}",
                "kind": agent_router.CLASS_AGENT_INIT,
                "session_id": None,
            },
        )

    monkeypatch.setattr(
        system_load, "is_system_busy", lambda policy: (False, "system free"),
    )

    dispatch_calls: list[str] = []

    async def fake_dispatch(**kw):
        dispatch_calls.append(kw["message_id"])

    monkeypatch.setattr(
        watcher, "_dispatch_agent_request", fake_dispatch,
    )
    _run(watcher._drain_deferred_if_free())
    # Oldest first — early was received_at=100, late was 200.
    assert dispatch_calls == ["<m-early@example.com>", "<m-late@example.com>"]


def test_drain_skipped_when_still_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferred rows survive a drain attempt that finds the system
    still busy. They get re-attempted on the next IDLE pass."""
    watcher = _make_watcher(tmp_path)
    _record_received(watcher.state, "<still-busy@example.com>")
    watcher.state.mark_deferred(
        message_id="<still-busy@example.com>",
        from_state="RECEIVED",
        deferred_payload={
            "request_body": "x",
            "kind": agent_router.CLASS_AGENT_INIT,
            "session_id": None,
        },
    )

    monkeypatch.setattr(
        system_load, "is_system_busy",
        lambda policy: (True, "principal is in gaming mode (gamescope)"),
    )
    fake_dispatch = MagicMock()
    monkeypatch.setattr(
        watcher, "_dispatch_agent_request", fake_dispatch,
    )
    _run(watcher._drain_deferred_if_free())
    # Drain bailed out without dispatching anything.
    fake_dispatch.assert_not_called()
    # Row is still in QUEUED_DEFERRED, ready for next attempt.
    assert len(watcher.state.select_deferred_messages()) == 1


def test_drain_skips_corrupt_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row whose payload is missing a request_body or kind is
    skipped (logged warn, not dispatched, not crashed)."""
    watcher = _make_watcher(tmp_path)
    _record_received(watcher.state, "<corrupt@example.com>")
    watcher.state.mark_deferred(
        message_id="<corrupt@example.com>",
        from_state="RECEIVED",
        deferred_payload={"unrelated_key": "nonsense"},
    )

    monkeypatch.setattr(
        system_load, "is_system_busy", lambda policy: (False, "system free"),
    )
    fake_dispatch = MagicMock()
    monkeypatch.setattr(
        watcher, "_dispatch_agent_request", fake_dispatch,
    )
    _run(watcher._drain_deferred_if_free())
    fake_dispatch.assert_not_called()


def test_drain_no_deferred_rows_is_zero_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No deferred rows → no loginctl call, no dispatch. Cheap."""
    watcher = _make_watcher(tmp_path)
    busy_calls = MagicMock()
    monkeypatch.setattr(system_load, "is_system_busy", busy_calls)
    _run(watcher._drain_deferred_if_free())
    busy_calls.assert_not_called()
