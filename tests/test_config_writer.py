"""Tests for daemon.config_writer (atomic INI rewrite for tier-4 verbs)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from daemon import config_writer
from daemon.config import load as load_config
from daemon.config_writer import AddRequest, ConfigWriteError


def write_conf(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "nightjar.conf"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(0o600)
    return path


def baseline_conf(tmp_path: Path) -> Path:
    return write_conf(
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
        trusted_authserv = mx.google.com
        allowed_contacts = principal, friend
        """,
    )


# ---- add_contact -----------------------------------------------------------


def test_add_contact_round_trips(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    req = AddRequest(
        contact_id="newbie",
        address="newbie@example.com",
        display_name="Newbie",
        relationship="freshly added",
        daily_limit=3,
        inbox_name="nightjar",
    )
    config_writer.add_contact(request=req, config=cfg, config_path=path)
    cfg2 = load_config(path)
    assert "newbie" in cfg2.contacts
    assert cfg2.contacts["newbie"].addresses == ("newbie@example.com",)
    assert cfg2.contacts["newbie"].display_name == "Newbie"
    assert cfg2.contacts["newbie"].daily_limit == 3
    assert cfg2.contacts["newbie"].is_principal is False
    assert "newbie" in cfg2.inboxes["nightjar"].allowed_contacts
    assert cfg2.address_index["newbie@example.com"] == "newbie"


def test_add_contact_preserves_existing_contacts(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    config_writer.add_contact(
        request=AddRequest(
            contact_id="newbie",
            address="newbie@example.com",
            display_name="Newbie",
            relationship="",
            daily_limit=3,
            inbox_name="nightjar",
        ),
        config=cfg,
        config_path=path,
    )
    cfg2 = load_config(path)
    assert "principal" in cfg2.contacts
    assert "friend" in cfg2.contacts
    assert cfg2.contacts["principal"].is_principal is True


def test_add_contact_unlimited_daily_limit(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    config_writer.add_contact(
        request=AddRequest(
            contact_id="vip",
            address="vip@example.com",
            display_name="VIP",
            relationship="",
            daily_limit=-1,
            inbox_name="nightjar",
        ),
        config=cfg,
        config_path=path,
    )
    cfg2 = load_config(path)
    assert cfg2.contacts["vip"].daily_limit == -1
    # The on-disk form should be the literal token 'unlimited'.
    assert "unlimited" in path.read_text(encoding="utf-8")


def test_add_contact_rejects_duplicate_id(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    with pytest.raises(ConfigWriteError, match="already exists"):
        config_writer.add_contact(
            request=AddRequest(
                contact_id="friend",
                address="other@example.com",
                display_name="Other",
                relationship="",
                daily_limit=3,
                inbox_name="nightjar",
            ),
            config=cfg,
            config_path=path,
        )


def test_add_contact_rejects_duplicate_address(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    with pytest.raises(ConfigWriteError, match="already claimed"):
        config_writer.add_contact(
            request=AddRequest(
                contact_id="newbie",
                address="friend@example.com",
                display_name="Newbie",
                relationship="",
                daily_limit=3,
                inbox_name="nightjar",
            ),
            config=cfg,
            config_path=path,
        )


def test_add_contact_rejects_bad_email(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    with pytest.raises(ConfigWriteError, match="not an email"):
        config_writer.add_contact(
            request=AddRequest(
                contact_id="newbie",
                address="not-an-email",
                display_name="Newbie",
                relationship="",
                daily_limit=3,
                inbox_name="nightjar",
            ),
            config=cfg,
            config_path=path,
        )


def test_add_contact_rejects_bad_id(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    with pytest.raises(ConfigWriteError, match="must match"):
        config_writer.add_contact(
            request=AddRequest(
                contact_id="bad id with spaces",
                address="ok@example.com",
                display_name="Ok",
                relationship="",
                daily_limit=3,
                inbox_name="nightjar",
            ),
            config=cfg,
            config_path=path,
        )


def test_add_contact_rejects_unknown_inbox(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    with pytest.raises(ConfigWriteError, match="inbox.*does not exist"):
        config_writer.add_contact(
            request=AddRequest(
                contact_id="newbie",
                address="newbie@example.com",
                display_name="Newbie",
                relationship="",
                daily_limit=3,
                inbox_name="missing",
            ),
            config=cfg,
            config_path=path,
        )


# ---- remove_contact --------------------------------------------------------


def test_remove_contact_round_trips(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    config_writer.remove_contact(contact_id="friend", config=cfg, config_path=path)
    cfg2 = load_config(path)
    assert "friend" not in cfg2.contacts
    assert "friend@example.com" not in cfg2.address_index
    assert "friend" not in cfg2.inboxes["nightjar"].allowed_contacts
    # Principal untouched.
    assert "principal" in cfg2.contacts


def test_remove_contact_refuses_principal(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    with pytest.raises(ConfigWriteError, match="principal"):
        config_writer.remove_contact(contact_id="principal", config=cfg, config_path=path)
    # File untouched (principal still loads).
    cfg2 = load_config(path)
    assert "principal" in cfg2.contacts


def test_remove_contact_rejects_unknown(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    with pytest.raises(ConfigWriteError, match="does not exist"):
        config_writer.remove_contact(contact_id="ghost", config=cfg, config_path=path)


def test_remove_contact_strips_from_multiple_inboxes(tmp_path: Path) -> None:
    """If a contact is in two inbox allowed_contacts lists, both should
    get stripped."""
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [contact:principal]
        addresses = me@example.com
        is_principal = true
        daily_limit = unlimited

        [contact:friend]
        addresses = friend@example.com
        daily_limit = 3

        [inbox:one]
        imap_host = imap.example.com
        imap_user = one@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        allowed_contacts = principal, friend

        [inbox:two]
        imap_host = imap.example.com
        imap_user = two@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        allowed_contacts = principal, friend
        """,
    )
    cfg = load_config(path)
    config_writer.remove_contact(contact_id="friend", config=cfg, config_path=path)
    cfg2 = load_config(path)
    assert "friend" not in cfg2.inboxes["one"].allowed_contacts
    assert "friend" not in cfg2.inboxes["two"].allowed_contacts


# ---- atomicity & permissions ----------------------------------------------


def test_write_preserves_chmod_600(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    config_writer.add_contact(
        request=AddRequest(
            contact_id="newbie",
            address="newbie@example.com",
            display_name="Newbie",
            relationship="",
            daily_limit=3,
            inbox_name="nightjar",
        ),
        config=cfg,
        config_path=path,
    )
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_no_tmp_files_left_after_write(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    config_writer.add_contact(
        request=AddRequest(
            contact_id="newbie",
            address="newbie@example.com",
            display_name="Newbie",
            relationship="",
            daily_limit=3,
            inbox_name="nightjar",
        ),
        config=cfg,
        config_path=path,
    )
    leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".nightjar.conf.")]
    assert leftovers == []


def test_validation_failure_does_not_corrupt_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If post-write validation throws ConfigError, the original
    file must remain untouched."""
    path = baseline_conf(tmp_path)
    original_text = path.read_text(encoding="utf-8")
    cfg = load_config(path)

    # Force the validator to fail by monkeypatching daemon.config.load
    # to raise the first time it's called via the writer's tmp validation.
    from daemon.config import ConfigError as RealConfigError
    real_load = config_writer.config_module.load
    call_count = {"n": 0}
    def fake_load(path_arg):
        call_count["n"] += 1
        # The writer always validates the tmp path, which has a name
        # like ".nightjar.conf.*.tmp".
        if ".tmp" in path_arg.name:
            raise RealConfigError("simulated validation failure")
        return real_load(path_arg)
    monkeypatch.setattr(config_writer.config_module, "load", fake_load)

    with pytest.raises(ConfigWriteError, match="post-write validation failed"):
        config_writer.add_contact(
            request=AddRequest(
                contact_id="newbie",
                address="newbie@example.com",
                display_name="Newbie",
                relationship="",
                daily_limit=3,
                inbox_name="nightjar",
            ),
            config=cfg,
            config_path=path,
        )
    # Original file content is exactly as before.
    assert path.read_text(encoding="utf-8") == original_text
    # No leftover tmp.
    leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".nightjar.conf.")]
    assert leftovers == []


# ---- apply_add / apply_remove (in-process refresh) ------------------------


def test_apply_add_mutates_config_in_place(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    config_writer.apply_add(
        request=AddRequest(
            contact_id="newbie",
            address="newbie@example.com",
            display_name="Newbie",
            relationship="",
            daily_limit=3,
            inbox_name="nightjar",
        ),
        config=cfg,
    )
    assert "newbie" in cfg.contacts
    assert cfg.contacts["newbie"].is_principal is False
    assert cfg.address_index["newbie@example.com"] == "newbie"
    assert "newbie" in cfg.inboxes["nightjar"].allowed_contacts


def test_apply_remove_mutates_config_in_place(tmp_path: Path) -> None:
    path = baseline_conf(tmp_path)
    cfg = load_config(path)
    config_writer.apply_remove(contact_id="friend", config=cfg)
    assert "friend" not in cfg.contacts
    assert "friend@example.com" not in cfg.address_index
    assert "friend" not in cfg.inboxes["nightjar"].allowed_contacts
