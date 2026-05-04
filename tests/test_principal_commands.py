"""Reply parser and tier-1 handler tests. No I/O beyond tmp_path."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from daemon.config import (
    Config, Contact, DaemonConfig, InboxConfig, SecurityConfig, SmtpConfig,
)
from daemon.principal_commands import (
    VERB_REGISTRY,
    describe_grammar,
    parse_principal_command,
)
from daemon.principal_handlers import HANDLERS, dispatch
from daemon.state import State


# ---- Parser ---------------------------------------------------------------


@pytest.mark.parametrize("subject,verb,expected_args", [
    ("Nightjar, status", "status", {}),
    ("nightjar, status", "status", {}),
    ("Nightjar: status", "status", {}),
    ("status", "status", {}),
    ("[123456] Nightjar, status", "status", {}),  # leftover code prefix tolerated
    ("Nightjar, list pending", "list pending", {}),
    ("Nightjar, tail log", "tail log", {}),
    ("Nightjar, tail log 2026-05-04", "tail log", {"date": "2026-05-04"}),
    ("Nightjar, show contact alice", "show contact", {"contact": "alice"}),
    ("Nightjar, show notes", "show notes", {}),
    ("Nightjar, show notes alice", "show notes", {"contact": "alice"}),
])
def test_parse_recognises_tier1_verbs(subject: str, verb: str, expected_args: dict) -> None:
    cmd = parse_principal_command(subject)
    assert cmd.verb == verb
    assert cmd.tier == 1
    assert cmd.args == expected_args
    assert cmd.is_free_form is False
    assert cmd.approval_token is None


def test_parse_approval_token_in_reply_subject() -> None:
    cmd = parse_principal_command("Re: [Nightjar #a4f2c1] approval needed")
    assert cmd.approval_token == "a4f2c1"
    assert cmd.verb is None
    assert cmd.is_free_form is False


def test_parse_approval_token_case_insensitive_and_token_lowered() -> None:
    cmd = parse_principal_command("re: [Nightjar #ABC123] approval needed")
    assert cmd.approval_token == "abc123"


def test_parse_unknown_subject_classifies_as_free_form() -> None:
    cmd = parse_principal_command("Hey, can you help me debug something?")
    assert cmd.is_free_form is True
    assert cmd.verb is None
    assert cmd.tier is None
    assert cmd.payload == "Hey, can you help me debug something?"


def test_parse_empty_subject_is_free_form() -> None:
    cmd = parse_principal_command(None)
    assert cmd.is_free_form is True
    cmd2 = parse_principal_command("")
    assert cmd2.is_free_form is True


def test_parse_strips_leading_code_and_lead_in() -> None:
    cmd = parse_principal_command("[123456] Nightjar, tail log 2026-05-04")
    assert cmd.verb == "tail log"
    assert cmd.args == {"date": "2026-05-04"}


def test_parse_show_contact_requires_arg() -> None:
    """Naked 'show contact' shouldn't match (we want the arg form)."""
    cmd = parse_principal_command("Nightjar, show contact")
    assert cmd.verb != "show contact"


def test_describe_grammar_lists_known_verbs() -> None:
    text = describe_grammar()
    for spec in VERB_REGISTRY:
        assert spec.name in text


# ---- Handlers -------------------------------------------------------------


def make_config(tmp_path: Path) -> Config:
    daemon = DaemonConfig(state_dir=tmp_path / "state", log_dir=tmp_path / "logs")
    daemon.state_dir.mkdir(parents=True)
    daemon.log_dir.mkdir(parents=True)
    contacts = {
        "principal": Contact(
            contact_id="principal",
            addresses=("me@example.com",),
            display_name="Operator",
            relationship="self",
            daily_limit=-1,
            is_principal=True,
        ),
        "composer": Contact(
            contact_id="composer",
            addresses=("composer@example.com",),
            display_name="Composer",
            relationship="collaborator",
            daily_limit=3,
            is_principal=False,
        ),
    }
    inbox = InboxConfig(
        name="nightjar",
        enabled=True,
        imap_host="imap.example.com",
        imap_port=993,
        imap_user="bot@example.com",
        imap_password="secret",
        allowed_contacts=("principal", "composer"),
    )
    security = SecurityConfig(
        totp_secret="JBSWY3DPEHPK3PXP",
        dead_mans_switch_window_minutes=60,
        dead_mans_switch_threshold=3,
    )
    smtp = SmtpConfig(
        host="smtp.example.com",
        port=587,
        user="bot@example.com",
        password="smtp-secret",
        from_name="Nightjar",
        from_addr="bot@example.com",
    )
    address_index = {"me@example.com": "principal", "composer@example.com": "composer"}
    return Config(
        daemon=daemon,
        contacts=contacts,
        inboxes={"nightjar": inbox},
        security=security,
        smtp=smtp,
        address_index=address_index,
    )


def make_state(tmp_path: Path) -> State:
    return State(db_path=tmp_path / "state.db")


def test_status_handler_runs_against_empty_state(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("Nightjar, status")
    subject, body = dispatch(command=cmd, config=cfg, state=s)
    assert subject.startswith("Nightjar:")
    assert "status" in subject
    assert "auth_mode:" in body
    assert "smtp configured:  yes" in body
    assert "panic state:      clear" in body


def test_status_handler_reports_panic_when_tripped(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    s.trip_panic(reason="3 invalid attempts", at=1_700_000_000)
    cmd = parse_principal_command("Nightjar, status")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "panic state:      TRIPPED" in body
    assert "3 invalid attempts" in body


def test_list_pending_reports_empty_when_nothing_queued(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("Nightjar, list pending")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "Nothing pending." in body


def test_list_pending_counts_messages_in_pending_states(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    s.record_message(
        message_id="<m1>",
        inbox="nightjar",
        from_addr="x@example.com",
        subject=None,
        contact_id=None,
        state="AWAITING_APPROVAL",
    )
    cmd = parse_principal_command("Nightjar, list pending")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "AWAITING_APPROVAL:   1" in body


def test_tail_log_returns_message_when_no_log(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("Nightjar, tail log 2099-01-01")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "No log file for 2099-01-01" in body


def test_tail_log_decodes_jsonl_lines(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    log_file = cfg.daemon.log_dir / "nightjar-2026-05-04.jsonl"
    log_file.write_text(
        '{"ts":"2026-05-04T01:00:00+00:00","event":"daemon_start","level":"info","inboxes":["nightjar"]}\n'
        '{"ts":"2026-05-04T01:00:01+00:00","event":"idle_connect","level":"info","inbox":"nightjar"}\n',
        encoding="utf-8",
    )
    s = make_state(tmp_path)
    cmd = parse_principal_command("Nightjar, tail log 2026-05-04")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "daemon_start" in body
    assert "idle_connect" in body
    assert "inboxes=['nightjar']" in body  # extra fields rendered


def test_show_contact_returns_known_contact(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("Nightjar, show contact composer")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "Contact: composer" in body
    assert "composer@example.com" in body
    assert "daily_limit:   3" in body


def test_show_contact_does_not_leak_passwords_or_secrets(tmp_path: Path) -> None:
    """The Contact dataclass doesn't carry creds, but verify the rendered
    output doesn't contain the SMTP password, IMAP password, or TOTP
    secret from the surrounding config."""
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("Nightjar, show contact composer")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "smtp-secret" not in body
    assert "JBSWY3DPEHPK3PXP" not in body
    assert cfg.inboxes["nightjar"].imap_password not in body


def test_show_contact_rejects_unknown_contact(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("Nightjar, show contact ghost")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "No contact 'ghost'" in body


def test_show_notes_placeholder_reports_step_7(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("Nightjar, show notes composer")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "Step 7" in body or "rapport notes" in body.lower()


def test_dispatch_returns_none_for_non_handler_command(tmp_path: Path) -> None:
    """A free-form parse has handler=None so dispatch returns None."""
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("totally not a verb")
    assert dispatch(command=cmd, config=cfg, state=s) is None


def test_handlers_dict_covers_all_tier1_registry_entries() -> None:
    """Every VerbSpec.handler has a matching HANDLERS entry."""
    for spec in VERB_REGISTRY:
        if spec.tier == 1:
            assert spec.handler in HANDLERS, (
                f"VERB_REGISTRY references handler {spec.handler!r} "
                "but HANDLERS dict has no entry"
            )
