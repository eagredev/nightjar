"""Boot-time smoke test for the compose_reply MCP server.

Called from the daemon's startup path before it enters its main
IDLE loop. Spawns the MCP server with a temp log path, runs the
canonical client handshake (initialize → notifications/initialized
→ tools/list), asserts the compose_reply tool is registered, then
closes stdin and verifies a clean exit.

Failures raise ComposeReplyProbeError, which the boot path should
treat as a fatal "do not start the daemon" condition. This catches
deployment regressions (broken script, missing imports, environment
issues) up-front rather than per-session.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# The MCP server lives in this same package directory.
_MCP_SCRIPT = Path(__file__).resolve().parent / "compose_reply_mcp.py"

_PROBE_TIMEOUT_SECONDS = 5.0


class ComposeReplyProbeError(RuntimeError):
    """Smoke test failed. Daemon should refuse to start."""


def probe_mcp_server(
    *,
    script_path: Path | None = None,
    timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
) -> None:
    """Boot-time smoke check. Raises ComposeReplyProbeError on any
    failure. Returns None on success.

    `script_path` is for testing — production callers leave it unset
    and the helper finds the script next to itself.
    """
    script = script_path or _MCP_SCRIPT
    if not script.exists():
        raise ComposeReplyProbeError(
            f"compose_reply MCP script not found at {script}",
        )

    with tempfile.TemporaryDirectory(prefix="nightjar-probe-") as td:
        log_path = Path(td) / "probe.jsonl"
        attachments_log_path = Path(td) / "probe.attachments.jsonl"
        env = {
            **os.environ,
            "NIGHTJAR_COMPOSE_REPLY_LOG": str(log_path),
            "NIGHTJAR_ATTACHMENTS_LOG": str(attachments_log_path),
        }
        try:
            proc = subprocess.Popen(
                [sys.executable, str(script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise ComposeReplyProbeError(
                f"could not spawn compose_reply MCP server: {exc}",
            ) from exc

        try:
            _drive_handshake(proc, timeout_seconds=timeout_seconds)
        except ComposeReplyProbeError:
            # Try to capture stderr for the operator's benefit.
            _try_terminate(proc)
            raise
        except Exception as exc:  # noqa: BLE001
            _try_terminate(proc)
            raise ComposeReplyProbeError(
                f"compose_reply MCP probe failed: {exc}",
            ) from exc

        # Clean shutdown: close stdin, wait briefly. If the server
        # doesn't exit promptly, that's a defect worth flagging.
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            rc = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _try_terminate(proc)
            raise ComposeReplyProbeError(
                "compose_reply MCP server did not exit on stdin EOF",
            ) from exc
        if rc != 0:
            err = proc.stderr.read() if proc.stderr else ""
            raise ComposeReplyProbeError(
                f"compose_reply MCP server exited rc={rc}; stderr={err!r}",
            )


def _drive_handshake(
    proc: subprocess.Popen[str],
    *,
    timeout_seconds: float,
) -> None:
    """Run initialize → notifications/initialized → tools/list and
    assert compose_reply is registered."""
    assert proc.stdin is not None and proc.stdout is not None

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv() -> dict:
        line = proc.stdout.readline()
        if not line:
            err = proc.stderr.read() if proc.stderr else ""
            raise ComposeReplyProbeError(
                f"server closed stdout during handshake; stderr={err!r}",
            )
        return json.loads(line)

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05",
                     "capabilities": {},
                     "clientInfo": {"name": "nightjar-probe"}}})
    init_resp = recv()
    if "result" not in init_resp:
        raise ComposeReplyProbeError(
            f"initialize did not return a result: {init_resp}",
        )

    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    # No response expected.

    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    list_resp = recv()
    tools = (list_resp.get("result") or {}).get("tools") or []
    names = [t.get("name") for t in tools]
    if "compose_reply" not in names:
        raise ComposeReplyProbeError(
            f"compose_reply tool not registered; got tools: {names}",
        )


def _try_terminate(proc: subprocess.Popen[str]) -> None:
    """Best-effort cleanup; never raises."""
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=1.0)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    # Allow `python -m daemon.compose_reply_smoke` as a standalone
    # diagnostic for operators.
    try:
        probe_mcp_server()
    except ComposeReplyProbeError as exc:
        sys.stderr.write(f"compose_reply MCP probe FAILED: {exc}\n")
        raise SystemExit(1)
    print("compose_reply MCP probe ok")
