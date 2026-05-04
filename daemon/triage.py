"""Single-shot triage of inbound contact mail via the Anthropic Messages API.

This module is the bridge between an inbound email from a known contact
and the approval-queue path that already exists for principal verbs. It
calls Claude with a tight system prompt, gets back a single
`draft_plan` tool call, validates it against a strict schema, enforces
the tier cap (no tier-4+), and returns a `TriagePlan` for the watcher
to enqueue.

Threat model recap (DESIGN.md §"System prompts" and §"Scenarios"):

- The email body, subject, and sender are UNTRUSTED. They reach the LLM
  inside delimited blocks and are described to the LLM as data only.
  The LLM has no path to action: its only output mechanism is
  `draft_plan`, and that output goes to the principal for explicit
  approval before any verb runs.
- The API key, the HOTP secret, and the principal's address never enter
  the prompt. The LLM has never seen them.
- The model is invoked exactly once per email. There is no agent loop,
  no multi-turn back-and-forth, and no on-the-fly tool calls beyond
  the single `draft_plan` invocation that triage requires.
- Tier enforcement is in *Python*, not in the prompt. Even if the model
  proposes a forbidden verb, this module refuses the plan rather than
  forwarding it to the executor.

The Anthropic SDK call is wrapped behind a small `ClaudeClient` Protocol
so tests can stub it without a network round-trip. Production code
constructs `AnthropicClient(api_key=...)` and calls through; tests
construct a `FakeClaudeClient` that returns canned tool-use payloads.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import ClaudeConfig, Contact


# ---- Verbs and tier cap ----------------------------------------------------

# The verbs the triage LLM is allowed to propose. These mirror the prompt
# in prompts/triage_default.md. Each entry maps a verb name to the tier
# the daemon will enqueue it at, and a list of required arg keys (other
# than the tier and bookkeeping fields).
#
# The TIER CAP is independent of the dict below: the watcher will refuse
# any plan whose verb is not in this dict, AND any plan whose tier (per
# this dict) exceeds TRIAGE_MAX_TIER. Two layers of defence so that an
# accidental tier-4 entry here cannot bypass the cap.
TRIAGE_MAX_TIER = 3

TRIAGE_VERBS: dict[str, dict[str, Any]] = {
    "reply": {"tier": 3, "required_args": ("body",)},
    "noop": {"tier": 1, "required_args": ()},
    "forward_to_principal": {"tier": 1, "required_args": ()},
    "flag_for_review": {"tier": 1, "required_args": ()},
}

# Risk flags the prompt may emit. Unknown flags are dropped silently
# (forward-compatible: a future prompt revision that adds a flag won't
# crash an older daemon). Known flags are passed through.
KNOWN_RISK_FLAGS = frozenset({
    "prompt_injection_attempted",
    "identity_claim",
    "urgency_pressure",
    "off_topic",
    "sensitive_topic",
    "low_information",
})


# ---- Result types ----------------------------------------------------------


@dataclass(frozen=True)
class TriagePlan:
    """A validated plan ready for the approval-queue path.

    Triage produces exactly one of these per inbound email. The watcher
    converts it into an `approvals` row at the indicated tier and pings
    the principal. No verb runs until the principal replies yes.
    """
    verb: str
    tier: int
    args: dict[str, Any]
    summary: str
    reasoning: str
    risk_flags: tuple[str, ...]
    notes: str
    raw_input_tokens: int
    raw_output_tokens: int


@dataclass(frozen=True)
class TriageError:
    """Triage failed in a way that is not the model's fault per se but
    still leaves the daemon without a usable plan. The watcher's
    response is to drop the email to TRIAGE_FAILED, log the reason,
    and ping the principal.
    """
    reason: str
    detail: str = ""


# ---- Client protocol -------------------------------------------------------


@dataclass(frozen=True)
class ClaudeResponse:
    """Minimal client-agnostic shape so tests don't need to mock the
    full Anthropic response object. Production code translates the
    real SDK response into this dataclass.
    """
    tool_uses: tuple[dict[str, Any], ...]
    text_blocks: tuple[str, ...]
    stop_reason: str
    input_tokens: int
    output_tokens: int


class ClaudeClient(Protocol):
    """Anything that can do a single Messages API call with one tool."""

    async def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ClaudeResponse: ...


class AnthropicClient:
    """Production ClaudeClient backed by the real anthropic SDK.

    The api_key is held privately on the SDK client; this class never
    logs it, never emits it in repr, never returns it from any method.
    """

    def __init__(self, *, api_key: str) -> None:
        # Imported lazily so the module can be imported (and tested with
        # the FakeClaudeClient) without anthropic being installed.
        from anthropic import AsyncAnthropic  # type: ignore[import-not-found]

        self._sdk = AsyncAnthropic(api_key=api_key)

    async def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ClaudeResponse:
        response = await self._sdk.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=[{"role": "user", "content": user}],
        )
        tool_uses: list[dict[str, Any]] = []
        text_blocks: list[str] = []
        for block in response.content:
            if block.type == "tool_use":
                tool_uses.append({
                    "name": block.name,
                    "input": dict(block.input) if isinstance(block.input, dict) else {},
                })
            elif block.type == "text":
                text_blocks.append(block.text)
        return ClaudeResponse(
            tool_uses=tuple(tool_uses),
            text_blocks=tuple(text_blocks),
            stop_reason=str(response.stop_reason),
            input_tokens=int(response.usage.input_tokens),
            output_tokens=int(response.usage.output_tokens),
        )


# ---- Prompt and tool schema ------------------------------------------------


def _load_prompt(prompts_dir: Path, name: str) -> str:
    path = prompts_dir / name
    return path.read_text(encoding="utf-8")


def build_system_prompt(prompts_dir: Path) -> str:
    """Compose the common header + the triage-specific prompt.

    Both files live under nightjar/prompts/ and are reloaded fresh every
    call so an operator-edited prompt picks up without a daemon restart.
    """
    header = _load_prompt(prompts_dir, "common.md")
    triage = _load_prompt(prompts_dir, "triage_default.md")
    return header.rstrip() + "\n\n" + triage.lstrip()


DRAFT_PLAN_TOOL: dict[str, Any] = {
    "name": "draft_plan",
    "description": (
        "Emit exactly one structured plan for the principal to approve. "
        "This is your only output mechanism; you must call it exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Neutral 1-3 sentence description of the email.",
            },
            "verb": {
                "type": "string",
                "enum": list(TRIAGE_VERBS.keys()),
                "description": (
                    "The action proposed if the principal approves. Pick "
                    "the lowest-risk verb that fits."
                ),
            },
            "args": {
                "type": "object",
                "description": "Verb-specific arguments. See system prompt.",
            },
            "reasoning": {
                "type": "string",
                "description": "1-3 sentences justifying the choice to the principal.",
            },
            "risk_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Subset of the risk flag vocabulary in the system prompt.",
            },
            "notes": {
                "type": "string",
                "description": "Optional extra context for the principal, <= 400 chars.",
            },
        },
        "required": ["summary", "verb", "args", "reasoning", "risk_flags"],
    },
}


# ---- Input building --------------------------------------------------------


def _strip_block_delimiters(text: str) -> str:
    """Remove any literal `</body>`, `</subject>`, etc. that an attacker
    might paste into the email content to confuse the delimiter scheme.

    We replace the closing-tag sequences with a visible marker so the
    LLM sees that something was tampered with rather than getting a
    partial body. The marker chars are themselves safe.
    """
    for tag in ("</contact_metadata>", "</sender>", "</subject>", "</body>"):
        text = text.replace(tag, "[stripped: closing-tag]")
    return text


def build_user_message(
    *,
    contact: Contact,
    sender: str,
    subject: str,
    body: str,
) -> str:
    """Format the four-block delimited input the prompt expects.

    Untrusted fields (sender, subject, body) get a strip pass so an
    attacker cannot inject a fake `</body>` to escape the block.
    contact_metadata is trusted (config-sourced) and not stripped.
    """
    safe_sender = _strip_block_delimiters(sender)
    safe_subject = _strip_block_delimiters(subject)
    safe_body = _strip_block_delimiters(body)
    daily_limit_repr = "unlimited" if contact.daily_limit == -1 else str(contact.daily_limit)
    return (
        "<contact_metadata>\n"
        f"contact_id: {contact.contact_id}\n"
        f"display_name: {contact.display_name}\n"
        f"relationship: {contact.relationship}\n"
        f"daily_limit: {daily_limit_repr}\n"
        "</contact_metadata>\n"
        "\n"
        "<sender>\n"
        f"{safe_sender}\n"
        "</sender>\n"
        "\n"
        "<subject>\n"
        f"{safe_subject}\n"
        "</subject>\n"
        "\n"
        "<body>\n"
        f"{safe_body}\n"
        "</body>\n"
    )


# ---- Validation ------------------------------------------------------------


_MAX_NOTES_LEN = 400
_MAX_REPLY_BODY_LEN = 2000


def validate_plan_payload(payload: dict[str, Any]) -> TriagePlan | TriageError:
    """Turn the raw `draft_plan` tool input into a typed TriagePlan, or
    a TriageError if any rule is broken.

    Rules enforced here, in order:
      - Required fields present.
      - `verb` is one of the known verbs.
      - Each verb's `required_args` are present in `args`.
      - The verb's tier <= TRIAGE_MAX_TIER (defence in depth: even if a
        future TRIAGE_VERBS edit slips a tier-4 verb in, the cap blocks).
      - reply.args.body is non-empty and length-capped.
      - notes <= MAX_NOTES_LEN.
      - All required string fields are strings; risk_flags is a list.

    Raw-input fields are NOT enforced here for length (sender, subject,
    body): those came from the inbound email and were already trusted
    to the inbox layer's reasonable-size limits.

    The token counts are filled in by `triage_contact_mail` after this
    runs, since they come from the SDK response, not from the payload.
    """
    if not isinstance(payload, dict):
        return TriageError(reason="malformed", detail="payload is not an object")

    # Required string fields.
    for field_name in ("summary", "verb", "reasoning"):
        if field_name not in payload:
            return TriageError(reason="missing_field", detail=field_name)
        if not isinstance(payload[field_name], str):
            return TriageError(reason="type_mismatch", detail=f"{field_name} not str")
        if not payload[field_name].strip() and field_name != "reasoning":
            # Reasoning may be empty in pathological cases but summary
            # and verb must be non-empty.
            return TriageError(reason="empty_field", detail=field_name)

    if "args" not in payload or not isinstance(payload["args"], dict):
        return TriageError(reason="missing_field", detail="args (must be object)")
    if "risk_flags" not in payload or not isinstance(payload["risk_flags"], list):
        return TriageError(reason="missing_field", detail="risk_flags (must be array)")

    verb = payload["verb"]
    if verb not in TRIAGE_VERBS:
        return TriageError(reason="unknown_verb", detail=verb)

    spec = TRIAGE_VERBS[verb]
    tier = int(spec["tier"])
    if tier > TRIAGE_MAX_TIER:
        return TriageError(
            reason="tier_too_high",
            detail=f"verb {verb!r} is tier {tier}, max is {TRIAGE_MAX_TIER}",
        )

    args = dict(payload["args"])
    for required in spec["required_args"]:
        if required not in args:
            return TriageError(
                reason="missing_arg",
                detail=f"verb {verb!r} requires arg {required!r}",
            )
        if not isinstance(args[required], str) or not args[required].strip():
            return TriageError(
                reason="empty_arg",
                detail=f"verb {verb!r} arg {required!r} is empty or non-string",
            )

    # Verb-specific length caps.
    if verb == "reply":
        body_len = len(args["body"])
        if body_len > _MAX_REPLY_BODY_LEN:
            return TriageError(
                reason="reply_too_long",
                detail=f"reply.body is {body_len} chars, max {_MAX_REPLY_BODY_LEN}",
            )

    notes = payload.get("notes", "")
    if not isinstance(notes, str):
        return TriageError(reason="type_mismatch", detail="notes not str")
    if len(notes) > _MAX_NOTES_LEN:
        return TriageError(
            reason="notes_too_long",
            detail=f"notes is {len(notes)} chars, max {_MAX_NOTES_LEN}",
        )

    raw_flags = payload["risk_flags"]
    risk_flags = tuple(
        f for f in raw_flags
        if isinstance(f, str) and f in KNOWN_RISK_FLAGS
    )

    return TriagePlan(
        verb=verb,
        tier=tier,
        args=args,
        summary=payload["summary"].strip(),
        reasoning=payload["reasoning"].strip(),
        risk_flags=risk_flags,
        notes=notes,
        raw_input_tokens=0,   # filled in by caller from ClaudeResponse
        raw_output_tokens=0,
    )


# ---- Top-level entry point -------------------------------------------------


async def triage_contact_mail(
    *,
    contact: Contact,
    sender: str,
    subject: str,
    body: str,
    config: ClaudeConfig,
    client: ClaudeClient,
    prompts_dir: Path,
) -> TriagePlan | TriageError:
    """Run one triage call. Returns a validated plan or a typed error.

    This function does no network I/O of its own: all SDK interaction
    goes through the injected `client`. Tests pass a FakeClaudeClient;
    production passes an AnthropicClient.
    """
    system = build_system_prompt(prompts_dir)
    user = build_user_message(
        contact=contact, sender=sender, subject=subject, body=body
    )

    try:
        response = await client.call(
            model=config.default_model,
            system=system,
            user=user,
            tools=[DRAFT_PLAN_TOOL],
            max_tokens=config.per_invocation_max_input_tokens,
        )
    except Exception as e:
        return TriageError(reason="sdk_error", detail=str(e))

    # We expect exactly one tool_use of name draft_plan. Anything else
    # is a model-side failure mode the prompt explicitly forbids.
    if not response.tool_uses:
        return TriageError(
            reason="no_tool_call",
            detail=f"stop_reason={response.stop_reason!r}, "
                   f"text_blocks={len(response.text_blocks)}",
        )
    if len(response.tool_uses) > 1:
        return TriageError(
            reason="multiple_tool_calls",
            detail=f"got {len(response.tool_uses)}",
        )

    tool_use = response.tool_uses[0]
    if tool_use.get("name") != "draft_plan":
        return TriageError(
            reason="unexpected_tool",
            detail=str(tool_use.get("name")),
        )

    result = validate_plan_payload(tool_use.get("input", {}))
    if isinstance(result, TriageError):
        return result

    # Stitch usage data onto the validated plan.
    return TriagePlan(
        verb=result.verb,
        tier=result.tier,
        args=result.args,
        summary=result.summary,
        reasoning=result.reasoning,
        risk_flags=result.risk_flags,
        notes=result.notes,
        raw_input_tokens=response.input_tokens,
        raw_output_tokens=response.output_tokens,
    )
