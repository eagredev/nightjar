"""Nightjar daemon entry point.

Build Step 1 scope:
    - Load config from ~/.config/nightjar/nightjar.conf
    - Open SQLite state at ~/.local/share/nightjar/state.db
    - Open JSONL log at <log_dir>/nightjar-YYYY-MM-DD.jsonl
    - For each enabled [inbox:*], spawn an InboxWatcher asyncio task
    - Heartbeat to SQLite every minute (used later by cold-start logic)
    - Handle SIGTERM / SIGINT cleanly
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from .config import Config, ConfigError, load as load_config
from .inbox_watcher import InboxWatcher
from .log import JSONLLogger
from .state import State


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
    )

    stop = asyncio.Event()

    def _handle_signal(signame: str) -> None:
        logger.event("daemon_stop_requested", signal=signame)
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig.name)

    watchers = [
        InboxWatcher(inbox=ic, config=config, state=state, logger=logger)
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

    logger.event("daemon_stop")
    logger.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nightjar", description="Nightjar email assistant daemon.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to nightjar.conf (default: ~/.config/nightjar/nightjar.conf)",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config) if args.config else load_config()
    except ConfigError as e:
        print(f"nightjar: config error: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"nightjar: {e}", file=sys.stderr)
        return 2

    try:
        return asyncio.run(_main_async(config))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
