# Agent suggestions — 2026-05-09 batch

Six pieces of feedback Nightjar surfaced in the 2026-05-09 10:52 reply
(workspace reorganisation task; full reply in
`agent-audit/92a3ea47-9e30-4eb3-af01-dece558585c6.compose-reply.jsonl`).

When an item lands, mark it **[SHIPPED]** with the date. Delete fully
shipped items only after a session or two of bedding-in.

---

## 1. Random project CLAUDE.md gets pulled into agent context

**Status:** pending. **Priority:** HIGH (cheap, ongoing token cost).

Today's session inherited `/home/deck/CLAUDE.md` (the Seihoku/TORCH
guidance, ~9KB) because that's what's in the cwd ancestry. Pure noise
for an email task and a small ongoing token cost on every email.

`claude` walks up from the spawn cwd looking for `CLAUDE.md` files
and concatenates them. Nightjar's spawn cwd is the agent-workspace dir
under `~/.local/share/nightjar/`, so it walks up through `~/` and
finds `/home/deck/CLAUDE.md`.

**Fix shape:** either (a) launch `claude` with a cwd that has no
parent CLAUDE.md to find (e.g. spawn from a directory under `/tmp/`
that contains a symlink to the workspace), or (b) pass the documented
opt-out flag if one exists. Verify which flag exists before committing
to (a).

**Cross-ref:** Nightjar's 2026-05-09 reply, item 1.

---

## 2. ToolSearch round-trip on every fresh session

**Status:** pending. **Priority:** HIGH (one wasted turn per email).

`compose_reply` and `attach_to_reply` are deferred MCP tools — the
agent has to call ToolSearch to load their schemas before it can use
them. One whole turn (input + output, plus the round-trip) per
session, every email.

**Fix shape:**
- Quick win: system-prompt nudge telling the agent to ToolSearch
  load both reply tools as the first action. One-line addition.
- Real fix: investigate whether non-deferred MCP tool loading is
  something we control from the spawn side, or whether deferred
  loading is a `claude -p` harness behaviour we can't override.

Ship the prompt nudge as an immediate win; investigate the harness
side separately.

**Cross-ref:** Nightjar's 2026-05-09 reply, item 2.

---

## 3. HOTP footer — agent doesn't know what it's for

**Status:** pending. **Priority:** MEDIUM (cheap, prevents future
confusion).

Every reply carries `Reply with one HOTP code on the first line to
continue.` Nightjar inferred from the daemon code that this is
principal-pipeline plumbing it shouldn't act on. Wants a one-liner
in the agent CLAUDE.md confirming the agent should NOT generate
HOTP codes itself.

Failure mode: a future agent generates fake HOTPs in its reply body.
Funny but bad. One-line addition to
`~/.local/share/nightjar/agent-workspace/CLAUDE.md` and the bootstrap
template in `daemon/principal_agent.py`.

**Cross-ref:** Nightjar's 2026-05-09 reply, item 3.

---

## 4. Audit-log path leaked in reply footer

**Status:** pending. **Priority:** LOW.

Every reply exposes the full local path to the session audit log.
If the principal forwards a Nightjar reply, the path leaks. Path
alone reveals nothing without filesystem access, but the
forwarded-email leak is real.

**Two options:**
- (a) Drop the path entirely from the footer.
- (b) Keep it but reduce to session-id only (`session: <uuid>`)
  without the filesystem prefix. Operator can still
  `ls audit/<uuid>*` from the email.

(b) preserves operator utility; (a) is purer. Lean (b).

**Cross-ref:** Nightjar's 2026-05-09 reply, item 4.

---

## 5. Repeated TodoWrite system reminders

**Status: SHIPPED 2026-05-09.** Set `CLAUDE_CODE_ENABLE_TASKS=false`
in the spawn env in `daemon/principal_agent.py`. Disables the entire
Task subsystem in the harness, including the periodic
`<system-reminder>` nudges that were appearing every ~2-3 tool calls.

The harness has no per-message opt-out for the reminder
(claude-code issue #26038). The Task subsystem is the only documented
escape hatch. Nightjar's 2-3-tool-call email tasks don't benefit from
todos anyway, so killing the subsystem outright costs us nothing.

**Why this matters more than it looks:** claude-code issues #40573,
#41091, #40176 document that these reminders degrade context
retrieval and bias attention quality. Not just noise — measurable
quality regression.

**Side artifact:** the harness internally distinguishes
agent-authored from harness-injected content via an `isMeta` flag
that isn't surfaced to operators.

Investigation: side-agent search of `anthropics/claude-code`
(2026-05-09). Findings in this conversation's transcript.

**Cross-ref:** Nightjar's 2026-05-09 reply, item 5.

---

## 6. No structured way to leave a "FYI" alongside a reply

**Status:** deferred — needs more signal first.

Reply body is the only channel. "I did the thing, also I noticed X"
goes in the same body. This very batch of feedback is exactly that
pattern (workspace-reorg task smuggled six observations into the
reply body).

**Possible shape:** a third MCP tool, e.g. `attach_aside(text)`,
with its own JSONL log, that the daemon either inlines at the bottom
of the reply or surfaces separately in the status report.

Defer until we have more signal that this happens often enough to
justify the design.

**Cross-ref:** Nightjar's 2026-05-09 reply, item 6.

---

## Cross-references

- Source: 2026-05-09 10:52 reply, session
  `92a3ea47-9e30-4eb3-af01-dece558585c6`. Compose-reply log at
  `~/.local/share/nightjar/agent-audit/<session>.compose-reply.jsonl`.
- Side-agent investigation of item 5: `~/Downloads/compass_artifact_*.md`
  (2026-05-09).
