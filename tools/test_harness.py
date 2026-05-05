"""Live-testing harness for Nightjar.

Lets the agent (or operator) drive the daemon end-to-end without
needing the principal's primary mail account:

  - send <subject> [body]        Send a fresh email FROM the test
                                  account TO the daemon's inbox, with
                                  a freshly-generated HOTP code prefix
                                  on the subject.
  - reply <token> <verdict>       Send an approval reply (Subject:
                                  Re: [Nightjar #<token>] <code>;
                                  Body: <verdict>).
  - code                          Print the next valid HOTP code and
                                  exit. Useful for manual testing.

Threat model and trust boundary:

  - The harness reads the HOTP secret out of the daemon's secrets.toml
    using the same secret_box decoder the daemon uses. Anyone with read
    access to that file can issue commands as the principal; this tool
    formalises that fact rather than expanding the trust surface.
  - The send-from credentials live in a SEPARATE file (default:
    ~/.config/nightjar/test_creds.toml) which holds only the test
    account's SMTP user + password. Compromise of that file does NOT
    grant access to the principal's primary mail account; it only
    grants the ability to send mail from the test account.
  - Every action prints to stdout in JSON-line form so the audit trail
    is visible in the conversation transcript.

Test account setup expected:
  - eagre.claude@gmail.com is a Gmail account whose app password is
    stored in test_creds.toml [eagre_claude].
  - The address is listed alongside the principal's primary address in
    contacts/principal.toml so the daemon recognises mail from it as
    principal-authenticated.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import tomllib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

# Make daemon/* importable when run as `python tools/test_harness.py`
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from daemon import auth, secret_box  # noqa: E402
from daemon.state import State  # noqa: E402


# ---- Defaults -------------------------------------------------------------

DEFAULT_NIGHTJAR_INBOX = "eagre.nightjar@gmail.com"
DEFAULT_TEST_CREDS = Path.home() / ".config" / "nightjar" / "test_creds.toml"
DEFAULT_SECRETS = Path.home() / ".config" / "nightjar" / "secrets.toml"
DEFAULT_STATE_DB = Path.home() / ".local" / "share" / "nightjar" / "state.db"
DEFAULT_TEST_CREDS_SECTION = "eagre_claude"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


# ---- Helpers --------------------------------------------------------------


def _emit(event: str, **fields) -> None:
    """Print one JSON-line audit event so the harness's actions are
    legible in the conversation transcript."""
    print(json.dumps({"event": event, **fields}, sort_keys=True))


def _load_test_creds(path: Path, section: str) -> tuple[str, str]:
    if not path.exists():
        raise SystemExit(
            f"test creds file missing: {path}\n"
            f"create with chmod 600 and the shape:\n"
            f"  [{section}]\n"
            f'  smtp_user = "..."\n'
            f'  smtp_password = "..."\n'
        )
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(
            f"refusing to read {path}: permissions are {oct(mode)}; "
            f"chmod 600 first so only the owner can read."
        )
    with open(path, "rb") as f:
        data = tomllib.load(f)
    sect = data.get(section)
    if not isinstance(sect, dict):
        raise SystemExit(
            f"section [{section}] not found in {path}"
        )
    user = sect.get("smtp_user")
    password = sect.get("smtp_password")
    if not user or not password:
        raise SystemExit(
            f"[{section}] in {path} must define smtp_user and smtp_password"
        )
    return str(user), str(password)


def _load_hotp_secret(secrets_path: Path) -> str:
    """Read the daemon's HOTP secret. Same decode path as daemon/config.py.

    The secret in secrets.toml is obfuscated (machine-id-bound). We
    use the same secret_box.deobfuscate to recover the plaintext,
    then return it for HOTP generation.
    """
    if not secrets_path.exists():
        raise SystemExit(f"secrets.toml not found: {secrets_path}")
    with open(secrets_path, "rb") as f:
        data = tomllib.load(f)
    sec = data.get("security", {}).get("totp_secret")
    if not isinstance(sec, str) or not sec:
        raise SystemExit("secrets.toml has no [security].totp_secret")
    try:
        return secret_box.deobfuscate(sec, label="security.totp_secret")
    except Exception as e:
        raise SystemExit(f"could not decode HOTP secret: {e}")


def _next_hotp_code(state_db: Path, hotp_secret: str) -> tuple[str, int]:
    """Read the daemon's current HOTP counter, return the code at
    counter+1 plus the counter value used. The harness never advances
    the counter — only the daemon does, on successful auth."""
    state = State(db_path=state_db)
    counter = state.get_hotp_counter()
    next_counter = counter + 1
    code = auth.hotp_at(hotp_secret, next_counter)
    return code, next_counter


# ---- Commands -------------------------------------------------------------


def cmd_code(args: argparse.Namespace) -> int:
    secret = _load_hotp_secret(args.secrets)
    code, counter = _next_hotp_code(args.state_db, secret)
    _emit("hotp_code_generated", counter=counter, code=code)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    smtp_user, smtp_password = _load_test_creds(args.test_creds, args.creds_section)
    secret = _load_hotp_secret(args.secrets)
    code, counter = _next_hotp_code(args.state_db, secret)

    subject_with_code = f"{code} {args.subject}"
    body = args.body if args.body is not None else "(test harness send)\n"

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = args.to
    msg["Subject"] = subject_with_code
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="eagre.test")
    msg.set_content(body)

    _emit(
        "smtp_send_begin",
        from_addr=smtp_user, to=args.to,
        subject=subject_with_code,
        hotp_counter=counter,
    )
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)
    except Exception as e:
        _emit("smtp_send_failed", error=type(e).__name__, detail=str(e))
        return 1
    _emit("smtp_send_ok", message_id=msg["Message-ID"])
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    smtp_user, smtp_password = _load_test_creds(args.test_creds, args.creds_section)
    secret = _load_hotp_secret(args.secrets)
    code, counter = _next_hotp_code(args.state_db, secret)

    subject = f"Re: [Nightjar #{args.token}] {code}"
    body = args.verdict + "\n"

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = args.to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="eagre.test")
    msg.set_content(body)

    _emit(
        "smtp_reply_begin",
        from_addr=smtp_user, to=args.to,
        token=args.token, verdict=args.verdict,
        subject=subject, hotp_counter=counter,
    )
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)
    except Exception as e:
        _emit("smtp_reply_failed", error=type(e).__name__, detail=str(e))
        return 1
    _emit("smtp_reply_ok", message_id=msg["Message-ID"])
    return 0


# ---- Argparse -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="test_harness",
        description="Live-testing harness for Nightjar.",
    )
    p.add_argument(
        "--secrets",
        type=Path, default=DEFAULT_SECRETS,
        help=f"Path to nightjar's secrets.toml (default: {DEFAULT_SECRETS})",
    )
    p.add_argument(
        "--test-creds",
        type=Path, default=DEFAULT_TEST_CREDS,
        help=f"Path to test creds toml (default: {DEFAULT_TEST_CREDS})",
    )
    p.add_argument(
        "--creds-section",
        default=DEFAULT_TEST_CREDS_SECTION,
        help=f"Section name in test creds toml (default: {DEFAULT_TEST_CREDS_SECTION})",
    )
    p.add_argument(
        "--state-db",
        type=Path, default=DEFAULT_STATE_DB,
        help=f"Path to state.db (default: {DEFAULT_STATE_DB})",
    )
    p.add_argument(
        "--to",
        default=DEFAULT_NIGHTJAR_INBOX,
        help=f"Daemon inbox to send to (default: {DEFAULT_NIGHTJAR_INBOX})",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    p_code = sub.add_parser("code", help="Print next valid HOTP code and exit.")
    p_code.set_defaults(func=cmd_code)

    p_send = sub.add_parser("send", help="Send a test email to the daemon.")
    p_send.add_argument("subject", help="Subject body (without the code prefix).")
    p_send.add_argument("body", nargs="?", default=None, help="Optional body text.")
    p_send.set_defaults(func=cmd_send)

    p_reply = sub.add_parser("reply", help="Send an approval reply to a token.")
    p_reply.add_argument("token", help="Approval token (hex).")
    p_reply.add_argument(
        "verdict",
        help="Verdict word: yes/no/approve/deny/etc. Goes verbatim into the body.",
    )
    p_reply.set_defaults(func=cmd_reply)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
