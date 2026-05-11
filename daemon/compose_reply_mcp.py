"""Stdio JSON-RPC 2.0 MCP server exposing two tools: compose_reply
and attach_to_reply.

Spawned as a child of `claude -p` via `--mcp-config`. The agent calls:

  - `compose_reply(body, subject?)` exactly once at the end of its
    turn; that argument is the email reply Nightjar sends to the
    principal. Last-call-wins semantic.
  - `attach_to_reply(path, filename?)` zero-or-more times during the
    turn; each call adds one file to the outbound reply.

Each tools/call appends one JSONL line to its respective log:
  - compose_reply  → NIGHTJAR_COMPOSE_REPLY_LOG
  - attach_to_reply → NIGHTJAR_ATTACHMENTS_LOG

The daemon reads both files when claude exits and threads the
results into AgentResult.

Why a separate process and not parsing stream-json: the MCP files
are the source of truth for what the model actually invoked,
independent of claude's stream-json schema (which has shifted
across versions). The audit log captures the tool_use events
naturally as a forensic mirror; the daemon does not depend on it
for routing.

Stdlib only. Python 3.13. No third-party MCP SDK — the wire format
is JSON-RPC 2.0, the tool surface is two methods, the protocol set
is small enough to implement directly.
"""
from __future__ import annotations

import json
import os
import sys
import time


PROTOCOL_VERSION_DEFAULT = "2024-11-05"
SERVER_NAME = "nightjar-reply"
SERVER_VERSION = "0.2.0"

# JSON-RPC 2.0 error codes (subset from the spec).
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603


# Attachment size policy. Gmail's wire limit is 25 MiB, base64 expands
# bytes by ~4/3, so the practical raw cap is ~18 MiB. We hard-reject
# above 18 MiB per file, soft-warn (in the tool result) above 10 MiB
# to give the agent a chance to gzip/split. Total cap across all
# attachments enforced at the same 18 MiB to keep the wire-budget
# simple.
ATTACHMENT_HARD_CAP_BYTES = 18 * 1024 * 1024
ATTACHMENT_SOFT_WARN_BYTES = 10 * 1024 * 1024


COMPOSE_REPLY_TOOL_SCHEMA = {
    "name": "compose_reply",
    "description": (
        "Compose the email reply Nightjar will send to the principal. "
        "Call exactly once at the end of your turn. The body argument "
        "becomes the reply body verbatim. Optional subject overrides "
        "the default 'Nightjar agent: response' subject. Calling more "
        "than once is allowed; only the last call wins (earlier calls "
        "remain visible in the audit log as drafts)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "body": {
                "type": "string",
                "description": (
                    "The reply body. Plain text; markdown allowed but "
                    "not rendered."
                ),
            },
            "subject": {
                "type": "string",
                "description": (
                    "Optional subject override. Omit to use the daemon "
                    "default ('Nightjar agent: response')."
                ),
            },
        },
        "required": ["body"],
    },
}


ATTACH_TO_REPLY_TOOL_SCHEMA = {
    "name": "attach_to_reply",
    "description": (
        "Attach a file to the outbound email reply Nightjar will send. "
        "Call once per file you want to attach; each call adds one "
        "attachment. Path must be absolute, must exist, must be a "
        "regular file. 18 MiB per-file cap (Gmail's 25 MiB wire limit "
        "after base64 expansion); soft warn at 10 MiB. Use this BEFORE "
        "or ALONGSIDE compose_reply; the daemon collects all attachments "
        "called during the turn and threads them into the SMTP send."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Absolute path to the file to attach. Must exist "
                    "and be readable by the daemon process at SMTP "
                    "send time."
                ),
            },
            "filename": {
                "type": "string",
                "description": (
                    "Optional filename for the recipient (the name "
                    "shown in their mail client). Defaults to the "
                    "basename of `path` when omitted."
                ),
            },
        },
        "required": ["path"],
    },
}


ALL_TOOL_SCHEMAS = [COMPOSE_REPLY_TOOL_SCHEMA, ATTACH_TO_REPLY_TOOL_SCHEMA]


def _send(obj: dict) -> None:
    """Write one JSON-RPC frame to stdout and flush."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _send({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    })


def _append_jsonl(log_path: str, entry: dict) -> None:
    """Append one JSONL line to a log file.

    Open/write/close per call so a SIGKILL between calls leaves at
    most one truncated line. The daemon's reader tolerates that.
    Used for both compose-reply and attachments logs.
    """
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    fd = os.open(
        log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600,
    )
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _handle_initialize(req_id, params: dict) -> None:
    """Respond with the MCP handshake.

    Echo the client's protocolVersion if present and a string; this
    is the documented compatibility pattern. Otherwise default to a
    known good version.
    """
    client_pv = params.get("protocolVersion")
    pv = client_pv if isinstance(client_pv, str) else PROTOCOL_VERSION_DEFAULT
    _result(req_id, {
        "protocolVersion": pv,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    })


def _handle_tools_list(req_id) -> None:
    _result(req_id, {"tools": ALL_TOOL_SCHEMAS})


def _handle_compose_reply(req_id, args: dict, compose_log: str) -> None:
    body = args.get("body")
    subject = args.get("subject")
    if not isinstance(body, str):
        _error(req_id, ERR_INVALID_PARAMS,
               "compose_reply requires a string `body` argument")
        return
    if subject is not None and not isinstance(subject, str):
        _error(req_id, ERR_INVALID_PARAMS,
               "compose_reply `subject` must be a string if provided")
        return
    try:
        _append_jsonl(compose_log, {
            "ts": int(time.time()),
            "body": body,
            "subject": subject,
            "call_id": req_id,
        })
    except OSError as exc:
        _error(req_id, ERR_INTERNAL,
               f"could not append to compose-reply log: {exc}")
        return
    _result(req_id, {
        "content": [{"type": "text", "text": "ok"}],
        "isError": False,
    })


def _handle_attach_to_reply(req_id, args: dict, attachments_log: str) -> None:
    path = args.get("path")
    filename = args.get("filename")
    if not isinstance(path, str):
        _error(req_id, ERR_INVALID_PARAMS,
               "attach_to_reply requires a string `path` argument")
        return
    if filename is not None and not isinstance(filename, str):
        _error(req_id, ERR_INVALID_PARAMS,
               "attach_to_reply `filename` must be a string if provided")
        return
    # Resolve absoluteness/existence/readability/size *here*, at tool
    # call time, so the agent can react (drop, gzip, split) rather
    # than discovering the failure at SMTP send time. This mirrors
    # proposal 03's "fail at the tool boundary" rule.
    if not path.startswith("/"):
        _error(req_id, ERR_INVALID_PARAMS,
               f"attach_to_reply `path` must be absolute, got {path!r}")
        return
    try:
        st = os.stat(path)
    except OSError as exc:
        _error(req_id, ERR_INVALID_PARAMS,
               f"attach_to_reply: cannot stat {path!r}: {exc}")
        return
    import stat as stat_mod
    if not stat_mod.S_ISREG(st.st_mode):
        _error(req_id, ERR_INVALID_PARAMS,
               f"attach_to_reply: {path!r} is not a regular file")
        return
    if not os.access(path, os.R_OK):
        _error(req_id, ERR_INVALID_PARAMS,
               f"attach_to_reply: {path!r} is not readable by the "
               "daemon process")
        return
    if st.st_size > ATTACHMENT_HARD_CAP_BYTES:
        _error(req_id, ERR_INVALID_PARAMS,
               f"attach_to_reply: {path!r} is "
               f"{st.st_size / 1024 / 1024:.1f} MiB; hard cap is "
               f"{ATTACHMENT_HARD_CAP_BYTES // 1024 // 1024} MiB "
               f"(Gmail wire limit after base64 expansion). Gzip or "
               f"split before attaching.")
        return
    try:
        _append_jsonl(attachments_log, {
            "ts": int(time.time()),
            "path": path,
            "filename": filename,
            "size_bytes": st.st_size,
            "call_id": req_id,
        })
    except OSError as exc:
        _error(req_id, ERR_INTERNAL,
               f"could not append to attachments log: {exc}")
        return
    # Ok response. Include a soft-warn message when the file is large
    # enough that the agent should consider gzip/split before stacking
    # more attachments.
    text = "ok"
    if st.st_size > ATTACHMENT_SOFT_WARN_BYTES:
        text = (
            f"ok (warning: {st.st_size / 1024 / 1024:.1f} MiB; "
            f"total reply payload nears Gmail's 25 MiB wire cap — "
            f"avoid stacking many more)"
        )
    _result(req_id, {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    })


def _handle_tools_call(req_id, params: dict, compose_log: str,
                       attachments_log: str) -> None:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "compose_reply":
        _handle_compose_reply(req_id, args, compose_log)
    elif name == "attach_to_reply":
        _handle_attach_to_reply(req_id, args, attachments_log)
    else:
        _error(req_id, ERR_METHOD_NOT_FOUND, f"unknown tool: {name!r}")


def _dispatch(message: dict, compose_log: str, attachments_log: str) -> None:
    """Route one parsed JSON-RPC message to the right handler.

    Notifications (no `id`) are silently dispatched and produce no
    response per JSON-RPC 2.0.
    """
    method = message.get("method")
    req_id = message.get("id")  # may be None for notifications
    params = message.get("params") or {}
    is_notification = req_id is None

    if method == "initialize":
        if is_notification:
            return  # malformed but don't crash
        _handle_initialize(req_id, params)
    elif method == "notifications/initialized":
        return  # one-way, no response
    elif method == "tools/list":
        if is_notification:
            return
        _handle_tools_list(req_id)
    elif method == "tools/call":
        if is_notification:
            return
        _handle_tools_call(req_id, params, compose_log, attachments_log)
    else:
        if is_notification:
            return  # unknown notifications are ignored per spec
        _error(req_id, ERR_METHOD_NOT_FOUND,
               f"method not found: {method!r}")


def main() -> int:
    compose_log = os.environ.get("NIGHTJAR_COMPOSE_REPLY_LOG")
    if not compose_log:
        sys.stderr.write(
            "compose_reply_mcp: NIGHTJAR_COMPOSE_REPLY_LOG must be set\n",
        )
        return 2
    attachments_log = os.environ.get("NIGHTJAR_ATTACHMENTS_LOG")
    if not attachments_log:
        sys.stderr.write(
            "compose_reply_mcp: NIGHTJAR_ATTACHMENTS_LOG must be set\n",
        )
        return 2

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            # Malformed JSON. Per JSON-RPC 2.0, parse errors get a
            # response with id=null.
            _error(None, ERR_PARSE, "parse error")
            continue
        if not isinstance(message, dict):
            _error(None, ERR_INVALID_REQUEST, "request must be an object")
            continue
        try:
            _dispatch(message, compose_log, attachments_log)
        except Exception as exc:  # noqa: BLE001 — last-resort guard
            req_id = message.get("id")
            _error(req_id, ERR_INTERNAL, f"internal error: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
