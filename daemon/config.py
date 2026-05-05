"""Config loader for Nightjar.

Reads ~/.config/nightjar/nightjar.conf (INI format) and produces typed
dataclasses describing the daemon, its contacts, its inboxes, its
security knobs, and its outbound SMTP credentials. Later build steps
will add [caps], prompt configuration, etc.

The contact directory is the single mechanism that handles allowlisting
and rate-limiting. Anyone not in [contact:*] is treated as
daily_limit=0 by callers.

Sensitive fields (TOTP secret, SMTP password) live only on the
dataclasses and the modules that need them. They are never logged,
never put into a prompt, never returned in a tool result.
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import auth


DEFAULT_CONFIG_PATH = Path("~/.config/nightjar/nightjar.conf").expanduser()
DEFAULT_SECRETS_PATH = Path("~/.config/nightjar/secrets.toml").expanduser()


class ConfigError(Exception):
    """Raised when the config is missing required fields or self-contradictory."""


@dataclass(frozen=True)
class DaemonConfig:
    state_dir: Path
    log_dir: Path
    notes_dir: Path = field(default_factory=lambda: Path("~/nightjar/contacts").expanduser())
    contacts_dir: Path = field(default_factory=lambda: Path("~/.config/nightjar/contacts").expanduser())


@dataclass(frozen=True)
class Contact:
    contact_id: str
    addresses: tuple[str, ...]
    display_name: str
    relationship: str
    daily_limit: int  # -1 means unlimited; 0 means blocked
    is_principal: bool
    # Inboxes this contact is allowed on. Per-file TOML stores this on
    # the contact; the loader inverts it into inbox.allowed_contacts.
    # Empty tuple is a misconfigured contact (parser rejects it).
    inboxes: tuple[str, ...] = ()
    # Step 7 forward-compat: when true, the daemon may append rapport
    # notes proposed by triage without a separate per-note approval.
    # Default false — every note proposal goes to the principal first.
    auto_approve_notes: bool = False


@dataclass(frozen=True)
class InboxConfig:
    name: str
    enabled: bool
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    allowed_contacts: tuple[str, ...]
    # `trusted_authserv` is the authserv-id stamped by the inbox's MTA
    # in its Authentication-Results header. Gmail uses "mx.google.com".
    # The watcher gates inbound mail on this header's dmarc verdict;
    # without a known trusted authserv, the daemon cannot tell real
    # Gmail-stamped headers from attacker-injected ones, so this is
    # required for any inbox that processes contact mail.
    trusted_authserv: str = ""
    # `catchup_window_days` is the lookback window the IMAP watcher
    # uses for its SINCE search on each catchup. Step 6e moved dedup
    # off the IMAP \Seen flag onto state-db Message-ID lookup, so the
    # window doesn't have to be tight — it just bounds the work per
    # catchup. 7 days is plenty of headroom for a daemon that's down
    # for a long weekend; bump it if the daemon may be offline longer.
    catchup_window_days: int = 7
    # `status_walk_count` is the number of recent IMAP messages the
    # status report fetches headers for to cross-reference against the
    # state-db. 200 is a good balance between coverage and cost (~2-4s,
    # ~500KB network on a typical Gmail connection). Bump up if you
    # have a busy inbox where 200 covers <a few weeks. The full-inbox
    # variant is what the 'audit' power-tool will provide once it
    # lands; this knob is the per-status-report cap.
    status_walk_count: int = 200


@dataclass(frozen=True)
class SmtpConfig:
    """Outbound SMTP credentials for daemon/notifier.py.

    The password is sensitive: same handling rules as TOTP secrets —
    never logged, never put into a prompt, never returned in a tool
    result. STARTTLS is implicit at port 587; we don't expose a knob
    for it (Gmail and most providers require it).
    """
    host: str
    port: int
    user: str
    password: str
    from_name: str
    from_addr: str


AUTH_MODES = ("hotp", "totp")
DEFAULT_AUTH_MODE = "hotp"

DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5"
DEFAULT_PER_HOUR_MAX_INVOCATIONS = 30
DEFAULT_PER_INVOCATION_MAX_INPUT_TOKENS = 8000
DEFAULT_PRINCIPAL_PER_MESSAGE_COST_CENTS = 10  # $0.10 soft cap
DEFAULT_PRINCIPAL_HARD_KILL_MULTIPLIER = 5  # 5x soft cap = hard refusal
DEFAULT_PRINCIPAL_ALWAYS_DIRECT = False


@dataclass(frozen=True)
class ClaudeConfig:
    """Anthropic API credentials and rate-limit knobs for triage.

    `api_key` is sensitive: same handling rules as the TOTP secret and
    the SMTP password. Never logged, never put into a prompt, never
    returned in a tool result. Lives only on this dataclass and on the
    triage module that calls anthropic.AsyncAnthropic.

    `default_model` is the model Nightjar uses for triage and
    principal-command interpretation. Haiku 4.5 is the floor: the
    threat model assumes a frontier-tier-or-better LLM, and Haiku is
    cheap enough that the daemon can stay inside the $20/month console
    cap with comfortable headroom (~thousands of triage calls).

    `per_hour_max_invocations` is the in-daemon rate limit. The first
    line of defence is the spend cap on the Anthropic console; this is
    the second line, so a runaway state inside the daemon (loop, bad
    state machine transition) cannot burn an entire month's budget in
    a single hour. 30/hr matches DESIGN.md's cost model worst case of
    ~$0.60/hr.

    `per_invocation_max_input_tokens` is a defensive cap on prompt
    size. A pathological email body should not be allowed to spend a
    whole hour's budget in one call. 8000 is enough for a normal
    email plus the system prompt and recent thread context.

    `principal_per_message_cost_cents` is the soft cost ceiling for ONE
    principal-interpret call. When a call exceeds this (computed from
    actual token usage and the model's published rates), the daemon
    surfaces the result to the principal but flags the overage in the
    reply or approval ping. Default 10 cents; configurable to suit
    operators with higher or lower tolerance. The cap exists because
    the gate-drop change (#107) removed the up-front 'yes interpret'
    confirmation, so this is the post-hoc backstop that catches
    runaway interpretations.

    `principal_hard_kill_multiplier` defines the 'absolutely no, drop
    the result' threshold. If a call costs more than `principal_per_message_cost_cents
    * principal_hard_kill_multiplier`, the daemon refuses to surface
    the LLM output at all and emails the principal a brief 'killed for
    cost' notice. Defends against pathological loops where the LLM
    somehow generates very large output. Default 5x the soft cap.

    `principal_always_direct` is an experimental UX knob. When true,
    even side-effect queries get an inline prose answer that suggests
    the deterministic verb to issue manually, instead of a structured
    plan that lands in the approval queue. Off by default.
    """
    api_key: str
    default_model: str = DEFAULT_CLAUDE_MODEL
    per_hour_max_invocations: int = DEFAULT_PER_HOUR_MAX_INVOCATIONS
    per_invocation_max_input_tokens: int = DEFAULT_PER_INVOCATION_MAX_INPUT_TOKENS
    principal_per_message_cost_cents: int = DEFAULT_PRINCIPAL_PER_MESSAGE_COST_CENTS
    principal_hard_kill_multiplier: int = DEFAULT_PRINCIPAL_HARD_KILL_MULTIPLIER
    principal_always_direct: bool = DEFAULT_PRINCIPAL_ALWAYS_DIRECT


@dataclass(frozen=True)
class SecurityConfig:
    """Authentication and dead-man's-switch tuning.

    `totp_secret` is sensitive: it never leaves this dataclass except
    into `daemon/auth.py`. Don't log it, don't include it in any tool
    result, don't pass it to any LLM call.

    `auth_mode` selects between HOTP (counter-based, the default — no
    time pressure on the operator, well-suited to email's async nature)
    and TOTP (time-based, useful when you want codes to auto-expire).
    The shared base32 secret is reused either way; only the verification
    primitive changes.
    """
    totp_secret: str
    dead_mans_switch_window_minutes: int
    dead_mans_switch_threshold: int
    auth_mode: str = DEFAULT_AUTH_MODE


@dataclass(frozen=True)
class Config:
    daemon: DaemonConfig
    contacts: dict[str, Contact]
    inboxes: dict[str, InboxConfig]
    security: SecurityConfig | None = None
    smtp: SmtpConfig | None = None
    claude: ClaudeConfig | None = None
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


def load(
    path: Path | None = None,
    *,
    secrets_path: Path | None = None,
) -> Config:
    """Parse the INI file at `path`, splice in secrets from
    `secrets_path` if it exists, return a validated Config.

    Validation rules enforced here:
      - Exactly one contact may have is_principal=true.
      - Every contact has at least one address.
      - Every address resolves to exactly one contact (no duplicates).
      - Every inbox's allowed_contacts list references known contact IDs.
      - secrets.toml (if present) must be chmod 600.
      - A secret found in BOTH the INI and secrets.toml is a misconfig:
        the migrator should have stripped the INI copy. We refuse to
        start in that case.

    Defaults are looked up at call time (not via mutable-default
    arguments) so tests can monkeypatch `DEFAULT_SECRETS_PATH` to
    isolate from the operator's live ~/.config/nightjar/.
    """
    if path is None:
        path = DEFAULT_CONFIG_PATH
    if secrets_path is None:
        secrets_path = DEFAULT_SECRETS_PATH
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

    # Load secrets.toml if present. Plaintext from this file overrides
    # the INI for the four secret fields (smtp.password,
    # security.totp_secret, claude.api_key, imap.<inbox>.password).
    # If a secret appears in BOTH, that's a misconfigured state — the
    # migrator should have stripped the INI copy. Refuse to start.
    secrets: dict[str, dict[str, str]] = {}
    if secrets_path.exists():
        from . import secret_box
        try:
            secrets = secret_box.read_secrets_file(secrets_path)
        except secret_box.SecretBoxError as e:
            raise ConfigError(
                f"could not load {secrets_path}: {e}. Either the file is "
                "corrupted, /etc/machine-id has changed since secrets "
                f"were obfuscated, or the file is not chmod 600. The "
                f"pre-migration backup at {path}.pre-migration.bak "
                "(if you still have it) contains plaintext secrets."
            ) from e

    def _secret(section: str, key: str) -> str | None:
        return secrets.get(section, {}).get(key)

    daemon_section = parser["daemon"]
    daemon = DaemonConfig(
        state_dir=Path(os.path.expanduser(daemon_section.get("state_dir", "~/.local/share/nightjar"))),
        log_dir=Path(os.path.expanduser(daemon_section.get("log_dir", "~/nightjar/logs"))),
        notes_dir=Path(os.path.expanduser(daemon_section.get("notes_dir", "~/nightjar/contacts"))),
        contacts_dir=Path(os.path.expanduser(daemon_section.get("contacts_dir", "~/.config/nightjar/contacts"))),
    )

    # Contacts now live in their own directory, one TOML file each.
    # The migrator (daemon/contacts_migrator.py) moves any legacy
    # [contact:*] blocks out of nightjar.conf before this load runs;
    # we defensive-check here to surface a misconfigured state.
    legacy_contact_sections = [
        s for s in parser.sections() if s.startswith("contact:")
    ]
    if legacy_contact_sections:
        raise ConfigError(
            f"{path}: legacy [contact:*] sections found "
            f"({', '.join(legacy_contact_sections)}); the contacts "
            "migrator should have moved these to per-file TOML in "
            f"{daemon.contacts_dir}. If the migration backup file "
            f"{path}.pre-migration.bak exists, the migration ran "
            "but did not strip the originals — investigate manually."
        )

    from . import contacts_loader
    contact_load = contacts_loader.load_all(daemon.contacts_dir)
    contacts: dict[str, Contact] = dict(contact_load.contacts)
    address_index: dict[str, str] = dict(contact_load.address_index)

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
        # Allowed-contacts is now derived from per-contact `inboxes` lists.
        # If the operator left an `allowed_contacts =` line in the INI,
        # that is a misconfigured state from before migration; surface it.
        if "allowed_contacts" in section:
            raise ConfigError(
                f"{section_name}.allowed_contacts is no longer accepted; "
                "the per-inbox allowlist is now derived from each "
                f"contact's `inboxes = [...]` list in {daemon.contacts_dir}. "
                "Remove the line from nightjar.conf and add the inbox "
                "name to the appropriate contact files."
            )
        # Invert per-contact inboxes lists into this inbox's allowed_contacts.
        allowed = tuple(
            cid for cid, c in contacts.items() if inbox_name in c.inboxes
        )
        trusted_authserv = section.get("trusted_authserv", "").strip()
        if not trusted_authserv:
            raise ConfigError(
                f"{section_name}.trusted_authserv is required. Set it to the "
                "authserv-id your provider stamps in Authentication-Results: "
                "headers (Gmail: 'mx.google.com'). The daemon refuses to "
                "process contact mail without a known authserv because it "
                "cannot otherwise tell real DMARC verdicts from attacker-"
                "injected ones."
            )
        # IMAP password: prefer secrets.toml; refuse if both present.
        imap_password_secret = _secret(f"imap.{inbox_name}", "password")
        imap_password_ini = section.get("imap_password", "")
        if imap_password_secret is not None and imap_password_ini:
            raise ConfigError(
                f"{section_name}.imap_password is in both nightjar.conf "
                f"and secrets.toml; remove it from {path}."
            )
        imap_password = imap_password_secret or imap_password_ini
        if not imap_password:
            raise ConfigError(
                f"{section_name}.imap_password is required (set it in "
                f"{secrets_path} or nightjar.conf)"
            )
        catchup_window_raw = section.get("catchup_window_days", "7").strip()
        try:
            catchup_window_days = int(catchup_window_raw)
        except ValueError as exc:
            raise ConfigError(
                f"{section_name}.catchup_window_days must be an integer, "
                f"got {catchup_window_raw!r}"
            ) from exc
        if catchup_window_days < 1:
            raise ConfigError(
                f"{section_name}.catchup_window_days must be >= 1, "
                f"got {catchup_window_days}"
            )
        status_walk_raw = section.get("status_walk_count", "200").strip()
        try:
            status_walk_count = int(status_walk_raw)
        except ValueError as exc:
            raise ConfigError(
                f"{section_name}.status_walk_count must be an integer, "
                f"got {status_walk_raw!r}"
            ) from exc
        if status_walk_count < 10:
            raise ConfigError(
                f"{section_name}.status_walk_count must be >= 10, "
                f"got {status_walk_count}"
            )
        inbox = InboxConfig(
            name=inbox_name,
            enabled=enabled,
            imap_host=section["imap_host"].strip(),
            imap_port=imap_port,
            imap_user=section["imap_user"].strip(),
            imap_password=imap_password,
            allowed_contacts=allowed,
            trusted_authserv=trusted_authserv,
            catchup_window_days=catchup_window_days,
            status_walk_count=status_walk_count,
        )
        inboxes[inbox_name] = inbox

    if not inboxes:
        raise ConfigError("no enabled [inbox:*] sections found")

    # Cross-check: every contact's `inboxes` list must reference an
    # enabled inbox. (We do this AFTER inbox parsing because the
    # loader doesn't know inbox names yet.)
    for contact in contacts.values():
        for inbox_name in contact.inboxes:
            if inbox_name not in inboxes:
                raise ConfigError(
                    f"contact {contact.contact_id!r} lists inbox "
                    f"{inbox_name!r} but no such enabled inbox exists. "
                    f"Either add the inbox to {path} or remove it from "
                    f"{daemon.contacts_dir / (contact.contact_id + '.toml')}."
                )

    security: SecurityConfig | None = None
    if "security" in parser:
        sec_section = parser["security"]
        totp_secret_secret = _secret("security", "totp_secret")
        totp_secret_ini = sec_section.get("totp_secret", "").strip()
        if totp_secret_secret is not None and totp_secret_ini:
            raise ConfigError(
                "[security].totp_secret is in both nightjar.conf and "
                f"secrets.toml; remove it from {path}."
            )
        totp_secret = totp_secret_secret or totp_secret_ini
        if not totp_secret:
            raise ConfigError(
                "[security].totp_secret is required (set it in "
                f"{secrets_path} or nightjar.conf)"
            )
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
        auth_mode = sec_section.get("auth_mode", DEFAULT_AUTH_MODE).strip().lower()
        if auth_mode not in AUTH_MODES:
            raise ConfigError(
                f"[security].auth_mode must be one of {AUTH_MODES}, got: {auth_mode!r}"
            )
        security = SecurityConfig(
            totp_secret=totp_secret,
            dead_mans_switch_window_minutes=window_minutes,
            dead_mans_switch_threshold=threshold,
            auth_mode=auth_mode,
        )

    smtp: SmtpConfig | None = None
    if "smtp" in parser:
        smtp_section = parser["smtp"]
        try:
            smtp_port = int(smtp_section.get("port", "587"))
        except ValueError as e:
            raise ConfigError(f"[smtp].port must be int: {e}") from e
        host = smtp_section.get("host", "").strip()
        user = smtp_section.get("user", "").strip()
        password_secret = _secret("smtp", "password")
        password_ini = smtp_section.get("password", "")
        if password_secret is not None and password_ini:
            raise ConfigError(
                "[smtp].password is in both nightjar.conf and "
                f"secrets.toml; remove it from {path}."
            )
        password = password_secret or password_ini
        from_addr = smtp_section.get("from_addr", user).strip()
        from_name = smtp_section.get("from_name", "Nightjar").strip()
        if not host:
            raise ConfigError("[smtp].host is required")
        if not user:
            raise ConfigError("[smtp].user is required")
        if not password:
            raise ConfigError(
                "[smtp].password is required (set it in "
                f"{secrets_path} or nightjar.conf)"
            )
        if "@" not in from_addr:
            raise ConfigError(f"[smtp].from_addr does not look like an email: {from_addr!r}")
        smtp = SmtpConfig(
            host=host,
            port=smtp_port,
            user=user,
            password=password,
            from_name=from_name,
            from_addr=from_addr,
        )

    claude: ClaudeConfig | None = None
    if "claude" in parser:
        claude_section = parser["claude"]
        api_key_secret = _secret("claude", "api_key")
        api_key_ini = claude_section.get("api_key", "").strip()
        if api_key_secret is not None and api_key_ini:
            raise ConfigError(
                "[claude].api_key is in both nightjar.conf and "
                f"secrets.toml; remove it from {path}."
            )
        api_key = api_key_secret or api_key_ini
        if not api_key:
            raise ConfigError(
                "[claude].api_key is required (set it in "
                f"{secrets_path} or nightjar.conf)"
            )
        if not (api_key.startswith("sk-ant-") and len(api_key) > 50):
            raise ConfigError(
                "[claude].api_key does not look like an Anthropic API key "
                "(expected prefix 'sk-ant-' and length > 50)"
            )
        default_model = claude_section.get("default_model", DEFAULT_CLAUDE_MODEL).strip()
        if not default_model:
            raise ConfigError("[claude].default_model must not be empty")
        try:
            per_hour = int(claude_section.get(
                "per_hour_max_invocations", str(DEFAULT_PER_HOUR_MAX_INVOCATIONS)
            ))
            per_inv_tokens = int(claude_section.get(
                "per_invocation_max_input_tokens", str(DEFAULT_PER_INVOCATION_MAX_INPUT_TOKENS)
            ))
            cost_cents = int(claude_section.get(
                "principal_per_message_cost_cents",
                str(DEFAULT_PRINCIPAL_PER_MESSAGE_COST_CENTS),
            ))
            kill_multiplier = int(claude_section.get(
                "principal_hard_kill_multiplier",
                str(DEFAULT_PRINCIPAL_HARD_KILL_MULTIPLIER),
            ))
        except ValueError as e:
            raise ConfigError(f"[claude] integer field must be int: {e}") from e
        always_direct_raw = claude_section.get(
            "principal_always_direct",
            "true" if DEFAULT_PRINCIPAL_ALWAYS_DIRECT else "false",
        ).strip().lower()
        if always_direct_raw not in ("true", "false", "1", "0", "yes", "no"):
            raise ConfigError(
                "[claude].principal_always_direct must be a boolean "
                f"(true/false), got: {always_direct_raw!r}"
            )
        always_direct = always_direct_raw in ("true", "1", "yes")
        if per_hour <= 0:
            raise ConfigError("[claude].per_hour_max_invocations must be > 0")
        if per_inv_tokens <= 0:
            raise ConfigError("[claude].per_invocation_max_input_tokens must be > 0")
        if cost_cents <= 0:
            raise ConfigError(
                "[claude].principal_per_message_cost_cents must be > 0"
            )
        if kill_multiplier < 1:
            raise ConfigError(
                "[claude].principal_hard_kill_multiplier must be >= 1"
            )
        claude = ClaudeConfig(
            api_key=api_key,
            default_model=default_model,
            per_hour_max_invocations=per_hour,
            per_invocation_max_input_tokens=per_inv_tokens,
            principal_per_message_cost_cents=cost_cents,
            principal_hard_kill_multiplier=kill_multiplier,
            principal_always_direct=always_direct,
        )

    return Config(
        daemon=daemon,
        contacts=contacts,
        inboxes=inboxes,
        security=security,
        smtp=smtp,
        claude=claude,
        address_index=address_index,
    )
