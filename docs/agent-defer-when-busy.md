# Design plan: defer agent dispatch when the system is busy

**Status: SHIPPED 2026-05-08.** All four moving parts landed:
`daemon/system_load.py` (loginctl + load + memavail probes,
fail-open), `[agent.dispatch]` config section in `daemon/config.py`,
`State.mark_deferred / select_deferred_messages / mark_deferred_running`
plumbing reusing `plan_json` (no schema migration), and the
defer + drain wiring in `inbox_watcher._dispatch_agent_request`
+ `_drain_deferred_if_free` (called at the tail of `_idle_once`
after every catchup). 33 new tests (1038 → 1071 passing): 15
system_load, 5 config, 6 state, 7 integration. Defaults are
backward-compatible (no deferral until operator opts in via
`defer_when_gaming_mode = true`).

**Goal:** When the principal is gaming (or the system is otherwise heavily loaded),
defer Opus 4.7 agent dispatch until things calm down, rather than running the agent
in a constrained environment where it might OOM, lag the game, or both.

## Background

### Today's facts
- Nightjar daemon idle: ~0.32% sustained CPU, 62 MB RSS. Invisible.
- Active agent session (`claude -p` Opus 4.7) can use 1-2 GB RAM and a couple of cores
  while a model turn is in flight. Bounded by `DEFAULT_TIMEOUT_SECONDS = 1800`.
- The systemd unit caps the entire daemon cgroup (including agent subprocesses) at
  `MemoryHigh=200MB` / `MemoryMax=500MB`. **An Opus session with a heavy MCP load
  could conceivably trip MemoryMax and get killed.** Hasn't happened yet but the
  margin is thin.
- Steam Deck runs SteamOS with two graphical session modes:
  - **Plasma (KDE x11)** — desktop mode. Active session has `Type=x11`,
    `Desktop=KDE (One-Time Launch)`, `Service=sddm-autologin`.
  - **Gamescope** — gaming mode. Active session has `Type=wayland`,
    `Desktop=gamescope`, leader is the gamescope session unit.
- Switching between them is via `steamos-session-select`. The user@.service stays
  alive; only the graphical session under `session.slice` is replaced.

### What we want
1. **Defer, not degrade.** An agent that runs in a constrained environment is worse
   than one that runs five minutes later when the environment is free. We want the
   agent to run *well*, not *carefully*.
2. **Be honest with the principal.** If we defer, tell them. Not silently — they
   should know their request is queued, not dropped.
3. **Drain automatically.** When the system frees up, the queue runs. No principal
   intervention required.

### What we explicitly do NOT want
- A "be lean" prompt injected into the agent's system prompt. Models can't reliably
  introspect on resource use, and prompt-injection signals are an attack surface.
- A whole-system load watchdog daemon. Too much infrastructure for a question we
  can answer with a single `loginctl` call at dispatch time.
- A configurable "agent budget" knob (Haiku vs Opus, timeout cap) — that's a
  separate design question for another time. This plan is purely about *when* the
  agent runs, not *what* the agent is.

## Detection signal

**Primary:** the active session on `seat0` is of type `wayland` AND its `Desktop`
property contains `gamescope`. This is the unambiguous "user is in gaming mode"
signal. One `loginctl` call, no fragile process matching.

```python
# Pseudocode
def _system_is_busy() -> tuple[bool, str]:
    """Return (is_busy, reason). reason is human-readable for the
    deferred-reply email and for log events.
    """
    active_session_id = _query_loginctl_seat_active("seat0")
    if active_session_id is None:
        return (False, "no active session on seat0")
    session_props = _query_loginctl_session(active_session_id)
    desktop = session_props.get("Desktop", "")
    sess_type = session_props.get("Type", "")
    if "gamescope" in desktop or sess_type == "wayland":
        # Wayland on a Steam Deck is gamescope; KDE on plasma uses x11.
        return (True, "principal is in gaming mode")
    # Optional second signal: load average exceeds 4.0 (full saturation
    # on the 4-core Deck APU). Catches "browsing while compiling" cases
    # but not gaming itself.
    load_1m = _read_loadavg()
    if load_1m > 4.0:
        return (True, f"system load {load_1m:.1f} (>4.0)")
    return (False, f"load {load_1m:.1f}, desktop={desktop}")
```

Implementation notes:
- Use `subprocess.run(["loginctl", ...], timeout=2.0)` — the binary is already
  available on every SteamOS install. Keeps the daemon stdlib-only.
- Parse `loginctl show-session N -p Type -p Desktop -p Class --value` for a
  predictable shape.
- Bound the subprocess call with a short timeout so a hung loginctl can't wedge
  the dispatcher (lesson from this morning's bugs).
- `seat0` is the right seat on a Steam Deck — there's only one seat.

**Threshold tuning** is deferred to first-real-use. Start with the gaming-mode-only
detection. If you find that "browsing while compiling pokeemerald-expansion"
also throttles agent runs into uselessness, add the load-average check. If neither
is enough, add a memory check (`MemAvailable < 2 GB` from `/proc/meminfo`). One
predicate at a time. Keep them OR'd.

## State-machine changes

Today, the message-state lifecycle for an authenticated agent message is:
```
RECEIVED -> AGENT_RUNNING -> RESPONDED  (or ERRORED on failure)
```

New state needed:
```
RECEIVED -> QUEUED_DEFERRED -> AGENT_RUNNING -> RESPONDED
                ^                    |
                +-- (loop on next ---+
                     drain attempt)
```

`QUEUED_DEFERRED` rows are picked up by a new periodic drain task. The drain
checks `_system_is_busy()` again; if still busy, leaves the rows alone. If free,
moves the row to `AGENT_RUNNING` and dispatches.

Schema impact:
- `messages.state` already has free-form values. Add `QUEUED_DEFERRED` to
  whatever enum-like check exists (audit `state.py` and `state_transitions`).
- Optional: add a `deferred_at` column or store an ISO timestamp in the existing
  `plan_json` (which is already opaque JSON). Avoid schema migration if possible.
- Add a `transitions` row for each defer / re-dispatch — required by the existing
  audit pattern.

## Drain mechanism

Two reasonable shapes:

### Option A: piggyback on the catchup poll (recommended)

The watcher's IDLE loop already wakes every `poll_interval_seconds` (60s default)
to do catchup. After catchup completes, drain `QUEUED_DEFERRED` if `_system_is_busy()`
returns false. This adds zero new tasks and zero new wakeups.

```python
# In _idle_once, AFTER _catch_up returns successfully:
await self._drain_deferred_if_free()
```

Drawbacks:
- Drain frequency is bounded by `poll_interval_seconds`. A 60s wait between
  switching out of gaming mode and the queued message running is fine UX. Not
  instant, but honest.
- If the watcher is wedged for any reason, the queue doesn't drain. Acceptable —
  if the watcher is wedged we have bigger problems.

### Option B: dedicated drain task

Spawn a new asyncio task in `daemon.main._main_async` alongside the heartbeat task,
firing every 30s. Independent of the watcher state.

Drawbacks:
- One more task to reason about.
- Two parallel paths into `_dispatch_agent_request` — race condition risk if a
  message could be picked up by both the watcher (on next inbound mail) and the
  drain task. Need a transactional state check.

**Recommendation: Option A.** Same complexity envelope as today, no new task lifecycle.

## Notification UX

When the daemon defers, the principal should know. Three choices:

### Option 1: silent defer (no reply)
No email. Principal sees their message land in their Sent folder, gets nothing
back, and waits. When the agent runs (later), they get the actual reply.

**Pros:** zero noise. Matches the "agent will reply when done" model.
**Cons:** principal can't tell "deferred" from "wedged for the fifth time".
After today's incidents, that ambiguity is exactly what we should avoid.

### Option 2: deterministic "queued" reply (recommended)
Send a brief one-shot email immediately:
> Nightjar: queued. Your request is held while the system is busy
> (gaming mode active). I'll run it when free, usually within a minute
> of switching back. No action needed.

**Pros:** principal has clear ground truth. Distinguishes deferral from failure.
**Cons:** mild email noise. Mitigated by the principal being the same person who
just sent the inbound — they're reading their inbox anyway.

### Option 3: deterministic "queued" reply, but only on first defer
Send the queued-reply email the first time a request gets deferred. Subsequent
defers (e.g. a continuation message arriving while still gaming) chain to the
existing thread silently.

**Pros:** quieter than Option 2 if the principal sends multiple messages while
gaming.
**Cons:** more state to track. Probably not worth the complexity over Option 2.

**Recommendation: Option 2.** Honesty + simplicity. Email volume is bounded by
how often the principal sends mail while gaming, which is rare in practice.

## Putting it together

### Files to touch

| File | Change |
|---|---|
| `daemon/inbox_watcher.py` | New `_system_is_busy()` helper. Modify `_dispatch_agent_request` to consult it before `principal_agent.execute`. Add `_drain_deferred_if_free()` called from `_idle_once`. Send queued-reply via existing `_send_deterministic_reply`. |
| `daemon/state.py` | Add `QUEUED_DEFERRED` to the state enum or accept it via the existing free-form column. Add `select_deferred_messages()` + `mark_deferred()` + `mark_deferred_running()` accessors. |
| `daemon/config.py` | New `[agent.dispatch]` section: `defer_when_gaming_mode = true`, `defer_when_load_above = 0` (0 = disabled), `defer_when_memavail_below_mb = 0`. |
| `tests/test_inbox_watcher.py` (or new `test_agent_defer.py`) | (1) `_system_is_busy` returns True when fake loginctl reports gamescope, False otherwise. (2) `_dispatch_agent_request` calls execute() when not busy, sends queued reply and persists state when busy. (3) Drain re-dispatches when busy → free transition fires. (4) Queued reply only fires once per message. |
| `daemon/main.py` | No changes — the watcher's IDLE loop is the natural drain point. |

### Roughly 100-150 lines of code + 4-5 tests. Same scale as today's patches.

### Estimated session cost

- One half-session if everything goes smoothly.
- One full session if the state-machine plumbing surfaces edge cases.

Don't try to do this and the orphan-claude-detection (deferred-work #12) in the
same session. Both are state-machine changes touching `agent_sessions` /
`messages` tables; doing them serially is much easier to reason about.

## Edge cases worth thinking about before coding

1. **A message is QUEUED_DEFERRED and the daemon restarts.** The drain logic on
   next startup must pick up these rows. Easy — `_drain_deferred_if_free()` runs
   on every catchup completion, and catchup runs on startup.

2. **A continuation message arrives while its session is QUEUED_DEFERRED.**
   The continuation's session_id doesn't yet have an `agent_session` row (the
   agent never ran). The classifier will treat it as "init" not "continuation."
   That's *probably* fine — the principal sees two emails treated as separate
   asks. But worth deciding intentionally. Recommendation: queue the continuation
   too, in the same QUEUED_DEFERRED bucket. When the drain runs them, run the
   first as init and the second+ as continuations. Order them by `received_at`.

3. **Defer + DMS interaction.** If the dead-man's-switch trips while messages
   are deferred, what should happen? Recommendation: leave them deferred. DMS
   recovery will revive the daemon; the queue drains naturally. Don't try to
   notify the principal about deferred messages on DMS recovery — too much state
   for too little gain.

4. **Defer + cost cap.** Today's `cost_guard.py` checks the per-hour invocation
   cap before dispatch. If a deferred message tries to drain into a cost-capped
   slot, it should re-defer (or fall back to "cost cap" reply per existing logic).
   Verify the existing cap path handles a re-entrant call cleanly.

5. **What if the principal is in gaming mode for hours?** The queue grows.
   That's actually fine — `messages` is SQLite, can hold thousands. The drain
   will eventually run them. But: if a deferred message has been queued for >24h,
   it's probably stale and the principal would rather just be told.
   Recommendation: add a hard max-defer-age (12h?) after which the queued
   message becomes a deterministic-failure reply: "I held this for 12 hours but
   the system has been busy the whole time. If you still want me to act on it,
   please resend." Out of scope for v1; capture as future work.

## Implementation order (single session)

1. **Verify the loginctl detection actually works on this machine.** Spend 5
   minutes empirically: run `loginctl show-session $(loginctl show-seat seat0
   -p ActiveSession --value) -p Type -p Desktop -p Class --value` in plasma,
   record output. If you can convince me the user can switch this once, do
   the same in gaming mode, switch back. Now you know the truth values.
2. **Write `_system_is_busy()` + unit test.** Mock the loginctl subprocess.
   Test gamescope-busy, plasma-not-busy, no-active-session-not-busy, loginctl-
   timeout-not-busy (fail-open: if we can't tell, assume not busy).
3. **State-machine plumbing.** Add `QUEUED_DEFERRED` accessors to `state.py`.
   Test in isolation.
4. **Wire `_dispatch_agent_request` to consult busy + persist deferral.**
   Plus the queued-reply email.
5. **Add `_drain_deferred_if_free()` to `_idle_once`.**
6. **Integration test.** Fake loginctl returning busy → message gets deferred,
   email sent. Switch fake loginctl to free → next IDLE poll drains it,
   `principal_agent.execute` gets called.
7. **Run full suite, verify zero regressions.**
8. **Hold restart for review** (same protocol as today's two patches).

## What this does NOT solve

- The actual cgroup memory cap. If you're worried about that hitting during
  an active agent (rare but possible), the separate fix is to bump
  `MemoryMax=2G` in the systemd unit. Not part of this plan.
- Adapter-level resource control (forcing Haiku instead of Opus when busy).
  Different design conversation.
- Detecting "system is about to be busy" — only "is busy now". A game launching
  during an active agent session won't pre-empt the agent. That's fine: the
  agent has a bounded timeout and the cgroup is its hard ceiling.
- Notifying the principal at the *start* of each gaming session ("by the way,
  Nightjar is now in deferred mode"). Too noisy.

## Cross-references

- **`incidents/silent-wedge-2026-05-07T18-43.md`** — informs the
  fail-open-on-timeout discipline for `_system_is_busy()`.
- **`incidents/silent-wedge-2026-05-07T21-37.md`** — informs why we don't add
  a "be lean" system-prompt injection (system prompt is a security boundary).
- **`~/.claude/projects/-home-deck/memory/deferred-work.md` #12** —
  orphan-claude detection. Touches the same tables. Do this plan first; the
  orphan-detection plan can layer on top.
