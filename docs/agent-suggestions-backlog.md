# Agent suggestions — backlog

Six suggestions surfaced by Nightjar across the 2026-05-08 thread
(subject "Re: Nightjar agent: response", thread root msgid
`<CANELDKeXiRgUx1JQDyXyR6tMQuCY2xr_pafOPWmiZtyhmSwvHA@mail.gmail.com>`).
Working through them one at a time. Cross-references at the bottom.

When an item lands, mark it **[SHIPPED]** with the date and bump the
"Next up" pointer. Delete fully-shipped items only after a session or
two of bedding-in.

**Next up: #4 — extended thinking (BLOCKED on `claude` CLI exposing a flag).**

---

## 1. Audit-log gap — record `request_body` in `_audit_session_start`

**Status: SHIPPED 2026-05-08.** Two new tests added (979 → 981
passing). Edit landed in `daemon/principal_agent.py:519` (now adds
`"request_body": request_body,` to the envelope). Tests in
`tests/test_principal_agent.py` cover both init and continuation
spawn-failure cases plus an updated assertion in the existing
envelope test.

**Estimate was:** 30 min including test.
**Priority:** HIGH (do first; precondition for any future Class C
gate evaluation work).

`daemon/principal_agent.py:510` writes a session-start envelope to
the per-session audit JSONL but doesn't include the principal's
request body. If the executor errors before the first tool-use
event (as happened on 2026-05-06 with the PATH bug), the audit log
shows only `_audit_session_start` and the body is unrecoverable
from local state — the `messages` table stores metadata only.

**Fix:** add `"request_body": request_body,` to the dict written at
line 510. `request_body` is already in scope as a function parameter
(line 442). HOTP codes are already stripped by
`agent_router.classify` before the body reaches the executor
(`agent_router.py:104` and `:120`), so the field is sanitised in
the auth-code sense.

**Tests:** new test in `tests/test_principal_agent.py`:
`test_audit_session_start_records_request_body` — spawn a
fake-claude that errors immediately, assert the
`_audit_session_start` line in the resulting audit JSONL has
`request_body` matching the input. Cover both init and continuation
cases. Update any existing envelope test to include the new field
(quick grep needed).

**Sanitisation note:** the audit log is local-only, mode 0600,
never transmitted. The `claude` CLI itself probably caps request
size, so a 100MB-body pathological case is unlikely. Ship verbatim;
add truncation as a follow-up if it ever bites.

**Cross-ref:** `agent-workspace/proposals/02-audit-log-gap-fix.md`,
`project-nightjar-agent-mvp-shipped.md` "Audit-log gap finding"
section, `~/nightjar/docs/research-log.md`.

---

## 2. Compose-reply as a tool call

**Status: SHIPPED 2026-05-08.** Custom stdio MCP server
(`daemon/compose_reply_mcp.py`) exposes `compose_reply(body, subject?)`
to claude via `--mcp-config`. Server appends each call as JSONL to
`<session>.compose-reply.jsonl` next to the audit log. Daemon reads
the file after claude exits (last-valid-call wins; empty body
treated as no call) and prefers the composed body+subject over
final_text on the completed path. Killed/errored paths intentionally
ignore composed_body. Boot smoke probe (`daemon/compose_reply_smoke.py`)
runs at daemon startup; failure refuses to start. System prompt
rewritten to name compose_reply as the canonical reply mechanism.
30 new tests (16 MCP+probe, 9 executor, 5 reply-path); 1011 passing
(was 981). Plan at `~/.claude/plans/floating-forging-naur.md`.

**Estimate was:** half a day to a day, +4-6 tests.

Today the agent's reply IS the final assistant text block —
`AgentResult.final_text` gets emailed verbatim. There's no separation
between "I'm planning the reply" and "this is the reply." Symptom:
"Now the response..." planning-prose leakage at the top of replies
(observed in UID 131 of the 2026-05-08 thread, called out by the
principal in UID 140).

**Fix:** introduce a `compose_reply(body, subject?)` tool. Tool
argument becomes the canonical reply; assistant text in the same
turn is treated as scratch and discarded.

**Touches:**
- `daemon/principal_agent.py` — register the new tool, accumulate
  the call into `AgentResult`, parse the stream-JSON event for it.
- `AgentResult` shape — replace or shadow `final_text` with the tool
  argument when present.
- `daemon/inbox_watcher.py:1597` `_send_agent_reply` — use the
  tool-call body instead of `final_text` when present.
- System prompt — explain the tool, with a **rule: tool call wins;
  text-only output when a `compose_reply` is also present is
  discarded.**

**Open question:** what happens if the agent calls `compose_reply`
multiple times in one turn? Cleanest: last call wins; earlier calls
are visible in the audit log as drafts. This mirrors how
`final_text` already works (only the final assistant block matters).

**Tests:**
- single `compose_reply` produces the right reply.
- text-only output (no tool call) still works (back-compat).
- text + `compose_reply` → tool call wins, text is discarded.
- multiple `compose_reply` → last wins, earlier draft preserved in
  audit JSONL.
- `compose_reply` with subject override.
- continuation session: each turn produces its own reply.

**Cross-ref:** Nightjar's UID 133 reply (2026-05-08 17:25 UTC), the
Order-of-Operations paragraph.

---

## 3. Attachments passthrough on the agent reply path

**Status: SHIPPED 2026-05-08.** New `attach_to_reply(path, filename?)`
tool added to the same MCP server (now v0.2.0, exposes both tools).
Path validated at tool-call time (absolute / exists / regular file
/ readable / 18 MiB hard cap, soft warn at 10 MiB). Each call
appends one JSONL entry to a per-session attachments log. Daemon
reads the log after claude exits, builds AgentAttachment instances,
threads them via AgentResult → `_send_agent_reply` → 
`_send_deterministic_reply` → `notifier.notify_principal(attachments=)`.
Notifier reads each file fresh from disk and adds as a MIME part
with mimetype guessing + octet-stream fallback. Completed-path-only
(killed/errored runs drop attachments per decision #6). Old SMTP
workaround in agent CLAUDE.md replaced with the new tool docs;
bootstrap template updated for fresh installs. 24 new tests
(1011 → 1035 passing).

**Follow-up SHIPPED 2026-05-08 (later same evening):** per-turn
truncation. The attachments + compose-reply JSONLs are session-scoped
on disk but their contents are turn-scoped. Without truncation a
prior turn's calls silently re-fired on the next turn (Nightjar
itself caught this in the e2e test). Fix: `principal_agent.execute()`
unlinks both logs before each spawn. Three regression tests added
(1035 → 1038 passing). Write-up:
`agent-workspace/proposals/attachments-log-per-turn-bug.md` (now
marked FIXED).

**Estimate was:** half a day, +6 tests, ~150-200 LOC across 4 modules.

`notifier.notify_principal` is body-only despite `notifier.py`
already having attachment plumbing (used by `forward_to_principal`
for the .eml triage forward, lines 444+). Today the agent works
around this by opening its own SMTP connection from
`eagre.nightjar` — only possible because the principal grants full
machine access. In a sandboxed setup the agent literally cannot
send a file.

**Fix (three changes, all backwards-compatible):**

1. `AgentResult` grows `attachments: tuple[AgentAttachment, ...] = ()`.
   New `AgentAttachment` dataclass (path, filename, maintype, subtype).
2. `notifier.notify_principal` accepts `attachments: Sequence[AttachmentSpec] = ()`.
   After `_build_message`, before `_smtp_send`, loop and `msg.add_attachment(...)`.
3. `_send_agent_reply` and `_send_deterministic_reply` thread it through.
   Default empty preserves all 28 existing `_send_deterministic_reply`
   call sites unchanged.

**How the agent populates it — recommended Option C:** new
`attach_to_reply(path, filename=None)` tool. Tool calls accumulate
into a list on the executor side; the executor copies into
`AgentResult.attachments` at session end. Mirrors how every other
side effect works (tool call → audit event); auditable from existing
JSONL trace; ports to a sandboxed setup (the tool can refuse paths
outside allowed roots).

**Caps:** 18 MiB combined raw (Gmail's 25 MiB wire after base64).
Soft warn above 10 MiB. Reject at the tool-call boundary so the
agent can react (drop, gzip, split) rather than failing silently
at send time.

**Outbound log:** add an `attachment_summary` text column ("`foo.zip
(2.3 MiB), bar.txt (812 B)`"); don't persist bytes. Replay still
possible from audit JSONL + local files if needed.

**Tests:** see proposal 03 for the full list (six cases across
notifier, inbox_watcher, principal_agent suites).

**Migration:** rewrite the "How do I send an attachment to the
principal?" chain in `~/.local/share/nightjar/agent-workspace/CLAUDE.md`
to point at `attach_to_reply` instead of the SMTP workaround. Keep
the workaround section for one release as fallback.

**Cross-ref:** `agent-workspace/proposals/03-attachments-passthrough.md`,
`agent-workspace/_send_rom.py` (the workaround in practice),
`daemon/notifier.py:444` (model for how the reply path could attach).

---

## 4. Extended thinking (conditional, opt-in)

**Status:** pending — **BLOCKED on `claude` CLI exposing a flag.**
**Priority:** LOW. Larger than it sounds.

Today the executor invokes `claude -p` with no thinking flag
(`principal_agent.py:491`). The CLI doesn't currently expose an
extended-thinking flag — `claude --help | grep -i think` returns
nothing as of 2026-05-08. So this isn't a half-day flag-flip; it's
either (a) wait for CLI support, or (b) move the executor off the
CLI and onto the SDK directly, which is a multi-day architectural
change.

**Recommended shape if/when it lands:** conditional, off by default.
Turn on when:
- request body exceeds N tokens, OR
- principal includes a `--think` token in the email, OR
- the agent itself signals "non-trivial" via an early tool call.

**Cost:** extended-thinking tokens are billed and multiplicative on
short acknowledgements. Default off, opt-in for proposal-drafting /
design review / debugging.

**Audit:** the SDK exposes thinking blocks in the message stream;
they're shape-compatible with the JSONL audit format. One extra
`type: "thinking"` line per block, written to the same per-session
file. No new infrastructure needed once item #2 (compose-as-tool)
lands.

**Dependency:** Nightjar called this out — without #2 in place,
extended thinking just produces "more polished planning prose
leaking into the reply." So #2 is a soft prerequisite for getting
value from this.

**Cross-ref:** Nightjar's UID 133 reply (2026-05-08 17:25 UTC),
"Extended thinking" section.

---

## 5. Bootstrap CLAUDE.md — consolidate the "one-off action" chain

**Status: SHIPPED 2026-05-08.** New chain "...run a one-off
action against the principal's mailbox without touching the
daemon?" added at the top of the "How do I..." section in both
the live agent workspace (`~/.local/share/nightjar/agent-workspace/CLAUDE.md`)
and the bootstrap template (`daemon/principal_agent.py`'s
`_AGENT_CLAUDE_MD_BOOTSTRAP`). Names the three primitives in
execution order (decrypt secrets → connect IMAP → optionally
send SMTP), points at the existing detailed chains, and includes
a worked end-to-end example using `BODY.PEEK[]` and the right
aioimaplib timeout (per silent-wedge #7).

**Estimate was:** 15 minutes.

The IMAP-read / SMTP-send / encrypted-secrets pattern is currently
spread across three "How do I..." chains in
`~/.local/share/nightjar/agent-workspace/CLAUDE.md`:

- "...read a prior email body the principal is referencing?" — IMAP read.
- "...send mail from a non-Nightjar address?" — SMTP send.
- "...decrypt the IMAP password (or any other secret)?" — secrets.

Add a new chain: **"...run a one-off action against the principal's
mailbox without touching the daemon"** that links the three
together with a concrete end-to-end example. Doesn't replace the
three existing chains — they're useful as standalone reference;
just adds a top-level entry that links them in the order an agent
would actually use them.

**Cross-ref:** Nightjar's UID 131 reply (2026-05-08 13:59 UTC),
"Other feedback" bullet 1.

---

## 6. HOTP footer — phone-readable audit hook

**Status:** pending — **needs design first, not just a footer
edit.** **Priority:** LOWEST (defer until #1-#3 are shipped).

Every Nightjar reply ends with `Audit log (local):
/home/deck/.local/share/nightjar/agent-audit/<session>.jsonl`. On
desktop the principal can `cat` it; on phone the path is dead text.
Nightjar self-graded "low-priority cosmetic."

**Real fix isn't shortening the path** — it's deciding *what* a
phone-readable audit hook should be. Options to weigh later:

- A web view served by a lightweight local HTTP listener (only
  reachable on LAN or via Tailscale). Requires auth.
- An email-back command: principal replies `audit` to get the
  session's tool-call summary in a follow-up email. Self-served
  via the existing IMAP loop.
- Drop the local path entirely and rely on the audit-command in
  status reports.

Option 2 is probably the right shape — no new listener, no auth
worry, fits the existing email-only UX. Worth a half-page design
sketch before implementation.

**Cross-ref:** Nightjar's UID 131 reply (2026-05-08 13:59 UTC),
"Other feedback" bullet 2.

---

## Cross-references

- Source thread: 2026-05-08, subject "Re: Nightjar agent: response",
  UIDs 131-142 in `eagre.nightjar@gmail.com` mailbox.
- Nightjar's proposal drafts:
  `~/.local/share/nightjar/agent-workspace/proposals/{02,03}-*.md`.
  Read these for the full design detail before implementing #1 or #3.
- Proposal 01 (`01-class-c-gate-mvp.md`) is the larger Class C gate
  research target — separate, not on this backlog. Tracked under
  `project-nightjar-research-target-capture.md`.
