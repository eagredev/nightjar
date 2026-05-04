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
classified as `free_form`. The watcher will then send the operator a
deterministic "interpret with LLM?" prompt rather than silently
invoking Claude.
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
    # Tier 4 verbs: queued, double-confirm. The principal must reply
    # "YES IRREVERSIBLE" (uppercase, exact phrase) with the matching
    # token. add and remove rewrite nightjar.conf, which is an
    # authentication-surface change, so they sit at tier 4 by code.
    VerbSpec(
        name="add",
        tier=4,
        # Email is the args. We accept anything with an @ and a dot to
        # let the executor do the strict parse, since RFC 5322 is wide.
        pattern=r"^add\s+(?P<email>\S+@\S+\.\S+)\s*$",
        handler="add",
    ),
    VerbSpec(
        name="remove",
        tier=4,
        pattern=r"^remove\s+(?P<contact>\S+)\s*$",
        handler="remove",
    ),
)


# Approval verdict words. The principal types these as the entire
# subject (after the [123456] prefix) when responding to a [Nightjar
# #abc123] approval ping. Strict matching: any decoration around the
# word, or any extra trailing words, classifies as a free-form reply
# instead of a verdict. This prevents accidental approval from a
# principal who quoted the previous email's body.
_APPROVE_WORDS = ("yes", "approve", "go")
_DENY_WORDS = ("no", "deny", "stop")
# Tier-4 double-confirm phrase. UPPERCASE EXACT, no leading code is
# stripped before this match (so the strict-uppercase test catches
# sloppy approvals).
_TIER4_CONFIRM = "YES IRREVERSIBLE"


# Approval-token subjects look like:  re: [Nightjar #a4f2c1] approval needed
# Token is hex, 6+ chars; we don't enforce a fixed length so future tokens
# can grow without changing the parser.
_APPROVAL_TOKEN_RE = re.compile(
    r"\[\s*Nightjar\s*#(?P<token>[a-f0-9]{6,})\s*\]",
    re.IGNORECASE,
)

# The TOTP/HOTP prefix is normally stripped by the auth layer, but the
# parser tolerates a leftover [123456] in case the caller forgets.
_LEADING_CODE_RE = re.compile(r"^\s*\[\d{6}\]\s*")

# Common reply prefixes. Stripped before approval-token detection.
_REPLY_PREFIX_RE = re.compile(r"^(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)


# ---- ParsedCommand --------------------------------------------------------


@dataclass(frozen=True)
class ParsedCommand:
    """The structured outcome of parsing one principal email.

    Exactly one classification is populated:
      - verb + tier + args: a recognised tier-1+ verb
      - approval_token + approval_verdict: a reply to a pending
        approval ping. verdict is one of APPROVE, DENY, IRREVERSIBLE,
        UNCLEAR (token recognised but the verdict word didn't match).
      - interpret_choice: the principal replied to a free-form
        clarification with "yes interpret" or "no". Surfaces as
        INTERPRET, NO_INTERPRET, or None. Mutually exclusive with
        verb / approval_token because they live in different reply
        threads.
      - is_free_form=True: anything else, deferred to the LLM gate
    """
    raw_subject: str
    verb: str | None = None
    tier: int | None = None
    args: dict[str, str] = field(default_factory=dict)
    approval_token: str | None = None
    approval_verdict: str | None = None  # APPROVE | DENY | IRREVERSIBLE | UNCLEAR
    interpret_choice: str | None = None  # INTERPRET | NO_INTERPRET
    is_free_form: bool = False
    handler: str | None = None
    # The subject after stripping the code prefix and "Nightjar," lead-in.
    # Useful for logging and for the LLM prompt if interpretation is
    # later authorised.
    payload: str = ""


# ---- Public parser --------------------------------------------------------


def parse_principal_command(subject: str | None) -> ParsedCommand:
    """Parse a principal-mail subject into a structured command.

    Body is intentionally not consulted at this stage: the entire
    grammar lives in the subject. This keeps the parser cheap, makes
    the threat surface narrow (a contact can't smuggle commands by
    quoting them in a body Nightjar later parses), and matches how
    operators interact with the daemon from a phone keyboard.
    """
    raw = subject or ""

    # Strip a single leading reply/forward prefix BEFORE checking for
    # approval tokens, so "Re: [Nightjar #abc123] ..." still surfaces
    # the token. We need the un-stripped version for token detection
    # though, since "[Nightjar #abc123]" might be later in the subject.
    no_reply_prefix = _REPLY_PREFIX_RE.sub("", raw, count=1)

    token_match = _APPROVAL_TOKEN_RE.search(no_reply_prefix)
    if token_match:
        # Verdict-word extraction: the principal's verdict is the rest of
        # the subject after the [Nightjar #token] tag is stripped, plus
        # any leading [123456] code. We classify into APPROVE / DENY /
        # IRREVERSIBLE / UNCLEAR. UNCLEAR is preserved (rather than
        # falling back to free-form) because the resolver needs to email
        # the principal a "your reply didn't parse" hint instead of the
        # generic free-form prompt.
        token = token_match.group("token").lower()
        # Remove the [Nightjar #token] tag, then strip leading code.
        leftover = _APPROVAL_TOKEN_RE.sub("", no_reply_prefix, count=1)
        leftover = _LEADING_CODE_RE.sub("", leftover).strip()
        verdict = _classify_verdict(leftover)
        return ParsedCommand(
            raw_subject=raw,
            approval_token=token,
            approval_verdict=verdict,
            payload=no_reply_prefix.strip(),
        )

    # Strip the leading [123456] code if the auth layer left it on. (It
    # normally doesn't, but defensive against future refactors.)
    stripped = _LEADING_CODE_RE.sub("", raw)
    payload = stripped.strip()

    if not payload:
        return ParsedCommand(raw_subject=raw, is_free_form=True, payload="")

    # Free-form clarification reply: "yes interpret" or "no" arrives as
    # the entire post-code subject after the principal replies to the
    # interpret-with-LLM prompt. We catch these BEFORE the verb registry
    # because "no" would otherwise hit the free-form fallback. Strict
    # match: trailing decoration disqualifies, same as verbs.
    lowered = payload.lower()
    if lowered == "yes interpret":
        return ParsedCommand(
            raw_subject=raw,
            interpret_choice="INTERPRET",
            payload=payload,
        )
    if lowered == "no":
        return ParsedCommand(
            raw_subject=raw,
            interpret_choice="NO_INTERPRET",
            payload=payload,
        )

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


def _classify_verdict(leftover: str) -> str:
    """Classify the post-token text of an approval reply.

    Returns one of APPROVE, DENY, IRREVERSIBLE, UNCLEAR. The leftover
    has already had Re:, the [Nightjar #token] tag, and the [123456]
    code stripped, so what remains should be just the verdict word.

    Strict match: extra trailing words make it UNCLEAR. The principal
    might think "yes please" is approval, but we want explicit single
    verdict words to keep the audit trail clean.
    """
    if not leftover:
        return "UNCLEAR"
    # Tier-4 confirm is case-sensitive on purpose; lowercase "yes
    # irreversible" must NOT pass.
    if leftover == _TIER4_CONFIRM:
        return "IRREVERSIBLE"
    word = leftover.lower()
    if word in _APPROVE_WORDS:
        return "APPROVE"
    if word in _DENY_WORDS:
        return "DENY"
    return "UNCLEAR"


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
