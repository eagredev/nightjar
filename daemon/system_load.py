"""System-load detection: should agent dispatch defer right now?

The Steam Deck has two graphical session modes — Plasma (KDE x11) and
Gamescope (wayland gaming mode). The principal gaming during a heavy
Opus 4.7 session is the canonical "system busy" condition: the agent
might lag the game, hit the systemd MemoryMax cap, or both.

This module answers one question: at this instant, should we defer
agent dispatch? It uses `loginctl` (already on every SteamOS install)
to inspect seat0's active session. Optional secondary signals — load
average and available memory — let the operator widen the trigger
without writing more code.

Fail-open discipline: if `loginctl` times out, errors, or returns
something we can't parse, we treat the system as NOT busy. Never
strand the principal's request because our detection broke.

Background: ~/nightjar/docs/agent-defer-when-busy.md.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

LOGINCTL_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class DispatchPolicy:
    """Operator knobs. All defaults preserve today's behaviour
    (no deferral) until an operator opts in via [agent.dispatch]."""
    defer_when_gaming_mode: bool = False
    defer_when_load_above: float = 0.0  # 0 disables
    defer_when_memavail_below_mb: int = 0  # 0 disables


def _run_loginctl(args: list[str]) -> str | None:
    """Return loginctl --value output, or None on any failure.

    Always uses a hard timeout. Never raises — every failure mode
    (binary missing, non-zero exit, timeout, garbage output) returns
    None and the caller treats that as "can't tell — assume free."
    """
    try:
        result = subprocess.run(
            ["loginctl", *args],
            capture_output=True,
            text=True,
            timeout=LOGINCTL_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _query_active_session_id() -> str | None:
    """Active session on seat0, or None on any error.

    Steam Deck has exactly one seat (seat0); on a multi-seat box this
    would need adjustment but the daemon is single-host single-seat
    by design.
    """
    out = _run_loginctl(["show-seat", "seat0", "-p", "ActiveSession", "--value"])
    if out is None:
        return None
    sid = out.strip()
    return sid if sid else None


def _query_session_props(session_id: str) -> dict[str, str]:
    """Return a {prop: value} dict for the requested properties.

    `loginctl show-session N -p A -p B --value` returns the values
    in alphabetical order by property name (NOT in the order they
    were requested), one per line. We sort the requested names
    locally to align.
    """
    requested = ["Class", "Desktop", "Type"]
    args = ["show-session", session_id]
    for name in requested:
        args += ["-p", name]
    args += ["--value"]
    out = _run_loginctl(args)
    if out is None:
        return {}
    lines = out.splitlines()
    # loginctl emits values in alphabetical order of property name.
    sorted_names = sorted(requested)
    if len(lines) < len(sorted_names):
        return {}
    return dict(zip(sorted_names, (line for line in lines)))


def _read_loadavg_1m() -> float | None:
    """1-minute load average from /proc/loadavg, or None on error."""
    try:
        text = Path("/proc/loadavg").read_text()
    except OSError:
        return None
    parts = text.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def _read_memavail_mb() -> int | None:
    """MemAvailable from /proc/meminfo in MiB, or None on error."""
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1]) // 1024  # kB to MiB
                except ValueError:
                    return None
    return None


def is_system_busy(policy: DispatchPolicy) -> tuple[bool, str]:
    """Return (is_busy, reason). reason is human-readable for logs
    and the deferred-reply email body.

    Order: gaming mode → load average → mem available. First match
    wins. All checks fail-open: if we can't probe a signal, that
    signal does not flag busy.
    """
    if policy.defer_when_gaming_mode:
        sid = _query_active_session_id()
        if sid is not None:
            props = _query_session_props(sid)
            desktop = props.get("Desktop", "")
            sess_type = props.get("Type", "")
            if "gamescope" in desktop.lower():
                return True, "principal is in gaming mode (gamescope)"
            # On a Steam Deck the only wayland session is gamescope; on
            # other hardware this would over-trigger, but the policy is
            # opt-in and the doc names Steam Deck explicitly.
            if sess_type == "wayland":
                return True, f"principal session is wayland (desktop={desktop or '?'})"
    if policy.defer_when_load_above > 0:
        load_1m = _read_loadavg_1m()
        if load_1m is not None and load_1m > policy.defer_when_load_above:
            return True, (
                f"system load {load_1m:.2f} > "
                f"{policy.defer_when_load_above:.2f}"
            )
    if policy.defer_when_memavail_below_mb > 0:
        memavail = _read_memavail_mb()
        if memavail is not None and memavail < policy.defer_when_memavail_below_mb:
            return True, (
                f"memory available {memavail} MiB < "
                f"{policy.defer_when_memavail_below_mb} MiB"
            )
    return False, "system free"
