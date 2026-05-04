"""Per-email TOTP authentication for principal mail.

This module is the load-bearing first line of defence in Nightjar's
threat model. The TOTP secret never enters any LLM context: it lives
in `nightjar.conf`, is loaded by this module, and is used only to
verify codes. No prompt, tool result, or log line ever contains it.

RFC 6238 TOTP, SHA-1, 6 digits, 30-second window, ±1 window grace
for clock skew. Stdlib only (`hmac`, `hashlib`, `base64`, `secrets`,
`re`, `struct`, `time`, `urllib.parse`).

Verification flow (all pre-LLM):

    extract_code_from_subject  -> 6-digit code or None
    verify_totp                -> True iff code matches one of the
                                  three windows (now-30s, now, now+30s)
    state.totp_code_was_used   -> replay protection
    state.mark_totp_code_used  -> consume the code

Failures fall through to the dead-man's-switch counter; see
`AuthVerdict` for the categorical outcomes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from dataclasses import dataclass
from urllib.parse import quote, urlencode


TOTP_DIGITS = 6
TOTP_PERIOD_SECONDS = 30
# ±1 window of clock skew tolerance, per RFC 6238 §6 implementation note.
TOTP_GRACE_WINDOWS = 1

# Subject-line code format: leading [123456], optionally followed by
# whitespace and the rest of the subject. Anchored to the start.
_SUBJECT_CODE_RE = re.compile(r"^\s*\[(\d{6})\]")

# Base32 alphabet for secret validation.
_BASE32_RE = re.compile(r"^[A-Z2-7]+=*$")


@dataclass(frozen=True)
class AuthVerdict:
    """Outcome of authenticating one principal-claimed email.

    `ok` is True only when the code parsed, matched a window, and was
    not a replay. Every other outcome is a switch-counter increment.
    """
    ok: bool
    reason: str  # human-readable; safe to log (does NOT contain the code)
    code: str | None = None  # the 6-digit code if extracted; consumed iff ok


def generate_secret() -> str:
    """Generate a fresh base32-encoded TOTP secret (160 bits / 32 chars).

    Uses `secrets.token_bytes` for cryptographic randomness. The result
    is the canonical secret string written into `nightjar.conf` and
    handed to the operator's TOTP app.
    """
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def is_valid_secret(secret: str) -> bool:
    """Return True if `secret` is a plausible base32 TOTP secret.

    Accepts uppercase A-Z and 2-7 with optional `=` padding. Length must
    be a multiple of 8 after padding (base32 invariant). Empty strings
    are rejected.
    """
    if not secret:
        return False
    s = secret.upper()
    if not _BASE32_RE.match(s):
        return False
    pad_needed = (-len(s)) % 8
    try:
        base64.b32decode(s + "=" * pad_needed, casefold=False)
    except Exception:
        return False
    return True


def provisioning_uri(*, secret: str, account: str, issuer: str = "Nightjar") -> str:
    """Build an otpauth:// URI for QR-code provisioning.

    The format is the de facto standard supported by Aegis, 2FAS,
    Authy, FreeOTP, etc. Form:

        otpauth://totp/Nightjar:account?secret=...&issuer=Nightjar
                                       &algorithm=SHA1&digits=6&period=30
    """
    label = quote(f"{issuer}:{account}", safe="")
    params = urlencode({
        "secret": secret,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": str(TOTP_DIGITS),
        "period": str(TOTP_PERIOD_SECONDS),
    })
    return f"otpauth://totp/{label}?{params}"


def _hotp(secret_bytes: bytes, counter: int) -> str:
    """RFC 4226 HOTP — the inner primitive of TOTP."""
    msg = struct.pack(">Q", counter)
    digest = hmac.new(secret_bytes, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def _decode_secret(secret: str) -> bytes:
    """Decode a base32 secret string to raw bytes for HMAC."""
    s = secret.upper()
    pad_needed = (-len(s)) % 8
    return base64.b32decode(s + "=" * pad_needed, casefold=False)


def verify_totp(*, secret: str, code: str, now: float | None = None) -> bool:
    """Verify a 6-digit code against the secret with ±1 window grace.

    Constant-time comparison via `hmac.compare_digest`. Returns False on
    any malformed input (wrong length, non-digit, bad secret) without
    leaking which one.
    """
    if not isinstance(code, str) or len(code) != TOTP_DIGITS or not code.isdigit():
        return False
    try:
        secret_bytes = _decode_secret(secret)
    except Exception:
        return False

    t = now if now is not None else time.time()
    counter_now = int(t // TOTP_PERIOD_SECONDS)

    for delta in range(-TOTP_GRACE_WINDOWS, TOTP_GRACE_WINDOWS + 1):
        candidate = _hotp(secret_bytes, counter_now + delta)
        if hmac.compare_digest(candidate, code):
            return True
    return False


def current_code(secret: str, now: float | None = None) -> str:
    """Return the code valid for the current 30-second window.

    Used by the `--revive` command to compare the operator-typed code
    against; never used for the email-auth path.
    """
    secret_bytes = _decode_secret(secret)
    t = now if now is not None else time.time()
    return _hotp(secret_bytes, int(t // TOTP_PERIOD_SECONDS))


def extract_code_from_subject(subject: str | None) -> str | None:
    """Pull a 6-digit `[123456]` prefix out of a Subject header.

    Returns the bare code or None. Tolerates leading whitespace and
    common Re:/Fwd: prefixes by stripping them once before matching.
    """
    if not subject:
        return None
    s = subject.strip()
    # Strip a single leading reply/forward prefix if present, so that
    # `Re: [123456] foo` still works for back-and-forth threads.
    s = re.sub(r"^(?:re|fwd|fw)\s*:\s*", "", s, flags=re.IGNORECASE)
    m = _SUBJECT_CODE_RE.match(s)
    return m.group(1) if m else None
