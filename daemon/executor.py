"""Post-approval action runner for tier-2+ principal verbs.

The watcher calls execute() exactly once per approval that has been
approved by the principal. The function dispatches to a per-verb
implementation, captures whatever the verb produced (a status line, a
modified file, an error), and returns an ExecutionResult that the
watcher then emails to the principal.

Failure modes are first-class: a verb that raises is captured rather
than crashing the watcher. The principal sees the failure in the reply
email; the audit trail records it. Tools that touch the world (file
writes, config rewrites) are responsible for their own atomicity.

Tier-4 verbs (add, remove) rewrite nightjar.conf via daemon.config_writer.
That module enforces atomic writes (tmp + fsync + rename) and post-
write validation, and it never leaves the original file in a torn or
invalid state. After a successful write the executor mutates the
in-memory Config dicts so the running daemon recognises the change
without a restart.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config as config_module
from . import config_writer
from . import notifier
from .config import Config
from .config_writer import AddRequest, ConfigWriteError
from .log import JSONLLogger
from .state import State


@dataclass(frozen=True)
class ExecutionResult:
    """The outcome of running one approved verb.

    `ok` is True if the verb completed without raising and reported
    success. `summary` is the human-readable single-line outcome that
    goes in the principal's email subject. `body` is the full report
    that goes in the email body.
    """
    ok: bool
    summary: str
    body: str


def execute(
    *,
    verb: str,
    args: dict,
    config: Config,
    state: State,
    now: int | None = None,
    config_path: Path | None = None,
    jlogger: JSONLLogger | None = None,
) -> ExecutionResult:
    """Dispatch one approved verb to its implementation.

    `config_path` is only consulted by verbs that mutate nightjar.conf
    (add, remove). It defaults to the live config path. Tests inject a
    tmp path so the live file is never touched.

    `jlogger` is consulted by verbs that send mail (`reply`) so the
    notifier can record send failures. None is fine for tests; the
    notifier will skip its own logging in that case.
    """
    fn = _DISPATCH.get(verb)
    if fn is None:
        return ExecutionResult(
            ok=False,
            summary=f"unknown verb '{verb}'",
            body=(
                f"No executor registered for verb '{verb}'.\n"
                "This indicates a code bug: a verb was queued for approval\n"
                "but its executor is missing. Check daemon/executor.py.\n"
            ),
        )
    cfg_path = config_path if config_path is not None else config_module.DEFAULT_CONFIG_PATH
    try:
        return fn(
            args=args, config=config, state=state,
            now=now or int(time.time()), config_path=cfg_path,
            jlogger=jlogger,
        )
    except Exception as e:
        return ExecutionResult(
            ok=False,
            summary=f"{verb} failed: {type(e).__name__}",
            body=(
                f"Executor for '{verb}' raised {type(e).__name__}: {e}\n"
                "\n"
                "Args:\n"
                f"  {args}\n"
            ),
        )


# ---- Executors -----------------------------------------------------------
# Each takes (args, config, state, now) and returns ExecutionResult.


def _exec_block(*, args: dict, config: Config, state: State, now: int, config_path: Path, jlogger: JSONLLogger | None = None) -> ExecutionResult:
    """Mark a contact as blocked. Idempotent: blocking an already-
    blocked contact is reported as a no-op rather than a failure."""
    contact_id = args.get("contact")
    if not contact_id:
        return ExecutionResult(
            ok=False,
            summary="block: missing 'contact' arg",
            body="Internal error: 'block' executor invoked without a contact arg.\n",
        )
    if contact_id not in config.contacts:
        return ExecutionResult(
            ok=False,
            summary=f"block: no contact '{contact_id}'",
            body=(
                f"No contact configured under id '{contact_id}'.\n"
                "Available contacts:\n"
                + "".join(f"  - {cid}\n" for cid in sorted(config.contacts))
            ),
        )
    newly = state.block_contact(
        contact_id=contact_id,
        reason="principal-issued block verb",
        at=now,
    )
    if newly:
        return ExecutionResult(
            ok=True,
            summary=f"blocked '{contact_id}'",
            body=(
                f"Contact '{contact_id}' is now blocked. Inbound mail from\n"
                "this contact will be DROPPED until you issue 'unblock'.\n"
                "The block is held in state.db, not in nightjar.conf, so\n"
                "lifting it does not require a daemon restart.\n"
            ),
        )
    return ExecutionResult(
        ok=True,
        summary=f"already blocked: '{contact_id}'",
        body=f"Contact '{contact_id}' was already blocked. No change.\n",
    )


def _exec_unblock(*, args: dict, config: Config, state: State, now: int, config_path: Path, jlogger: JSONLLogger | None = None) -> ExecutionResult:
    """Lift a contact's block. Reports clearly if the contact wasn't
    blocked in the first place, since 'unblock' on a non-blocked
    contact is almost always a typo."""
    contact_id = args.get("contact")
    if not contact_id:
        return ExecutionResult(
            ok=False,
            summary="unblock: missing 'contact' arg",
            body="Internal error: 'unblock' executor invoked without a contact arg.\n",
        )
    removed = state.unblock_contact(contact_id=contact_id)
    if removed:
        return ExecutionResult(
            ok=True,
            summary=f"unblocked '{contact_id}'",
            body=(
                f"Contact '{contact_id}' is no longer blocked. Subsequent\n"
                "mail from this contact will be processed normally.\n"
            ),
        )
    return ExecutionResult(
        ok=True,
        summary=f"not blocked: '{contact_id}'",
        body=(
            f"Contact '{contact_id}' was not blocked. Nothing to lift.\n"
            "(This may be a typo. Use 'list pending' or 'show contact' to\n"
            "verify.)\n"
        ),
    )


def _exec_forget(*, args: dict, config: Config, state: State, now: int, config_path: Path, jlogger: JSONLLogger | None = None) -> ExecutionResult:
    """Delete a contact's rapport-notes file.

    Notes live at config.daemon.notes_dir / <contact>.md. The notes
    system itself ships in Step 7; this executor is forward-compatible
    with that (it deletes whatever's there) and gracefully no-ops if
    the directory or file doesn't exist yet. We do NOT shred the file:
    git-style overwrite-with-zeros isn't meaningful on a journalled
    filesystem and would mislead the principal about what 'forget'
    guarantees.
    """
    contact_id = args.get("contact")
    if not contact_id:
        return ExecutionResult(
            ok=False,
            summary="forget: missing 'contact' arg",
            body="Internal error: 'forget' executor invoked without a contact arg.\n",
        )
    if contact_id not in config.contacts:
        return ExecutionResult(
            ok=False,
            summary=f"forget: no contact '{contact_id}'",
            body=(
                f"No contact configured under id '{contact_id}'. The verb\n"
                "operates on configured contacts only; use 'remove' to\n"
                "delete a contact entirely.\n"
            ),
        )
    notes_path = config.daemon.notes_dir / f"{contact_id}.md"
    if not notes_path.exists():
        return ExecutionResult(
            ok=True,
            summary=f"forget '{contact_id}': no notes file",
            body=(
                f"No notes file at {notes_path}. Nothing to delete.\n"
                "(Rapport notes are populated by Step 7; before that\n"
                "ships, 'forget' has nothing to act on.)\n"
            ),
        )
    try:
        notes_path.unlink()
    except OSError as e:
        return ExecutionResult(
            ok=False,
            summary=f"forget '{contact_id}': delete failed",
            body=f"Could not delete {notes_path}: {e}\n",
        )
    return ExecutionResult(
        ok=True,
        summary=f"forgot '{contact_id}'",
        body=(
            f"Deleted {notes_path}.\n"
            "\n"
            "Note: this removes rapport notes only. The contact's entry in\n"
            "nightjar.conf, message history in state.db, and JSONL log\n"
            "lines remain. Use 'remove' for full removal.\n"
        ),
    )


# Contact_id derivation: take the local-part of the email, lowercase,
# strip non-[a-zA-Z0-9_-] chars. If the result collides with an
# existing contact_id, suffix -2, -3, ... until unique.
_CONTACT_ID_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _derive_contact_id(email: str, existing: dict) -> str:
    """Pick a contact_id from an email's local-part, avoiding collisions
    with any contact_id already in `existing` (a dict-like)."""
    local = email.split("@", 1)[0].lower()
    base = _CONTACT_ID_SAFE_RE.sub("", local) or "contact"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _pick_inbox_for_add(config: Config) -> str | None:
    """Pick which [inbox:*] gets the new contact added to its
    allowed_contacts. v1 has only one enabled inbox, so the choice is
    unambiguous; if ever there are multiple we pick the lexicographically
    first to keep the behaviour deterministic and document the limit."""
    if not config.inboxes:
        return None
    return sorted(config.inboxes.keys())[0]


def _exec_add(*, args: dict, config: Config, state: State, now: int, config_path: Path, jlogger: JSONLLogger | None = None) -> ExecutionResult:
    """Add a new contact: append a [contact:*] block to nightjar.conf,
    extend the inbox's allowed_contacts list, and refresh the in-memory
    Config so the daemon picks up the change without a restart.

    daily_limit defaults to 3 (same as the example.conf default for
    non-principal contacts). The principal can edit nightjar.conf
    directly to raise it later.
    """
    email = args.get("email", "").strip()
    if not email or "@" not in email:
        return ExecutionResult(
            ok=False,
            summary="add: malformed email",
            body=f"'add' executor invoked with non-email arg: {email!r}\n",
        )
    inbox_name = _pick_inbox_for_add(config)
    if inbox_name is None:
        return ExecutionResult(
            ok=False,
            summary="add: no inbox configured",
            body="No [inbox:*] sections in nightjar.conf; cannot add contact.\n",
        )
    if email.lower() in config.address_index:
        existing_id = config.address_index[email.lower()]
        return ExecutionResult(
            ok=False,
            summary=f"add: address already exists as '{existing_id}'",
            body=(
                f"Address {email!r} is already configured under contact_id "
                f"'{existing_id}'. No change made.\n"
            ),
        )
    contact_id = _derive_contact_id(email, config.contacts)
    request = AddRequest(
        contact_id=contact_id,
        address=email,
        display_name=contact_id.capitalize(),
        relationship="added via principal verb",
        daily_limit=3,
        inbox_name=inbox_name,
    )
    try:
        config_writer.add_contact(request=request, config=config, config_path=config_path)
    except ConfigWriteError as e:
        return ExecutionResult(
            ok=False,
            summary=f"add: write failed",
            body=f"Could not add contact: {e}\n",
        )
    config_writer.apply_add(request=request, config=config)
    return ExecutionResult(
        ok=True,
        summary=f"added '{contact_id}' ({email})",
        body=(
            f"Added contact_id '{contact_id}' for {email}.\n"
            f"  display_name: {request.display_name}\n"
            f"  daily_limit:  {request.daily_limit}\n"
            f"  inbox:        {inbox_name}\n"
            "\n"
            "The new contact is live: nightjar.conf has been rewritten\n"
            "atomically and the daemon's in-memory config updated. No\n"
            "restart needed. Edit ~/.config/nightjar/nightjar.conf to\n"
            "tweak the display_name, relationship, or daily_limit.\n"
        ),
    )


def _exec_remove(*, args: dict, config: Config, state: State, now: int, config_path: Path, jlogger: JSONLLogger | None = None) -> ExecutionResult:
    """Remove a contact: strip its [contact:*] block from nightjar.conf,
    remove it from any allowed_contacts list, and refresh the in-memory
    Config. Refuses to remove the principal."""
    contact_id = args.get("contact", "").strip()
    if not contact_id:
        return ExecutionResult(
            ok=False,
            summary="remove: missing 'contact' arg",
            body="Internal error: 'remove' executor invoked without a contact arg.\n",
        )
    if contact_id not in config.contacts:
        return ExecutionResult(
            ok=False,
            summary=f"remove: no contact '{contact_id}'",
            body=(
                f"No contact configured under id '{contact_id}'.\n"
                "Available contacts:\n"
                + "".join(f"  - {cid}\n" for cid in sorted(config.contacts))
            ),
        )
    if config.contacts[contact_id].is_principal:
        return ExecutionResult(
            ok=False,
            summary=f"remove: refused on principal '{contact_id}'",
            body=(
                f"Refusing to remove '{contact_id}': it is the principal\n"
                "contact, which is the daemon's only authenticated identity.\n"
                "Removing it would lock the operator out. Edit nightjar.conf\n"
                "manually if you really mean to do this.\n"
            ),
        )
    addresses = config.contacts[contact_id].addresses
    try:
        config_writer.remove_contact(contact_id=contact_id, config=config, config_path=config_path)
    except ConfigWriteError as e:
        return ExecutionResult(
            ok=False,
            summary="remove: write failed",
            body=f"Could not remove contact: {e}\n",
        )
    config_writer.apply_remove(contact_id=contact_id, config=config)
    return ExecutionResult(
        ok=True,
        summary=f"removed '{contact_id}'",
        body=(
            f"Removed contact_id '{contact_id}' (addresses: "
            f"{', '.join(addresses)}).\n"
            "\n"
            "nightjar.conf has been rewritten atomically and the daemon's\n"
            "in-memory config updated. Subsequent mail from this address\n"
            "will be DROPPED as a stranger. To also wipe rapport notes,\n"
            "the 'forget' verb must be issued before 'remove' (notes are\n"
            "keyed by contact_id, which no longer exists).\n"
        ),
    )


def _exec_reply(*, args: dict, config: Config, state: State, now: int, config_path: Path, jlogger: JSONLLogger | None = None) -> ExecutionResult:
    """Send a triage-drafted reply to a contact.

    This executor is the post-approval terminus of the contact-triage
    flow. The watcher queued a tier-3 approval with these args:
      - contact_id: str. Resolved at queue time from the inbound
        message's sender. We re-resolve it here as a sanity check.
      - body: str. The plaintext reply the LLM drafted, already
        validated by daemon.triage. The notifier appends the standard
        contact footer; we don't add it here.
      - in_reply_to: str | None. Optional Message-ID for proper
        threading.
      - subject: str. The subject line for the reply.

    Sends through `notifier.send_to_contact`, which handles both the
    primary send and the audit copy. On primary failure we report
    EXECUTION_FAILED so the principal sees that no mail reached the
    contact. On audit-only failure we still report ok=True (primary
    delivered) and the audit retry loop drains the queue.
    """
    contact_id = args.get("contact_id")
    body = args.get("body")
    subject = args.get("subject", "")
    in_reply_to = args.get("in_reply_to")

    if not contact_id:
        return ExecutionResult(
            ok=False,
            summary="reply: missing 'contact_id' arg",
            body="Internal error: 'reply' executor invoked without a contact_id.\n",
        )
    if not body or not isinstance(body, str) or not body.strip():
        return ExecutionResult(
            ok=False,
            summary="reply: missing or empty 'body' arg",
            body="Internal error: 'reply' executor invoked without a body.\n",
        )
    if contact_id not in config.contacts:
        return ExecutionResult(
            ok=False,
            summary=f"reply: no contact '{contact_id}'",
            body=(
                f"Contact '{contact_id}' is no longer configured. The reply\n"
                "was approved earlier but the contact has since been\n"
                "removed. No mail was sent.\n"
            ),
        )
    contact = config.contacts[contact_id]
    if not contact.addresses:
        return ExecutionResult(
            ok=False,
            summary=f"reply: contact '{contact_id}' has no addresses",
            body="Internal error: contact has no addresses to reply to.\n",
        )

    # Reply to the first address (canonical). If the contact has
    # multiple addresses they're aliases; sending to the canonical one
    # is the operator-supplied default.
    contact_addr = contact.addresses[0]

    # Find the principal address for the audit copy.
    principal = next(
        (c for c in config.contacts.values() if c.is_principal), None
    )
    if principal is None or not principal.addresses:
        return ExecutionResult(
            ok=False,
            summary="reply: no principal configured for audit",
            body="Internal error: cannot send reply because no principal is\n"
                 "configured to receive the audit copy.\n",
        )

    reply_subject = subject or f"Re: (no subject)"

    result = notifier.send_to_contact(
        smtp=config.smtp,
        state=state,
        principal_addr=principal.addresses[0],
        contact_addr=contact_addr,
        subject=reply_subject,
        body=body,
        jlogger=jlogger,
        in_reply_to=in_reply_to,
        related_message_id=in_reply_to,
    )

    if not result.primary_sent:
        return ExecutionResult(
            ok=False,
            summary=f"reply: send to '{contact_id}' failed",
            body=(
                f"Failed to send reply to {contact_addr}.\n"
                f"Reason: {result.error}\n"
                "\n"
                "The audit copy may still have been attempted; check the\n"
                "principal's inbox for a (SEND FAILED) banner.\n"
            ),
        )

    audit_note = ""
    if not result.audit_sent:
        audit_note = (
            "\n"
            "Note: the audit copy did NOT reach the principal inbox.\n"
            "It has been queued in pending_audits and will retry.\n"
        )

    return ExecutionResult(
        ok=True,
        summary=f"replied to '{contact_id}'",
        body=(
            f"Reply sent to {contact_addr} (subject: {reply_subject!r}).\n"
            f"{audit_note}"
        ),
    )


def _exec_forward(*, args: dict, config: Config, state: State, now: int, config_path: Path, jlogger: JSONLLogger | None = None) -> ExecutionResult:
    """Forward a triage-flagged email to the principal as an attachment.

    This executor is the post-approval terminus of the forward-to-principal
    path. The watcher queued a tier-3 approval with these args:
      - contact_id: str. The contact whose email we're forwarding.
        Looked up here for the wrapper body framing only; we never send
        anything to the contact.
      - subject: str. The Fwd:-prefixed wrapper subject.
      - raw_rfc822_b64: str. base64-encoded raw RFC822 bytes of the
        original message. Decoded here and attached verbatim.
      - summary, reasoning, risk_flags, notes: from the triage plan.
        Rendered into the wrapper body so the principal sees the LLM's
        reading at the top of the forward email.
      - in_reply_to: str | None. Original Message-ID for thread linkage.

    The raw bytes are stored in the approval row at queue time, so this
    executor needs no IMAP connection. After delivery the bytes remain
    in state.db until the approval row is deleted; that's acceptable
    because the approval window is bounded and the message would be
    stored in IMAP anyway.

    Failure semantics: a primary-send failure reports EXECUTION_FAILED.
    There is no audit copy: the principal IS the recipient.
    """
    import base64

    contact_id = args.get("contact_id")
    subject = args.get("subject", "")
    raw_b64 = args.get("raw_rfc822_b64")
    summary = args.get("summary", "")
    reasoning = args.get("reasoning", "")
    risk_flags = args.get("risk_flags") or []
    notes = args.get("notes", "")
    in_reply_to = args.get("in_reply_to")

    if not contact_id:
        return ExecutionResult(
            ok=False,
            summary="forward: missing 'contact_id' arg",
            body="Internal error: 'forward_to_principal' invoked without a contact_id.\n",
        )
    if not raw_b64 or not isinstance(raw_b64, str):
        return ExecutionResult(
            ok=False,
            summary="forward: missing raw message",
            body="Internal error: 'forward_to_principal' invoked without "
                 "raw_rfc822_b64. The raw bytes should have been stored at "
                 "queue time.\n",
        )
    try:
        raw_rfc822 = base64.b64decode(raw_b64.encode("ascii"))
    except Exception as e:
        return ExecutionResult(
            ok=False,
            summary="forward: corrupt raw message",
            body=f"Internal error: raw_rfc822_b64 failed to decode: {e}\n",
        )
    if not raw_rfc822:
        return ExecutionResult(
            ok=False,
            summary="forward: empty raw message",
            body="Internal error: raw_rfc822 decoded to zero bytes.\n",
        )

    # The contact may have been removed between queue and approval.
    # Forwarding still makes sense (the bytes are in the args), so we
    # do not refuse, but we do degrade the wrapper body to flag it.
    contact = config.contacts.get(contact_id)
    contact_label = (
        f"{contact.display_name} <{contact.addresses[0]}>"
        if contact and contact.addresses
        else f"{contact_id} (no longer configured)"
    )

    principal = next(
        (c for c in config.contacts.values() if c.is_principal), None
    )
    if principal is None or not principal.addresses:
        return ExecutionResult(
            ok=False,
            summary="forward: no principal configured",
            body="Internal error: cannot forward because no principal is\n"
                 "configured to receive the forwarded mail.\n",
        )

    flags_line = (
        ", ".join(risk_flags) if risk_flags else "(none)"
    )
    notes_block = f"\nNotes from triage:\n  {notes}\n" if notes else ""
    wrapper_body = (
        f"Forwarded from {contact_label} on the principal's behalf.\n"
        f"\n"
        f"Triage summary:\n  {summary}\n"
        f"\n"
        f"Reasoning:\n  {reasoning}\n"
        f"{notes_block}"
        f"Risk flags: {flags_line}\n"
        f"\n"
        f"The original message is attached as an .eml file. Open it in\n"
        f"your mail client to see the message exactly as it arrived,\n"
        f"including any HTML formatting, attachments, and inline images\n"
        f"the plain-text view used for triage could not show.\n"
    )

    result = notifier.forward_to_principal(
        smtp=config.smtp,
        state=state,
        principal_addr=principal.addresses[0],
        subject=subject or f"Fwd: from {contact_id}",
        wrapper_body=wrapper_body,
        raw_rfc822=raw_rfc822,
        jlogger=jlogger,
        related_message_id=in_reply_to,
    )

    if not result.primary_sent:
        return ExecutionResult(
            ok=False,
            summary=f"forward: send failed",
            body=(
                f"Failed to forward {contact_id}'s email to the principal.\n"
                f"Reason: {result.error}\n"
            ),
        )
    return ExecutionResult(
        ok=True,
        summary=f"forwarded '{contact_id}' to principal",
        body=(
            f"Forwarded {contact_id}'s email to {principal.addresses[0]}\n"
            f"({len(raw_rfc822)} bytes attached as .eml).\n"
        ),
    )


_DISPATCH: dict[str, Callable] = {
    "block": _exec_block,
    "unblock": _exec_unblock,
    "forget": _exec_forget,
    "add": _exec_add,
    "remove": _exec_remove,
    "reply": _exec_reply,
    "forward_to_principal": _exec_forward,
}
