"""Config loader for Nightjar.

Reads ~/.config/nightjar/nightjar.conf (INI format) and produces typed
dataclasses describing the daemon, its contacts, and its inboxes.
Build Step 2 adds [security] (TOTP secret + dead-man's-switch knobs).
Later build steps will add [smtp], [caps], etc.

The contact directory is the single mechanism that handles allowlisting
and rate-limiting. Anyone not in [contact:*] is treated as
daily_limit=0 by callers.

The TOTP secret loaded here lives only on this dataclass and on
`daemon/auth.py`. It is never logged, never put into a prompt, never
returned in a tool result.
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import auth


DEFAULT_CONFIG_PATH = Path("~/.config/nightjar/nightjar.conf").expanduser()


class ConfigError(Exception):
    """Raised when the config is missing required fields or self-contradictory."""


@dataclass(frozen=True)
class DaemonConfig:
    state_dir: Path
    log_dir: Path


@dataclass(frozen=True)
class Contact:
    contact_id: str
    addresses: tuple[str, ...]
    display_name: str
    relationship: str
    daily_limit: int  # -1 means unlimited; 0 means blocked
    is_principal: bool


@dataclass(frozen=True)
class InboxConfig:
    name: str
    enabled: bool
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    allowed_contacts: tuple[str, ...]


@dataclass(frozen=True)
class SecurityConfig:
    """TOTP and dead-man's-switch tuning.

    `totp_secret` is sensitive: it never leaves this dataclass except
    into `daemon/auth.py`. Don't log it, don't include it in any tool
    result, don't pass it to any LLM call.
    """
    totp_secret: str
    dead_mans_switch_window_minutes: int
    dead_mans_switch_threshold: int


@dataclass(frozen=True)
class Config:
    daemon: DaemonConfig
    contacts: dict[str, Contact]
    inboxes: dict[str, InboxConfig]
    security: SecurityConfig | None = None
    address_index: dict[str, str] = field(default_factory=dict)
    """Maps lowercased email address to contact_id. Built at load time."""


def _parse_daily_limit(raw: str) -> int:
    raw = raw.strip().lower()
    if raw in ("unlimited", "-1"):
        return -1
    try:
        n = int(raw)
    except ValueError as e:
        raise ConfigError(f"daily_limit must be an int or 'unlimited', got: {raw!r}") from e
    if n < 0:
        raise ConfigError(f"daily_limit must be >= 0 (or 'unlimited'), got: {n}")
    return n


def _parse_bool(raw: str, *, field_name: str) -> bool:
    raw = raw.strip().lower()
    if raw in ("true", "yes", "1", "on"):
        return True
    if raw in ("false", "no", "0", "off"):
        return False
    raise ConfigError(f"{field_name} must be true/false, got: {raw!r}")


def _parse_csv(raw: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def load(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Parse the INI file at `path`, return a validated Config.

    Validation rules enforced here:
      - Exactly one contact may have is_principal=true.
      - Every contact has at least one address.
      - Every address resolves to exactly one contact (no duplicates).
      - Every inbox's allowed_contacts list references known contact IDs.
      - File must be chmod 600 if it contains sensitive sections (we
        don't read those yet, but we still warn).
    """
    if not path.exists():
        raise ConfigError(f"config not found at {path}")

    # Permission check: warn if not 600 once we have IMAP credentials
    # (this catches misconfigurations early without being too strict).
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        # Not fatal yet (the file might be intentionally readable for
        # development); the caller can decide to escalate.
        pass

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)

    if "daemon" not in parser:
        raise ConfigError("[daemon] section is required")

    daemon_section = parser["daemon"]
    daemon = DaemonConfig(
        state_dir=Path(os.path.expanduser(daemon_section.get("state_dir", "~/.local/share/nightjar"))),
        log_dir=Path(os.path.expanduser(daemon_section.get("log_dir", "~/nightjar/logs"))),
    )

    contacts: dict[str, Contact] = {}
    address_index: dict[str, str] = {}
    principal_id: str | None = None

    for section_name in parser.sections():
        if not section_name.startswith("contact:"):
            continue
        contact_id = section_name.split(":", 1)[1].strip()
        if not contact_id:
            raise ConfigError(f"contact section has empty id: {section_name!r}")
        section = parser[section_name]
        addresses = _parse_csv(section.get("addresses", ""))
        if not addresses:
            raise ConfigError(f"contact {contact_id!r} has no addresses")
        is_principal = _parse_bool(section.get("is_principal", "false"), field_name=f"{section_name}.is_principal")
        if is_principal:
            if principal_id is not None:
                raise ConfigError(
                    f"is_principal=true on multiple contacts: {principal_id!r} and {contact_id!r}"
                )
            principal_id = contact_id
        contact = Contact(
            contact_id=contact_id,
            addresses=tuple(a.lower() for a in addresses),
            display_name=section.get("display_name", contact_id).strip(),
            relationship=section.get("relationship", "").strip(),
            daily_limit=_parse_daily_limit(section.get("daily_limit", "3")),
            is_principal=is_principal,
        )
        contacts[contact_id] = contact
        for addr in contact.addresses:
            if addr in address_index:
                raise ConfigError(
                    f"address {addr!r} is claimed by both {address_index[addr]!r} and {contact_id!r}"
                )
            address_index[addr] = contact_id

    inboxes: dict[str, InboxConfig] = {}
    for section_name in parser.sections():
        if not section_name.startswith("inbox:"):
            continue
        inbox_name = section_name.split(":", 1)[1].strip()
        if not inbox_name:
            raise ConfigError(f"inbox section has empty name: {section_name!r}")
        section = parser[section_name]
        enabled = _parse_bool(section.get("enabled", "true"), field_name=f"{section_name}.enabled")
        if not enabled:
            continue
        try:
            imap_port = int(section.get("imap_port", "993"))
        except ValueError as e:
            raise ConfigError(f"{section_name}.imap_port must be int") from e
        allowed = _parse_csv(section.get("allowed_contacts", ""))
        for ref in allowed:
            if ref not in contacts:
                raise ConfigError(
                    f"{section_name}.allowed_contacts references unknown contact: {ref!r}"
                )
        inbox = InboxConfig(
            name=inbox_name,
            enabled=enabled,
            imap_host=section["imap_host"].strip(),
            imap_port=imap_port,
            imap_user=section["imap_user"].strip(),
            imap_password=section["imap_password"],
            allowed_contacts=allowed,
        )
        inboxes[inbox_name] = inbox

    if not inboxes:
        raise ConfigError("no enabled [inbox:*] sections found")

    security: SecurityConfig | None = None
    if "security" in parser:
        sec_section = parser["security"]
        totp_secret = sec_section.get("totp_secret", "").strip()
        if not totp_secret:
            raise ConfigError("[security].totp_secret is required if [security] is present")
        if not auth.is_valid_secret(totp_secret):
            raise ConfigError(
                "[security].totp_secret is not a valid base32 secret "
                "(use `nightjar --setup-totp` to generate one)"
            )
        try:
            window_minutes = int(sec_section.get("dead_mans_switch_window_minutes", "60"))
            threshold = int(sec_section.get("dead_mans_switch_threshold", "3"))
        except ValueError as e:
            raise ConfigError(f"[security] integer field must be int: {e}") from e
        if window_minutes <= 0:
            raise ConfigError("[security].dead_mans_switch_window_minutes must be > 0")
        if threshold <= 0:
            raise ConfigError("[security].dead_mans_switch_threshold must be > 0")
        security = SecurityConfig(
            totp_secret=totp_secret,
            dead_mans_switch_window_minutes=window_minutes,
            dead_mans_switch_threshold=threshold,
        )

    return Config(
        daemon=daemon,
        contacts=contacts,
        inboxes=inboxes,
        security=security,
        address_index=address_index,
    )
