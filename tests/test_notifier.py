"""Notifier tests. Mocks smtplib.SMTP so nothing leaves the test process."""
from __future__ import annotations

from email import message_from_string
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from daemon import notifier
from daemon.config import SmtpConfig
from daemon.state import State


SMTP_CONFIG = SmtpConfig(
    host="smtp.example.com",
    port=587,
    user="bot@example.com",
    password="secret",
    from_name="Nightjar",
    from_addr="bot@example.com",
)


def make_state(tmp_path: Path) -> State:
    return State(db_path=tmp_path / "state.db")


class FakeSMTP:
    """Capture-everything SMTP stand-in for smtplib.SMTP."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: int | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ehlo_called = False
        self.starttls_called = False
        self.logged_in = False
        self.sent_messages: list = []
        self.fail_on_send: Exception | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def ehlo(self) -> None:
        self.ehlo_called = True

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user: str, password: str) -> None:
        self.logged_in = True

    def send_message(self, msg) -> None:
        if self.fail_on_send is not None:
            raise self.fail_on_send
        # Round-trip the message through email parsing so tests can
        # inspect the wire format the recipient would actually see.
        as_str = msg.as_string()
        parsed = message_from_string(as_str)
        self.sent_messages.append(parsed)


@pytest.fixture(autouse=True)
def reset_fake_smtp():
    FakeSMTP.instances.clear()
    yield
    FakeSMTP.instances.clear()


# --- notify_principal ------------------------------------------------------


def test_notify_principal_raises_without_smtp_config(tmp_path: Path) -> None:
    with pytest.raises(notifier.SmtpNotConfiguredError):
        notifier.notify_principal(
            smtp=None,
            principal_addr="me@example.com",
            subject="x",
            body="x",
        )


def test_notify_principal_sends_one_message_no_footer(tmp_path: Path) -> None:
    with patch.object(notifier.smtplib, "SMTP", FakeSMTP):
        result = notifier.notify_principal(
            smtp=SMTP_CONFIG,
            principal_addr="me@example.com",
            subject="status",
            body="all systems nominal",
        )
    assert result.primary_sent is True
    assert result.audit_sent is False  # principal IS the recipient
    assert result.audit_queued is False
    assert len(FakeSMTP.instances) == 1  # one SMTP transaction
    msg = FakeSMTP.instances[0].sent_messages[0]
    assert msg["To"] == "me@example.com"
    assert msg["Subject"] == "status"
    payload = msg.get_payload()
    assert "all systems nominal" in payload
    # No footer on principal mail.
    assert notifier.CONTACT_FOOTER.splitlines()[1] not in payload


def test_notify_principal_returns_error_on_smtp_failure(tmp_path: Path) -> None:
    fake = FakeSMTP("x", 0)
    fake.fail_on_send = ConnectionRefusedError("nope")

    def factory(*a, **k):
        return fake

    with patch.object(notifier.smtplib, "SMTP", factory):
        result = notifier.notify_principal(
            smtp=SMTP_CONFIG,
            principal_addr="me@example.com",
            subject="x",
            body="x",
        )
    assert result.primary_sent is False
    assert "ConnectionRefusedError" in (result.error or "")


# --- send_to_contact -------------------------------------------------------


def test_send_to_contact_appends_footer_and_sends_audit(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    with patch.object(notifier.smtplib, "SMTP", FakeSMTP):
        result = notifier.send_to_contact(
            smtp=SMTP_CONFIG,
            state=state,
            principal_addr="me@example.com",
            contact_addr="composer@example.com",
            subject="hello",
            body="quick question",
        )
    assert result.primary_sent is True
    assert result.audit_sent is True
    assert result.audit_queued is False
    # Two SMTP transactions, never BCC.
    assert len(FakeSMTP.instances) == 2
    primary = FakeSMTP.instances[0].sent_messages[0]
    audit = FakeSMTP.instances[1].sent_messages[0]
    assert primary["To"] == "composer@example.com"
    assert primary["Bcc"] is None
    assert primary["Cc"] is None
    # Footer on the contact-facing message.
    primary_payload = primary.get_payload()
    assert "show my notes" in primary_payload
    assert "delete my data" in primary_payload
    assert "stop\ncontacting me" in primary_payload
    # Audit subject formatted right; audit copy goes to principal.
    assert audit["To"] == "me@example.com"
    assert audit["Subject"] == "[Nightjar Audit] To composer@example.com, hello"
    audit_payload = audit.get_payload()
    assert "--- audit headers ---" in audit_payload
    assert "Subject: hello" in audit_payload
    assert "quick question" in audit_payload  # literal body included


def test_send_to_contact_recipient_view_has_no_principal_address(tmp_path: Path) -> None:
    """The recipient's 'show original' must reveal nothing about the principal."""
    state = make_state(tmp_path)
    with patch.object(notifier.smtplib, "SMTP", FakeSMTP):
        notifier.send_to_contact(
            smtp=SMTP_CONFIG,
            state=state,
            principal_addr="me@example.com",
            contact_addr="composer@example.com",
            subject="hello",
            body="quick question",
        )
    primary = FakeSMTP.instances[0].sent_messages[0]
    raw = primary.as_string()
    assert "me@example.com" not in raw  # principal addr nowhere in primary
    assert "Bcc:" not in raw


def test_send_to_contact_queues_audit_on_audit_failure(tmp_path: Path) -> None:
    """Primary succeeds, audit fails: row is queued in pending_audits."""
    state = make_state(tmp_path)
    primary_smtp = FakeSMTP("primary", 0)
    audit_smtp = FakeSMTP("audit", 0)
    audit_smtp.fail_on_send = TimeoutError("audit channel down")
    instances = iter([primary_smtp, audit_smtp])

    def factory(*a, **k):
        return next(instances)

    with patch.object(notifier.smtplib, "SMTP", factory):
        result = notifier.send_to_contact(
            smtp=SMTP_CONFIG,
            state=state,
            principal_addr="me@example.com",
            contact_addr="composer@example.com",
            subject="hello",
            body="body",
        )
    assert result.primary_sent is True
    assert result.audit_sent is False
    assert result.audit_queued is True
    assert result.audit_id is not None
    assert state.count_pending_audits() == 1
    pending = state.list_pending_audits()[0]
    assert pending["to_addr"] == "me@example.com"
    assert "TimeoutError" in (pending["last_error"] or "")


def test_send_to_contact_audit_marked_send_failed_on_primary_failure(tmp_path: Path) -> None:
    """Primary fails: audit copy still goes out, with a (SEND FAILED) banner."""
    state = make_state(tmp_path)
    primary_smtp = FakeSMTP("primary", 0)
    primary_smtp.fail_on_send = ConnectionRefusedError("nope")
    audit_smtp = FakeSMTP("audit", 0)
    instances = iter([primary_smtp, audit_smtp])

    def factory(*a, **k):
        return next(instances)

    with patch.object(notifier.smtplib, "SMTP", factory):
        result = notifier.send_to_contact(
            smtp=SMTP_CONFIG,
            state=state,
            principal_addr="me@example.com",
            contact_addr="composer@example.com",
            subject="hello",
            body="body",
        )
    assert result.primary_sent is False
    assert result.audit_sent is True  # audit went out informing principal
    assert result.audit_queued is False
    audit = audit_smtp.sent_messages[0]
    assert "(SEND FAILED)" in audit.get_payload()


def test_send_to_contact_raises_without_smtp_config(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    with pytest.raises(notifier.SmtpNotConfiguredError):
        notifier.send_to_contact(
            smtp=None,
            state=state,
            principal_addr="me@example.com",
            contact_addr="x@example.com",
            subject="x",
            body="x",
        )


# --- send_audit_retry ------------------------------------------------------


def test_send_audit_retry_success_clears_row(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    audit_id = state.queue_audit(
        primary_message_id="<m@x>",
        to_addr="me@example.com",
        subject="[Nightjar Audit] x",
        body="(audit)",
    )
    with patch.object(notifier.smtplib, "SMTP", FakeSMTP):
        ok = notifier.send_audit_retry(
            smtp=SMTP_CONFIG,
            state=state,
            audit_row=state.list_pending_audits()[0],
        )
    assert ok is True
    assert state.count_pending_audits() == 0


def test_send_audit_retry_failure_increments_attempts(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    audit_id = state.queue_audit(
        primary_message_id=None,
        to_addr="me@example.com",
        subject="x",
        body="x",
    )
    fake = FakeSMTP("x", 0)
    fake.fail_on_send = ConnectionResetError("retry also fails")

    def factory(*a, **k):
        return fake

    with patch.object(notifier.smtplib, "SMTP", factory):
        ok = notifier.send_audit_retry(
            smtp=SMTP_CONFIG,
            state=state,
            audit_row=state.list_pending_audits()[0],
        )
    assert ok is False
    rows = state.list_pending_audits()
    assert len(rows) == 1
    assert rows[0]["attempts"] == 2
    assert "ConnectionResetError" in (rows[0]["last_error"] or "")
