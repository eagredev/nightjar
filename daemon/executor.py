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

Tier-4 verbs (add, remove) intentionally raise NotImplementedError
in this build step. They sit in the registry so the queue + double-
confirm flow can be exercised end-to-end against live mail; the
config-rewrite implementation lands in a follow-up step where it
gets its own atomic-write tests and round-trip validation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .config import Config
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
) -> ExecutionResult:
    """Dispatch one approved verb to its implementation."""
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
    try:
        return fn(args=args, config=config, state=state, now=now or int(time.time()))
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


def _exec_block(*, args: dict, config: Config, state: State, now: int) -> ExecutionResult:
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


def _exec_unblock(*, args: dict, config: Config, state: State, now: int) -> ExecutionResult:
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


def _exec_forget(*, args: dict, config: Config, state: State, now: int) -> ExecutionResult:
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


def _exec_add(*, args: dict, config: Config, state: State, now: int) -> ExecutionResult:
    """Add a new contact. Currently NotImplementedError: rewriting
    nightjar.conf safely (atomic write, comment preservation,
    round-trip parse validation) needs its own dedicated step."""
    return ExecutionResult(
        ok=False,
        summary="add not yet wired",
        body=(
            "The 'add' verb is registered as tier-4 but its executor is\n"
            "not yet implemented. Adding a new contact requires rewriting\n"
            "nightjar.conf, which we have intentionally deferred to a\n"
            "follow-up step where it gets its own atomic-write and round-\n"
            "trip-parse tests.\n"
            "\n"
            "For now, edit ~/.config/nightjar/nightjar.conf manually and\n"
            "restart the daemon.\n"
        ),
    )


def _exec_remove(*, args: dict, config: Config, state: State, now: int) -> ExecutionResult:
    """Remove a contact. Same deferral as add."""
    return ExecutionResult(
        ok=False,
        summary="remove not yet wired",
        body=(
            "The 'remove' verb is registered as tier-4 but its executor is\n"
            "not yet implemented. See 'add' for the rationale (config\n"
            "rewrite ships in a follow-up step).\n"
            "\n"
            "For now, edit ~/.config/nightjar/nightjar.conf manually,\n"
            "delete the [contact:*] block, and restart the daemon. To\n"
            "wipe rapport notes at the same time, use 'forget' first.\n"
        ),
    )


_DISPATCH: dict[str, Callable] = {
    "block": _exec_block,
    "unblock": _exec_unblock,
    "forget": _exec_forget,
    "add": _exec_add,
    "remove": _exec_remove,
}
