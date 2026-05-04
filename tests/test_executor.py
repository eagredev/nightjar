"""Tier-2 executor tests (block, unblock, forget) and tier-4 stubs."""
from __future__ import annotations

from pathlib import Path

import pytest

from daemon import executor
from daemon.config import (
    Config, Contact, DaemonConfig, InboxConfig, SecurityConfig, SmtpConfig,
)
from daemon.state import State


def make_config(tmp_path: Path) -> Config:
    daemon = DaemonConfig(
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        notes_dir=tmp_path / "contacts",
    )
    daemon.state_dir.mkdir(parents=True)
    daemon.log_dir.mkdir(parents=True)
    daemon.notes_dir.mkdir(parents=True)
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
    return Config(
        daemon=daemon,
        contacts=contacts,
        inboxes={"nightjar": inbox},
        security=security,
        smtp=smtp,
        address_index={"me@example.com": "principal", "composer@example.com": "composer"},
    )


def make_state(tmp_path: Path) -> State:
    return State(db_path=tmp_path / "state.db")


# ---- block ----------------------------------------------------------------


def test_block_marks_contact_blocked(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    result = executor.execute(
        verb="block", args={"contact": "composer"},
        config=cfg, state=s, now=1_000,
    )
    assert result.ok is True
    assert "blocked" in result.summary
    assert s.is_contact_blocked("composer") is True


def test_block_idempotent(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    executor.execute(verb="block", args={"contact": "composer"}, config=cfg, state=s, now=1)
    result = executor.execute(
        verb="block", args={"contact": "composer"}, config=cfg, state=s, now=2,
    )
    assert result.ok is True
    assert "already blocked" in result.summary


def test_block_unknown_contact_fails(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    result = executor.execute(
        verb="block", args={"contact": "ghost"}, config=cfg, state=s, now=1,
    )
    assert result.ok is False
    assert "no contact" in result.summary
    assert s.is_contact_blocked("ghost") is False


def test_block_missing_arg_fails(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    result = executor.execute(
        verb="block", args={}, config=cfg, state=s, now=1,
    )
    assert result.ok is False


# ---- unblock --------------------------------------------------------------


def test_unblock_lifts_block(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    s.block_contact(contact_id="composer", at=1)
    result = executor.execute(
        verb="unblock", args={"contact": "composer"},
        config=cfg, state=s, now=2,
    )
    assert result.ok is True
    assert s.is_contact_blocked("composer") is False


def test_unblock_when_not_blocked_is_noop(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    result = executor.execute(
        verb="unblock", args={"contact": "composer"},
        config=cfg, state=s, now=1,
    )
    assert result.ok is True
    assert "not blocked" in result.summary


# ---- forget ---------------------------------------------------------------


def test_forget_deletes_notes_file(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    notes = cfg.daemon.notes_dir / "composer.md"
    notes.write_text("# Composer\n\nNotes about the composer.\n", encoding="utf-8")
    result = executor.execute(
        verb="forget", args={"contact": "composer"},
        config=cfg, state=s, now=1,
    )
    assert result.ok is True
    assert "forgot" in result.summary
    assert not notes.exists()


def test_forget_no_notes_file_is_ok(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    result = executor.execute(
        verb="forget", args={"contact": "composer"},
        config=cfg, state=s, now=1,
    )
    assert result.ok is True
    assert "no notes file" in result.summary


def test_forget_unknown_contact_fails(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    result = executor.execute(
        verb="forget", args={"contact": "ghost"},
        config=cfg, state=s, now=1,
    )
    assert result.ok is False
    assert "no contact" in result.summary


# ---- tier-4: add ----------------------------------------------------------
# These tests use a real on-disk nightjar.conf at tmp_path/nightjar.conf
# so config_writer's atomic-write + validation path is exercised end to end.


def write_baseline_conf(tmp_path: Path) -> Path:
    import textwrap
    path = tmp_path / "nightjar.conf"
    path.write_text(
        textwrap.dedent(f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs
        notes_dir = {tmp_path}/contacts

        [contact:principal]
        addresses = me@example.com
        display_name = Operator
        is_principal = true
        daily_limit = unlimited

        [contact:composer]
        addresses = composer@example.com
        display_name = Composer
        daily_limit = 3

        [inbox:nightjar]
        enabled = true
        imap_host = imap.example.com
        imap_port = 993
        imap_user = bot@example.com
        imap_password = secret
        allowed_contacts = principal, composer
        """).lstrip(),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def load_real_config(tmp_path: Path):
    """Load Config from a tmp on-disk nightjar.conf so add/remove
    executors can rewrite it."""
    from daemon.config import load as load_config
    return load_config(write_baseline_conf(tmp_path))


def test_add_creates_new_contact(tmp_path: Path) -> None:
    cfg = load_real_config(tmp_path)
    s = make_state(tmp_path)
    conf_path = tmp_path / "nightjar.conf"
    result = executor.execute(
        verb="add", args={"email": "newbie@example.com"},
        config=cfg, state=s, now=1, config_path=conf_path,
    )
    assert result.ok is True
    assert "added 'newbie'" in result.summary
    assert "newbie@example.com" in result.summary
    # In-memory config refreshed.
    assert "newbie" in cfg.contacts
    assert cfg.address_index["newbie@example.com"] == "newbie"
    assert "newbie" in cfg.inboxes["nightjar"].allowed_contacts
    # Disk file rewritten and re-parses.
    from daemon.config import load as load_config
    cfg2 = load_config(conf_path)
    assert "newbie" in cfg2.contacts
    assert cfg2.contacts["newbie"].daily_limit == 3
    assert cfg2.contacts["newbie"].is_principal is False


def test_add_rejects_existing_address(tmp_path: Path) -> None:
    cfg = load_real_config(tmp_path)
    s = make_state(tmp_path)
    conf_path = tmp_path / "nightjar.conf"
    result = executor.execute(
        verb="add", args={"email": "composer@example.com"},
        config=cfg, state=s, now=1, config_path=conf_path,
    )
    assert result.ok is False
    assert "already exists" in result.summary or "already" in result.body
    # Original contacts intact.
    assert "newbie" not in cfg.contacts


def test_add_rejects_malformed_email(tmp_path: Path) -> None:
    cfg = load_real_config(tmp_path)
    s = make_state(tmp_path)
    conf_path = tmp_path / "nightjar.conf"
    result = executor.execute(
        verb="add", args={"email": "not-an-email"},
        config=cfg, state=s, now=1, config_path=conf_path,
    )
    assert result.ok is False
    assert "malformed" in result.summary


def test_add_derives_unique_contact_id(tmp_path: Path) -> None:
    """If the local-part collides with an existing contact_id, the
    executor suffixes -2 to keep it unique."""
    cfg = load_real_config(tmp_path)
    s = make_state(tmp_path)
    conf_path = tmp_path / "nightjar.conf"
    # composer is already taken; adding composer@something-else.com should
    # produce contact_id 'composer-2'.
    result = executor.execute(
        verb="add", args={"email": "composer@otherdomain.com"},
        config=cfg, state=s, now=1, config_path=conf_path,
    )
    assert result.ok is True
    assert "composer-2" in result.summary
    assert "composer-2" in cfg.contacts


# ---- tier-4: remove -------------------------------------------------------


def test_remove_deletes_contact(tmp_path: Path) -> None:
    cfg = load_real_config(tmp_path)
    s = make_state(tmp_path)
    conf_path = tmp_path / "nightjar.conf"
    result = executor.execute(
        verb="remove", args={"contact": "composer"},
        config=cfg, state=s, now=1, config_path=conf_path,
    )
    assert result.ok is True
    assert "removed 'composer'" in result.summary
    # In-memory config refreshed.
    assert "composer" not in cfg.contacts
    assert "composer@example.com" not in cfg.address_index
    assert "composer" not in cfg.inboxes["nightjar"].allowed_contacts
    # Disk file rewritten.
    from daemon.config import load as load_config
    cfg2 = load_config(conf_path)
    assert "composer" not in cfg2.contacts


def test_remove_refuses_principal(tmp_path: Path) -> None:
    cfg = load_real_config(tmp_path)
    s = make_state(tmp_path)
    conf_path = tmp_path / "nightjar.conf"
    result = executor.execute(
        verb="remove", args={"contact": "principal"},
        config=cfg, state=s, now=1, config_path=conf_path,
    )
    assert result.ok is False
    assert "principal" in result.summary
    # Principal still in config.
    assert "principal" in cfg.contacts


def test_remove_rejects_unknown_contact(tmp_path: Path) -> None:
    cfg = load_real_config(tmp_path)
    s = make_state(tmp_path)
    conf_path = tmp_path / "nightjar.conf"
    result = executor.execute(
        verb="remove", args={"contact": "ghost"},
        config=cfg, state=s, now=1, config_path=conf_path,
    )
    assert result.ok is False
    assert "no contact" in result.summary


def test_remove_missing_arg_fails(tmp_path: Path) -> None:
    cfg = load_real_config(tmp_path)
    s = make_state(tmp_path)
    conf_path = tmp_path / "nightjar.conf"
    result = executor.execute(
        verb="remove", args={}, config=cfg, state=s, now=1, config_path=conf_path,
    )
    assert result.ok is False
    assert "missing" in result.summary


# ---- dispatch -------------------------------------------------------------


def test_unknown_verb_returns_error(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    result = executor.execute(
        verb="unknown", args={}, config=cfg, state=s, now=1,
    )
    assert result.ok is False
    assert "unknown verb" in result.summary


def test_executor_catches_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a verb body raises, the executor surfaces the failure
    rather than crashing the watcher."""
    def boom(*, args, config, state, now, config_path):
        raise RuntimeError("simulated boom")
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    monkeypatch.setitem(executor._DISPATCH, "boom", boom)
    result = executor.execute(
        verb="boom", args={}, config=cfg, state=s, now=1,
    )
    assert result.ok is False
    assert "RuntimeError" in result.summary
    assert "simulated boom" in result.body
