"""Tests for triage_with_scope — Step 7b two-pass orchestrator.

Covers the empty-scopes pass-through path, in-scope two-pass, out-of-scope
synthetic plan, classifier-error fail-closed path, and combined cost
accounting.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daemon.config import ClaudeConfig, Contact
from daemon.triage import (
    ClaudeResponse,
    MessageStructure,
    TriagePlan,
    triage_with_scope,
)


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


# ---- Test fixtures --------------------------------------------------------


@dataclass
class _CallRecord:
    model: str
    user: str
    tools: list[dict[str, Any]]


@dataclass
class FakeClaudeClient:
    """Returns a queue of canned responses, recording each call. Used to
    simulate two-pass: first response = classifier, second = triage."""
    responses: list[ClaudeResponse] = field(default_factory=list)
    calls: list[_CallRecord] = field(default_factory=list)

    async def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ClaudeResponse:
        self.calls.append(_CallRecord(model=model, user=user, tools=tools))
        if not self.responses:
            raise AssertionError("FakeClaudeClient out of canned responses")
        return self.responses.pop(0)


def _classifier_response(scope: str, *, in_tokens: int = 600, out_tokens: int = 8) -> ClaudeResponse:
    return ClaudeResponse(
        tool_uses=({"name": "classify_scope", "input": {"scope": scope}},),
        text_blocks=(),
        stop_reason="tool_use",
        input_tokens=in_tokens,
        output_tokens=out_tokens,
    )


def _triage_response(
    *,
    verb: str = "reply",
    body: str = "Sure, sounds good.",
    in_tokens: int = 1800,
    out_tokens: int = 120,
) -> ClaudeResponse:
    payload = {
        "summary": "test summary",
        "verb": verb,
        "args": {"body": body} if verb == "reply" else {},
        "reasoning": "test reasoning",
        "risk_flags": [],
    }
    return ClaudeResponse(
        tool_uses=({"name": "draft_plan", "input": payload},),
        text_blocks=(),
        stop_reason="tool_use",
        input_tokens=in_tokens,
        output_tokens=out_tokens,
    )


def _make_contact(scopes: tuple[str, ...] = ()) -> Contact:
    return Contact(
        contact_id="fraser",
        addresses=("fraser@example.com",),
        display_name="Fraser",
        relationship="collaborator",
        daily_limit=3,
        is_principal=False,
        inboxes=("nightjar",),
        scopes=scopes,
    )


def _make_config() -> ClaudeConfig:
    return ClaudeConfig(
        api_key="sk-ant-test-1234567890abcdef1234567890abcdef1234567890abcdef",
        default_model="claude-haiku-4-5",
        scope_classifier_model="claude-haiku-4-5",
    )


def _structure() -> MessageStructure:
    return MessageStructure(
        has_html_alternative=False,
        attachment_count=0,
        attachment_names=(),
        inline_image_count=0,
        plain_size_bytes=120,
        html_size_bytes=0,
        body_truncated_in_prompt=False,
    )


_REGISTRY = {
    "aurora": "the Aurora redesign work",
    "music-tech": "music production and chiptune workflows",
}


def _run(coro):
    return asyncio.run(coro)


# ---- Empty-scopes pass-through -------------------------------------------


def test_unscoped_contact_skips_classifier(tmp_path: Path) -> None:
    """Contact with empty scopes runs single-pass triage (today's
    behaviour). Classifier is not called."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_contact(scopes=())
    client = FakeClaudeClient(responses=[_triage_response()])

    result = _run(triage_with_scope(
        contact=contact,
        sender="fraser@example.com",
        subject="hi",
        body="quick question",
        structure=_structure(),
        config=_make_config(),
        triage_client=client,
        classifier_client=client,
        prompts_dir=PROMPTS_DIR,
        notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.verb == "reply"
    # Exactly one call was made (the triage call), no classifier.
    assert len(client.calls) == 1
    # And the call used the triage tool, not the classifier tool.
    assert client.calls[0].tools[0]["name"] == "draft_plan"


def test_unscoped_contact_includes_full_notes(tmp_path: Path) -> None:
    """Empty scopes → full notes injected (active_scope=None)."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    notes_path = notes_dir / "fraser.md"
    notes_path.write_text(
        "---\ncontact_id: fraser\n---\n\n## General\n\n- Replies fast.\n",
        encoding="utf-8",
    )
    contact = _make_contact(scopes=())
    client = FakeClaudeClient(responses=[_triage_response()])

    _run(triage_with_scope(
        contact=contact,
        sender="fraser@example.com",
        subject="hi",
        body="quick question",
        structure=_structure(),
        config=_make_config(),
        triage_client=client,
        classifier_client=client,
        prompts_dir=PROMPTS_DIR,
        notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    triage_call = client.calls[0]
    assert "Replies fast" in triage_call.user


def test_unscoped_contact_no_notes_file_yields_empty_block(tmp_path: Path) -> None:
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_contact(scopes=())
    client = FakeClaudeClient(responses=[_triage_response()])

    _run(triage_with_scope(
        contact=contact,
        sender="fraser@example.com",
        subject="hi",
        body="b",
        structure=_structure(),
        config=_make_config(),
        triage_client=client,
        classifier_client=client,
        prompts_dir=PROMPTS_DIR,
        notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    # The notes block should still be present (empty), and no notes
    # content should leak in.
    triage_call = client.calls[0]
    assert "<notes>\n\n</notes>" in triage_call.user


# ---- In-scope two-pass ----------------------------------------------------


def test_in_scope_runs_classifier_then_triage(tmp_path: Path) -> None:
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_contact(scopes=("aurora", "music-tech"))
    client = FakeClaudeClient(responses=[
        _classifier_response("aurora"),
        _triage_response(),
    ])
    result = _run(triage_with_scope(
        contact=contact,
        sender="fraser@example.com",
        subject="track 3",
        body="here's the mix",
        structure=_structure(),
        config=_make_config(),
        triage_client=client,
        classifier_client=client,
        prompts_dir=PROMPTS_DIR,
        notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.verb == "reply"
    assert len(client.calls) == 2
    assert client.calls[0].tools[0]["name"] == "classify_scope"
    assert client.calls[1].tools[0]["name"] == "draft_plan"


def test_in_scope_passes_safe_notes_to_classifier(tmp_path: Path) -> None:
    """Pass-1 sees only safe notes; scoped content stays out."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    notes_path = notes_dir / "fraser.md"
    notes_path.write_text(
        "---\ncontact_id: fraser\n---\n\n"
        "## Aurora [scopes: aurora]\n\n"
        "- Track 3 deadline 2026-05-15.\n"
        "\n"
        "## General\n\n"
        "- Uses British English. [scopes: *]\n",
        encoding="utf-8",
    )
    contact = _make_contact(scopes=("aurora",))
    client = FakeClaudeClient(responses=[
        _classifier_response("aurora"),
        _triage_response(),
    ])
    _run(triage_with_scope(
        contact=contact,
        sender="fraser@example.com",
        subject="s",
        body="b",
        structure=_structure(),
        config=_make_config(),
        triage_client=client,
        classifier_client=client,
        prompts_dir=PROMPTS_DIR,
        notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    classifier_call = client.calls[0]
    # Safe content reaches the classifier.
    assert "British English" in classifier_call.user
    # Aurora-scoped content does NOT reach the classifier.
    assert "Track 3 deadline" not in classifier_call.user


def test_in_scope_passes_scope_filtered_notes_to_triage(tmp_path: Path) -> None:
    """Pass-2 sees scope-filtered notes for the chosen scope, not the
    full file."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    notes_path = notes_dir / "fraser.md"
    notes_path.write_text(
        "---\ncontact_id: fraser\n---\n\n"
        "## Aurora [scopes: aurora]\n\n"
        "- Aurora-only content.\n"
        "\n"
        "## Personal [scopes: personal]\n\n"
        "- Personal content.\n",
        encoding="utf-8",
    )
    contact = _make_contact(scopes=("aurora", "personal"))
    client = FakeClaudeClient(responses=[
        _classifier_response("aurora"),
        _triage_response(),
    ])
    _run(triage_with_scope(
        contact=contact,
        sender="fraser@example.com",
        subject="s",
        body="b",
        structure=_structure(),
        config=_make_config(),
        triage_client=client,
        classifier_client=client,
        prompts_dir=PROMPTS_DIR,
        notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    triage_call = client.calls[1]
    # Aurora content is in scope, surfaces.
    assert "Aurora-only content" in triage_call.user
    # Personal content is in a different scope, does NOT reach triage.
    assert "Personal content" not in triage_call.user


def test_combined_cost_summed_across_passes(tmp_path: Path) -> None:
    """The returned plan's token totals are classifier + triage."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_contact(scopes=("aurora",))
    client = FakeClaudeClient(responses=[
        _classifier_response("aurora", in_tokens=500, out_tokens=10),
        _triage_response(in_tokens=1800, out_tokens=120),
    ])
    result = _run(triage_with_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.raw_input_tokens == 500 + 1800
    assert result.raw_output_tokens == 10 + 120


# ---- Out-of-scope synthetic plan -----------------------------------------


def test_out_of_scope_classification_returns_synthetic_plan(tmp_path: Path) -> None:
    """Classifier returns out_of_scope → synthetic decline plan, no
    triage call."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_contact(scopes=("aurora",))
    client = FakeClaudeClient(responses=[_classifier_response("out_of_scope")])

    result = _run(triage_with_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.verb == "out_of_scope_decline"
    assert result.tier == 3
    assert "body" in result.args
    assert len(result.args["body"]) > 0
    # Only the classifier call, no triage call.
    assert len(client.calls) == 1


def test_out_of_scope_decline_body_lists_allowed_topics(tmp_path: Path) -> None:
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_contact(scopes=("aurora", "music-tech"))
    client = FakeClaudeClient(responses=[_classifier_response("out_of_scope")])

    result = _run(triage_with_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    body = result.args["body"]
    # The descriptions from the registry surface in the decline body
    # so the contact knows what they CAN come back with.
    assert "Aurora redesign" in body
    assert "music production" in body


# ---- Classifier failure → fail-closed decline ----------------------------


def test_classifier_sdk_error_returns_synthetic_decline(tmp_path: Path) -> None:
    """Pass-1 SDK error → synthetic decline (fail closed). No pass-2."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_contact(scopes=("aurora",))
    @dataclass
    class _RaisingClient:
        async def call(self, **kwargs):
            raise RuntimeError("network down")

    result = _run(triage_with_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        structure=_structure(), config=_make_config(),
        triage_client=_RaisingClient(),  # type: ignore[arg-type]
        classifier_client=_RaisingClient(),  # type: ignore[arg-type]
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.verb == "out_of_scope_decline"
    # Reasoning makes the failure surfacing visible to the principal.
    assert "fail" in result.reasoning.lower() or "error" in result.reasoning.lower()


def test_classifier_unknown_scope_returns_synthetic_decline(tmp_path: Path) -> None:
    """Model returns a scope name we didn't allow → fail closed."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_contact(scopes=("aurora",))
    # Returns a scope NOT in contact.scopes.
    client = FakeClaudeClient(responses=[_classifier_response("personal")])

    result = _run(triage_with_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.verb == "out_of_scope_decline"
    # Triage was NOT called.
    assert len(client.calls) == 1


def test_classifier_error_carries_classifier_tokens(tmp_path: Path) -> None:
    """The synthetic decline's token counts include the classifier's
    spend so the cost backstop sees what was actually paid."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_contact(scopes=("aurora",))
    # unknown_scope path — tokens preserved through the validator error.
    client = FakeClaudeClient(responses=[
        _classifier_response("personal", in_tokens=600, out_tokens=10),
    ])

    result = _run(triage_with_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry=_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.raw_input_tokens == 600
    assert result.raw_output_tokens == 10


# ===========================================================================
# Scope/sensitivity Part 1: two-axis orchestrator path
# ===========================================================================


_FACETS_REGISTRY = {
    "calendar": "scheduling and availability",
    "communication-style": "tone and cadence",
}

_PROJECTS_REGISTRY = {
    "aurora": "the Aurora redesign",
    "aurora.music": "music for Aurora",
    "aurora.legal": "legal work for Aurora",
}


def _make_two_axis_contact(
    facets: tuple[str, ...] = ("calendar",),
    projects: tuple[str, ...] = ("aurora", "aurora.music"),
) -> Contact:
    return Contact(
        contact_id="fraser",
        addresses=("fraser@example.com",),
        display_name="Fraser",
        relationship="collaborator",
        daily_limit=3,
        is_principal=False,
        inboxes=("nightjar",),
        scopes=(),
        facets=facets,
        projects=projects,
    )


def _two_axis_classifier_response(
    *,
    facets: list[str],
    project: str,
    in_tokens: int = 700,
    out_tokens: int = 14,
) -> ClaudeResponse:
    return ClaudeResponse(
        tool_uses=(
            {
                "name": "classify_two_axis",
                "input": {"facets": facets, "project": project},
            },
        ),
        text_blocks=(),
        stop_reason="tool_use",
        input_tokens=in_tokens,
        output_tokens=out_tokens,
    )


def test_two_axis_in_scope_runs_classifier_then_triage(tmp_path: Path) -> None:
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_two_axis_contact()
    client = FakeClaudeClient(responses=[
        _two_axis_classifier_response(facets=["calendar"], project="aurora.music"),
        _triage_response(),
    ])

    result = _run(triage_with_scope(
        contact=contact, sender="fraser@example.com",
        subject="track 3 demo", body="when can we sync about the demo?",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry={},
        facets_registry=_FACETS_REGISTRY,
        projects_registry=_PROJECTS_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.verb == "reply"
    # Two calls: classifier then triage.
    assert len(client.calls) == 2
    assert client.calls[0].tools[0]["name"] == "classify_two_axis"
    assert client.calls[1].tools[0]["name"] == "draft_plan"


def test_two_axis_full_out_of_scope_returns_decline(tmp_path: Path) -> None:
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_two_axis_contact()
    client = FakeClaudeClient(responses=[
        _two_axis_classifier_response(facets=[], project="out_of_scope"),
    ])

    result = _run(triage_with_scope(
        contact=contact, sender="fraser@example.com",
        subject="hi", body="random thing not in scope",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry={},
        facets_registry=_FACETS_REGISTRY,
        projects_registry=_PROJECTS_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.verb == "out_of_scope_decline"
    # Triage NOT called.
    assert len(client.calls) == 1


def test_two_axis_facets_only_routes_to_triage(tmp_path: Path) -> None:
    """Facets non-empty + project=out_of_scope is NOT full out-of-scope.
    Triage runs with facet-only visibility."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_two_axis_contact()
    client = FakeClaudeClient(responses=[
        _two_axis_classifier_response(
            facets=["calendar"], project="out_of_scope",
        ),
        _triage_response(),
    ])

    result = _run(triage_with_scope(
        contact=contact, sender="fraser@example.com",
        subject="when free?", body="schedule check",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry={},
        facets_registry=_FACETS_REGISTRY,
        projects_registry=_PROJECTS_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.verb == "reply"
    assert len(client.calls) == 2


def test_two_axis_project_filters_notes_by_hierarchy(tmp_path: Path) -> None:
    """Notes tagged aurora.legal must NOT reach triage when classifier
    chose aurora.music (sibling sub-scopes are isolated)."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    notes_path = notes_dir / "fraser.md"
    notes_path.write_text(
        "---\ncontact_id: fraser\n---\n\n"
        "## Aurora overall [scopes: aurora]\n\n"
        "- Generic aurora context.\n\n"
        "## Aurora music [scopes: aurora.music]\n\n"
        "- Track 3 in progress.\n\n"
        "## Aurora legal [scopes: aurora.legal]\n\n"
        "- Contract pending.\n",
        encoding="utf-8",
    )
    contact = _make_two_axis_contact(
        facets=(), projects=("aurora.music",),
    )
    client = FakeClaudeClient(responses=[
        _two_axis_classifier_response(facets=[], project="aurora.music"),
        _triage_response(),
    ])

    _run(triage_with_scope(
        contact=contact, sender="fraser@example.com",
        subject="s", body="b", structure=_structure(),
        config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry={},
        facets_registry=_FACETS_REGISTRY,
        projects_registry=_PROJECTS_REGISTRY,
    ))
    triage_call = client.calls[1]
    # Sub-scope content visible.
    assert "Track 3 in progress" in triage_call.user
    # Parent content visible (parent-tagged bullet at parent visible to child).
    assert "Generic aurora context" in triage_call.user
    # Sibling NOT visible.
    assert "Contract pending" not in triage_call.user


def test_two_axis_classifier_error_returns_decline(tmp_path: Path) -> None:
    """If the two-axis classifier returns an unknown facet, the
    validator surfaces a ClassifierError → synthetic decline."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_two_axis_contact()
    bad_response = ClaudeResponse(
        tool_uses=(
            {
                "name": "classify_two_axis",
                "input": {"facets": ["finance"], "project": "out_of_scope"},
            },
        ),
        text_blocks=(),
        stop_reason="tool_use",
        input_tokens=400,
        output_tokens=8,
    )
    client = FakeClaudeClient(responses=[bad_response])

    result = _run(triage_with_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry={},
        facets_registry=_FACETS_REGISTRY,
        projects_registry=_PROJECTS_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.verb == "out_of_scope_decline"
    # Triage not called.
    assert len(client.calls) == 1


def test_two_axis_combined_token_accounting(tmp_path: Path) -> None:
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_two_axis_contact()
    client = FakeClaudeClient(responses=[
        _two_axis_classifier_response(
            facets=["calendar"], project="aurora",
            in_tokens=700, out_tokens=14,
        ),
        _triage_response(in_tokens=2000, out_tokens=150),
    ])

    result = _run(triage_with_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry={},
        facets_registry=_FACETS_REGISTRY,
        projects_registry=_PROJECTS_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    # Tokens summed across passes.
    assert result.raw_input_tokens == 2700
    assert result.raw_output_tokens == 164


def test_two_axis_decline_body_lists_facets_and_projects(
    tmp_path: Path,
) -> None:
    """The decline body for a two-axis contact should list the
    contact's facets AND projects (using their human descriptions)."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_two_axis_contact(
        facets=("calendar",),
        projects=("aurora",),
    )
    client = FakeClaudeClient(responses=[
        _two_axis_classifier_response(facets=[], project="out_of_scope"),
    ])

    result = _run(triage_with_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry={},
        facets_registry=_FACETS_REGISTRY,
        projects_registry=_PROJECTS_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    body = result.args["body"]
    # Both axes' descriptions appear.
    assert "scheduling and availability" in body  # calendar
    assert "the Aurora redesign" in body  # aurora


def test_two_axis_safe_notes_passed_to_classifier(tmp_path: Path) -> None:
    """Pass-1 classifier sees only safe (unscoped+wildcard) notes, just
    like the legacy single-axis path."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    notes_path = notes_dir / "fraser.md"
    notes_path.write_text(
        "---\ncontact_id: fraser\n---\n\n"
        "## General\n\n"
        "- Wildcard fact. [scopes: *]\n\n"
        "## Aurora [scopes: aurora]\n\n"
        "- Project secret.\n",
        encoding="utf-8",
    )
    contact = _make_two_axis_contact(
        facets=(), projects=("aurora",),
    )
    client = FakeClaudeClient(responses=[
        _two_axis_classifier_response(facets=[], project="aurora"),
        _triage_response(),
    ])

    _run(triage_with_scope(
        contact=contact, sender="fraser@example.com",
        subject="s", body="b", structure=_structure(),
        config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry={},
        facets_registry=_FACETS_REGISTRY,
        projects_registry=_PROJECTS_REGISTRY,
    ))
    classifier_call = client.calls[0]
    assert "Wildcard fact" in classifier_call.user
    # Scoped content MUST NOT appear in classifier prompt.
    assert "Project secret" not in classifier_call.user


def test_two_axis_classifier_sdk_failure_fails_closed(tmp_path: Path) -> None:
    """SDK exception during pass-1 → synthetic decline (fail closed)."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    contact = _make_two_axis_contact()

    @dataclass
    class _RaisingClient:
        calls: list[Any] = field(default_factory=list)

        async def call(
            self, *, model: str, system: str, user: str,
            tools: list[dict[str, Any]], max_tokens: int,
        ) -> ClaudeResponse:
            self.calls.append((model, user))
            raise RuntimeError("SDK exploded")

    client = _RaisingClient()
    result = _run(triage_with_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        structure=_structure(), config=_make_config(),
        triage_client=client, classifier_client=client,
        prompts_dir=PROMPTS_DIR, notes_dir=notes_dir,
        scopes_registry={},
        facets_registry=_FACETS_REGISTRY,
        projects_registry=_PROJECTS_REGISTRY,
    ))
    assert isinstance(result, TriagePlan)
    assert result.verb == "out_of_scope_decline"
    # Only the classifier call was attempted.
    assert len(client.calls) == 1
