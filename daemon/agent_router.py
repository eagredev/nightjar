"""Body-first agent routing — classifies a principal email as
agent_init / agent_continuation / not_agent based on the first body
line and (for continuations) the In-Reply-To header.

This module is pure: no I/O, no state-db access. The watcher calls
classify() with the inputs it already has, gets back a classification,
and dispatches accordingly. Auth validation (HOTP verify + counter
advance) happens in the watcher with the codes returned here.

Why body-first and not a verb word: presence of valid HOTP codes IS
the verb. A subject word would be vestigial — the LLM reads the body,
not a verb name. Two HOTPs in a body's first line is a strong enough
signal on its own; one HOTP plus an active session reference suffices
for continuations. See conversation log 2026-05-06 for the design
discussion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# HOTP/TOTP codes are exactly 6 ASCII digits. Match anchored at start
# of body so "123456 654321" only counts when it's the literal first
# token sequence — not embedded in a sentence.
_INIT_RE = re.compile(r"^(\d{6})[ \t]+(\d{6})\s*$")
_CONTINUATION_RE = re.compile(r"^(\d{6})\s*$")


CLASS_NOT_AGENT = "not_agent"
CLASS_AGENT_INIT = "agent_init"
CLASS_AGENT_CONTINUATION = "agent_continuation"


@dataclass(frozen=True)
class AgentClassification:
    """Result of classify(). For agent_* classifications, the parsed
    fields are populated and ready to feed to the auth verifier; the
    request body has the auth line stripped so the caller can pass it
    straight to the executor."""
    kind: str  # one of CLASS_*
    primary_code: str | None = None
    secondary_code: str | None = None
    session_id: str | None = None  # only set on agent_continuation
    request_body: str = ""

    @property
    def is_agent(self) -> bool:
        return self.kind != CLASS_NOT_AGENT


def _first_line_and_rest(body: str) -> tuple[str, str]:
    """Split body into (first_line, rest). Rest preserves the original
    layout including the line break — only the first line is stripped
    away, since that's the auth line. The rest is what the principal
    actually wrote."""
    nl = body.find("\n")
    if nl == -1:
        return body, ""
    return body[:nl], body[nl + 1:]


def classify(
    *,
    body: str | None,
    in_reply_to: str | None,
    active_session_lookup,  # Callable[[str], str | None]: in_reply_to -> session_id
) -> AgentClassification:
    """Decide whether the email is an agent invocation.

    Args:
        body: The email body. None or empty → not_agent.
        in_reply_to: The In-Reply-To header value if present (raw,
            including angle brackets — the lookup is responsible for
            normalising).
        active_session_lookup: Function the watcher provides. Given an
            in_reply_to message-id string, returns the session_id of
            an in-progress agent session whose last_message_id matches,
            or None. Pure dependency injection so the parser stays
            free of state-db imports.

    Returns:
        AgentClassification. Caller should:
          - if kind == not_agent: fall through to existing auth.
          - if kind == agent_init: validate primary_code + secondary_code,
            advance both counters, dispatch executor with session_id=None.
          - if kind == agent_continuation: validate secondary_code,
            advance secondary counter, dispatch executor with
            session_id=<returned>.
    """
    if not body:
        return AgentClassification(kind=CLASS_NOT_AGENT)

    first_line, rest = _first_line_and_rest(body)
    first_line = first_line.rstrip("\r")

    # Init shape: two whitespace-separated 6-digit numbers, first line.
    init_m = _INIT_RE.match(first_line)
    if init_m is not None:
        return AgentClassification(
            kind=CLASS_AGENT_INIT,
            primary_code=init_m.group(1),
            secondary_code=init_m.group(2),
            request_body=rest,
        )

    # Continuation shape: one 6-digit number, first line, AND
    # in_reply_to identifies an active session.
    cont_m = _CONTINUATION_RE.match(first_line)
    if cont_m is not None and in_reply_to:
        # Strip whitespace from the In-Reply-To. Some MUAs add
        # surrounding whitespace.
        normalised_irt = in_reply_to.strip()
        session_id = active_session_lookup(normalised_irt)
        if session_id is not None:
            return AgentClassification(
                kind=CLASS_AGENT_CONTINUATION,
                secondary_code=cont_m.group(1),
                session_id=session_id,
                request_body=rest,
            )
        # First-line-looks-like-a-code BUT no matching session. Fall
        # through to not_agent — the principal probably typed a number
        # at the top of an unrelated reply. Don't burn DMS budget on
        # that.
        return AgentClassification(kind=CLASS_NOT_AGENT)

    return AgentClassification(kind=CLASS_NOT_AGENT)
