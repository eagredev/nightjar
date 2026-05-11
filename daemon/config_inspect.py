"""Read-only operator commands: show + validate the active config.

These are surfaced via `nightjar --show-config` / `--validate-config`
in `main.py`. Neither command starts the daemon; both run the same
config loader the daemon would and either print or summarise.

`--show-config` is a discoverability tool: it dumps every section's
resolved values, marking which came from the operator's INI vs which
are defaults. Secrets are never printed — only "(set)" / "(not set)".

`--validate-config` is a CI-style check: load the config, catch any
ConfigError, and either print "OK" with summary stats or a friendly
error message and a non-zero exit.

Background: the project has grown to ~50 settings across ~10
sections. The maintenance cost of a control-panel UI isn't worth
paying yet (project is pre-1.0; design is still in flux), but the
discoverability pain is real. These two commands plus a freshened
`example.conf` solve the same pain at one-tenth the cost.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import (
    Config,
    ConfigError,
    DEFAULT_AGENT_NAME,
    DEFAULT_AGENT_PERSONALITY,
    load,
)


def _fmt_value(value: object, default: object | None) -> str:
    """Render a value with a (default) / (from file) tag.

    `default=None` means we couldn't compute a default for this field
    (e.g. it's a credential or a complex object); the tag is omitted.
    """
    if default is None:
        rendered = repr(value) if isinstance(value, str) else str(value)
        return rendered
    if value == default:
        return f"{value!r}  (default)"
    return f"{value!r}  (from file)"


def _print_section(name: str, lines: list[tuple[str, str]]) -> None:
    print(f"[{name}]")
    if not lines:
        print("  (not configured)")
    else:
        width = max(len(k) for k, _ in lines)
        for k, v in lines:
            print(f"  {k:<{width}}  {v}")
    print()


def show(config_path: Path | None) -> int:
    """Print the active config with provenance. Exit 0 on success,
    2 if the config can't be loaded (ConfigError, missing file).
    """
    try:
        cfg = load(config_path) if config_path is not None else load()
    except (ConfigError, FileNotFoundError) as e:
        print(f"nightjar: cannot show config: {e}", file=sys.stderr)
        return 2

    actual_path = config_path or Path("~/.config/nightjar/nightjar.conf").expanduser()
    print(f"# Nightjar config: {actual_path}")
    print(f"# Showing active values. (default) = unchanged from code, "
          f"(from file) = set in your INI.")
    print()

    # [daemon] — paths. Defaults are paths off ~, hard to compare exactly,
    # so we omit the tag.
    _print_section("daemon", [
        ("state_dir",   _fmt_value(str(cfg.daemon.state_dir),   default=None)),
        ("log_dir",     _fmt_value(str(cfg.daemon.log_dir),     default=None)),
        ("notes_dir",   _fmt_value(str(cfg.daemon.notes_dir),   default=None)),
        ("contacts_dir", _fmt_value(str(cfg.daemon.contacts_dir), default=None)),
    ])

    # [security] — secrets are NEVER printed. Just presence.
    sec = cfg.security
    if sec is None:
        _print_section("security", [("(not configured)", "")])
    else:
        _print_section("security", [
            ("auth_mode",                       _fmt_value(sec.auth_mode, default="hotp")),
            ("totp_secret",                     "(set)"),
            ("secondary_hotp_secret",           "(set)" if sec.secondary_hotp_secret else "(not set)"),
            ("dead_mans_switch_window_minutes", _fmt_value(sec.dead_mans_switch_window_minutes, default=60)),
            ("dead_mans_switch_threshold",      _fmt_value(sec.dead_mans_switch_threshold,      default=3)),
        ])

    # [smtp] — credentials redacted.
    smtp = cfg.smtp
    if smtp is None:
        _print_section("smtp", [("(not configured)", "")])
    else:
        _print_section("smtp", [
            ("host",      _fmt_value(smtp.host,     default=None)),
            ("port",      _fmt_value(smtp.port,     default=587)),
            ("user",      _fmt_value(smtp.user,     default=None)),
            ("password",  "(set)"),
            ("from_name", _fmt_value(smtp.from_name, default=None)),
            ("from_addr", _fmt_value(smtp.from_addr, default=None)),
        ])

    # [claude] — credentials redacted.
    cl = cfg.claude
    if cl is None:
        _print_section("claude", [
            ("(not configured)", "(LLM features disabled)"),
        ])
    else:
        _print_section("claude", [
            ("backend",                          _fmt_value(cl.backend,                          default="anthropic_api")),
            ("api_key",                          "(set)" if cl.api_key else "(not set)"),
            ("default_model",                    _fmt_value(cl.default_model,                    default="claude-haiku-4-5")),
            ("scope_classifier_model",           _fmt_value(cl.scope_classifier_model,           default="claude-haiku-4-5")),
            ("per_hour_max_invocations",         _fmt_value(cl.per_hour_max_invocations,         default=30)),
            ("per_invocation_max_input_tokens",  _fmt_value(cl.per_invocation_max_input_tokens,  default=8000)),
            ("principal_per_message_cost_cents", _fmt_value(cl.principal_per_message_cost_cents, default=10)),
            ("principal_hard_kill_multiplier",   _fmt_value(cl.principal_hard_kill_multiplier,   default=5)),
            ("principal_always_direct",          _fmt_value(cl.principal_always_direct,          default=False)),
        ])

    # [agent] + [agent.dispatch].
    a = cfg.agent
    _print_section("agent", [
        ("name",        _fmt_value(a.name,        default=DEFAULT_AGENT_NAME)),
        ("personality", _fmt_value(a.personality, default=DEFAULT_AGENT_PERSONALITY)),
    ])
    d = a.dispatch
    _print_section("agent.dispatch", [
        ("defer_when_gaming_mode",       _fmt_value(d.defer_when_gaming_mode,       default=False)),
        ("defer_when_load_above",        _fmt_value(d.defer_when_load_above,        default=0.0)),
        ("defer_when_memavail_below_mb", _fmt_value(d.defer_when_memavail_below_mb, default=0)),
    ])

    # Per-call-site LLM overrides (live on cfg.claude.per_site).
    if cl is not None and cl.per_site:
        for site, override in sorted(cl.per_site.items()):
            lines: list[tuple[str, str]] = []
            if override.backend is not None:
                lines.append(("backend", _fmt_value(override.backend, default=None)))
            if override.model is not None:
                lines.append(("model",   _fmt_value(override.model,   default=None)))
            _print_section(f"llm.{site}", lines)

    # Inboxes — credentials redacted.
    for ib_name, ib in sorted(cfg.inboxes.items()):
        _print_section(f"inbox:{ib_name}", [
            ("enabled",             _fmt_value(ib.enabled,             default=True)),
            ("imap_host",           _fmt_value(ib.imap_host,           default=None)),
            ("imap_port",           _fmt_value(ib.imap_port,           default=993)),
            ("imap_user",           _fmt_value(ib.imap_user,           default=None)),
            ("imap_password",       "(set)" if ib.imap_password else "(not set)"),
            ("trusted_authserv",    _fmt_value(ib.trusted_authserv,    default=None)),
            ("catchup_window_days", _fmt_value(ib.catchup_window_days, default=7)),
            ("status_walk_count",   _fmt_value(ib.status_walk_count,   default=200)),
            ("poll_interval_seconds", _fmt_value(ib.poll_interval_seconds, default=60)),
        ])

    # Scope/facet/project taxonomies — names only (descriptions go to triage).
    if cfg.scopes:
        _print_section("scopes",
            [(k, repr(v)) for k, v in sorted(cfg.scopes.items())])
    if cfg.facets:
        _print_section("facets",
            [(k, repr(v)) for k, v in sorted(cfg.facets.items())])
    if cfg.projects:
        _print_section("projects",
            [(k, repr(v)) for k, v in sorted(cfg.projects.items())])

    # Contacts — addresses redacted to local-part only on non-principal
    # rows for safety if an operator pastes the output somewhere.
    print("contacts:")
    for cid, contact in sorted(cfg.contacts.items()):
        marker = "*" if contact.is_principal else " "
        print(
            f"  {marker} {cid:<20}  daily_limit={contact.daily_limit:<10}  "
            f"inboxes={list(contact.inboxes)}"
        )
    print()
    print("# (Per-contact details live in ~/.config/nightjar/contacts/<id>.toml)")
    return 0


def validate(config_path: Path | None) -> int:
    """Run the config loader and report. Exit 0 on success, 2 on
    ConfigError or missing file."""
    actual_path = config_path or Path("~/.config/nightjar/nightjar.conf").expanduser()
    try:
        cfg = load(config_path) if config_path is not None else load()
    except FileNotFoundError as e:
        print(f"nightjar: config file not found: {e}", file=sys.stderr)
        return 2
    except ConfigError as e:
        print(f"nightjar: config invalid:\n  {e}", file=sys.stderr)
        return 2
    return _summarise_ok(cfg, actual_path)


def _summarise_ok(cfg: Config, path: Path) -> int:
    """One-shot success summary. Highlights the things an operator
    most often gets wrong: missing principal, no inboxes, dispatch
    deferral on without security configured, etc."""
    n_contacts = len(cfg.contacts)
    n_principals = sum(1 for c in cfg.contacts.values() if c.is_principal)
    n_inboxes = len(cfg.inboxes)
    n_enabled_inboxes = sum(1 for i in cfg.inboxes.values() if i.enabled)
    n_scopes = len(cfg.scopes)
    n_facets = len(cfg.facets)
    n_projects = len(cfg.projects)
    n_llm_sites = len(cfg.claude.per_site) if cfg.claude else 0
    dispatch = cfg.agent.dispatch
    dispatch_active = (
        dispatch.defer_when_gaming_mode
        or dispatch.defer_when_load_above > 0
        or dispatch.defer_when_memavail_below_mb > 0
    )

    print(f"OK: {path}")
    print(f"  contacts       : {n_contacts} ({n_principals} principal)")
    print(f"  inboxes        : {n_inboxes} ({n_enabled_inboxes} enabled)")
    print(f"  scopes/facets/projects : {n_scopes}/{n_facets}/{n_projects}")
    print(f"  security       : "
          f"{'configured' if cfg.security else 'NOT configured'}")
    print(f"  smtp           : "
          f"{'configured' if cfg.smtp else 'NOT configured'}")
    print(f"  claude         : "
          f"{'configured' if cfg.claude else 'not configured (LLM features disabled)'}")
    if n_llm_sites:
        print(f"  llm overrides  : {n_llm_sites} site(s)")
    print(f"  agent name     : {cfg.agent.name!r}")
    print(f"  dispatch defer : "
          f"{'ON' if dispatch_active else 'off'}")
    if dispatch_active:
        if dispatch.defer_when_gaming_mode:
            print(f"    - on gamescope (gaming mode)")
        if dispatch.defer_when_load_above > 0:
            print(f"    - load_1m > {dispatch.defer_when_load_above}")
        if dispatch.defer_when_memavail_below_mb > 0:
            print(f"    - memavail < {dispatch.defer_when_memavail_below_mb} MiB")
    return 0
