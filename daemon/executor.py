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
from .config import Config
from .config_writer import AddRequest, ConfigWriteError
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
) -> ExecutionResult:
    """Dispatch one approved verb to its implementation.

    `config_path` is only consulted by verbs that mutate nightjar.conf
    (add, remove). It defaults to the live config path. Tests inject a
    tmp path so the live file is never touched.
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


def _exec_block(*, args: dict, config: Config, state: State, now: int, config_path: Path) -> ExecutionResult:
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


def _exec_unblock(*, args: dict, config: Config, state: State, now: int, config_path: Path) -> ExecutionResult:
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


def _exec_forget(*, args: dict, config: Config, state: State, now: int, config_path: Path) -> ExecutionResult:
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


def _exec_add(*, args: dict, config: Config, state: State, now: int, config_path: Path) -> ExecutionResult:
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


def _exec_remove(*, args: dict, config: Config, state: State, now: int, config_path: Path) -> ExecutionResult:
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


_DISPATCH: dict[str, Callable] = {
    "block": _exec_block,
    "unblock": _exec_unblock,
    "forget": _exec_forget,
    "add": _exec_add,
    "remove": _exec_remove,
}
