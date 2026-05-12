"""Principal-agent executor — the `do` verb's heart.

Spawns `claude -p` with the principal's free-form request, full agentic
loop, no sandbox. Streams the event stream to a per-session JSONL audit
log as events arrive (so a kill mid-run preserves what happened).
Returns a final-text result the daemon emails back to the principal.

Threat model boundary: the principal is FULLY TRUSTED in this module.
TOTP/HOTP authentication happens upstream in the verb dispatcher. By
the time execute() is called, the daemon has already concluded the
request really came from the principal. Inside execute, the agent has
the same surface the principal would have at the keyboard:
- `claude -p --permission-mode bypassPermissions` runs without prompts
- working directory is the principal's home
- all MCP servers and tools the logged-in account has are available
- network egress is unrestricted (the agent can hit Gmail, web APIs, etc.)

What this module DOES NOT route: outbound mail to *contacts* via the
daemon's SMTP. The agent can email contacts only through whatever
Gmail/MCP tooling it has access to in its own session — not through
nightjar's reply pipeline. The daemon's SMTP from this path goes only
to the principal. This preserves the contact-side scope gating; the
agent literally cannot use the daemon to reach contacts.

Cancellation: each running subprocess registers itself with the
daemon's stop event. Panic / SIGTERM / DMS triggers a SIGTERM on the
subprocess, which propagates to the agent. The audit log records the
kill event before the process exits.

See `~/.claude/projects/-home-deck/memory/project-nightjar-cc-executor-shipped.md`
for the parent ClaudeClient framework, and the `do` verb section of
the principal-commands docs for how this gets invoked.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Default model for the agent. Opus is the right call here — the
# principal asks for arbitrary work, latency is rarely critical (it's
# email), and the cost is subscription-bounded.
DEFAULT_AGENT_MODEL = "claude-opus-4-7"

# Wall-clock cap. Without one, a misbehaving agent could hang forever.
# Half an hour is generous for any reasonable single-turn agent run.
# Continuations get a fresh budget per message.
DEFAULT_TIMEOUT_SECONDS = 1800

# Permission mode. The principal is fully trusted; there's no human
# at the keyboard to approve prompts mid-run. bypassPermissions skips
# all prompts.
DEFAULT_PERMISSION_MODE = "bypassPermissions"

# StreamReader chunk limit for claude -p's stdout.
# `--include-partial-messages --output-format stream-json --verbose`
# emits per-line JSON frames that can carry an entire model token
# stream (assistant deltas, large tool inputs, base64 image fragments).
# asyncio's default StreamReader limit is 64 KiB — a single long
# assistant frame routinely exceeds that and surfaces as
# "Separator is found, but chunk is longer than limit".
# When that happens our stream task dies, the executor loses sight
# of the agent, and the subprocess keeps running unsupervised
# (silent-wedge incident 2026-05-07T21-37). 16 MiB is well above
# any realistic single frame and stays inside RSS budget.
SUBPROCESS_STDOUT_LIMIT = 16 * 1024 * 1024

# How long to wait for SIGTERM to take before escalating to SIGKILL.
# Matches the existing 5s used in the cancel/timeout branches.
KILL_GRACE_SECONDS = 5.0


class PrincipalAgentError(Exception):
    """Raised on subprocess failure (non-zero exit, malformed output,
    audit log write failure). Caller is expected to convert this into
    an `errored` agent_session row plus an outbound reply explaining
    the failure."""


@dataclass(frozen=True)
class AgentAttachment:
    """A file the agent wants delivered alongside its reply.

    Populated by `attach_to_reply` MCP tool calls during the agent's
    turn. The daemon reads the per-session attachments JSONL after
    the subprocess exits, builds AgentAttachment instances from the
    entries, and threads them into the SMTP send via
    `notifier.notify_principal(attachments=...)`.

    `path` is absolute and must exist + be readable at send time.
    `filename` defaults to `path.name` when None. `maintype`/`subtype`
    default to `mimetypes.guess_type(path)` when both None, falling
    back to application/octet-stream if the guess fails — every mail
    client handles that fine.
    """
    path: Path
    filename: str | None = None
    maintype: str | None = None
    subtype: str | None = None


@dataclass(frozen=True)
class AgentResult:
    """Returned by execute() on every code path — completed, killed, or
    errored. The daemon decides how to convert this into an outbound
    reply. final_text is what the agent wrote in its last assistant
    text block; empty string when no text was emitted (e.g. killed
    before first reply).

    composed_body / composed_subject come from the compose_reply MCP
    tool. When set, the daemon prefers them over final_text on the
    completed path. The killed/errored paths fall back to final_text
    regardless — a partial run hasn't earned the right to claim "this
    is the final reply." None means the agent didn't call the tool;
    the daemon falls back to final_text and emits a missing-event
    in the audit log so adoption is observable.
    """
    session_id: str
    status: str  # "completed" | "killed" | "errored"
    final_text: str
    audit_log_path: Path
    started_at: int
    completed_at: int
    error_detail: str = ""
    composed_body: str | None = None
    composed_subject: str | None = None
    attachments: tuple["AgentAttachment", ...] = ()


# The bootstrap CLAUDE.md the daemon seeds into agent_cwd if no
# CLAUDE.md is already present. Idempotent: never overwrites — the
# agent is encouraged to edit and personalise it. Bootstrap-chain
# shaped: the agent should be able to start from "I need to do X" and
# follow a logical chain to working primitives. Goes hand in hand with
# the system prompt; the prompt names the files, this CLAUDE.md walks
# the agent through *using* them.
_AGENT_CLAUDE_MD_BOOTSTRAP = """\
# Nightjar agent workspace

This is your working directory. You can write files here freely —
notes, scratch state, per-contact memory, anything you want to
persist across turns beyond what `--resume` gives you. Nightjar
will not delete or overwrite anything you put here. Organise it
however serves you best.

This file (CLAUDE.md) is yours to edit. Add what's useful, prune
what's not. The starter content below is a bootstrap chain —
follow the threads when you need them.

## Identity and posture

You are Nightjar, the principal's personal email-summoned agent.
Read your real system prompt for the canonical voice and posture.
This file documents the *machine* you're running on; the system
prompt documents *who you are on it*.

## "How do I..." — bootstrap chains

### ...run a one-off action against the principal's mailbox without touching the daemon?

The most common shape: read mail directly via IMAP, do something
to it, optionally send a result via SMTP. The daemon doesn't see
any of this; it's a side channel for diagnostics, exploration,
or tasks the daemon's pipeline doesn't cover.

The three primitives below are in execution order — read them in
sequence the first time, then jump to the one you need.

1. **Decrypt secrets.** `from daemon.secret_box import read_secrets_file`,
   call with a `Path`. Returns a dict; `secrets["imap.nightjar"]["password"]`
   and `secrets["smtp"]["password"]` are what you typically want.
   (Detail below: "...decrypt the IMAP password.")

2. **Connect IMAP.** `aioimaplib.IMAP4_SSL(host="imap.gmail.com", port=993,
   timeout=30)`, then `wait_hello_from_server`, `login`, `select`.
   Use `BODY.PEEK[...]` for read-only fetches that don't flip
   `\\Seen` flags. (Detail below: "...read a prior email body.")

3. **Send SMTP** (only if you need to). `smtplib.SMTP("smtp.gmail.com",
   587)` + `starttls()` + `login(...)`. Build an
   `email.message.EmailMessage` with `From`/`To`/`Subject`/`Message-ID`/
   `In-Reply-To`/`References` set explicitly. (Detail below:
   "...send mail from a non-Nightjar address.")

Always use `BODY.PEEK[...]` for read-only fetches. Plain
`BODY[...]` flips `\\Seen` and disturbs the principal's mailbox.

Use `timeout=30` on aioimaplib.IMAP4_SSL — the library's 10s
default is too tight against Gmail's tail latency on busy nights
(silent-wedge incident #7, 2026-05-08).

### ...read a prior email body the principal is referencing?

1. The state DB at `~/.local/share/nightjar/state.db` has every
   message Nightjar has handled, BUT only metadata for inbounds
   (no body). Bodies are NOT persisted there.
2. Outbound replies (Nightjar's own sent mail) DO have full bodies
   in `outbound_log.body`. Use this for "what did I tell them last
   time."
3. For inbound bodies (or anything older than Nightjar): connect
   to IMAP. Gmail at imap.gmail.com:993, principal's mailbox.
   Decryption pattern below.
4. The mailbox is `eagre.nightjar@gmail.com` per nightjar.conf.
   Search by subject or by Message-ID; Gmail's HEADER search by
   Message-ID is unreliable, so prefer subject or fetch-by-UID.

### ...send an attachment to the principal alongside your reply?

Use the `attach_to_reply` MCP tool. Each call adds one file to
the outbound reply Nightjar will send. The daemon collects all
calls during the turn and threads them into the SMTP send
alongside your `compose_reply` body.

```
attach_to_reply(path="/absolute/path/to/file", filename="optional-display-name")
```

Rules enforced at tool-call time:
- Path must be absolute and must exist as a regular readable file.
- 18 MiB hard cap per file (Gmail's 25 MiB wire limit after
  base64 expansion). Soft warn at 10 MiB — gzip or split before
  stacking more.
- Multiple calls accumulate within a turn; order is preserved.
- Per-turn slate: each turn starts fresh. An attach in turn N does
  NOT carry into turn N+1; re-attach explicitly if needed. Same
  for compose_reply — every turn must call it again.
- Completed-path-only: if the run errors or is killed before
  finishing, attachments are NOT sent.

### ...convert a markdown file before attaching it to the principal?

Use the `render_markdown` MCP tool when the principal will be
reading on a phone and the source is `.md`. Mobile mail clients do
not render markdown (raw `#` and `-` arrive as literal characters);
HTML renders inline.

```
render_markdown(input_path="/abs/path/in.md", format="html")
  -> returns "/tmp/nightjar-render-<uuid>.html"
```

Then pass the returned path to `attach_to_reply`. Do NOT call this
for every attachment — only when format conversion is genuinely
useful. If the principal asked for the raw `.md`, attach the raw
`.md`.

Formats:
- `"html"` — recommended for phone reading. Standalone document
  with inline CSS sized for mobile.
- `"text"` — strip markdown syntax to plain prose. Rarely useful;
  fallback for clients that mangle HTML.
- `"pdf"` — pure-Python rendering via `inkmd`. No system binary
  required; produces deterministic, kerned PDFs. Good for things
  the principal might want to print, archive, or hash.

### ...send mail from a non-Nightjar address?

The daemon will only send to the principal. Anything else, you
do directly via SMTP.

1. Read `~/.config/nightjar/test_creds.toml` (plaintext TOML).
   It has named entries for each available sender account.
2. Pick the appropriate one (eagre.claude is the agent-presence
   one for testing; eagre.dev and araziah.music are also available
   for end-to-end exercises).
3. Build an `email.message.EmailMessage`, send via
   `smtplib.SMTP_SSL('smtp.gmail.com', 465)` with login/password
   from the TOML. STARTTLS on 587 also works.

### ...decrypt the IMAP password (or any other secret)?

Secrets live in `~/.config/nightjar/secrets.toml` encrypted by the
machine_id of this device. Code:

    from daemon.secret_box import read_secrets_file
    from pathlib import Path
    secrets = read_secrets_file(
        Path("~/.config/nightjar/secrets.toml").expanduser()
    )
    imap_pwd = secrets["imap.nightjar"]["password"]
    smtp_pwd = secrets["smtp"]["password"]

`read_secrets_file` takes a `Path`, not a `str`. The decrypt is
side-effect-free; you can call it as often as you like.

### ...look at what Nightjar has been doing recently?

- Live event stream: `~/nightjar/logs/nightjar-YYYY-MM-DD.jsonl`,
  one JSONL line per event. Most recent activity at the bottom.
- Per-session agent traces: `~/.local/share/nightjar/agent-audit/`
  (one .jsonl per session, includes every tool use you made).
- State at a glance:
  `sqlite3 ~/.local/share/nightjar/state.db ".tables"` and explore.
  Useful tables: `messages`, `outbound_log`, `agent_sessions`,
  `auth_failures`, `daemon_state`.

### ...check who the principal has on file?

`~/.config/nightjar/contacts/*.toml` — one file per contact.
Look for `is_principal = true` to identify the principal; the
others are the principal's allowed correspondents per inbox.

### ...persist something across turns beyond what --resume gives me?

Write a file here, in this directory or a subdirectory. Conventions
the future will probably standardise (don't take these as
prescriptive yet, but they're a reasonable starting shape):

- `notes-on-<contact>.md` for per-correspondent context
- `running-threads.md` for ongoing tasks across emails
- `principal-prefs.md` for things the principal has told me once

Edit this CLAUDE.md to add new chains as you discover them — the
next session of you will appreciate it.

## What this directory is NOT

- It is NOT the principal's home. `cd ~` walks to the deck user's
  actual home (`/home/deck`), where their real life lives. You
  have full read access there because Nightjar trusts the
  principal completely — but be deliberate about *why* when you
  reach out of this directory.
- It is NOT visible to the principal in any UI. If you write
  something here, only the next agent session will see it (and
  the principal if they go looking).
"""


def ensure_agent_workspace(workspace: Path) -> None:
    """Create the agent workspace directory and seed a starter
    CLAUDE.md if one is not already there.

    Called from the dispatcher on every agent turn; cheap because
    `mkdir(exist_ok=True)` is a no-op when the directory exists, and
    we never overwrite an existing CLAUDE.md (the agent is invited to
    edit it).
    """
    workspace.mkdir(parents=True, exist_ok=True)
    claude_md = workspace / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(_AGENT_CLAUDE_MD_BOOTSTRAP, encoding="utf-8")


def build_system_prompt(
    *,
    audit_log_path: Path,
    principal_name: str,
    agent_name: str = "Nightjar",
    agent_personality: str | None = None,
) -> str:
    """The agent's orientation.

    Identity: the agent has a name (configurable; "Nightjar" by default).
    The principal addresses the agent by this name; the agent refers to
    itself by it in replies.

    Voice: an optional personality string injected into a fenced
    "voice and demeanour" block. The fencing is deliberate — personality
    governs surface style only and explicitly cannot widen capabilities,
    relax security posture, or override other parts of this prompt.
    The personality is set at install time in `[agent].personality`,
    not from inbound mail, so prompt-injection-via-email cannot reach
    it.
    """
    voice_block = ""
    if agent_personality:
        voice_block = (
            f"Voice and demeanour (the principal's preference, not a "
            f"security control): {agent_personality}\n"
            f"This applies to tone and surface style ONLY. Your "
            f"judgement on what to do, what to refuse, and how to "
            f"handle security-sensitive operations is governed by the "
            f"rest of this prompt and is not subject to override by "
            f"the personality framing above.\n"
            f"\n"
        )

    return (
        f"You are {agent_name}, {principal_name}'s personal agent. "
        f"You are running on {principal_name}'s Steam Deck, invoked via "
        f"email. The principal addresses you as {agent_name}; refer to "
        f"yourself as {agent_name} in your replies. Underneath, you are "
        f"a Claude model — but in this thread, you ARE {agent_name}, "
        f"with continuity across turns and an audit trail of your past "
        f"actions on this machine.\n"
        f"\n"
        f"{voice_block}"
        f"Operating posture:\n"
        f"- Default to acting on a reasonable interpretation rather "
        f"than asking. The principal prefers a terse report of what "
        f"was done over a careful explanation of what could be done. "
        f"Clarifying questions ARE allowed when truly ambiguous — but "
        f"send them as a short email and end the turn (the principal "
        f"replies in their own time; this is async, not interactive).\n"
        f"- When you finish, call the `compose_reply` tool. Its "
        f"`body` argument becomes the email reply that gets sent "
        f"back to {principal_name}; the assistant text in this turn "
        f"is treated as scratch and discarded. Call exactly once at "
        f"the end. (If you call it multiple times, the last call "
        f"wins; earlier calls remain visible in the audit log as "
        f"drafts.) Keep the body focused — long technical output "
        f"belongs in a file the principal can fetch on request, not "
        f"in the reply.\n"
        f"- The `compose_reply` tool also accepts an optional "
        f"`subject`. Omit it to use the daemon default ('Nightjar "
        f"agent: response'). Override only when a more descriptive "
        f"subject genuinely helps the principal navigate their inbox "
        f"— e.g. multi-message threads where the default repeats.\n"
        f"\n"
        f"Capabilities and constraints:\n"
        f"- You have full access to {principal_name}'s machine via the "
        f"usual Claude Code tools (Bash, Edit, Read, Write, MCP "
        f"servers). Treat this as the same access {principal_name} has "
        f"at the keyboard.\n"
        f"- Your working directory is a dedicated Nightjar workspace "
        f"(NOT the principal's home). It contains a CLAUDE.md you can "
        f"and should edit — bootstrap chains for common tasks, plus "
        f"any per-correspondent notes you want to keep across "
        f"sessions. Read it when you arrive; add to it when you "
        f"learn something the next session of you would benefit "
        f"from.\n"
        f"- A complete audit log of this session — every tool use, "
        f"every file touched, every command run — is being written to "
        f"{audit_log_path}. If asked for the log, you may attach or "
        f"summarise it.\n"
        f"- The daemon does NOT route mail from you to anyone other "
        f"than the principal. If a task requires emailing a contact, "
        f"do it directly via SMTP (creds in `~/.config/nightjar/"
        f"test_creds.toml`, plaintext, keyed by purpose — separate "
        f"sender accounts live there for non-principal mail) or via "
        f"Gmail tooling in your own session.\n"
        f"\n"
        f"Prior email context — where it lives:\n"
        f"- State DB: ~/.local/share/nightjar/state.db (SQLite). Tables: "
        f"`messages` (inbound METADATA only — bodies are NOT persisted "
        f"here; if you need an inbound body, pull it from IMAP), "
        f"`outbound_log` (every reply Nightjar has sent, with full "
        f"body), `agent_sessions` (prior agent turns + session_ids).\n"
        f"- Daemon JSONL logs: ~/nightjar/logs/nightjar-YYYY-MM-DD.jsonl "
        f"(per-day event stream — triage decisions, reply Message-IDs, "
        f"state transitions; not full bodies).\n"
        f"- Past agent audit logs: ~/.local/share/nightjar/agent-audit/ "
        f"(one .jsonl per session, full event-by-event trace).\n"
        f"- Live IMAP for inbound bodies, or any mail predating Nightjar: "
        f"imap.gmail.com:993, password in ~/.config/nightjar/secrets.toml "
        f"(encrypted with this machine's machine_id; decrypt via "
        f"`from daemon.secret_box import read_secrets_file` and call it "
        f"with a `Path` — `read_secrets_file(Path('~/.config/nightjar/"
        f"secrets.toml').expanduser())`).\n"
        f"  Reach for prior context when the task plausibly needs it — "
        f"e.g. 'what did I tell Fraser last week', 'find the invoice "
        f"from X', 'continue the thread about Y'. Don't fabricate; "
        f"query.\n"
    )


def _audit_log_path(session_id: str, *, audit_dir: Path) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir / f"{session_id}.jsonl"


def _compose_reply_log_path(session_id: str, *, audit_dir: Path) -> Path:
    """Per-session JSONL the compose_reply MCP server appends to.

    Sits next to the audit log in the same directory. The filename
    is suffixed `.compose-reply.jsonl` to keep it grep-distinct.
    """
    return audit_dir / f"{session_id}.compose-reply.jsonl"


def _read_compose_reply_log(
    path: Path,
) -> tuple[str | None, str | None]:
    """Read the compose-reply JSONL and return (body, subject) of
    the last *valid* call.

    Tolerant: skips blank lines, unparseable lines (a SIGKILL between
    write boundaries can leave a half-line), entries without a string
    `body`, and entries whose body is empty after stripping. Returns
    (None, None) if the file doesn't exist or no valid entry exists.

    Empty-body guard: `body.strip() == ""` is treated as no call,
    matching decision #7 of the implementation plan. The daemon's
    fallback to final_text + missing-event fires in that case.
    """
    if not path.exists():
        return None, None
    last_body: str | None = None
    last_subject: str | None = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # Tolerate a trailing partial line from a mid-write
            # SIGKILL. Keep walking; if a later line parses, it
            # supersedes anything before.
            continue
        if not isinstance(entry, dict):
            continue
        body = entry.get("body")
        subject = entry.get("subject")
        if not isinstance(body, str) or not body.strip():
            # Empty-body guard. An agent that calls compose_reply()
            # with body="" is treated as not having called at all.
            continue
        last_body = body
        last_subject = subject if isinstance(subject, str) else None
    return last_body, last_subject


def _attachments_log_path(session_id: str, *, audit_dir: Path) -> Path:
    """Per-session JSONL the attach_to_reply MCP tool appends to.

    Sits next to the audit log + compose-reply log. One JSONL line
    per `attach_to_reply` call.
    """
    return audit_dir / f"{session_id}.attachments.jsonl"


def _read_attachments_log(path: Path) -> tuple["AgentAttachment", ...]:
    """Read the attachments JSONL and return AgentAttachment instances
    in call order.

    Tolerant: skips blank lines, unparseable lines (mid-write SIGKILL),
    entries without a string `path`. Returns an empty tuple if the
    file doesn't exist or no valid entries exist.

    The MCP tool already validates path existence + size at call
    time, so the daemon mostly trusts what it reads back. Files may
    still go missing between tool call and SMTP send; the notifier
    surfaces those failures naturally.
    """
    if not path.exists():
        return ()
    attachments: list[AgentAttachment] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        p = entry.get("path")
        if not isinstance(p, str) or not p:
            continue
        filename = entry.get("filename")
        attachments.append(AgentAttachment(
            path=Path(p),
            filename=filename if isinstance(filename, str) else None,
        ))
    return tuple(attachments)


async def _kill_and_reap_group(
    proc: asyncio.subprocess.Process,
    *,
    grace_seconds: float = KILL_GRACE_SECONDS,
) -> None:
    """Send SIGTERM to claude's process group, wait up to `grace_seconds`
    for the parent to exit, then escalate to SIGKILL on the group.

    Used by every cleanup path in `execute` (cancel, timeout, stop_event,
    and stream-error) so the subprocess and ALL its descendants die
    together. Without process-group signalling, a tool subprocess that
    set its own session would survive even SIGKILL on the parent. With
    `start_new_session=True` at spawn time, `proc.pid` is the PGID of
    claude's group, so killpg(pgid, ...) reaches every descendant.

    Idempotent on a process that has already exited (ProcessLookupError
    from killpg is swallowed). Always awaits proc.wait() so the asyncio
    Process state is reaped — never leaks a zombie.
    """
    pgid = proc.pid  # we spawned with start_new_session=True
    # SIGTERM the whole group.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # Already dead, or the kernel rejected the signal because the
        # group is gone. Either way, fall through to wait().
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass
    # Didn't honour SIGTERM in time. SIGKILL the group.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    # SIGKILL is uninterruptible from kernel side, so wait() will
    # complete promptly. Bound it anyway in case proc is in
    # uninterruptible sleep on a stuck filesystem.
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        # Last resort: leave proc.wait() pending; the asyncio process
        # transport will reap on shutdown. We've done all we can here.
        pass


async def _stream_events_to_audit(
    proc: asyncio.subprocess.Process,
    audit_path: Path,
) -> tuple[str, list[dict[str, Any]]]:
    """Read stdout line-by-line (the CLI emits stream-json: one JSON
    object per line). Append each event to the audit log as it arrives,
    so a mid-run kill leaves a useful log behind. Return the final
    text block from the last assistant message + the captured events."""
    if proc.stdout is None:
        raise PrincipalAgentError("subprocess has no stdout pipe")

    final_text = ""
    captured: list[dict[str, Any]] = []
    with audit_path.open("a", encoding="utf-8") as fh:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            if not text.strip():
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                # Malformed line — record the raw line so a postmortem
                # can see what came back, but don't crash the whole run.
                fh.write(json.dumps({
                    "type": "_audit_unparseable",
                    "raw": text[:2000],
                    "ts": int(time.time()),
                }) + "\n")
                fh.flush()
                continue
            fh.write(json.dumps(event) + "\n")
            fh.flush()
            captured.append(event)
            # Track the most recent assistant text block. The CLI's
            # stream-json emits assistant turns as either single
            # message events or streamed chunks; we look at completed
            # message events which have a content array.
            if event.get("type") == "assistant":
                msg = event.get("message", {}) or {}
                for block in msg.get("content", []) or []:
                    if block.get("type") == "text":
                        text_value = block.get("text")
                        if isinstance(text_value, str) and text_value.strip():
                            final_text = text_value
    return final_text, captured


async def execute(
    *,
    request_body: str,
    principal_name: str,
    audit_dir: Path,
    cwd: Path,
    session_id: str | None = None,
    model: str = DEFAULT_AGENT_MODEL,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    stop_event: asyncio.Event | None = None,
    executable: str = "claude",
    agent_name: str = "Nightjar",
    agent_personality: str | None = None,
) -> AgentResult:
    """Run one agent turn.

    Args:
        request_body: The principal's request, with HOTP code line(s)
            already stripped by the caller.
        principal_name: Used in the system prompt only.
        audit_dir: Directory for per-session audit logs. Created if
            missing.
        cwd: Working directory for the subprocess. Conventionally the
            principal's home; restrict only if you want to.
        session_id: None for a fresh session (a new UUID is generated).
            A string for continuation (passed as `--resume`); the agent
            picks up where the prior turn left off.
        stop_event: Optional asyncio Event. If it sets while the
            subprocess is running, the subprocess gets SIGTERM and the
            session is recorded as killed.

    Returns:
        AgentResult — always, even on failure. Caller inspects
        `.status` to decide how to relay.
    """
    is_continuation = session_id is not None
    if not is_continuation:
        session_id = str(uuid.uuid4())
    assert session_id is not None  # for the type checker

    audit_path = _audit_log_path(session_id, audit_dir=audit_dir)
    compose_reply_log_path = _compose_reply_log_path(
        session_id, audit_dir=audit_dir,
    )
    attachments_log_path = _attachments_log_path(
        session_id, audit_dir=audit_dir,
    )
    # Both logs are session-scoped on disk, but their semantic is
    # turn-scoped: each turn's compose_reply / attach_to_reply calls
    # are what the daemon should send. Truncate at the top of every
    # turn (including continuations) so a prior turn's calls don't
    # silently re-fire. The session-wide audit log next door
    # preserves the forensic record. See
    # `~/.local/share/nightjar/agent-workspace/proposals/attachments-log-per-turn-bug.md`.
    audit_dir.mkdir(parents=True, exist_ok=True)
    for _turn_log in (compose_reply_log_path, attachments_log_path):
        try:
            _turn_log.unlink()
        except FileNotFoundError:
            pass
    started_at = int(time.time())

    system_prompt = build_system_prompt(
        audit_log_path=audit_path,
        principal_name=principal_name,
        agent_name=agent_name,
        agent_personality=agent_personality,
    )

    # Spawn the compose_reply MCP server alongside claude. The server
    # is a stdio child of claude (claude reads --mcp-config and starts
    # it during its MCP client handshake); it appends one JSONL line
    # per `compose_reply` call to compose_reply_log_path. The daemon
    # reads that file after claude exits and prefers its body+subject
    # over the legacy "last assistant text block" reply.
    mcp_script = Path(__file__).parent / "compose_reply_mcp.py"
    render_script = Path(__file__).parent / "render_markdown_mcp.py"
    mcp_config = json.dumps({
        "mcpServers": {
            "nightjar-reply": {
                "type": "stdio",
                "command": "python3",
                "args": [str(mcp_script)],
                "env": {
                    "NIGHTJAR_COMPOSE_REPLY_LOG": str(compose_reply_log_path),
                    "NIGHTJAR_ATTACHMENTS_LOG": str(attachments_log_path),
                },
            },
            "nightjar-render": {
                "type": "stdio",
                "command": "python3",
                "args": [str(render_script)],
            },
        },
    })

    cmd: list[str] = [
        executable, "-p",
        "--system-prompt", system_prompt,
        "--output-format", "stream-json",
        "--include-partial-messages",  # required for stream-json with -p? no — but harmless and gives chunked text events
        "--verbose",  # stream-json requires verbose mode for full event surface
        "--model", model,
        "--permission-mode", permission_mode,
        "--mcp-config", mcp_config,
    ]
    if is_continuation:
        cmd += ["--resume", session_id]
    else:
        cmd += ["--session-id", session_id]

    # Write a session-start marker into the audit log before spawning,
    # so even if the spawn itself fails we have a record. `request_body`
    # is the principal's request after agent_router stripped HOTP
    # codes, so it's safe to record verbatim — without it, a spawn that
    # errors before the first tool-use event leaves the audit log
    # blind on the input side and forensics have to re-fetch from IMAP.
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "_audit_session_start",
            "session_id": session_id,
            "is_continuation": is_continuation,
            "principal_name": principal_name,
            "started_at": started_at,
            "model": model,
            "cwd": str(cwd),
            "request_body": request_body,
        }) + "\n")

    # Disable the claude harness's TodoWrite/Task subsystem. Nightjar's
    # email tasks are 2-3 tool calls and never benefit from a todo list,
    # but the harness fires <system-reminder> nudges every ~2-3 calls
    # ("the task tools haven't been used recently...") with no per-message
    # opt-out. The reminders are harness-injected meta-content the
    # operator did not author, and have been documented to degrade
    # response quality (claude-code issues #40573, #41091, #40176).
    # The only documented escape hatch is killing the entire Task
    # subsystem via this env var (issue #26038). We're happy to.
    spawn_env = dict(os.environ)
    spawn_env["CLAUDE_CODE_ENABLE_TASKS"] = "false"

    try:
        # `limit` raises the per-line buffer ceiling for stdout/stderr's
        # StreamReader so a long stream-json frame doesn't kill the
        # reader (Bug Y, silent-wedge incident 2026-05-07T21-37).
        # `start_new_session=True` puts claude in its own session/PGID;
        # combined with os.killpg in the cleanup paths, this guarantees
        # claude AND any subprocesses it spawned die together even if
        # claude's own SIGTERM handler misfires.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=spawn_env,
            limit=SUBPROCESS_STDOUT_LIMIT,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return AgentResult(
            session_id=session_id, status="errored",
            final_text="", audit_log_path=audit_path,
            started_at=started_at,
            completed_at=int(time.time()),
            error_detail=f"claude executable not found: {e}",
        )

    if proc.stdin is None:
        return AgentResult(
            session_id=session_id, status="errored",
            final_text="", audit_log_path=audit_path,
            started_at=started_at,
            completed_at=int(time.time()),
            error_detail="subprocess has no stdin pipe",
        )

    # Pipe in the request body and close stdin; the CLI in -p mode
    # reads a single message from stdin and runs to completion.
    try:
        proc.stdin.write(request_body.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError) as e:
        # Subprocess died before we could feed it. Treat as an
        # immediate errored exit; the wait() below will surface the
        # real return code.
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "_audit_stdin_failed",
                "detail": str(e),
                "ts": int(time.time()),
            }) + "\n")

    # Set up the kill wait: either stdout drains (normal completion),
    # the stop_event fires (DMS / panic), or we time out. asyncio.wait
    # with FIRST_COMPLETED gives us all three semantics in one shot.
    stream_task = asyncio.create_task(
        _stream_events_to_audit(proc, audit_path),
        name=f"agent-stream-{session_id}",
    )
    waiters: list[asyncio.Task[Any]] = [stream_task]
    if stop_event is not None:
        waiters.append(asyncio.create_task(
            stop_event.wait(), name=f"agent-stop-{session_id}",
        ))

    timed_out = False
    killed_by_stop = False
    cancelled = False
    try:
        done, pending = await asyncio.wait(
            waiters, timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            timed_out = True
        else:
            # If the stop event fired before stdout drained, that means
            # we need to kill the subprocess. The stream task will
            # finish on its own once stdout closes.
            for task in done:
                if task is not stream_task:
                    killed_by_stop = True
    except asyncio.CancelledError:
        # Daemon shutdown / panic / direct task cancel. We MUST kill
        # the subprocess before re-raising — otherwise we leak it.
        cancelled = True
    finally:
        # Clean up any pending awaitables.
        for task in waiters:
            if task is not stream_task and not task.done():
                task.cancel()

    if cancelled:
        try:
            await _kill_and_reap_group(proc)
        except asyncio.CancelledError:
            # Re-cancellation during cleanup; the helper has already
            # tried SIGTERM and SIGKILL. Force one more SIGKILL on the
            # group as a last resort and let the cancellation propagate.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            with audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "type": "_audit_session_killed",
                    "reason": "cancelled_double",
                    "completed_at": int(time.time()),
                }) + "\n")
            raise
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "_audit_session_killed",
                "reason": "cancelled",
                "completed_at": int(time.time()),
            }) + "\n")
        # Re-raise CancelledError so the awaiting watcher's task
        # cancellation completes properly.
        raise asyncio.CancelledError()

    if killed_by_stop or timed_out:
        # SIGTERM the whole group, escalate to SIGKILL after grace.
        # `_kill_and_reap_group` is idempotent and never leaks a zombie.
        await _kill_and_reap_group(proc)
        # Make sure the streaming task finishes draining whatever
        # arrived between SIGTERM and exit.
        try:
            final_text, _ = await asyncio.wait_for(stream_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            final_text = ""
        completed_at = int(time.time())
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "_audit_session_killed",
                "reason": "timeout" if timed_out else "stop_event",
                "completed_at": completed_at,
            }) + "\n")
        composed_body, composed_subject = _read_compose_reply_log(
            compose_reply_log_path,
        )
        attachments_tuple = _read_attachments_log(attachments_log_path)
        return AgentResult(
            session_id=session_id, status="killed",
            final_text=final_text or "",
            audit_log_path=audit_path,
            started_at=started_at, completed_at=completed_at,
            error_detail="timeout" if timed_out else "stop_event",
            composed_body=composed_body,
            composed_subject=composed_subject,
            attachments=attachments_tuple,
        )

    # Normal completion: stream task is done, get its result.
    try:
        final_text, _ = await stream_task
    except Exception as e:
        # Bug X (silent-wedge incident 2026-05-07T21-37): the stream
        # task can throw on an unbounded asyncio StreamReader chunk,
        # a malformed JSON frame, an EOF, or a transport hiccup.
        # Whatever the cause, the subprocess is still alive — its
        # model loop reads tool results via internal MCP channels, not
        # via our stdout reader, so it can keep firing real-world side
        # effects (Chrome/Playwright tool calls) until something kills
        # it. Kill-and-reap the whole group so the subprocess can't
        # outlive the daemon's tracking.
        await _kill_and_reap_group(proc)
        completed_at = int(time.time())
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "_audit_stream_error",
                "detail": str(e),
                "completed_at": completed_at,
            }) + "\n")
        composed_body, composed_subject = _read_compose_reply_log(
            compose_reply_log_path,
        )
        attachments_tuple = _read_attachments_log(attachments_log_path)
        return AgentResult(
            session_id=session_id, status="errored",
            final_text="", audit_log_path=audit_path,
            started_at=started_at, completed_at=completed_at,
            error_detail=f"stream error: {e}",
            composed_body=composed_body,
            composed_subject=composed_subject,
            attachments=attachments_tuple,
        )

    return_code = await proc.wait()
    completed_at = int(time.time())
    composed_body, composed_subject = _read_compose_reply_log(
        compose_reply_log_path,
    )
    attachments_tuple = _read_attachments_log(attachments_log_path)
    # Track whether the agent actually delivered a usable composed
    # reply on a clean run. If it didn't, emit a missing-event into
    # the audit log so adoption is observable: grep
    # `_audit_compose_reply_missing` across sessions to see how many
    # the agent forgot to call (or how many ran on a build where the
    # MCP tool wasn't available).
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "_audit_session_completed",
            "completed_at": completed_at,
            "return_code": return_code,
        }) + "\n")
        if return_code == 0 and composed_body is None:
            fh.write(json.dumps({
                "type": "_audit_compose_reply_missing",
                "completed_at": completed_at,
            }) + "\n")

    if return_code != 0:
        stderr_bytes = await proc.stderr.read() if proc.stderr is not None else b""
        return AgentResult(
            session_id=session_id, status="errored",
            final_text=final_text, audit_log_path=audit_path,
            started_at=started_at, completed_at=completed_at,
            error_detail=(
                f"claude -p exited with code {return_code}; "
                f"stderr: {stderr_bytes.decode('utf-8', errors='replace')[:400]!r}"
            ),
            composed_body=composed_body,
            composed_subject=composed_subject,
            attachments=attachments_tuple,
        )

    return AgentResult(
        session_id=session_id, status="completed",
        final_text=final_text, audit_log_path=audit_path,
        started_at=started_at, completed_at=completed_at,
        composed_body=composed_body,
        composed_subject=composed_subject,
        attachments=attachments_tuple,
    )
