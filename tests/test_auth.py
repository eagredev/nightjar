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
