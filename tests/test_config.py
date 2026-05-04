"""Config parser tests. No network, no IMAP, pure stdlib + parser logic."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from daemon.config import ConfigError, load as load_config


def write_conf(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "nightjar.conf"
    path.write_text(textwrap.dedent(body).lstrip())
    path.chmod(0o600)
    return path


def test_minimal_config_loads(tmp_path: Path) -> None:
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [contact:principal]
        addresses = me@example.com
        display_name = Me
        is_principal = true
        daily_limit = unlimited

        [contact:friend]
        addresses = friend@example.com
        display_name = Friend
        daily_limit = 3

        [inbox:nightjar]
        enabled = true
        imap_host = imap.example.com
        imap_port = 993
        imap_user = nightjar@example.com
        imap_password = secret
        allowed_contacts = principal, friend
        """,
    )
    cfg = load_config(path)
    assert "principal" in cfg.contacts
    assert "friend" in cfg.contacts
    assert cfg.contacts["principal"].is_principal is True
    assert cfg.contacts["principal"].daily_limit == -1
    assert cfg.contacts["friend"].daily_limit == 3
    assert cfg.address_index["me@example.com"] == "principal"
    assert cfg.address_index["friend@example.com"] == "friend"
    assert "nightjar" in cfg.inboxes
    assert cfg.inboxes["nightjar"].imap_password == "secret"
    assert cfg.inboxes["nightjar"].allowed_contacts == ("principal", "friend")


def test_address_index_is_lowercased(tmp_path: Path) -> None:
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [contact:principal]
        addresses = ME@Example.COM
        is_principal = true
        daily_limit = unlimited

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        allowed_contacts = principal
        """,
    )
    cfg = load_config(path)
    assert "me@example.com" in cfg.address_index
    assert "ME@Example.COM" not in cfg.address_index


def test_two_principals_rejected(tmp_path: Path) -> None:
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [contact:a]
        addresses = a@example.com
        is_principal = true
        daily_limit = unlimited

        [contact:b]
        addresses = b@example.com
        is_principal = true
        daily_limit = unlimited

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        allowed_contacts = a, b
        """,
    )
    with pytest.raises(ConfigError, match="multiple contacts"):
        load_config(path)


def test_duplicate_address_rejected(tmp_path: Path) -> None:
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [contact:a]
        addresses = shared@example.com
        is_principal = true
        daily_limit = unlimited

        [contact:b]
        addresses = shared@example.com
        daily_limit = 3

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        allowed_contacts = a, b
        """,
    )
    with pytest.raises(ConfigError, match="claimed by both"):
        load_config(path)


def test_unknown_contact_in_allowlist_rejected(tmp_path: Path) -> None:
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [contact:a]
        addresses = a@example.com
        is_principal = true
        daily_limit = unlimited

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        allowed_contacts = a, ghost
        """,
    )
    with pytest.raises(ConfigError, match="unknown contact: 'ghost'"):
        load_config(path)


def test_disabled_inbox_skipped(tmp_path: Path) -> None:
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [contact:a]
        addresses = a@example.com
        is_principal = true
        daily_limit = unlimited

        [inbox:archived]
        enabled = false
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        allowed_contacts = a

        [inbox:active]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        allowed_contacts = a
        """,
    )
    cfg = load_config(path)
    assert "active" in cfg.inboxes
    assert "archived" not in cfg.inboxes


def test_no_inboxes_rejected(tmp_path: Path) -> None:
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [contact:a]
        addresses = a@example.com
        is_principal = true
        daily_limit = unlimited
        """,
    )
    with pytest.raises(ConfigError, match="no enabled"):
        load_config(path)
