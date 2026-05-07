"""Generate and install an auth secret in secrets.toml.

Two targets:
  - primary  (default) — the [security].totp_secret used for principal
    command auth. HOTP or TOTP depending on auth_mode.
  - secondary — the [security].secondary_hotp_secret used by the agent
    path's two-secret gate. HOTP only; the agent path bypasses
    auth_mode and always uses counter-based codes.

For both targets, the tool writes the obfuscated secret to
~/.config/nightjar/secrets.toml (chmod 600), splicing into any existing
content rather than replacing it. The plaintext never lands in
nightjar.conf.

Refuses to overwrite an existing secret without --force. If --force is
used and the existing primary lives in the INI (legacy install), the
tool refuses with a clear migration hint — silent relocation would
surprise operators.

Resets the relevant HOTP counter to 0 on every (re)provision so the
daemon and the freshly-paired authenticator agree on the starting
point. The OTHER counter is left alone — provisioning the secondary
must not invalidate the primary's session continuity, and vice versa.
"""
from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import auth, secret_box
from .config import DEFAULT_AUTH_MODE, DEFAULT_CONFIG_PATH, DEFAULT_SECRETS_PATH
from .state import State


TARGET_PRIMARY = "primary"
TARGET_SECONDARY = "secondary"


@dataclass(frozen=True)
class _TargetSpec:
    """Per-target wiring. Lives in this module rather than spreading
    if/else branches through run()."""
    secrets_section: str
    secrets_key: str
    label_suffix: str
    counter_setter_name: str  # method name on State

    @property
    def is_primary(self) -> bool:
        return self.secrets_key == "totp_secret"


_PRIMARY_SPEC = _TargetSpec(
    secrets_section="security",
    secrets_key="totp_secret",
    label_suffix="",
    counter_setter_name="set_hotp_counter",
)
_SECONDARY_SPEC = _TargetSpec(
    secrets_section="security",
    secrets_key="secondary_hotp_secret",
    label_suffix=" (secondary)",
    counter_setter_name="set_secondary_hotp_counter",
)


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


def _confirm_old_entry_removed(target_label: str) -> bool:
    """Block until the operator confirms they cleared any old entry.

    Critical for HOTP: an authenticator app paired against a previous
    secret will generate codes the daemon won't accept, and counter
    resync won't recover from that. Better to refuse to proceed than
    to ship the operator into a confusing failure mode.
    """
    print()
    print(f"Before we provision: do you have an EXISTING {target_label!r} entry")
    print("in your authenticator app?")
    print()
    print("If yes, delete it now. A new entry with this fresh secret will")
    print("replace it. The daemon will reset the relevant HOTP counter to")
    print("0 to match a freshly-paired app.")
    print()
    try:
        answer = input("Old entry removed (or never existed)? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


def _ini_has_legacy_secret(
    parser: configparser.ConfigParser, spec: _TargetSpec,
) -> bool:
    """Return True if the INI still carries a plaintext copy of the
    target secret. Tested separately from secrets.toml so the operator
    can be told exactly where the surprise lives."""
    if spec.secrets_section not in parser:
        return False
    return bool(parser[spec.secrets_section].get(spec.secrets_key, "").strip())


def _read_existing_secrets(secrets_path: Path) -> dict[str, dict[str, str]]:
    """Read and decode secrets.toml if it exists; empty dict otherwise.
    Surface decode failures rather than silently overwriting — a
    corrupted secrets file deserves the operator's attention."""
    if not secrets_path.exists():
        return {}
    return secret_box.read_secrets_file(secrets_path)


def _splice_and_write_secrets(
    secrets_path: Path,
    *,
    spec: _TargetSpec,
    new_value: str,
) -> None:
    """Read existing → set our field → write back. Atomic via the
    secret_box layer, which writes a tmp file and renames."""
    existing = _read_existing_secrets(secrets_path)
    section = existing.setdefault(spec.secrets_section, {})
    section[spec.secrets_key] = new_value
    secret_box.write_secrets_file(secrets_path, existing)


def run(
    config_path: Path | None,
    *,
    force: bool = False,
    target: str = TARGET_PRIMARY,
    secrets_path: Path | None = None,
) -> int:
    if not _is_local_tty():
        print(
            "nightjar: --setup-auth must be run at a local TTY (not over SSH).",
            file=sys.stderr,
        )
        return 4

    if target not in (TARGET_PRIMARY, TARGET_SECONDARY):
        print(
            f"nightjar: invalid target {target!r}; expected 'primary' or 'secondary'.",
            file=sys.stderr,
        )
        return 2

    spec = _PRIMARY_SPEC if target == TARGET_PRIMARY else _SECONDARY_SPEC

    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        print(f"nightjar: config not found at {path}", file=sys.stderr)
        return 2
    s_path = secrets_path or DEFAULT_SECRETS_PATH

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)

    # Refuse to silently relocate an INI-resident copy. Operators who
    # haven't run the migrator yet (legacy install) need to do that
    # first OR move the value manually; we will not erase plaintext
    # from the INI as a side effect of provisioning.
    if _ini_has_legacy_secret(parser, spec):
        print(
            f"nightjar: [{spec.secrets_section}].{spec.secrets_key} is in "
            f"plaintext in {path}.",
            file=sys.stderr,
        )
        print(
            "  This tool only writes to secrets.toml. To migrate the "
            "existing INI-resident secret:",
            file=sys.stderr,
        )
        print(
            "    - Start the daemon once; the migrator will move "
            "plaintext secrets to secrets.toml automatically.",
            file=sys.stderr,
        )
        print(
            "    - Or remove the INI line manually before re-running "
            "this tool with --force.",
            file=sys.stderr,
        )
        return 3

    # Refuse to overwrite an existing secret in secrets.toml without --force.
    try:
        existing_secrets = _read_existing_secrets(s_path)
    except secret_box.SecretBoxError as e:
        print(
            f"nightjar: cannot read {s_path}: {e}",
            file=sys.stderr,
        )
        print(
            "  Fix the file or remove it before re-running.",
            file=sys.stderr,
        )
        return 3
    section = existing_secrets.get(spec.secrets_section, {})
    if section.get(spec.secrets_key) and not force:
        print(
            f"nightjar: [{spec.secrets_section}].{spec.secrets_key} "
            f"already set in {s_path}. Re-run with --force to overwrite.",
            file=sys.stderr,
        )
        return 5

    target_label_for_app = (
        f"Nightjar{spec.label_suffix}".strip()
    )
    if not _confirm_old_entry_removed(target_label_for_app):
        print("nightjar: aborted.", file=sys.stderr)
        return 1

    # Resolve auth_mode (primary path may use TOTP; secondary always HOTP).
    auth_mode = (
        parser["security"].get("auth_mode", DEFAULT_AUTH_MODE).strip().lower()
        if "security" in parser
        else DEFAULT_AUTH_MODE
    )
    if spec.is_primary:
        if auth_mode not in {"hotp", "totp"}:
            print(
                f"nightjar: invalid [security].auth_mode={auth_mode!r}; "
                "expected hotp or totp.",
                file=sys.stderr,
            )
            return 2
        effective_mode = auth_mode
    else:
        # Secondary is always HOTP regardless of auth_mode. The agent
        # path's shape (codes in body, async reply windows) makes time-
        # based codes a non-starter.
        effective_mode = "hotp"

    secret = auth.generate_secret()
    account = _pick_account_label(parser)
    qr_label = f"{account}{spec.label_suffix}"
    if effective_mode == "hotp":
        uri = auth.hotp_provisioning_uri(
            secret=secret, account=qr_label, counter=0,
        )
    else:
        uri = auth.provisioning_uri(secret=secret, account=qr_label)

    # Splice the new secret into secrets.toml without disturbing other
    # entries (smtp password, claude api_key, imap passwords, the
    # *other* HOTP secret, etc.).
    try:
        _splice_and_write_secrets(s_path, spec=spec, new_value=secret)
    except secret_box.SecretBoxError as e:
        print(
            f"nightjar: failed to write {s_path}: {e}",
            file=sys.stderr,
        )
        return 3

    # Reset the relevant HOTP counter (the OTHER one is left alone).
    state_dir = _pick_state_dir(parser)
    state = State(db_path=state_dir / "state.db")
    setter = getattr(state, spec.counter_setter_name)
    setter(0)

    print()
    print("=" * 60)
    print(
        f"Nightjar {target.upper()} {effective_mode.upper()} provisioning"
    )
    print("=" * 60)
    print(f"Account label: {qr_label}")
    print()
    print(
        "Scan this QR code with a FOSS authenticator app (Aegis, 2FAS, "
        "FreeOTP):"
    )
    print()
    _render_qr(uri)
    print()
    print("Or paste this URI into your authenticator manually:")
    print(f"  {uri}")
    print()
    if effective_mode == "hotp":
        first_code = auth.hotp_at(secret, 1)
        print(
            f"Sanity check: the FIRST code your authenticator shows "
            f"should be {first_code}."
        )
        print(
            "If it doesn't match, your app paired against an old "
            "secret; delete the entry and rescan."
        )
        print()
    print(
        f"Secret written to {s_path} (chmod 600). "
        f"{'Primary' if spec.is_primary else 'Secondary'} HOTP counter reset to 0."
    )
    print()
    if not spec.is_primary:
        print(
            "IMPORTANT: store this secondary seed OFF-machine. The whole "
            "point of the two-secret gate is that compromise of the deck "
            "alone does not yield agent-path access. If both your "
            "authenticator backup and your deck end up in the same place "
            "(same cloud account, same drive), you have one factor, not "
            "two."
        )
        print()
    print("Keep the secret OFF cloud backups. Treat it like a passphrase.")
    return 0
