"""Direct tests of inbox_watcher._send_agent_reply.

These pin the contract for the reply path:
- composed_body wins over final_text on the completed path.
- composed_subject wins over the default subject on the completed path.
- Killed and errored paths IGNORE composed_body / composed_subject —
  a partial run hasn't earned the right to claim "this is the final
  reply." This is decision #6 of the compose_reply rollout.
- The legacy final_text path still works when the agent didn't call
  the new MCP tool (backwards compatibility).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from daemon import inbox_watcher, principal_agent
from daemon.notifier import SendResult

from tests.test_agent_continuation_regression import _make_watcher


def _stub_send_result() -> SendResult:
    return SendResult(
        primary_message_id="<outbound@bot.example.com>",
        primary_sent=True,
        audit_sent=True,
        audit_queued=False,
        audit_id=None,
        error=None,
    )


def _capture_notify(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    def fake_notify_principal(**kwargs):
        captured.update(kwargs)
        return _stub_send_result()

    monkeypatch.setattr(
        inbox_watcher.notifier, "notify_principal", fake_notify_principal,
    )
    return captured


def _make_result(
    *,
    status: str = "completed",
    final_text: str = "",
    composed_body: str | None = None,
    composed_subject: str | None = None,
    attachments: tuple[principal_agent.AgentAttachment, ...] = (),
    tmp_path: Path,
) -> principal_agent.AgentResult:
    return principal_agent.AgentResult(
        session_id="deadbeef-1111-2222-3333-444444444444",
        status=status,
        final_text=final_text,
        audit_log_path=tmp_path / "audit.jsonl",
        started_at=1000,
        completed_at=1010,
        error_detail="" if status == "completed" else "for-test",
        composed_body=composed_body,
        composed_subject=composed_subject,
        attachments=attachments,
    )


def test_send_agent_reply_uses_composed_body_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """composed_body wins over final_text on the completed path."""
    watcher = _make_watcher(tmp_path)
    captured = _capture_notify(monkeypatch)
    # Pre-record the inbound message so _send_deterministic_reply
    # can transition() it without panicking.
    watcher.state.record_message(
        message_id="<inbound@principal.example.com>",
        inbox="nightjar", from_addr="me@example.com",
        subject="x", contact_id="principal", state="DISPATCHED",
        received_at=1000,
    )
    result = _make_result(
        composed_body="the real reply",
        final_text="ignore this scratch",
        tmp_path=tmp_path,
    )
    import asyncio
    asyncio.run(watcher._send_agent_reply(
        originating_message_id="<inbound@principal.example.com>",
        from_addr="me@example.com",
        result=result,
    ))
    assert "the real reply" in captured["body"]
    assert "ignore this scratch" not in captured["body"]


def test_send_agent_reply_uses_composed_subject_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """composed_subject overrides the default subject."""
    watcher = _make_watcher(tmp_path)
    captured = _capture_notify(monkeypatch)
    watcher.state.record_message(
        message_id="<inbound@principal.example.com>",
        inbox="nightjar", from_addr="me@example.com",
        subject="x", contact_id="principal", state="DISPATCHED",
        received_at=1000,
    )
    result = _make_result(
        composed_body="hi",
        composed_subject="Re: invoice",
        tmp_path=tmp_path,
    )
    import asyncio
    asyncio.run(watcher._send_agent_reply(
        originating_message_id="<inbound@principal.example.com>",
        from_addr="me@example.com",
        result=result,
    ))
    assert captured["subject"] == "Re: invoice"


def test_send_agent_reply_falls_back_to_final_text_when_no_composed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backwards compat: composed_body=None → final_text gets sent.
    Subject falls back to the default."""
    watcher = _make_watcher(tmp_path)
    captured = _capture_notify(monkeypatch)
    watcher.state.record_message(
        message_id="<inbound@principal.example.com>",
        inbox="nightjar", from_addr="me@example.com",
        subject="x", contact_id="principal", state="DISPATCHED",
        received_at=1000,
    )
    result = _make_result(
        composed_body=None,
        final_text="legacy reply via final_text",
        tmp_path=tmp_path,
    )
    import asyncio
    asyncio.run(watcher._send_agent_reply(
        originating_message_id="<inbound@principal.example.com>",
        from_addr="me@example.com",
        result=result,
    ))
    assert "legacy reply via final_text" in captured["body"]
    assert captured["subject"] == "Nightjar agent: response"


def test_send_agent_reply_killed_path_ignores_composed_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #6: the killed path uses the kill-template + final_text
    regardless of any composed_body. A partial run hasn't earned the
    right to claim 'this is the final reply.'"""
    watcher = _make_watcher(tmp_path)
    captured = _capture_notify(monkeypatch)
    watcher.state.record_message(
        message_id="<inbound@principal.example.com>",
        inbox="nightjar", from_addr="me@example.com",
        subject="x", contact_id="principal", state="DISPATCHED",
        received_at=1000,
    )
    result = _make_result(
        status="killed",
        final_text="some partial output",
        composed_body="this should NOT be used",
        composed_subject="this subject should NOT be used",
        tmp_path=tmp_path,
    )
    import asyncio
    asyncio.run(watcher._send_agent_reply(
        originating_message_id="<inbound@principal.example.com>",
        from_addr="me@example.com",
        result=result,
    ))
    assert "this should NOT be used" not in captured["body"]
    assert captured["subject"].startswith("Nightjar agent: session killed")


def test_send_agent_reply_errored_path_ignores_composed_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same isolation rule on the errored path."""
    watcher = _make_watcher(tmp_path)
    captured = _capture_notify(monkeypatch)
    watcher.state.record_message(
        message_id="<inbound@principal.example.com>",
        inbox="nightjar", from_addr="me@example.com",
        subject="x", contact_id="principal", state="DISPATCHED",
        received_at=1000,
    )
    result = _make_result(
        status="errored",
        final_text="partial",
        composed_body="must not appear",
        composed_subject="must not appear either",
        tmp_path=tmp_path,
    )
    import asyncio
    asyncio.run(watcher._send_agent_reply(
        originating_message_id="<inbound@principal.example.com>",
        from_addr="me@example.com",
        result=result,
    ))
    assert "must not appear" not in captured["body"]
    assert captured["subject"].startswith("Nightjar agent: session errored")


def test_send_agent_reply_passes_attachments_on_completed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentResult.attachments → notify_principal called with the
    same files translated to AttachmentSpec."""
    watcher = _make_watcher(tmp_path)
    captured = _capture_notify(monkeypatch)
    watcher.state.record_message(
        message_id="<inbound@principal.example.com>",
        inbox="nightjar", from_addr="me@example.com",
        subject="x", contact_id="principal", state="DISPATCHED",
        received_at=1000,
    )
    target1 = tmp_path / "doc.txt"
    target1.write_text("hello")
    target2 = tmp_path / "report.pdf"
    target2.write_bytes(b"%PDF-1.4 fake")
    result = _make_result(
        composed_body="here are your files",
        attachments=(
            principal_agent.AgentAttachment(
                path=target1, filename="renamed.txt"),
            principal_agent.AgentAttachment(path=target2),
        ),
        tmp_path=tmp_path,
    )
    import asyncio
    asyncio.run(watcher._send_agent_reply(
        originating_message_id="<inbound@principal.example.com>",
        from_addr="me@example.com",
        result=result,
    ))
    sent_attachments = captured["attachments"]
    assert len(sent_attachments) == 2
    assert sent_attachments[0].path == target1
    assert sent_attachments[0].filename == "renamed.txt"
    assert sent_attachments[1].path == target2
    assert sent_attachments[1].filename is None


def test_send_agent_reply_no_attachments_passes_empty_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No attachments on AgentResult → notify_principal called with
    empty attachments tuple. Backwards-compat for the common case."""
    watcher = _make_watcher(tmp_path)
    captured = _capture_notify(monkeypatch)
    watcher.state.record_message(
        message_id="<inbound@principal.example.com>",
        inbox="nightjar", from_addr="me@example.com",
        subject="x", contact_id="principal", state="DISPATCHED",
        received_at=1000,
    )
    result = _make_result(composed_body="hi", tmp_path=tmp_path)
    import asyncio
    asyncio.run(watcher._send_agent_reply(
        originating_message_id="<inbound@principal.example.com>",
        from_addr="me@example.com",
        result=result,
    ))
    assert captured["attachments"] == ()


def test_send_agent_reply_killed_path_drops_attachments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per decision #6 carry-over: attachments are completed-path-only.
    A killed run with attachments populated must NOT pass them
    through to the notifier."""
    watcher = _make_watcher(tmp_path)
    captured = _capture_notify(monkeypatch)
    watcher.state.record_message(
        message_id="<inbound@principal.example.com>",
        inbox="nightjar", from_addr="me@example.com",
        subject="x", contact_id="principal", state="DISPATCHED",
        received_at=1000,
    )
    target = tmp_path / "would-not-attach.txt"
    target.write_text("nope")
    result = _make_result(
        status="killed",
        final_text="partial",
        attachments=(
            principal_agent.AgentAttachment(path=target),
        ),
        tmp_path=tmp_path,
    )
    import asyncio
    asyncio.run(watcher._send_agent_reply(
        originating_message_id="<inbound@principal.example.com>",
        from_addr="me@example.com",
        result=result,
    ))
    assert captured["attachments"] == ()
