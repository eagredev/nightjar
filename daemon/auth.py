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


# ---- HOTP (RFC 4226) -------------------------------------------------------

# How many counters past `last_counter` we'll search for a match. RFC 4226
# §7.4 calls this the "look-ahead synchronization window". 20 is the spec's
# recommended default and tolerates the operator skipping a handful of
# codes by accident on the authenticator app.
HOTP_LOOKAHEAD = 20


def hotp_at(secret: str, counter: int) -> str:
    """Return the HOTP code for a specific counter value.

    Used by tests and by the setup command to display the first code
    (counter=1) so the operator can verify their authenticator is in sync.
    """
    return _hotp(_decode_secret(secret), counter)


def verify_hotp(
    *,
    secret: str,
    code: str,
    last_counter: int,
    lookahead: int = HOTP_LOOKAHEAD,
) -> int | None:
    """Verify an HOTP code against `last_counter+1 .. last_counter+lookahead`.

    Returns the matched counter (a strictly-greater integer than
    `last_counter`) on success, or None on any failure: malformed input,
    bad secret, no match within the lookahead window.

    The caller is responsible for advancing the persisted counter to the
    returned value. Codes at or below `last_counter` are never accepted,
    which gives replay protection for free: each counter is single-use
    because the counter only ever moves forward.

    Constant-time per-candidate comparison via `hmac.compare_digest`. The
    loop is not constant-time across candidates, but the timing only
    leaks how many counters ahead the matching one was — negligible
    given the attacker has no oracle for which counter the operator is on.
    """
    if not isinstance(code, str) or len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    if lookahead <= 0:
        return None
    try:
        secret_bytes = _decode_secret(secret)
    except Exception:
        return None
    for delta in range(1, lookahead + 1):
        candidate_counter = last_counter + delta
        candidate_code = _hotp(secret_bytes, candidate_counter)
        if hmac.compare_digest(candidate_code, code):
            return candidate_counter
    return None


def hotp_provisioning_uri(
    *, secret: str, account: str, issuer: str = "Nightjar", counter: int = 0
) -> str:
    """Build an otpauth://hotp/ URI for QR-code provisioning.

    HOTP URIs include an explicit counter (typically 0 at first
    provisioning) so the authenticator app starts in sync with the
    daemon. Most apps display "code N" alongside each generated code,
    making counter sync visible to the operator.
    """
    label = quote(f"{issuer}:{account}", safe="")
    params = urlencode({
        "secret": secret,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": str(TOTP_DIGITS),
        "counter": str(counter),
    })
    return f"otpauth://hotp/{label}?{params}"


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
