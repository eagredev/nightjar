"""Generate and install a TOTP secret in nightjar.conf.

Prints the provisioning URI and (if `qrencode` is on PATH) a terminal
QR code. The operator scans the QR or pastes the URI into an
authenticator app on their phone (Aegis, 2FAS, FreeOTP — never Google
Authenticator, which lacks export).

Refuses to overwrite an existing [security].totp_secret without --force.
The operator must be at a real TTY: a remote SSH session typing this
out into a log is the same risk as printing the secret to a public
channel.
"""
from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import auth
from .config import DEFAULT_CONFIG_PATH


def _is_local_tty() -> bool:
    """Refuse to print a secret to a remote session or non-TTY."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"):
        return False
    return True


def _render_qr(uri: str) -> None:
    """Try to render a terminal QR. Silent fallback if qrencode is missing."""
    qrencode = shutil.which("qrencode")
    if not qrencode:
        return
    try:
        subprocess.run(
            [qrencode, "-t", "ANSIUTF8", "-m", "1", uri],
            check=True,
        )
    except subprocess.CalledProcessError:
        pass


def _pick_account_label(parser: configparser.ConfigParser) -> str:
    """Use the principal's first email address as the QR label, if present."""
    for section_name in parser.sections():
        if not section_name.startswith("contact:"):
            continue
        sec = parser[section_name]
        is_principal = sec.get("is_principal", "").strip().lower() in {"true", "yes", "1", "on"}
        if not is_principal:
            continue
        addresses = [a.strip() for a in sec.get("addresses", "").split(",") if a.strip()]
        if addresses:
            return addresses[0]
    return "operator"


def run(config_path: Path | None, *, force: bool = False) -> int:
    if not _is_local_tty():
        print(
            "nightjar: --setup-totp must be run at a local TTY (not over SSH).",
            file=sys.stderr,
        )
        return 4

    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        print(f"nightjar: config not found at {path}", file=sys.stderr)
        return 2

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)

    existing = ""
    if "security" in parser:
        existing = parser["security"].get("totp_secret", "").strip()
    if existing and not force:
        print(
            "nightjar: [security].totp_secret already set. "
            "Re-run with --force to overwrite.",
            file=sys.stderr,
        )
        return 5

    secret = auth.generate_secret()
    account = _pick_account_label(parser)
    uri = auth.provisioning_uri(secret=secret, account=account)

    if "security" not in parser:
        parser["security"] = {}
    parser["security"]["totp_secret"] = secret
    parser["security"].setdefault("dead_mans_switch_window_minutes", "60")
    parser["security"].setdefault("dead_mans_switch_threshold", "3")

    # Write atomically and lock down permissions.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        parser.write(f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)

    print()
    print("=" * 60)
    print("Nightjar TOTP provisioning")
    print("=" * 60)
    print(f"Account label: {account}")
    print()
    print("Scan this QR code with a FOSS TOTP app (Aegis, 2FAS, FreeOTP):")
    print()
    _render_qr(uri)
    print()
    print("Or paste this URI into your authenticator manually:")
    print(f"  {uri}")
    print()
    print(f"Secret written to {path} (chmod 600).")
    print()
    print("Keep the secret OFF cloud backups. Treat it like a passphrase.")
    return 0
