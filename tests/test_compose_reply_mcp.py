"""Protocol-level tests for daemon/compose_reply_mcp.py.

The MCP server is a stdio JSON-RPC 2.0 process. We drive it via
subprocess.Popen with stdin/stdout pipes, write JSON-RPC frames,
read responses, and inspect the JSONL log it produces. Stdlib I/O
only — no pytest-asyncio, no third-party MCP client.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SERVER_PATH = (
    Path(__file__).resolve().parent.parent
    / "daemon" / "compose_reply_mcp.py"
)


def _spawn(log_path: Path,
           attachments_log: Path | None = None) -> subprocess.Popen[str]:
    """Spawn the MCP server with the given env and pipes for stdio.

    Caller is responsible for closing stdin and waiting for exit.
    `attachments_log` defaults to a sibling of `log_path` so existing
    compose-reply-only tests don't need to construct one explicitly.
    """
    if attachments_log is None:
        attachments_log = log_path.parent / "attachments.jsonl"
    return subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "NIGHTJAR_COMPOSE_REPLY_LOG": str(log_path),
            "NIGHTJAR_ATTACHMENTS_LOG": str(attachments_log),
        },
        text=True,
        bufsize=1,  # line-buffered so each frame flushes promptly
    )


def _send(proc: subprocess.Popen[str], message: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _recv(proc: subprocess.Popen[str]) -> dict:
    """Read one JSON-RPC response from stdout."""
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        # Drain stderr so test failures show what the server said.
        err = proc.stderr.read() if proc.stderr else ""
        raise AssertionError(f"server closed stdout; stderr={err!r}")
    return json.loads(line)


def _close_and_wait(proc: subprocess.Popen[str], timeout: float = 5.0) -> int:
    if proc.stdin is not None:
        proc.stdin.close()
    return proc.wait(timeout=timeout)


def test_initialize_response_shape(tmp_path: Path) -> None:
    """initialize → protocolVersion echoed, capabilities.tools, serverInfo."""
    log = tmp_path / "calls.jsonl"
    proc = _spawn(log)
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05",
                       "capabilities": {}, "clientInfo": {"name": "test"}},
        })
        resp = _recv(proc)
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert "result" in resp
        result = resp["result"]
        assert result["protocolVersion"] == "2024-11-05"
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "nightjar-reply"
        assert "version" in result["serverInfo"]
    finally:
        _close_and_wait(proc)


def test_initialize_uses_default_version_when_client_omits(
    tmp_path: Path,
) -> None:
    """Client doesn't send protocolVersion → server picks the default."""
    log = tmp_path / "calls.jsonl"
    proc = _spawn(log)
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {},
        })
        resp = _recv(proc)
        assert isinstance(resp["result"]["protocolVersion"], str)
        assert resp["result"]["protocolVersion"]  # non-empty
    finally:
        _close_and_wait(proc)


def test_tools_list_returns_compose_reply(tmp_path: Path) -> None:
    """tools/list → exactly one tool, named compose_reply, with the
    expected schema shape (body required, subject optional)."""
    log = tmp_path / "calls.jsonl"
    proc = _spawn(log)
    try:
        # The handshake doesn't have to precede tools/list per the
        # MCP spec strictly, but the canonical client sequence does.
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}})
        _recv(proc)  # discard initialize response
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        # No response expected for the notification.
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = _recv(proc)
        assert resp["id"] == 2
        tools = resp["result"]["tools"]
        # Two tools: compose_reply and attach_to_reply.
        names = [t["name"] for t in tools]
        assert "compose_reply" in names
        assert "attach_to_reply" in names
        compose = next(t for t in tools if t["name"] == "compose_reply")
        schema = compose["inputSchema"]
        assert schema["type"] == "object"
        assert "body" in schema["properties"]
        assert "subject" in schema["properties"]
        assert schema["required"] == ["body"]
    finally:
        _close_and_wait(proc)


def test_tools_call_appends_to_log(tmp_path: Path) -> None:
    """A successful compose_reply call appends one JSONL line and
    returns ok."""
    log = tmp_path / "calls.jsonl"
    proc = _spawn(log)
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}})
        _recv(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "compose_reply",
                "arguments": {"body": "hello principal", "subject": "Re: x"},
            },
        })
        resp = _recv(proc)
        assert resp["id"] == 2
        assert resp["result"]["isError"] is False
        assert resp["result"]["content"][0]["text"] == "ok"
    finally:
        _close_and_wait(proc)
    # File written; one line; correct fields.
    assert log.exists()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["body"] == "hello principal"
    assert entry["subject"] == "Re: x"
    assert isinstance(entry["ts"], int)
    assert entry["call_id"] == 2


def test_tools_call_body_only_records_subject_as_null(
    tmp_path: Path,
) -> None:
    """Omitting subject → JSONL line has subject: null."""
    log = tmp_path / "calls.jsonl"
    proc = _spawn(log)
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}})
        _recv(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "compose_reply",
                "arguments": {"body": "just a body"},
            },
        })
        _recv(proc)
    finally:
        _close_and_wait(proc)
    entry = json.loads(log.read_text().splitlines()[0])
    assert entry["body"] == "just a body"
    assert entry["subject"] is None


def test_tools_call_multi_call_appends_each(tmp_path: Path) -> None:
    """Two calls produce two JSONL lines, in order."""
    log = tmp_path / "calls.jsonl"
    proc = _spawn(log)
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}})
        _recv(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "compose_reply",
                       "arguments": {"body": "draft one"}},
        })
        _recv(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "compose_reply",
                       "arguments": {"body": "draft two", "subject": "v2"}},
        })
        _recv(proc)
    finally:
        _close_and_wait(proc)
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["body"] == "draft one"
    assert first["subject"] is None
    assert second["body"] == "draft two"
    assert second["subject"] == "v2"


def test_tools_call_missing_body_returns_invalid_params(
    tmp_path: Path,
) -> None:
    """compose_reply with no body argument → JSON-RPC -32602."""
    log = tmp_path / "calls.jsonl"
    proc = _spawn(log)
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}})
        _recv(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "compose_reply", "arguments": {}},
        })
        resp = _recv(proc)
    finally:
        _close_and_wait(proc)
    assert "error" in resp
    assert resp["error"]["code"] == -32602
    # Nothing was appended.
    assert not log.exists() or log.read_text() == ""


def test_unknown_method_returns_method_not_found(tmp_path: Path) -> None:
    """Calling foo/bar → JSON-RPC -32601."""
    log = tmp_path / "calls.jsonl"
    proc = _spawn(log)
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "foo/bar",
                     "params": {}})
        resp = _recv(proc)
    finally:
        _close_and_wait(proc)
    assert resp["error"]["code"] == -32601


def test_notifications_get_no_response(tmp_path: Path) -> None:
    """A JSON-RPC notification (no id) gets no response. Verify by
    sending a notification followed by a request and checking only
    one response comes back."""
    log = tmp_path / "calls.jsonl"
    proc = _spawn(log)
    try:
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        # No response expected.
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {}})
        resp = _recv(proc)
        assert resp["id"] == 1
        # If the notification had produced a response, _recv above
        # would have returned that one and the next read would block
        # or return the actual initialize response. Verify by reading
        # one more time with a small timeout — should not return.
        # Cheaper: rely on the id check; the notification has no id,
        # so if we got id=1, the notification didn't produce output.
    finally:
        _close_and_wait(proc)


def test_eof_exits_cleanly(tmp_path: Path) -> None:
    """Closing stdin → server exits with code 0 promptly."""
    log = tmp_path / "calls.jsonl"
    proc = _spawn(log)
    rc = _close_and_wait(proc, timeout=5.0)
    assert rc == 0


def test_missing_compose_env_var_exits_nonzero(tmp_path: Path) -> None:
    """No NIGHTJAR_COMPOSE_REPLY_LOG → server exits nonzero with
    a clear stderr message."""
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={k: v for k, v in os.environ.items()
             if k not in ("NIGHTJAR_COMPOSE_REPLY_LOG",
                          "NIGHTJAR_ATTACHMENTS_LOG")},
        text=True,
    )
    proc.stdin.close()
    rc = proc.wait(timeout=5.0)
    assert rc != 0
    err = proc.stderr.read()
    assert "NIGHTJAR_COMPOSE_REPLY_LOG" in err


def test_missing_attachments_env_var_exits_nonzero(tmp_path: Path) -> None:
    """NIGHTJAR_COMPOSE_REPLY_LOG set but NIGHTJAR_ATTACHMENTS_LOG
    missing → server exits nonzero with a clear stderr message."""
    env = {
        k: v for k, v in os.environ.items()
        if k != "NIGHTJAR_ATTACHMENTS_LOG"
    }
    env["NIGHTJAR_COMPOSE_REPLY_LOG"] = str(tmp_path / "calls.jsonl")
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    proc.stdin.close()
    rc = proc.wait(timeout=5.0)
    assert rc != 0
    err = proc.stderr.read()
    assert "NIGHTJAR_ATTACHMENTS_LOG" in err


def test_malformed_json_returns_parse_error(tmp_path: Path) -> None:
    """Unparseable line → JSON-RPC -32700 with id=null."""
    log = tmp_path / "calls.jsonl"
    proc = _spawn(log)
    try:
        assert proc.stdin is not None
        proc.stdin.write("this is not json\n")
        proc.stdin.flush()
        resp = _recv(proc)
    finally:
        _close_and_wait(proc)
    assert resp["error"]["code"] == -32700
    assert resp["id"] is None


# ---- attach_to_reply ------------------------------------------------------


def _initialize(proc: subprocess.Popen[str]) -> None:
    _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {}})
    _recv(proc)


def test_attach_to_reply_appends_to_log(tmp_path: Path) -> None:
    """A successful attach_to_reply call appends one JSONL line to
    the attachments log and returns ok."""
    log = tmp_path / "calls.jsonl"
    attlog = tmp_path / "attachments.jsonl"
    target = tmp_path / "doc.txt"
    target.write_text("hello")
    proc = _spawn(log, attachments_log=attlog)
    try:
        _initialize(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "attach_to_reply",
                "arguments": {"path": str(target), "filename": "out.txt"},
            },
        })
        resp = _recv(proc)
        assert resp["result"]["isError"] is False
        assert resp["result"]["content"][0]["text"] == "ok"
    finally:
        _close_and_wait(proc)
    lines = attlog.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["path"] == str(target)
    assert entry["filename"] == "out.txt"
    assert entry["size_bytes"] == 5  # b"hello"
    assert entry["call_id"] == 2


def test_attach_to_reply_filename_defaults_to_basename(tmp_path: Path) -> None:
    """Omitting filename → JSONL entry has filename: null. Default
    to basename happens at notifier time, not at MCP call time."""
    log = tmp_path / "calls.jsonl"
    attlog = tmp_path / "attachments.jsonl"
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-1.4 fake")
    proc = _spawn(log, attachments_log=attlog)
    try:
        _initialize(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "attach_to_reply",
                "arguments": {"path": str(target)},
            },
        })
        _recv(proc)
    finally:
        _close_and_wait(proc)
    entry = json.loads(attlog.read_text().splitlines()[0])
    assert entry["filename"] is None


def test_attach_to_reply_rejects_relative_path(tmp_path: Path) -> None:
    """attach_to_reply with a relative path → -32602 invalid params."""
    log = tmp_path / "calls.jsonl"
    attlog = tmp_path / "attachments.jsonl"
    proc = _spawn(log, attachments_log=attlog)
    try:
        _initialize(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "attach_to_reply",
                "arguments": {"path": "relative/file.txt"},
            },
        })
        resp = _recv(proc)
    finally:
        _close_and_wait(proc)
    assert resp["error"]["code"] == -32602
    assert "absolute" in resp["error"]["message"]
    assert not attlog.exists() or attlog.read_text() == ""


def test_attach_to_reply_rejects_missing_file(tmp_path: Path) -> None:
    """Path doesn't exist → -32602."""
    log = tmp_path / "calls.jsonl"
    attlog = tmp_path / "attachments.jsonl"
    proc = _spawn(log, attachments_log=attlog)
    try:
        _initialize(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "attach_to_reply",
                "arguments": {"path": str(tmp_path / "does-not-exist")},
            },
        })
        resp = _recv(proc)
    finally:
        _close_and_wait(proc)
    assert resp["error"]["code"] == -32602
    assert "stat" in resp["error"]["message"]


def test_attach_to_reply_rejects_directory(tmp_path: Path) -> None:
    """Path points at a directory → -32602."""
    log = tmp_path / "calls.jsonl"
    attlog = tmp_path / "attachments.jsonl"
    target_dir = tmp_path / "subdir"
    target_dir.mkdir()
    proc = _spawn(log, attachments_log=attlog)
    try:
        _initialize(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "attach_to_reply",
                "arguments": {"path": str(target_dir)},
            },
        })
        resp = _recv(proc)
    finally:
        _close_and_wait(proc)
    assert resp["error"]["code"] == -32602
    assert "regular file" in resp["error"]["message"]


def test_attach_to_reply_rejects_oversize_file(tmp_path: Path) -> None:
    """Reject files > 18 MiB hard cap. Build a sparse file at 19 MiB."""
    log = tmp_path / "calls.jsonl"
    attlog = tmp_path / "attachments.jsonl"
    big = tmp_path / "huge.bin"
    # 19 MiB sparse file — fast to create, no actual disk consumption.
    with big.open("wb") as fh:
        fh.truncate(19 * 1024 * 1024)
    proc = _spawn(log, attachments_log=attlog)
    try:
        _initialize(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "attach_to_reply",
                "arguments": {"path": str(big)},
            },
        })
        resp = _recv(proc)
    finally:
        _close_and_wait(proc)
    assert resp["error"]["code"] == -32602
    assert "hard cap" in resp["error"]["message"]


def test_attach_to_reply_soft_warns_on_large_file(tmp_path: Path) -> None:
    """Files between 10 MiB and 18 MiB succeed but the ok message
    includes a warning string the agent can react to."""
    log = tmp_path / "calls.jsonl"
    attlog = tmp_path / "attachments.jsonl"
    medium = tmp_path / "medium.bin"
    with medium.open("wb") as fh:
        fh.truncate(11 * 1024 * 1024)
    proc = _spawn(log, attachments_log=attlog)
    try:
        _initialize(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "attach_to_reply",
                "arguments": {"path": str(medium)},
            },
        })
        resp = _recv(proc)
    finally:
        _close_and_wait(proc)
    assert resp["result"]["isError"] is False
    text = resp["result"]["content"][0]["text"]
    assert text.startswith("ok")
    assert "warning" in text.lower()


def test_attach_to_reply_multi_call_appends_each(tmp_path: Path) -> None:
    """Three attach calls produce three JSONL lines in order."""
    log = tmp_path / "calls.jsonl"
    attlog = tmp_path / "attachments.jsonl"
    paths = []
    for name in ("a.txt", "b.txt", "c.txt"):
        p = tmp_path / name
        p.write_text(name)
        paths.append(p)
    proc = _spawn(log, attachments_log=attlog)
    try:
        _initialize(proc)
        for i, p in enumerate(paths, start=2):
            _send(proc, {
                "jsonrpc": "2.0", "id": i, "method": "tools/call",
                "params": {
                    "name": "attach_to_reply",
                    "arguments": {"path": str(p)},
                },
            })
            _recv(proc)
    finally:
        _close_and_wait(proc)
    lines = attlog.read_text().splitlines()
    assert len(lines) == 3
    for line, expected in zip(lines, paths):
        entry = json.loads(line)
        assert entry["path"] == str(expected)


# ---- Boot-time smoke probe ------------------------------------------------


def test_probe_passes_against_real_server(tmp_path: Path) -> None:
    """probe_mcp_server() should succeed against the actual MCP
    server script — this is the boot-time guarantee."""
    from daemon.compose_reply_smoke import probe_mcp_server
    probe_mcp_server()  # should not raise


def test_probe_fails_when_script_missing(tmp_path: Path) -> None:
    """Pointing the probe at a non-existent script raises
    ComposeReplyProbeError."""
    from daemon.compose_reply_smoke import (
        ComposeReplyProbeError,
        probe_mcp_server,
    )
    with pytest.raises(ComposeReplyProbeError, match="not found"):
        probe_mcp_server(script_path=tmp_path / "does-not-exist.py")


def test_probe_fails_when_script_broken(tmp_path: Path) -> None:
    """A broken MCP script (raises on startup) trips the probe."""
    broken = tmp_path / "broken.py"
    broken.write_text(
        "import sys; sys.stderr.write('boom\\n'); raise SystemExit(7)\n",
    )
    from daemon.compose_reply_smoke import (
        ComposeReplyProbeError,
        probe_mcp_server,
    )
    with pytest.raises(ComposeReplyProbeError):
        probe_mcp_server(script_path=broken, timeout_seconds=2.0)


def test_probe_fails_when_script_misses_tool(tmp_path: Path) -> None:
    """A script that handshakes but doesn't expose compose_reply
    fails the probe with a clear message."""
    fake = tmp_path / "no_tool.py"
    fake.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('method') == 'initialize':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],"
        "'result':{'protocolVersion':'2024-11-05','capabilities':{},"
        "'serverInfo':{'name':'fake','version':'0'}}}), flush=True)\n"
        "    elif msg.get('method') == 'tools/list':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],"
        "'result':{'tools':[]}}), flush=True)\n"
    )
    from daemon.compose_reply_smoke import (
        ComposeReplyProbeError,
        probe_mcp_server,
    )
    with pytest.raises(ComposeReplyProbeError, match="not registered"):
        probe_mcp_server(script_path=fake, timeout_seconds=2.0)
