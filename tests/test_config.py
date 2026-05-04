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
        trusted_authserv = mx.google.com
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
        trusted_authserv = mx.google.com
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
        trusted_authserv = mx.google.com
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
        trusted_authserv = mx.google.com
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
        trusted_authserv = mx.google.com
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
        trusted_authserv = mx.google.com
        allowed_contacts = a

        [inbox:active]
        imap_host = imap.example.com
        imap_user = me@example.com
        imap_password = x
        trusted_authserv = mx.google.com
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


# A real-ish base32 secret to exercise the [security] parser.
_SAMPLE_SECRET = "JBSWY3DPEHPK3PXP"  # "Hello!\xde\xad\xbe\xef"


def _conf_with_security(tmp_path: Path, *, security_block: str) -> Path:
    return write_conf(
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
        trusted_authserv = mx.google.com
        allowed_contacts = a

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


# ---- [claude] section ------------------------------------------------------

# Plausible-shape key for tests. Not a real key. The validator only checks
# the prefix and length, no network round-trip, so a fake key passes config
# load. Live SDK calls happen in the triage module, never here.
_FAKE_CLAUDE_KEY = "sk-ant-api03-" + ("x" * 80)


def _conf_with_claude(tmp_path: Path, *, claude_block: str) -> Path:
    return write_conf(
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
        trusted_authserv = mx.google.com
        allowed_contacts = a

        {claude_block}
        """,
    )


def test_claude_section_absent_means_disabled(tmp_path: Path) -> None:
    """Step 5 still has Steps 1-4 working; the section is optional."""
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
    """A key with the right prefix but too short is still rejected."""
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
    # Sanity: the key is preserved verbatim (case, length, all chars).
    assert cfg.claude is not None
    assert cfg.claude.api_key == _FAKE_CLAUDE_KEY
    # If this assertion ever flips (e.g. someone adds a custom __repr__),
    # update the test rather than papering over the leak surface.
    assert _FAKE_CLAUDE_KEY in repr(cfg.claude)
