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
   │ DMARC + TOTP check  │       │  Contact directory   │
   │  (pure Python,      │       │  + rate-limit check  │
   │   pre-LLM)          │       │   (pure Python)      │
   │                     │       │                      │
   │  fail = halt if     │       │  fail = log in       │
   │  threshold reached  │       │  digest, drop        │
   └──────────┬──────────┘       └──────────┬───────────┘
              │                              │
        pass  ▼                              ▼  pass
   ┌─────────────────────┐       ┌──────────────────────┐
   │  Deterministic      │       │  TRIAGE LLM call     │
   │  command parse      │       │  (read-only tools,   │
   │  (no LLM)           │       │   produces a plan)   │
   │                     │       │                      │
   │  recognised verb    │       └──────────┬───────────┘
   │  → tier-based path  │                  │
   │                     │                  ▼
   │  unrecognised       │       ┌──────────────────────┐
   │  → principal-       │       │  Plan emailed to     │
   │     interpret pass  │       │  operator for        │
   └──────────┬──────────┘       │  approval            │
              │                  └──────────┬───────────┘
              ▼                              │
   ┌─────────────────────┐                   │
   │  Tier-gated action  │◄──────────────────┘
   │  (1: auto-execute,  │  (operator replies, also
   │   2-3: approval,    │   TOTP-authenticated)
   │   4: double-confirm)│
   └──────────┬──────────┘
              │
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
  Approval is a deterministic email reply parsed by Python regex,
  not an LLM round-trip, so approving can't be tricked and can't
  cost tokens.
- **Email is the only interaction surface.** No web UI, no Slack,
  no chat. Email's weaknesses (slow, threaded, asynchronous) are
  exactly the right frame for an assistant whose job is to *not*
  rush.
- **Pull-based throughout.** Nothing in this system can be
  triggered from the internet. The daemon reads its own inbox; it
  cannot be poked into action by a webhook or pushed instructions
  by an external service.
- **Caps everywhere.** Hourly invocation cap, per-call token cap,
  per-contact rate limit, daily spend cap on the API console. The
  daemon is incapable of running away.
- **Self-contained.** No runtime dependency on other software the
  user happens to be running. One project, one config, one process.

### Security

- **Per-email cryptographic authentication for the operator.**
  Every email from the operator must carry a valid TOTP code
  (RFC 6238, the same primitive as Google Authenticator and
  equivalent FOSS apps). The TOTP seed lives on the operator's
  phone and on the machine's encrypted config; it never enters any
  LLM context. The check is pure Python, runs before any LLM call,
  and the LLM has no API to bypass it.
- **Dead-man's-switch on authentication failure.** Three invalid
  attempts within an hour halt the daemon entirely. Recovery
  requires physical presence at the machine *and* a valid TOTP
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

### Ethical

- **Memory is bounded by the work.** Nightjar can keep brief notes
  per correspondent (what they're working on, how they prefer to
  communicate, what's specific to the collaboration). It is
  forbidden by its system prompt from noting health, financial,
  family, relational, or third-party-confidential information,
  even when the correspondent shares it freely. These aren't
  Nightjar's facts to remember.
- **Notes pass three tests or they don't get written.** Every
  proposed note must be readable by its subject without surprise,
  must be a professional observation about the work rather than a
  personal one about the person, and must respect the subject's
  dignity in the relationship being recorded. Anything that fails
  any test is flagged for human review and not written.
- **Correspondents can read, erase, and opt out at any time,
  without asking.** Every email Nightjar sends has a footer
  explaining how. `show my notes` returns the notes file, `delete
  my data` wipes it, `stop contacting me` blocks further outbound.
  These commands are parsed by the daemon, not the LLM. They
  cannot fail because the model misunderstood them, and they don't
  depend on the operator approving.
- **Disclosure, never concealment.** A new correspondent's first
  email from Nightjar is a one-time disclosure: who Nightjar is,
  what it's used for, where data goes, what their rights are.
  Until they reply acknowledging, no further mail is sent. The
  footer reminds them on every subsequent message.
- **No covert observation.** Nightjar will not be configured to
  read mail from people who haven't been told it's reading mail.

---

## Threat model

A serious project should name what it's defending against. The
[DESIGN.md](./DESIGN.md) threat-model section enumerates this in
detail; the short version:

- **Contact-content prompt injection.** Defended by the
  data-not-instructions rule and the architectural constraint that
  contact-driven actions are capped at reversible tiers, by code,
  with no agent path to escalate.
- **Sender-address impersonation.** Defended by DMARC verification
  on inbound mail.
- **Operator email account compromise.** Defended by per-email
  TOTP. An attacker with the email account but not the TOTP seed
  cannot authenticate.
- **Indirect injection across LLM calls.** Defended by treating
  every cross-call data flow as content, not control. Outputs of
  earlier LLM calls are never inputs as commands to later ones.
- **Disclosure of secrets via the LLM.** Defended by never giving
  the LLM the secrets in the first place. The system prompt
  refuses to discuss authentication, but the load-bearing defence
  is that there's nothing to disclose.
- **Disk compromise of the operator's machine.** Defended in v1 by
  filesystem permissions; in hardening, by encrypting the TOTP
  seed at rest and gating tier-5 actions on a hardware key.
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

**The first-contact ceremony is a fait accompli, not consent.** A
new correspondent learns Nightjar exists when they receive their
first email from it. Yes, they can opt out immediately. Yes, the
disclosure is clear. But they didn't choose to be in this
conversation in the way the operator chose to put them in it. The
asymmetry is real. Mitigation: Nightjar holds all subsequent
outbound until they reply `ok proceed`, so the *only* email they
ever receive without explicit consent is the disclosure itself.
It's the smallest fait accompli that could be engineered, but
it's still one.

**Notes are based on inferences the correspondent doesn't preview.**
The footer says "see what's been noted about you," which is honest
about *what* exists, but a correspondent has no preview of what
kinds of things tend to be noted before they are. The three-test
rule is a strong constraint and the categorical refusals are a
strong floor, but the correspondent is still trusting the
operator's judgement about what their assistant chooses to
remember. The defaults ship strict and the public posture ships
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

**Authentication closes most of the door, not all of it.** TOTP
defends against email-account compromise, which is the most
realistic attack. It does not defend against simultaneous
compromise of the operator's email *and* their phone, or against
filesystem-level compromise of the machine itself. v1 accepts this;
hardening upgrades (encrypted seed at rest, hardware-key
authentication for the highest-risk operations) are documented as
explicit next steps.

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

v0 design complete. Implementation underway, following the build
sequence at the bottom of [DESIGN.md](./DESIGN.md). Each step
produces a working system; the project becomes more capable in
stages rather than appearing all at once.

The first three build steps establish the security perimeter
(watcher, then authentication and dead-man's-switch, then notifier
with audit copy and footer) before any LLM is wired in. By design.

**Build Step 1 (watcher only): complete.** The daemon connects
to IMAP via IDLE, processes incoming mail, looks up senders against
the contact directory, and persists message metadata to SQLite.
No LLM, no outbound, no authentication on principal mail yet. 11
unit tests cover config parsing and state persistence; the IDLE
loop will be validated against a live Gmail inbox over several days
of real sleep/wake cycles before Step 2 begins.

This README will be updated as the implementation lands. Where
the design and the code disagree, the **code is right and the
design should be updated to match**. `DESIGN.md` is a working
document, not a contract carved in stone.

---

## Stack

- Python 3.13, mostly stdlib.
- `claude-agent-sdk` for the LLM-side agent loop. Default model:
  Haiku 4.5.
- `aioimaplib` for IMAP IDLE on Python 3.13. (Drops out when
  Python 3.14's stdlib IDLE support reaches the deployment target.)
- SQLite for state persistence.
- SMTP via stdlib `smtplib` for outbound. No third-party send
  service.
- `hmac` + `secrets` (stdlib) for TOTP. No external auth library.
- Runs on a Steam Deck (immutable Arch). User-level installation
  only.

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

- **Secrets** (SMTP password, IMAP password, TOTP secret, Anthropic
  API key): in `~/.config/nightjar/secrets.toml`, obfuscated. See
  the "Secrets at rest" section above.
- **Non-secret config** (SMTP host, IMAP host, daemon paths, Claude
  model, security thresholds): in `~/.config/nightjar/nightjar.conf`.
- **Contacts**: one TOML file each in `~/.config/nightjar/contacts/`.
- **The tool surface** in `tools/`: Nightjar's tools are scoped per
  inbox via named bundles. The example tools in this repo are
  specific to the operator's projects; a fork would write its own.
- **The system prompts** in `prompts/`: tuned for the operator's
  correspondents and norms. Worth re-reading and adapting before
  pointing it at a different inbox.

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
