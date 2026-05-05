"""Builder for the principal-facing status report.

The thin pre-Step-6g `status` command answered "is the daemon alive?"
and counted approval rows. That's not enough to keep the operator in
the loop. This module replaces it with a systemic accountability
surface — one report email that answers, in order:

  1. Daemon health             — uptime, last catchup, last Claude call,
                                  last sent message, IMAP connection state.
  2. Awaiting your action      — open approval tickets the operator can
                                  reply to.
  3. Approaching expiry        — tickets within 24h of expires_at.
  4. In-flight (mid-pipeline)  — messages received but not at approval;
                                  steady state empty.
  5. Out-of-band mail          — IMAP walk over the last
                                  status_walk_count messages, cross-
                                  referenced against the messages table.
  6. Recently sent             — last N outbound messages.
  7. Footer                    — pointer to `audit` for full sweeps.

All sections are fed by state-db queries plus one IMAP walk per inbox.
The walk is the only network I/O; everything else is microsecond-cost.
The report is a plain-text email body that the deterministic `status`
verb returns, so it threads naturally with the principal's request.

Sections degrade gracefully when their inputs are unavailable: a
section with no rows returns "(none)" rather than being dropped, so
the structure of the report is stable across days. Empty sections are
useful signal — "Awaiting your action: (none)" is exactly what the
operator wants to see most days.
"""
from __future__ import annotations

import datetime
import email
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import principal_commands
from .config import Config
from .state import State


# How far in the future an approval can be before it shows up in the
# "Approaching expiry" section. 24 hours: late enough to give the
# operator a full reading cycle but early enough to act.
DEFAULT_EXPIRY_WINDOW_SECONDS = 24 * 3600

# How long an in-flight message is allowed to sit before we flag it.
# 10 minutes is plenty for a normal triage round-trip; anything older
# is mid-pipeline AND stuck.
IN_FLIGHT_GRACE_SECONDS = 10 * 60

# How many recent outbound messages the report includes inline. More
# than this lives in the audit report or the outbound_log table.
RECENT_OUTBOUND_LIMIT = 10

# How many out-of-band messages to list per inbox before truncating.
# Anything more turns the email into a wall.
OUT_OF_BAND_LIST_LIMIT = 10


# ---- Out-of-band message classification ----------------------------------

OOB_PRE_NIGHTJAR = "pre_nightjar"
OOB_WITHIN_WINDOW = "within_window"
OOB_BEYOND_DAEMON = "beyond_daemon"


@dataclass(frozen=True)
class OutOfBandMessage:
    """One IMAP message that's not in the state-db `messages` table.

    `category` slots the message into the right sub-list of the
    out-of-band section:
      pre_nightjar   — older than now() - catchup_window. Will not get
                       picked up automatically; principal can use
                       `pickup <message-id>` to force it.
      within_window  — should have been picked up but wasn't. The next
                       catchup will get it; including it here is just
                       informational.
      beyond_daemon  — received before the daemon was ever installed
                       (older than the earliest row in `messages`).
    """
    inbox: str
    uid: str
    message_id: str
    from_addr: str
    subject: str
    received_at: int  # unix
    category: str


@dataclass(frozen=True)
class StatusReport:
    """The full structured status output. Caller renders to email
    body. Keeping it structured (not just a string) makes it
    test-friendly and lets a future digest feature reuse the data
    without re-parsing strings."""
    generated_at: int
    health: dict[str, Any]
    awaiting: list[dict]
    expiring: list[dict]
    in_flight: list[dict]
    out_of_band: dict[str, list[OutOfBandMessage]]  # by inbox name
    recent_outbound: list[dict]


# ---- IMAP walk helper ----------------------------------------------------


@dataclass(frozen=True)
class InboxWalkResult:
    """One inbox's contribution to the IMAP walk. The status-report
    builder converts these into the OutOfBandMessage list."""
    inbox: str
    walked_count: int
    headers: tuple[dict[str, Any], ...]
    error: str | None  # set when the walk failed; headers will be empty


# Type alias for the injected walker. The watcher passes a callable
# that performs the actual IMAP work; this module never opens an IMAP
# connection of its own. Keeps status_report.py testable (tests pass
# a fake walker) and keeps the IMAP plumbing centralised in
# inbox_watcher.py.
InboxWalker = Callable[[str, int], Awaitable[InboxWalkResult]]


# ---- Section builders ----------------------------------------------------


def build_health_block(*, state: State, config: Config) -> dict[str, Any]:
    """Daemon health snapshot. Pure state-db queries.

    Fields:
      now_iso              — current time, ISO8601
      heartbeat_iso        — last heartbeat row, or "(never)"
      last_catchup_iso_per_inbox — { inbox_name: iso | "(never)" }
      last_claude_call_iso — last successful Claude invocation
      last_sent_iso        — last successful outbound send
      panicked             — bool; if true, dead-man's-switch tripped
      panic_info           — dict | None
    """
    now = int(time.time())
    heartbeat = state.last_heartbeat()
    last_claude = state.last_successful_claude_invocation_at()
    last_sent = state.last_outbound_sent_at()
    panic = state.panic_info()

    per_inbox = {}
    for inbox_name in config.inboxes:
        ts = state.get_last_catchup_at(inbox_name)
        per_inbox[inbox_name] = _iso_or_never(ts)

    return {
        "now_iso": _iso(now),
        "heartbeat_iso": _iso_or_never(heartbeat),
        "last_catchup_iso_per_inbox": per_inbox,
        "last_claude_call_iso": _iso_or_never(last_claude),
        "last_sent_iso": _iso_or_never(last_sent),
        "panicked": state.is_panicked(),
        "panic_info": panic,
    }


def build_awaiting_section(*, state: State, now: int) -> list[dict]:
    """Open approval tickets, oldest-first. Returns the same shape
    state.list_pending_approvals does, with an `age_seconds` field
    added for rendering."""
    rows = state.list_pending_approvals(now=now)
    out = []
    for r in rows:
        age = max(0, now - int(r.get("created_at", now)))
        d = dict(r)
        d["age_seconds"] = age
        out.append(d)
    return out


def build_expiring_section(*, state: State, now: int) -> list[dict]:
    """Approval tickets due to expire within 24h. Sorted soonest-first."""
    rows = state.expiring_approvals(
        within_seconds=DEFAULT_EXPIRY_WINDOW_SECONDS, now=now,
    )
    out = []
    for r in rows:
        ttl = max(0, int(r["expires_at"]) - now)
        d = dict(r)
        d["ttl_seconds"] = ttl
        out.append(d)
    return out


def build_in_flight_section(*, state: State, now: int) -> list[dict]:
    """Mid-pipeline messages older than IN_FLIGHT_GRACE_SECONDS.
    Steady state empty; non-empty means triage stalled or daemon hit
    a wall."""
    return state.in_flight_messages(
        older_than_seconds=IN_FLIGHT_GRACE_SECONDS,
        now=now,
    )


def classify_out_of_band(
    *,
    headers: tuple[dict[str, Any], ...],
    state_message_ids: set[str],
    catchup_window_seconds: int,
    daemon_first_seen_at: int | None,
    inbox: str,
    now: int,
) -> list[OutOfBandMessage]:
    """Pure classifier: given fetched headers, the set of Message-IDs
    the state-db knows about, and the catchup window, return only
    the messages that are out-of-band, each tagged with a category.

    Time fields:
      pre_nightjar:   received_at < now - catchup_window_seconds
      within_window:  received_at >= now - catchup_window_seconds
      beyond_daemon:  received_at < daemon_first_seen_at (this overrides
                      pre_nightjar; messages that predate the daemon's
                      first install are neither category-1 nor 2)
    """
    out: list[OutOfBandMessage] = []
    cutoff_window = now - catchup_window_seconds
    for h in headers:
        message_id = h.get("message_id") or ""
        if not message_id:
            continue
        if message_id in state_message_ids:
            continue
        received_at = int(h.get("received_at") or 0)
        if (
            daemon_first_seen_at is not None
            and received_at > 0
            and received_at < daemon_first_seen_at
        ):
            category = OOB_BEYOND_DAEMON
        elif received_at < cutoff_window:
            category = OOB_PRE_NIGHTJAR
        else:
            category = OOB_WITHIN_WINDOW
        out.append(OutOfBandMessage(
            inbox=inbox,
            uid=str(h.get("uid", "")),
            message_id=message_id,
            from_addr=str(h.get("from_addr", "")),
            subject=str(h.get("subject", "")),
            received_at=received_at,
            category=category,
        ))
    return out


async def build_out_of_band_section(
    *,
    state: State,
    config: Config,
    walker: InboxWalker,
    now: int,
) -> dict[str, list[OutOfBandMessage]]:
    """Walk each enabled inbox, classify messages not in the state-db.

    Returns a per-inbox dict; an inbox whose walk failed gets an empty
    list and the error is exposed via the `walker` callable's
    InboxWalkResult.error path (the caller can surface that up to the
    health block if it wants).
    """
    result: dict[str, list[OutOfBandMessage]] = {}
    daemon_first = state.first_message_received_at()
    for inbox_name, inbox_cfg in config.inboxes.items():
        if not inbox_cfg.enabled:
            continue
        walked = await walker(inbox_name, inbox_cfg.status_walk_count)
        if walked.error is not None:
            result[inbox_name] = []
            continue
        ids_in_db = state.list_message_ids_in_db(inbox=inbox_name)
        oob = classify_out_of_band(
            headers=walked.headers,
            state_message_ids=ids_in_db,
            catchup_window_seconds=inbox_cfg.catchup_window_days * 86400,
            daemon_first_seen_at=daemon_first,
            inbox=inbox_name,
            now=now,
        )
        result[inbox_name] = oob
    return result


def build_recent_outbound_section(*, state: State) -> list[dict]:
    """Last RECENT_OUTBOUND_LIMIT outbound rows. Most recent first."""
    return state.list_recent_outbound(limit=RECENT_OUTBOUND_LIMIT)


# ---- Top-level orchestrator ----------------------------------------------


async def build_status_report(
    *,
    state: State,
    config: Config,
    walker: InboxWalker,
    now: int | None = None,
) -> StatusReport:
    """Compose all sections into one StatusReport.

    The walker is async because it does IMAP I/O; everything else is
    sync. We stage the sync sections first, then await the walker
    last so a slow IMAP walk doesn't delay the cheap state-db work.
    """
    now = now if now is not None else int(time.time())
    health = build_health_block(state=state, config=config)
    awaiting = build_awaiting_section(state=state, now=now)
    expiring = build_expiring_section(state=state, now=now)
    in_flight = build_in_flight_section(state=state, now=now)
    recent_outbound = build_recent_outbound_section(state=state)
    out_of_band = await build_out_of_band_section(
        state=state, config=config, walker=walker, now=now,
    )
    return StatusReport(
        generated_at=now,
        health=health,
        awaiting=awaiting,
        expiring=expiring,
        in_flight=in_flight,
        out_of_band=out_of_band,
        recent_outbound=recent_outbound,
    )


# ---- Renderer ------------------------------------------------------------


def render_status_report(report: StatusReport) -> str:
    """Render a StatusReport into a plain-text email body.

    Format goal: dense but scannable. Each section has a heading and
    a table or short paragraph. Empty sections show "(none)" rather
    than being elided, so the report shape is stable day-to-day.
    """
    out: list[str] = []
    out.append(f"Nightjar status @ {report.health['now_iso']}")
    out.append("=" * 60)
    out.append("")

    # 1. Health.
    out.append("DAEMON HEALTH")
    out.append("-" * 60)
    h = report.health
    if h["panicked"]:
        info = h["panic_info"] or {}
        out.append(
            f"  *** PANICKED ***   reason: {info.get('panic_reason', '?')}"
        )
        out.append(
            f"                     since:  "
            f"{_iso_or_never(info.get('panic_at'))}"
        )
    out.append(f"  heartbeat:        {h['heartbeat_iso']}")
    for inbox, ts in h["last_catchup_iso_per_inbox"].items():
        out.append(f"  last catchup:     {ts}  ({inbox})")
    out.append(f"  last claude call: {h['last_claude_call_iso']}")
    out.append(f"  last sent:        {h['last_sent_iso']}")
    out.append("")

    # 2. Awaiting.
    out.append(f"AWAITING YOUR ACTION  ({len(report.awaiting)})")
    out.append("-" * 60)
    if not report.awaiting:
        out.append("  (none)")
    else:
        for r in report.awaiting:
            out.append(_format_approval_line(r, age_field="age_seconds"))
        out.append("")
        out.append(
            "  To respond: reply with subject `Re: [Nightjar #<token>] <code>`"
            "\n  body `yes` (or `no` to deny)."
        )
    out.append("")

    # 3. Expiring.
    out.append(f"APPROACHING EXPIRY  ({len(report.expiring)})")
    out.append("-" * 60)
    if not report.expiring:
        out.append("  (none)")
    else:
        for r in report.expiring:
            out.append(_format_approval_line(r, age_field="ttl_seconds"))
    out.append("")

    # 4. In-flight.
    out.append(f"IN FLIGHT (mid-pipeline)  ({len(report.in_flight)})")
    out.append("-" * 60)
    if not report.in_flight:
        out.append("  (none — clean)")
    else:
        for r in report.in_flight:
            age = max(0, report.generated_at - int(r.get("updated_at", 0)))
            out.append(
                f"  {r.get('state', '?'):20s} "
                f"{(r.get('subject') or '')[:40]:40s}  "
                f"age={_format_duration(age)}"
            )
    out.append("")

    # 5. Out-of-band.
    total_oob = sum(len(v) for v in report.out_of_band.values())
    out.append(f"OUT-OF-BAND MAIL  ({total_oob} across {len(report.out_of_band)} inbox(es))")
    out.append("-" * 60)
    if total_oob == 0:
        out.append("  (none)")
    else:
        for inbox_name, msgs in report.out_of_band.items():
            if not msgs:
                out.append(f"  [{inbox_name}] (none)")
                continue
            out.append(f"  [{inbox_name}]")
            shown = 0
            for m in msgs[:OUT_OF_BAND_LIST_LIMIT]:
                cat_label = {
                    OOB_PRE_NIGHTJAR: "pre-nightjar",
                    OOB_WITHIN_WINDOW: "within-window",
                    OOB_BEYOND_DAEMON: "beyond-daemon",
                }.get(m.category, m.category)
                out.append(
                    f"    {cat_label:14s}  {m.from_addr[:30]:30s}  "
                    f"{(m.subject or '')[:35]:35s}"
                )
                out.append(f"      message_id: {m.message_id}")
                shown += 1
            remaining = len(msgs) - shown
            if remaining > 0:
                out.append(
                    f"    ... and {remaining} more (run `audit` for the full list)"
                )
        out.append("")
        out.append(
            "  To pull one of these in: reply `[<code>] pickup <message-id>`."
        )
    out.append("")

    # 6. Recent outbound.
    out.append(f"RECENTLY SENT  ({len(report.recent_outbound)})")
    out.append("-" * 60)
    if not report.recent_outbound:
        out.append("  (none)")
    else:
        for r in report.recent_outbound:
            ts_iso = _iso_or_never(r.get("ts"))
            ok_marker = "ok" if r.get("ok") else "FAIL"
            out.append(
                f"  {ts_iso}  [{ok_marker}]  -> {r.get('to_addr', '?')[:40]}"
            )
            # Subject on its own indented line — no length cap. The
            # subject is the most useful field for spotting accidental
            # sends; we don't truncate it. Wrapping in email clients
            # is fine.
            subj = r.get("subject") or ""
            if subj:
                out.append(f"      {subj}")
    out.append("")

    # 7. Footer.
    out.append("-" * 60)
    out.append(
        "Showing recent activity per-inbox to "
        f"status_walk_count messages back. "
        "For a full inbox sweep, use `audit`."
    )
    return "\n".join(out) + "\n"


# ---- Helpers -------------------------------------------------------------


def _iso(ts: int | None) -> str:
    if ts is None:
        return "(never)"
    return datetime.datetime.fromtimestamp(
        int(ts), tz=datetime.timezone.utc,
    ).isoformat()


def _iso_or_never(ts: int | None) -> str:
    return _iso(ts)


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _format_approval_line(row: dict, *, age_field: str) -> str:
    """One line for an approval row in the awaiting / expiring sections.

    `age_field` selects whether to render age (since created) or TTL
    (until expiry); both paths reuse this layout for visual consistency.
    """
    args = row.get("args") or {}
    if isinstance(args, dict):
        args_repr = ", ".join(f"{k}={v}" for k, v in args.items())
    else:
        args_repr = str(args)
    when_label = "age" if age_field == "age_seconds" else "expires_in"
    return (
        f"  #{row.get('token', '?'):10s}  "
        f"tier {row.get('tier', '?')}  "
        f"{row.get('verb', '?'):14s}  "
        f"{args_repr[:40]:40s}  "
        f"{when_label}={_format_duration(int(row.get(age_field, 0)))}"
    )


# ---- Synthetic command construction --------------------------------------


def get_status_verb_spec() -> principal_commands.VerbSpec | None:
    """Return the registry entry for the `status` verb, if present.
    Used by the principal handler that dispatches to this module so it
    doesn't have to re-implement registry lookup."""
    for spec in principal_commands.VERB_REGISTRY:
        if spec.name == "status":
            return spec
    return None
