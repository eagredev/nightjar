"""Single-shot interpretation of free-form principal mail via Claude.

This module is the principal-side counterpart to triage.py. Where triage
parses an inbound email from a CONTACT into an approval-queue plan,
principal-interpret parses a free-form request from the PRINCIPAL into
one of three shapes:

    respond_inline      — tier 1, answer the principal's question
                          directly via an email reply
    dispatch_deterministic — tier 1, the principal's free-form maps
                            onto an existing deterministic verb;
                            the watcher runs it and replies with that
                            verb's real output
    propose_action      — tier 2-3, the principal wants a side-effect
                          action; produces a draft plan for the
                          approval queue

The earlier "yes interpret" confirmation gate was dropped on 2026-05-06
(see project-nightjar-drop-interpret-gate memory). An authenticated
principal sending free-form mail is already authority enough to spend
tokens on interpretation within the tier ceiling.

Threat-model notes:

- The principal is TRUSTED to this module. They could edit the prompt
  directly if they wanted to, so prompt-injection defences from
  triage.py do not apply here.
- HOWEVER: the executing tier system still applies. Even if the
  principal asks for tier-4, the LLM may not propose it, AND validation
  in this module refuses any plan above the tier cap independent of
  the prompt.
- The `<daemon_state>` and `<verb_registry>` blocks ARE trustworthy
  (daemon-derived); the LLM uses them to ground answers.
- The api_key, the HOTP secret, and any cryptographic material never
  enter the prompt. The LLM has never seen them.

The Anthropic SDK call is reused via the `ClaudeClient` Protocol
defined in triage.py, so production and tests share infrastructure.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ClaudeConfig
from .triage import ClaudeClient


# ---- Tier cap -------------------------------------------------------------

# Principal-interpret produces tier-1 inline responses (no approval) or
# tier-2/3 proposals (queued for approval). Tier 4+ is forbidden — even
# the principal must use the deterministic `[code] verb` path to opt
# into double-confirm verbs. This cap is enforced in Python regardless
# of what the prompt says, so a prompt edit cannot widen the executing
# surface accidentally.
PRINCIPAL_INTERPRET_MAX_TIER = 3

# Output kinds the LLM may emit.
KIND_RESPOND_INLINE = "respond_inline"
KIND_DISPATCH_DETERMINISTIC = "dispatch_deterministic"
KIND_PROPOSE_ACTION = "propose_action"

_KNOWN_KINDS = frozenset({
    KIND_RESPOND_INLINE,
    KIND_DISPATCH_DETERMINISTIC,
    KIND_PROPOSE_ACTION,
})


# ---- Result types ---------------------------------------------------------


@dataclass(frozen=True)
class InlineResponse:
    """Tier-1 direct answer. The watcher emails `body` straight back to
    the principal with a `RESPONDED` state transition."""
    summary: str
    body: str
    reasoning: str
    raw_input_tokens: int
    raw_output_tokens: int


@dataclass(frozen=True)
class DeterministicDispatch:
    """Tier-1 redirect to a deterministic verb. The watcher constructs
    a synthetic ParsedCommand from `verb` + `args` and runs it through
    the same handler the deterministic path would use, then emails the
    result. `summary` and `reasoning` are surfaced in the reply so the
    principal sees what the LLM thought they meant."""
    summary: str
    verb: str
    args: dict[str, str]
    reasoning: str
    raw_input_tokens: int
    raw_output_tokens: int


@dataclass(frozen=True)
class ActionProposal:
    """Tier 2-3 action for the approval queue. Lands in the same
    `approvals` table tier-2+ deterministic verbs use. The verb may be
    a known registry name (e.g. "block") or a free-form description
    (e.g. "draft and send a polite decline to alice@…"); in the latter
    case the executor wiring depends on the manifest-gated work landing.
    `irreversible_warning` is surfaced verbatim in the approval ping."""
    summary: str
    verb: str
    tier: int
    args: dict[str, Any]
    reasoning: str
    irreversible_warning: str
    raw_input_tokens: int
    raw_output_tokens: int


@dataclass(frozen=True)
class InterpretError:
    """Interpret failed in a way that leaves the daemon without a usable
    output. The watcher transitions the inbound to INTERPRET_FAILED,
    logs the reason, and pings the principal so they know."""
    reason: str
    detail: str = ""


# Convenience type for the union the watcher consumes.
InterpretOutcome = (
    InlineResponse | DeterministicDispatch | ActionProposal | InterpretError
)


# ---- Daemon state snapshot ------------------------------------------------


@dataclass(frozen=True)
class DaemonStateSnapshot:
    """Lightweight snapshot of daemon state passed into the user message.

    Built by the watcher from existing state.py accessors; this is just
    the data shape, not the gathering logic. The watcher is responsible
    for keeping the snapshot cheap to compute (no full IMAP walks here;
    state-db queries only).
    """
    pending_approvals: tuple[dict[str, Any], ...]
    state_counts_24h: dict[str, int]
    last_catchup_iso: str  # "(never)" if no catchup has run yet


# ---- Verb registry summary ------------------------------------------------


@dataclass(frozen=True)
class VerbRegistrySummary:
    """The set of deterministic verb names the LLM may dispatch to,
    grouped by tier band. Built from principal_commands.VERB_REGISTRY
    by the watcher so this module doesn't import principal_commands
    (which would create a cycle through inbox_watcher).
    """
    tier1_names: tuple[str, ...]
    tier2_3_names: tuple[str, ...]


# ---- Prompt and tool schema -----------------------------------------------


def _load_prompt(prompts_dir: Path, name: str) -> str:
    return (prompts_dir / name).read_text(encoding="utf-8")


def build_system_prompt(prompts_dir: Path) -> str:
    """Compose the common header + the principal-interpret prompt.

    Reloaded fresh every call so an operator-edited prompt picks up
    without a daemon restart, same convention as triage.
    """
    header = _load_prompt(prompts_dir, "common.md")
    body = _load_prompt(prompts_dir, "principal_interpret.md")
    return header.rstrip() + "\n\n" + body.lstrip()


INTERPRET_REQUEST_TOOL: dict[str, Any] = {
    "name": "interpret_request",
    "description": (
        "Emit exactly one structured interpretation of the principal's "
        "free-form request. This is your only output mechanism; you must "
        "call it exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": list(_KNOWN_KINDS),
                "description": (
                    "Which output shape: respond_inline (tier-1 answer), "
                    "dispatch_deterministic (tier-1 verb dispatch), or "
                    "propose_action (tier 2-3 approval-queue plan)."
                ),
            },
            "summary": {
                "type": "string",
                "description": "1-sentence neutral description of the request.",
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "Why this kind/verb/answer was chosen, "
                    "in 1-3 sentences. Surfaced to the principal."
                ),
            },
            # Shape-specific fields. Validation enforces which fields
            # are required for each kind.
            "body": {
                "type": "string",
                "description": (
                    "respond_inline only: the prose answer to email back. "
                    "Plain text. Suitable for direct inclusion in a reply."
                ),
            },
            "verb": {
                "type": "string",
                "description": (
                    "dispatch_deterministic: name of an existing tier-1 "
                    "verb from <verb_registry>. propose_action: either "
                    "an existing tier-2/3 verb name, or a free-form "
                    "action description."
                ),
            },
            "args": {
                "type": "object",
                "description": (
                    "Verb-specific arguments. For dispatch_deterministic, "
                    "must match the verb's required args. For "
                    "propose_action, structure depends on the verb."
                ),
            },
            "tier": {
                "type": "integer",
                "description": (
                    "propose_action only: 2 (reversible local) or 3 "
                    "(outbound mail). Never 4+."
                ),
            },
            "irreversible_warning": {
                "type": "string",
                "description": (
                    "propose_action only: optional warning surfaced "
                    "verbatim in the approval ping. Empty string if "
                    "no special warning needed."
                ),
            },
        },
        "required": ["kind", "summary", "reasoning"],
    },
}


# ---- User-message rendering -----------------------------------------------


def _strip_block_delimiters(text: str) -> str:
    """Defensive: even though the principal is trusted, the parser shape
    expects unambiguous block boundaries. If the principal's body
    contains a literal `</request_body>` we replace it with a marker
    so the LLM still parses the input cleanly.
    """
    for tag in (
        "</request_subject>",
        "</request_body>",
        "</daemon_state>",
        "</verb_registry>",
    ):
        text = text.replace(tag, "[stripped: closing-tag]")
    return text


_MAX_PENDING_APPROVALS_RENDERED = 20


def _render_pending_approvals(rows: tuple[dict[str, Any], ...]) -> str:
    """Format the pending-approvals table for the daemon_state block."""
    if not rows:
        return "(none)"
    lines: list[str] = []
    import datetime
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    for row in rows[:_MAX_PENDING_APPROVALS_RENDERED]:
        ttl_seconds = max(0, int(row.get("expires_at", now_ts)) - now_ts)
        ttl_days = ttl_seconds / 86400.0
        token = str(row.get("token", "?"))
        verb = str(row.get("verb", "?"))
        # args can be dict or JSON-encoded string per state.py shape;
        # render compactly without exploding nested structures.
        args_repr = row.get("args", {})
        if isinstance(args_repr, dict):
            args_repr = ", ".join(f"{k}={v}" for k, v in args_repr.items())
        lines.append(
            f"  - #{token}  {verb}  {args_repr}  expires_in {ttl_days:.1f}d"
        )
    if len(rows) > _MAX_PENDING_APPROVALS_RENDERED:
        lines.append(f"  (+{len(rows) - _MAX_PENDING_APPROVALS_RENDERED} more)")
    return "\n".join(lines)


def build_user_message(
    *,
    request_subject: str,
    request_body: str,
    state_snapshot: DaemonStateSnapshot,
    verb_registry: VerbRegistrySummary,
) -> str:
    """Format the three-block user message principal-interpret expects."""
    safe_subject = _strip_block_delimiters(request_subject or "(empty)")
    safe_body = _strip_block_delimiters(request_body or "(empty)")
    pending_block = _render_pending_approvals(state_snapshot.pending_approvals)
    counts = state_snapshot.state_counts_24h
    if counts:
        counts_lines = "\n".join(
            f"  {k}: {v}" for k, v in sorted(counts.items())
        )
    else:
        counts_lines = "  (no recent messages)"
    tier1_names = ", ".join(verb_registry.tier1_names) or "(none)"
    tier23_names = ", ".join(verb_registry.tier2_3_names) or "(none)"
    return (
        "<request_subject>\n"
        f"{safe_subject}\n"
        "</request_subject>\n"
        "\n"
        "<request_body>\n"
        f"{safe_body}\n"
        "</request_body>\n"
        "\n"
        "<daemon_state>\n"
        f"pending_approvals: {len(state_snapshot.pending_approvals)}\n"
        f"{pending_block}\n"
        "recent_message_states (last 24h):\n"
        f"{counts_lines}\n"
        f"last_catchup: {state_snapshot.last_catchup_iso}\n"
        "</daemon_state>\n"
        "\n"
        "<verb_registry>\n"
        f"Tier 1 (inline-dispatchable): {tier1_names}\n"
        f"Tier 2-3 (require approval): {tier23_names}\n"
        "</verb_registry>\n"
    )


# ---- Validation -----------------------------------------------------------


_MAX_BODY_LEN = 4000           # respond_inline body
_MAX_REASONING_LEN = 1000
_MAX_SUMMARY_LEN = 400
_MAX_WARNING_LEN = 600


def validate_payload(
    payload: dict[str, Any],
    *,
    tier1_verb_names: frozenset[str],
) -> InterpretOutcome:
    """Turn the raw `interpret_request` tool input into a typed outcome,
    or an InterpretError if any rule is broken.

    `tier1_verb_names` is the set of registry names that
    dispatch_deterministic is allowed to target. Passed in (rather than
    imported here) so tests don't need a full registry.

    Rules enforced:
      - kind is one of the known kinds.
      - Required common fields (summary, reasoning) present and string.
      - kind-specific required fields present.
      - For dispatch_deterministic: verb is in `tier1_verb_names`.
      - For propose_action: tier in {2, 3} (defensive cap on top of
        prompt). Verb is non-empty string.
      - All length caps respected.
    """
    if not isinstance(payload, dict):
        return InterpretError(reason="malformed", detail="payload not an object")

    kind = payload.get("kind")
    if kind not in _KNOWN_KINDS:
        return InterpretError(reason="unknown_kind", detail=str(kind))

    summary = payload.get("summary")
    reasoning = payload.get("reasoning")
    if not isinstance(summary, str) or not summary.strip():
        return InterpretError(reason="missing_field", detail="summary")
    if not isinstance(reasoning, str):
        return InterpretError(reason="type_mismatch", detail="reasoning not str")
    if len(summary) > _MAX_SUMMARY_LEN:
        return InterpretError(
            reason="summary_too_long",
            detail=f"{len(summary)} > {_MAX_SUMMARY_LEN}",
        )
    if len(reasoning) > _MAX_REASONING_LEN:
        return InterpretError(
            reason="reasoning_too_long",
            detail=f"{len(reasoning)} > {_MAX_REASONING_LEN}",
        )

    if kind == KIND_RESPOND_INLINE:
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip():
            return InterpretError(reason="missing_field", detail="body")
        if len(body) > _MAX_BODY_LEN:
            return InterpretError(
                reason="body_too_long",
                detail=f"{len(body)} > {_MAX_BODY_LEN}",
            )
        return InlineResponse(
            summary=summary.strip(),
            body=body,
            reasoning=reasoning.strip(),
            raw_input_tokens=0,
            raw_output_tokens=0,
        )

    if kind == KIND_DISPATCH_DETERMINISTIC:
        verb = payload.get("verb")
        if not isinstance(verb, str) or not verb.strip():
            return InterpretError(reason="missing_field", detail="verb")
        if verb not in tier1_verb_names:
            return InterpretError(
                reason="unknown_verb",
                detail=f"{verb!r} not in tier-1 registry",
            )
        args = payload.get("args", {})
        if not isinstance(args, dict):
            return InterpretError(reason="type_mismatch", detail="args not object")
        # Coerce args to {str: str}; the deterministic handlers expect
        # string values per principal_commands.parse_principal_command.
        coerced: dict[str, str] = {}
        for k, v in args.items():
            if not isinstance(k, str):
                return InterpretError(reason="bad_arg_key", detail=str(k))
            coerced[k] = str(v)
        return DeterministicDispatch(
            summary=summary.strip(),
            verb=verb,
            args=coerced,
            reasoning=reasoning.strip(),
            raw_input_tokens=0,
            raw_output_tokens=0,
        )

    # KIND_PROPOSE_ACTION
    verb = payload.get("verb")
    if not isinstance(verb, str) or not verb.strip():
        return InterpretError(reason="missing_field", detail="verb")
    tier = payload.get("tier")
    if not isinstance(tier, int):
        return InterpretError(reason="missing_field", detail="tier")
    if tier < 2 or tier > PRINCIPAL_INTERPRET_MAX_TIER:
        return InterpretError(
            reason="tier_out_of_range",
            detail=f"tier={tier}, allowed 2..{PRINCIPAL_INTERPRET_MAX_TIER}",
        )
    args = payload.get("args", {})
    if not isinstance(args, dict):
        return InterpretError(reason="type_mismatch", detail="args not object")
    warning = payload.get("irreversible_warning", "")
    if not isinstance(warning, str):
        return InterpretError(reason="type_mismatch", detail="irreversible_warning not str")
    if len(warning) > _MAX_WARNING_LEN:
        return InterpretError(
            reason="warning_too_long",
            detail=f"{len(warning)} > {_MAX_WARNING_LEN}",
        )
    return ActionProposal(
        summary=summary.strip(),
        verb=verb.strip(),
        tier=tier,
        args=dict(args),
        reasoning=reasoning.strip(),
        irreversible_warning=warning.strip(),
        raw_input_tokens=0,
        raw_output_tokens=0,
    )


# ---- Top-level entry point ------------------------------------------------


async def interpret_principal_request(
    *,
    request_subject: str,
    request_body: str,
    state_snapshot: DaemonStateSnapshot,
    verb_registry: VerbRegistrySummary,
    config: ClaudeConfig,
    client: ClaudeClient,
    prompts_dir: Path,
) -> InterpretOutcome:
    """Run one principal-interpret call. Returns an outcome or error.

    No network I/O of its own: all SDK interaction goes through the
    injected `client`. Tests pass a FakeClaudeClient; production passes
    an AnthropicClient (the same instance triage uses; one Claude
    config = one client).
    """
    system = build_system_prompt(prompts_dir)
    user = build_user_message(
        request_subject=request_subject,
        request_body=request_body,
        state_snapshot=state_snapshot,
        verb_registry=verb_registry,
    )

    try:
        response = await client.call(
            model=config.model_for_site("principal_interpret"),
            system=system,
            user=user,
            tools=[INTERPRET_REQUEST_TOOL],
            max_tokens=config.per_invocation_max_input_tokens,
        )
    except Exception as e:
        return InterpretError(reason="sdk_error", detail=str(e))

    if not response.tool_uses:
        return InterpretError(
            reason="no_tool_call",
            detail=f"stop_reason={response.stop_reason!r}, "
                   f"text_blocks={len(response.text_blocks)}",
        )
    if len(response.tool_uses) > 1:
        return InterpretError(
            reason="multiple_tool_calls",
            detail=f"got {len(response.tool_uses)}",
        )

    tool_use = response.tool_uses[0]
    if tool_use.get("name") != "interpret_request":
        return InterpretError(
            reason="unexpected_tool",
            detail=str(tool_use.get("name")),
        )

    tier1_names = frozenset(verb_registry.tier1_names)
    outcome = validate_payload(
        tool_use.get("input", {}),
        tier1_verb_names=tier1_names,
    )
    if isinstance(outcome, InterpretError):
        return outcome

    # Stitch token counts onto the typed outcome. Each shape carries the
    # same two fields, so we replace by dataclass kind.
    in_tok = response.input_tokens
    out_tok = response.output_tokens
    if isinstance(outcome, InlineResponse):
        return InlineResponse(
            summary=outcome.summary, body=outcome.body,
            reasoning=outcome.reasoning,
            raw_input_tokens=in_tok, raw_output_tokens=out_tok,
        )
    if isinstance(outcome, DeterministicDispatch):
        return DeterministicDispatch(
            summary=outcome.summary, verb=outcome.verb, args=outcome.args,
            reasoning=outcome.reasoning,
            raw_input_tokens=in_tok, raw_output_tokens=out_tok,
        )
    # ActionProposal
    return ActionProposal(
        summary=outcome.summary, verb=outcome.verb, tier=outcome.tier,
        args=outcome.args, reasoning=outcome.reasoning,
        irreversible_warning=outcome.irreversible_warning,
        raw_input_tokens=in_tok, raw_output_tokens=out_tok,
    )
