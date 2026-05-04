"""Post-approval action runner for tier-2+ principal verbs.

The watcher calls execute() exactly once per approval that has been
approved by the principal. The function dispatches to a per-verb
implementation, captures whatever the verb produced (a status line, a
modified file, an error), and returns an ExecutionResult that the
watcher then emails to the principal.

Failure modes are first-class: a verb that raises is captured rather
than crashing the watcher. The principal sees the failure in the reply
email; the audit trail records it. Tools that touch the world (file
writes, config rewrites) are responsible for their own atomicity.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .state import State


@dataclass(frozen=True)
class ExecutionResult:
    """The outcome of running one approved verb.

    `ok` is True if the verb completed without raising and reported
    success. `summary` is the human-readable single-line outcome that
    goes in the principal's email subject. `body` is the full report
    that goes in the email body.
    """
    ok: bool
    summary: str
    body: str


def execute(
    *,
    verb: str,
    args: dict,
    config: Config,
    state: State,
    now: int | None = None,
) -> ExecutionResult:
    """Dispatch one approved verb to its implementation."""
    fn = _DISPATCH.get(verb)
    if fn is None:
        return ExecutionResult(
            ok=False,
            summary=f"unknown verb '{verb}'",
            body=(
                f"No executor registered for verb '{verb}'.\n"
                "This indicates a code bug: a verb was queued for approval\n"
                "but its executor is missing. Check daemon/executor.py.\n"
            ),
        )
    try:
        return fn(args=args, config=config, state=state, now=now or int(time.time()))
    except Exception as e:
        return ExecutionResult(
            ok=False,
            summary=f"{verb} failed: {type(e).__name__}",
            body=(
                f"Executor for '{verb}' raised {type(e).__name__}: {e}\n"
                "\n"
                "Args:\n"
                f"  {args}\n"
            ),
        )


# ---- Executors -----------------------------------------------------------
# Each takes (args, config, state, now) and returns ExecutionResult.
# Stubs for now; bodies land in the next commit (task 33).


def _exec_block(*, args: dict, config: Config, state: State, now: int) -> ExecutionResult:
    raise NotImplementedError("block executor not yet wired (task 33)")


def _exec_unblock(*, args: dict, config: Config, state: State, now: int) -> ExecutionResult:
    raise NotImplementedError("unblock executor not yet wired (task 33)")


def _exec_forget(*, args: dict, config: Config, state: State, now: int) -> ExecutionResult:
    raise NotImplementedError("forget executor not yet wired (task 33)")


def _exec_add(*, args: dict, config: Config, state: State, now: int) -> ExecutionResult:
    raise NotImplementedError("add executor not yet wired (task 33)")


def _exec_remove(*, args: dict, config: Config, state: State, now: int) -> ExecutionResult:
    raise NotImplementedError("remove executor not yet wired (task 33)")


_DISPATCH: dict[str, Callable] = {
    "block": _exec_block,
    "unblock": _exec_unblock,
    "forget": _exec_forget,
    "add": _exec_add,
    "remove": _exec_remove,
}
