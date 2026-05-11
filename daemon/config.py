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
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import auth
from . import system_load


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

    @property
    def agent_cwd(self) -> Path:
        """Working directory for the principal-agent subprocess. Lives
        inside state_dir/agent-workspace so it's clearly under
        Nightjar's data area (not the user home), and so any CLAUDE.md
        the agent reads is one Nightjar seeded — not whatever happens
        to live in the principal's home tree.

        The directory is also the agent's scratch space for
        notes-to-future-self (per-contact context, ongoing threads,
        anything the agent wants to persist across turns beyond what
        Claude's --resume gives it). The agent organises this
        directory itself; Nightjar makes no a priori structural claim.
        """
        return self.state_dir / "agent-workspace"


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
    # Step 7b: topical scopes the contact is allowed to discuss. Empty
    # tuple = unrestricted (the historical default; preserves existing
    # behaviour). Non-empty = triage classifies each inbound message
    # into one of these scopes (or out_of_scope) and the out-of-scope
    # default is a polite decline. Every named scope must exist in
    # `Config.scopes` (the [scopes] registry); cross-validation
    # happens in load() because the loader can't see the registry.
    scopes: tuple[str, ...] = ()
    # Scope/sensitivity Part 1: two-axis scope vocabulary. `facets` are
    # universal axes (calendar, communication-style, finance, etc.)
    # that apply across most contacts; `projects` are specific shared
    # contexts (aurora, nightjar-dev). Project names may carry
    # dot-separated sub-scopes (aurora.music) with parent/child
    # visibility rules in notes_store.
    #
    # Mutual exclusion with `scopes`: a contact uses EITHER the legacy
    # `scopes` field OR the (facets, projects) pair. Mixing both is a
    # config error — the migration is deliberate, not silent.
    facets: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()


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
    # `poll_interval_seconds` belt-and-braces protection against IMAP
    # IDLE missed pushes. The watcher relies on Gmail's IDLE channel
    # for sub-second latency on new mail, but Gmail occasionally
    # silently fails to push a notification — observed in production.
    # Without a backstop, a missed push waits for the next IDLE
    # refresh (~27 min). With this set, the watcher tears IDLE down
    # every N seconds, runs a fresh catchup search, and resumes IDLE.
    # Worst-case latency drops from ~27 min to N seconds. Cost is one
    # extra IMAP round-trip per N seconds when no mail is arriving;
    # negligible at 60s. Set to 0 to disable (re-create the original
    # IDLE-only behaviour, e.g. for an inbox that's never user-visible).
    poll_interval_seconds: int = 60


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
DEFAULT_SCOPE_CLASSIFIER_MODEL = "claude-haiku-4-5"
DEFAULT_PER_HOUR_MAX_INVOCATIONS = 30
DEFAULT_PER_INVOCATION_MAX_INPUT_TOKENS = 8000
DEFAULT_PRINCIPAL_PER_MESSAGE_COST_CENTS = 10  # $0.10 soft cap
DEFAULT_PRINCIPAL_HARD_KILL_MULTIPLIER = 5  # 5x soft cap = hard refusal
DEFAULT_PRINCIPAL_ALWAYS_DIRECT = False

# Backend selector. The api backend uses anthropic.AsyncAnthropic with
# an api key (per-token billing). The claude_code_pipe backend shells
# out to `claude -p` and runs against the principal's logged-in
# subscription (no api key, subscription-bounded cost). The two
# backends have been verified to produce equivalent output for the
# call sites in this codebase.
BACKEND_ANTHROPIC_API = "anthropic_api"
BACKEND_CLAUDE_CODE_PIPE = "claude_code_pipe"
_KNOWN_BACKENDS = frozenset({BACKEND_ANTHROPIC_API, BACKEND_CLAUDE_CODE_PIPE})
DEFAULT_BACKEND = BACKEND_ANTHROPIC_API

# Named LLM call sites. Per-site backend + model can be overridden via
# `[llm.<site>]` sections in the INI; sites not overridden inherit
# the global `[claude].backend` and the call site's own default model.
#
# Add new site names here as they ship in production code. Do NOT add
# names for hypothetical future sites — the resolver fails loudly on
# unknown sections to catch typos, and an empty placeholder section
# would make a typo silent.
LLM_SITE_TRIAGE = "triage"
LLM_SITE_SCOPE_CLASSIFIER = "scope_classifier"
LLM_SITE_PRINCIPAL_INTERPRET = "principal_interpret"
KNOWN_LLM_SITES = frozenset({
    LLM_SITE_TRIAGE,
    LLM_SITE_SCOPE_CLASSIFIER,
    LLM_SITE_PRINCIPAL_INTERPRET,
})


@dataclass(frozen=True)
class LlmSiteConfig:
    """Per-call-site backend and model override.

    None on either field means "inherit from the global default":
      - backend=None  -> use [claude].backend
      - model=None    -> use the call site's own default model
        (config.default_model for triage/principal_interpret,
         config.scope_classifier_model for scope_classifier).

    Set fields override the inherited value. This shape lets an
    operator override only what they want to change (e.g. flip
    triage to the pipe backend without touching the model, or run
    sleep-cycle on Opus while leaving the backend on api).
    """
    backend: str | None = None
    model: str | None = None


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

    `backend` selects the execution path. `anthropic_api` (default)
    uses the Anthropic SDK and requires `api_key`. `claude_code_pipe`
    shells out to `claude -p` against the principal's logged-in
    subscription; `api_key` may be empty in that mode. Toggle this
    to move off per-token API billing onto subscription-bounded usage.

    `per_site` holds optional per-call-site overrides parsed from
    `[llm.<site>]` sections. Each override may set `backend` and/or
    `model`; whichever fields are unset inherit from the global
    defaults. See `LlmSiteConfig` for the shape and `KNOWN_LLM_SITES`
    for the legal site names.
    """
    api_key: str
    default_model: str = DEFAULT_CLAUDE_MODEL
    per_hour_max_invocations: int = DEFAULT_PER_HOUR_MAX_INVOCATIONS
    per_invocation_max_input_tokens: int = DEFAULT_PER_INVOCATION_MAX_INPUT_TOKENS
    principal_per_message_cost_cents: int = DEFAULT_PRINCIPAL_PER_MESSAGE_COST_CENTS
    principal_hard_kill_multiplier: int = DEFAULT_PRINCIPAL_HARD_KILL_MULTIPLIER
    principal_always_direct: bool = DEFAULT_PRINCIPAL_ALWAYS_DIRECT
    # Step 7b: model used for the pass-1 scope classifier when a
    # contact has non-empty scopes. Defaulted to Haiku regardless of
    # what the main triage model is, because scope classification is
    # a small structured task and Haiku is cheapest+fastest. Bump to
    # a stronger model only if classification accuracy on real
    # contacts proves a problem.
    scope_classifier_model: str = DEFAULT_SCOPE_CLASSIFIER_MODEL
    backend: str = DEFAULT_BACKEND
    per_site: dict[str, LlmSiteConfig] = field(default_factory=dict)

    def model_for_site(self, site: str) -> str:
        """Resolve the model a given call site should use. Per-site
        override (`[llm.<site>].model`) wins; otherwise the call site's
        own default — `scope_classifier_model` for the classifier,
        `default_model` for everything else."""
        site_cfg = self.per_site.get(site)
        if site_cfg is not None and site_cfg.model is not None:
            return site_cfg.model
        if site == LLM_SITE_SCOPE_CLASSIFIER:
            return self.scope_classifier_model
        return self.default_model

    def backend_for_site(self, site: str) -> str:
        """Resolve the backend a given call site should use. Per-site
        override wins; otherwise the global default."""
        site_cfg = self.per_site.get(site)
        if site_cfg is not None and site_cfg.backend is not None:
            return site_cfg.backend
        return self.backend


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

    `secondary_hotp_secret` is the off-machine second factor for the
    `do` verb (the principal-agent path that hands free-form requests
    straight to claude -p). The seed is stored alongside the primary
    here for ergonomic config loading, but the OPERATIONAL discipline
    is that the seed itself lives off-machine — on a hardware token, a
    paper list, or an authenticator app — and only the daemon's copy
    sits in secrets.toml. Empty disables the `do` verb entirely.
    """
    totp_secret: str
    dead_mans_switch_window_minutes: int
    dead_mans_switch_threshold: int
    auth_mode: str = DEFAULT_AUTH_MODE
    secondary_hotp_secret: str = ""


DEFAULT_AGENT_NAME = "Nightjar"
DEFAULT_AGENT_PERSONALITY = (
    "Crisp and direct. Reports facts before opinions, acts before "
    "explaining. Will riff if invited but does not perform "
    "enthusiasm. Uses the principal's first name; refers to itself "
    "by its configured agent name. No emoji."
)


@dataclass(frozen=True)
class AgentConfig:
    """Identity and voice for the principal-agent path.

    `name` is what the agent calls itself in replies and how the
    principal addresses it. Defaults to "Nightjar"; override in
    `[agent].name` if you want a different mascot.

    `personality` is the *voice-and-demeanour* override: tone,
    pacing, level of formality. It is *not* a security control.
    The system prompt fences this section explicitly so a personality
    string cannot widen the agent's actual capabilities, and the
    personality cannot be set from inbound mail — only from the
    operator's INI at install time.
    """
    name: str = DEFAULT_AGENT_NAME
    personality: str = DEFAULT_AGENT_PERSONALITY
    dispatch: system_load.DispatchPolicy = field(
        default_factory=system_load.DispatchPolicy,
    )
    """Operator-tunable thresholds for deferring agent dispatch when
    the system is busy. See [agent.dispatch] section in nightjar.conf;
    default is "never defer" so existing installs are unchanged."""


@dataclass(frozen=True)
class Config:
    daemon: DaemonConfig
    contacts: dict[str, Contact]
    inboxes: dict[str, InboxConfig]
    security: SecurityConfig | None = None
    smtp: SmtpConfig | None = None
    claude: ClaudeConfig | None = None
    agent: AgentConfig = field(default_factory=AgentConfig)
    address_index: dict[str, str] = field(default_factory=dict)
    """Maps lowercased email address to contact_id. Built at load time."""
    scopes: dict[str, str] = field(default_factory=dict)
    """Step 7b: topical scope registry. {scope_name: human_description}.
    Empty = no scopes defined; contacts must have empty `scopes = []`
    (any non-empty scope reference fails cross-validation). The
    descriptions are fed into triage's prompt so the LLM has a
    concrete anchor for what each tag means.
    """
    facets: dict[str, str] = field(default_factory=dict)
    """Scope/sensitivity Part 1: facets registry — universal axes that
    cross most contacts (calendar, communication-style, finance,
    health, etc.). Same shape as `scopes`, distinct namespace. Facets
    are flat names; no dot-notation."""
    projects: dict[str, str] = field(default_factory=dict)
    """Scope/sensitivity Part 1: projects registry — specific shared
    contexts (aurora, nightjar-dev). Project names may include
    dot-separated sub-projects (aurora.music). The registry stores
    each entry as its full dotted name; parent existence is enforced
    at load time."""


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


# Scope names: lowercase ASCII, kebab-case, no leading digits. Tight
# enough that the LLM can echo them back without tokenisation surprises.
_SCOPE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Project names: scope-name segments separated by dots. Each segment
# must satisfy _SCOPE_NAME_RE; "aurora.music" and "aurora.music.demo"
# are both valid. Facets do NOT use this — they're flat by design.
_PROJECT_NAME_RE = re.compile(
    r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)*$"
)


def _validate_scope_name(name: str, *, source: str) -> None:
    """Raise ConfigError if `name` doesn't fit the scope-name shape.
    `source` names the location for the error message (e.g. 'scopes
    registry' or contact filename)."""
    if not _SCOPE_NAME_RE.match(name):
        raise ConfigError(
            f"{source}: scope name {name!r} is invalid; must match "
            f"{_SCOPE_NAME_RE.pattern} (lowercase letters, digits, "
            "underscore, hyphen; first char must be a letter)"
        )


def _validate_facet_name(name: str, *, source: str) -> None:
    """Facets are flat names (no dots). Same shape as legacy scopes."""
    _validate_scope_name(name, source=source)


def _validate_project_name(name: str, *, source: str) -> None:
    """Project names may include dot-separated sub-projects. Each
    segment is a valid scope name; the whole thing matches
    _PROJECT_NAME_RE."""
    if not _PROJECT_NAME_RE.match(name):
        raise ConfigError(
            f"{source}: project name {name!r} is invalid; must match "
            f"{_PROJECT_NAME_RE.pattern} (dot-separated lowercase "
            "segments, e.g. 'aurora' or 'aurora.music')"
        )


def project_parent(name: str) -> str | None:
    """Return the immediate parent of a dotted project name, or None.

    `aurora.music` -> `aurora`; `aurora.music.demo` -> `aurora.music`;
    `aurora` -> None. The caller can walk this iteratively for
    ancestor enumeration.
    """
    if "." not in name:
        return None
    return name.rsplit(".", 1)[0]


def project_ancestors(name: str) -> tuple[str, ...]:
    """Return all ancestors of `name`, root-first.

    `aurora.music.demo` -> ('aurora', 'aurora.music').
    `aurora` -> ().
    """
    parts = name.split(".")
    if len(parts) == 1:
        return ()
    out = []
    for i in range(1, len(parts)):
        out.append(".".join(parts[:i]))
    return tuple(out)


def project_descendant_of(child: str, ancestor: str) -> bool:
    """True if `child` is `ancestor` or any sub-project of it.

    `aurora.music` is descendant_of `aurora` and of `aurora.music`;
    `aurora.legal` is NOT descendant_of `aurora.music`.
    """
    if child == ancestor:
        return True
    return child.startswith(ancestor + ".")


def project_visibility(
    bullet_project: str, contact_projects: tuple[str, ...] | frozenset[str],
) -> bool:
    """Decide whether a note bullet tagged with `bullet_project` is
    visible to a contact whose project list is `contact_projects`.

    Bidirectional visibility:

    - Bullet tagged exactly matches when the contact has that project
      OR any ancestor of it. Example: a bullet tagged `aurora.music`
      is visible to a contact with `aurora.music` or with `aurora`
      (parent subsumes child).
    - Bullet tagged a parent is visible to a contact with any
      descendant. Example: a bullet tagged `aurora` is visible to a
      contact with `aurora.music` (the contact has access to a
      sub-area, so generic-aurora content is appropriate).
    - Sibling sub-scopes are isolated: a bullet tagged `aurora.music`
      is NOT visible to a contact with only `aurora.legal`.

    Both arguments are pre-validated project names (caller is expected
    to have routed them through `_validate_project_name` upstream).
    """
    contact_set = (
        contact_projects if isinstance(contact_projects, (set, frozenset))
        else set(contact_projects)
    )
    for cp in contact_set:
        # Contact's project covers bullet (contact has parent or exact).
        if project_descendant_of(bullet_project, cp):
            return True
        # Contact's project is a descendant of the bullet's project
        # (bullet tags a parent, contact has a child).
        if project_descendant_of(cp, bullet_project):
            return True
    return False


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

    # Step 7b: [scopes] registry. Optional section; absence == no
    # scopes defined, which means every contact must have empty scopes.
    scopes: dict[str, str] = {}
    if "scopes" in parser:
        scopes_section = parser["scopes"]
        for name, description in scopes_section.items():
            _validate_scope_name(name, source="[scopes]")
            description = description.strip()
            if not description:
                raise ConfigError(
                    f"[scopes].{name} has empty description; every scope "
                    "must have a one-line description (it's fed into the "
                    "triage prompt)"
                )
            scopes[name] = description

    # Scope/sensitivity Part 1: [facets] registry. Universal axes,
    # flat names. Same description-required rule as [scopes].
    facets: dict[str, str] = {}
    if "facets" in parser:
        facets_section = parser["facets"]
        for name, description in facets_section.items():
            _validate_facet_name(name, source="[facets]")
            description = description.strip()
            if not description:
                raise ConfigError(
                    f"[facets].{name} has empty description; every facet "
                    "must have a one-line description (it's fed into the "
                    "triage prompt)"
                )
            facets[name] = description

    # Scope/sensitivity Part 1: [projects] registry. Specific contexts,
    # dot-separated names allowed for sub-projects. Each project's
    # parent (if any) must also exist in the registry — we enforce this
    # after parsing all entries because the order in the INI file is
    # not guaranteed parent-first.
    projects: dict[str, str] = {}
    if "projects" in parser:
        projects_section = parser["projects"]
        for name, description in projects_section.items():
            _validate_project_name(name, source="[projects]")
            description = description.strip()
            if not description:
                raise ConfigError(
                    f"[projects].{name} has empty description; every "
                    "project must have a one-line description (it's "
                    "fed into the triage prompt)"
                )
            projects[name] = description
        for name in projects:
            for ancestor in project_ancestors(name):
                if ancestor not in projects:
                    raise ConfigError(
                        f"[projects].{name} declares a sub-project but "
                        f"its parent {ancestor!r} is not defined. Add "
                        f"{ancestor} = <description> to [projects] or "
                        f"rename {name!r} so it has no parent."
                    )

    # Cross-check namespaces don't collide. A name appearing in both
    # [facets] and [projects] (or in either + [scopes]) would create
    # ambiguity at the contact-TOML level (which axis does this name
    # belong to?). Reject at config load.
    if facets.keys() & projects.keys():
        clash = sorted(facets.keys() & projects.keys())
        raise ConfigError(
            f"name(s) {clash!r} appear in both [facets] and [projects]; "
            "each name must belong to exactly one axis. Rename one side."
        )
    if scopes.keys() & facets.keys():
        clash = sorted(scopes.keys() & facets.keys())
        raise ConfigError(
            f"name(s) {clash!r} appear in both [scopes] (legacy) and "
            "[facets]. Remove from one side; the migration is meant to "
            "be deliberate, not silent."
        )
    if scopes.keys() & projects.keys():
        clash = sorted(scopes.keys() & projects.keys())
        raise ConfigError(
            f"name(s) {clash!r} appear in both [scopes] (legacy) and "
            "[projects]. Remove from one side; the migration is meant "
            "to be deliberate, not silent."
        )

    # Cross-check: every scope/facet/project referenced by any contact
    # must exist in its respective registry. We deferred this from the
    # contacts_loader because the loader can't see these sections.
    for contact in contacts.values():
        # Mutual exclusion: contact uses EITHER legacy `scopes` OR the
        # (facets, projects) pair. Mixing both is a config error — the
        # migration is deliberate, not silent.
        has_legacy = bool(contact.scopes)
        has_new = bool(contact.facets) or bool(contact.projects)
        if has_legacy and has_new:
            raise ConfigError(
                f"contact {contact.contact_id!r} mixes legacy "
                f"`scopes` with new `facets`/`projects`. Pick one "
                "axis vocabulary per contact; mixing them at the "
                "same time is ambiguous."
            )

        for scope in contact.scopes:
            if scope not in scopes:
                raise ConfigError(
                    f"contact {contact.contact_id!r} lists scope "
                    f"{scope!r} but it is not defined in the [scopes] "
                    f"registry in {path}. Either add a "
                    f"{scope} = <description> entry under [scopes], "
                    f"or remove it from "
                    f"{daemon.contacts_dir / (contact.contact_id + '.toml')}."
                )
        for facet in contact.facets:
            if facet not in facets:
                raise ConfigError(
                    f"contact {contact.contact_id!r} lists facet "
                    f"{facet!r} but it is not defined in the [facets] "
                    f"registry in {path}. Either add a "
                    f"{facet} = <description> entry under [facets], "
                    f"or remove it from "
                    f"{daemon.contacts_dir / (contact.contact_id + '.toml')}."
                )
        for project in contact.projects:
            if project not in projects:
                raise ConfigError(
                    f"contact {contact.contact_id!r} lists project "
                    f"{project!r} but it is not defined in the "
                    f"[projects] registry in {path}. Either add a "
                    f"{project} = <description> entry under [projects], "
                    f"or remove it from "
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
        # Optional secondary HOTP seed for the `do` verb's two-secret
        # gate. Same secrets-vs-INI dual-source rule as the primary.
        secondary_secret = _secret("security", "secondary_hotp_secret")
        secondary_ini = sec_section.get("secondary_hotp_secret", "").strip()
        if secondary_secret is not None and secondary_ini:
            raise ConfigError(
                "[security].secondary_hotp_secret is in both nightjar.conf "
                f"and secrets.toml; remove it from {path}."
            )
        secondary_hotp_secret = secondary_secret or secondary_ini
        security = SecurityConfig(
            totp_secret=totp_secret,
            dead_mans_switch_window_minutes=window_minutes,
            dead_mans_switch_threshold=threshold,
            auth_mode=auth_mode,
            secondary_hotp_secret=secondary_hotp_secret,
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
        backend = claude_section.get("backend", DEFAULT_BACKEND).strip()
        if backend not in _KNOWN_BACKENDS:
            raise ConfigError(
                f"[claude].backend must be one of "
                f"{sorted(_KNOWN_BACKENDS)!r}; got {backend!r}"
            )
        api_key_secret = _secret("claude", "api_key")
        api_key_ini = claude_section.get("api_key", "").strip()
        if api_key_secret is not None and api_key_ini:
            raise ConfigError(
                "[claude].api_key is in both nightjar.conf and "
                f"secrets.toml; remove it from {path}."
            )
        api_key = api_key_secret or api_key_ini
        if backend == BACKEND_ANTHROPIC_API:
            if not api_key:
                raise ConfigError(
                    "[claude].api_key is required when backend = "
                    f"{BACKEND_ANTHROPIC_API!r} (set it in "
                    f"{secrets_path} or nightjar.conf)"
                )
            if not (api_key.startswith("sk-ant-") and len(api_key) > 50):
                raise ConfigError(
                    "[claude].api_key does not look like an Anthropic API key "
                    "(expected prefix 'sk-ant-' and length > 50)"
                )
        else:
            # claude_code_pipe — api_key not used. If one is set, leave
            # it on the dataclass for harmless visibility but don't
            # require or validate it.
            pass
        default_model = claude_section.get("default_model", DEFAULT_CLAUDE_MODEL).strip()
        if not default_model:
            raise ConfigError("[claude].default_model must not be empty")
        scope_classifier_model = claude_section.get(
            "scope_classifier_model", DEFAULT_SCOPE_CLASSIFIER_MODEL,
        ).strip()
        if not scope_classifier_model:
            raise ConfigError(
                "[claude].scope_classifier_model must not be empty"
            )
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
        # Per-site overrides — `[llm.<site>]` sections. Optional; any
        # site not overridden inherits from the global defaults.
        per_site: dict[str, LlmSiteConfig] = {}
        for section_name in parser.sections():
            if not section_name.startswith("llm."):
                continue
            site_name = section_name.split(".", 1)[1].strip()
            if not site_name:
                raise ConfigError(
                    f"llm section has empty name: {section_name!r}"
                )
            if site_name not in KNOWN_LLM_SITES:
                raise ConfigError(
                    f"[{section_name}] names an unknown LLM call site "
                    f"{site_name!r}; expected one of "
                    f"{sorted(KNOWN_LLM_SITES)!r}"
                )
            site_section = parser[section_name]
            site_backend: str | None = None
            if "backend" in site_section:
                site_backend = site_section.get("backend", "").strip()
                if site_backend not in _KNOWN_BACKENDS:
                    raise ConfigError(
                        f"[{section_name}].backend must be one of "
                        f"{sorted(_KNOWN_BACKENDS)!r}; got {site_backend!r}"
                    )
                # If a per-site override flips to anthropic_api but the
                # global has no api_key, fail loudly. Catching this at
                # config load is much less confusing than catching it on
                # the first call to that site.
                if site_backend == BACKEND_ANTHROPIC_API and not api_key:
                    raise ConfigError(
                        f"[{section_name}].backend = {BACKEND_ANTHROPIC_API!r} "
                        f"requires [claude].api_key (set it in {secrets_path} "
                        "or nightjar.conf)"
                    )
            site_model: str | None = None
            if "model" in site_section:
                site_model = site_section.get("model", "").strip()
                if not site_model:
                    raise ConfigError(
                        f"[{section_name}].model must not be empty if set"
                    )
            per_site[site_name] = LlmSiteConfig(
                backend=site_backend, model=site_model,
            )

        claude = ClaudeConfig(
            api_key=api_key,
            default_model=default_model,
            per_hour_max_invocations=per_hour,
            per_invocation_max_input_tokens=per_inv_tokens,
            principal_per_message_cost_cents=cost_cents,
            principal_hard_kill_multiplier=kill_multiplier,
            principal_always_direct=always_direct,
            scope_classifier_model=scope_classifier_model,
            backend=backend,
            per_site=per_site,
        )

    agent_name = DEFAULT_AGENT_NAME
    agent_personality = DEFAULT_AGENT_PERSONALITY
    if "agent" in parser:
        agent_section = parser["agent"]
        raw_name = agent_section.get("name", "").strip()
        if raw_name:
            agent_name = raw_name
        raw_personality = agent_section.get("personality", "").strip()
        if raw_personality:
            agent_personality = raw_personality
    dispatch_policy = system_load.DispatchPolicy()
    if "agent.dispatch" in parser:
        ds = parser["agent.dispatch"]
        try:
            defer_gaming = ds.getboolean(
                "defer_when_gaming_mode",
                fallback=dispatch_policy.defer_when_gaming_mode,
            )
        except ValueError as e:
            raise ConfigError(
                f"[agent.dispatch].defer_when_gaming_mode must be true/false: {e}"
            ) from e
        try:
            defer_load = float(ds.get(
                "defer_when_load_above",
                str(dispatch_policy.defer_when_load_above),
            ))
        except ValueError as e:
            raise ConfigError(
                f"[agent.dispatch].defer_when_load_above must be a number: {e}"
            ) from e
        try:
            defer_mem = int(ds.get(
                "defer_when_memavail_below_mb",
                str(dispatch_policy.defer_when_memavail_below_mb),
            ))
        except ValueError as e:
            raise ConfigError(
                f"[agent.dispatch].defer_when_memavail_below_mb must be an integer: {e}"
            ) from e
        dispatch_policy = system_load.DispatchPolicy(
            defer_when_gaming_mode=defer_gaming,
            defer_when_load_above=defer_load,
            defer_when_memavail_below_mb=defer_mem,
        )
    agent = AgentConfig(
        name=agent_name,
        personality=agent_personality,
        dispatch=dispatch_policy,
    )

    return Config(
        daemon=daemon,
        contacts=contacts,
        inboxes=inboxes,
        security=security,
        smtp=smtp,
        claude=claude,
        agent=agent,
        address_index=address_index,
        scopes=scopes,
        facets=facets,
        projects=projects,
    )
