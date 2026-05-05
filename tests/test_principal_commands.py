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
    ("status", "status", {}),
    ("STATUS", "status", {}),
    ("[123456] status", "status", {}),  # leftover code prefix tolerated
    ("list pending", "list pending", {}),
    ("tail log", "tail log", {}),
    ("tail log 2026-05-04", "tail log", {"date": "2026-05-04"}),
    ("show contact alice", "show contact", {"contact": "alice"}),
    ("show notes", "show notes", {}),
    ("show notes alice", "show notes", {"contact": "alice"}),
])
def test_parse_recognises_tier1_verbs(subject: str, verb: str, expected_args: dict) -> None:
    cmd = parse_principal_command(subject)
    assert cmd.verb == verb
    assert cmd.tier == 1
    assert cmd.args == expected_args
    assert cmd.is_free_form is False
    assert cmd.approval_token is None


@pytest.mark.parametrize("subject", [
    "Nightjar, status",
    "nightjar: status",
    "Nightjar status",
])
def test_parse_rejects_decorative_lead_in(subject: str) -> None:
    """Strict matching: 'Nightjar,' prefix is not accepted. Falls through
    to free-form so the watcher hands it to the principal-interpret pass
    rather than silently matching a verb. See module docstring."""
    cmd = parse_principal_command(subject)
    assert cmd.is_free_form is True
    assert cmd.verb is None


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


def test_parse_strips_leading_code() -> None:
    cmd = parse_principal_command("[123456] tail log 2026-05-04")
    assert cmd.verb == "tail log"
    assert cmd.args == {"date": "2026-05-04"}


def test_parse_strips_bare_leading_code() -> None:
    """Bare `123456` (no brackets) is accepted as a prefix the same
    way as the bracketed form."""
    cmd = parse_principal_command("123456 tail log 2026-05-04")
    assert cmd.verb == "tail log"
    assert cmd.args == {"date": "2026-05-04"}


def test_parse_trailing_code_after_token() -> None:
    """The new ergonomic format puts the auth code AFTER the token tag,
    where the cursor sits after Reply: `Re: [Nightjar #abc] 123456`.
    Verdict comes from the body."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc1234] 123456",
        body="YES IRREVERSIBLE\n",
    )
    assert cmd.approval_token == "abc1234"
    assert cmd.approval_verdict == "IRREVERSIBLE"


def test_parse_trailing_bracketed_code_after_token() -> None:
    """Bracketed form at the trailing position also accepted."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc1234] [123456]",
        body="yes\n",
    )
    assert cmd.approval_token == "abc1234"
    assert cmd.approval_verdict == "APPROVE"


def test_parse_bare_code_with_free_form_falls_through() -> None:
    """The 'yes interpret' gate is gone; free-form just stays free-form
    for the watcher to hand to the principal-interpret pass."""
    cmd = parse_principal_command("123456 something to think about")
    assert cmd.is_free_form is True
    assert cmd.payload == "something to think about"


def test_parse_show_contact_requires_arg() -> None:
    """Naked 'show contact' shouldn't match (we want the arg form)."""
    cmd = parse_principal_command("show contact")
    assert cmd.verb != "show contact"


def test_describe_grammar_lists_known_verbs() -> None:
    text = describe_grammar()
    for spec in VERB_REGISTRY:
        assert spec.name in text


# ---- pickup verb (Step 6g part 2) -----------------------------------------


def test_parse_pickup_with_angle_brackets() -> None:
    cmd = parse_principal_command("pickup <abc123@example.com>")
    assert cmd.verb == "pickup"
    assert cmd.tier == 1
    assert cmd.args == {"message_id": "<abc123@example.com>"}


def test_parse_pickup_without_angle_brackets() -> None:
    """The parser is tolerant of bare email-address-shaped IDs; the
    handler normalises them to angle-bracket form before lookup."""
    cmd = parse_principal_command("pickup abc123@example.com")
    assert cmd.verb == "pickup"
    assert cmd.args == {"message_id": "abc123@example.com"}


def test_parse_pickup_no_arg_falls_through_to_free_form() -> None:
    """Bare `pickup` with no message-id is not a valid invocation."""
    cmd = parse_principal_command("pickup")
    assert cmd.verb is None
    assert cmd.is_free_form is True


def test_parse_pickup_with_leading_code() -> None:
    cmd = parse_principal_command("[123456] pickup <a@b>")
    assert cmd.verb == "pickup"
    assert cmd.args == {"message_id": "<a@b>"}


# ---- Tier 2/4 verb parsing (Build Step 4b) --------------------------------


@pytest.mark.parametrize("subject,verb,expected_args", [
    ("block composer", "block", {"contact": "composer"}),
    ("unblock composer", "unblock", {"contact": "composer"}),
    ("forget composer", "forget", {"contact": "composer"}),
])
def test_parse_recognises_tier2_verbs(subject: str, verb: str, expected_args: dict) -> None:
    cmd = parse_principal_command(subject)
    assert cmd.verb == verb
    assert cmd.tier == 2
    assert cmd.args == expected_args
    assert cmd.is_free_form is False


@pytest.mark.parametrize("subject,verb,expected_args", [
    # add and remove dropped from tier 4 → tier 2 in Step 6c, when
    # contacts moved out of nightjar.conf into per-file TOML. The
    # blast radius is one contact file, not a global config rewrite.
    ("add new@example.com", "add", {"email": "new@example.com"}),
    ("remove composer", "remove", {"contact": "composer"}),
])
def test_parse_recognises_add_remove_as_tier2(subject: str, verb: str, expected_args: dict) -> None:
    cmd = parse_principal_command(subject)
    assert cmd.verb == verb
    assert cmd.tier == 2
    assert cmd.args == expected_args


def test_parse_block_requires_contact_arg() -> None:
    """Naked 'block' is free-form, not the verb."""
    cmd = parse_principal_command("block")
    assert cmd.verb is None
    assert cmd.is_free_form is True


def test_parse_add_rejects_non_email_arg() -> None:
    """'add foobar' looks like a verb but the email pattern doesn't match,
    so it falls through to free-form."""
    cmd = parse_principal_command("add foobar")
    assert cmd.verb is None
    assert cmd.is_free_form is True


# ---- Approval verdict extraction (verdict in body, code in subject) -------


@pytest.mark.parametrize("body,expected_verdict", [
    # APPROVE synonyms
    ("yes", "APPROVE"),
    ("yes please", "APPROVE"),
    ("approve", "APPROVE"),
    ("approved", "APPROVE"),
    ("go", "APPROVE"),
    ("go for it", "APPROVE"),
    ("go ahead", "APPROVE"),
    ("ok", "APPROVE"),
    ("okay", "APPROVE"),
    ("confirm", "APPROVE"),
    ("confirmed", "APPROVE"),
    ("do it", "APPROVE"),
    ("YES", "APPROVE"),  # case-insensitive
    ("Approve", "APPROVE"),
    # DENY synonyms
    ("no", "DENY"),
    ("no thanks", "DENY"),
    ("deny", "DENY"),
    ("denied", "DENY"),
    ("refuse", "DENY"),
    ("refused", "DENY"),
    ("reject", "DENY"),
    ("rejected", "DENY"),
    ("stop", "DENY"),
    ("cancel", "DENY"),
    ("nope", "DENY"),
    # Tier-4 (uppercase exact)
    ("YES IRREVERSIBLE", "IRREVERSIBLE"),
])
def test_parse_approval_verdict_from_body(body: str, expected_verdict: str) -> None:
    """Verdict lives in the body. Subject is just `Re: [Nightjar #token] code`."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] 123456", body=body,
    )
    assert cmd.approval_token == "abc123"
    assert cmd.approval_verdict == expected_verdict


def test_parse_approval_verdict_unclear_when_extra_words() -> None:
    """Strict match: extra words past the synonym make UNCLEAR rather
    than accidental approve."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] 123456", body="yes if you must\n",
    )
    assert cmd.approval_token == "abc123"
    assert cmd.approval_verdict == "UNCLEAR"


def test_parse_tier4_confirm_must_be_uppercase() -> None:
    """Lowercase 'yes irreversible' is NOT a valid tier-4 confirm.
    UPPERCASE EXACT is the friction that makes tier-4 deliberate."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] 123456", body="yes irreversible\n",
    )
    assert cmd.approval_token == "abc123"
    assert cmd.approval_verdict == "UNCLEAR"


def test_parse_approval_verdict_unclear_when_body_missing() -> None:
    """Token present but no body -> UNCLEAR (resolver prompts retry)."""
    cmd = parse_principal_command("Re: [Nightjar #abc123] 123456", body=None)
    assert cmd.approval_token == "abc123"
    assert cmd.approval_verdict == "UNCLEAR"


def test_parse_approval_verdict_unclear_when_body_empty() -> None:
    cmd = parse_principal_command("Re: [Nightjar #abc123] 123456", body="")
    assert cmd.approval_token == "abc123"
    assert cmd.approval_verdict == "UNCLEAR"


# ---- Quoted-reply stripping in body ---------------------------------------


def test_parse_skips_blank_leading_lines() -> None:
    """Mail clients sometimes start the body with a blank line; the
    first non-blank line is what counts."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] 123456",
        body="\n\nyes\n\nOn Mon, May 5 ... wrote:\n> stuff\n",
    )
    assert cmd.approval_verdict == "APPROVE"


def test_parse_strips_gmail_attribution() -> None:
    """A Gmail-style `On ... wrote:` line is the quoted-block boundary.
    Anything after is original ping content, not the principal's verdict."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] 123456",
        body="approve\n\nOn Mon, May 5, 2026 at 12:34 AM Nightjar <bot@x.com> wrote:\n> Approval needed: reply\n> Body: yes\n",
    )
    assert cmd.approval_verdict == "APPROVE"


def test_parse_strips_outlook_attribution() -> None:
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] 123456",
        body="no thanks\n\n-----Original Message-----\nFrom: Nightjar\n",
    )
    assert cmd.approval_verdict == "DENY"


def test_parse_strips_apple_mail_forwarded_marker() -> None:
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] 123456",
        body="confirm\n\nBegin forwarded message:\nFrom: ...\n",
    )
    assert cmd.approval_verdict == "APPROVE"


def test_parse_skips_signature_block() -> None:
    """A `-- ` separator line marks the start of the signature; the
    verdict above it still wins."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] 123456",
        body="yes\n\n-- \nDylan\nSent from my phone\n",
    )
    assert cmd.approval_verdict == "APPROVE"


def test_parse_signature_above_verdict_returns_unclear() -> None:
    """If the body is just a signature block (no verdict above it),
    classification is UNCLEAR."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] 123456",
        body="-- \nDylan\n",
    )
    assert cmd.approval_verdict == "UNCLEAR"


def test_parse_quoted_only_body_returns_unclear() -> None:
    """A reply that is *just* the quoted original (no fresh content)
    must NOT classify as a verdict from a quoted line."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] 123456",
        body="On Mon, May 5 ... wrote:\n> Body: yes\n> Approval needed.\n",
    )
    assert cmd.approval_verdict == "UNCLEAR"


def test_parse_ignores_quoted_verdict_below_real_verdict() -> None:
    """A `>`-prefixed line containing 'no' must not flip an APPROVE."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] 123456",
        body="approved\n> previous: no\n> someone said no\n",
    )
    assert cmd.approval_verdict == "APPROVE"


def test_parse_approval_verdict_with_leading_code_in_subject() -> None:
    """Auth layer normally strips the code, but defensive: a leftover
    [code] in the subject is tolerated."""
    cmd = parse_principal_command(
        "Re: [Nightjar #abc123] [654321]", body="yes\n",
    )
    assert cmd.approval_token == "abc123"
    assert cmd.approval_verdict == "APPROVE"


# ---- Free-form (gate removed 2026-05-06) ----------------------------------


def test_parse_yes_interpret_now_free_form() -> None:
    """Post-gate-drop, 'yes interpret' is just a free-form payload —
    the parser no longer special-cases it."""
    cmd = parse_principal_command("yes interpret")
    assert cmd.is_free_form is True
    assert cmd.payload == "yes interpret"


def test_parse_bare_no_is_free_form() -> None:
    """A bare 'no' to a non-approval thread is just an unrecognised
    free-form payload. (Approval replies route via [Nightjar #token];
    this test covers the case where no token tag is present.)"""
    cmd = parse_principal_command("no")
    assert cmd.is_free_form is True


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
        trusted_authserv="mx.google.com",
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
    cmd = parse_principal_command("status")
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
    cmd = parse_principal_command("status")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "panic state:      TRIPPED" in body
    assert "3 invalid attempts" in body


def test_list_pending_reports_empty_when_nothing_queued(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("list pending")
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
    cmd = parse_principal_command("list pending")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "AWAITING_APPROVAL:   1" in body


def test_list_pending_includes_approval_queue_rows(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    s.queue_approval(
        token="abc12345",
        message_id="<m1>",
        verb="block",
        args={"contact": "composer"},
        tier=2,
    )
    cmd = parse_principal_command("list pending")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "#abc12345" in body
    assert "block" in body
    assert "composer" in body
    assert "tier 2" in body


def test_tail_log_returns_message_when_no_log(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("tail log 2099-01-01")
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
    cmd = parse_principal_command("tail log 2026-05-04")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "daemon_start" in body
    assert "idle_connect" in body
    assert "inboxes=['nightjar']" in body  # extra fields rendered


def test_show_contact_returns_known_contact(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("show contact composer")
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
    cmd = parse_principal_command("show contact composer")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "smtp-secret" not in body
    assert "JBSWY3DPEHPK3PXP" not in body
    assert cfg.inboxes["nightjar"].imap_password not in body


def test_show_contact_rejects_unknown_contact(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("show contact ghost")
    _, body = dispatch(command=cmd, config=cfg, state=s)
    assert "No contact 'ghost'" in body


def test_show_notes_placeholder_reports_step_7(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    cmd = parse_principal_command("show notes composer")
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
