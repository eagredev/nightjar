"""Tests for daemon/cc_executor.py.

The executor's external dependency is the `claude` CLI subprocess. We
patch subprocess.run via monkeypatch and feed the executor synthetic
event-stream JSON the way the real CLI would emit. The shape of those
events was captured empirically against the live CLI.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

import pytest

from daemon import cc_executor
from daemon.cc_executor import (
    ClaudeCodePipeClient,
    ClaudeCodePipeError,
    build_claude_client,
)


# ---- Helpers ---------------------------------------------------------------


def _ok_events(
    *,
    tool_input: dict[str, Any] | None,
    in_tokens: int = 5,
    out_tokens: int = 80,
    cache_creation: int = 6500,
    text_blocks: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Build a minimal but realistic claude -p event stream."""
    assistant_content: list[dict[str, Any]] = []
    for tb in text_blocks:
        assistant_content.append({"type": "text", "text": tb})
    if tool_input is not None:
        assistant_content.append({
            "type": "tool_use",
            "name": "StructuredOutput",
            "id": "toolu_test_1",
            "input": tool_input,
        })
    return [
        {"type": "system", "subtype": "init", "tools": ["StructuredOutput"]},
        {"type": "assistant", "message": {"content": assistant_content}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
            "stop_reason": "end_turn",
            "duration_ms": 5000,
            "num_turns": 2,
            "total_cost_usd": 0.01,
            "usage": {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    ]


class _FakeProc:
    def __init__(
        self,
        *,
        stdout: str,
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str,
    returncode: int = 0,
    stderr: str = "",
    raise_exc: BaseException | None = None,
    capture: dict[str, Any] | None = None,
) -> None:
    """Replace subprocess.run with a stub that records the call and
    returns a canned proc object."""
    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        if capture is not None:
            capture["cmd"] = cmd
            capture["kwargs"] = kwargs
        if raise_exc is not None:
            raise raise_exc
        return _FakeProc(stdout=stdout, stderr=stderr, returncode=returncode)
    monkeypatch.setattr(cc_executor.subprocess, "run", _fake_run)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# A representative tool — same shape as classify_two_axis.
_TOOL = {
    "name": "classify_two_axis",
    "description": "test",
    "input_schema": {
        "type": "object",
        "properties": {
            "facets": {"type": "array", "items": {"type": "string"}},
            "project": {"type": "string"},
        },
        "required": ["facets", "project"],
        "additionalProperties": False,
    },
}


# ---- Happy path ------------------------------------------------------------


def test_call_returns_tool_use_with_remapped_name(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _ok_events(tool_input={"facets": ["calendar"], "project": "aurora.music"})
    capture: dict[str, Any] = {}
    _patch_subprocess(monkeypatch, stdout=json.dumps(events), capture=capture)

    client = ClaudeCodePipeClient()
    response = _run(client.call(
        model="claude-haiku-4-5",
        system="you are a classifier.",
        user="classify this",
        tools=[_TOOL],
        max_tokens=256,
    ))

    assert len(response.tool_uses) == 1
    assert response.tool_uses[0]["name"] == "classify_two_axis"
    assert response.tool_uses[0]["input"] == {
        "facets": ["calendar"],
        "project": "aurora.music",
    }
    assert response.input_tokens == 5
    assert response.output_tokens == 80
    assert response.stop_reason == "end_turn"


def test_call_passes_schema_via_json_schema_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _ok_events(tool_input={"facets": [], "project": "out_of_scope"})
    capture: dict[str, Any] = {}
    _patch_subprocess(monkeypatch, stdout=json.dumps(events), capture=capture)

    client = ClaudeCodePipeClient()
    _run(client.call(
        model="claude-haiku-4-5", system="sys", user="usr",
        tools=[_TOOL], max_tokens=128,
    ))

    cmd = capture["cmd"]
    assert "claude" in cmd[0] or cmd[0] == "claude"
    assert "-p" in cmd
    assert "--json-schema" in cmd
    schema_idx = cmd.index("--json-schema")
    schema = json.loads(cmd[schema_idx + 1])
    assert schema == _TOOL["input_schema"]
    # Model passed through, not silently swapped.
    assert "--model" in cmd
    model_idx = cmd.index("--model")
    assert cmd[model_idx + 1] == "claude-haiku-4-5"


def test_call_passes_user_via_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _ok_events(tool_input={"facets": [], "project": "out_of_scope"})
    capture: dict[str, Any] = {}
    _patch_subprocess(monkeypatch, stdout=json.dumps(events), capture=capture)

    client = ClaudeCodePipeClient()
    _run(client.call(
        model="m", system="sys", user="this is the user message",
        tools=[_TOOL], max_tokens=1,
    ))

    assert capture["kwargs"]["input"] == "this is the user message"


def test_call_captures_text_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI sometimes emits text turns alongside the structured turn.
    Those blocks are surfaced on ClaudeResponse.text_blocks for any future
    call site that wants them — even though current call sites don't."""
    events = _ok_events(
        tool_input={"facets": [], "project": "out_of_scope"},
        text_blocks=("preliminary thoughts", "more thoughts"),
    )
    _patch_subprocess(monkeypatch, stdout=json.dumps(events))

    client = ClaudeCodePipeClient()
    response = _run(client.call(
        model="m", system="s", user="u", tools=[_TOOL], max_tokens=1,
    ))
    assert response.text_blocks == ("preliminary thoughts", "more thoughts")


# ---- Missing structured output --------------------------------------------


def test_call_returns_empty_tool_uses_when_no_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the CLI emits no StructuredOutput block (e.g. the model
    refused), the executor returns a ClaudeResponse with empty
    tool_uses — same shape the SDK would produce. Caller's existing
    'no_tool_call' branch handles it."""
    events = _ok_events(tool_input=None, text_blocks=("I cannot help with that.",))
    _patch_subprocess(monkeypatch, stdout=json.dumps(events))

    client = ClaudeCodePipeClient()
    response = _run(client.call(
        model="m", system="s", user="u", tools=[_TOOL], max_tokens=1,
    ))
    assert response.tool_uses == ()
    assert response.text_blocks == ("I cannot help with that.",)
    assert response.stop_reason == "end_turn"


# ---- Failure modes --------------------------------------------------------


def test_call_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(
        monkeypatch, stdout="", stderr="auth required", returncode=1,
    )
    client = ClaudeCodePipeClient()
    with pytest.raises(ClaudeCodePipeError, match="exited with code 1"):
        _run(client.call(
            model="m", system="s", user="u", tools=[_TOOL], max_tokens=1,
        ))


def test_call_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(
        monkeypatch, stdout="",
        raise_exc=subprocess.TimeoutExpired(cmd=["claude"], timeout=1),
    )
    client = ClaudeCodePipeClient()
    with pytest.raises(ClaudeCodePipeError, match="timed out"):
        _run(client.call(
            model="m", system="s", user="u", tools=[_TOOL], max_tokens=1,
        ))


def test_call_raises_on_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(
        monkeypatch, stdout="",
        raise_exc=FileNotFoundError("claude not found"),
    )
    client = ClaudeCodePipeClient(executable="/nonexistent/claude")
    with pytest.raises(ClaudeCodePipeError, match="claude executable not found"):
        _run(client.call(
            model="m", system="s", user="u", tools=[_TOOL], max_tokens=1,
        ))


def test_call_raises_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, stdout="not valid json {")
    client = ClaudeCodePipeClient()
    with pytest.raises(ClaudeCodePipeError, match="could not parse"):
        _run(client.call(
            model="m", system="s", user="u", tools=[_TOOL], max_tokens=1,
        ))


def test_call_raises_on_result_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some CLI failures (rate limits, auth issues) come back with
    is_error=true on the result event rather than a non-zero exit code.
    The executor must surface those as a clean error, not a silent
    empty response."""
    events = _ok_events(tool_input=None)
    # Mark the result event as error.
    for ev in events:
        if ev.get("type") == "result":
            ev["is_error"] = True
            ev["result"] = "Rate limit exceeded"
    _patch_subprocess(monkeypatch, stdout=json.dumps(events))
    client = ClaudeCodePipeClient()
    with pytest.raises(ClaudeCodePipeError, match="Rate limit"):
        _run(client.call(
            model="m", system="s", user="u", tools=[_TOOL], max_tokens=1,
        ))


# ---- Input validation -----------------------------------------------------


def test_call_rejects_zero_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ClaudeCodePipeClient()
    with pytest.raises(ClaudeCodePipeError, match="exactly one tool"):
        _run(client.call(
            model="m", system="s", user="u", tools=[], max_tokens=1,
        ))


def test_call_rejects_multiple_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ClaudeCodePipeClient()
    with pytest.raises(ClaudeCodePipeError, match="exactly one tool"):
        _run(client.call(
            model="m", system="s", user="u",
            tools=[_TOOL, _TOOL], max_tokens=1,
        ))


def test_call_rejects_tool_without_input_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ClaudeCodePipeClient()
    bad_tool = {"name": "x"}  # no input_schema
    with pytest.raises(ClaudeCodePipeError, match="input_schema"):
        _run(client.call(
            model="m", system="s", user="u",
            tools=[bad_tool], max_tokens=1,
        ))


# ---- Factory --------------------------------------------------------------


def test_build_claude_client_picks_anthropic_api() -> None:
    # Don't actually instantiate the SDK — just verify the factory
    # routes correctly. AnthropicClient.__init__ imports anthropic
    # lazily, so this works as long as the package is installed.
    client = build_claude_client(
        backend="anthropic_api",
        api_key="sk-ant-test-key-much-longer-than-fifty-characters-for-validation-yes",
    )
    # Type-by-name to avoid the import dance in the test.
    assert type(client).__name__ == "AnthropicClient"


def test_build_claude_client_picks_pipe_client() -> None:
    client = build_claude_client(backend="claude_code_pipe", api_key=None)
    assert isinstance(client, ClaudeCodePipeClient)


def test_build_claude_client_pipe_ignores_api_key() -> None:
    """Even if an api_key is set, the pipe backend doesn't use it."""
    client = build_claude_client(backend="claude_code_pipe", api_key="sk-ant-foo")
    assert isinstance(client, ClaudeCodePipeClient)


def test_build_claude_client_anthropic_requires_api_key() -> None:
    with pytest.raises(ValueError, match="requires"):
        build_claude_client(backend="anthropic_api", api_key=None)
    with pytest.raises(ValueError, match="requires"):
        build_claude_client(backend="anthropic_api", api_key="")


def test_build_claude_client_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown"):
        build_claude_client(backend="ollama", api_key=None)
