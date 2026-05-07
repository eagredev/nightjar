"""ClaudeClient backend that drives `claude -p` (Claude Code CLI).

This is the local-first / subscription-bounded execution path. It
implements the same `ClaudeClient` protocol the SDK-backed
`AnthropicClient` does, so call sites (triage, classifier,
principal-interpret) don't know which backend they're talking to.

The bridge works via Claude Code's `--json-schema` flag. The CLI
injects a synthetic tool named `StructuredOutput` whose input matches
the schema; the model emits `tool_use` blocks with that name; this
module remaps the name back to whatever the caller's tool was actually
called (e.g. `classify_two_axis`) before constructing a `ClaudeResponse`.

The round-trip has been verified empirically against the existing
validators across the simplest schema in the codebase and the
densest, kind-discriminated union schema.

Trade-offs vs the SDK path:
- Two model turns per call (markdown emit then synthetic reprompt).
- Cannot pass max_tokens (CLI doesn't accept it). Hint dropped.
- Auth via the principal's logged-in subscription, no API key needed.
- Cost is bounded by subscription rate limit, not per-token spend.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from typing import Any

from .triage import ClaudeClient, ClaudeResponse


# Default deadline. CLI calls take ~5–10s round-trip on Haiku for the
# schemas in this codebase; 120s gives plenty of headroom for slower
# models or temporarily slow rate-limit checks without letting a stuck
# process hang the daemon indefinitely.
_DEFAULT_TIMEOUT_SECONDS = 120


class ClaudeCodePipeError(Exception):
    """Raised when the CLI invocation itself fails (non-zero exit,
    timeout, malformed JSON output). Triage and classifier code already
    catch broad `Exception` and convert to a `*_error` reason, so this
    flows through the same path as anthropic SDK errors do today."""


@dataclass(frozen=True)
class _ParsedOutput:
    tool_input: dict[str, Any] | None
    text_blocks: tuple[str, ...]
    stop_reason: str
    input_tokens: int
    output_tokens: int


def _parse_event_stream(stdout: str) -> _ParsedOutput:
    """Walk the `claude -p --output-format json` event array and pull
    out the StructuredOutput tool input plus token usage. The CLI
    sometimes emits text-only turns before the structured turn; we
    capture text blocks too in case a future call site wants them."""
    try:
        events = json.loads(stdout) if stdout else []
    except json.JSONDecodeError as e:
        raise ClaudeCodePipeError(
            f"could not parse claude -p output as JSON: {e}; "
            f"first 200 chars: {stdout[:200]!r}"
        ) from e
    if not isinstance(events, list):
        raise ClaudeCodePipeError(
            f"claude -p output was not a JSON array; got {type(events).__name__}"
        )

    tool_input: dict[str, Any] | None = None
    text_blocks: list[str] = []
    stop_reason = ""
    input_tokens = 0
    output_tokens = 0

    for ev in events:
        ev_type = ev.get("type")
        if ev_type == "assistant":
            msg = ev.get("message", {}) or {}
            for block in msg.get("content", []) or []:
                bt = block.get("type")
                if bt == "tool_use" and block.get("name") == "StructuredOutput":
                    inp = block.get("input")
                    if isinstance(inp, dict):
                        # Last write wins. The CLI generally emits a
                        # single StructuredOutput block, but in case of
                        # repeats we want the model's final answer.
                        tool_input = inp
                elif bt == "text":
                    text_value = block.get("text")
                    if isinstance(text_value, str) and text_value:
                        text_blocks.append(text_value)
        elif ev_type == "result":
            stop_reason = str(ev.get("stop_reason") or "")
            usage = ev.get("usage", {}) or {}
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            if ev.get("is_error"):
                # Surface authentication / rate-limit / permission failures
                # as a structured exception rather than letting them masquerade
                # as a successful response with empty tool_input.
                err_msg = ev.get("result") or ev.get("api_error_status") or "unknown error"
                raise ClaudeCodePipeError(
                    f"claude -p reported is_error=true: {err_msg}"
                )

    return _ParsedOutput(
        tool_input=tool_input,
        text_blocks=tuple(text_blocks),
        stop_reason=stop_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class ClaudeCodePipeClient:
    """Production ClaudeClient backed by `claude -p`.

    No api key. Authentication relies on the principal having logged in
    via `claude auth` once; the keychain entry is read by the CLI on
    each invocation.
    """

    def __init__(
        self,
        *,
        executable: str = "claude",
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._executable = executable
        self._timeout_seconds = timeout_seconds

    async def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int,  # ignored — CLI doesn't accept max_tokens
    ) -> ClaudeResponse:
        if len(tools) != 1:
            raise ClaudeCodePipeError(
                f"ClaudeCodePipeClient supports exactly one tool per call; "
                f"got {len(tools)}. Multi-tool agentic loops aren't wired in."
            )
        tool = tools[0]
        tool_name = tool.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            raise ClaudeCodePipeError("tool.name missing or not a string")
        input_schema = tool.get("input_schema")
        if not isinstance(input_schema, dict):
            raise ClaudeCodePipeError("tool.input_schema missing or not a dict")

        cmd = [
            self._executable, "-p",
            "--system-prompt", system,
            "--json-schema", json.dumps(input_schema),
            "--output-format", "json",
            "--model", model,
        ]

        # Run subprocess off the event loop so we don't block the
        # daemon. asyncio.to_thread is appropriate for blocking
        # subprocess calls — asyncio.create_subprocess_exec would also
        # work but adds complexity around stdout draining for the
        # ~tens-of-KB outputs the CLI produces.
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                input=user,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise ClaudeCodePipeError(
                f"claude -p timed out after {self._timeout_seconds}s"
            ) from e
        except FileNotFoundError as e:
            raise ClaudeCodePipeError(
                f"claude executable not found at {self._executable!r}: {e}"
            ) from e

        if proc.returncode != 0:
            raise ClaudeCodePipeError(
                f"claude -p exited with code {proc.returncode}; "
                f"stderr: {proc.stderr[:400]!r}"
            )

        parsed = _parse_event_stream(proc.stdout)

        tool_uses: tuple[dict[str, Any], ...] = ()
        if parsed.tool_input is not None:
            # Remap the synthetic StructuredOutput name back to what
            # the caller's downstream code expects to see, so the
            # `tool_use.get("name") != "<expected>"` guards in
            # scope_classifier and principal_interpret keep working.
            tool_uses = ({"name": tool_name, "input": parsed.tool_input},)

        return ClaudeResponse(
            tool_uses=tool_uses,
            text_blocks=parsed.text_blocks,
            stop_reason=parsed.stop_reason,
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
        )


def build_claude_client(
    *,
    backend: str,
    api_key: str | None,
) -> ClaudeClient:
    """Pick a backend based on config. Raises ValueError on unknown
    backend or missing-when-required api key."""
    if backend == "anthropic_api":
        if not api_key:
            raise ValueError(
                "[claude].backend = anthropic_api requires [claude].api_key"
            )
        from .triage import AnthropicClient
        return AnthropicClient(api_key=api_key)
    if backend == "claude_code_pipe":
        return ClaudeCodePipeClient()
    raise ValueError(
        f"unknown [claude].backend {backend!r}; "
        f"expected 'anthropic_api' or 'claude_code_pipe'"
    )


def build_claude_client_for(site: str, config: "ClaudeConfig") -> ClaudeClient:
    """Construct the ClaudeClient for one named call site. Routes per
    `[llm.<site>].backend` if set, else `[claude].backend`. Raises
    ValueError on unknown site name; the config loader is supposed to
    catch unknown sites first, but this is the load-time backstop."""
    from .config import KNOWN_LLM_SITES
    if site not in KNOWN_LLM_SITES:
        raise ValueError(
            f"unknown LLM call site {site!r}; "
            f"expected one of {sorted(KNOWN_LLM_SITES)!r}"
        )
    return build_claude_client(
        backend=config.backend_for_site(site),
        api_key=config.api_key,
    )


# Forward-ref import for the type hint above; never executed at runtime.
if False:  # pragma: no cover -- type-checking only
    from .config import ClaudeConfig  # noqa: F401
