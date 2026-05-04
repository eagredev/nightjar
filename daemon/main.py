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

from .config import Config, ConfigError, load as load_config
from .inbox_watcher import InboxWatcher
from .log import JSONLLogger
from .state import State


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

    # Construct the Claude client once, share across watchers. None when
    # [claude] is missing from config; in that case the contact-mail
    # branch falls through to RECEIVED with disposition "no_claude_config"
    # and no triage runs. The daemon does NOT refuse to start without a
    # [claude] section: the principal command path (Steps 1-4) still
    # works and is independently useful.
    claude_client = None
    if config.claude is not None:
        from .triage import AnthropicClient
        claude_client = AnthropicClient(api_key=config.claude.api_key)

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
            on_panic=_on_panic, claude_client=claude_client,
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --setup-auth: overwrite an existing secret",
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

    if args.setup_auth:
        from . import setup_auth
        return setup_auth.run(args.config, force=args.force)

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

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
