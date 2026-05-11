"""Nightjar daemon entry point.

Build Step 1 + 2 scope:
    - Load config from ~/.config/nightjar/nightjar.conf
    - Open SQLite state at ~/.local/share/nightjar/state.db
    - Refuse to start if the dead-man's-switch is set (run --revive first)
    - Open JSONL log at <log_dir>/nightjar-YYYY-MM-DD.jsonl
    - For each enabled [inbox:*], spawn an InboxWatcher asyncio task
    - Heartbeat to SQLite every minute (used later by cold-start logic)
    - On switch trip during runtime: write PANIC.txt, halt the loop
    - Handle SIGTERM / SIGINT cleanly
    - Subcommands: --revive, --setup-auth
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import signal
import sys
from pathlib import Path

from .config import Config, ConfigError, DEFAULT_CONFIG_PATH, load as load_config
from .inbox_watcher import InboxWatcher
from .log import JSONLLogger
from .state import State


def _peek_contacts_dir(config_path: Path) -> Path:
    """Read just enough of nightjar.conf to find contacts_dir.

    Used by the migrator before config.load() runs, since the loader
    refuses to parse a file with legacy [contact:*] blocks. Falls
    back to the default if the [daemon] section is missing or the
    field is unset.
    """
    import configparser
    parser = configparser.ConfigParser(interpolation=None)
    if config_path.exists():
        parser.read(config_path, encoding="utf-8")
    daemon_section = parser["daemon"] if "daemon" in parser else {}
    raw = daemon_section.get("contacts_dir", "~/.config/nightjar/contacts")
    import os as _os
    return Path(_os.path.expanduser(raw))


def _peek_state_dir(config_path: Path) -> Path:
    """Read just enough of nightjar.conf to find state_dir."""
    import configparser
    parser = configparser.ConfigParser(interpolation=None)
    if config_path.exists():
        parser.read(config_path, encoding="utf-8")
    daemon_section = parser["daemon"] if "daemon" in parser else {}
    raw = daemon_section.get("state_dir", "~/.local/share/nightjar")
    import os as _os
    return Path(_os.path.expanduser(raw))


def _panic_file_path(config: Config) -> Path:
    return config.daemon.state_dir / "PANIC.txt"


def _write_panic_file(config: Config, state: State, reason: str) -> Path:
    """Write the human-readable panic record. Best-effort, never raises."""
    path = _panic_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    recent = state.recent_auth_failures(limit=10)
    lines = [
        "Nightjar safety protocol tripped.",
        f"Tripped at: {now}",
        f"Reason: {reason}",
        "",
        "Recent auth failures (most recent first):",
    ]
    if not recent:
        lines.append("  (none recorded)")
    else:
        for f in recent:
            ts = datetime.datetime.fromtimestamp(
                f["ts"], tz=datetime.timezone.utc
            ).isoformat()
            lines.append(f"  {ts}  {f['from_addr']}  {f['reason']}")
    lines += [
        "",
        "To revive, run `nightjar --revive` at the physical machine.",
        "",
    ]
    try:
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass
    return path


HEARTBEAT_INTERVAL_SECONDS = 60


async def _heartbeat_loop(state: State, logger: JSONLLogger, stop: asyncio.Event) -> None:
    while not stop.is_set():
        state.heartbeat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


async def _main_async(config: Config) -> int:
    state = State(db_path=config.daemon.state_dir / "state.db")
    logger = JSONLLogger(log_dir=config.daemon.log_dir)
    logger.event(
        "daemon_start",
        inboxes=list(config.inboxes.keys()),
        contacts=len(config.contacts),
        claude_configured=config.claude is not None,
    )

    # Construct one Claude client per named call site, share across
    # watchers. Empty dict when [claude] is missing from config; in
    # that case the contact-mail branch falls through to RECEIVED
    # with disposition "no_claude_config" and no triage runs. The
    # daemon does NOT refuse to start without a [claude] section:
    # the principal command path (Steps 1-4) still works and is
    # independently useful.
    claude_clients: dict[str, object] = {}
    if config.claude is not None:
        from .cc_executor import build_claude_client_for
        from .config import KNOWN_LLM_SITES
        for site in sorted(KNOWN_LLM_SITES):
            claude_clients[site] = build_claude_client_for(site, config.claude)
            logger.event(
                "claude_backend_selected",
                site=site,
                backend=config.claude.backend_for_site(site),
                model=config.claude.model_for_site(site),
            )

    stop = asyncio.Event()
    panic_reason: dict[str, str] = {}  # mutable holder for the on_panic callback

    def _handle_signal(signame: str) -> None:
        logger.event("daemon_stop_requested", signal=signame)
        stop.set()

    def _on_panic(reason: str) -> None:
        # Record the reason so the post-loop teardown can write PANIC.txt.
        panic_reason["reason"] = reason
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig.name)

    watchers = [
        InboxWatcher(
            inbox=ic, config=config, state=state, logger=logger,
            on_panic=_on_panic, claude_clients=claude_clients,
        )
        for ic in config.inboxes.values()
    ]

    tasks = [asyncio.create_task(w.run(), name=f"watcher:{w.inbox.name}") for w in watchers]
    tasks.append(asyncio.create_task(_heartbeat_loop(state, logger, stop), name="heartbeat"))

    # Wait until stop is set, then cancel everything.
    await stop.wait()
    for w in watchers:
        w.stop()
    for t in tasks:
        t.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for t, r in zip(tasks, results):
        if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
            logger.event(
                "task_exited_with_error",
                level="warn",
                task=t.get_name(),
                error=type(r).__name__,
                message=str(r),
            )

    if "reason" in panic_reason:
        path = _write_panic_file(config, state, panic_reason["reason"])
        logger.event(
            "panic_halt",
            level="error",
            reason=panic_reason["reason"],
            panic_file=str(path),
        )

    logger.event("daemon_stop")
    logger.close()
    return 0


def _check_panic_preflight(config: Config) -> int | None:
    """If the panic flag is set, print the halt message and return exit code 3.

    Otherwise return None and let the daemon proceed.
    """
    state = State(db_path=config.daemon.state_dir / "state.db")
    info = state.panic_info()
    if info is None:
        return None
    when = ""
    if info["at"]:
        when = datetime.datetime.fromtimestamp(
            info["at"], tz=datetime.timezone.utc
        ).isoformat()
    print(
        f"nightjar: halted by safety protocol at {when}.\n"
        f"reason: {info['reason']}\n"
        f"to revive, run `nightjar --revive` at the physical machine.",
        file=sys.stderr,
    )
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nightjar", description="Nightjar email assistant daemon.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to nightjar.conf (default: ~/.config/nightjar/nightjar.conf)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--revive",
        action="store_true",
        help="clear the dead-man's-switch (requires physical TTY + valid auth code)",
    )
    mode.add_argument(
        "--setup-auth",
        action="store_true",
        help="generate an auth secret and print the provisioning URI",
    )
    mode.add_argument(
        "--test-notify",
        action="store_true",
        help="send a test email via the notifier path; pair with --principal or --contact",
    )
    mode.add_argument(
        "--show-config",
        action="store_true",
        help="print the active config with provenance (default vs from file); secrets redacted",
    )
    mode.add_argument(
        "--validate-config",
        action="store_true",
        help="run the config loader and print OK + summary, or a friendly error",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --setup-auth: overwrite an existing secret",
    )
    parser.add_argument(
        "--secondary",
        action="store_true",
        help=(
            "with --setup-auth: provision the secondary HOTP seed used by "
            "the agent path's two-secret gate. Default is to provision "
            "the primary."
        ),
    )
    parser.add_argument(
        "--principal",
        action="store_true",
        help="with --test-notify: target the principal (no audit, no footer)",
    )
    parser.add_argument(
        "--contact",
        metavar="CONTACT_ID",
        default=None,
        help="with --test-notify: target this contact (audit + footer; prompts to confirm)",
    )
    args = parser.parse_args(argv)

    # Read-only inspection commands short-circuit BEFORE any side-effecting
    # paths — no migrator, no machine-id check, no daemon spawn. They
    # exist precisely so an operator can `--validate-config` on a
    # half-set-up install or `--show-config` on a system where the
    # daemon is wedged. They run the same loader the daemon uses.
    if args.show_config:
        from . import config_inspect
        return config_inspect.show(args.config)
    if args.validate_config:
        from . import config_inspect
        return config_inspect.validate(args.config)

    # Run the contacts/secrets migrator BEFORE config.load(). It is
    # idempotent and a no-op once migration has run; on first start
    # after the Step 6c upgrade it moves [contact:*] blocks and
    # plaintext secrets out of nightjar.conf into per-file layouts.
    config_path_for_load = args.config or DEFAULT_CONFIG_PATH
    if not args.setup_auth:
        from . import contacts_migrator
        from .config import DEFAULT_SECRETS_PATH
        # contacts_dir defaults are baked into DaemonConfig; we need a
        # quick read of the [daemon] section to find any override
        # without doing a full load (which would crash on legacy state).
        try:
            contacts_dir = _peek_contacts_dir(config_path_for_load)
            report = contacts_migrator.migrate_if_needed(
                config_path_for_load, contacts_dir,
                secrets_path=DEFAULT_SECRETS_PATH,
            )
            if report.did_migrate:
                # Stamp the machine-id fingerprint into state.db so
                # subsequent starts can detect machine-id drift.
                if report.machine_id_fp is not None:
                    from .config import DaemonConfig
                    # We need state_dir to open the DB. Re-peek for it.
                    state_dir = _peek_state_dir(config_path_for_load)
                    state = State(db_path=state_dir / "state.db")
                    state.set_machine_id_fp(report.machine_id_fp)
                # JSONL log line on stderr so the operator sees it on
                # first start; the log infrastructure isn't up yet.
                print(
                    f"nightjar: migrated "
                    f"{report.contacts_migrated} contact(s) and "
                    f"{report.secrets_migrated} secret(s); backup at "
                    f"{report.backup_path}",
                    file=sys.stderr,
                )
        except contacts_migrator.MigrationError as e:
            print(f"nightjar: migration error: {e}", file=sys.stderr)
            return 2

    try:
        config = load_config(args.config) if args.config else load_config()
    except ConfigError as e:
        # --setup-auth is the only command that can run without [security].
        if not args.setup_auth:
            print(f"nightjar: config error: {e}", file=sys.stderr)
            return 2
        config = None  # setup_auth handles a missing/incomplete config itself
    except FileNotFoundError as e:
        print(f"nightjar: {e}", file=sys.stderr)
        return 2

    # Machine-id check: if we have a stored fingerprint and a
    # secrets.toml file is in use, verify the machine-id hasn't
    # changed since secrets were obfuscated.
    if config is not None and not args.setup_auth:
        from .config import DEFAULT_SECRETS_PATH
        if DEFAULT_SECRETS_PATH.exists():
            from . import secret_box
            try:
                current_fp = secret_box.machine_id_fingerprint()
            except secret_box.SecretBoxError as e:
                print(
                    f"nightjar: cannot read /etc/machine-id: {e}",
                    file=sys.stderr,
                )
                return 2
            state_for_check = State(db_path=config.daemon.state_dir / "state.db")
            stored_fp = state_for_check.get_machine_id_fp()
            if stored_fp is None:
                # First start with secrets.toml but no fingerprint
                # stored — happens for installs that already ran the
                # migrator before the fingerprint check shipped, or
                # if state.db was wiped. Stamp now.
                state_for_check.set_machine_id_fp(current_fp)
            elif stored_fp != current_fp:
                print(
                    "nightjar: /etc/machine-id has changed since secrets "
                    "were obfuscated. The secrets file is no longer "
                    "decodable on this machine. Restore "
                    f"{config_path_for_load}.pre-migration.bak (if you "
                    "have it) or re-run setup. Refusing to start.",
                    file=sys.stderr,
                )
                return 2

    if args.setup_auth:
        from . import setup_auth
        target = (
            setup_auth.TARGET_SECONDARY if args.secondary
            else setup_auth.TARGET_PRIMARY
        )
        return setup_auth.run(
            args.config, force=args.force, target=target,
        )

    if args.revive:
        from . import revive
        return revive.run(config)

    if args.test_notify:
        from . import test_notify
        return test_notify.run(
            config, principal=args.principal, contact=args.contact
        )

    code = _check_panic_preflight(config)
    if code is not None:
        return code

    # Boot-time smoke test: confirm the compose_reply MCP server is
    # spawnable and registers its tool. Catches deployment regressions
    # (broken script, missing imports, environment issues) up-front
    # rather than as a silent per-session degradation. Per-session
    # fallback to final_text still applies if this somehow passes
    # boot but fails at runtime.
    from . import compose_reply_smoke
    try:
        compose_reply_smoke.probe_mcp_server()
    except compose_reply_smoke.ComposeReplyProbeError as e:
        print(
            f"nightjar: compose_reply MCP probe failed at boot: {e}\n"
            f"  Refusing to start. Run "
            f"`python -m daemon.compose_reply_smoke` to reproduce.",
            file=sys.stderr,
        )
        return 2

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
