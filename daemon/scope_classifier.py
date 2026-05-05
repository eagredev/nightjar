"""Step 7b pass-1: scope classifier.

When a contact has non-empty `scopes`, an inbound message must first
be classified into one of those scopes (or `out_of_scope`) before
triage proper runs. This module is that classification step.

Why two passes:

  Pass 1 (this module) sees only what's safe to show *any* scope:
  contact metadata, the inbound message, the registry of allowed
  scopes with descriptions, and unscoped+wildcard notes. It does NOT
  see scoped notes, because the whole point of scope tagging is to
  withhold sensitive content from out-of-scope conversations.

  Pass 2 (triage.py) runs on a successful classify-as-in-scope.
  Triage's prompt then includes scope-filtered notes.

  On classify-as-out-of-scope, triage doesn't run at all — the watcher
  composes a polite decline directly from the [scopes] registry. No
  LLM-generated reply for an out-of-scope conversation, by design.

Fail-closed posture: any classifier error (SDK, parse, validation,
unknown-scope output) returns a `ClassifierError`. The watcher's
policy is to treat that as out_of_scope for contacts with non-empty
scopes — fail closed. Contacts with empty scopes skip this module
entirely (their behaviour is unchanged from pre-Step-7b).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ClaudeConfig, Contact
from .triage import ClaudeClient, ClaudeResponse


# ---- Result types ---------------------------------------------------------


@dataclass(frozen=True)
class ClassifierResult:
    """A successfully classified inbound message."""
    scope: str  # the scope name, OR the literal "out_of_scope"
    raw_input_tokens: int
    raw_output_tokens: int


@dataclass(frozen=True)
class ClassifierError:
    """Classifier failed. Caller treats as out_of_scope (fail closed)."""
    reason: str
    detail: str = ""
    raw_input_tokens: int = 0
    raw_output_tokens: int = 0


# Sentinel scope name returned when the classifier judges the message
# falls outside every allowed scope. The literal string is used in
# the structured output schema so the model can produce it.
OUT_OF_SCOPE = "out_of_scope"


# ---- Tool schema (one tool, one input field) ------------------------------


def _classify_tool_schema(allowed_scopes: tuple[str, ...]) -> dict[str, Any]:
    """Build the tool definition for the classifier call.

    The single allowed-values enum is the strongest constraint we can
    put on the model: anthropic's tool schema validation will refuse
    a tool_use whose input doesn't match the enum, so we get a typed
    error before our own validator runs."""
    enum_values = list(allowed_scopes) + [OUT_OF_SCOPE]
    return {
        "name": "classify_scope",
        "description": (
            "Classify the inbound message's primary intent into "
            "exactly one of the contact's allowed scopes, or "
            f"{OUT_OF_SCOPE!r} if the message does not fit any."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": enum_values,
                    "description": (
                        "The single scope name this message belongs in. "
                        "Use 'out_of_scope' if no scope fits."
                    ),
                },
            },
            "required": ["scope"],
            "additionalProperties": False,
        },
    }


# ---- Prompts --------------------------------------------------------------


_CLASSIFIER_SYSTEM_PROMPT = """You classify inbound emails by topical scope.

You will be given:
  - the contact metadata (who's writing)
  - the inbound message (sender, subject, body)
  - a registry of allowed scopes for this contact, each with a description
  - any unscoped or wildcard-visible notes about the contact

Your job is to pick exactly one scope from the allowed list, OR pick
"out_of_scope" if the message's *primary intent* does not fit any
scope. Incidental pleasantries ("how was your weekend, btw") are not
the primary intent and should not push a classification out of scope
on their own — only the main thrust of the message matters.

Be conservative on the boundary. If a message could plausibly fit a
scope but is mostly drifting outside, pick out_of_scope. The
out-of-scope path produces a polite decline; that is recoverable. A
misclassified in-scope message can leak content through the reply
path, which is harder to reverse.

You MUST call the classify_scope tool exactly once with your choice.
Never produce text. Never call any other tool.
"""


def _format_scopes_block(
    contact: Contact, registry: dict[str, str],
) -> str:
    lines = []
    for scope_name in contact.scopes:
        description = registry.get(scope_name, "(no description)")
        lines.append(f"  - {scope_name}: {description}")
    return "\n".join(lines)


def build_classifier_user_message(
    *,
    contact: Contact,
    sender: str,
    subject: str,
    body: str,
    scopes_registry: dict[str, str],
    safe_notes: str,
) -> str:
    """Render the user message for a classifier call.

    `safe_notes` is the unscoped+wildcard notes content, already
    rendered (caller used notes_store.filtered_text or read_notes
    with active_scope=None and post-filtered to drop scoped sections,
    OR — easier — passed only the wildcard subset). Either way, this
    function does not re-parse it; whatever the caller passes ends up
    verbatim in the prompt.

    Block delimiters mirror the triage user-message format. Untrusted
    fields are scrubbed for the same close-tag-injection reason."""
    from .triage import _strip_block_delimiters

    safe_sender = _strip_block_delimiters(sender)
    safe_subject = _strip_block_delimiters(subject)
    safe_body = _strip_block_delimiters(body)
    scope_lines = _format_scopes_block(contact, scopes_registry)
    notes_block = safe_notes.strip() or "(no shareable notes)"
    return (
        "<contact_metadata>\n"
        f"contact_id: {contact.contact_id}\n"
        f"display_name: {contact.display_name}\n"
        f"relationship: {contact.relationship}\n"
        "</contact_metadata>\n"
        "\n"
        "<allowed_scopes>\n"
        f"{scope_lines}\n"
        "</allowed_scopes>\n"
        "\n"
        "<safe_notes>\n"
        f"{notes_block}\n"
        "</safe_notes>\n"
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


def validate_classifier_payload(
    payload: dict[str, Any], *, allowed_scopes: tuple[str, ...],
) -> str | ClassifierError:
    """Check the tool_use input. Returns the validated scope string or
    a ClassifierError. The Anthropic enum validation should catch
    most violations upstream, but we belt-and-brace here in case the
    SDK forwards a malformed payload anyway."""
    if not isinstance(payload, dict):
        return ClassifierError(
            reason="invalid_payload",
            detail=f"expected dict, got {type(payload).__name__}",
        )
    scope = payload.get("scope")
    if not isinstance(scope, str):
        return ClassifierError(
            reason="missing_scope",
            detail=f"payload had no string 'scope' field: {payload!r}",
        )
    if scope == OUT_OF_SCOPE:
        return scope
    if scope not in allowed_scopes:
        return ClassifierError(
            reason="unknown_scope",
            detail=(
                f"model returned {scope!r} but allowed values are "
                f"{list(allowed_scopes) + [OUT_OF_SCOPE]!r}"
            ),
        )
    return scope


# ---- Entry point -----------------------------------------------------------


async def classify_scope(
    *,
    contact: Contact,
    sender: str,
    subject: str,
    body: str,
    scopes_registry: dict[str, str],
    safe_notes: str,
    config: ClaudeConfig,
    client: ClaudeClient,
) -> ClassifierResult | ClassifierError:
    """Run pass-1 scope classification.

    Pre-conditions:
      - contact.scopes is non-empty (caller must skip this module
        entirely for unscoped contacts).
      - Every scope in contact.scopes appears in scopes_registry
        (config.load enforces this cross-check).

    `safe_notes` MUST contain only unscoped + wildcard content. The
    caller is responsible for the filtering — passing scoped content
    here would defeat the whole point of two-pass.
    """
    if not contact.scopes:
        # Programmer error to call this on an unscoped contact.
        return ClassifierError(
            reason="empty_scopes",
            detail=(
                "classify_scope was called on a contact with no "
                "scopes; the caller should skip pass-1 in that case."
            ),
        )

    user = build_classifier_user_message(
        contact=contact,
        sender=sender,
        subject=subject,
        body=body,
        scopes_registry=scopes_registry,
        safe_notes=safe_notes,
    )
    tool = _classify_tool_schema(contact.scopes)

    try:
        response = await client.call(
            model=config.scope_classifier_model,
            system=_CLASSIFIER_SYSTEM_PROMPT,
            user=user,
            tools=[tool],
            # The classifier output is one tiny field; cap small to
            # bound cost. 256 leaves plenty of room.
            max_tokens=256,
        )
    except Exception as e:
        return ClassifierError(reason="sdk_error", detail=str(e))

    if not response.tool_uses:
        return ClassifierError(
            reason="no_tool_call",
            detail=(
                f"stop_reason={response.stop_reason!r}, "
                f"text_blocks={len(response.text_blocks)}"
            ),
            raw_input_tokens=response.input_tokens,
            raw_output_tokens=response.output_tokens,
        )
    if len(response.tool_uses) > 1:
        return ClassifierError(
            reason="multiple_tool_calls",
            detail=f"got {len(response.tool_uses)}",
            raw_input_tokens=response.input_tokens,
            raw_output_tokens=response.output_tokens,
        )

    tool_use = response.tool_uses[0]
    if tool_use.get("name") != "classify_scope":
        return ClassifierError(
            reason="unexpected_tool",
            detail=str(tool_use.get("name")),
            raw_input_tokens=response.input_tokens,
            raw_output_tokens=response.output_tokens,
        )

    result = validate_classifier_payload(
        tool_use.get("input", {}), allowed_scopes=contact.scopes,
    )
    if isinstance(result, ClassifierError):
        # Stitch usage onto the validation error.
        return ClassifierError(
            reason=result.reason,
            detail=result.detail,
            raw_input_tokens=response.input_tokens,
            raw_output_tokens=response.output_tokens,
        )

    return ClassifierResult(
        scope=result,
        raw_input_tokens=response.input_tokens,
        raw_output_tokens=response.output_tokens,
    )
