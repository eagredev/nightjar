"""Tests for daemon/contacts_writer.py."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from daemon.config import Config, Contact, DaemonConfig, InboxConfig
from daemon.contacts_loader import load_all
from daemon.contacts_writer import (
    AddContactRequest,
    ContactsWriteError,
    apply_add,
    apply_remove,
    delete_contact,
    write_contact,
)


def _make_config(tmp_path: Path) -> Config:
    daemon = DaemonConfig(
        state_dir=tmp_path / "state", log_dir=tmp_path / "logs",
    )
    daemon.state_dir.mkdir(parents=True)
    daemon.log_dir.mkdir(parents=True)
    principal = Contact(
        contact_id="principal",
        addresses=("me@example.com",),
        display_name="Me",
        relationship="Administrator",
        daily_limit=-1,
        is_principal=True,
        inboxes=("nightjar",),
    )
    inbox = InboxConfig(
        name="nightjar",
        enabled=True,
        imap_host="imap.example.com",
        imap_port=993,
        imap_user="bot@example.com",
        imap_password="x",
        allowed_contacts=("principal",),
        trusted_authserv="mx.google.com",
    )
    return Config(
        daemon=daemon,
        contacts={"principal": principal},
        inboxes={"nightjar": inbox},
        address_index={"me@example.com": "principal"},
    )


def _add_request(contact_id: str, address: str, **overrides) -> AddContactRequest:
    base = dict(
        contact_id=contact_id,
        address=address,
        display_name=contact_id.title(),
        relationship="collaborator",
        daily_limit=3,
        inboxes=("nightjar",),
    )
    base.update(overrides)
    return AddContactRequest(**base)


# ---- write_contact ----------------------------------------------------


def test_write_contact_creates_file(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    contacts_dir = tmp_path / "contacts"
    req = _add_request("alice", "alice@example.com")
    path = write_contact(request=req, contacts_dir=contacts_dir, config=cfg)
    assert path == contacts_dir / "alice.toml"
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_contact_round_trips_through_loader(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    contacts_dir = tmp_path / "contacts"
    req = _add_request("alice", "Alice@Example.COM",  # exercise lowercasing
                       display_name="Alice", relationship="poet")
    write_contact(request=req, contacts_dir=contacts_dir, config=cfg)
    loaded = load_all(contacts_dir).contacts
    alice = loaded["alice"]
    assert alice.addresses == ("alice@example.com",)
    assert alice.display_name == "Alice"
    assert alice.relationship == "poet"
    assert alice.daily_limit == 3
    assert alice.is_principal is False
    assert alice.inboxes == ("nightjar",)


def test_write_contact_unlimited_renders_string(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    contacts_dir = tmp_path / "contacts"
    req = _add_request("alice", "alice@example.com", daily_limit=-1)
    path = write_contact(request=req, contacts_dir=contacts_dir, config=cfg)
    assert 'daily_limit = "unlimited"' in path.read_text()


def test_write_contact_rejects_invalid_id(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    req = _add_request("with spaces", "x@example.com")
    with pytest.raises(ContactsWriteError, match="must match"):
        write_contact(request=req, contacts_dir=tmp_path / "contacts", config=cfg)


def test_write_contact_rejects_duplicate_id(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    req = _add_request("principal", "x@example.com")
    with pytest.raises(ContactsWriteError, match="already exists"):
        write_contact(request=req, contacts_dir=tmp_path / "contacts", config=cfg)


def test_write_contact_rejects_duplicate_address(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    # The principal already claims me@example.com.
    req = _add_request("alice", "Me@Example.com")
    with pytest.raises(ContactsWriteError, match="already claimed"):
        write_contact(request=req, contacts_dir=tmp_path / "contacts", config=cfg)


def test_write_contact_rejects_unknown_inbox(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    req = _add_request("alice", "alice@example.com", inboxes=("ghost",))
    with pytest.raises(ContactsWriteError, match="does not exist"):
        write_contact(request=req, contacts_dir=tmp_path / "contacts", config=cfg)


def test_write_contact_rejects_empty_inboxes(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    req = _add_request("alice", "alice@example.com", inboxes=())
    with pytest.raises(ContactsWriteError, match="non-empty"):
        write_contact(request=req, contacts_dir=tmp_path / "contacts", config=cfg)


def test_write_contact_rejects_non_email_address(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    req = _add_request("alice", "not-an-email")
    with pytest.raises(ContactsWriteError, match="not an email"):
        write_contact(request=req, contacts_dir=tmp_path / "contacts", config=cfg)


def test_write_contact_refuses_to_overwrite_existing_file(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    contacts_dir = tmp_path / "contacts"
    contacts_dir.mkdir()
    # File exists on disk but not in config — out-of-sync state.
    (contacts_dir / "alice.toml").write_text("contact_id = 'alice'\n")
    req = _add_request("alice", "alice@example.com")
    with pytest.raises(ContactsWriteError, match="already exists"):
        write_contact(request=req, contacts_dir=contacts_dir, config=cfg)


def test_write_contact_special_chars_in_display_name(tmp_path: Path) -> None:
    """Operators may use quotes / backslashes in display names; the
    TOML escape must round-trip them."""
    cfg = _make_config(tmp_path)
    contacts_dir = tmp_path / "contacts"
    req = _add_request(
        "alice", "alice@example.com",
        display_name='Alice "the poet" O\'Brien',
        relationship='Project lead\nlives in Cambridge',
    )
    write_contact(request=req, contacts_dir=contacts_dir, config=cfg)
    loaded = load_all(contacts_dir).contacts["alice"]
    assert loaded.display_name == 'Alice "the poet" O\'Brien'
    assert loaded.relationship == 'Project lead\nlives in Cambridge'


# ---- delete_contact ---------------------------------------------------


def test_delete_contact_removes_file(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    contacts_dir = tmp_path / "contacts"
    write_contact(
        request=_add_request("alice", "alice@example.com"),
        contacts_dir=contacts_dir, config=cfg,
    )
    apply_add(
        request=_add_request("alice", "alice@example.com"), config=cfg,
    )
    delete_contact(contact_id="alice", contacts_dir=contacts_dir, config=cfg)
    assert not (contacts_dir / "alice.toml").exists()


def test_delete_contact_refuses_principal(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with pytest.raises(ContactsWriteError, match="refusing to remove principal"):
        delete_contact(
            contact_id="principal",
            contacts_dir=tmp_path / "contacts", config=cfg,
        )


def test_delete_contact_unknown_id(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with pytest.raises(ContactsWriteError, match="does not exist"):
        delete_contact(
            contact_id="ghost",
            contacts_dir=tmp_path / "contacts", config=cfg,
        )


def test_delete_contact_with_missing_file(tmp_path: Path) -> None:
    """In-memory has the contact, file is gone — drift state surfaces."""
    cfg = _make_config(tmp_path)
    apply_add(
        request=_add_request("alice", "alice@example.com"), config=cfg,
    )
    contacts_dir = tmp_path / "contacts"
    contacts_dir.mkdir()
    with pytest.raises(ContactsWriteError, match="out of sync"):
        delete_contact(
            contact_id="alice", contacts_dir=contacts_dir, config=cfg,
        )


# ---- apply_add / apply_remove ----------------------------------------


def test_apply_add_updates_in_memory_config(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    req = _add_request("alice", "alice@example.com")
    apply_add(request=req, config=cfg)
    assert "alice" in cfg.contacts
    assert cfg.address_index["alice@example.com"] == "alice"
    assert "alice" in cfg.inboxes["nightjar"].allowed_contacts


def test_apply_remove_strips_in_memory_config(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    req = _add_request("alice", "alice@example.com")
    apply_add(request=req, config=cfg)
    apply_remove(contact_id="alice", config=cfg)
    assert "alice" not in cfg.contacts
    assert "alice@example.com" not in cfg.address_index
    assert "alice" not in cfg.inboxes["nightjar"].allowed_contacts


def test_apply_add_preserves_existing_allowed_contacts(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    apply_add(
        request=_add_request("alice", "alice@example.com"), config=cfg,
    )
    apply_add(
        request=_add_request("bob", "bob@example.com"), config=cfg,
    )
    allowed = cfg.inboxes["nightjar"].allowed_contacts
    assert "principal" in allowed
    assert "alice" in allowed
    assert "bob" in allowed


def test_apply_remove_idempotent_for_unknown_id(tmp_path: Path) -> None:
    """apply_remove on a non-existent id should be a no-op (the
    write-side delete_contact is the gatekeeper that raises)."""
    cfg = _make_config(tmp_path)
    apply_remove(contact_id="ghost", config=cfg)  # no exception
    assert set(cfg.contacts) == {"principal"}
