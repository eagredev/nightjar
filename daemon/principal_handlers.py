"""Tier-1 command handlers for principal mail.

Each handler returns (subject, body) ready to feed into
`notifier.notify_principal`. Handlers are pure-ish: they read from
config, state, and the log directory, but they don't mutate anything
or call out to the network.

The dispatch table at the bottom maps the symbolic handler names from
`principal_commands.VERB_REGISTRY` to the actual functions. The watcher
calls `dispatch(command, config, state)` and gets back the reply
content; it owns the transport.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from .config import Config
from .principal_commands import ParsedCommand
from .state import State


# Reply subject prefix. Mirrors the inbound convention so threading is
# stable in the operator's mail client.
REPLY_SUBJECT_PREFIX = "Nightjar:"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _subject(verb: str, summary: str = "") -> str:
    if summary:
        return f"{REPLY_SUBJECT_PREFIX} {verb} - {summary}"
    return f"{REPLY_SUBJECT_PREFIX} {verb}"


# ---- status ----------------------------------------------------------------


def handle_status(*, config: Config, state: State, args: dict[str, str]) -> tuple[str, str]:
    """Daemon-health snapshot. Read-only, cheap, never blocks.

    Reports: current time, last heartbeat (and gap from now), panic
    state, configured inboxes/contacts counts, message-state breakdown,
    pending audit retries, HOTP counter.
    """
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    last_hb = state.last_heartbeat()
    hb_age = (now_ts - last_hb) if last_hb is not None else None
    panic = state.panic_info()
    counts = state.count_by_state()

    lines = [
        f"Nightjar status @ {_now_iso()}",
        "",
        f"Daemon:",
        f"  last heartbeat:   {datetime.datetime.fromtimestamp(last_hb, tz=datetime.timezone.utc).isoformat() if last_hb else '(none)'}",
        f"  age (seconds):    {hb_age if hb_age is not None else '(none)'}",
        f"  panic state:      {'TRIPPED — ' + (panic['reason'] or '?') if panic else 'clear'}",
        "",
        f"Config:",
        f"  inboxes:          {', '.join(config.inboxes.keys()) or '(none)'}",
        f"  contacts:         {len(config.contacts)} ({', '.join(sorted(config.contacts.keys()))})",
        f"  auth_mode:        {config.security.auth_mode if config.security else '(no [security])'}",
        f"  smtp configured:  {'yes' if config.smtp else 'no'}",
        "",
        f"Messages by state:",
    ]
    if not counts:
        lines.append("  (no messages on record)")
    else:
        for s in sorted(counts):
            lines.append(f"  {s:24s} {counts[s]}")
    lines += [
        "",
        f"Pending audit retries: {state.count_pending_audits()}",
        f"HOTP counter:          {state.get_hotp_counter()}",
    ]
    return _subject("status"), "\n".join(lines) + "\n"


# ---- list pending ----------------------------------------------------------


def handle_list_pending(*, config: Config, state: State, args: dict[str, str]) -> tuple[str, str]:
    """Pending approvals queue.

    Reports two things:
      1. Per-message pending state counts (AWAITING_APPROVAL etc.):
         how many inbound messages are mid-flow.
      2. Approval-queue rows: the actual queued tier-2+ verbs with
         their tokens, verbs, args, and time-to-expiry.

    Both views are useful: (1) is a quick sanity check the daemon is
    busy, (2) is the actionable list the principal can reply to.
    """
    counts = state.count_by_state()
    awaiting = counts.get("AWAITING_APPROVAL", 0)
    interpret = counts.get("INTERPRET_OFFERED", 0)
    pending_approvals = state.list_pending_approvals()

    lines = [
        f"Pending items @ {_now_iso()}",
        "",
        f"  message rows AWAITING_APPROVAL:   {awaiting}",
        f"  message rows INTERPRET_OFFERED:   {interpret}",
        f"  approval-queue rows:              {len(pending_approvals)}",
        "",
    ]
    if pending_approvals:
        lines.append("Pending approvals:")
        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        for row in pending_approvals:
            ttl_seconds = max(0, row["expires_at"] - now_ts)
            ttl_days = ttl_seconds / 86400
            lines.append(
                f"  #{row['token']}  tier {row['tier']}  {row['verb']}  "
                f"{row['args']}  expires_in {ttl_days:.1f}d"
            )
        lines.append("")
        lines.append(
            "Reply with subject:  [<code>] [Nightjar #<token>] yes"
        )
        lines.append(
            "  (tier-4 needs YES IRREVERSIBLE instead of yes)"
        )
    elif awaiting == 0 and interpret == 0:
        lines.append("Nothing pending.")
    return _subject("list pending"), "\n".join(lines) + "\n"


# ---- tail log --------------------------------------------------------------


_TAIL_LIMIT = 100


def handle_tail_log(*, config: Config, state: State, args: dict[str, str]) -> tuple[str, str]:
    """Tail of the JSONL log for the requested date (default: today).

    Returns the last `_TAIL_LIMIT` events, decoded enough to be
    readable but kept structured. Sensitive event payloads are not
    expected to land in the log in the first place; if they do, this
    handler does NOT redact (the operator already has the file).
    """
    date_str = args.get("date") or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    log_path = Path(config.daemon.log_dir) / f"nightjar-{date_str}.jsonl"

    if not log_path.exists():
        body = (
            f"No log file for {date_str}.\n"
            f"Looked at: {log_path}\n"
        )
        return _subject("tail log", date_str), body

    try:
        # Cheap tail: read all lines, take the last _TAIL_LIMIT. Logs
        # are small enough (~thousands of lines/day max) that this is
        # not worth optimising.
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        return _subject("tail log", date_str), f"Could not read log: {e}\n"

    tail = lines[-_TAIL_LIMIT:]
    body_lines = [
        f"Tail of {log_path.name} (last {len(tail)} of {len(lines)} lines):",
        "",
    ]
    for raw in tail:
        try:
            obj = json.loads(raw)
            ts = obj.get("ts", "?")
            event = obj.get("event", "?")
            level = obj.get("level", "info")
            extra = " ".join(
                f"{k}={v}" for k, v in obj.items()
                if k not in {"ts", "event", "level"}
            )
            body_lines.append(f"{ts}  {level:5s}  {event}  {extra}")
        except json.JSONDecodeError:
            body_lines.append(raw)
    return _subject("tail log", date_str), "\n".join(body_lines) + "\n"


# ---- show contact ----------------------------------------------------------


# Keys that must NEVER appear in a show_contact reply, even if the operator
# asks. The contact's record itself is config-side, not a problem; but the
# Contact dataclass doesn't carry passwords. Belt-and-braces: filter
# defensively in case the dataclass grows.
_REDACT_KEYS = frozenset({
    "password", "imap_password", "smtp_password", "totp_secret", "secret",
})


def handle_show_contact(*, config: Config, state: State, args: dict[str, str]) -> tuple[str, str]:
    contact_id = args.get("contact", "").strip()
    if not contact_id:
        return _subject("show contact"), "Usage: Nightjar, show contact <name>\n"
    contact = config.contacts.get(contact_id)
    if contact is None:
        known = ", ".join(sorted(config.contacts.keys())) or "(none)"
        return _subject("show contact"), f"No contact {contact_id!r}. Known: {known}\n"
    lines = [
        f"Contact: {contact.contact_id}",
        f"  display_name:  {contact.display_name}",
        f"  relationship:  {contact.relationship}",
        f"  is_principal:  {contact.is_principal}",
        f"  daily_limit:   {'unlimited' if contact.daily_limit < 0 else contact.daily_limit}",
        f"  addresses:     {', '.join(contact.addresses)}",
    ]
    return _subject("show contact", contact_id), "\n".join(lines) + "\n"


# ---- show notes ------------------------------------------------------------


def handle_show_notes(*, config: Config, state: State, args: dict[str, str]) -> tuple[str, str]:
    """Rapport notes file content. Placeholder until Step 7 ships notes."""
    contact_id = args.get("contact", "").strip()
    if not contact_id:
        body = "Rapport notes ship in Step 7. Usage will be: Nightjar, show notes <contact>\n"
        return _subject("show notes"), body
    if contact_id not in config.contacts:
        known = ", ".join(sorted(config.contacts.keys())) or "(none)"
        return _subject("show notes"), f"No contact {contact_id!r}. Known: {known}\n"
    body = (
        f"No rapport notes for {contact_id} yet.\n"
        "Note storage and the three-test rule ship with Step 7.\n"
    )
    return _subject("show notes", contact_id), body


# ---- dispatch --------------------------------------------------------------


HANDLERS = {
    "status": handle_status,
    "list_pending": handle_list_pending,
    "tail_log": handle_tail_log,
    "show_contact": handle_show_contact,
    "show_notes": handle_show_notes,
}


def dispatch(*, command: ParsedCommand, config: Config, state: State) -> tuple[str, str] | None:
    """Run the handler for a tier-1 command. Returns None if no match.

    The watcher calls this only after the parser has set
    command.tier == 1; we still defensive-check the handler exists in
    case the registry and HANDLERS dict drift.
    """
    if command.handler is None:
        return None
    handler = HANDLERS.get(command.handler)
    if handler is None:
        return None
    return handler(config=config, state=state, args=command.args)
