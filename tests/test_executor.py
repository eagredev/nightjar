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


# ---- tier-4 stubs ---------------------------------------------------------


def test_add_returns_not_yet_wired(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    result = executor.execute(
        verb="add", args={"email": "new@example.com"},
        config=cfg, state=s, now=1,
    )
    assert result.ok is False
    assert "not yet wired" in result.summary
    # Body should explain the workaround.
    assert "nightjar.conf" in result.body


def test_remove_returns_not_yet_wired(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    s = make_state(tmp_path)
    result = executor.execute(
        verb="remove", args={"contact": "composer"},
        config=cfg, state=s, now=1,
    )
    assert result.ok is False
    assert "not yet wired" in result.summary


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
    def boom(*, args, config, state, now):
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
