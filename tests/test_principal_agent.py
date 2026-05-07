"""Tests for daemon/principal_agent.py.

The executor's external dependency is the `claude` CLI subprocess.
We monkey-patch asyncio.create_subprocess_exec to feed the executor
synthetic event-stream JSONL the way the real CLI emits.
"""
from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path
from typing import Any

import pytest

from daemon import principal_agent


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
        self._return_code = return_code
        self._wait_delay = wait_delay
        self._ignores_sigterm = ignores_sigterm
        self.signals_received: list[int] = []
        self.killed = False
        self._waited = asyncio.Event()

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
    assert types[0] == "_audit_session_start"
    assert "system" in types
    assert "assistant" in types
    assert "user" in types
    assert "result" in types
    assert types[-1] == "_audit_session_completed"


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
    has been written to the audit log first."""
    _patch_subprocess(
        monkeypatch, raise_exc=FileNotFoundError("nope"),
    )
    result = _run(principal_agent.execute(
        request_body="x",
        principal_name="DylanIsHere",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))
    assert result.status == "errored"
    audit_text = result.audit_log_path.read_text()
    assert "DylanIsHere" in audit_text
    assert "_audit_session_start" in audit_text
