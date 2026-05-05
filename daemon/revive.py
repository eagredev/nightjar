"""`nightjar --revive` — clear the dead-man's-switch.

Two gates must both pass:
  1. Physical-presence check: stdin/stdout are TTYs, no SSH_CONNECTION,
     XDG_SESSION_TYPE indicates a local session.
  2. Live TOTP code typed at the terminal.

Either gate alone is insufficient. Both together prove the operator
is sitting at the machine with their authenticator in hand. Clears
panic_until_revived in SQLite and writes an incident report.
"""
from __future__ import annotations

import datetime
import getpass
import os
import sys
from pathlib import Path

from . import auth
from .config import Config
from .state import State


INCIDENTS_DIR = Path("~/nightjar/incidents").expanduser()


def _is_physically_present() -> tuple[bool, str]:
    """Return (ok, reason). Reason is human-readable when ok is False."""
    if not sys.stdin.isatty():
        return False, "stdin is not a TTY"
    if not sys.stdout.isatty():
        return False, "stdout is not a TTY"
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"):
        return False, "running over SSH"
    session_type = os.environ.get("XDG_SESSION_TYPE", "")
    if session_type and session_type not in {"tty", "wayland", "x11"}:
        return False, f"XDG_SESSION_TYPE={session_type!r} is not a local session"
    return True, ""


def _format_failures(rows: list[dict]) -> list[str]:
    if not rows:
        return ["  (none recorded)"]
    out = []
    for r in rows:
        ts = datetime.datetime.fromtimestamp(
            r["ts"], tz=datetime.timezone.utc
        ).astimezone().isoformat(timespec="seconds")
        out.append(f"  {ts}  {r['from_addr']}  {r['reason']}")
    return out


def _write_incident(state_dir: Path, info: dict, recent: list[dict]) -> Path:
    INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
    when = datetime.datetime.now(datetime.timezone.utc)
    fname = INCIDENTS_DIR / f"panic-{when.strftime('%Y-%m-%dT%H-%M')}.md"
    lines = [
        f"# Nightjar panic incident: {when.isoformat()}",
        "",
        f"- Reason: {info['reason']}",
        f"- Tripped at: {datetime.datetime.fromtimestamp(info['at'], tz=datetime.timezone.utc).isoformat() if info['at'] else 'unknown'}",
        f"- Revived at: {when.isoformat()}",
        "",
        "## Recent auth failures at revive time",
        "",
        *_format_failures(recent),
        "",
    ]
    fname.write_text("\n".join(lines), encoding="utf-8")
    return fname


def run(config: Config | None) -> int:
    if config is None:
        print("nightjar: --revive requires a valid config", file=sys.stderr)
        return 2
    if config.security is None:
        print(
            "nightjar: --revive requires [security].totp_secret. "
            "Run `nightjar --setup-totp` first.",
            file=sys.stderr,
        )
        return 2

    ok, reason = _is_physically_present()
    if not ok:
        print(f"nightjar: --revive refused: {reason}.", file=sys.stderr)
        print(
            "this command must be run at the physical machine.",
            file=sys.stderr,
        )
        return 4

    state = State(db_path=config.daemon.state_dir / "state.db")
    info = state.panic_info()
    if info is None:
        print("nightjar: not in panic state. nothing to do.")
        return 0

    when_local = ""
    if info["at"]:
        when_local = datetime.datetime.fromtimestamp(
            info["at"], tz=datetime.timezone.utc
        ).astimezone().isoformat(timespec="seconds")

    print()
    print(f"Nightjar safety protocol triggered: {when_local}")
    print(f"Reason: {info['reason']}")
    print()
    print("Recent auth failures:")
    for line in _format_failures(state.recent_auth_failures(limit=5)):
        print(line)
    print()

    # Pick the verification mode the daemon is configured for. The
    # secret is shared between TOTP and HOTP — only the verification
    # primitive changes — so HOTP-configured operators don't need a
    # separate authenticator entry to revive. (Pre-fix, revive always
    # called verify_totp regardless of auth_mode, which silently locked
    # out HOTP setups: their authenticator emitted counter-based codes
    # but revive required time-based ones.)
    auth_mode = config.security.auth_mode
    code_label = auth_mode.upper()

    # Three attempts, then give up. getpass hides the typed code, so the
    # six digits are not echoed and don't end up in scrollback.
    matched_hotp_counter: int | None = None
    for attempt in range(1, 4):
        try:
            code = getpass.getpass(
                f"Type {code_label} code to revive ({attempt}/3): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print("nightjar: aborted, panic state unchanged.", file=sys.stderr)
            return 1
        if auth_mode == "totp":
            if auth.verify_totp(secret=config.security.totp_secret, code=code):
                break
        else:  # hotp
            matched = auth.verify_hotp(
                secret=config.security.totp_secret,
                code=code,
                last_counter=state.get_hotp_counter(),
            )
            if matched is not None:
                matched_hotp_counter = matched
                break
        print("  invalid code.")
    else:
        print("nightjar: too many failed attempts, panic state unchanged.", file=sys.stderr)
        return 6

    # HOTP advances the counter on a successful verification so the
    # accepted code can't be replayed. TOTP has no equivalent (time
    # implicitly advances), so this branch only runs in HOTP mode.
    if matched_hotp_counter is not None:
        state.set_hotp_counter(matched_hotp_counter)

    state.clear_panic()

    # Best-effort: also remove PANIC.txt if it exists.
    panic_file = config.daemon.state_dir / "PANIC.txt"
    if panic_file.exists():
        try:
            panic_file.unlink()
        except OSError:
            pass

    incident = _write_incident(
        config.daemon.state_dir, info, state.recent_auth_failures(limit=10)
    )
    print()
    print("Code accepted. Panic state cleared.")
    print(f"Incident report: {incident}")
    print("Restart the daemon: systemctl --user restart nightjar.service")
    return 0
