# Nightjar

A self-hosted email assistant, and a deliberate exercise in what it
means to use AI seriously.

> The bird this project is named after spends most of its life
> motionless. It hunts at the edges of light, audible only when it
> chooses to be. The software here is meant to behave the same way.

---

## What this is

Nightjar turns a single user's machine into something reachable by
email. It does two distinct kinds of work.

**As a remote control plane**, the operator can email Nightjar from
anywhere, authenticate, and have a real conversation about ongoing
projects. Status queries, scoped operations on the machine, drafted
outbound messages, multi-turn threads. All of it runs through a
tiered approval model: read-only operations are quick, irreversible
ones require explicit double-confirmation. Every email from the
operator must carry per-email cryptographic authentication, or the
daemon refuses it. Repeated failed authentication trips a
dead-man's-switch and halts the daemon until manual revival at the
physical machine.

**As a mediator between the operator and their collaborators**,
Nightjar reads incoming mail from people in the operator's contact
directory, drafts proposed actions (validate this file, render this
preview, reply with these notes), and emails the operator for
approval before doing anything. The collaborator's email is treated
as data, never as instruction. Anything that looks like an
embedded command is reported as a flag, not honoured.

These two relationships have very different security properties.
The operator can do almost anything with proper authentication and
approval. Collaborators are bounded, by code, to a small set of
non-destructive action categories that the operator has
deliberately enabled.

It exists because there's a gap between "AI tools can do useful
work" and "AI tools should be allowed to do useful work
unsupervised on behalf of a real person who has real relationships
with real consequences." Nightjar is one attempt at closing that
gap honestly.

It is not a product. It runs on one machine, for one user, and is
not designed to scale. The approach is portable; this
implementation isn't.

---

## What problem this is actually solving

The operator works on independent technical projects in their spare
time. Some of those projects involve collaborating with people who
aren't technical and don't want to learn the toolchain (a composer
sending audio files for a game soundtrack, for example). Some of
them are solo work the operator wants to be able to drive remotely:
checking on a build from the train, kicking off a long task from
the office, drafting a reply to a collaborator while away from the
machine.

The natural question is whether the AI assistant the operator
already uses for development can handle both: technical mediation
with collaborators, and remote control of the operator's own
workflow.

The answer is yes, but only if it's done in a way that:

- Doesn't expose collaborators to surveillance they haven't
  consented to.
- Doesn't expose the operator's machine to anyone who happens to
  gain access to their email account.
- Doesn't put either party at the mercy of an LLM hallucinating its
  way through a conversation.
- Doesn't let arbitrary email content trick the LLM into actions
  the operator didn't authorise.

That last one is the hardest. Email is an attacker-controlled
channel by design. An LLM that processes email can be talked into
things it shouldn't do, unless the architecture makes it
impossible for it to *act* on anything it's been talked into. Most
of Nightjar's design is about making that architecturally
impossible rather than promising it through prompts.

---

## Design at a glance

```
        ┌────────────────────────────────────────────┐
        │              INCOMING EMAIL                │
        └────────┬───────────────────────────┬───────┘
                 │                           │
        from operator                  from a contact
                 │                           │
                 ▼                           ▼
   ┌─────────────────────┐       ┌──────────────────────┐
   │ DMARC + HOTP check  │       │ DMARC + contact      │
   │  (pure Python,      │       │ directory + rate     │
   │   pre-LLM)          │       │ limit (pure Python)  │
   │                     │       │                      │
   │  fail = halt if     │       │  fail = drop, log    │
   │  threshold reached  │       │  in status report    │
   └──────────┬──────────┘       └──────────┬───────────┘
              │                              │
        pass  ▼                              ▼  pass
   ┌─────────────────────┐       ┌──────────────────────┐
   │  Deterministic      │       │  Pass 1: scope       │
   │  command parse      │       │  classifier          │
   │  (no LLM)           │       │  (Haiku, ~1k toks)   │
   │                     │       └──────────┬───────────┘
   │  recognised verb    │                  │
   │  → tier-based path  │     in scope     ▼
   │                     │       ┌──────────────────────┐
   │  unrecognised       │       │  Pass 2: triage      │
   │  → principal-       │       │  (Haiku, scope-      │
   │     interpret pass  │       │  filtered notes,     │
   │     (Haiku, opt-in) │       │  produces a plan +   │
   │                     │       │  note proposals)     │
   └──────────┬──────────┘       └──────────┬───────────┘
              │                              │
              ▼                              ▼
   ┌─────────────────────┐       ┌──────────────────────┐
   │  Tier-gated action  │◄──────│  Plan emailed to     │
   │  (1: auto-execute,  │       │  operator with the   │
   │   2-3: approval,    │       │  verbatim original   │
   │   4: double-confirm)│       │  appended            │
   └──────────┬──────────┘       └──────────┬───────────┘
              │                              │
              │      (operator replies, HOTP-authenticated)
              │                              │
              └──────────────┬───────────────┘
                             ▼
   ┌─────────────────────┐       ┌──────────────────────┐
   │  Outbound to        │──────►│  Silent audit copy   │
   │  recipient          │       │  to operator         │
   │  (footer attached)  │       │  (separate SMTP)     │
   └─────────────────────┘       └──────────────────────┘
```

The detailed engineering contract lives in [DESIGN.md](./DESIGN.md).
What follows is the rationale for the choices that shape the
project.

---

## Principles

### Operational

- **The LLM never decides whether to act.** It produces plans, the
  daemon executes them, the human approves anything irreversible.
  Approval is a deterministic email reply parsed by Python, not an
  LLM round-trip, so approving can't be tricked and can't cost
  tokens.
- **Recognised verbs bypass the LLM entirely.** `status`,
  `tail log`, `show contact`, `show notes`, `pickup`, `add`,
  `remove`, `block`, `unblock`, `forget`, `reply`,
  `forward_to_principal`: all dispatched by deterministic Python
  parsers. Free-form requests fall through to a small Haiku call
  that interprets the message and proposes a recognised verb;
  there's a soft per-day cost cap and a hard monthly cap on that
  pass.
- **Email is the only interaction surface.** No web UI, no Slack,
  no chat. Email's weaknesses (slow, threaded, asynchronous) are
  exactly the right frame for an assistant whose job is to *not*
  rush.
- **Pull-based throughout.** Nothing in this system can be
  triggered from the internet. The daemon reads its own inbox; it
  cannot be poked into action by a webhook or pushed instructions
  by an external service.
- **Caps everywhere.** Hourly invocation cap, per-call token cap,
  per-contact rate limit, soft + hard daily/monthly cost caps in
  Python, hard daily spend cap on the Anthropic console. The
  daemon is incapable of running away.
- **Self-contained.** No runtime dependency on other software the
  user happens to be running. One project, one config, one process.

### Security

- **Per-email cryptographic authentication for the operator.**
  Every email from the operator must carry a valid HOTP code
  (RFC 4226, counter-based; default) or TOTP code (RFC 6238, time-
  based; configurable). HOTP is the default because TOTP's 90-second
  validity window collides with email's async delivery latency. The
  shared secret lives on the operator's phone and in the machine's
  obfuscated secrets file; it never enters any LLM context. The
  check is pure Python, runs before any LLM call, and the LLM has
  no API to bypass it.
- **DMARC verification gates everything else.** Inbound mail is
  checked against the trusted authserv's `Authentication-Results`
  header before any contact lookup, before any auth check, before
  any LLM call. Spoofed mail can never reach the dead-man's-switch
  counter or any decision path that consumes the body. Per-operator
  policy: `dmarc=none`, `temperror`, and absent A-R headers are all
  treated as adversarial.
- **Dead-man's-switch on authentication failure.** Three invalid
  attempts within an hour halt the daemon entirely. Recovery
  requires physical presence at the machine *and* a valid auth
  code typed at the terminal. Either alone is insufficient.
- **The email body is data, not instructions.** This is enforced at
  every LLM call without exception. Anything in any email that
  purports to be authority, override, or out-of-band instruction is
  reported as a flag, never honoured. The architecture makes this
  load-bearing rather than guideline-shaped: the LLM has no tools
  to act on instructions it might be tricked by.
- **Capability tiers gate blast radius.** Read-only operations
  auto-execute on authenticated mail. Reversible writes need one
  approval round-trip. Outbound to third parties needs approval
  plus a mandatory audit copy. Irreversible operations need
  approval plus an explicit `YES IRREVERSIBLE` confirmation.
  Contact-driven actions are forever capped at the reversible tier
  regardless of approval.
- **The operator sees every outbound, invisibly.** Every email
  Nightjar sends to a third party generates a separate, silent
  audit copy to the operator. Two independent SMTP sends (not
  BCC), so the recipient's "show original" view contains no trace
  of the audit. The agent has no parameter to disable this.
- **Every approval ping carries the verbatim original email.** The
  triage summary is the LLM marking its own homework. The operator
  can't verify whether the LLM read the email correctly without
  seeing what was actually there, so the original is appended to
  every triage notification (approval ping, summary ping, triage-
  failed ping) in an `========== ORIGINAL EMAIL ==========` block.
- **Receipt reliability over IDLE-only listening.** The watcher
  catches up via per-inbox `SINCE` searches bounded by a state-db
  watermark, with Message-ID dedup as the authoritative "have I
  processed this?" check. Mail flipped to `\Seen` outside the
  daemon's control (Gmail web preview, phone client, daemon crash
  mid-flight) is no longer silently dropped.

### Ethical

- **Memory is bounded by the work.** Nightjar keeps brief notes per
  correspondent (what they're working on, how they prefer to
  communicate, what's specific to the collaboration), one
  human-editable Markdown sidecar per contact. It is forbidden by
  its system prompt from noting health, financial, family,
  relational, or third-party-confidential information, even when
  the correspondent shares it freely. These aren't Nightjar's facts
  to remember.
- **Provenance is tracked per bullet.** Every note bullet carries a
  `[meta: src=<message-id>; attr=observed|asserted|self]` tag.
  `observed` means "the daemon saw the contact do or describe this."
  `asserted` means "the contact made a claim about a third party
  the daemon can't verify." `self` means "the contact made a claim
  about themselves." The audit view (`show notes`) renders ⚠ markers
  for `asserted` and `self` bullets. The triage prompt and a
  deterministic gate forbid the LLM from drafting replies that
  enumerate unverified bullet content; matches downgrade to
  `flag_for_review` so the operator decides.
- **Scopes gate what the LLM ever sees.** Each contact opts into a
  set of scopes (`aurora`, `music-tech`, etc.); each note bullet is
  tagged with the scopes it's visible under. A pass-1 classifier
  routes the inbound message to a scope; pass 2 only loads notes
  visible in that scope. A sensitive bullet under `personal` is
  never present in the prompt for a `music-tech` conversation.
- **Notes pass three tests or they don't get written.** Every
  proposed note must be readable by its subject without surprise,
  must be a professional observation about the work rather than a
  personal one about the person, and must respect the subject's
  dignity in the relationship being recorded. Anything that fails
  any test is flagged for human review and not written.
- **Correspondents can read, erase, and opt out at any time,
  without asking.** Every email Nightjar sends has a footer
  explaining how. `show my notes` returns the notes file with
  unverified bullets marked, `delete my data` wipes it, `stop
  contacting me` blocks further outbound. These commands are parsed
  by the daemon, not the LLM. They cannot fail because the model
  misunderstood them, and they don't depend on the operator
  approving.
- **Disclosure, never concealment.** A new correspondent's first
  email from Nightjar will be a one-time disclosure: who Nightjar
  is, what it's used for, where data goes, what their rights are.
  Until they reply acknowledging, no further mail is sent. The
  footer reminds them on every subsequent message. (The
  first-contact ceremony is designed but not yet shipped; today,
  unknown senders are silently dropped and surfaced to the operator
  only via the status report. See "Status" below.)
- **No covert observation.** Nightjar will not be configured to
  read mail from people who haven't been told it's reading mail.

---

## Threat model

A serious project should name what it's defending against. The
[DESIGN.md](./DESIGN.md) threat-model section enumerates this in
detail; the short version:

- **Contact-content prompt injection.** Defended by the
  data-not-instructions rule, by a strict tool schema with a Python
  validator that exhaustively checks every field, and by the
  architectural constraint that contact-driven actions are capped
  at reversible tiers, by code, with no agent path to escalate. The
  prompt also strips literal block delimiters (`</body>`,
  `</notes>` etc.) and routes recognised injection patterns to
  `flag_for_review` rather than action.
- **Persistent self-poisoning via notes.** Defended by per-bullet
  provenance tagging (`observed` / `asserted` / `self`) on the
  write side, plus a read-side prompt rule and a deterministic gate
  on the verb side: any draft reply that enumerates the content of
  an unverified bullet is downgraded to `flag_for_review` so a
  contact who later claims "you confirmed X to me" can't get the
  daemon to repeat their own assertion back as fact.
- **Sender-address impersonation.** Defended by DMARC verification
  on inbound mail. Spoofed mail can never reach the auth path or
  any LLM call.
- **Operator email account compromise.** Defended by per-email HOTP
  (default) or TOTP. An attacker with the email account but not the
  shared secret cannot authenticate.
- **Indirect injection across LLM calls.** Defended by treating
  every cross-call data flow as content, not control. Outputs of
  earlier LLM calls are never inputs as commands to later ones.
- **Disclosure of secrets via the LLM.** Defended by never giving
  the LLM the secrets in the first place. The system prompt
  refuses to discuss authentication, but the load-bearing defence
  is that there's nothing to disclose.
- **Disk compromise of the operator's machine.** Defended in v1 by
  filesystem permissions and by machine-id-bound obfuscation of
  the secrets file (so a copied backup decodes to garbage on any
  other machine). Encryption against a co-resident root attacker is
  documented as a hardening upgrade, not a v1 promise.
- **Dead-man's-switch as denial-of-service.** Defended by trip
  threshold (3 invalid attempts in an hour) and the requirement
  that only DMARC-passed operator-claimed mail is eligible to
  trip.

These defences are layered. Each one is breachable in isolation;
together they require an attacker to compromise multiple
independent factors simultaneously.

---

## Where the limits are (and an honest accounting)

This document would be dishonest if it presented the principles
above as a finished solution to the problem of AI-mediated
communication. They aren't. There are real gaps, and a serious
project should name them rather than paper over them.

**The first-contact ceremony is a fait accompli, not consent.** As
designed, a new correspondent learns Nightjar exists when they
receive their first email from it. Yes, they can opt out
immediately. Yes, the disclosure is clear. But they didn't choose
to be in this conversation in the way the operator chose to put
them in it. The asymmetry is real. Mitigation in the design:
Nightjar holds all subsequent outbound until they reply `ok
proceed`, so the *only* email they ever receive without explicit
consent is the disclosure itself. It's the smallest fait accompli
that could be engineered, but it's still one. (As of today the
ceremony hasn't shipped: unknown senders are dropped without any
auto-reply, and the operator sees them via the status report. So
the *current* gap is the inverse — silence rather than a fait
accompli — and the work to close it is on the build list.)

**Notes are based on inferences the correspondent doesn't preview.**
The footer says "see what's been noted about you," which is honest
about *what* exists, but a correspondent has no preview of what
kinds of things tend to be noted before they are. The three-test
rule is a strong constraint, the provenance tags surface
unverified bullets with ⚠ markers when notes are dumped, and the
categorical refusals are a strong floor, but the correspondent is
still trusting the operator's judgement about what their assistant
chooses to remember. The defaults ship strict and the public
posture ships
transparent, but this is a place where the operator carries real
responsibility.

**Visibility is asymmetric.** The operator can read the notes any
time; the correspondent only sees them on request. Fixing this
fully would mean emailing the correspondent every note as it's
added, which is absurd. It isn't fixed. It's acknowledged, and the
work goes into the side that *can* be improved: making sure no
note exists that the correspondent would be unhappy to discover.

**The audit copy is for the operator, not the correspondent.** The
operator sees everything Nightjar says on their behalf. The
correspondent doesn't get a mirror. This isn't symmetric, and it
isn't meant to be. It's a tool for the operator to maintain
awareness of an assistant operating in their name, not a
transparency mechanism for correspondents. Correspondents have
their own transparency mechanism in `show my notes` and the
footer.

**Authentication closes most of the door, not all of it.**
HOTP/TOTP defends against email-account compromise, which is the
most realistic attack. It does not defend against simultaneous
compromise of the operator's email *and* their phone, or against
filesystem-level compromise of the machine itself. The shared
secret is stored obfuscated, machine-bound — useless on another
machine, looks like noise to a casual reader, but recoverable by
an attacker who already has root on this one. v1 accepts that
trade; encryption against a co-resident root attacker (passphrase
prompt at start, or hardware-key gating on the highest-risk
operations) is documented as a hardening upgrade rather than a v1
promise.

**The operator is trusted by design.** Nothing in the architecture
prevents the operator from approving an action they shouldn't,
editing notes maliciously, or weaponising the assistant. The
system limits what *the LLM* can do unsupervised; it does not
limit what *the human at the controls* can do. That's the right
place to draw the line, but it should be drawn explicitly. This
software does not absolve its operator of responsibility.

If someone reads this and is moved to build something better (push
on the consent ceremony, add a notes-policy preview, find a way to
mirror visibility more symmetrically without drowning the
correspondent in updates, or strengthen the authentication path),
that would be a legitimate continuation of the project. The
principles here are a starting position, not a destination.

---

## Status

v0.4-ish, mid-Step-7. The security perimeter, the principal control
plane, and the contact-mediation path are all live. Rapport notes
with provenance tagging and scope-aware triage are live. The
first-contact ceremony for unknown senders is the next significant
piece of work.

736 unit and integration tests pass on `main`.

| Step | Subject | Status |
|------|---------|--------|
| 1 | IMAP IDLE watcher, contact lookup, state DB | shipped + live |
| 2 | HOTP/TOTP auth + dead-man's-switch | shipped + live (tripped + recovered live) |
| 3 | SMTP notifier + audit copy + hardcoded footer | shipped + live |
| 4a-d | Tier-1 commands, tier-2 verbs + approval queue, tier-4 add/remove with atomic config rewrite, bare-code subject prefix | shipped + live |
| 5a-b | Triage module + `[claude]` config + spend ledger; full round-trip with DMARC + reply executor + outbound log | shipped + live |
| 6 | Real `forward_to_principal` with `message/rfc822` attachment; hidden-content sweep; jlogger plumbing | shipped + live |
| 6b | Reply-format overhaul (code at end of subject, verdict in body, curated synonym list) | shipped + live |
| 6c | Contacts and secrets extracted from `nightjar.conf` to per-file TOMLs and a machine-id-bound obfuscated secrets file; auto-migrated on first start | shipped + live |
| 6d | Hidden-content false-positive fix (split `total_size_bytes` into plain/html sizes) | shipped + live |
| 6e | Receipt reliability: `SINCE`-bounded catchup, per-inbox watermark, Message-ID dedup, first-run 30-day reconciliation | shipped + live |
| 6f | Drop the "yes interpret" gate; principal-interpret pass with tiered output and soft + hard cost backstops | shipped + live |
| 6g | Status-report overhaul (7 sections including out-of-band IMAP walk) and `pickup <message-id>` verb | shipped + live |
| 7a | Notes infrastructure: notes_store, scope-tagged Markdown sidecars, `show notes` verb | shipped + live |
| 7b | Two-pass scope-aware triage: pass-1 classifier → pass-2 triage; out-of-scope decline path; scope-filtered notes injection | shipped + live |
| 7d | Autonomous note writes from triage's `note_proposals` (per Step 8 memory architecture; the original 7d approval-queue design was reframed and dropped) | shipped + live |
| 7-w2 | Provenance tagging on notes (`[meta: src; attr=observed\|asserted\|self]`) + skeptic prompt rule + audit-view ⚠ markers | shipped + live |
| 7-w3a | Read-side provenance defence: read-aware skeptic clause + verb-side notes-enumeration gate that downgrades a reply to `flag_for_review` if it enumerates an unverified bullet | shipped + live |
| 7c | First-contact ceremony for unknown senders | designed, not yet built |
| 7-w3b | Skeptic rule extension to unnamed implicit consensus ("we agreed", "as we discussed"); hallucination-nudge carve-out for procedural-coordination questions | designed, not yet built |
| 8 | Memory architecture (per-contact + principal-only + project + action-ledger rings) | design phase |

A closed-circuit testing harness (`tools/sim_harness.py`) routes
classifier and triage calls through Claude Code sub-agents instead
of the live API, so red-team probes and regression sweeps can run
without touching the inbox or the spend ledger. The first
auto-redteam loop using it found the persistent-self-poisoning lane
that wave 3a closed.

This README will be updated as the implementation lands. Where the
design and the code disagree, the **code is right and the design
should be updated to match**. `DESIGN.md` is a working document,
not a contract carved in stone.

---

## Stack

- Python 3.13, mostly stdlib.
- `anthropic` (Messages API, `AsyncAnthropic`) for the LLM hops.
  Default model: Haiku 4.5 for both the scope classifier and triage,
  configurable per-call via `[claude]` in `nightjar.conf`. Single-
  shot Messages API only, no agent loop. Earlier drafts named
  `claude-agent-sdk`; that's the Claude Code CLI substrate and the
  wrong fit for single-input/single-output triage. See DESIGN.md
  for the rationale.
- `aioimaplib` for IMAP IDLE on Python 3.13. (Drops out when
  Python 3.14's stdlib IDLE support reaches the deployment target.)
- SQLite (`sqlite3` stdlib) for state persistence. Schema is at
  V10 with idempotent migrations.
- SMTP via stdlib `smtplib` for outbound. No third-party send
  service.
- `hmac` + `secrets` (stdlib) for HOTP/TOTP. No external auth
  library.
- Runs on a Steam Deck (immutable Arch). User-level installation
  only; systemd `--user` unit handles supervision.

---

## Secrets at rest

Nightjar stores its sensitive credentials — SMTP password, IMAP
password, TOTP/HOTP secret, Anthropic API key — in a separate file
at `~/.config/nightjar/secrets.toml`, chmod 600. The file is
**obfuscated, not encrypted**. The keystream is derived from
`/etc/machine-id` via HMAC-SHA256, with a fresh 16-byte salt per
write, so:

- A copy of the file is **useless on another machine**. Backup
  drives, dotfile sync to a public repo, screenshots — all of these
  surface bytes that don't decode without the original install's
  machine-id.
- The file looks like noise to casual inspection. No recognisable
  prefix, no structure that suggests "this is an API key."
- An attacker who already has root on this machine can decode the
  file by reading `/etc/machine-id` and applying the open-source
  algorithm. Encryption against that adversary requires a passphrase
  prompt at every daemon start (which would break unattended
  restart) or a system keyring (which requires non-stdlib
  dependencies). Neither fits Nightjar's operational model in v1.

The threat model this defends against is **accidental exposure**:
backups that include `~/.config/`, sharing a config snippet for
debugging, syncing dotfiles to a public repo, leaving the Steam
Deck unattended at a coffee shop. It does not defend against an
attacker with full local access. The README and module docstrings
spell this out so the protection isn't mistaken for something
stronger than it is.

The file binds to this machine via a **fingerprint** (HMAC of the
machine-id) stored in `state.db`. If `/etc/machine-id` ever changes
(reinstall, manual reset, copy to a different machine), the daemon
detects the drift on startup and refuses to run rather than emit
garbage decoded plaintext into SMTP / IMAP / API calls. Recovery
in that case is to restore `nightjar.conf.pre-migration.bak` (the
plaintext backup the migrator created on first run) and re-migrate.

**The pre-migration backup is plaintext.** It contains the
unobfuscated copies of every secret. The migrator leaves it
forever — it's the operator's job to delete it once they've
confirmed the migration worked. Do not back up `~/.config/nightjar/`
to anything you don't fully control until that file is gone.

---

## Running it yourself

This isn't packaged as a product, but the code is open and the
design is documented. Adapting it to a different machine, mail
provider, or tool surface should be tractable for someone
comfortable with Python. The pieces most likely to need rework if
it's forked:

- **Secrets** (SMTP password, IMAP password, HOTP/TOTP secret,
  Anthropic API key): in `~/.config/nightjar/secrets.toml`,
  obfuscated. See the "Secrets at rest" section above.
- **Non-secret config** (SMTP host, IMAP host, daemon paths, Claude
  model, security thresholds, scope registry, cost caps): in
  `~/.config/nightjar/nightjar.conf`.
- **Contacts**: one TOML file each in `~/.config/nightjar/contacts/`,
  one per correspondent, with their own scopes / inbox membership /
  rate limit / `auto_approve_notes` flag.
- **Notes**: one Markdown sidecar per contact in
  `~/nightjar/contacts/<contact_id>.md` (default location;
  configurable). Human-readable, version-controllable, with
  per-section scope tags.
- **The verb surface** in `daemon/executor.py` and
  `daemon/principal_handlers.py`: tier-tagged operations the
  principal can request. Adding a new verb means adding it here,
  not exposing a tool to the LLM.
- **The system prompts** in `prompts/`: `triage_default.md`
  (contact triage, scope classification, note proposals),
  `principal_interpret.md` (free-form interpret pass), and
  `common.md`. Tuned for the operator's correspondents and norms;
  worth re-reading and adapting before pointing it at a different
  inbox.
- **The closed-circuit testing harness** in `tools/sim_harness.py`:
  drives the classifier and triage end-to-end without an API call,
  by routing each LLM hop through a Claude Code sub-agent. Useful
  for regression sweeps and red-team probes without touching the
  inbox or the spend ledger.

Read all of [DESIGN.md](./DESIGN.md) first, particularly the
threat model, principal authentication, and safeguarding sections,
because the security and ethical posture described above is
enforced by specific code paths that can be weakened by careless
edits.

---

## License

All rights reserved until v1. See [LICENSE](./LICENSE) for the full
text. The intent is to flip to a permissive open-source license
(likely MIT) once the implementation is far enough along to be
useful to other people.
