"""Reply parser for principal-command emails.

This module is purely deterministic: given a Subject and Body, it
returns a structured ParsedCommand. No LLM involvement, no I/O. The
watcher calls this *after* TOTP auth succeeds; everything here treats
the inputs as already-trusted-as-from-the-principal.

The grammar (post-Step-4a tweak):

    [123456] status                        -> tier-1 verb 'status'
    [123456] list pending                  -> tier-1 verb 'list pending'
    [123456] tail log 2026-05-04           -> tier-1 verb with arg
    [123456] show contact alice            -> tier-1 verb with arg
    [123456] run the build                 -> tier-2+ (recognised, queued)
    [123456] re: [Nightjar #a4f2c1] ...    -> approval-token reply

The TOTP/HOTP prefix is stripped by the auth layer before this parser
runs; we accept either the full subject (and re-strip defensively) or
the post-auth subject. The subject after the prefix must be exactly
the verb (with any registered args); decorative lead-ins like
"Nightjar," are NOT accepted, because they create ambiguity with
casual subjects that happen to mention the daemon's name. Strict
matching keeps the grammar unambiguous: a subject is a command, not
a sentence.

Anything that doesn't match a recognised verb OR an approval token is
classified as `free_form`. The watcher hands free-form requests to
the principal-interpret pass (see daemon/principal_interpret.py), which
asks Claude to either answer inline (tier-1) or propose a structured
plan for approval (tier-2+). The earlier "yes interpret" confirmation
gate was dropped on 2026-05-06 — once an authenticated principal sends
a free-form query, that's already authority enough to run interpretation
within the tier ceiling.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---- Verb registry --------------------------------------------------------


@dataclass(frozen=True)
class VerbSpec:
    """One row of the deterministic command grammar.

    `pattern` is anchored at the start of the verb-bearing slice (the
    text after `[code] Nightjar,` is stripped). The compiled regex
    captures named groups for handler args.

    `tier` follows DESIGN.md "Capability tiers":
      1: read-only, auto-execute
      2: reversible, queued + single confirmation
      3: outbound, queued + single confirmation + audit
      4: irreversible, queued + double-confirm
      5: external effects, queued + double-confirm + hardware
    """
    name: str
    tier: int
    pattern: str
    handler: str  # symbolic; resolved by principal_handlers.py


# Ordered longest-pattern-first so that "list pending" matches before any
# hypothetical bare "list" verb. The parser walks this list in order and
# returns the first match.
VERB_REGISTRY: tuple[VerbSpec, ...] = (
    VerbSpec(
        name="list pending",
        tier=1,
        pattern=r"^list\s+pending\s*$",
        handler="list_pending",
    ),
    VerbSpec(
        name="tail log",
        tier=1,
        # Optional date arg in YYYY-MM-DD form; defaults to today in handler.
        pattern=r"^tail\s+log(?:\s+(?P<date>\d{4}-\d{2}-\d{2}))?\s*$",
        handler="tail_log",
    ),
    VerbSpec(
        name="show notes",
        tier=1,
        pattern=r"^show\s+notes(?:\s+(?P<contact>\S+))?\s*$",
        handler="show_notes",
    ),
    VerbSpec(
        name="show contact",
        tier=1,
        pattern=r"^show\s+contact\s+(?P<contact>\S+)\s*$",
        handler="show_contact",
    ),
    VerbSpec(
        name="status",
        tier=1,
        pattern=r"^status\s*$",
        handler="status",
    ),
    # Tier 2 verbs: queued, single-approval. The handler runs after the
    # principal replies "yes" / "approve" / "go" with the matching token.
    VerbSpec(
        name="block",
        tier=2,
        pattern=r"^block\s+(?P<contact>\S+)\s*$",
        handler="block",
    ),
    VerbSpec(
        name="unblock",
        tier=2,
        pattern=r"^unblock\s+(?P<contact>\S+)\s*$",
        handler="unblock",
    ),
    VerbSpec(
        name="forget",
        tier=2,
        pattern=r"^forget\s+(?P<contact>\S+)\s*$",
        handler="forget",
    ),
    # add and remove are tier 2 (single approval): each touches one
    # contact's TOML file in contacts_dir, not the global nightjar.conf.
    # The blast radius is one contact — same shape as block/unblock —
    # so the double-confirm friction of tier 4 is no longer warranted.
    VerbSpec(
        name="add",
        tier=2,
        # Email is the args. We accept anything with an @ and a dot to
        # let the executor do the strict parse, since RFC 5322 is wide.
        pattern=r"^add\s+(?P<email>\S+@\S+\.\S+)\s*$",
        handler="add",
    ),
    VerbSpec(
        name="remove",
        tier=2,
        pattern=r"^remove\s+(?P<contact>\S+)\s*$",
        handler="remove",
    ),
)


# Approval verdict synonyms. Curated list — strict whole-line match
# (case-insensitive for these two sets), no extra trailing words. The
# principal types these in the BODY of an approval reply, after a
# `Re: [Nightjar #abc123] <code>` subject. The strict-line rule prevents
# an accidental approval from a quoted line in a back-and-forth thread.
_APPROVE_PHRASES = frozenset({
    "yes",
    "yes please",
    "approve",
    "approved",
    "go",
    "go for it",
    "go ahead",
    "ok",
    "okay",
    "confirm",
    "confirmed",
    "do it",
})
_DENY_PHRASES = frozenset({
    "no",
    "no thanks",
    "deny",
    "denied",
    "refuse",
    "refused",
    "reject",
    "rejected",
    "stop",
    "cancel",
    "nope",
})
# Tier-4 double-confirm phrase. UPPERCASE EXACT, no case folding;
# must be a standalone first non-quoted line in the body.
_TIER4_CONFIRM = "YES IRREVERSIBLE"


# Approval-token subjects look like:  re: [Nightjar #a4f2c1] 123456
# Token is hex, 6+ chars; we don't enforce a fixed length so future tokens
# can grow without changing the parser.
_APPROVAL_TOKEN_RE = re.compile(
    r"\[\s*Nightjar\s*#(?P<token>[a-f0-9]{6,})\s*\]",
    re.IGNORECASE,
)

# The TOTP/HOTP prefix is normally stripped by the auth layer, but the
# parser tolerates a leftover 6-digit code in case the caller forgets.
# Codes can be at either end of the subject; the trailing form is the
# ergonomic default for approval replies.
_LEADING_CODE_RE = re.compile(r"^\s*(?:\[\d{6}\]|\d{6}(?=\s|$))\s*")
_TRAILING_CODE_RE = re.compile(r"\s*(?:\[\d{6}\]|(?<=\s)\d{6})\s*$")

# Common reply prefixes. Stripped before approval-token detection.
_REPLY_PREFIX_RE = re.compile(r"^(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)

# Quoted-reply attribution lines. Anything from one of these onward is
# treated as quoted original, not the principal's verdict. Patterns
# cover Gmail/Apple Mail/Outlook conventions.
_QUOTE_ATTRIBUTION_RE = re.compile(
    r"^(?:"
    r"On\s.+wrote:\s*$"           # Gmail: `On Mon, May 5, 2026 at 12:34 ... wrote:`
    r"|-+\s*Original\s+Message\s*-+\s*$"  # Outlook: `-----Original Message-----`
    r"|From:\s.+$"                 # Outlook header-block start (rare in plain reply)
    r"|Begin\s+forwarded\s+message:\s*$"  # Apple Mail forward
    r")",
    re.IGNORECASE,
)
# Signature separator per RFC 3676 §4.3: a line consisting solely of
# "-- " (dash dash space). Anything below is the principal's signature
# and not part of the verdict.
_SIG_SEPARATOR = "-- "


# ---- ParsedCommand --------------------------------------------------------


@dataclass(frozen=True)
class ParsedCommand:
    """The structured outcome of parsing one principal email.

    Exactly one classification is populated:
      - verb + tier + args: a recognised tier-1+ verb
      - approval_token + approval_verdict: a reply to a pending
        approval ping. verdict is one of APPROVE, DENY, IRREVERSIBLE,
        UNCLEAR (token recognised but the verdict word didn't match).
      - is_free_form=True: anything else, handed to the principal-
        interpret pass (Claude call) for inline answer or plan.
    """
    raw_subject: str
    verb: str | None = None
    tier: int | None = None
    args: dict[str, str] = field(default_factory=dict)
    approval_token: str | None = None
    approval_verdict: str | None = None  # APPROVE | DENY | IRREVERSIBLE | UNCLEAR
    is_free_form: bool = False
    handler: str | None = None
    # The subject after stripping the code prefix and "Nightjar," lead-in.
    # Used for logging and as the user query for the principal-interpret
    # LLM call.
    payload: str = ""


# ---- Public parser --------------------------------------------------------


def parse_principal_command(
    subject: str | None, body: str | None = None
) -> ParsedCommand:
    """Parse a principal-mail subject (and optional body) into a structured command.

    For tier-1 verbs and free-form requests, the entire grammar lives
    in the subject and the body is ignored. For approval replies (the
    `[Nightjar #abc123]` shape), the verdict is extracted from the
    BODY: the first non-quoted, non-empty line of the reply is matched
    against the curated APPROVE / DENY synonym sets (or the literal
    `YES IRREVERSIBLE` for tier-4). This format puts the code at the
    end of the subject (where the cursor naturally sits after Reply)
    and frees the subject line to act as a stable thread identifier.
    """
    raw = subject or ""

    # Strip a single leading reply/forward prefix BEFORE checking for
    # approval tokens, so "Re: [Nightjar #abc123] ..." still surfaces
    # the token. We need the un-stripped version for token detection
    # though, since "[Nightjar #abc123]" might be later in the subject.
    no_reply_prefix = _REPLY_PREFIX_RE.sub("", raw, count=1)

    token_match = _APPROVAL_TOKEN_RE.search(no_reply_prefix)
    if token_match:
        # Approval reply: the verdict is in the BODY. We strip the
        # token tag and any stray code(s) from the subject for the
        # payload field (useful for logs), then classify body content
        # into APPROVE / DENY / IRREVERSIBLE / UNCLEAR. UNCLEAR is
        # preserved (rather than falling back to free-form) because
        # the resolver needs to email a "your reply didn't parse" hint
        # rather than the generic free-form prompt.
        token = token_match.group("token").lower()
        leftover = _APPROVAL_TOKEN_RE.sub("", no_reply_prefix, count=1)
        leftover = _LEADING_CODE_RE.sub("", leftover)
        leftover = _TRAILING_CODE_RE.sub("", leftover).strip()
        verdict = _classify_body_verdict(body)
        return ParsedCommand(
            raw_subject=raw,
            approval_token=token,
            approval_verdict=verdict,
            payload=no_reply_prefix.strip(),
        )

    # Strip a leading or trailing [123456] code if the auth layer left
    # it on. (It normally doesn't, but defensive against future refactors.)
    stripped = _LEADING_CODE_RE.sub("", raw)
    stripped = _TRAILING_CODE_RE.sub("", stripped)
    payload = stripped.strip()

    if not payload:
        return ParsedCommand(raw_subject=raw, is_free_form=True, payload="")

    lowered = payload.lower()
    for spec in VERB_REGISTRY:
        m = re.match(spec.pattern, lowered, re.IGNORECASE)
        if m:
            args = {k: v for k, v in m.groupdict().items() if v is not None}
            return ParsedCommand(
                raw_subject=raw,
                verb=spec.name,
                tier=spec.tier,
                args=args,
                handler=spec.handler,
                payload=payload,
            )

    return ParsedCommand(raw_subject=raw, is_free_form=True, payload=payload)


def _classify_body_verdict(body: str | None) -> str:
    """Classify the verdict from an approval reply body.

    Returns one of APPROVE, DENY, IRREVERSIBLE, UNCLEAR.

    Strategy: walk the body line by line, stopping at the first
    quoted-block boundary (Gmail "On ... wrote:" attribution, Apple
    "Begin forwarded message:", Outlook "-----Original Message-----",
    or a `>`-prefixed quoted line). Of the lines BEFORE that boundary,
    take the first non-blank one and match it against the synonym
    sets. Tier-4 IRREVERSIBLE requires the literal uppercase phrase
    `YES IRREVERSIBLE` as its own line.

    Strict match: extra trailing words on the verdict line make it
    UNCLEAR. The synonym list is the curated allowlist of phrases an
    operator might naturally type. Anything else routes to UNCLEAR so
    the daemon can prompt for a clearer verdict instead of guessing.
    """
    line = _first_nonquoted_line(body)
    if line is None:
        return "UNCLEAR"
    # Tier-4 confirm is case-sensitive on purpose; lowercase "yes
    # irreversible" must NOT pass.
    if line == _TIER4_CONFIRM:
        return "IRREVERSIBLE"
    folded = line.lower()
    if folded in _APPROVE_PHRASES:
        return "APPROVE"
    if folded in _DENY_PHRASES:
        return "DENY"
    return "UNCLEAR"


def _first_nonquoted_line(body: str | None) -> str | None:
    """Return the first non-empty, non-quoted line of an email body.

    Lines are stripped of trailing whitespace before matching but the
    raw line is returned (so the tier-4 uppercase check sees the
    original casing). Returns None if no such line exists before a
    quoted-block boundary.
    """
    if not body:
        return None
    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if line == _SIG_SEPARATOR:
            return None
        if line.startswith(">"):
            return None
        if _QUOTE_ATTRIBUTION_RE.match(line):
            return None
        if line.strip() == "":
            continue
        return line.strip()
    return None


def describe_grammar() -> str:
    """Operator-facing reference. Used in the 'didn't recognise this' reply.

    Lists the recognised verbs by tier. Stable enough to inline in
    notifier replies; if we add a verb, this updates automatically
    because it reads the registry.

    The subject format is `[123456] <verb>`: code prefix, then the
    verb as the entire rest of the subject, no decorative lead-in.
    """
    by_tier: dict[int, list[VerbSpec]] = {}
    for spec in VERB_REGISTRY:
        by_tier.setdefault(spec.tier, []).append(spec)
    tier_label = {
        1: "Tier 1 (auto-execute):",
        2: "Tier 2 (single approval):",
        4: "Tier 4 (double-confirm: reply YES IRREVERSIBLE):",
    }
    lines = ["Subject format: [code] <verb>", ""]
    for tier in sorted(by_tier):
        lines.append(tier_label.get(tier, f"Tier {tier}:"))
        for spec in by_tier[tier]:
            lines.append(f"  - {spec.name}")
    return "\n".join(lines)
