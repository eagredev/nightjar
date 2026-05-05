"""Tests for daemon/contacts_loader.py.

Covers:
  - Happy path: round-trip from TOML to Contact dataclass.
  - Filename / contact_id mismatch rejected.
  - Cross-file invariants: principal uniqueness, address uniqueness.
  - Field validation: daily_limit accepted forms, bool fields, list shapes.
  - Empty / missing directory handling.
  - Mixed file types (.toml + .md + symlinks) — only .toml loaded.
"""
from __future__ import annotations

from pathlib import Path
import textwrap
import pytest

from daemon.config import ConfigError
from daemon.contacts_loader import load_all


def _write(tmp_path: Path, name: str, body: str) -> Path:
    """Write a contact TOML file. `name` may include or omit the .toml suffix."""
    contacts_dir = tmp_path / "contacts"
    contacts_dir.mkdir(exist_ok=True)
    path = contacts_dir / (name if name.endswith(".toml") else f"{name}.toml")
    path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return contacts_dir


# ---- Happy path --------------------------------------------------------


def test_load_single_contact(tmp_path: Path) -> None:
    contacts_dir = _write(tmp_path, "principal", """
        contact_id = "principal"
        addresses = ["me@example.com"]
        display_name = "Me"
        relationship = "Administrator"
        daily_limit = "unlimited"
        is_principal = true
        inboxes = ["nightjar"]
    """)
    r = load_all(contacts_dir)
    assert set(r.contacts) == {"principal"}
    p = r.contacts["principal"]
    assert p.contact_id == "principal"
    assert p.addresses == ("me@example.com",)
    assert p.daily_limit == -1   # 'unlimited' -> -1
    assert p.is_principal is True
    assert p.inboxes == ("nightjar",)
    assert p.auto_approve_notes is False  # default
    assert r.address_index == {"me@example.com": "principal"}


def test_load_multiple_contacts(tmp_path: Path) -> None:
    contacts_dir = _write(tmp_path, "principal", """
        contact_id = "principal"
        addresses = ["me@example.com"]
        display_name = "Me"
        relationship = "Administrator"
        is_principal = true
        inboxes = ["nightjar"]
    """)
    _write(tmp_path, "composer", """
        contact_id = "composer"
        addresses = ["composer@example.com"]
        display_name = "Composer"
        relationship = "Project composer"
        daily_limit = 3
        inboxes = ["nightjar"]
    """)
    r = load_all(contacts_dir)
    assert set(r.contacts) == {"principal", "composer"}
    assert r.contacts["composer"].daily_limit == 3
    assert r.contacts["composer"].is_principal is False
    assert r.address_index == {
        "me@example.com": "principal",
        "composer@example.com": "composer",
    }


def test_addresses_are_lowercased(tmp_path: Path) -> None:
    contacts_dir = _write(tmp_path, "p", """
        contact_id = "p"
        addresses = ["MixedCase@Example.COM"]
        is_principal = true
        inboxes = ["nightjar"]
    """)
    r = load_all(contacts_dir)
    assert r.contacts["p"].addresses == ("mixedcase@example.com",)
    assert "mixedcase@example.com" in r.address_index


def test_default_values(tmp_path: Path) -> None:
    contacts_dir = _write(tmp_path, "p", """
        contact_id = "p"
        addresses = ["p@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
    """)
    p = load_all(contacts_dir).contacts["p"]
    assert p.daily_limit == 3   # default
    assert p.display_name == "p"  # falls back to contact_id
    assert p.relationship == ""
    assert p.auto_approve_notes is False


def test_auto_approve_notes_when_set_true(tmp_path: Path) -> None:
    contacts_dir = _write(tmp_path, "p", """
        contact_id = "p"
        addresses = ["p@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
        auto_approve_notes = true
    """)
    p = load_all(contacts_dir).contacts["p"]
    assert p.auto_approve_notes is True


# ---- Empty / missing directory ----------------------------------------


def test_missing_directory_returns_empty(tmp_path: Path) -> None:
    r = load_all(tmp_path / "no-such-dir")
    assert r.contacts == {}
    assert r.address_index == {}


def test_existing_but_empty_directory_returns_empty(tmp_path: Path) -> None:
    contacts_dir = tmp_path / "contacts"
    contacts_dir.mkdir()
    r = load_all(contacts_dir)
    assert r.contacts == {}


def test_directory_path_is_file_raises(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "contacts"
    not_a_dir.write_text("hello")
    with pytest.raises(ConfigError, match="not a directory"):
        load_all(not_a_dir)


def test_non_toml_files_are_skipped(tmp_path: Path) -> None:
    contacts_dir = tmp_path / "contacts"
    contacts_dir.mkdir()
    (contacts_dir / "README.md").write_text("just a readme")
    (contacts_dir / "principal.toml").write_text(textwrap.dedent("""
        contact_id = "principal"
        addresses = ["p@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
    """).strip())
    r = load_all(contacts_dir)
    assert set(r.contacts) == {"principal"}


# ---- Filename / contact_id mismatch -----------------------------------


def test_contact_id_must_match_filename_stem(tmp_path: Path) -> None:
    _write(tmp_path, "alice", """
        contact_id = "bob"
        addresses = ["x@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
    """)
    with pytest.raises(ConfigError, match="does not match filename"):
        load_all(tmp_path / "contacts")


def test_filename_stem_invalid_chars_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "with spaces", """
        contact_id = "with spaces"
        addresses = ["x@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
    """)
    with pytest.raises(ConfigError, match="must match"):
        load_all(tmp_path / "contacts")


# ---- Cross-file invariants --------------------------------------------


def test_two_principals_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "alice", """
        contact_id = "alice"
        addresses = ["alice@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
    """)
    _write(tmp_path, "bob", """
        contact_id = "bob"
        addresses = ["bob@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
    """)
    with pytest.raises(ConfigError, match="multiple"):
        load_all(tmp_path / "contacts")


def test_duplicate_address_across_files_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "alice", """
        contact_id = "alice"
        addresses = ["shared@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
    """)
    _write(tmp_path, "bob", """
        contact_id = "bob"
        addresses = ["shared@example.com"]
        inboxes = ["nightjar"]
    """)
    with pytest.raises(ConfigError, match="claimed by both"):
        load_all(tmp_path / "contacts")


def test_duplicate_address_within_one_file_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "alice", """
        contact_id = "alice"
        addresses = ["dup@example.com", "dup@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
    """)
    with pytest.raises(ConfigError, match="duplicate address"):
        load_all(tmp_path / "contacts")


# ---- Field validation -------------------------------------------------


def test_missing_addresses_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "p", """
        contact_id = "p"
        is_principal = true
        inboxes = ["nightjar"]
    """)
    with pytest.raises(ConfigError, match="addresses"):
        load_all(tmp_path / "contacts")


def test_empty_addresses_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "p", """
        contact_id = "p"
        addresses = []
        is_principal = true
        inboxes = ["nightjar"]
    """)
    with pytest.raises(ConfigError, match="addresses list is empty"):
        load_all(tmp_path / "contacts")


def test_address_without_at_sign_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "p", """
        contact_id = "p"
        addresses = ["not-an-email"]
        is_principal = true
        inboxes = ["nightjar"]
    """)
    with pytest.raises(ConfigError, match="not an email"):
        load_all(tmp_path / "contacts")


def test_missing_inboxes_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "p", """
        contact_id = "p"
        addresses = ["p@example.com"]
        is_principal = true
    """)
    with pytest.raises(ConfigError, match="inboxes list is required"):
        load_all(tmp_path / "contacts")


def test_empty_inboxes_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "p", """
        contact_id = "p"
        addresses = ["p@example.com"]
        is_principal = true
        inboxes = []
    """)
    with pytest.raises(ConfigError, match="inboxes list is empty"):
        load_all(tmp_path / "contacts")


def test_daily_limit_unlimited_string(tmp_path: Path) -> None:
    contacts_dir = _write(tmp_path, "p", """
        contact_id = "p"
        addresses = ["p@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
        daily_limit = "unlimited"
    """)
    assert load_all(contacts_dir).contacts["p"].daily_limit == -1


def test_daily_limit_negative_int_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "p", """
        contact_id = "p"
        addresses = ["p@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
        daily_limit = -5
    """)
    with pytest.raises(ConfigError, match="daily_limit"):
        load_all(tmp_path / "contacts")


def test_daily_limit_string_int_rejected(tmp_path: Path) -> None:
    """We don't tolerate the operator quoting a number."""
    _write(tmp_path, "p", """
        contact_id = "p"
        addresses = ["p@example.com"]
        is_principal = true
        inboxes = ["nightjar"]
        daily_limit = "3"
    """)
    with pytest.raises(ConfigError, match="bare int"):
        load_all(tmp_path / "contacts")


def test_is_principal_must_be_bool(tmp_path: Path) -> None:
    _write(tmp_path, "p", """
        contact_id = "p"
        addresses = ["p@example.com"]
        is_principal = "yes"
        inboxes = ["nightjar"]
    """)
    with pytest.raises(ConfigError, match="is_principal"):
        load_all(tmp_path / "contacts")


def test_invalid_toml_rejected(tmp_path: Path) -> None:
    contacts_dir = tmp_path / "contacts"
    contacts_dir.mkdir()
    (contacts_dir / "p.toml").write_text("this is = not [valid toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_all(contacts_dir)
