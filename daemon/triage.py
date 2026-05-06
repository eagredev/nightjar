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

import re
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
    "forward_to_principal": {"tier": 3, "required_args": ()},
    "flag_for_review": {"tier": 1, "required_args": ()},
    # Step 7b: synthetic verb produced ONLY by the orchestrator's
    # out-of-scope path (classify-as-out_of_scope or classifier
    # failure). NOT in the model's tool enum (see _MODEL_VERBS
    # below) — triage cannot pick this. Tier 3 because the result
    # composes a reply body and sends it, which still routes
    # through the principal's normal approval flow.
    "out_of_scope_decline": {"tier": 3, "required_args": ("body",)},
}

# Subset of TRIAGE_VERBS that the model is allowed to emit. Synthetic
# verbs (currently just out_of_scope_decline) are excluded from the
# enum so the model has no path to produce them. The executor still
# accepts them via TRIAGE_VERBS for the orchestrator's synthetic
# plans.
_MODEL_VERBS = (
    "reply",
    "noop",
    "forward_to_principal",
    "flag_for_review",
)

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
    "hidden_content_suspected",
})


# Provenance vocabulary for note proposals. Mirrors notes_store's
# _KNOWN_ATTRIBUTIONS — kept in lockstep but defined here so the tool
# schema can reference it without importing notes_store at module load.
_KNOWN_ATTRIBUTIONS: tuple[str, ...] = ("observed", "asserted", "self")


# ---- Message structure -----------------------------------------------------

# The `<body>` block the LLM sees is the plain-text view of the email. It
# does not show HTML alternatives, attachments, inline images, or remote
# resources. A `<message_structure>` block alongside it gives the LLM
# the structural facts it would otherwise have to guess about. The LLM
# is prompted to flag `hidden_content_suspected` when this block reports
# parts the plain-text view cannot represent.

# Cap on the rendered attachment-names list. Filenames can be long and
# numerous; this stops a pathological multipart from blowing past the
# token budget. Names beyond the cap are summarised as "(+N more)".
_MAX_ATTACHMENT_NAMES_RENDERED = 10
_MAX_ATTACHMENT_NAME_LEN = 80


@dataclass(frozen=True)
class MessageStructure:
    """Structural fingerprint of an inbound MIME message.

    All fields describe the message as the daemon parsed it, NOT as the
    LLM sees it. The LLM sees only the plain-text body; this dataclass
    is what it would learn if it could read the headers and walk the
    parts. Used to ground hidden-content detection.

    `plain_size_bytes` and `html_size_bytes` are the byte sizes of the
    decoded plain-text and (if present) HTML alternatives respectively.
    Reporting them separately lets the LLM compare the two: a sparse
    plain-text body with a much larger HTML alternative is the real
    signal that the HTML carries content the plain-text view doesn't.

    We deliberately do NOT report the size of the entire raw RFC822
    envelope — that includes 5+ KB of MTA-injected headers (ARC-Seal,
    ARC-Message-Signature, DKIM, Received chains) which have nothing
    to do with the content the sender actually wrote, and would
    consistently make small plain-text emails look "huge" and trip
    the hidden-content sweep falsely.
    """
    has_html_alternative: bool
    attachment_count: int
    attachment_names: tuple[str, ...]
    inline_image_count: int
    plain_size_bytes: int
    html_size_bytes: int  # 0 when no HTML alternative is present
    body_truncated_in_prompt: bool


# ---- Result types ----------------------------------------------------------


@dataclass(frozen=True)
class NoteProposal:
    """One proposed addition to a contact's rapport-notes file.

    Triage emits these in the `note_proposals` array on its plan when
    it judges something worth recording about the contact. The watcher
    applies them directly to the contact's notes file via
    notes_store.append_note() — no approval queue, no per-contact
    auto-approve flag. The principal reviews via `show notes` and
    deletes via `forget`. See project-nightjar-step-8.md for the
    memory architecture rationale (rapport notes are working memory,
    not a privileged-action surface).

    `scope` is the topic this note belongs to. For scoped contacts
    it MUST be one of the contact's registered scopes (the tool
    schema's enum enforces this on the model side; validation enforces
    it daemon-side as belt-and-braces). For unscoped contacts (no
    scopes set) `scope` is None — there's no scope vocabulary.

    `is_universal` is the explicit override for genuinely cross-cutting
    notes. When True, the daemon writes the bullet as wildcard-visible
    (`[scopes: *]`) instead of under the chosen scope. When False
    (the default), the bullet is tagged with `scope`. The split exists
    because the model could not be reliably prompted to use a single
    `null` field correctly — see the Step 7 live-test memo.

    `attribution` is the provenance classification:
      - 'observed' — daemon saw this firsthand (writing style, tone,
        cadence, message structure). Trustworthy.
      - 'asserted' — the contact claimed something about a third party
        (the principal, another collaborator). UNVERIFIED.
      - 'self' — the contact claimed something about themselves
        (preferences, project status). UNVERIFIED.
    The model is required to pick one; the schema enforces the enum.
    Defends against persistent poisoning attacks (DR2 from the
    2026-05-05 red-team session) by surfacing sender attribution to
    the principal when they `show notes`.
    """
    scope: str | None
    section_heading: str
    body: str
    is_universal: bool = False
    attribution: str = "observed"


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
    # Step 7d: zero or more proposed additions to the contact's
    # rapport-notes file. Default empty tuple — many plans propose no
    # notes (routine messages, attempts that failed, low-content
    # interactions). The watcher iterates these and enqueues each one
    # into note_proposals after the plan transitions to TRIAGED.
    note_proposals: tuple[NoteProposal, ...] = ()


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


def build_draft_plan_tool(contact: Contact | None = None) -> dict[str, Any]:
    """Build the draft_plan tool spec, parametrised on the contact's scopes.

    When the contact has scopes set, the note_proposals item schema enforces:
      - `scope` is a required enum of the contact's allowed scopes (no null,
        no wildcards). The model cannot pick a scope outside the contact's
        registered list, and cannot omit the field.
      - `is_universal` is a required boolean. When true, the daemon writes
        the note as wildcard-visible (`[scopes: *]`); when false, the daemon
        writes it under the chosen scope. The bar for `is_universal=true`
        is high — see prompt for guidance.

    When the contact has no scopes, the schema accepts the legacy shape
    (scope: string|null, no is_universal) — there's no scope vocabulary to
    enforce against, and universal-vs-scoped is not a meaningful choice.

    Splitting "what scope does this belong to" from "is this genuinely
    cross-cutting" forces the model to commit to two independent decisions.
    The previous shape (scope: null|string) collapsed both into one slot,
    which the model resolved to "null" too liberally — see Step 7 live-test
    observations in project-nightjar-step-7.md.
    """
    attribution_property = {
        "type": "string",
        "enum": list(_KNOWN_ATTRIBUTIONS),
        "description": (
            "Required. Provenance of this observation. Pick exactly one:\n"
            "- 'observed': you saw this firsthand from the contact's "
            "behaviour or message structure (writing style, tone, "
            "cadence, response timing, attachment habits). Trustworthy.\n"
            "- 'asserted': the contact stated something about a THIRD "
            "PARTY — the principal, another collaborator, an external "
            "fact. UNVERIFIED. Use this for any 'X said Y', 'the team "
            "agreed', 'Dylan approved' content. The principal will see "
            "this flagged when they review notes.\n"
            "- 'self': the contact stated something about THEMSELVES "
            "(their preferences, their project status, their location). "
            "UNVERIFIED but lower-risk than 'asserted' — false self-"
            "claims tend to surface naturally over time.\n"
            "When in doubt between 'observed' and 'self', pick 'self'. "
            "When in doubt between 'self' and 'asserted', pick 'asserted'. "
            "Better to over-flag than to laundering a sender claim into "
            "established context."
        ),
    }
    if contact is not None and contact.scopes:
        proposal_item_schema = {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": list(contact.scopes),
                    "description": (
                        "Required. Pick the scope this note belongs to. "
                        "Must be one of the contact's registered scopes. "
                        "Never set this for content unrelated to the active "
                        "conversation — see is_universal for the cross-cutting "
                        "case."
                    ),
                },
                "is_universal": {
                    "type": "boolean",
                    "description": (
                        "Required. Set true ONLY for content that's safe to "
                        "surface in any future conversation regardless of "
                        "topic — communication style, address routing, "
                        "writing conventions. Default false. Project "
                        "deadlines, tools, and work patterns are NOT "
                        "universal even if they sound general — they belong "
                        "to their scope."
                    ),
                },
                "attribution": attribution_property,
                "section_heading": {
                    "type": "string",
                    "description": (
                        "Section heading the bullet belongs under (an "
                        "existing heading or a new one). Short topic label."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "The bullet text. Concise prose, one observation, "
                        "no leading hyphen — the daemon adds bullet formatting."
                    ),
                },
            },
            "required": [
                "scope", "is_universal", "attribution",
                "section_heading", "body",
            ],
            "additionalProperties": False,
        }
    else:
        proposal_item_schema = {
            "type": "object",
            "properties": {
                "scope": {
                    "type": ["string", "null"],
                    "description": (
                        "Always null for unscoped contacts — there's no "
                        "scope vocabulary to tag against."
                    ),
                },
                "attribution": attribution_property,
                "section_heading": {
                    "type": "string",
                    "description": (
                        "Section heading the bullet belongs under (an "
                        "existing heading or a new one). Short topic label."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "The bullet text. Concise prose, one observation, "
                        "no leading hyphen — the daemon adds bullet formatting."
                    ),
                },
            },
            "required": [
                "scope", "attribution", "section_heading", "body",
            ],
            "additionalProperties": False,
        }

    return {
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
                    "enum": list(_MODEL_VERBS),
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
                    "description": "Optional extra context for the principal, <= 800 chars.",
                },
                "note_proposals": {
                    "type": "array",
                    "description": (
                        "Optional. Zero or more proposed additions to "
                        "the contact's rapport-notes file. Propose only "
                        "concrete observations worth remembering "
                        "long-term — never propose for routine messages "
                        "or just to fill the field. Default to empty."
                    ),
                    "items": proposal_item_schema,
                },
            },
            "required": ["summary", "verb", "args", "reasoning", "risk_flags"],
        },
    }


# Legacy unscoped shape for callers that pre-date Step 7b's contact-aware
# tool building. Used by direct triage_contact_mail callers that pass no
# contact context. Equivalent to build_draft_plan_tool(None).
DRAFT_PLAN_TOOL: dict[str, Any] = build_draft_plan_tool(None)


# ---- Input building --------------------------------------------------------


def _strip_block_delimiters(text: str) -> str:
    """Remove any literal `</body>`, `</subject>`, etc. that an attacker
    might paste into the email content to confuse the delimiter scheme.

    We replace the closing-tag sequences with a visible marker so the
    LLM sees that something was tampered with rather than getting a
    partial body. The marker chars are themselves safe.
    """
    for tag in (
        "</contact_metadata>",
        "</sender>",
        "</subject>",
        "</message_structure>",
        "</body>",
        "</notes>",
    ):
        text = text.replace(tag, "[stripped: closing-tag]")
    return text


def _render_attachment_names(names: tuple[str, ...]) -> str:
    """Compress a list of attachment filenames for the structure block.

    Each name is truncated to _MAX_ATTACHMENT_NAME_LEN; the list as a
    whole is truncated to _MAX_ATTACHMENT_NAMES_RENDERED entries with
    a "(+N more)" suffix. The strip-block-delimiters pass also applies
    so filenames cannot escape the structure block.
    """
    if not names:
        return "(none)"
    rendered: list[str] = []
    for name in names[:_MAX_ATTACHMENT_NAMES_RENDERED]:
        clean = _strip_block_delimiters(name)
        if len(clean) > _MAX_ATTACHMENT_NAME_LEN:
            clean = clean[:_MAX_ATTACHMENT_NAME_LEN] + "..."
        rendered.append(clean)
    suffix = ""
    if len(names) > _MAX_ATTACHMENT_NAMES_RENDERED:
        suffix = f" (+{len(names) - _MAX_ATTACHMENT_NAMES_RENDERED} more)"
    return ", ".join(rendered) + suffix


def build_user_message(
    *,
    contact: Contact,
    sender: str,
    subject: str,
    body: str,
    structure: MessageStructure,
    notes: str = "",
) -> str:
    """Format the delimited input the prompt expects.

    Six blocks total: contact_metadata, sender, subject,
    message_structure, notes, body. The notes block is Step 7b and
    carries scope-filtered rapport notes when the daemon has any to
    inject. Empty `notes` produces an empty `<notes>` block; the
    prompt instructs the LLM to treat that as "no recorded context."

    Untrusted fields (sender, subject, body) get a strip pass so an
    attacker cannot inject a fake `</body>` to escape the block.
    contact_metadata is trusted (config-sourced) and not stripped.
    The `<message_structure>` block is daemon-derived facts about the
    raw MIME structure. Filenames inside it are also stripped because
    a contact controls what they're called. Notes are operator-
    authored (or daemon-proposed-then-operator-approved) and treated
    as trusted, but we still strip the close-tag because a corrupted
    notes file could still confuse the parser.
    """
    safe_sender = _strip_block_delimiters(sender)
    safe_subject = _strip_block_delimiters(subject)
    safe_body = _strip_block_delimiters(body)
    safe_notes = _strip_block_delimiters(notes)
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
        "<message_structure>\n"
        f"has_html_alternative: {str(structure.has_html_alternative).lower()}\n"
        f"attachment_count: {structure.attachment_count}\n"
        f"attachment_names: {_render_attachment_names(structure.attachment_names)}\n"
        f"inline_image_count: {structure.inline_image_count}\n"
        f"plain_size_bytes: {structure.plain_size_bytes}\n"
        f"html_size_bytes: {structure.html_size_bytes}\n"
        f"body_truncated_in_prompt: {str(structure.body_truncated_in_prompt).lower()}\n"
        "</message_structure>\n"
        "\n"
        "<notes>\n"
        f"{safe_notes}\n"
        "</notes>\n"
        "\n"
        "<body>\n"
        f"{safe_body}\n"
        "</body>\n"
    )


# ---- Validation ------------------------------------------------------------


_MAX_NOTES_LEN = 800
_MAX_REPLY_BODY_LEN = 2000

# Step 7d: caps on note_proposals fields. Heading is a section title
# so it should be short. Body is one bullet — concise prose, not a
# paragraph. The cap also bounds the cost of a runaway model that
# tries to dump message body into a proposal body.
_MAX_NOTE_HEADING_LEN = 80
_MAX_NOTE_BODY_LEN = 280
# Cap on how many proposals per plan. The model should propose
# sparingly; many proposals for one email is a smell.
_MAX_NOTE_PROPOSALS_PER_PLAN = 5


def validate_plan_payload(
    payload: dict[str, Any],
    *,
    contact: Contact | None = None,
) -> TriagePlan | TriageError:
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
      - note_proposals: array if present; each item has scope (str or
        null) + section_heading + body, length-capped, count-capped.
        When `contact` is provided, scopes are checked against
        contact.scopes (None always allowed; non-None must be in
        contact.scopes). Bad proposals are dropped silently from the
        plan rather than failing the whole plan — losing one note is
        recoverable; losing the reply isn't.
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

    # Step 7d: validate note_proposals leniently. Bad proposals are
    # dropped from the returned plan; we don't fail the whole plan
    # because the reply is the main payload. Caps apply per-item
    # and across-the-list.
    raw_proposals = payload.get("note_proposals", [])
    note_proposals: list[NoteProposal] = []
    if isinstance(raw_proposals, list):
        for raw in raw_proposals[:_MAX_NOTE_PROPOSALS_PER_PLAN]:
            if not isinstance(raw, dict):
                continue
            scope = raw.get("scope")
            if scope is not None and not isinstance(scope, str):
                continue
            heading = raw.get("section_heading")
            body_text = raw.get("body")
            if not isinstance(heading, str) or not heading.strip():
                continue
            if not isinstance(body_text, str) or not body_text.strip():
                continue
            heading_clean = heading.strip()[:_MAX_NOTE_HEADING_LEN]
            body_clean = body_text.strip()[:_MAX_NOTE_BODY_LEN]
            raw_is_universal = raw.get("is_universal", False)
            is_universal = bool(raw_is_universal) if isinstance(raw_is_universal, bool) else False

            # Provenance: attribution is required by the schema, but
            # validate defensively. Unknown values fall back to
            # 'asserted' — fail-pessimistic so a model that omits or
            # garbles the field can't downgrade its own claim to
            # 'observed' (the trustworthy bucket). See provenance
            # tagging memo / 2026-05-05 red-team observations.
            raw_attr = raw.get("attribution")
            if isinstance(raw_attr, str) and raw_attr in _KNOWN_ATTRIBUTIONS:
                attribution = raw_attr
            else:
                attribution = "asserted"

            # Scope vs contact.scopes: the tool schema's enum already
            # constrains what the model can emit, but we re-check
            # daemon-side as belt-and-braces. The rules are:
            #   - Contact has scopes: scope MUST be a string in
            #     contact.scopes. None or unknown scope = drop the
            #     proposal silently. is_universal stays as supplied.
            #   - Contact has no scopes: scope MUST be None.
            #     is_universal is ignored (forced False) — there's no
            #     scope vocabulary so universal-vs-scoped is not a
            #     meaningful distinction.
            if contact is not None:
                if contact.scopes:
                    if not isinstance(scope, str) or scope not in contact.scopes:
                        continue
                else:
                    if scope is not None:
                        continue
                    is_universal = False

            note_proposals.append(NoteProposal(
                scope=scope,
                section_heading=heading_clean,
                body=body_clean,
                is_universal=is_universal,
                attribution=attribution,
            ))

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
        note_proposals=tuple(note_proposals),
    )


# ---- Notes-enumeration gate (Step 7 wave 3a) ------------------------------


# Minimum significant-token length: tokens shorter than this get dropped
# from match candidates. "the", "is", "of" coincidentally collide; "auth",
# "TTL", "600s" do not.
_MIN_TOKEN_LEN = 3

# How many consecutive significant tokens a reply must share with an
# unverified bullet body before we count the reply as enumerating it.
# Calibrated against the 2026-05-06 auto-redteam burn m5 output: the
# poisoned reply contained four-token spans copied from earlier
# self-tagged bullet bodies. Three-token spans hit too many coincidental
# matches in routine prose; five-token spans missed paraphrased
# enumerations the gate is meant to catch.
_MIN_CONSECUTIVE_TOKEN_MATCH = 4

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _significant_tokens(text: str) -> tuple[str, ...]:
    """Lowercase, alphanumeric-only token list, dropping short tokens.

    Used by the notes-enumeration gate for matching reply bodies against
    unverified bullet bodies. The lowercase + alphanumeric pass is
    deliberately lossy (drops punctuation, casing, fancy unicode) so a
    model paraphrasing 'TTL of 600s' as 'TTL: 600 s' still matches.
    """
    return tuple(
        t for t in _TOKEN_RE.findall(text.lower())
        if len(t) >= _MIN_TOKEN_LEN
    )


def _consecutive_token_overlap(
    needle: tuple[str, ...], haystack: tuple[str, ...],
) -> bool:
    """Return True if any window of >= _MIN_CONSECUTIVE_TOKEN_MATCH
    consecutive tokens from `needle` appears in `haystack` in order.

    Linear in len(needle) * len(haystack); both are bounded by note body
    cap (280 chars) and reply body cap (2000 chars), so this is ms-scale.
    """
    if len(needle) < _MIN_CONSECUTIVE_TOKEN_MATCH:
        return False
    if len(haystack) < _MIN_CONSECUTIVE_TOKEN_MATCH:
        return False
    span = _MIN_CONSECUTIVE_TOKEN_MATCH
    for start in range(len(needle) - span + 1):
        window = needle[start:start + span]
        # Slide window across haystack.
        for h_start in range(len(haystack) - span + 1):
            if haystack[h_start:h_start + span] == window:
                return True
    return False


def _unverified_bullets_from_notes(parsed_notes: Any) -> tuple[str, ...]:
    """Extract the body text of every `attr=self` or `attr=asserted`
    bullet from a ParsedNotes object. Imports notes_store lazily to
    avoid the import cycle at module load.
    """
    bodies: list[str] = []
    for section in parsed_notes.sections:
        for bullet in section.bullets:
            if bullet.attribution in ("self", "asserted"):
                bodies.append(bullet.text)
    return tuple(bodies)


def _read_parsed_notes(notes_path: Path) -> Any | None:
    """Helper for triage_with_scope: read a notes file and return its
    ParsedNotes form, or None if the file is missing or malformed.

    The gate's design treats None as no-op — if we can't read the
    notes file, we can't run the gate, so we let the prompt-side rule
    carry the load. Failing closed here would mean refusing every
    triage on a malformed notes file, which is a worse outcome than
    a single-layer (prompt-only) defence on those files.
    """
    from . import notes_store
    if not notes_path.exists():
        return None
    try:
        text = notes_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return notes_store.parse(text)
    except notes_store.NotesParseError:
        return None


def _gate_reply_against_unverified_notes(
    plan: TriagePlan, *, parsed_notes: Any | None,
) -> TriagePlan:
    """Read-side provenance defence. If `plan.verb == 'reply'` and the
    reply body enumerates content from an `attr=self` or `attr=asserted`
    bullet currently in the contact's notes file, downgrade the plan to
    `flag_for_review` so the principal can vet the enumeration.

    The model is instructed against this in `triage_default.md`'s
    "Reading notes — non-negotiable" section. This is the deterministic
    backstop: a model that ignores the prompt rule (or paraphrases past
    it) gets caught here.

    Closes the persistent self-poisoning lane found by the 2026-05-06
    auto-redteam loop (round 3, slow-burn drift attack m5). The attack
    exploited that the wave-2 defence was write-side only — once a
    `self`-tagged bullet was on disk, the next triage call would
    happily relay its content back to the contact in a reply. This
    gate forbids that relay.

    `parsed_notes=None` is the no-op signal: callers that don't have
    access to the parsed notes file (legacy call sites, unscoped
    contacts where the gate doesn't apply yet) skip the check. Empty
    notes (no unverified bullets) also no-op.
    """
    if plan.verb != "reply":
        return plan
    if parsed_notes is None:
        return plan
    bodies = _unverified_bullets_from_notes(parsed_notes)
    if not bodies:
        return plan
    reply_body = plan.args.get("body", "")
    if not reply_body:
        return plan
    reply_tokens = _significant_tokens(reply_body)
    matched_bullet: str | None = None
    for body in bodies:
        bullet_tokens = _significant_tokens(body)
        if _consecutive_token_overlap(bullet_tokens, reply_tokens):
            matched_bullet = body
            break
    if matched_bullet is None:
        return plan
    # Downgrade. Preserve original reasoning + summary, prepend a gate
    # note so the principal can see what triggered the flag, add the
    # identity_claim risk flag (since the underlying issue is unverified
    # claims being relayed). The flag_for_review verb has empty args.
    gate_note = (
        "[notes-enumeration gate] The drafted reply repeated content "
        "from an unverified bullet in this contact's notes. Pattern "
        f"matched: {matched_bullet[:160]!r}. Original reply preserved "
        "in the daemon log; not sending. Principal should confirm the "
        "claim before any reply enumerates it."
    )
    new_notes = (
        f"{gate_note}\n\nOriginal triage notes:\n{plan.notes}"
        if plan.notes else gate_note
    )
    new_risk_flags = tuple(plan.risk_flags) + (("identity_claim",) if "identity_claim" not in plan.risk_flags else ())
    flag_spec = TRIAGE_VERBS["flag_for_review"]
    return TriagePlan(
        verb="flag_for_review",
        tier=int(flag_spec["tier"]),
        args={},
        summary=plan.summary,
        reasoning=plan.reasoning,
        risk_flags=new_risk_flags,
        notes=new_notes[:_MAX_NOTES_LEN],
        raw_input_tokens=plan.raw_input_tokens,
        raw_output_tokens=plan.raw_output_tokens,
        note_proposals=plan.note_proposals,
    )


# ---- Top-level entry point -------------------------------------------------


async def triage_contact_mail(
    *,
    contact: Contact,
    sender: str,
    subject: str,
    body: str,
    structure: MessageStructure,
    config: ClaudeConfig,
    client: ClaudeClient,
    prompts_dir: Path,
) -> TriagePlan | TriageError:
    """Run one triage call. Returns a validated plan or a typed error.

    This function does no network I/O of its own: all SDK interaction
    goes through the injected `client`. Tests pass a FakeClaudeClient;
    production passes an AnthropicClient.

    `structure` is a daemon-derived fingerprint of the raw MIME message:
    presence of HTML alternative, attachment count, inline images, and
    so on. It feeds the `<message_structure>` block in the user message
    so the LLM can ground hidden-content suspicion in facts rather than
    speculation. Caller is responsible for building it from the fetched
    bytes; see InboxWatcher._extract_message_structure.
    """
    system = build_system_prompt(prompts_dir)
    user = build_user_message(
        contact=contact, sender=sender, subject=subject, body=body,
        structure=structure,
    )

    try:
        response = await client.call(
            model=config.default_model,
            system=system,
            user=user,
            tools=[build_draft_plan_tool(contact)],
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

    result = validate_plan_payload(tool_use.get("input", {}), contact=contact)
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
        note_proposals=result.note_proposals,
    )


# ---- Step 7b orchestrator --------------------------------------------------


def _build_decline_body(
    *,
    contact: Contact,
    scopes_registry: dict[str, str],
    reason_was_classifier_failure: bool,
    facets_registry: dict[str, str] | None = None,
    projects_registry: dict[str, str] | None = None,
) -> str:
    """Compose the polite decline body for an out-of-scope conversation.

    Templated, not LLM-generated, on purpose — the decline path runs
    on classifier failures too, and we don't want the daemon producing
    LLM-authored prose under those conditions. The body cites the
    contact's allowed scopes (using their human descriptions from the
    registry) so the contact knows what they can come back with.

    Two-axis path (Scope/sensitivity Part 1): when the contact uses
    facets+projects instead of legacy scopes, list both axes' allowed
    topics. Same template shape — only the topic list changes.
    """
    if contact.scopes:
        topic_lines = []
        for scope_name in contact.scopes:
            description = scopes_registry.get(scope_name, scope_name)
            topic_lines.append(f"  - {description}")
        topics = "\n".join(topic_lines)
        topic_block = (
            "I'm only set up to discuss the following topics on this channel:\n"
            f"\n{topics}\n"
        )
    elif contact.facets or contact.projects:
        # Two-axis contact: build topic list from both registries.
        topic_lines = []
        for facet_name in contact.facets:
            desc = (facets_registry or {}).get(facet_name, facet_name)
            topic_lines.append(f"  - {desc}")
        for project_name in contact.projects:
            desc = (projects_registry or {}).get(project_name, project_name)
            topic_lines.append(f"  - {desc}")
        topics = "\n".join(topic_lines) if topic_lines else (
            "  - (no topics configured)"
        )
        topic_block = (
            "I'm only set up to discuss the following topics on this channel:\n"
            f"\n{topics}\n"
        )
    else:
        # Defensive: orchestrator should never call this for unscoped
        # contacts, but if it does, fall back to a generic decline.
        topic_block = "This message falls outside what I can discuss right now."

    if reason_was_classifier_failure:
        # We don't want to expose internal failure modes to the contact;
        # the body shape is identical to the genuine out-of-scope case
        # so an attacker watching the daemon's behaviour can't infer
        # whether the classifier succeeded or fell over.
        pass

    greeting = f"Hi {contact.display_name or contact.contact_id},"
    return (
        f"{greeting}\n"
        "\n"
        "Thanks for getting in touch. "
        f"{topic_block}"
        "\n"
        "If you'd like to chat about something else, please reach me on "
        "a different channel and I'll get back to you when I can.\n"
    )


def _synth_out_of_scope_plan(
    *,
    contact: Contact,
    scopes_registry: dict[str, str],
    reason: str,
    detail: str,
    classifier_input_tokens: int,
    classifier_output_tokens: int,
    facets_registry: dict[str, str] | None = None,
    projects_registry: dict[str, str] | None = None,
) -> TriagePlan:
    """Construct the synthetic plan for an out-of-scope outcome.

    No LLM call goes into this; the body is templated and the metadata
    is built from the classifier's reason. Tokens are carried through
    so the cost backstop sees the classifier's spend.

    Two-axis contacts: pass `facets_registry` and `projects_registry`
    so the decline body lists the right topics. Legacy contacts
    leave them None.
    """
    body = _build_decline_body(
        contact=contact,
        scopes_registry=scopes_registry,
        reason_was_classifier_failure=(reason != "out_of_scope"),
        facets_registry=facets_registry,
        projects_registry=projects_registry,
    )
    if reason == "out_of_scope":
        summary = (
            "Message classified out of scope for this contact. "
            "Daemon proposes a polite decline."
        )
        reasoning = (
            "The pass-1 scope classifier judged the message's primary "
            "intent did not fit any of this contact's allowed scopes. "
            "The decline reply is templated, not LLM-generated."
        )
    else:
        summary = (
            "Scope classifier did not produce a result; defaulting to "
            "out-of-scope decline (fail closed)."
        )
        reasoning = (
            f"Classifier returned an error ({reason}). Per the daemon's "
            "fail-closed posture, the message is treated as out of "
            "scope and a templated decline is offered for principal "
            "approval."
        )
    return TriagePlan(
        verb="out_of_scope_decline",
        tier=TRIAGE_VERBS["out_of_scope_decline"]["tier"],
        args={"body": body},
        summary=summary,
        reasoning=reasoning,
        risk_flags=("off_topic",),
        notes=(detail or "")[:_MAX_NOTES_LEN],
        raw_input_tokens=classifier_input_tokens,
        raw_output_tokens=classifier_output_tokens,
    )


async def _triage_two_axis(
    *,
    contact: Contact,
    sender: str,
    subject: str,
    body: str,
    structure: MessageStructure,
    config: ClaudeConfig,
    client: ClaudeClient,
    prompts_dir: Path,
    notes_path: Path,
    facets_registry: dict[str, str],
    projects_registry: dict[str, str],
) -> TriagePlan | TriageError:
    """Two-axis triage path. Classifier returns (facets, project);
    notes are filtered through a ScopeContext that walks both axes.

    Failure modes mirror the legacy path:
      - SDK / parse errors: synthetic decline (fail closed).
      - Full out-of-scope (no facets, no in-scope project): synthetic
        decline.
      - Otherwise: triage with notes filtered to the matching context.
    """
    from . import notes_store
    from . import scope_classifier as sc_module

    try:
        safe_notes = notes_store.read_safe_notes(notes_path)
    except notes_store.NotesParseError:
        safe_notes = ""

    classification = await sc_module.classify_two_axis(
        contact=contact,
        sender=sender,
        subject=subject,
        body=body,
        facets_registry=facets_registry,
        projects_registry=projects_registry,
        safe_notes=safe_notes,
        config=config,
        client=client,
    )

    if isinstance(classification, sc_module.ClassifierError):
        return _synth_out_of_scope_plan(
            contact=contact,
            scopes_registry={},
            reason=classification.reason,
            detail=classification.detail,
            classifier_input_tokens=classification.raw_input_tokens,
            classifier_output_tokens=classification.raw_output_tokens,
            facets_registry=facets_registry,
            projects_registry=projects_registry,
        )

    if classification.is_full_out_of_scope():
        return _synth_out_of_scope_plan(
            contact=contact,
            scopes_registry={},
            reason="out_of_scope",
            detail="",
            classifier_input_tokens=classification.raw_input_tokens,
            classifier_output_tokens=classification.raw_output_tokens,
            facets_registry=facets_registry,
            projects_registry=projects_registry,
        )

    # In-scope (any axis matched): build ScopeContext and read notes.
    project_set: frozenset[str] = (
        frozenset({classification.project})
        if classification.project != sc_module.OUT_OF_SCOPE
        else frozenset()
    )
    ctx = notes_store.ScopeContext(
        facets=frozenset(classification.facets),
        projects=project_set,
    )

    try:
        scoped_notes = notes_store.read_notes_two_axis(notes_path, ctx)
    except notes_store.NotesParseError:
        scoped_notes = ""

    plan_or_error = await _triage_with_notes(
        contact=contact,
        sender=sender,
        subject=subject,
        body=body,
        structure=structure,
        notes=scoped_notes,
        config=config,
        client=client,
        prompts_dir=prompts_dir,
    )

    if isinstance(plan_or_error, TriageError):
        return plan_or_error

    # Step 7 wave 3a: read-side provenance gate. Same gate as the
    # legacy path; operates on the parsed notes file directly.
    parsed = _read_parsed_notes(notes_path)
    plan_or_error = _gate_reply_against_unverified_notes(
        plan_or_error, parsed_notes=parsed,
    )

    return TriagePlan(
        verb=plan_or_error.verb,
        tier=plan_or_error.tier,
        args=plan_or_error.args,
        summary=plan_or_error.summary,
        reasoning=plan_or_error.reasoning,
        risk_flags=plan_or_error.risk_flags,
        notes=plan_or_error.notes,
        raw_input_tokens=(
            plan_or_error.raw_input_tokens
            + classification.raw_input_tokens
        ),
        raw_output_tokens=(
            plan_or_error.raw_output_tokens
            + classification.raw_output_tokens
        ),
        note_proposals=plan_or_error.note_proposals,
    )


async def triage_with_scope(
    *,
    contact: Contact,
    sender: str,
    subject: str,
    body: str,
    structure: MessageStructure,
    config: ClaudeConfig,
    client: ClaudeClient,
    prompts_dir: Path,
    notes_dir: Path,
    scopes_registry: dict[str, str],
    facets_registry: dict[str, str] | None = None,
    projects_registry: dict[str, str] | None = None,
) -> TriagePlan | TriageError:
    """Step 7b orchestrator. Two-pass triage when the contact has scopes.

    Sequencing (per axis vocabulary the contact uses):

      - Legacy single-axis (`contact.scopes` non-empty): pass-1
        classifier + scope-filtered notes for triage. On out-of-scope
        OR classifier error, synthetic decline.
      - Two-axis (`contact.facets` or `contact.projects` non-empty):
        pass-1 two-axis classifier returning (facets, project). If
        the result is full out-of-scope, synthetic decline. Otherwise
        triage runs with notes filtered through the matching
        `ScopeContext`.
      - Unrestricted (all axes empty): skip classification; full
        notes; existing behaviour.

    Two-axis vs legacy is mutually exclusive (config.load enforces
    this), so dispatch is unambiguous.

    Cost: pass-1 classifier tokens always counted; pass-2 triage
    tokens added when the in-scope path is taken. Caller's cost
    backstop should evaluate against the combined sum on the returned
    plan's `raw_input_tokens` + `raw_output_tokens` fields.

    The notes module is imported lazily so callers that don't go
    through this function (the existing `triage_contact_mail` direct
    path) don't pay the import cost. Same reason scope_classifier is
    a local import.
    """
    from . import notes_store
    from . import scope_classifier as sc_module

    notes_path = notes_dir / f"{contact.contact_id}.md"

    # Two-axis path: contact has facets and/or projects (and no legacy
    # `scopes` — config.load enforces mutual exclusion). Goes through
    # `classify_two_axis` and `read_notes_two_axis` with a ScopeContext.
    if contact.facets or contact.projects:
        return await _triage_two_axis(
            contact=contact, sender=sender, subject=subject, body=body,
            structure=structure, config=config, client=client,
            prompts_dir=prompts_dir, notes_path=notes_path,
            facets_registry=facets_registry or {},
            projects_registry=projects_registry or {},
        )

    # Empty scopes path: skip classification, read full notes, pass
    # through to triage_contact_mail. Existing behaviour preserved.
    if not contact.scopes:
        try:
            full_notes = notes_store.read_notes(notes_path, active_scope=None)
        except notes_store.NotesParseError:
            # Malformed notes file: fail closed on the notes block
            # (pass empty notes through) rather than failing the whole
            # triage. The principal sees the contact's reply with no
            # notes context, the daemon's error log surfaces the
            # parse failure for them to fix.
            full_notes = ""
        plan_or_error = await _triage_with_notes(
            contact=contact,
            sender=sender,
            subject=subject,
            body=body,
            structure=structure,
            notes=full_notes,
            config=config,
            client=client,
            prompts_dir=prompts_dir,
        )
        if isinstance(plan_or_error, TriageError):
            return plan_or_error
        # Step 7 wave 3a: read-side provenance gate. Re-parse the notes
        # file directly so the gate sees attribution metadata that the
        # rendered text strips. Failures here just skip the gate (it's
        # a defence-in-depth layer; the prompt-side rule is the
        # primary defence).
        parsed = _read_parsed_notes(notes_path)
        return _gate_reply_against_unverified_notes(
            plan_or_error, parsed_notes=parsed,
        )

    # Scoped path: pass 1 classifier with safe-only notes.
    try:
        safe_notes = notes_store.read_safe_notes(notes_path)
    except notes_store.NotesParseError:
        # Same fail-closed reasoning as above: empty notes, classifier
        # still runs (it has the metadata + message body to work on).
        safe_notes = ""

    classification = await sc_module.classify_scope(
        contact=contact,
        sender=sender,
        subject=subject,
        body=body,
        scopes_registry=scopes_registry,
        safe_notes=safe_notes,
        config=config,
        client=client,
    )

    if isinstance(classification, sc_module.ClassifierError):
        # Fail closed: synthetic decline plan, classifier tokens
        # carried through.
        return _synth_out_of_scope_plan(
            contact=contact,
            scopes_registry=scopes_registry,
            reason=classification.reason,
            detail=classification.detail,
            classifier_input_tokens=classification.raw_input_tokens,
            classifier_output_tokens=classification.raw_output_tokens,
        )

    if classification.scope == sc_module.OUT_OF_SCOPE:
        return _synth_out_of_scope_plan(
            contact=contact,
            scopes_registry=scopes_registry,
            reason="out_of_scope",
            detail="",
            classifier_input_tokens=classification.raw_input_tokens,
            classifier_output_tokens=classification.raw_output_tokens,
        )

    # In-scope: pass 2 with scope-filtered notes.
    try:
        scoped_notes = notes_store.read_notes(
            notes_path, active_scope=classification.scope,
        )
    except notes_store.NotesParseError:
        scoped_notes = ""

    plan_or_error = await _triage_with_notes(
        contact=contact,
        sender=sender,
        subject=subject,
        body=body,
        structure=structure,
        notes=scoped_notes,
        config=config,
        client=client,
        prompts_dir=prompts_dir,
    )

    if isinstance(plan_or_error, TriageError):
        return plan_or_error

    # Step 7 wave 3a: read-side provenance gate. Run before the token
    # stitch so the returned plan reflects the gate's downgrade if
    # any. Parse the notes file directly so the gate sees attribution
    # metadata that the rendered text strips.
    parsed = _read_parsed_notes(notes_path)
    plan_or_error = _gate_reply_against_unverified_notes(
        plan_or_error, parsed_notes=parsed,
    )

    # Sum classifier + triage token usage so the cost backstop sees the
    # combined budget for this message.
    return TriagePlan(
        verb=plan_or_error.verb,
        tier=plan_or_error.tier,
        args=plan_or_error.args,
        summary=plan_or_error.summary,
        reasoning=plan_or_error.reasoning,
        risk_flags=plan_or_error.risk_flags,
        notes=plan_or_error.notes,
        raw_input_tokens=(
            plan_or_error.raw_input_tokens
            + classification.raw_input_tokens
        ),
        raw_output_tokens=(
            plan_or_error.raw_output_tokens
            + classification.raw_output_tokens
        ),
        note_proposals=plan_or_error.note_proposals,
    )


async def _triage_with_notes(
    *,
    contact: Contact,
    sender: str,
    subject: str,
    body: str,
    structure: MessageStructure,
    notes: str,
    config: ClaudeConfig,
    client: ClaudeClient,
    prompts_dir: Path,
) -> TriagePlan | TriageError:
    """Inner shape of triage that takes pre-rendered notes.

    Mirrors triage_contact_mail but with the notes string injected into
    the user message. Kept private; the public entry points are
    `triage_contact_mail` (no notes — preserves the pre-Step-7b call
    surface) and `triage_with_scope` (orchestrates pass 1 + 2).
    """
    system = build_system_prompt(prompts_dir)
    user = build_user_message(
        contact=contact, sender=sender, subject=subject, body=body,
        structure=structure, notes=notes,
    )

    try:
        response = await client.call(
            model=config.default_model,
            system=system,
            user=user,
            tools=[build_draft_plan_tool(contact)],
            max_tokens=config.per_invocation_max_input_tokens,
        )
    except Exception as e:
        return TriageError(reason="sdk_error", detail=str(e))

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

    result = validate_plan_payload(tool_use.get("input", {}), contact=contact)
    if isinstance(result, TriageError):
        return result

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
        note_proposals=result.note_proposals,
    )
