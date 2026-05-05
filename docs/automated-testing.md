# Automated Testing Guide for Nightjar

Conventions, paths, and gotchas for any agent (or operator) running
live tests against a Nightjar daemon. Start here before doing any
end-to-end testing — this captures the bumps that aren't obvious from
the codebase.

## Scope

This guide covers **live tests against a running daemon**: sending
mail through SMTP, watching the daemon ingest and triage it, inspecting
state and notes after the fact. For unit tests, see `tests/` and
`pytest` directly — those don't touch the network or the daemon.

## Paths and conventions

| Thing | Location |
|---|---|
| Repo | `~/nightjar/` |
| Daemon entry point | `~/nightjar/.venv/bin/python -m daemon.main` |
| Daemon config | `~/.config/nightjar/nightjar.conf` |
| Daemon secrets (encrypted) | `~/.config/nightjar/secrets.toml` |
| State database (sqlite) | `~/.local/share/nightjar/state.db` |
| **JSONL daemon logs** | `~/nightjar/logs/nightjar-YYYY-MM-DD.jsonl` |
| **Per-contact notes** | `~/nightjar/contacts/<contact_id>.md` |
| Contact configs | `~/.config/nightjar/contacts/<contact_id>.toml` |
| Test SMTP creds (separate file) | `~/.config/nightjar/test_creds.toml` |
| Test harness | `~/nightjar/tools/test_harness.py` |

The trap: `notes_dir` and `log_dir` default to under `~/nightjar/`,
NOT under `~/.local/share/nightjar/` (which is only `state_dir`). All
three are configurable via `nightjar.conf` `[daemon]`. Confirm with:

```bash
.venv/bin/python -c "from daemon import config; from pathlib import Path; \
  c = config.load(Path.home() / '.config/nightjar/nightjar.conf'); \
  print('state_dir:', c.daemon.state_dir); \
  print('log_dir:', c.daemon.log_dir); \
  print('notes_dir:', c.daemon.notes_dir)"
```

## The test harness

`tools/test_harness.py` is the only sanctioned way to put mail in
front of the daemon. It speaks SMTP to Gmail using credentials from
`test_creds.toml` and signs principal mail with HOTP codes pulled
from the daemon's encrypted secrets file.

### Sending mail

```bash
# From the principal's test account (HOTP-prefixed subject — counts
# against authentication state):
.venv/bin/python tools/test_harness.py send "subject text" "body text"

# From a contact's test account (still HOTP-prefixed but the prefix
# is irrelevant for contact triage):
.venv/bin/python tools/test_harness.py --creds-section eagre_dev \
  send "subject text" "body text"
```

The `--creds-section` flag picks which `[section]` of `test_creds.toml`
provides the SMTP login. Default is `eagre_claude` (the principal's
test account). For contact-impersonation tests, pass the contact's
section explicitly. The harness does NOT consult `contacts/*.toml` to
infer this — the section name is a free-form choice.

### Replying to an approval

```bash
.venv/bin/python tools/test_harness.py reply <token> <verdict>
```

Token is the `#xxxxxxxx` hex you see in the principal-facing
notification email subject (and in `triage_approval_queued` log
events). Verdict is verbatim body text — typically `yes`/`no` but
the daemon's `principal_interpret` handles free-form too.

### Generating a HOTP code without sending

```bash
.venv/bin/python tools/test_harness.py code
```

Useful when sending principal mail by hand (rare; usually let the
harness do it).

## Watching the daemon during a test

The daemon emits structured JSONL events to today's log file. The
events you care about for live testing:

| Event | When |
|---|---|
| `mail_received` | Inbox has a new message, post-DMARC, post-routing |
| `scope_classify_complete` | Pass-1 Haiku classifier returned a scope |
| `triage_complete` | Pass-2 Sonnet triage emitted a plan |
| `triage_failed` | Validator rejected the plan |
| `note_written` | A note proposal landed on disk |
| `note_write_failed` | Note write hit an error (NotesParseError, OSError) |
| `triage_approval_queued` | Plan went into the approval queue |
| `notify_principal_sent` | Principal got an approval-prompt email |
| `principal_approval_executed` | Principal said yes, executor ran |
| `principal_approval_resolved` | Approval row terminated |
| `panic_trip` | Dead-man's-switch armed (CRITICAL) |

Recommended pattern for an agent running tests:

```bash
# Stream relevant events as they fire
tail -F ~/nightjar/logs/nightjar-$(date -u +%Y-%m-%d).jsonl | \
  grep --line-buffered -E '"event": "(triage_complete|triage_failed|note_written|note_write_failed|triage_approval_queued|notify_principal_sent|panic_trip|out_of_scope_decline)"'
```

Use the `Monitor` tool with this command (not `tail -f` directly in
Bash) so each event arrives as a notification.

## Inspecting state

### State database

```bash
.venv/bin/python -c "
import sqlite3, json
conn = sqlite3.connect('/home/deck/.local/share/nightjar/state.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
# What's in each table:
cur.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")
print([r[0] for r in cur.fetchall()])
"
```

Tables you'll touch most:
- `messages`: every inbound email's lifecycle. Columns include
  `state`, `approval_token`, `plan_json` (often empty in current
  versions — plan content goes into `approvals.args_json`).
- `approvals`: pending and resolved approval queue. Columns:
  `token, message_id, verb, args_json, tier, state, created_at,
  expires_at, resolved_at, resolved_detail`. **There is no
  `summary` or `reasoning` column** — those live inside `args_json`
  along with the rest of the plan.
- `transitions`: state-machine audit trail.
- `claude_invocations`: per-call cost and outcome (no plan content).

### Notes files

```bash
cat ~/nightjar/contacts/test.md  # or whichever contact_id
```

Format is markdown with YAML-ish frontmatter, h2 sections with
`[scopes: ...]` tags, bullets with optional `[meta: src=...; attr=...]`
provenance tags. See `daemon/notes_store.py` docstring for the spec.

`Nightjar, show notes <contact_id>` (sent as principal mail) renders
the audit view with attribution badges. For agent purposes reading
the file directly is usually cheaper.

### Principal email body inspection

The triage plan's `summary`, `reasoning`, `risk_flags`, and `notes`
fields are materialised into the principal-notification email body
and discarded. If you need that content, you have to either:
1. Read the principal's actual mailbox (manual, slow), or
2. Fetch from `approvals.args_json` IF the plan was a queueable
   verb (reply, forward, out_of_scope_decline). Plans for
   `flag_for_review` and `noop` don't queue and aren't recoverable
   from state — only the email body has them.

This is a known gap. If you need plan content for ALL verbs during
testing, log into the principal's inbox or write a separate hook.

## Daemon lifecycle

```bash
# Start (foreground):
cd ~/nightjar && .venv/bin/python -m daemon.main

# Start (background, stdout+stderr to file):
cd ~/nightjar && nohup .venv/bin/python -m daemon.main \
  > /tmp/nightjar-daemon.log 2>&1 &

# Stop:
pkill -f "daemon.main"

# Confirm (note: there are persistent kate sessions on the conf file
# that match a `nightjar` grep — match `daemon.main` not `nightjar`):
ps -ef | grep "daemon.main" | grep -v grep
```

After ANY edit to a daemon module, the running daemon must be
restarted to pick it up. Prompts (`prompts/*.md`) are reloaded fresh
per triage call; no restart needed for prompt edits.

## Cleanup between runs

The daemon accumulates state across runs. Before a fresh test wave
you typically want to:

```bash
# Wipe the contact's notes file (avoid carrying poisoning between waves):
rm -f ~/nightjar/contacts/test.md

# Expire pending approvals (so they don't pile up in the principal
# inbox or trigger expiry-time pings):
.venv/bin/python -c "
from daemon.state import State
from pathlib import Path
import time
s = State(db_path=Path.home() / '.local/share/nightjar/state.db')
# Find pending approvals
import sqlite3
conn = sqlite3.connect(s.db_path)
cur = conn.cursor()
cur.execute(\"SELECT token FROM approvals WHERE state = 'PENDING'\")
tokens = [r[0] for r in cur.fetchall()]
conn.close()
for t in tokens:
    s.resolve_approval(token=t, outcome='EXPIRED', detail='manual cleanup')
print(f'expired {len(tokens)} approvals')
"
```

Do NOT `DELETE FROM messages` — see the project memory note on
state-db deletion. Always transition to a terminal state.

## Common bumps (and how to avoid them)

| Bump | Fix |
|---|---|
| Daemon imports fail with `ModuleNotFoundError: aioimaplib` | Use `.venv/bin/python`, not the system `python3` |
| `pkill -f` returns exit 1 in compound commands | Run as separate Bash calls, not chained with `&&` |
| `State` not `StateStore` | Class is named `State` in `daemon/state.py` |
| `resolve_approval` positional args fail | Pass kwargs: `s.resolve_approval(token=t, outcome='EXPIRED', detail=...)` |
| Logs aren't where you expect | Default `log_dir` is `~/nightjar/logs/`, not `~/.local/share/nightjar/logs/` |
| Plans don't show up in `approvals.summary` | They're inside `args_json` (`json.loads(row['args_json'])['summary']`) |
| `flag_for_review` plans aren't recoverable from state | They never queue — only the principal email body has the content |
| Contact mail subject has a HOTP prefix | Harness adds it unconditionally; daemon ignores it for non-principal senders |

## Don't-do list

- **Never bypass the harness** to send test mail directly via SMTP.
  The harness is the audit point — its JSON-line events show up in
  the conversation transcript, and they're the only record that a
  test mail came from inside the box rather than the wild.
- **Never edit `~/.config/nightjar/nightjar.conf` mid-run** unless
  you also restart the daemon. Daemon caches config at startup.
- **Never delete from `messages` or `transitions`** — always
  transition to a terminal state. Catchup will re-surface deleted
  rows and may trip the panic switch.
- **Never use `--no-verify` on git commits**. (Standard rule, but
  worth restating because pre-commit hooks here include the daemon's
  test sanity checks.)

## Quick start for a fresh agent

If you're an agent reading this for the first time, the minimum
sequence to get useful is:

1. `cat ~/.config/nightjar/nightjar.conf` — what scopes exist, what
   the principal address is, which inbox names are configured.
2. `ls ~/.config/nightjar/contacts/` — what test contacts exist.
3. `ls ~/nightjar/contacts/` — what notes files already exist (any
   pre-existing state).
4. `ps -ef | grep daemon.main | grep -v grep` — daemon running?
5. Open a Monitor tail on today's log file (pattern above).
6. Send a known-benign test through the harness to confirm the loop
   works end-to-end.
7. Then start the actual scenario.
