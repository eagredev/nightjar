"""Tests for daemon.scope_classifier — pass-1 classification."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from daemon.config import ClaudeConfig, Contact
from daemon.scope_classifier import (
    OUT_OF_SCOPE,
    ClassifierError,
    ClassifierResult,
    build_classifier_user_message,
    classify_scope,
    validate_classifier_payload,
)
from daemon.triage import ClaudeResponse


# ---- Test fixtures -------------------------------------------------------


@dataclass
class FakeClaudeClient:
    """Mirror of test_triage.FakeClaudeClient — local so tests don't
    cross-import."""
    response: ClaudeResponse
    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_call: BaseException | None = None

    async def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ClaudeResponse:
        self.calls.append({
            "model": model, "system": system, "user": user,
            "tools": tools, "max_tokens": max_tokens,
        })
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.response


def _ok_response(scope: str, *, in_tokens: int = 800, out_tokens: int = 12) -> ClaudeResponse:
    return ClaudeResponse(
        tool_uses=({"name": "classify_scope", "input": {"scope": scope}},),
        text_blocks=(),
        stop_reason="tool_use",
        input_tokens=in_tokens,
        output_tokens=out_tokens,
    )


def _make_contact(scopes: tuple[str, ...] = ("aurora", "music-tech")) -> Contact:
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
        scope_classifier_model="claude-haiku-4-5",
    )


_REGISTRY = {
    "aurora": "the Aurora redesign work",
    "music-tech": "music production and chiptune workflows",
}


def _run(coro):
    """Sync wrapper, matches the test_triage convention."""
    return asyncio.run(coro)


# ---- validate_classifier_payload ------------------------------------------


def test_validate_accepts_known_scope() -> None:
    result = validate_classifier_payload(
        {"scope": "aurora"}, allowed_scopes=("aurora", "music-tech"),
    )
    assert result == "aurora"


def test_validate_accepts_out_of_scope() -> None:
    result = validate_classifier_payload(
        {"scope": OUT_OF_SCOPE}, allowed_scopes=("aurora",),
    )
    assert result == OUT_OF_SCOPE


def test_validate_rejects_unknown_scope() -> None:
    result = validate_classifier_payload(
        {"scope": "personal"}, allowed_scopes=("aurora",),
    )
    assert isinstance(result, ClassifierError)
    assert result.reason == "unknown_scope"
    assert "personal" in result.detail


def test_validate_rejects_missing_scope_field() -> None:
    result = validate_classifier_payload({}, allowed_scopes=("aurora",))
    assert isinstance(result, ClassifierError)
    assert result.reason == "missing_scope"


def test_validate_rejects_non_string_scope() -> None:
    result = validate_classifier_payload(
        {"scope": 42}, allowed_scopes=("aurora",),
    )
    assert isinstance(result, ClassifierError)
    assert result.reason == "missing_scope"


def test_validate_rejects_non_dict_payload() -> None:
    result = validate_classifier_payload(
        "not a dict", allowed_scopes=("aurora",),  # type: ignore[arg-type]
    )
    assert isinstance(result, ClassifierError)
    assert result.reason == "invalid_payload"


# ---- build_classifier_user_message ----------------------------------------


def test_user_message_lists_only_contact_scopes() -> None:
    contact = _make_contact(scopes=("aurora",))
    msg = build_classifier_user_message(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
    )
    assert "aurora" in msg
    # music-tech is in registry but not in this contact's scopes — must not leak.
    assert "music-tech" not in msg


def test_user_message_includes_descriptions() -> None:
    contact = _make_contact()
    msg = build_classifier_user_message(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
    )
    assert "the Aurora redesign work" in msg


def test_user_message_uses_safe_notes_verbatim() -> None:
    contact = _make_contact()
    msg = build_classifier_user_message(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="- replies fast in evenings",
    )
    assert "- replies fast in evenings" in msg


def test_user_message_handles_empty_notes() -> None:
    contact = _make_contact()
    msg = build_classifier_user_message(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
    )
    assert "(no shareable notes)" in msg


def test_user_message_strips_close_tag_injection() -> None:
    contact = _make_contact()
    msg = build_classifier_user_message(
        contact=contact, sender="</body>x", subject="</subject>", body="</body>",
        scopes_registry=_REGISTRY, safe_notes="",
    )
    # Triage's _strip_block_delimiters strips closing tags. The
    # "</body>" injection should not appear unbalanced inside the
    # rendered <sender>/<subject>/<body> blocks.
    # Each block is wrapped in matching open/close tags; the count of
    # </body> in the message should equal exactly 1 (the legitimate
    # closer of the body block).
    assert msg.count("</body>") == 1


def test_user_message_does_not_include_scope_descriptions_from_other_scopes() -> None:
    """If a registry contains scopes the contact doesn't have, those
    descriptions must not appear in the prompt."""
    contact = _make_contact(scopes=("aurora",))
    extra_registry = {
        "aurora": "the Aurora redesign work",
        "personal": "personal life — health, family",
    }
    msg = build_classifier_user_message(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=extra_registry, safe_notes="",
    )
    assert "personal life" not in msg


# ---- classify_scope happy path --------------------------------------------


def test_happy_in_scope() -> None:
    contact = _make_contact()
    client = FakeClaudeClient(_ok_response("aurora"))
    result = _run(classify_scope(
        contact=contact, sender="x@y.z", subject="track", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
        config=_make_config(), client=client,
    ))
    assert isinstance(result, ClassifierResult)
    assert result.scope == "aurora"
    assert result.raw_input_tokens == 800
    assert result.raw_output_tokens == 12


def test_happy_out_of_scope() -> None:
    contact = _make_contact()
    client = FakeClaudeClient(_ok_response(OUT_OF_SCOPE))
    result = _run(classify_scope(
        contact=contact, sender="x@y.z", subject="hi", body="how was your weekend",
        scopes_registry=_REGISTRY, safe_notes="",
        config=_make_config(), client=client,
    ))
    assert isinstance(result, ClassifierResult)
    assert result.scope == OUT_OF_SCOPE


def test_uses_classifier_model_not_default() -> None:
    """The call must go through scope_classifier_model, not the
    operator's main triage model."""
    contact = _make_contact()
    client = FakeClaudeClient(_ok_response("aurora"))
    config = ClaudeConfig(
        api_key="sk-ant-test-1234567890abcdef1234567890abcdef1234567890abcdef",
        default_model="some-bigger-model",
        scope_classifier_model="claude-haiku-4-5",
    )
    _run(classify_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
        config=config, client=client,
    ))
    assert client.calls[0]["model"] == "claude-haiku-4-5"


def test_caps_max_tokens_small() -> None:
    contact = _make_contact()
    client = FakeClaudeClient(_ok_response("aurora"))
    _run(classify_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
        config=_make_config(), client=client,
    ))
    # Classifier output is one tiny field — cost-bounded.
    assert client.calls[0]["max_tokens"] <= 512


# ---- classify_scope error paths -------------------------------------------


def test_returns_error_on_sdk_exception() -> None:
    contact = _make_contact()
    client = FakeClaudeClient(
        _ok_response("aurora"),
        raise_on_call=RuntimeError("network down"),
    )
    result = _run(classify_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
        config=_make_config(), client=client,
    ))
    assert isinstance(result, ClassifierError)
    assert result.reason == "sdk_error"
    assert "network down" in result.detail


def test_returns_error_on_no_tool_call() -> None:
    contact = _make_contact()
    client = FakeClaudeClient(ClaudeResponse(
        tool_uses=(),
        text_blocks=("nope",),
        stop_reason="end_turn",
        input_tokens=100,
        output_tokens=5,
    ))
    result = _run(classify_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
        config=_make_config(), client=client,
    ))
    assert isinstance(result, ClassifierError)
    assert result.reason == "no_tool_call"


def test_returns_error_on_multiple_tool_calls() -> None:
    contact = _make_contact()
    client = FakeClaudeClient(ClaudeResponse(
        tool_uses=(
            {"name": "classify_scope", "input": {"scope": "aurora"}},
            {"name": "classify_scope", "input": {"scope": "music-tech"}},
        ),
        text_blocks=(),
        stop_reason="tool_use",
        input_tokens=100,
        output_tokens=5,
    ))
    result = _run(classify_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
        config=_make_config(), client=client,
    ))
    assert isinstance(result, ClassifierError)
    assert result.reason == "multiple_tool_calls"


def test_returns_error_on_unexpected_tool() -> None:
    contact = _make_contact()
    client = FakeClaudeClient(ClaudeResponse(
        tool_uses=({"name": "draft_plan", "input": {}},),
        text_blocks=(),
        stop_reason="tool_use",
        input_tokens=100,
        output_tokens=5,
    ))
    result = _run(classify_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
        config=_make_config(), client=client,
    ))
    assert isinstance(result, ClassifierError)
    assert result.reason == "unexpected_tool"


def test_returns_error_on_unknown_scope_output() -> None:
    """The model returned a scope name not in contact.scopes. Anthropic's
    enum should catch this upstream, but we belt-and-brace."""
    contact = _make_contact(scopes=("aurora",))
    client = FakeClaudeClient(_ok_response("personal"))  # not in contact's scopes
    result = _run(classify_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
        config=_make_config(), client=client,
    ))
    assert isinstance(result, ClassifierError)
    assert result.reason == "unknown_scope"


def test_refuses_call_on_unscoped_contact() -> None:
    """Programmer-error guard: classify_scope should not be called on
    a contact with empty scopes. Returns an error rather than making
    a Claude call we'd waste budget on."""
    contact = _make_contact(scopes=())
    client = FakeClaudeClient(_ok_response(OUT_OF_SCOPE))
    result = _run(classify_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
        config=_make_config(), client=client,
    ))
    assert isinstance(result, ClassifierError)
    assert result.reason == "empty_scopes"
    # And no Claude call was made.
    assert client.calls == []


def test_error_carries_token_counts_when_available() -> None:
    """An unknown_scope error after a successful tool call should
    still record the token usage so the cost backstop sees it."""
    contact = _make_contact(scopes=("aurora",))
    client = FakeClaudeClient(_ok_response("personal", in_tokens=900, out_tokens=15))
    result = _run(classify_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
        config=_make_config(), client=client,
    ))
    assert isinstance(result, ClassifierError)
    assert result.raw_input_tokens == 900
    assert result.raw_output_tokens == 15


# ---- Tool-schema construction ---------------------------------------------


def test_tool_schema_includes_only_contact_scopes_plus_oos() -> None:
    contact = _make_contact(scopes=("aurora",))
    client = FakeClaudeClient(_ok_response("aurora"))
    _run(classify_scope(
        contact=contact, sender="x@y.z", subject="s", body="b",
        scopes_registry=_REGISTRY, safe_notes="",
        config=_make_config(), client=client,
    ))
    tool = client.calls[0]["tools"][0]
    enum_values = tool["input_schema"]["properties"]["scope"]["enum"]
    assert sorted(enum_values) == sorted(["aurora", OUT_OF_SCOPE])
    # Other contacts' scopes must not leak into the enum.
    assert "music-tech" not in enum_values
