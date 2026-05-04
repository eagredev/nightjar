"""Parse Authentication-Results headers stamped by a trusted authserv.

This module is the inbound spoofing defence. It reads the
`Authentication-Results:` header that the inbox MTA (Gmail's
mx.google.com, in our case) writes when a message arrives, extracts
the `dmarc=` verdict, and returns a typed result the watcher can
gate on.

Why we trust ONE specific authserv (and not "any A-R header we see"):
an attacker can prepend their own Authentication-Results header to a
message they craft. RFC 8601 §5 calls this out and says receivers
SHOULD remove A-R headers added by upstream and add their own. The
defence is to ignore everything except the header whose `authserv-id`
matches our configured trusted server. For our Gmail inbox that's
`mx.google.com`. Any other A-R header is data, not authentication.

The parser is deliberately tolerant of the exact comment/property
format Gmail uses (and of the older RFC 5451 / 7001 / 8601
variations) but strict about the verdict token: only `pass` is
treated as authenticated. `none`, `fail`, `temperror`, `permerror`,
or a missing `dmarc=` token all flow to a non-authenticated result
and are gated separately by the caller.

Stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message


# Verdicts we surface to the watcher. "pass" is the only authenticated
# state; everything else is some flavour of "do not trust this sender."
DMARC_PASS = "pass"
DMARC_FAIL = "fail"
DMARC_NONE = "none"
DMARC_TEMPERROR = "temperror"
DMARC_PERMERROR = "permerror"
DMARC_MISSING = "missing"  # no dmarc= token in the trusted A-R header
DMARC_NO_TRUSTED_HEADER = "no_trusted_header"  # no A-R header from our authserv

# The "trust this sender" set. Everything else is treated as adversarial
# or insufficient by the caller.
AUTHENTICATED = frozenset({DMARC_PASS})


@dataclass(frozen=True)
class DmarcVerdict:
    """The outcome of parsing the trusted authserv's A-R header.

    `verdict` is one of the DMARC_* constants. `header_from_domain` is
    the `header.from=` value the authserv attached to the dmarc check
    (i.e. the domain the verdict applies to). The watcher cross-checks
    this against the actual From: header's domain to catch the case
    where an attacker fakes the visible From while only authenticating
    a different domain.

    `raw_header` is the original line, kept for diagnostics.
    """
    verdict: str
    header_from_domain: str | None
    raw_header: str
    reason: str = ""

    @property
    def authenticated(self) -> bool:
        return self.verdict in AUTHENTICATED


# RFC 8601 §2.1: "Authentication-Results: <authserv-id>; <method>=<result> ..."
# We lean on Python's email parser to unfold the header for us; the
# leading authserv-id is always a single token before the first `;`.
_AUTHSERV_SPLIT = re.compile(r"^([^;\s]+)\s*;\s*(.*)$", re.DOTALL)

# Match `dmarc=<result>` where result is a bare token (alphanumeric
# plus a few). We deliberately accept the result without parens or
# trailing properties; the props are parsed separately.
_DMARC_VERDICT = re.compile(
    r"\bdmarc\s*=\s*([A-Za-z][A-Za-z0-9_-]*)",
    re.IGNORECASE,
)

# Match `header.from=<domain>` after the dmarc verdict. RFC 8601 §2.3
# allows several "ptype.property" tokens; we want the from-domain.
_HEADER_FROM = re.compile(
    r"\bheader\.from\s*=\s*([A-Za-z0-9_.\-]+)",
    re.IGNORECASE,
)


def parse_authentication_results(
    msg: Message, *, trusted_authserv: str
) -> DmarcVerdict:
    """Walk all A-R headers on `msg`, return the verdict from the
    trusted authserv. Returns DMARC_NO_TRUSTED_HEADER if no header
    from `trusted_authserv` is present (treat as adversarial).

    `trusted_authserv` is compared case-insensitively. The match is
    exact: a header stamped by `mx.google.com` does NOT match a
    config of `google.com`, so the operator picks the precise string
    they trust.
    """
    # `get_all` returns a list of header values, in the order they
    # appear, with each one already unfolded (RFC 5322 §2.2.3
    # continuation lines stitched together).
    raw_headers = msg.get_all("Authentication-Results") or []
    trusted = trusted_authserv.lower().strip()

    for raw in raw_headers:
        # Each header value has the form "<authserv-id>; methods..."
        # We don't lstrip the value because Python's parser already
        # delivers it unfolded.
        m = _AUTHSERV_SPLIT.match(raw.strip())
        if not m:
            continue
        authserv_id = m.group(1).strip().lower()
        if authserv_id != trusted:
            # Not from our authserv: ignore. An attacker who controls
            # any upstream hop could write a fake A-R header here, but
            # we never read it.
            continue

        methods_blob = m.group(2)

        verdict_match = _DMARC_VERDICT.search(methods_blob)
        from_match = _HEADER_FROM.search(methods_blob)
        header_from = from_match.group(1).lower() if from_match else None

        if not verdict_match:
            return DmarcVerdict(
                verdict=DMARC_MISSING,
                header_from_domain=header_from,
                raw_header=raw,
                reason="trusted_authserv stamped A-R but no dmarc= token",
            )

        verdict_token = verdict_match.group(1).lower()
        # Normalise: any token we don't recognise is treated as failure.
        if verdict_token in (
            DMARC_PASS, DMARC_FAIL, DMARC_NONE,
            DMARC_TEMPERROR, DMARC_PERMERROR,
        ):
            return DmarcVerdict(
                verdict=verdict_token,
                header_from_domain=header_from,
                raw_header=raw,
            )
        return DmarcVerdict(
            verdict=DMARC_FAIL,
            header_from_domain=header_from,
            raw_header=raw,
            reason=f"unrecognised dmarc verdict token: {verdict_token!r}",
        )

    return DmarcVerdict(
        verdict=DMARC_NO_TRUSTED_HEADER,
        header_from_domain=None,
        raw_header="",
        reason=f"no Authentication-Results header from {trusted_authserv!r}",
    )


def from_header_domain(msg: Message) -> str | None:
    """Extract the domain part of the From: header.

    Used by the watcher to cross-check `header_from_domain` against
    the visible From: a passing DMARC for `attacker.com` does not
    authenticate a From of `composer@hotmail.co.uk`.
    """
    raw = msg.get("From", "")
    # Pull the address from "Display Name <addr@domain>" or bare addr.
    import email.utils
    _, addr = email.utils.parseaddr(raw)
    addr = addr.lower().strip()
    if "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1] or None
