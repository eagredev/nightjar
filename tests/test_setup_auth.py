"""Tests for daemon/setup_auth.py.

The tool has a few interactive guards (TTY check, "delete old entry"
confirmation) that we patch out per-test. The tests focus on:
  - which file gets written (secrets.toml, never INI)
  - splice semantics (existing secrets preserved)
  - --force / no-force semantics
  - INI-resident legacy primary detection
  - per-target counter reset (one resets, the other untouched)
  - distinct authenticator label per target
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from daemon import auth, secret_box, setup_auth
from daemon.state import State


_PRINCIPAL_TOML = textwrap.dedent("""
    contact_id     = "principal"
    addresses      = ["op@example.com"]
    is_principal   = true
    relationship   = "self"
    daily_limit    = 0
    inboxes        = ["nightjar"]
""").strip() + "\n"


def _stash_machine_id(monkeypatch: pytest.MonkeyPatch, mid: bytes = bytes(16)) -> None:
    monkeypatch.setattr(secret_box, "read_machine_id", lambda *, path=None: mid)


def _patch_interactive(
    monkeypatch: pytest.MonkeyPatch,
    *,
    confirm: bool = True,
    is_tty: bool = True,
) -> None:
    monkeypatch.setattr(setup_auth, "_is_local_tty", lambda: is_tty)
    monkeypatch.setattr(setup_auth, "_confirm_old_entry_removed", lambda label: confirm)


def _write_minimal_conf(tmp_path: Path) -> Path:
    """Produce a minimum-viable nightjar.conf the tool will accept."""
    contacts_dir = tmp_path / "contacts"
    contacts_dir.mkdir()
    (contacts_dir / "principal.toml").write_text(_PRINCIPAL_TOML)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()

    conf = textwrap.dedent(f"""
        [daemon]
        contacts_dir = {contacts_dir}
        state_dir = {state_dir}
        log_dir = {log_dir}
        notes_dir = {notes_dir}

        [security]
        auth_mode = hotp

        [smtp]
        host = smtp.example.com
        port = 587
        user = op@example.com
        from_name = Op
        from_addr = op@example.com

        [inbox:nightjar]
        imap_host = imap.example.com
        imap_user = op@example.com
        trusted_authserv = mx.example.com

        [contact:principal]
        addresses = op@example.com
        is_principal = true
        daily_limit = 0
        inboxes = nightjar
    """).strip() + "\n"
    path = tmp_path / "nightjar.conf"
    path.write_text(conf, encoding="utf-8")
    path.chmod(0o600)
    return path


# ---- Primary target: writes to secrets.toml ------------------------------


def test_primary_writes_to_secrets_toml_not_ini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stash_machine_id(monkeypatch)
    _patch_interactive(monkeypatch)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"

    rc = setup_auth.run(
        conf_path, target=setup_auth.TARGET_PRIMARY, secrets_path=secrets_path,
    )
    assert rc == 0

    # secrets.toml exists and contains the primary secret.
    assert secrets_path.exists()
    decoded = secret_box.read_secrets_file(secrets_path)
    assert "security" in decoded
    assert "totp_secret" in decoded["security"]
    assert auth.is_valid_secret(decoded["security"]["totp_secret"])

    # The INI was NOT modified to add the secret.
    ini_text = conf_path.read_text()
    assert "totp_secret" not in ini_text


def test_primary_resets_primary_counter_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stash_machine_id(monkeypatch)
    _patch_interactive(monkeypatch)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"
    state_dir = tmp_path / "state"
    s = State(db_path=state_dir / "state.db")
    s.set_hotp_counter(7)
    s.set_secondary_hotp_counter(11)

    rc = setup_auth.run(
        conf_path, target=setup_auth.TARGET_PRIMARY, secrets_path=secrets_path,
    )
    assert rc == 0
    assert s.get_hotp_counter() == 0
    assert s.get_secondary_hotp_counter() == 11  # untouched


# ---- Secondary target ----------------------------------------------------


def test_secondary_writes_to_secrets_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stash_machine_id(monkeypatch)
    _patch_interactive(monkeypatch)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"

    rc = setup_auth.run(
        conf_path, target=setup_auth.TARGET_SECONDARY, secrets_path=secrets_path,
    )
    assert rc == 0
    decoded = secret_box.read_secrets_file(secrets_path)
    assert "secondary_hotp_secret" in decoded.get("security", {})
    assert auth.is_valid_secret(decoded["security"]["secondary_hotp_secret"])
    # Should NOT have created a primary entry as a side effect.
    assert "totp_secret" not in decoded.get("security", {})


def test_secondary_resets_secondary_counter_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stash_machine_id(monkeypatch)
    _patch_interactive(monkeypatch)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"
    state_dir = tmp_path / "state"
    s = State(db_path=state_dir / "state.db")
    s.set_hotp_counter(7)
    s.set_secondary_hotp_counter(11)

    rc = setup_auth.run(
        conf_path, target=setup_auth.TARGET_SECONDARY, secrets_path=secrets_path,
    )
    assert rc == 0
    assert s.get_hotp_counter() == 7  # untouched
    assert s.get_secondary_hotp_counter() == 0


# ---- Splice preserves existing secrets ------------------------------------


def test_provisioning_secondary_preserves_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing primary in secrets.toml stays put when we add the
    secondary — the splice must not blow it away."""
    _stash_machine_id(monkeypatch)
    _patch_interactive(monkeypatch)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"

    # Pre-populate secrets.toml with primary, smtp password, claude key.
    pre_existing = {
        "security": {"totp_secret": auth.generate_secret()},
        "smtp": {"password": "smtp-pass"},
        "claude": {"api_key": "sk-ant-test-" + "x" * 60},
    }
    secret_box.write_secrets_file(
        secrets_path, pre_existing, machine_id=bytes(16),
    )

    rc = setup_auth.run(
        conf_path, target=setup_auth.TARGET_SECONDARY, secrets_path=secrets_path,
    )
    assert rc == 0

    decoded = secret_box.read_secrets_file(secrets_path)
    # Primary preserved.
    assert decoded["security"]["totp_secret"] == pre_existing["security"]["totp_secret"]
    # Secondary added.
    assert decoded["security"]["secondary_hotp_secret"]
    # Other sections preserved.
    assert decoded["smtp"]["password"] == "smtp-pass"
    assert decoded["claude"]["api_key"] == pre_existing["claude"]["api_key"]


def test_provisioning_primary_preserves_secondary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric: re-provisioning primary doesn't blow away the secondary."""
    _stash_machine_id(monkeypatch)
    _patch_interactive(monkeypatch)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"

    secondary_seed = auth.generate_secret()
    secret_box.write_secrets_file(
        secrets_path,
        {"security": {"secondary_hotp_secret": secondary_seed}},
        machine_id=bytes(16),
    )

    rc = setup_auth.run(
        conf_path, target=setup_auth.TARGET_PRIMARY, secrets_path=secrets_path,
    )
    assert rc == 0
    decoded = secret_box.read_secrets_file(secrets_path)
    assert decoded["security"]["secondary_hotp_secret"] == secondary_seed
    assert decoded["security"]["totp_secret"]


# ---- --force semantics ----------------------------------------------------


def test_refuses_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stash_machine_id(monkeypatch)
    _patch_interactive(monkeypatch)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"

    secret_box.write_secrets_file(
        secrets_path,
        {"security": {"totp_secret": auth.generate_secret()}},
        machine_id=bytes(16),
    )
    rc = setup_auth.run(
        conf_path, target=setup_auth.TARGET_PRIMARY, secrets_path=secrets_path,
    )
    assert rc == 5  # exit code for "exists, no force"


def test_force_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stash_machine_id(monkeypatch)
    _patch_interactive(monkeypatch)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"

    old = auth.generate_secret()
    secret_box.write_secrets_file(
        secrets_path,
        {"security": {"totp_secret": old}},
        machine_id=bytes(16),
    )
    rc = setup_auth.run(
        conf_path, target=setup_auth.TARGET_PRIMARY,
        secrets_path=secrets_path, force=True,
    )
    assert rc == 0
    decoded = secret_box.read_secrets_file(secrets_path)
    assert decoded["security"]["totp_secret"] != old


# ---- INI-resident legacy primary refusal ---------------------------------


def test_refuses_when_legacy_ini_secret_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the operator has an old-style plaintext primary in the INI,
    the tool refuses cleanly and points at the migrator."""
    _stash_machine_id(monkeypatch)
    _patch_interactive(monkeypatch)
    conf_path = _write_minimal_conf(tmp_path)
    # Inject a plaintext totp_secret in the INI.
    text = conf_path.read_text()
    text = text.replace(
        "[security]\nauth_mode = hotp",
        "[security]\nauth_mode = hotp\ntotp_secret = " + auth.generate_secret(),
    )
    conf_path.write_text(text, encoding="utf-8")

    secrets_path = tmp_path / "secrets.toml"
    rc = setup_auth.run(
        conf_path, target=setup_auth.TARGET_PRIMARY,
        secrets_path=secrets_path, force=True,
    )
    assert rc == 3  # exit code for INI-resident legacy detection
    # No secrets.toml was created as a side effect.
    assert not secrets_path.exists()


# ---- Distinct labels per target ------------------------------------------


def test_different_targets_produce_different_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provision both targets; the seeds must be independent."""
    _stash_machine_id(monkeypatch)
    _patch_interactive(monkeypatch)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"

    setup_auth.run(
        conf_path, target=setup_auth.TARGET_PRIMARY, secrets_path=secrets_path,
    )
    setup_auth.run(
        conf_path, target=setup_auth.TARGET_SECONDARY, secrets_path=secrets_path,
    )
    decoded = secret_box.read_secrets_file(secrets_path)
    primary = decoded["security"]["totp_secret"]
    secondary = decoded["security"]["secondary_hotp_secret"]
    assert primary != secondary


# ---- Validation surfaces -------------------------------------------------


def test_invalid_target_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_interactive(monkeypatch)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"
    rc = setup_auth.run(
        conf_path, target="ternary", secrets_path=secrets_path,
    )
    assert rc == 2


def test_non_tty_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_interactive(monkeypatch, is_tty=False)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"
    rc = setup_auth.run(
        conf_path, target=setup_auth.TARGET_PRIMARY, secrets_path=secrets_path,
    )
    assert rc == 4


def test_user_declines_old_entry_confirmation_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stash_machine_id(monkeypatch)
    _patch_interactive(monkeypatch, confirm=False)
    conf_path = _write_minimal_conf(tmp_path)
    secrets_path = tmp_path / "secrets.toml"
    rc = setup_auth.run(
        conf_path, target=setup_auth.TARGET_PRIMARY, secrets_path=secrets_path,
    )
    assert rc == 1
    assert not secrets_path.exists()
