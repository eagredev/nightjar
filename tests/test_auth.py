"""TOTP and auth-related tests. Stdlib-only."""
from __future__ import annotations

import time

import pytest

from daemon import auth


def test_generate_secret_is_valid_base32() -> None:
    s = auth.generate_secret()
    assert auth.is_valid_secret(s)


def test_is_valid_secret_rejects_garbage() -> None:
    assert not auth.is_valid_secret("")
    assert not auth.is_valid_secret("not base32!")
    assert not auth.is_valid_secret("ABC1")  # 1 not in base32 alphabet


def test_verify_totp_roundtrip_at_current_time() -> None:
    secret = auth.generate_secret()
    code = auth.current_code(secret)
    assert auth.verify_totp(secret=secret, code=code)


def test_verify_totp_accepts_previous_window_for_clock_skew() -> None:
    secret = auth.generate_secret()
    now = time.time()
    # Code that was valid 30s ago.
    prev = auth.current_code(secret, now=now - auth.TOTP_PERIOD_SECONDS)
    assert auth.verify_totp(secret=secret, code=prev, now=now)


def test_verify_totp_accepts_next_window_for_clock_skew() -> None:
    secret = auth.generate_secret()
    now = time.time()
    nxt = auth.current_code(secret, now=now + auth.TOTP_PERIOD_SECONDS)
    assert auth.verify_totp(secret=secret, code=nxt, now=now)


def test_verify_totp_rejects_two_windows_ago() -> None:
    """Grace is ±1 window; ±2 should fail."""
    secret = auth.generate_secret()
    now = time.time()
    far = auth.current_code(secret, now=now - 2 * auth.TOTP_PERIOD_SECONDS)
    # Note: this can be flaky in the unlucky case where two windows
    # back happens to collide with a valid window. The TOTP space is
    # 1e6, so the false-positive probability is ~1e-6 per run.
    assert not auth.verify_totp(secret=secret, code=far, now=now)


def test_verify_totp_rejects_malformed_codes() -> None:
    secret = auth.generate_secret()
    assert not auth.verify_totp(secret=secret, code="12345")     # 5 digits
    assert not auth.verify_totp(secret=secret, code="1234567")   # 7 digits
    assert not auth.verify_totp(secret=secret, code="abcdef")    # alpha
    assert not auth.verify_totp(secret=secret, code="")          # empty
    assert not auth.verify_totp(secret=secret, code="12 456")    # space


def test_verify_totp_rejects_with_bad_secret() -> None:
    assert not auth.verify_totp(secret="not base32", code="123456")
    assert not auth.verify_totp(secret="", code="123456")


def test_extract_code_from_subject_prefix() -> None:
    assert auth.extract_code_from_subject("[123456] do the thing") == "123456"
    assert auth.extract_code_from_subject("  [654321] hi  ") == "654321"


def test_extract_code_from_subject_handles_reply_prefix() -> None:
    assert auth.extract_code_from_subject("Re: [123456] re: existing thread") == "123456"
    assert auth.extract_code_from_subject("Fwd: [123456] forwarded") == "123456"


def test_extract_code_from_subject_returns_none_when_missing() -> None:
    assert auth.extract_code_from_subject("no code here") is None
    assert auth.extract_code_from_subject("") is None
    assert auth.extract_code_from_subject(None) is None
    # Wrong format: not exactly 6 digits.
    assert auth.extract_code_from_subject("[12345] short") is None
    assert auth.extract_code_from_subject("[1234567] long") is None
    assert auth.extract_code_from_subject("[abcdef] alpha") is None


def test_extract_code_only_matches_at_start() -> None:
    """Code must be the leading prefix, not buried in the middle."""
    assert auth.extract_code_from_subject("hello [123456] world") is None


def test_provisioning_uri_round_trips_secret() -> None:
    secret = auth.generate_secret()
    uri = auth.provisioning_uri(secret=secret, account="test@example.com")
    assert uri.startswith("otpauth://totp/Nightjar%3Atest%40example.com?")
    assert f"secret={secret}" in uri
    assert "issuer=Nightjar" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


# ---- HOTP -----------------------------------------------------------------


def test_verify_hotp_matches_next_counter() -> None:
    secret = auth.generate_secret()
    code = auth.hotp_at(secret, 1)
    matched = auth.verify_hotp(secret=secret, code=code, last_counter=0)
    assert matched == 1


def test_verify_hotp_lookahead_finds_skipped_counter() -> None:
    """Operator tapped 'next' a few times; the daemon should resync."""
    secret = auth.generate_secret()
    # Daemon last accepted counter 3; phone is now showing counter 8.
    code = auth.hotp_at(secret, 8)
    matched = auth.verify_hotp(secret=secret, code=code, last_counter=3)
    assert matched == 8


def test_verify_hotp_rejects_past_counter() -> None:
    """A code at or below last_counter is a replay and must fail."""
    secret = auth.generate_secret()
    code = auth.hotp_at(secret, 5)
    assert auth.verify_hotp(secret=secret, code=code, last_counter=5) is None
    assert auth.verify_hotp(secret=secret, code=code, last_counter=10) is None


def test_verify_hotp_rejects_outside_lookahead_window() -> None:
    secret = auth.generate_secret()
    # Default lookahead is 20; counter 25 is out of range from last_counter=0.
    code = auth.hotp_at(secret, 25)
    assert auth.verify_hotp(secret=secret, code=code, last_counter=0) is None


def test_verify_hotp_respects_custom_lookahead() -> None:
    secret = auth.generate_secret()
    code = auth.hotp_at(secret, 50)
    assert auth.verify_hotp(secret=secret, code=code, last_counter=0, lookahead=100) == 50
    assert auth.verify_hotp(secret=secret, code=code, last_counter=0, lookahead=10) is None


def test_verify_hotp_rejects_malformed_codes() -> None:
    secret = auth.generate_secret()
    assert auth.verify_hotp(secret=secret, code="12345", last_counter=0) is None
    assert auth.verify_hotp(secret=secret, code="abcdef", last_counter=0) is None
    assert auth.verify_hotp(secret=secret, code="", last_counter=0) is None


def test_verify_hotp_rejects_bad_secret() -> None:
    assert auth.verify_hotp(secret="not base32", code="123456", last_counter=0) is None


def test_verify_hotp_replay_via_advancing_counter() -> None:
    """After accepting counter 5, the same code at counter 5 must fail."""
    secret = auth.generate_secret()
    code = auth.hotp_at(secret, 5)
    assert auth.verify_hotp(secret=secret, code=code, last_counter=4) == 5
    # After persisting matched=5, the same code is now in the past.
    assert auth.verify_hotp(secret=secret, code=code, last_counter=5) is None


def test_hotp_provisioning_uri_includes_counter_zero() -> None:
    secret = auth.generate_secret()
    uri = auth.hotp_provisioning_uri(secret=secret, account="x@example.com")
    assert uri.startswith("otpauth://hotp/Nightjar%3Ax%40example.com?")
    assert f"secret={secret}" in uri
    assert "counter=0" in uri
    assert "digits=6" in uri
