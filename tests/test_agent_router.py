"""Tests for agent_router.classify()."""
from __future__ import annotations

import pytest

from daemon.agent_router import (
    CLASS_AGENT_CONTINUATION,
    CLASS_AGENT_INIT,
    CLASS_NOT_AGENT,
    classify,
)


def _no_session(_irt: str) -> None:
    return None


def _has_session(_irt: str) -> str:
    return "sess-1"


# ---- Init shape -----------------------------------------------------------


def test_init_two_codes_first_line() -> None:
    body = "123456 654321\nplease do a thing for me"
    r = classify(body=body, in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_AGENT_INIT
    assert r.primary_code == "123456"
    assert r.secondary_code == "654321"
    assert r.request_body == "please do a thing for me"


def test_init_with_tab_separator() -> None:
    body = "123456\t654321\ntab-separated also accepted"
    r = classify(body=body, in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_AGENT_INIT
    assert r.primary_code == "123456"
    assert r.secondary_code == "654321"


def test_init_strips_only_first_line() -> None:
    """Multi-line bodies preserve everything after the first newline."""
    body = "123456 654321\nline two\nline three"
    r = classify(body=body, in_reply_to=None, active_session_lookup=_no_session)
    assert r.request_body == "line two\nline three"


def test_init_with_trailing_carriage_return() -> None:
    """Outlook etc. sends CRLF; the first line should still match."""
    body = "123456 654321\r\nrequest"
    r = classify(body=body, in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_AGENT_INIT
    assert r.request_body == "request"


def test_init_rejects_three_codes() -> None:
    body = "123456 654321 999999\nnope"
    r = classify(body=body, in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_NOT_AGENT


def test_init_rejects_with_leading_text() -> None:
    """Init line must be JUST the two codes — no preamble."""
    body = "codes: 123456 654321\nrequest"
    r = classify(body=body, in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_NOT_AGENT


def test_init_rejects_wrong_digit_count() -> None:
    body = "12345 654321\nrequest"  # 5 digits in first
    r = classify(body=body, in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_NOT_AGENT


def test_init_rejects_letters() -> None:
    body = "123abc 654321\nrequest"
    r = classify(body=body, in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_NOT_AGENT


# ---- Continuation shape ---------------------------------------------------


def test_continuation_with_matching_session() -> None:
    body = "654321\nfollow-up question"
    r = classify(
        body=body,
        in_reply_to="<reply@example.com>",
        active_session_lookup=_has_session,
    )
    assert r.kind == CLASS_AGENT_CONTINUATION
    assert r.secondary_code == "654321"
    assert r.session_id == "sess-1"
    assert r.request_body == "follow-up question"


def test_continuation_without_matching_session_falls_through() -> None:
    """Single 6-digit number at top of body BUT no matching session →
    not_agent, no DMS burn."""
    body = "654321\nactually just a number I happened to type"
    r = classify(
        body=body,
        in_reply_to="<unrelated@example.com>",
        active_session_lookup=_no_session,
    )
    assert r.kind == CLASS_NOT_AGENT


def test_continuation_without_in_reply_to_falls_through() -> None:
    body = "654321\nfollow-up"
    r = classify(
        body=body,
        in_reply_to=None,
        active_session_lookup=_has_session,
    )
    assert r.kind == CLASS_NOT_AGENT


def test_continuation_strips_whitespace_from_in_reply_to() -> None:
    """MUAs sometimes pad headers with whitespace."""
    captured = {}

    def lookup(irt: str) -> str:
        captured["received"] = irt
        return "sess-1"

    classify(
        body="654321\nx",
        in_reply_to="  <reply@example.com>  \n",
        active_session_lookup=lookup,
    )
    assert captured["received"] == "<reply@example.com>"


def test_continuation_rejects_two_codes() -> None:
    """Two codes is init shape — even if there's a session match."""
    body = "111111 222222\nx"
    r = classify(
        body=body,
        in_reply_to="<reply@example.com>",
        active_session_lookup=_has_session,
    )
    assert r.kind == CLASS_AGENT_INIT  # init wins, not continuation


# ---- Not-agent fallthrough ------------------------------------------------


def test_empty_body_not_agent() -> None:
    r = classify(body="", in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_NOT_AGENT


def test_none_body_not_agent() -> None:
    r = classify(body=None, in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_NOT_AGENT


def test_normal_email_not_agent() -> None:
    body = "Hi Nightjar,\n\nplease run status\n\n--\nDylan"
    r = classify(body=body, in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_NOT_AGENT


def test_body_starting_with_one_number_no_irt_not_agent() -> None:
    body = "100\nthat's how many things"
    r = classify(body=body, in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_NOT_AGENT


def test_body_starting_with_long_number_not_agent() -> None:
    """7 digits doesn't match either pattern."""
    body = "1234567\nbody"
    r = classify(
        body=body,
        in_reply_to="<x@y>",
        active_session_lookup=_has_session,
    )
    assert r.kind == CLASS_NOT_AGENT


def test_request_body_with_no_newline_after_codes_is_empty() -> None:
    """Body of just `123456 654321` with no further content yields an
    empty request body. The watcher should reject this loudly — empty
    request to the agent is meaningless — but the parser correctly
    classifies it."""
    body = "123456 654321"
    r = classify(body=body, in_reply_to=None, active_session_lookup=_no_session)
    assert r.kind == CLASS_AGENT_INIT
    assert r.request_body == ""
