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


# ===========================================================================
# Scope/sensitivity Part 1: two-axis classifier.
#
# When a contact uses the new (facets, projects) vocabulary, pass-1
# answers TWO questions instead of one:
#
#   1. Which facets (0-N from the contact's facet list) does this
#      message touch? Universal axes can stack: a message can be both
#      `calendar` and `personal-life`.
#   2. Which project (0-1 from the contact's project list, or
#      out_of_scope) does it classify into? Specific contexts — by
#      design at most one applies per message; out_of_scope means none.
#
# A successful two-axis classification routes to triage with both axes
# active. The notes-block assembly walks both axes and includes any
# bullet whose facet OR project tags match. Sub-projects use the
# bidirectional visibility rules in `daemon.config.project_visibility`.
#
# Out-of-scope semantics:
#
#   - facets empty AND project=out_of_scope: full out-of-scope path.
#     Synthesise a polite decline with no triage call. Same posture as
#     legacy out_of_scope — fail closed.
#   - facets non-empty (regardless of project): route to triage. The
#     contact has SOME relevant axis; triage runs with the matched
#     axes only. This matches the design doc's "single message can be
#     'scheduling for aurora music work'" framing.
#   - facets empty BUT project in-scope: route to triage with project
#     only. (Common case: a message about a specific shared project
#     that doesn't touch any universal axis.)
# ===========================================================================


@dataclass(frozen=True)
class TwoAxisResult:
    """A successfully classified two-axis inbound message.

    `facets` is a tuple of zero or more facet names from the contact's
    facet list. `project` is either a project name from the contact's
    project list OR the OUT_OF_SCOPE sentinel.
    """
    facets: tuple[str, ...]
    project: str  # project name OR OUT_OF_SCOPE
    raw_input_tokens: int
    raw_output_tokens: int

    def is_full_out_of_scope(self) -> bool:
        """True when no axis matched. Caller should synthesise a
        decline plan instead of routing to triage."""
        return not self.facets and self.project == OUT_OF_SCOPE


def _two_axis_tool_schema(
    allowed_facets: tuple[str, ...],
    allowed_projects: tuple[str, ...],
) -> dict[str, Any]:
    """Tool definition for the two-axis classifier call.

    Two enum-constrained input fields. Anthropic's tool-schema
    validation rejects out-of-enum values upstream; we belt-and-brace
    in the validator.

    The `facets` array enum is constrained per-element. An empty array
    is allowed and means "no universal axis applies." `project` is a
    single string; the enum includes the OUT_OF_SCOPE sentinel so the
    model can pick it cleanly when no project fits.
    """
    facet_enum = list(allowed_facets)
    project_enum = list(allowed_projects) + [OUT_OF_SCOPE]
    properties: dict[str, Any] = {
        "project": {
            "type": "string",
            "enum": project_enum,
            "description": (
                "The single project context this message classifies "
                f"into, or {OUT_OF_SCOPE!r} if no project fits. At "
                "most one project applies per message."
            ),
        },
    }
    # If the contact has no facets, the schema field is still present
    # but with an empty enum — the model can only return []. This is
    # the correct shape: a contact may have projects but no facets, or
    # vice versa.
    if facet_enum:
        properties["facets"] = {
            "type": "array",
            "items": {"type": "string", "enum": facet_enum},
            "description": (
                "Zero or more universal axes this message touches. "
                "Multiple may apply (e.g. a scheduling message about "
                "music work touches both `calendar` and `music-tech`)."
            ),
        }
    else:
        properties["facets"] = {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 0,
            "description": (
                "This contact has no facets configured; return []."
            ),
        }
    return {
        "name": "classify_two_axis",
        "description": (
            "Classify the inbound message along TWO axes: which "
            "universal facets (0-N) does it touch, and which "
            "specific project (0-1) does it classify into."
        ),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": ["facets", "project"],
            "additionalProperties": False,
        },
    }


_TWO_AXIS_SYSTEM_PROMPT = """You classify inbound emails along two axes.

You will be given:
  - the contact metadata (who's writing)
  - the inbound message (sender, subject, body)
  - a registry of universal FACETS (calendar, communication-style,
    finance, etc.) the contact has access to, each with a description
  - a registry of specific PROJECTS (or sub-projects via dot-notation)
    the contact has access to, each with a description
  - any unscoped or wildcard-visible notes about the contact

Your job is to answer two questions:

  1. FACETS — which universal axes does this message touch? Zero or
     more facets may apply. A message asking 'when can we meet to
     review the demo' touches `calendar` (scheduling) and possibly
     a project like `aurora.music` (the demo). A message that's pure
     small-talk touches no facet — return [].

  2. PROJECT — which single specific context does this message
     classify into? Pick exactly one project name from the contact's
     project list, OR pick "out_of_scope" if no project fits. At most
     one project applies per message; if a message could fit two
     projects, pick the one most central to the message.

Boundary discipline:

  - Incidental pleasantries do not change the classification. Only
    the message's primary intent counts.
  - For sub-projects (dot-notation like `aurora.music`): pick the most
    specific level that fits. Don't pick `aurora` if the message is
    clearly about `aurora.music` and the contact has access to both.
  - Be conservative on the project boundary. If a message could
    plausibly fit a project but is mostly drifting, pick out_of_scope
    and let facets carry whatever universal context applies.

You MUST call the classify_two_axis tool exactly once. Never produce
text. Never call any other tool.
"""


def _format_two_axis_blocks(
    contact: Contact,
    facets_registry: dict[str, str],
    projects_registry: dict[str, str],
) -> tuple[str, str]:
    """Render the (facets, projects) blocks for the user message."""
    facet_lines = []
    for name in contact.facets:
        desc = facets_registry.get(name, "(no description)")
        facet_lines.append(f"  - {name}: {desc}")
    if not facet_lines:
        facet_block = "  (this contact has no facets configured)"
    else:
        facet_block = "\n".join(facet_lines)

    project_lines = []
    for name in contact.projects:
        desc = projects_registry.get(name, "(no description)")
        project_lines.append(f"  - {name}: {desc}")
    if not project_lines:
        project_block = "  (this contact has no projects configured)"
    else:
        project_block = "\n".join(project_lines)

    return facet_block, project_block


def build_two_axis_user_message(
    *,
    contact: Contact,
    sender: str,
    subject: str,
    body: str,
    facets_registry: dict[str, str],
    projects_registry: dict[str, str],
    safe_notes: str,
) -> str:
    """Render the user message for a two-axis classifier call.

    Same close-tag-injection scrubbing as the legacy classifier. Both
    registry blocks appear; the model picks 0+ facets and exactly one
    project (or out_of_scope).
    """
    from .triage import _strip_block_delimiters

    safe_sender = _strip_block_delimiters(sender)
    safe_subject = _strip_block_delimiters(subject)
    safe_body = _strip_block_delimiters(body)
    facet_block, project_block = _format_two_axis_blocks(
        contact, facets_registry, projects_registry,
    )
    notes_block = safe_notes.strip() or "(no shareable notes)"
    return (
        "<contact_metadata>\n"
        f"contact_id: {contact.contact_id}\n"
        f"display_name: {contact.display_name}\n"
        f"relationship: {contact.relationship}\n"
        "</contact_metadata>\n"
        "\n"
        "<allowed_facets>\n"
        f"{facet_block}\n"
        "</allowed_facets>\n"
        "\n"
        "<allowed_projects>\n"
        f"{project_block}\n"
        "</allowed_projects>\n"
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


def validate_two_axis_payload(
    payload: dict[str, Any],
    *,
    allowed_facets: tuple[str, ...],
    allowed_projects: tuple[str, ...],
) -> tuple[tuple[str, ...], str] | ClassifierError:
    """Validate the tool_use input. Returns (facets, project) tuple
    on success, or a ClassifierError. Anthropic's enum validation
    catches most violations upstream; this is the belt-and-brace path
    for malformed SDK payloads or pathological model output."""
    if not isinstance(payload, dict):
        return ClassifierError(
            reason="invalid_payload",
            detail=f"expected dict, got {type(payload).__name__}",
        )

    raw_facets = payload.get("facets")
    if not isinstance(raw_facets, list):
        return ClassifierError(
            reason="missing_facets",
            detail=(
                f"payload had no list 'facets' field: {payload!r}"
            ),
        )
    facets: list[str] = []
    seen: set[str] = set()
    allowed_facets_set = set(allowed_facets)
    for item in raw_facets:
        if not isinstance(item, str):
            return ClassifierError(
                reason="non_string_facet",
                detail=f"facets contained non-string: {item!r}",
            )
        if item not in allowed_facets_set:
            return ClassifierError(
                reason="unknown_facet",
                detail=(
                    f"model returned facet {item!r} but allowed values "
                    f"are {list(allowed_facets)!r}"
                ),
            )
        if item in seen:
            # De-duplicate silently — the model occasionally repeats.
            continue
        seen.add(item)
        facets.append(item)

    project = payload.get("project")
    if not isinstance(project, str):
        return ClassifierError(
            reason="missing_project",
            detail=f"payload had no string 'project' field: {payload!r}",
        )
    if project != OUT_OF_SCOPE and project not in allowed_projects:
        return ClassifierError(
            reason="unknown_project",
            detail=(
                f"model returned project {project!r} but allowed values "
                f"are {list(allowed_projects) + [OUT_OF_SCOPE]!r}"
            ),
        )

    return tuple(facets), project


async def classify_two_axis(
    *,
    contact: Contact,
    sender: str,
    subject: str,
    body: str,
    facets_registry: dict[str, str],
    projects_registry: dict[str, str],
    safe_notes: str,
    config: ClaudeConfig,
    client: ClaudeClient,
) -> TwoAxisResult | ClassifierError:
    """Run pass-1 two-axis classification.

    Pre-conditions:
      - contact uses new vocabulary: contact.facets OR contact.projects
        is non-empty (caller skips this module otherwise).
      - Every facet in contact.facets exists in facets_registry; every
        project in contact.projects exists in projects_registry.
        (config.load enforces both cross-checks.)

    `safe_notes` MUST contain only unscoped + wildcard content. Same
    constraint as the legacy classifier: passing scoped content here
    defeats the two-pass safety property.
    """
    if not contact.facets and not contact.projects:
        return ClassifierError(
            reason="empty_axes",
            detail=(
                "classify_two_axis was called on a contact with no "
                "facets and no projects; the caller should skip "
                "pass-1 in that case."
            ),
        )

    user = build_two_axis_user_message(
        contact=contact,
        sender=sender,
        subject=subject,
        body=body,
        facets_registry=facets_registry,
        projects_registry=projects_registry,
        safe_notes=safe_notes,
    )
    tool = _two_axis_tool_schema(contact.facets, contact.projects)

    try:
        response = await client.call(
            model=config.scope_classifier_model,
            system=_TWO_AXIS_SYSTEM_PROMPT,
            user=user,
            tools=[tool],
            # Two enum fields cap output tightly; 256 is plenty.
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
    if tool_use.get("name") != "classify_two_axis":
        return ClassifierError(
            reason="unexpected_tool",
            detail=str(tool_use.get("name")),
            raw_input_tokens=response.input_tokens,
            raw_output_tokens=response.output_tokens,
        )

    validated = validate_two_axis_payload(
        tool_use.get("input", {}),
        allowed_facets=contact.facets,
        allowed_projects=contact.projects,
    )
    if isinstance(validated, ClassifierError):
        return ClassifierError(
            reason=validated.reason,
            detail=validated.detail,
            raw_input_tokens=response.input_tokens,
            raw_output_tokens=response.output_tokens,
        )

    facets, project = validated
    return TwoAxisResult(
        facets=facets,
        project=project,
        raw_input_tokens=response.input_tokens,
        raw_output_tokens=response.output_tokens,
    )
