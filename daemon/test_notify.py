"""Temporary CLI for live-validating daemon/notifier.py.

Used by Build Step 3's live test. Strip after the notifier is exercised
by the real triage flow (Build Step 5 or so), unless the operator wants
to keep it as a maintenance tool.

Usage:
    nightjar --test-notify --principal
    nightjar --test-notify --contact CONTACT_ID

The principal mode sends one email to the configured principal address
with no footer or audit. The contact mode sends one email to a given
contact's first address, with footer + audit. Both refuse to run unless
[smtp] is configured.

Contact mode includes a confirmation prompt for safety; we do NOT want
to accidentally email a real third party during testing. The operator
must type the contact's display name verbatim before the send fires.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from .config import Config
from .log import JSONLLogger
from . import notifier
from .state import State


def _test_principal_send(config: Config) -> int:
    if config.smtp is None:
        print("nightjar: --test-notify needs [smtp] in nightjar.conf.", file=sys.stderr)
        return 2

    principal = next(
        (c for c in config.contacts.values() if c.is_principal), None
    )
    if principal is None or not principal.addresses:
        print("nightjar: no principal contact configured.", file=sys.stderr)
        return 2

    state = State(db_path=config.daemon.state_dir / "state.db")
    jlogger = JSONLLogger(log_dir=config.daemon.log_dir)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = (
        "This is a test email from `nightjar --test-notify --principal`.\n"
        "If you're reading this, the SMTP path to the principal works.\n"
        f"\nSent at: {now}\n"
    )
    result = notifier.notify_principal(
        smtp=config.smtp,
        principal_addr=principal.addresses[0],
        subject="Nightjar test: notify-principal smoke",
        body=body,
        jlogger=jlogger,
    )
    jlogger.close()
    if result.primary_sent:
        print(f"sent OK to {principal.addresses[0]} (id {result.primary_message_id}).")
        return 0
    print(f"send failed: {result.error}", file=sys.stderr)
    return 1


def _test_contact_send(config: Config, contact_id: str) -> int:
    if config.smtp is None:
        print("nightjar: --test-notify needs [smtp] in nightjar.conf.", file=sys.stderr)
        return 2

    contact = config.contacts.get(contact_id)
    if contact is None:
        print(
            f"nightjar: unknown contact {contact_id!r}. "
            f"Known: {', '.join(sorted(config.contacts))}",
            file=sys.stderr,
        )
        return 2
    if contact.is_principal:
        print(
            "nightjar: --contact targets a third party; "
            "use --principal to test the principal path.",
            file=sys.stderr,
        )
        return 2
    principal = next(
        (c for c in config.contacts.values() if c.is_principal), None
    )
    if principal is None or not principal.addresses:
        print("nightjar: no principal contact configured.", file=sys.stderr)
        return 2

    target_addr = contact.addresses[0]

    # Safety prompt: the operator must type the display name verbatim.
    # Catches accidental real-contact sends during testing.
    print()
    print(f"You are about to send a test email to a real third party:")
    print(f"  contact:  {contact.contact_id}")
    print(f"  name:     {contact.display_name}")
    print(f"  address:  {target_addr}")
    print()
    print("An audit copy will be sent to the principal.")
    print(f"Type the display name ({contact.display_name!r}) to confirm:")
    try:
        typed = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("aborted.", file=sys.stderr)
        return 1
    if typed != contact.display_name:
        print("aborted: display name mismatch.", file=sys.stderr)
        return 1

    state = State(db_path=config.daemon.state_dir / "state.db")
    jlogger = JSONLLogger(log_dir=config.daemon.log_dir)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = (
        f"Hi {contact.display_name},\n"
        "\n"
        "This is a test message from Nightjar's notifier path.\n"
        f"Sent at: {now}\n"
    )
    result = notifier.send_to_contact(
        smtp=config.smtp,
        state=state,
        principal_addr=principal.addresses[0],
        contact_addr=target_addr,
        subject="Nightjar test: contact channel",
        body=body,
        jlogger=jlogger,
    )
    jlogger.close()

    print()
    print(f"primary -> {target_addr}: {'sent' if result.primary_sent else 'FAILED'}")
    print(f"audit   -> {principal.addresses[0]}: ", end="")
    if result.audit_sent:
        print("sent")
    elif result.audit_queued:
        print(f"queued (audit_id={result.audit_id})")
    else:
        print("not attempted")
    if result.error:
        print(f"error: {result.error}")
    return 0 if result.primary_sent else 1


def run(config: Config | None, *, principal: bool, contact: str | None) -> int:
    if config is None:
        print("nightjar: --test-notify requires a valid config", file=sys.stderr)
        return 2
    if principal:
        return _test_principal_send(config)
    if contact:
        return _test_contact_send(config, contact)
    print(
        "nightjar: --test-notify requires --principal or --contact CONTACT_ID",
        file=sys.stderr,
    )
    return 2
