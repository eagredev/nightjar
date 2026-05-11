"""Tests for daemon/principal_agent.py.

The executor's external dependency is the `claude` CLI subprocess.
We monkey-patch asyncio.create_subprocess_exec to feed the executor
synthetic event-stream JSONL the way the real CLI emits.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from daemon import principal_agent


# Fake-PID generator. The executor calls os.killpg(proc.pid, ...) so
# every _FakeProc needs a unique pid that os.killpg patches can route
# back to the right fake. Real PIDs are >0, <2^22; this stays out of
# that range so a stray real-process killpg can't collide.
_FAKE_PID_COUNTER = itertools.count(start=999_000_001)
_FAKE_PROCS_BY_PID: dict[int, "_FakeProc"] = {}


@pytest.fixture(autouse=True)
def _patch_killpg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route os.killpg(pgid, sig) for fake-test PIDs to the registered
    _FakeProc's send_signal/kill methods. Real-PID killpgs (which the
    test suite should never make) are passed through to os."""
    real_killpg = os.killpg

    def _fake_killpg(pgid: int, sig: int) -> None:
        proc = _FAKE_PROCS_BY_PID.get(pgid)
        if proc is None:
            real_killpg(pgid, sig)
            return
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.send_signal(sig)

    monkeypatch.setattr(principal_agent.os, "killpg", _fake_killpg)
    yield
    _FAKE_PROCS_BY_PID.clear()


# ---- Fake subprocess scaffolding ------------------------------------------


class _FakeStream:
    """Minimal asyncio.StreamReader stand-in. Returns lines from a
    pre-populated queue, then EOF (returns b"")."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self, *_args: Any, **_kwargs: Any) -> bytes:
        return b""


class _FakeStdin:
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    """Stand-in for asyncio.subprocess.Process."""

    def __init__(
        self,
        *,
        stream_lines: list[bytes],
        return_code: int = 0,
        wait_delay: float = 0.0,
        ignores_sigterm: bool = False,
    ) -> None:
        self.stdout = _FakeStream(stream_lines)
        self.stderr = _FakeStream([])
        self.stdin = _FakeStdin()
        self.pid = next(_FAKE_PID_COUNTER)
        self._return_code = return_code
        self._wait_delay = wait_delay
        self._ignores_sigterm = ignores_sigterm
        self.signals_received: list[int] = []
        self.killed = False
        self._waited = asyncio.Event()
        # Register so our os.killpg patch can route back to this fake.
        _FAKE_PROCS_BY_PID[self.pid] = self

    async def wait(self) -> int:
        if self._wait_delay > 0:
            try:
                await asyncio.wait_for(
                    self._waited.wait(), timeout=self._wait_delay,
                )
            except asyncio.TimeoutError:
                pass
        return self._return_code

    def send_signal(self, sig: int) -> None:
        self.signals_received.append(sig)
        if not self._ignores_sigterm:
            self._waited.set()

    def kill(self) -> None:
        self.killed = True
        self._waited.set()


def _ev(typ: str, **kwargs: Any) -> bytes:
    return (json.dumps({"type": typ, **kwargs}) + "\n").encode("utf-8")


def _assistant_text(text: str) -> bytes:
    return _ev(
        "assistant",
        message={"content": [{"type": "text", "text": text}]},
    )


def _result(stop_reason: str = "end_turn") -> bytes:
    return _ev(
        "result",
        subtype="success",
        is_error=False,
        result="",
        stop_reason=stop_reason,
        usage={"input_tokens": 1, "output_tokens": 1},
    )


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proc: _FakeProc | None = None,
    raise_exc: BaseException | None = None,
    capture: dict[str, Any] | None = None,
) -> None:
    async def _fake_create(*args, **kwargs):  # type: ignore[no-untyped-def]
        if capture is not None:
            capture["args"] = args
            capture["kwargs"] = kwargs
        if raise_exc is not None:
            raise raise_exc
        return proc

    monkeypatch.setattr(
        principal_agent.asyncio,
        "create_subprocess_exec",
        _fake_create,
    )


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# ---- Happy path -----------------------------------------------------------


def test_execute_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(stream_lines=[
        _ev("system", subtype="init"),
        _assistant_text("Done. The file is at ~/agent-scratch/note.txt."),
        _result(),
    ])
    capture: dict[str, Any] = {}
    _patch_subprocess(monkeypatch, proc=proc, capture=capture)
    result = _run(principal_agent.execute(
        request_body="write a note to ~/agent-scratch/note.txt",
        principal_name="Dylan",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.status == "completed"
    assert "agent-scratch/note.txt" in result.final_text
    # Audit log was written and contains the events.
    audit_text = result.audit_log_path.read_text()
    assert "_audit_session_start" in audit_text
    assert "_audit_session_completed" in audit_text
    # Subprocess was fed the request body via stdin.
    assert proc.stdin.written.decode().startswith("write a note")
    assert proc.stdin.closed


def test_execute_passes_session_id_for_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProc(stream_lines=[_assistant_text("ok"), _result()])
    capture: dict[str, Any] = {}
    _patch_subprocess(monkeypatch, proc=proc, capture=capture)
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    cmd = capture["args"]
    assert "--session-id" in cmd
    idx = cmd.index("--session-id")
    assert cmd[idx + 1] == result.session_id
    assert "--resume" not in cmd


def test_execute_passes_resume_for_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProc(stream_lines=[_assistant_text("ok"), _result()])
    capture: dict[str, Any] = {}
    _patch_subprocess(monkeypatch, proc=proc, capture=capture)
    result = _run(principal_agent.execute(
        request_body="continue",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
        session_id="existing-session-uuid",
    ))
    assert result.session_id == "existing-session-uuid"
    cmd = capture["args"]
    assert "--resume" in cmd
    idx = cmd.index("--resume")
    assert cmd[idx + 1] == "existing-session-uuid"
    assert "--session-id" not in cmd


def test_execute_audit_log_written_per_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each event arrives as its own JSONL line in the audit log."""
    proc = _FakeProc(stream_lines=[
        _ev("system", subtype="init"),
        _ev("assistant", message={"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]}),
        _ev("user", message={"content": [
            {"type": "tool_result", "content": "file1\nfile2"},
        ]}),
        _assistant_text("Listed two files."),
        _result(),
    ])
    _patch_subprocess(monkeypatch, proc=proc)
    result = _run(principal_agent.execute(
        request_body="ls",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    lines = [
        json.loads(l) for l in result.audit_log_path.read_text().splitlines()
        if l.strip()
    ]
    types = [l.get("type") for l in lines]
    # Should have start envelope, the four CLI events, completed envelope.
    # `_audit_compose_reply_missing` may follow `_audit_session_completed`
    # on the completed path when the agent didn't call compose_reply
    # (which is the case here — the synthetic stream-json doesn't
    # invoke the tool). So check `_audit_session_completed` is present
    # rather than that it's the final entry.
    assert types[0] == "_audit_session_start"
    assert "system" in types
    assert "assistant" in types
    assert "user" in types
    assert "result" in types
    assert "_audit_session_completed" in types


def test_execute_captures_last_text_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple assistant turns: only the last text block becomes final_text."""
    proc = _FakeProc(stream_lines=[
        _assistant_text("intermediate thought"),
        _assistant_text("final answer"),
        _result(),
    ])
    _patch_subprocess(monkeypatch, proc=proc)
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.final_text == "final answer"


# ---- Error paths ----------------------------------------------------------


def test_execute_executable_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_subprocess(
        monkeypatch, raise_exc=FileNotFoundError("no claude here"),
    )
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.status == "errored"
    assert "not found" in result.error_detail


def test_execute_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProc(
        stream_lines=[_assistant_text("partial"), _result()],
        return_code=2,
    )
    _patch_subprocess(monkeypatch, proc=proc)
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.status == "errored"
    assert "exited with code 2" in result.error_detail
    # Final text from before the error is still surfaced.
    assert result.final_text == "partial"


def test_execute_malformed_json_line_recorded_not_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad JSON line in the stream is recorded as _audit_unparseable
    but doesn't kill the run."""
    proc = _FakeProc(stream_lines=[
        b"this is not json\n",
        _assistant_text("kept going"),
        _result(),
    ])
    _patch_subprocess(monkeypatch, proc=proc)
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.status == "completed"
    assert result.final_text == "kept going"
    audit_text = result.audit_log_path.read_text()
    assert "_audit_unparseable" in audit_text


# ---- DMS / cancellation paths --------------------------------------------


def test_execute_killed_by_stop_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop event fires while subprocess is producing → SIGTERM sent,
    status=killed."""
    # Use a slow stream that won't finish before we set the stop.
    slow_lines: list[bytes] = []  # empty stream → readline returns b"" immediately
    # Actually we want the stream to NOT close immediately — use a
    # reader that hangs forever instead.

    class _BlockingStream:
        async def readline(self) -> bytes:
            await asyncio.Event().wait()  # never set
            return b""
        async def read(self, *args, **kwargs) -> bytes:
            return b""

    proc = _FakeProc(stream_lines=[], return_code=143)
    proc.stdout = _BlockingStream()  # type: ignore[assignment]
    _patch_subprocess(monkeypatch, proc=proc)

    async def _drive() -> Any:
        stop = asyncio.Event()
        task = asyncio.create_task(principal_agent.execute(
            request_body="x",
            principal_name="P",
            audit_dir=tmp_path / "audit",
            cwd=tmp_path,
            stop_event=stop,
            timeout_seconds=30,
        ))
        # Let the executor reach asyncio.wait, then trip stop.
        await asyncio.sleep(0.05)
        stop.set()
        return await task

    result = _run(_drive())
    assert result.status == "killed"
    assert result.error_detail == "stop_event"
    assert signal.SIGTERM in proc.signals_received
    audit_text = result.audit_log_path.read_text()
    assert "_audit_session_killed" in audit_text


def test_execute_killed_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BlockingStream:
        async def readline(self) -> bytes:
            await asyncio.Event().wait()
            return b""
        async def read(self, *args, **kwargs) -> bytes:
            return b""

    proc = _FakeProc(stream_lines=[])
    proc.stdout = _BlockingStream()  # type: ignore[assignment]
    _patch_subprocess(monkeypatch, proc=proc)
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
        timeout_seconds=1,
    ))
    assert result.status == "killed"
    assert result.error_detail == "timeout"
    assert signal.SIGTERM in proc.signals_received


def test_execute_force_kill_when_sigterm_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subprocess that ignores SIGTERM gets SIGKILL'd after the grace
    period."""
    class _BlockingStream:
        async def readline(self) -> bytes:
            await asyncio.Event().wait()
            return b""
        async def read(self, *args, **kwargs) -> bytes:
            return b""

    # ignores_sigterm + return_code stays default 0 but we'll never
    # reach normal completion. Wait won't return until kill() is called.
    proc = _FakeProc(stream_lines=[], ignores_sigterm=True, wait_delay=10.0)
    proc.stdout = _BlockingStream()  # type: ignore[assignment]
    _patch_subprocess(monkeypatch, proc=proc)
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
        timeout_seconds=1,
    ))
    assert result.status == "killed"
    assert proc.killed  # SIGKILL was fired


# ---- Misc -----------------------------------------------------------------


def test_execute_session_start_envelope_records_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if the spawn fails immediately, the session-start envelope
    has been written to the audit log first, including the request
    body (so forensics can answer 'what did the principal ask?'
    without having to re-fetch from IMAP)."""
    _patch_subprocess(
        monkeypatch, raise_exc=FileNotFoundError("nope"),
    )
    result = _run(principal_agent.execute(
        request_body="please draft a response to Bob about the meeting",
        principal_name="DylanIsHere",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.status == "errored"
    audit_text = result.audit_log_path.read_text()
    assert "DylanIsHere" in audit_text
    assert "_audit_session_start" in audit_text
    # The first JSONL line is the start envelope; parse it and check
    # the request_body is present and faithful.
    first = json.loads(audit_text.splitlines()[0])
    assert first["type"] == "_audit_session_start"
    assert first["request_body"] == (
        "please draft a response to Bob about the meeting"
    )


def test_audit_session_start_records_request_body_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Init session: request_body lands in _audit_session_start
    even when the spawn errors immediately (forensics blind-spot
    fix). HOTP codes are already stripped by agent_router before
    the body reaches execute(), so what's recorded is the body
    after auth-line removal."""
    _patch_subprocess(monkeypatch, raise_exc=FileNotFoundError("no"))
    body = "summarise inbox and tell me what's urgent"
    result = _run(principal_agent.execute(
        request_body=body,
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.status == "errored"
    lines = result.audit_log_path.read_text().splitlines()
    envelope = json.loads(lines[0])
    assert envelope["type"] == "_audit_session_start"
    assert envelope["is_continuation"] is False
    assert envelope["request_body"] == body


def test_audit_session_start_records_request_body_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuation session: same envelope contract applies. The
    body is the principal's reply (line-1-stripped); same forensics
    argument."""
    _patch_subprocess(monkeypatch, raise_exc=FileNotFoundError("no"))
    body = "yes go ahead with that draft"
    result = _run(principal_agent.execute(
        request_body=body,
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
        session_id="continuing-session-uuid",
    ))
    assert result.status == "errored"
    lines = result.audit_log_path.read_text().splitlines()
    envelope = json.loads(lines[0])
    assert envelope["type"] == "_audit_session_start"
    assert envelope["is_continuation"] is True
    assert envelope["request_body"] == body


# ---- compose_reply MCP integration ----------------------------------------
#
# These tests verify the executor's interaction with the compose_reply
# stdio MCP server. Most monkeypatch `_read_compose_reply_log` so we
# exercise the propagation logic without needing real file I/O. One
# integration-shaped test pre-writes a real JSONL fixture to verify
# the format-level read path.


def test_execute_passes_mcp_config_for_compose_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spawn cmd must include --mcp-config with a JSON value
    that registers the nightjar-reply MCP server pointing at the
    correct env path."""
    proc = _FakeProc(stream_lines=[_assistant_text("ok"), _result()])
    capture: dict[str, Any] = {}
    _patch_subprocess(monkeypatch, proc=proc, capture=capture)
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    cmd = list(capture["args"])
    assert "--mcp-config" in cmd
    cfg_str = cmd[cmd.index("--mcp-config") + 1]
    cfg = json.loads(cfg_str)
    server = cfg["mcpServers"]["nightjar-reply"]
    assert server["type"] == "stdio"
    assert server["command"] == "python3"
    assert server["args"][0].endswith("compose_reply_mcp.py")
    expected_log = (
        tmp_path / "audit" / f"{result.session_id}.compose-reply.jsonl"
    )
    assert server["env"]["NIGHTJAR_COMPOSE_REPLY_LOG"] == str(expected_log)


def test_execute_compose_reply_body_only_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body-only call → composed_body set, composed_subject is None,
    no missing-event in audit log."""
    proc = _FakeProc(stream_lines=[_assistant_text("scratch"), _result()])
    _patch_subprocess(monkeypatch, proc=proc)
    monkeypatch.setattr(
        principal_agent, "_read_compose_reply_log",
        lambda _path: ("the real reply", None),
    )
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.status == "completed"
    assert result.composed_body == "the real reply"
    assert result.composed_subject is None
    assert "_audit_compose_reply_missing" not in result.audit_log_path.read_text()


def test_execute_compose_reply_with_subject_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body+subject call → both surface on AgentResult."""
    proc = _FakeProc(stream_lines=[_assistant_text("scratch"), _result()])
    _patch_subprocess(monkeypatch, proc=proc)
    monkeypatch.setattr(
        principal_agent, "_read_compose_reply_log",
        lambda _path: ("hi", "Re: meeting"),
    )
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.composed_body == "hi"
    assert result.composed_subject == "Re: meeting"


def test_execute_compose_reply_missing_emits_audit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No compose_reply call on a clean run → composed_body is None
    AND `_audit_compose_reply_missing` event lands in the audit log
    so adoption is observable."""
    proc = _FakeProc(stream_lines=[_assistant_text("legacy reply"), _result()])
    _patch_subprocess(monkeypatch, proc=proc)
    monkeypatch.setattr(
        principal_agent, "_read_compose_reply_log",
        lambda _path: (None, None),
    )
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.status == "completed"
    assert result.composed_body is None
    assert result.composed_subject is None
    audit_text = result.audit_log_path.read_text()
    assert "_audit_compose_reply_missing" in audit_text


def test_execute_compose_reply_missing_not_emitted_on_errored_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On non-zero exit, we don't emit the missing-event — the agent
    didn't have the chance to call the tool cleanly. Adoption metric
    should only count completed sessions."""
    proc = _FakeProc(
        stream_lines=[_assistant_text("partial"), _result()],
        return_code=2,
    )
    _patch_subprocess(monkeypatch, proc=proc)
    monkeypatch.setattr(
        principal_agent, "_read_compose_reply_log",
        lambda _path: (None, None),
    )
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.status == "errored"
    audit_text = result.audit_log_path.read_text()
    assert "_audit_compose_reply_missing" not in audit_text


def test_read_compose_reply_log_jsonl_format(
    tmp_path: Path,
) -> None:
    """Format check on the reader directly: a JSONL with two valid
    entries plus a malformed middle line returns the last valid one.

    (Originally this drove `execute()` end-to-end, but per-turn
    truncation now wipes any pre-written log before the spawn — so
    the format check has to target the reader function directly.)"""
    log_path = tmp_path / "compose-reply.jsonl"
    log_path.write_text(
        json.dumps({"ts": 1, "body": "draft 1", "subject": None,
                    "call_id": 1}) + "\n"
        "{not valid json\n"
        + json.dumps({"ts": 2, "body": "final reply",
                      "subject": "Re: x", "call_id": 2}) + "\n",
        encoding="utf-8",
    )
    body, subject = principal_agent._read_compose_reply_log(log_path)
    assert body == "final reply"
    assert subject == "Re: x"


def test_read_compose_reply_log_empty_body_treated_as_no_call(
    tmp_path: Path,
) -> None:
    """compose_reply(body="") is treated as no call (decision #7)."""
    log = tmp_path / "calls.jsonl"
    log.write_text(
        json.dumps({"ts": 1, "body": "", "subject": None}) + "\n",
        encoding="utf-8",
    )
    body, subject = principal_agent._read_compose_reply_log(log)
    assert body is None
    assert subject is None


def test_read_compose_reply_log_whitespace_only_body_treated_as_no_call(
    tmp_path: Path,
) -> None:
    """compose_reply(body="   \\n\\t") is also treated as no call."""
    log = tmp_path / "calls.jsonl"
    log.write_text(
        json.dumps({"ts": 1, "body": "   \n\t  ", "subject": None}) + "\n",
        encoding="utf-8",
    )
    body, subject = principal_agent._read_compose_reply_log(log)
    assert body is None


def test_read_compose_reply_log_missing_file_returns_none_pair(
    tmp_path: Path,
) -> None:
    body, subject = principal_agent._read_compose_reply_log(
        tmp_path / "does-not-exist.jsonl",
    )
    assert body is None
    assert subject is None


# ---- attach_to_reply MCP integration -------------------------------------


def test_execute_passes_attachments_env_var_to_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The --mcp-config JSON must include NIGHTJAR_ATTACHMENTS_LOG
    pointing at a sibling file alongside the compose-reply log."""
    proc = _FakeProc(stream_lines=[_assistant_text("ok"), _result()])
    capture: dict[str, Any] = {}
    _patch_subprocess(monkeypatch, proc=proc, capture=capture)
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    cmd = list(capture["args"])
    cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])
    server = cfg["mcpServers"]["nightjar-reply"]
    expected = (
        tmp_path / "audit" / f"{result.session_id}.attachments.jsonl"
    )
    assert server["env"]["NIGHTJAR_ATTACHMENTS_LOG"] == str(expected)


def test_execute_attachments_propagate_when_log_has_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attachments JSONL with two entries → AgentResult.attachments
    has two AgentAttachment instances in order."""
    proc = _FakeProc(stream_lines=[_assistant_text("scratch"), _result()])
    _patch_subprocess(monkeypatch, proc=proc)
    monkeypatch.setattr(
        principal_agent, "_read_attachments_log",
        lambda _path: (
            principal_agent.AgentAttachment(
                path=Path("/tmp/a.txt"), filename="a.txt"),
            principal_agent.AgentAttachment(
                path=Path("/tmp/b.pdf"), filename=None),
        ),
    )
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert len(result.attachments) == 2
    assert result.attachments[0].path == Path("/tmp/a.txt")
    assert result.attachments[0].filename == "a.txt"
    assert result.attachments[1].path == Path("/tmp/b.pdf")
    assert result.attachments[1].filename is None


def test_execute_attachments_default_empty_when_no_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No attachments log file → AgentResult.attachments == ()."""
    proc = _FakeProc(stream_lines=[_assistant_text("ok"), _result()])
    _patch_subprocess(monkeypatch, proc=proc)
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.attachments == ()


def test_read_attachments_log_jsonl_format(
    tmp_path: Path,
) -> None:
    """Format check on the reader directly: a JSONL with two valid
    entries plus a malformed middle line returns the two valid ones
    in order.

    (Originally this drove `execute()` end-to-end, but per-turn
    truncation now wipes any pre-written log before the spawn — so
    the format check has to target the reader function directly.)"""
    log_path = tmp_path / "attachments.jsonl"
    log_path.write_text(
        json.dumps({"ts": 1, "path": "/tmp/x.zip",
                    "filename": "report.zip", "size_bytes": 100}) + "\n"
        "{not valid json\n"
        + json.dumps({"ts": 2, "path": "/tmp/y.txt",
                      "filename": None, "size_bytes": 5}) + "\n",
        encoding="utf-8",
    )
    attachments = principal_agent._read_attachments_log(log_path)
    assert len(attachments) == 2
    assert attachments[0].path == Path("/tmp/x.zip")
    assert attachments[0].filename == "report.zip"
    assert attachments[1].path == Path("/tmp/y.txt")
    assert attachments[1].filename is None


def test_read_attachments_log_missing_file_returns_empty(
    tmp_path: Path,
) -> None:
    assert principal_agent._read_attachments_log(
        tmp_path / "no-such.jsonl",
    ) == ()


def test_read_attachments_log_skips_entries_without_path(
    tmp_path: Path,
) -> None:
    log = tmp_path / "att.jsonl"
    log.write_text(
        json.dumps({"ts": 1, "filename": "no-path.txt"}) + "\n"
        + json.dumps({"ts": 2, "path": "", "filename": "empty.txt"}) + "\n"
        + json.dumps({"ts": 3, "path": "/tmp/ok.txt"}) + "\n",
        encoding="utf-8",
    )
    result = principal_agent._read_attachments_log(log)
    assert len(result) == 1
    assert result[0].path == Path("/tmp/ok.txt")


# ---- Per-turn log truncation ----------------------------------------------
#
# Both the compose-reply and attachments JSONL files are session-scoped on
# disk but their contents are turn-scoped: each turn's calls are what the
# daemon should send. On a `--resume`d continuation the same paths are
# reused, so without truncation a turn-N call silently re-fires on turn
# N+1. These tests pin the per-turn truncation that prevents that.
#
# Bug: 2026-05-08, found by Nightjar in session 92a3ea47-...,
# write-up at proposals/attachments-log-per-turn-bug.md.


def test_execute_truncates_attachments_log_at_turn_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing attachments log from a prior turn is wiped before
    the new turn spawns, so prior attachments don't re-fire."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    session_id = "00000000-0000-0000-0000-000000000001"
    stale_log = audit_dir / f"{session_id}.attachments.jsonl"
    stale_log.write_text(
        json.dumps({
            "ts": 1, "path": "/tmp/from-prior-turn.txt",
            "filename": None, "size_bytes": 10, "call_id": 1,
        }) + "\n",
        encoding="utf-8",
    )
    proc = _FakeProc(stream_lines=[_assistant_text("ok"), _result()])
    _patch_subprocess(monkeypatch, proc=proc)
    result = _run(principal_agent.execute(
        request_body="continuation",
        principal_name="P",
        audit_dir=audit_dir,
        cwd=tmp_path,
        session_id=session_id,
    ))
    # No attachments — the stale log was wiped before the turn.
    assert result.attachments == ()
    # And the file does not contain the stale entry post-run.
    if stale_log.exists():
        text = stale_log.read_text(encoding="utf-8")
        assert "from-prior-turn.txt" not in text


def test_execute_truncates_compose_reply_log_at_turn_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing compose-reply log from a prior turn is wiped before
    the new turn spawns, so the prior body doesn't silently re-send."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    session_id = "00000000-0000-0000-0000-000000000002"
    stale_log = audit_dir / f"{session_id}.compose-reply.jsonl"
    stale_log.write_text(
        json.dumps({
            "ts": 1, "body": "stale body from turn 1",
            "subject": "stale subject", "call_id": 1,
        }) + "\n",
        encoding="utf-8",
    )
    proc = _FakeProc(stream_lines=[_assistant_text("fresh fallback"), _result()])
    _patch_subprocess(monkeypatch, proc=proc)
    result = _run(principal_agent.execute(
        request_body="continuation",
        principal_name="P",
        audit_dir=audit_dir,
        cwd=tmp_path,
        session_id=session_id,
    ))
    # Stale body did NOT survive the turn boundary.
    assert result.composed_body is None
    assert result.composed_subject is None
    # And the missing-event fired (because no compose_reply called this turn).
    audit_text = result.audit_log_path.read_text()
    assert "_audit_compose_reply_missing" in audit_text


def test_execute_truncate_is_idempotent_when_logs_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truncate when the files don't yet exist (init turn) does not crash."""
    audit_dir = tmp_path / "audit"  # not created — execute should mkdir
    proc = _FakeProc(stream_lines=[_assistant_text("ok"), _result()])
    _patch_subprocess(monkeypatch, proc=proc)
    result = _run(principal_agent.execute(
        request_body="initial",
        principal_name="P",
        audit_dir=audit_dir,
        cwd=tmp_path,
    ))
    assert result.status == "completed"
    assert result.attachments == ()
    assert result.composed_body is None


# ---- Silent-wedge incident 2026-05-07T21-37 regression tests --------------
#
# Three bugs combined to produce an unsupervised post-stream-error agent:
#   X) executor's stream-error branch returned without killing the subprocess
#   Y) asyncio StreamReader's 64KB default chunk limit killed the stream task
#      on long stream-json frames
#   Z) defence-in-depth: claude wasn't in its own session/PGID, so even if
#      the parent died, descendants might not
# These tests pin all three so we don't regress.


def test_subprocess_spawned_with_high_stream_limit_and_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug Y + defence-in-depth: create_subprocess_exec must be called
    with a high `limit` (StreamReader chunk ceiling) and
    `start_new_session=True` (puts claude in its own PGID for clean
    group-kill on cleanup)."""
    proc = _FakeProc(stream_lines=[
        _ev("system", subtype="init"),
        _assistant_text("ok"),
        _result(),
    ])
    capture: dict[str, Any] = {}
    _patch_subprocess(monkeypatch, proc=proc, capture=capture)
    _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    kwargs = capture["kwargs"]
    # Bug Y: limit must be set to a value well above the asyncio
    # default of 64KB so a long stream-json frame doesn't kill the
    # reader. We don't pin the exact value (it can be tuned) but it
    # must be at least 1 MiB.
    assert "limit" in kwargs, "limit kwarg missing — Bug Y regression"
    assert kwargs["limit"] >= 1024 * 1024, (
        f"limit={kwargs['limit']} too small; long stream-json frames "
        f"will trigger 'Separator is found, but chunk is longer than "
        f"limit' and silently wedge the agent"
    )
    # Defence-in-depth: start_new_session=True so proc.pid == PGID,
    # enabling killpg(pgid, sig) to reach every descendant.
    assert kwargs.get("start_new_session") is True, (
        "start_new_session not set — agent's child processes won't "
        "die when the parent does"
    )


def test_execute_kills_subprocess_on_stream_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug X: the silent-wedge bug. If the stream task throws (e.g.
    asyncio StreamReader hits its 64KB chunk limit), the executor
    USED to return status='errored' without touching the subprocess,
    leaving claude alive and looping unsupervised. Now it must kill
    the process group before returning."""

    class _ThrowingStream:
        """First readline call raises — simulates the StreamReader
        chunk-limit exception that triggered the production wedge."""
        async def readline(self) -> bytes:
            raise ValueError(
                "Separator is found, but chunk is longer than limit"
            )
        async def read(self, *args, **kwargs) -> bytes:
            return b""

    proc = _FakeProc(stream_lines=[], wait_delay=10.0)
    proc.stdout = _ThrowingStream()  # type: ignore[assignment]
    _patch_subprocess(monkeypatch, proc=proc)

    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
        timeout_seconds=30,
    ))

    assert result.status == "errored"
    assert "stream error" in result.error_detail
    # The subprocess MUST have received SIGTERM (via os.killpg → fake
    # send_signal). Without this assert, the production silent-wedge
    # comes back: an agent declared errored to the principal but still
    # running unsupervised on the machine.
    assert signal.SIGTERM in proc.signals_received, (
        "subprocess never received SIGTERM after stream error — "
        "Bug X regression. Agent leaked."
    )
    audit_text = result.audit_log_path.read_text()
    assert "_audit_stream_error" in audit_text


def test_execute_force_kills_subprocess_after_stream_error_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a subprocess that's hit a stream error ALSO ignores SIGTERM
    (e.g. because it's wedged inside a kernel call), the cleanup helper
    must escalate to SIGKILL after the grace period."""

    class _ThrowingStream:
        async def readline(self) -> bytes:
            raise ValueError("simulated stream error")
        async def read(self, *args, **kwargs) -> bytes:
            return b""

    # ignores_sigterm + a long wait_delay → wait() doesn't return on
    # SIGTERM, forcing the helper to escalate.
    proc = _FakeProc(
        stream_lines=[], ignores_sigterm=True, wait_delay=30.0,
    )
    proc.stdout = _ThrowingStream()  # type: ignore[assignment]
    _patch_subprocess(monkeypatch, proc=proc)

    # Shrink the grace so the test runs quickly.
    monkeypatch.setattr(principal_agent, "KILL_GRACE_SECONDS", 0.1)

    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
        timeout_seconds=30,
    ))

    assert result.status == "errored"
    assert proc.killed, (
        "subprocess didn't get SIGKILL after ignoring SIGTERM — "
        "stream-error path doesn't escalate"
    )


def test_execute_uses_killpg_for_group_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence-in-depth: cleanup must signal the process group, not
    the parent. Verifies via spy on os.killpg that it was called with
    proc.pid (which equals the PGID under start_new_session)."""

    class _BlockingStream:
        async def readline(self) -> bytes:
            await asyncio.Event().wait()
            return b""
        async def read(self, *args, **kwargs) -> bytes:
            return b""

    proc = _FakeProc(stream_lines=[])
    proc.stdout = _BlockingStream()  # type: ignore[assignment]
    _patch_subprocess(monkeypatch, proc=proc)

    # Replace the autouse killpg patch with a spy variant so we can
    # assert the exact (pgid, sig) pairs.
    killpg_calls: list[tuple[int, int]] = []
    real_send_signal = proc.send_signal

    def _spy_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))
        # Still route to the fake so the timeout branch can complete.
        if pgid == proc.pid:
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                real_send_signal(sig)

    monkeypatch.setattr(principal_agent.os, "killpg", _spy_killpg)

    _run(principal_agent.execute(
        request_body="x",
        principal_name="P",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
        timeout_seconds=1,
    ))

    assert any(pgid == proc.pid and sig == signal.SIGTERM
               for pgid, sig in killpg_calls), (
        f"os.killpg(pid, SIGTERM) was not called; saw {killpg_calls}. "
        f"Bug: cleanup is signalling the parent only, not the group."
    )
