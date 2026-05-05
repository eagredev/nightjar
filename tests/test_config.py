"""Config parser tests. No network, no IMAP, pure stdlib + parser logic.

Post-Step-6c, contacts live in their own per-file TOMLs in a sibling
directory; `[contact:*]` blocks in nightjar.conf are no longer
accepted (the migrator strips them on first start). Test fixtures
reflect that: the helpers below write contact TOMLs into a `contacts/`
subdir of tmp_path and emit a nightjar.conf that points at it.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from daemon.config import ConfigError, load as load_config


_PRINCIPAL_TOML = """
contact_id = "principal"
addresses = ["me@example.com"]
display_name = "Me"
relationship = "Administrator"
daily_limit = "unlimited"
is_principal = true
inboxes = ["nightjar"]
"""


def write_contact(tmp_path: Path, name: str, body: str) -> None:
    """Write a contact TOML file. `body` is the full TOML text."""
    contacts_dir = tmp_path / "contacts"
    contacts_dir.mkdir(exist_ok=True)
    (contacts_dir / f"{name}.toml").write_text(
        textwrap.dedent(body).strip() + "\n", encoding="utf-8",
    )


def write_principal(tmp_path: Path) -> None:
    """Write a default principal TOML — most fixture configs need one."""
    write_contact(tmp_path, "principal", _PRINCIPAL_TOML)


def write_conf(tmp_path: Path, body: str) -> Path:
    """Write nightjar.conf. The contacts_dir is automatically pointed
    at tmp_path/contacts, so callers just need to call write_contact()
    or write_principal() to populate the directory."""
    path = tmp_path / "nightjar.conf"
    # Inject contacts_dir if the body has a [daemon] section but no
    # explicit override. This keeps the body strings short.
    text = textwrap.dedent(body).lstrip()
    if "contacts_dir" not in text and "[daemon]" in text:
        text = text.replace(
            "[daemon]",
            f"[daemon]\ncontacts_dir = {tmp_path}/contacts",
            1,
        )
    path.write_text(text)
    path.chmod(0o600)
    return path


# ---- Minimal happy path ---------------------------------------------------


def test_minimal_config_loads(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "friend", """
        contact_id = "friend"
        addresses = ["friend@example.com"]
        display_name = "Friend"
        daily_limit = 3
        inboxes = ["nightjar"]
    """)
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        enabled = true
        imap_host = imap.example.com
        imap_port = 993
        imap_user = nightjar@example.com
        imap_password = secret
        trusted_authserv = mx.google.com
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
    # allowed_contacts is now derived from per-contact `inboxes` lists.
    assert set(cfg.inboxes["nightjar"].allowed_contacts) == {"principal", "friend"}


def test_address_index_is_lowercased(tmp_path: Path) -> None:
    write_contact(tmp_path, "principal", """
        contact_id = "principal"
        addresses = ["ME@Example.COM"]
        is_principal = true
        daily_limit = "unlimited"
        inboxes = ["nightjar"]
    """)
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        """,
    )
    cfg = load_config(path)
    assert "me@example.com" in cfg.address_index
    assert "ME@Example.COM" not in cfg.address_index


def test_two_principals_rejected(tmp_path: Path) -> None:
    write_contact(tmp_path, "a", """
        contact_id = "a"
        addresses = ["a@example.com"]
        is_principal = true
        daily_limit = "unlimited"
        inboxes = ["nightjar"]
    """)
    write_contact(tmp_path, "b", """
        contact_id = "b"
        addresses = ["b@example.com"]
        is_principal = true
        daily_limit = "unlimited"
        inboxes = ["nightjar"]
    """)
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        """,
    )
    with pytest.raises(ConfigError, match="multiple"):
        load_config(path)


def test_duplicate_address_rejected(tmp_path: Path) -> None:
    write_contact(tmp_path, "a", """
        contact_id = "a"
        addresses = ["shared@example.com"]
        is_principal = true
        daily_limit = "unlimited"
        inboxes = ["nightjar"]
    """)
    write_contact(tmp_path, "b", """
        contact_id = "b"
        addresses = ["shared@example.com"]
        daily_limit = 3
        inboxes = ["nightjar"]
    """)
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        """,
    )
    with pytest.raises(ConfigError, match="claimed by both"):
        load_config(path)


def test_contact_referencing_unknown_inbox_rejected(tmp_path: Path) -> None:
    """A contact's `inboxes` list must reference enabled inboxes."""
    write_contact(tmp_path, "principal", """
        contact_id = "principal"
        addresses = ["me@example.com"]
        is_principal = true
        daily_limit = "unlimited"
        inboxes = ["ghost"]
    """)
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        """,
    )
    with pytest.raises(ConfigError, match="no such enabled inbox"):
        load_config(path)


def test_disabled_inbox_skipped(tmp_path: Path) -> None:
    write_principal(tmp_path)
    # principal lists "active" — must reference an enabled inbox or load fails.
    # Override the helper-default principal with one that lists active.
    write_contact(tmp_path, "principal", """
        contact_id = "principal"
        addresses = ["me@example.com"]
        is_principal = true
        daily_limit = "unlimited"
        inboxes = ["active"]
    """)
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:archived]
        enabled = false
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com

        [inbox:active]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        """,
    )
    cfg = load_config(path)
    assert "active" in cfg.inboxes
    assert "archived" not in cfg.inboxes


def test_no_inboxes_rejected(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs
        """,
    )
    with pytest.raises(ConfigError, match="no enabled"):
        load_config(path)


def test_legacy_contact_section_in_ini_rejected(tmp_path: Path) -> None:
    """If the migrator hasn't run yet (or someone re-added a legacy
    block), the loader refuses to start with a clear error pointing
    at the migrator."""
    write_principal(tmp_path)  # also a TOML
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [contact:stray]
        addresses = stray@example.com

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        """,
    )
    with pytest.raises(ConfigError, match="legacy.*contact"):
        load_config(path)


def test_legacy_allowed_contacts_line_rejected(tmp_path: Path) -> None:
    """The `allowed_contacts =` line is now derived from per-contact
    inboxes lists. Leaving the line in the INI is a misconfiguration."""
    write_principal(tmp_path)
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        allowed_contacts = principal
        """,
    )
    with pytest.raises(ConfigError, match="allowed_contacts is no longer accepted"):
        load_config(path)


# ---- [security] section ---------------------------------------------------


_SAMPLE_SECRET = "JBSWY3DPEHPK3PXP"


def _conf_with_security(tmp_path: Path, *, security_block: str) -> Path:
    write_principal(tmp_path)
    return write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com

        {security_block}
        """,
    )


def test_security_defaults_to_hotp(tmp_path: Path) -> None:
    path = _conf_with_security(
        tmp_path,
        security_block=f"[security]\n        totp_secret = {_SAMPLE_SECRET}",
    )
    cfg = load_config(path)
    assert cfg.security is not None
    assert cfg.security.auth_mode == "hotp"


def test_security_accepts_explicit_totp(tmp_path: Path) -> None:
    path = _conf_with_security(
        tmp_path,
        security_block=(
            f"[security]\n        totp_secret = {_SAMPLE_SECRET}"
            f"\n        auth_mode = totp"
        ),
    )
    cfg = load_config(path)
    assert cfg.security is not None
    assert cfg.security.auth_mode == "totp"


def test_security_rejects_bad_auth_mode(tmp_path: Path) -> None:
    path = _conf_with_security(
        tmp_path,
        security_block=(
            f"[security]\n        totp_secret = {_SAMPLE_SECRET}"
            f"\n        auth_mode = magic"
        ),
    )
    with pytest.raises(ConfigError, match="auth_mode must be one of"):
        load_config(path)


def test_security_rejects_invalid_secret(tmp_path: Path) -> None:
    path = _conf_with_security(
        tmp_path,
        security_block="[security]\n        totp_secret = not-base32!",
    )
    with pytest.raises(ConfigError, match="not a valid base32"):
        load_config(path)


# ---- [claude] section -----------------------------------------------------


_FAKE_CLAUDE_KEY = "sk-ant-api03-" + ("x" * 80)


def _conf_with_claude(tmp_path: Path, *, claude_block: str) -> Path:
    write_principal(tmp_path)
    return write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com

        {claude_block}
        """,
    )


def test_claude_section_absent_means_disabled(tmp_path: Path) -> None:
    path = _conf_with_claude(tmp_path, claude_block="")
    cfg = load_config(path)
    assert cfg.claude is None


def test_claude_minimal_uses_defaults(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block=f"[claude]\n        api_key = {_FAKE_CLAUDE_KEY}",
    )
    cfg = load_config(path)
    assert cfg.claude is not None
    assert cfg.claude.api_key == _FAKE_CLAUDE_KEY
    assert cfg.claude.default_model == "claude-haiku-4-5"
    assert cfg.claude.per_hour_max_invocations == 30
    assert cfg.claude.per_invocation_max_input_tokens == 8000


def test_claude_explicit_values_round_trip(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block=(
            f"[claude]\n        api_key = {_FAKE_CLAUDE_KEY}\n"
            "        default_model = claude-sonnet-4-6\n"
            "        per_hour_max_invocations = 10\n"
            "        per_invocation_max_input_tokens = 4000"
        ),
    )
    cfg = load_config(path)
    assert cfg.claude is not None
    assert cfg.claude.default_model == "claude-sonnet-4-6"
    assert cfg.claude.per_hour_max_invocations == 10
    assert cfg.claude.per_invocation_max_input_tokens == 4000


def test_claude_rejects_missing_api_key(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block="[claude]\n        default_model = claude-haiku-4-5",
    )
    with pytest.raises(ConfigError, match="api_key is required"):
        load_config(path)


def test_claude_rejects_malformed_api_key(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block="[claude]\n        api_key = not-a-real-key",
    )
    with pytest.raises(ConfigError, match="does not look like an Anthropic API key"):
        load_config(path)


def test_claude_rejects_short_api_key(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block="[claude]\n        api_key = sk-ant-tiny",
    )
    with pytest.raises(ConfigError, match="does not look like an Anthropic API key"):
        load_config(path)


def test_claude_rejects_zero_per_hour(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block=(
            f"[claude]\n        api_key = {_FAKE_CLAUDE_KEY}\n"
            "        per_hour_max_invocations = 0"
        ),
    )
    with pytest.raises(ConfigError, match="per_hour_max_invocations must be > 0"):
        load_config(path)


def test_claude_rejects_negative_per_invocation_tokens(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block=(
            f"[claude]\n        api_key = {_FAKE_CLAUDE_KEY}\n"
            "        per_invocation_max_input_tokens = -5"
        ),
    )
    with pytest.raises(ConfigError, match="per_invocation_max_input_tokens must be > 0"):
        load_config(path)


def test_claude_rejects_non_integer_per_hour(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block=(
            f"[claude]\n        api_key = {_FAKE_CLAUDE_KEY}\n"
            "        per_hour_max_invocations = lots"
        ),
    )
    with pytest.raises(ConfigError, match="integer field must be int"):
        load_config(path)


# ---- secrets.toml splice -------------------------------------------------


def _stash_machine_id(monkeypatch: pytest.MonkeyPatch, mid: bytes) -> None:
    """Patch secret_box.read_machine_id to return a fixed test value."""
    from daemon import secret_box
    monkeypatch.setattr(secret_box, "read_machine_id", lambda *, path=None: mid)


def test_load_splices_secrets_from_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets in secrets.toml override (replace) the INI fields."""
    _stash_machine_id(monkeypatch, bytes(16))
    write_principal(tmp_path)
    from daemon import secret_box
    secrets_path = tmp_path / "secrets.toml"
    secret_box.write_secrets_file(
        secrets_path,
        {
            "smtp": {"password": "smtp-from-secrets"},
            "security": {"totp_secret": _SAMPLE_SECRET},
            "imap.nightjar": {"password": "imap-from-secrets"},
        },
        machine_id=bytes(16),
    )
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        trusted_authserv = mx.google.com

        [security]

        [smtp]
        host = smtp.example.com
        port = 587
        user = me@example.com
        """,
    )
    from daemon.config import load as load_config_local
    cfg = load_config_local(path, secrets_path=secrets_path)
    assert cfg.smtp.password == "smtp-from-secrets"
    assert cfg.security.totp_secret == _SAMPLE_SECRET
    assert cfg.inboxes["nightjar"].imap_password == "imap-from-secrets"


def test_load_rejects_secret_in_both_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret in BOTH the INI and secrets.toml is a misconfigured
    state — refuse to start."""
    _stash_machine_id(monkeypatch, bytes(16))
    write_principal(tmp_path)
    from daemon import secret_box
    secrets_path = tmp_path / "secrets.toml"
    secret_box.write_secrets_file(
        secrets_path,
        {
            "smtp": {"password": "from-secrets"},
            "imap.nightjar": {"password": "imap-from-secrets"},
        },
        machine_id=bytes(16),
    )
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        trusted_authserv = mx.google.com

        [smtp]
        host = smtp.example.com
        port = 587
        user = me@example.com
        password = also-in-ini
        """,
    )
    from daemon.config import load as load_config_local
    with pytest.raises(ConfigError, match="in both"):
        load_config_local(path, secrets_path=secrets_path)


def test_load_secrets_file_world_readable_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stash_machine_id(monkeypatch, bytes(16))
    write_principal(tmp_path)
    from daemon import secret_box
    secrets_path = tmp_path / "secrets.toml"
    secret_box.write_secrets_file(
        secrets_path, {"smtp": {"password": "x"}}, machine_id=bytes(16),
    )
    import os
    os.chmod(secrets_path, 0o644)
    path = write_conf(
        tmp_path,
        f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        trusted_authserv = mx.google.com
        """,
    )
    from daemon.config import load as load_config_local
    with pytest.raises(ConfigError, match="could not load"):
        load_config_local(path, secrets_path=secrets_path)


# ---- end secrets splice section -------------------------------------------


def test_claude_api_key_is_not_in_repr(tmp_path: Path) -> None:
    """ClaudeConfig is frozen but the api_key still appears in repr by
    default. We can't suppress it without writing a custom __repr__,
    which is overkill — but we DO need to make sure no log line
    accidentally emits the dataclass. This test pins the contract:
    the Config object is fine, but anyone who logs `cfg.claude` is
    leaking the key. We guard against that by reading the key only
    in daemon/triage.py, never logging the dataclass."""
    path = _conf_with_claude(
        tmp_path,
        claude_block=f"[claude]\n        api_key = {_FAKE_CLAUDE_KEY}",
    )
    cfg = load_config(path)
    assert cfg.claude is not None
    assert cfg.claude.api_key == _FAKE_CLAUDE_KEY
    assert _FAKE_CLAUDE_KEY in repr(cfg.claude)
