"""Tests for daemon/dmarc.py.

The corpus uses real Gmail-stamped Authentication-Results headers
captured from the live inbox so the parser handles the formatting
quirks Gmail actually emits (folding, parenthesised comments, the
ARC and DARA pseudo-methods, multiple methods in one string).
"""
from __future__ import annotations

import email
from email.message import Message

from daemon import dmarc
from daemon.dmarc import (
    DMARC_FAIL,
    DMARC_MISSING,
    DMARC_NO_TRUSTED_HEADER,
    DMARC_NONE,
    DMARC_PASS,
    DMARC_TEMPERROR,
    DmarcVerdict,
    from_header_domain,
    parse_authentication_results,
)


# ---- Real-world Gmail samples ----------------------------------------------

# Captured from the live inbox. dmarc=pass, header.from=gmail.com.
_GMAIL_PASS_FROM_GMAIL = (
    "From: Dy Mo <dylanmoir97@gmail.com>\r\n"
    "Subject: hi\r\n"
    "Authentication-Results: mx.google.com; dkim=pass header.i=@gmail.com\r\n"
    " header.s=20251104 header.b=GYBzeLkA; arc=pass (i=1); spf=pass\r\n"
    " (google.com: domain of dylanmoir97@gmail.com designates 209.85.220.41\r\n"
    " as permitted sender) smtp.mailfrom=dylanmoir97@gmail.com;\r\n"
    " dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) header.from=gmail.com;\r\n"
    " dara=pass header.i=@gmail.com\r\n"
    "\r\n"
    "body\r\n"
)


def _make_msg(blob: str) -> Message:
    return email.message_from_string(blob)


def test_real_gmail_pass_parses_to_pass() -> None:
    msg = _make_msg(_GMAIL_PASS_FROM_GMAIL)
    v = parse_authentication_results(msg, trusted_authserv="mx.google.com")
    assert v.verdict == DMARC_PASS
    assert v.authenticated is True
    assert v.header_from_domain == "gmail.com"


def test_authserv_id_match_is_case_insensitive() -> None:
    msg = _make_msg(_GMAIL_PASS_FROM_GMAIL)
    v = parse_authentication_results(msg, trusted_authserv="MX.Google.COM")
    assert v.verdict == DMARC_PASS


def test_dmarc_fail_returns_fail() -> None:
    blob = (
        "From: x@example.com\r\n"
        "Authentication-Results: mx.google.com; spf=fail; dmarc=fail "
        "(p=NONE) header.from=example.com\r\n\r\nbody"
    )
    v = parse_authentication_results(_make_msg(blob), trusted_authserv="mx.google.com")
    assert v.verdict == DMARC_FAIL
    assert v.header_from_domain == "example.com"


def test_dmarc_none_returns_none() -> None:
    blob = (
        "From: x@example.com\r\n"
        "Authentication-Results: mx.google.com; dmarc=none "
        "(p=NONE) header.from=nopublishedrecord.test\r\n\r\nbody"
    )
    v = parse_authentication_results(_make_msg(blob), trusted_authserv="mx.google.com")
    assert v.verdict == DMARC_NONE
    assert v.authenticated is False


def test_dmarc_temperror_returns_temperror() -> None:
    blob = (
        "From: x@example.com\r\n"
        "Authentication-Results: mx.google.com; dmarc=temperror "
        "header.from=example.com\r\n\r\nbody"
    )
    v = parse_authentication_results(_make_msg(blob), trusted_authserv="mx.google.com")
    assert v.verdict == DMARC_TEMPERROR


def test_unknown_verdict_token_treated_as_fail() -> None:
    blob = (
        "From: x@example.com\r\n"
        "Authentication-Results: mx.google.com; dmarc=lolnope "
        "header.from=example.com\r\n\r\nbody"
    )
    v = parse_authentication_results(_make_msg(blob), trusted_authserv="mx.google.com")
    assert v.verdict == DMARC_FAIL
    assert "unrecognised" in v.reason


def test_no_authentication_results_header_returns_no_trusted_header() -> None:
    blob = "From: x@example.com\r\nSubject: hi\r\n\r\nbody"
    v = parse_authentication_results(_make_msg(blob), trusted_authserv="mx.google.com")
    assert v.verdict == DMARC_NO_TRUSTED_HEADER


def test_a_r_header_from_other_authserv_is_ignored() -> None:
    """An attacker prepends their own A-R header from a fake authserv.
    We MUST refuse to read it."""
    blob = (
        "From: x@example.com\r\n"
        "Authentication-Results: attacker.evil; dmarc=pass "
        "header.from=victim.com\r\n"
        "\r\nbody"
    )
    v = parse_authentication_results(_make_msg(blob), trusted_authserv="mx.google.com")
    assert v.verdict == DMARC_NO_TRUSTED_HEADER
    assert "no Authentication-Results header from" in v.reason


def test_attacker_injected_a_r_header_does_not_override_real_one() -> None:
    """Attacker prepends a fake A-R, real Gmail one is below it. We
    select by authserv-id, not by header order, so the real one wins."""
    blob = (
        "Authentication-Results: attacker.evil; dmarc=pass "
        "header.from=victim.com\r\n"
        "Authentication-Results: mx.google.com; dmarc=fail "
        "header.from=actualspoofer.com\r\n"
        "From: x@victim.com\r\n"
        "\r\nbody"
    )
    v = parse_authentication_results(_make_msg(blob), trusted_authserv="mx.google.com")
    assert v.verdict == DMARC_FAIL
    assert v.header_from_domain == "actualspoofer.com"


def test_a_r_header_with_no_dmarc_token_returns_missing() -> None:
    blob = (
        "From: x@example.com\r\n"
        "Authentication-Results: mx.google.com; spf=pass; dkim=pass\r\n"
        "\r\nbody"
    )
    v = parse_authentication_results(_make_msg(blob), trusted_authserv="mx.google.com")
    assert v.verdict == DMARC_MISSING
    assert "no dmarc= token" in v.reason


def test_authenticated_property_only_true_for_pass() -> None:
    for verdict in (DMARC_FAIL, DMARC_NONE, DMARC_MISSING, DMARC_NO_TRUSTED_HEADER):
        v = DmarcVerdict(verdict=verdict, header_from_domain=None, raw_header="x")
        assert v.authenticated is False


def test_pass_authenticated_property_is_true() -> None:
    v = DmarcVerdict(verdict=DMARC_PASS, header_from_domain="gmail.com", raw_header="x")
    assert v.authenticated is True


# ---- from_header_domain ----------------------------------------------------


def test_from_header_domain_extracts_from_display_name_form() -> None:
    msg = _make_msg("From: Dy Mo <dylanmoir97@gmail.com>\r\n\r\n")
    assert from_header_domain(msg) == "gmail.com"


def test_from_header_domain_handles_bare_address() -> None:
    msg = _make_msg("From: x@example.com\r\n\r\n")
    assert from_header_domain(msg) == "example.com"


def test_from_header_domain_returns_none_when_no_at() -> None:
    msg = _make_msg("From: noreply\r\n\r\n")
    assert from_header_domain(msg) is None


def test_from_header_domain_lowercases() -> None:
    msg = _make_msg("From: X <X@Example.COM>\r\n\r\n")
    assert from_header_domain(msg) == "example.com"
