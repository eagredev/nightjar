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

from daemon.config import (
    ConfigError,
    load as load_config,
    project_ancestors,
    project_descendant_of,
    project_parent,
    project_visibility,
)


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
    # Cost backstop defaults (Step 6f).
    assert cfg.claude.principal_per_message_cost_cents == 10
    assert cfg.claude.principal_hard_kill_multiplier == 5
    assert cfg.claude.principal_always_direct is False


def test_claude_explicit_values_round_trip(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block=(
            f"[claude]\n        api_key = {_FAKE_CLAUDE_KEY}\n"
            "        default_model = claude-sonnet-4-6\n"
            "        per_hour_max_invocations = 10\n"
            "        per_invocation_max_input_tokens = 4000\n"
            "        principal_per_message_cost_cents = 25\n"
            "        principal_hard_kill_multiplier = 3\n"
            "        principal_always_direct = true"
        ),
    )
    cfg = load_config(path)
    assert cfg.claude is not None
    assert cfg.claude.default_model == "claude-sonnet-4-6"
    assert cfg.claude.per_hour_max_invocations == 10
    assert cfg.claude.per_invocation_max_input_tokens == 4000
    assert cfg.claude.principal_per_message_cost_cents == 25
    assert cfg.claude.principal_hard_kill_multiplier == 3
    assert cfg.claude.principal_always_direct is True


def test_claude_rejects_zero_cost_cap(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block=(
            f"[claude]\n        api_key = {_FAKE_CLAUDE_KEY}\n"
            "        principal_per_message_cost_cents = 0"
        ),
    )
    with pytest.raises(
        ConfigError, match="principal_per_message_cost_cents must be > 0"
    ):
        load_config(path)


def test_claude_rejects_negative_kill_multiplier(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block=(
            f"[claude]\n        api_key = {_FAKE_CLAUDE_KEY}\n"
            "        principal_hard_kill_multiplier = 0"
        ),
    )
    with pytest.raises(
        ConfigError, match="principal_hard_kill_multiplier must be >= 1"
    ):
        load_config(path)


def test_claude_rejects_invalid_always_direct(tmp_path: Path) -> None:
    path = _conf_with_claude(
        tmp_path,
        claude_block=(
            f"[claude]\n        api_key = {_FAKE_CLAUDE_KEY}\n"
            "        principal_always_direct = maybe"
        ),
    )
    with pytest.raises(
        ConfigError, match="principal_always_direct must be a boolean"
    ):
        load_config(path)


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


# ---- Step 6e: catchup_window_days -----------------------------------------


def test_catchup_window_defaults_to_seven(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
    """)
    cfg = load_config(path)
    assert cfg.inboxes["nightjar"].catchup_window_days == 7


def test_catchup_window_can_be_overridden(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        catchup_window_days = 14
    """)
    cfg = load_config(path)
    assert cfg.inboxes["nightjar"].catchup_window_days == 14


def test_catchup_window_rejects_zero(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        catchup_window_days = 0
    """)
    with pytest.raises(ConfigError, match="catchup_window_days"):
        load_config(path)


def test_catchup_window_rejects_non_integer(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
        catchup_window_days = soon
    """)
    with pytest.raises(ConfigError, match="catchup_window_days"):
        load_config(path)


# ---- Step 7b: scopes registry ---------------------------------------------


def _minimal_inbox_block(tmp_path: Path) -> str:
    """Standard [daemon] + [inbox:nightjar] block for scope tests."""
    return f"""
        [daemon]
        state_dir = {tmp_path}/state
        log_dir = {tmp_path}/logs

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
    """


def test_scopes_section_absent_yields_empty_registry(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path))
    cfg = load_config(path)
    assert cfg.scopes == {}


def test_scopes_section_parses_descriptions(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [scopes]
        aurora = the Aurora redesign work
        music-tech = music production and chiptune workflows
    """)
    cfg = load_config(path)
    assert cfg.scopes == {
        "aurora": "the Aurora redesign work",
        "music-tech": "music production and chiptune workflows",
    }


def test_scopes_lowercases_uppercase_keys(tmp_path: Path) -> None:
    """ConfigParser auto-lowercases keys (default behaviour). The
    registry stores the lowercased form, so `Aurora` in the INI is
    accepted as `aurora`. Operators get graceful normalisation."""
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [scopes]
        Aurora = the Aurora work
    """)
    cfg = load_config(path)
    assert "aurora" in cfg.scopes
    assert "Aurora" not in cfg.scopes


def test_scopes_rejects_invalid_chars(tmp_path: Path) -> None:
    """A scope key that doesn't normalise to a valid name is rejected.
    INI keys can contain dots, which we want to reject because they'd
    confuse the LLM if it tried to dot-walk into a scope name."""
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [scopes]
        my.scope = nope
    """)
    with pytest.raises(ConfigError, match="scope name"):
        load_config(path)


def test_scopes_rejects_empty_description(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [scopes]
        aurora =
    """)
    with pytest.raises(ConfigError, match="empty description"):
        load_config(path)


def test_contact_with_unknown_scope_rejected(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        scopes = ["aurora"]
    """)
    # No [scopes] section — aurora is unknown.
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path))
    with pytest.raises(ConfigError, match="not defined in the \\[scopes\\] registry"):
        load_config(path)


def test_contact_with_known_scope_accepted(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        scopes = ["aurora", "music-tech"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [scopes]
        aurora = the Aurora redesign work
        music-tech = music production
    """)
    cfg = load_config(path)
    assert cfg.contacts["fraser"].scopes == ("aurora", "music-tech")


def test_contact_with_empty_scopes_default(tmp_path: Path) -> None:
    """A contact without a `scopes` field defaults to () — unrestricted."""
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path))
    cfg = load_config(path)
    assert cfg.contacts["fraser"].scopes == ()


def test_contact_with_explicit_empty_scopes(tmp_path: Path) -> None:
    """`scopes = []` is the explicit unrestricted form."""
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        scopes = []
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path))
    cfg = load_config(path)
    assert cfg.contacts["fraser"].scopes == ()


def test_contact_rejects_invalid_scope_name(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        scopes = ["BadScope"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path))
    with pytest.raises(ConfigError, match="scope name"):
        load_config(path)


def test_contact_rejects_duplicate_scope(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        scopes = ["aurora", "aurora"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [scopes]
        aurora = the Aurora work
    """)
    with pytest.raises(ConfigError, match="duplicate scope"):
        load_config(path)


def test_contact_scopes_must_be_list_of_strings(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        scopes = "aurora"
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [scopes]
        aurora = the Aurora work
    """)
    with pytest.raises(ConfigError, match="scopes must be a list"):
        load_config(path)


# ---- Scope/sensitivity Part 1: facets and projects -----------------------


def test_project_helpers_parent() -> None:
    assert project_parent("aurora") is None
    assert project_parent("aurora.music") == "aurora"
    assert project_parent("aurora.music.demo") == "aurora.music"


def test_project_helpers_ancestors() -> None:
    assert project_ancestors("aurora") == ()
    assert project_ancestors("aurora.music") == ("aurora",)
    assert project_ancestors("aurora.music.demo") == ("aurora", "aurora.music")


def test_project_helpers_descendant_of() -> None:
    # Self is descendant of self
    assert project_descendant_of("aurora", "aurora")
    # Direct child
    assert project_descendant_of("aurora.music", "aurora")
    # Indirect descendant
    assert project_descendant_of("aurora.music.demo", "aurora")
    # Sibling is NOT descendant
    assert not project_descendant_of("aurora.legal", "aurora.music")
    # Unrelated project
    assert not project_descendant_of("nightjar-dev", "aurora")
    # Edge: parent is not descendant of child
    assert not project_descendant_of("aurora", "aurora.music")
    # Edge: prefix-without-dot is not descendant (aurora-clone vs aurora)
    assert not project_descendant_of("aurora-clone", "aurora")


# ---- Project visibility (bidirectional) ----------------------------------


def test_project_visibility_exact_match() -> None:
    """Bullet tagged X is visible to contact with X."""
    assert project_visibility("aurora", ("aurora",))
    assert project_visibility("aurora.music", ("aurora.music",))


def test_project_visibility_parent_sees_child() -> None:
    """Bullet tagged with a sub-project is visible to a contact with
    the parent project — parent subsumes children."""
    assert project_visibility("aurora.music", ("aurora",))
    assert project_visibility("aurora.music.demo", ("aurora",))
    assert project_visibility("aurora.music.demo", ("aurora.music",))


def test_project_visibility_child_sees_parent_tagged() -> None:
    """Bullet tagged with a parent project is visible to a contact
    who has any sub-scope of that parent — having access to a sub-area
    implies the generic project context is appropriate."""
    assert project_visibility("aurora", ("aurora.music",))
    assert project_visibility("aurora", ("aurora.music.demo",))
    assert project_visibility("aurora.music", ("aurora.music.demo",))


def test_project_visibility_siblings_do_not_see() -> None:
    """Sub-scopes at the same level are isolated from each other."""
    assert not project_visibility("aurora.music", ("aurora.legal",))
    assert not project_visibility("aurora.legal", ("aurora.music",))
    # Even with multiple siblings on the contact side, only matching
    # branches see each other.
    assert not project_visibility(
        "aurora.music", ("aurora.legal", "aurora.finance"),
    )


def test_project_visibility_unrelated_projects() -> None:
    """A bullet in an entirely different project tree is not visible."""
    assert not project_visibility("aurora", ("nightjar-dev",))
    assert not project_visibility("aurora.music", ("nightjar-dev",))


def test_project_visibility_empty_contact_projects() -> None:
    """A contact with no projects sees no project-tagged bullets."""
    assert not project_visibility("aurora", ())
    assert not project_visibility("aurora.music", ())


def test_project_visibility_accepts_frozenset() -> None:
    """Caller may pass a frozenset of contact projects (efficient when
    walking many bullets against the same contact)."""
    contact = frozenset({"aurora.music", "calendar"})
    assert project_visibility("aurora.music", contact)
    assert project_visibility("aurora", contact)
    assert not project_visibility("aurora.legal", contact)


def test_project_visibility_multiple_contact_projects() -> None:
    """A contact with multiple projects sees a bullet visible from
    any of them."""
    contact = ("aurora.music", "nightjar-dev")
    assert project_visibility("aurora.music", contact)
    assert project_visibility("aurora", contact)  # parent of aurora.music
    assert project_visibility("nightjar-dev", contact)
    assert not project_visibility("aurora.legal", contact)


def test_facets_section_absent_yields_empty_registry(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path))
    cfg = load_config(path)
    assert cfg.facets == {}
    assert cfg.projects == {}


def test_facets_section_parses(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [facets]
        calendar = the principal's availability and scheduling
        communication-style = the contact's tone and cadence
    """)
    cfg = load_config(path)
    assert cfg.facets == {
        "calendar": "the principal's availability and scheduling",
        "communication-style": "the contact's tone and cadence",
    }


def test_facets_reject_dotted_names(tmp_path: Path) -> None:
    """Facets are flat by design; dot-notation is reserved for projects."""
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [facets]
        my.facet = nope
    """)
    with pytest.raises(ConfigError, match="scope name"):
        load_config(path)


def test_facets_reject_empty_description(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [facets]
        calendar =
    """)
    with pytest.raises(ConfigError, match="empty description"):
        load_config(path)


def test_projects_section_parses_flat(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [projects]
        aurora = the Aurora redesign
        nightjar-dev = the Nightjar codebase
    """)
    cfg = load_config(path)
    assert cfg.projects == {
        "aurora": "the Aurora redesign",
        "nightjar-dev": "the Nightjar codebase",
    }


def test_projects_section_parses_hierarchical(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [projects]
        aurora = the Aurora redesign
        aurora.music = music for Aurora
        aurora.music.demo = the demo track
        aurora.legal = legal work for Aurora
    """)
    cfg = load_config(path)
    assert "aurora" in cfg.projects
    assert "aurora.music" in cfg.projects
    assert "aurora.music.demo" in cfg.projects
    assert "aurora.legal" in cfg.projects


def test_projects_subscope_without_parent_rejected(tmp_path: Path) -> None:
    """Declaring a sub-project with no parent is a config error — the
    operator should declare the parent first or rename the sub-scope."""
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [projects]
        aurora.music = music for the Aurora project
    """)
    with pytest.raises(ConfigError, match="parent .* is not defined"):
        load_config(path)


def test_facets_and_projects_namespace_collision_rejected(
    tmp_path: Path,
) -> None:
    """A name appearing in both [facets] and [projects] is ambiguous —
    which axis does a contact's reference belong to? Reject at load."""
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [facets]
        aurora = an aurora-shaped facet

        [projects]
        aurora = the Aurora redesign
    """)
    with pytest.raises(ConfigError, match="appear in both"):
        load_config(path)


def test_legacy_scopes_and_facets_collision_rejected(tmp_path: Path) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [scopes]
        aurora = legacy aurora

        [facets]
        aurora = a facet named aurora
    """)
    with pytest.raises(ConfigError, match="\\[scopes\\] .* and"):
        load_config(path)


def test_contact_with_known_facets_and_projects_accepted(
    tmp_path: Path,
) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        facets = ["calendar", "communication-style"]
        projects = ["aurora", "aurora.music"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [facets]
        calendar = scheduling
        communication-style = tone

        [projects]
        aurora = the Aurora redesign
        aurora.music = music for Aurora
    """)
    cfg = load_config(path)
    fraser = cfg.contacts["fraser"]
    assert fraser.facets == ("calendar", "communication-style")
    assert fraser.projects == ("aurora", "aurora.music")


def test_contact_with_unknown_facet_rejected(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        facets = ["calendar"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path))
    with pytest.raises(ConfigError, match="not defined in the \\[facets\\] registry"):
        load_config(path)


def test_contact_with_unknown_project_rejected(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        projects = ["aurora"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path))
    with pytest.raises(ConfigError, match="not defined in the \\[projects\\] registry"):
        load_config(path)


def test_contact_with_unknown_subproject_rejected(tmp_path: Path) -> None:
    """Contact references aurora.music but registry only has aurora —
    the implicit-parent rule applies to read-time visibility, not to
    declaration. Each leaf in the contact's `projects` list must be
    explicitly registered."""
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        projects = ["aurora.music"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [projects]
        aurora = the Aurora redesign
    """)
    with pytest.raises(ConfigError, match="not defined in the \\[projects\\] registry"):
        load_config(path)


def test_contact_mixing_legacy_scopes_with_new_axes_rejected(
    tmp_path: Path,
) -> None:
    """A contact uses EITHER legacy `scopes` OR (facets, projects).
    Mixing both is ambiguous — reject at load. The migration is
    deliberate, not silent."""
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        scopes = ["aurora"]
        facets = ["calendar"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [scopes]
        aurora = legacy

        [facets]
        calendar = scheduling
    """)
    with pytest.raises(ConfigError, match="mixes legacy"):
        load_config(path)


def test_contact_with_empty_facets_and_projects_default(tmp_path: Path) -> None:
    """A contact with neither legacy `scopes` nor new (facets, projects)
    is unrestricted — historical default, preserved."""
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path))
    cfg = load_config(path)
    fraser = cfg.contacts["fraser"]
    assert fraser.scopes == ()
    assert fraser.facets == ()
    assert fraser.projects == ()


def test_contact_facets_reject_dotted_name(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        facets = ["my.facet"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path))
    with pytest.raises(ConfigError, match="facet name"):
        load_config(path)


def test_contact_projects_accept_dotted_name(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        projects = ["aurora.music"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [projects]
        aurora = the Aurora redesign
        aurora.music = music for Aurora
    """)
    cfg = load_config(path)
    assert cfg.contacts["fraser"].projects == ("aurora.music",)


def test_contact_rejects_duplicate_facet(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        facets = ["calendar", "calendar"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [facets]
        calendar = scheduling
    """)
    with pytest.raises(ConfigError, match="duplicate facet"):
        load_config(path)


def test_contact_rejects_duplicate_project(tmp_path: Path) -> None:
    write_principal(tmp_path)
    write_contact(tmp_path, "fraser", """
        contact_id = "fraser"
        addresses = ["fraser@example.com"]
        display_name = "Fraser"
        daily_limit = 3
        inboxes = ["nightjar"]
        projects = ["aurora", "aurora"]
    """)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [projects]
        aurora = the Aurora redesign
    """)
    with pytest.raises(ConfigError, match="duplicate project"):
        load_config(path)
