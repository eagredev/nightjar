"""Generate and install an auth secret in nightjar.conf.

Picks the right kind of provisioning URI for the configured auth_mode
(hotp or totp), prints the URI and (if `qrencode` is on PATH) a terminal
QR code. The operator scans the QR or pastes the URI into an
authenticator app on their phone (Aegis, 2FAS, FreeOTP — never Google
Authenticator, which lacks export).

The shared base32 secret is reused across modes; only the URI scheme
and the verification primitive differ. So switching auth_mode does NOT
require regenerating the secret, but it DOES require rescanning a fresh
QR (the authenticator stores the URI scheme alongside the secret).

Refuses to overwrite an existing [security].totp_secret without --force.
Resets the HOTP counter to 0 on every (re)provision so the daemon and
the freshly-paired authenticator agree on the starting point.
"""
from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import auth
from .config import DEFAULT_AUTH_MODE, DEFAULT_CONFIG_PATH
from .state import State


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


def _pick_state_dir(parser: configparser.ConfigParser) -> Path:
    daemon = parser["daemon"] if "daemon" in parser else {}
    raw = daemon.get("state_dir", "~/.local/share/nightjar") if daemon else "~/.local/share/nightjar"
    return Path(os.path.expanduser(raw))


def _confirm_old_entry_removed() -> bool:
    """Block until the operator confirms they cleared any old Nightjar entry.

    Critical for HOTP: an authenticator app paired against a previous
    secret will generate codes the daemon won't accept, and counter
    resync won't recover from that. Better to refuse to proceed than
    to ship the operator into a confusing failure mode.
    """
    print()
    print("Before we provision: do you have an EXISTING 'Nightjar' entry")
    print("in your authenticator app?")
    print()
    print("If yes, delete it now. A new entry with this fresh secret will")
    print("replace it. The daemon will reset the HOTP counter to 0 to")
    print("match a freshly-paired app.")
    print()
    try:
        answer = input("Old entry removed (or never existed)? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


def run(config_path: Path | None, *, force: bool = False) -> int:
    if not _is_local_tty():
        print(
            "nightjar: --setup-auth must be run at a local TTY (not over SSH).",
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

    if not _confirm_old_entry_removed():
        print("nightjar: aborted.", file=sys.stderr)
        return 1

    auth_mode = (
        parser["security"].get("auth_mode", DEFAULT_AUTH_MODE).strip().lower()
        if "security" in parser
        else DEFAULT_AUTH_MODE
    )
    if auth_mode not in {"hotp", "totp"}:
        print(
            f"nightjar: invalid [security].auth_mode={auth_mode!r}; expected hotp or totp.",
            file=sys.stderr,
        )
        return 2

    secret = auth.generate_secret()
    account = _pick_account_label(parser)
    if auth_mode == "hotp":
        uri = auth.hotp_provisioning_uri(secret=secret, account=account, counter=0)
    else:
        uri = auth.provisioning_uri(secret=secret, account=account)

    if "security" not in parser:
        parser["security"] = {}
    parser["security"]["totp_secret"] = secret
    parser["security"].setdefault("auth_mode", auth_mode)
    parser["security"].setdefault("dead_mans_switch_window_minutes", "60")
    parser["security"].setdefault("dead_mans_switch_threshold", "3")

    # Write atomically and lock down permissions.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        parser.write(f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)

    # Reset the HOTP counter for any mode change. For HOTP this puts the
    # daemon at counter=0 so the first code the authenticator emits
    # (counter=1) is what gets accepted. For TOTP the field is unused
    # but resetting it costs nothing and keeps state tidy.
    state_dir = _pick_state_dir(parser)
    state = State(db_path=state_dir / "state.db")
    state.set_hotp_counter(0)

    print()
    print("=" * 60)
    print(f"Nightjar {auth_mode.upper()} provisioning")
    print("=" * 60)
    print(f"Account label: {account}")
    print()
    print("Scan this QR code with a FOSS authenticator app (Aegis, 2FAS, FreeOTP):")
    print()
    _render_qr(uri)
    print()
    print("Or paste this URI into your authenticator manually:")
    print(f"  {uri}")
    print()
    if auth_mode == "hotp":
        first_code = auth.hotp_at(secret, 1)
        print(f"Sanity check: the FIRST code your authenticator shows should be {first_code}.")
        print("If it doesn't match, your app paired against an old secret;")
        print("delete the entry and rescan.")
        print()
    print(f"Secret written to {path} (chmod 600). HOTP counter reset to 0.")
    print()
    print("Keep the secret OFF cloud backups. Treat it like a passphrase.")
    return 0
