"""SQLite state layer for Nightjar.

Build Step 1 set up: messages, transitions, daemon_heartbeat.
Build Step 2 adds: daemon_state (panic flag), used_totp_codes (replay
protection), auth_failures (sliding-window counter for the
dead-man's-switch).

Future build steps add: principal_commands, principal_sessions,
rate_buckets, contact_state, credit_ledger, pending_audits,
cold_start_backlog. The full schema lives in DESIGN.md.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    inbox           TEXT NOT NULL,
    contact_id      TEXT,
    from_addr       TEXT NOT NULL,
    subject         TEXT,
    received_at     INTEGER NOT NULL,
    state           TEXT NOT NULL,
    approval_token  TEXT,
    plan_json       TEXT,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_state   ON messages(state);
CREATE INDEX IF NOT EXISTS idx_messages_token   ON messages(approval_token);
CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id);

CREATE TABLE IF NOT EXISTS transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    at          INTEGER NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_transitions_message ON transitions(message_id);

CREATE TABLE IF NOT EXISTS daemon_heartbeat (
    ts INTEGER PRIMARY KEY
);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS daemon_state (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    panic_until_revived   INTEGER NOT NULL DEFAULT 0,
    panic_reason          TEXT,
    panic_at              INTEGER
);
INSERT OR IGNORE INTO daemon_state (id, panic_until_revived) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS used_totp_codes (
    code     TEXT PRIMARY KEY,
    used_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_used_totp_codes_used_at ON used_totp_codes(used_at);

CREATE TABLE IF NOT EXISTS auth_failures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    from_addr  TEXT NOT NULL,
    reason     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_failures_ts ON auth_failures(ts);
"""

# V3 adds the HOTP counter on daemon_state. Existing rows on V2 databases
# will gain the new column with default 0 via the ALTER below.
SCHEMA_V3_ALTER_HOTP_COUNTER = (
    "ALTER TABLE daemon_state ADD COLUMN hotp_counter INTEGER NOT NULL DEFAULT 0"
)

# V9 adds the machine-id fingerprint on daemon_state. Stamped on first
# start after secrets migration; checked on every subsequent start.
# A mismatch means /etc/machine-id has changed since secrets were
# obfuscated and the secrets file is no longer decodable on this
# machine. NULL until migration runs.
SCHEMA_V9_ALTER_MACHINE_ID_FP = (
    "ALTER TABLE daemon_state ADD COLUMN machine_id_fp TEXT"
)

# V4 adds pending_audits, the queue of audit copies that need a retry. The
# daemon writes to this table when an audit-copy SMTP send fails after the
# primary already succeeded; a separate retry loop drains it. Fields are
# enough to reconstruct the audit copy in full so the retry doesn't depend
# on any other row staying around.
SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS pending_audits (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_message_id TEXT,
    to_addr           TEXT NOT NULL,
    subject           TEXT NOT NULL,
    body              TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   INTEGER,
    last_error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_audits_attempts ON pending_audits(attempts);
"""

# Audit retry policy. The daemon retries up to MAX_AUDIT_ATTEMPTS times
# before giving up; the row is left in place so the principal can see it
# in the diagnostic surface and decide whether to resend manually.
MAX_AUDIT_ATTEMPTS = 3

# V5 adds approvals, the queue of tier-2+ verbs awaiting principal
# confirmation. Each row pins one inbound principal-command message to an
# action that's been parsed but not executed. The token is the public
# handle that appears in [Nightjar #abc123] and lets the principal's
# reply route back to the right pending action without us needing
# threading. State lifecycle: PENDING -> APPROVED | DENIED | EXPIRED.
SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS approvals (
    token            TEXT PRIMARY KEY,
    message_id       TEXT NOT NULL,
    verb             TEXT NOT NULL,
    args_json        TEXT NOT NULL,
    tier             INTEGER NOT NULL,
    state            TEXT NOT NULL,
    created_at       INTEGER NOT NULL,
    expires_at       INTEGER NOT NULL,
    resolved_at      INTEGER,
    resolved_detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state);
CREATE INDEX IF NOT EXISTS idx_approvals_expires ON approvals(expires_at);
"""

# How long an approval ping is honoured before expiring. Matches the
# default mentioned in DESIGN.md ("approval_window_days = 7"). Counted
# in seconds so the call site does not have to convert; a future config
# read can override per principal.
DEFAULT_APPROVAL_WINDOW_SECONDS = 7 * 24 * 60 * 60

# V6 adds contact_blocks: per-contact block flag set by the `block`
# tier-2 verb and cleared by `unblock`. Holding the flag here (rather
# than in nightjar.conf) keeps the verb fully reversible without
# touching the config file. The watcher consults this table on inbound
# mail and treats a blocked contact as DROPPED.
SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS contact_blocks (
    contact_id  TEXT PRIMARY KEY,
    blocked_at  INTEGER NOT NULL,
    reason      TEXT
);
"""

# V7 adds claude_invocations, the spend ledger for triage and (later)
# principal-command interpretation calls. Two functions of the table:
#   1. Rate limit. count_claude_invocations_since() backs the in-daemon
#      runaway-loop guard. The first line of cost defence is the
#      Anthropic console spend cap; this table is the second.
#   2. Audit trail. Every call logs sender, model, token counts. An
#      operator inspecting "what did Claude do today" reads here.
# The api_key is NEVER recorded. Neither is the prompt nor the response.
# Only metadata: who triggered the call, what model, how many tokens,
# whether it ended ok or with an error reason.
SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS claude_invocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    purpose         TEXT NOT NULL,
    contact_id      TEXT,
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    ok              INTEGER NOT NULL,
    error_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_claude_invocations_ts ON claude_invocations(ts);
CREATE INDEX IF NOT EXISTS idx_claude_invocations_contact ON claude_invocations(contact_id);
"""

# V8 adds outbound_log, the consolidated audit register of every email
# Nightjar sends. Two functions:
#   1. Verifiable record. The principal can answer "what did Nightjar
#      say to who" without trawling JSONL. Every send is one row.
#   2. Forensic surface. After-the-fact review of an incident: did the
#      audit copy reach the principal? When was the SMTP failure?
#      What body did Nightjar send to whom?
# Body is stored verbatim. The api_key, HOTP secret, SMTP password are
# never in any rendered email body, so this table cannot leak them
# absent an upstream bug. notify_principal sends nothing sensitive
# (it's the principal's own outbound channel); send_to_contact also
# sends nothing sensitive (the LLM is prompted to never produce
# sensitive content, and the operator approves before any send).
SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS outbound_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  INTEGER NOT NULL,
    channel             TEXT NOT NULL,
    to_addr             TEXT NOT NULL,
    subject             TEXT NOT NULL,
    body                TEXT NOT NULL,
    smtp_message_id     TEXT,
    related_message_id  TEXT,
    ok                  INTEGER NOT NULL,
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbound_log_ts ON outbound_log(ts);
CREATE INDEX IF NOT EXISTS idx_outbound_log_channel ON outbound_log(channel);
CREATE INDEX IF NOT EXISTS idx_outbound_log_related ON outbound_log(related_message_id);
"""

# V10 adds the per-inbox catchup watermark. Step 6e (receipt reliability)
# stops trusting IMAP \Seen as the "have I processed this?" signal and
# uses Message-ID dedup against the messages table instead. The watermark
# bounds the IMAP search window so we don't re-walk the whole mailbox on
# every catchup. NULL last_catchup_at means "never run" — the watcher
# uses a wider first-run window and emits a reconciliation summary.
SCHEMA_V10 = """
CREATE TABLE IF NOT EXISTS inbox_state (
    name             TEXT PRIMARY KEY,
    last_catchup_at  INTEGER
);
"""

# V11 adds note_proposals: triage's queue of proposed additions to a
# contact's rapport-notes file. Step 7a ships the table and accessors;
# the triage emission and approval flow that fill it land in 7b/7d.
# Status lifecycle: pending -> approved -> applied (terminal) | rejected
# (terminal) | expired (terminal). Applied means the .md file has been
# updated; rejected means the principal said no; expired means the TTL
# elapsed without a decision.
SCHEMA_V11 = """
CREATE TABLE IF NOT EXISTS note_proposals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id       TEXT NOT NULL,
    proposed_at      INTEGER NOT NULL,
    scope            TEXT,
    section_heading  TEXT NOT NULL,
    body             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    expires_at       INTEGER NOT NULL,
    approved_at      INTEGER,
    applied_at       INTEGER,
    rejected_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_note_proposals_contact ON note_proposals(contact_id);
CREATE INDEX IF NOT EXISTS idx_note_proposals_status ON note_proposals(status);
CREATE INDEX IF NOT EXISTS idx_note_proposals_expires ON note_proposals(expires_at);
"""

# Default TTL for a pending note proposal. Long enough to ride out a
# few days of operator absence; short enough that an old "principal
# never approved or rejected" doesn't pile up indefinitely.
DEFAULT_NOTE_PROPOSAL_TTL_SECONDS = 14 * 24 * 60 * 60

# How long a used TOTP code is remembered. Codes outside the verification
# window (±30s) cannot succeed anyway, so 90s of replay-protection memory
# is plenty.
TOTP_REPLAY_RETENTION_SECONDS = 90


class State:
    """Thin wrapper around sqlite3 with the connection lifecycle managed.

    Connections are short-lived per write (the daemon doesn't run hot
    enough to need pooling); reads use a separate connection per call.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA_V1)
            conn.executescript(SCHEMA_V2)
            # V3: add hotp_counter to daemon_state. Idempotent because we
            # check the column list before issuing the ALTER.
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(daemon_state)")}
            if "hotp_counter" not in cols:
                conn.execute(SCHEMA_V3_ALTER_HOTP_COUNTER)
            # V4: pending_audits table. CREATE IF NOT EXISTS makes this
            # idempotent without a separate column-presence check.
            conn.executescript(SCHEMA_V4)
            # V5: approvals table. Same pattern as V4.
            conn.executescript(SCHEMA_V5)
            # V6: contact_blocks table. Same pattern.
            conn.executescript(SCHEMA_V6)
            # V7: claude_invocations table. Same pattern.
            conn.executescript(SCHEMA_V7)
            # V8: outbound_log table. Same pattern.
            conn.executescript(SCHEMA_V8)
            # V9: machine_id_fp on daemon_state. Idempotent ALTER.
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(daemon_state)")}
            if "machine_id_fp" not in cols:
                conn.execute(SCHEMA_V9_ALTER_MACHINE_ID_FP)
            # V10: inbox_state table for catchup watermark.
            conn.executescript(SCHEMA_V10)
            # V11: note_proposals table (Step 7a — rapport notes).
            conn.executescript(SCHEMA_V11)
            cur = conn.execute("SELECT version FROM schema_version")
            row = cur.fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (11)")
            elif row["version"] < 11:
                conn.execute("UPDATE schema_version SET version = 11")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def heartbeat(self, ts: int | None = None) -> None:
        ts = ts if ts is not None else int(time.time())
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO daemon_heartbeat (ts) VALUES (?)", (ts,))

    def last_heartbeat(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(ts) AS ts FROM daemon_heartbeat").fetchone()
            return row["ts"] if row and row["ts"] is not None else None

    def record_message(
        self,
        *,
        message_id: str,
        inbox: str,
        from_addr: str,
        subject: str | None,
        contact_id: str | None,
        state: str,
        received_at: int | None = None,
    ) -> bool:
        """Record an observed message. Returns True if newly inserted, False if already known.

        Idempotent: if the same Message-ID arrives twice (replay, IMAP
        glitch, sleep/wake), the second call is a no-op.
        """
        received_at = received_at if received_at is not None else int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (id, inbox, contact_id, from_addr, subject, received_at, state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, inbox, contact_id, from_addr, subject, received_at, state, received_at),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                """
                INSERT INTO transitions (message_id, from_state, to_state, at, detail)
                VALUES (?, NULL, ?, ?, ?)
                """,
                (message_id, state, received_at, "received"),
            )
            return True

    def transition(
        self,
        *,
        message_id: str,
        from_state: str,
        to_state: str,
        detail: str | None = None,
        at: int | None = None,
    ) -> None:
        at = at if at is not None else int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE messages SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
                (to_state, at, message_id, from_state),
            )
            conn.execute(
                """
                INSERT INTO transitions (message_id, from_state, to_state, at, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                (message_id, from_state, to_state, at, detail),
            )

    def message_exists(self, message_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM messages WHERE id = ?", (message_id,)).fetchone()
            return row is not None

    def count_by_state(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM messages GROUP BY state"
            ).fetchall()
            return {row["state"]: row["n"] for row in rows}

    # --- Auth state (Build Step 2) -----------------------------------------

    def is_panicked(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT panic_until_revived FROM daemon_state WHERE id = 1"
            ).fetchone()
            return bool(row and row["panic_until_revived"])

    def panic_info(self) -> dict | None:
        """Return panic_reason and panic_at if panicked, else None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT panic_until_revived, panic_reason, panic_at "
                "FROM daemon_state WHERE id = 1"
            ).fetchone()
            if not row or not row["panic_until_revived"]:
                return None
            return {"reason": row["panic_reason"], "at": row["panic_at"]}

    def trip_panic(self, *, reason: str, at: int | None = None) -> None:
        at = at if at is not None else int(time.time())
        with self._connect() as conn:
            conn.execute(
                "UPDATE daemon_state SET panic_until_revived = 1, "
                "panic_reason = ?, panic_at = ? WHERE id = 1",
                (reason, at),
            )

    def clear_panic(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE daemon_state SET panic_until_revived = 0, "
                "panic_reason = NULL, panic_at = NULL WHERE id = 1"
            )

    def get_hotp_counter(self) -> int:
        """Return the highest HOTP counter consumed so far. 0 means no codes used yet."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT hotp_counter FROM daemon_state WHERE id = 1"
            ).fetchone()
            return int(row["hotp_counter"]) if row else 0

    def set_hotp_counter(self, counter: int) -> None:
        """Set the HOTP counter. Called on (re)provision (resets to 0) and on accepted codes."""
        if counter < 0:
            raise ValueError("hotp_counter must be >= 0")
        with self._connect() as conn:
            conn.execute(
                "UPDATE daemon_state SET hotp_counter = ? WHERE id = 1",
                (counter,),
            )

    def get_machine_id_fp(self) -> str | None:
        """Return the stored machine-id fingerprint, or None if never set.

        Set on first daemon start after secrets migration; checked on
        every subsequent start. None means migration hasn't run yet
        (the daemon is on a pre-Step-6c install) or has just been
        wiped by a state.db reset.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT machine_id_fp FROM daemon_state WHERE id = 1"
            ).fetchone()
            if row is None:
                return None
            val = row["machine_id_fp"]
            return val if val else None

    def set_machine_id_fp(self, fp: str) -> None:
        """Stamp the machine-id fingerprint. Called by the migrator on
        first run after writing secrets.toml. Subsequent calls overwrite
        (e.g. operator re-runs migration after a deliberate machine-id
        change)."""
        if not fp:
            raise ValueError("machine_id_fp must be a non-empty string")
        with self._connect() as conn:
            conn.execute(
                "UPDATE daemon_state SET machine_id_fp = ? WHERE id = 1",
                (fp,),
            )

    def get_last_catchup_at(self, inbox: str) -> int | None:
        """Return the watermark timestamp for `inbox`, or None if never set.

        None means the catchup loop has never completed for this inbox
        on this database — the watcher treats this as a first-run
        condition and uses a wider initial search window plus a one-shot
        reconciliation summary to the principal.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_catchup_at FROM inbox_state WHERE name = ?",
                (inbox,),
            ).fetchone()
            if row is None:
                return None
            val = row["last_catchup_at"]
            return int(val) if val is not None else None

    def set_last_catchup_at(self, inbox: str, ts: int) -> None:
        """Advance the watermark for `inbox` to `ts`.

        Called after every successful catchup pass. The watcher uses
        `max(now - catchup_window_days, watermark - 1d)` as the lower
        bound of the IMAP search; the 1-day overlap absorbs clock skew
        and mid-flight crashes.
        """
        if ts < 0:
            raise ValueError("last_catchup_at must be >= 0")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO inbox_state (name, last_catchup_at) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET last_catchup_at = excluded.last_catchup_at",
                (inbox, ts),
            )

    def totp_code_was_used(self, code: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM used_totp_codes WHERE code = ?", (code,)
            ).fetchone()
            return row is not None

    def mark_totp_code_used(self, code: str, at: int | None = None) -> bool:
        """Insert the code as used. Returns True if new, False if replay."""
        at = at if at is not None else int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO used_totp_codes (code, used_at) VALUES (?, ?)",
                (code, at),
            )
            return cur.rowcount > 0

    def prune_used_totp_codes(self, *, before: int | None = None) -> int:
        """Drop codes older than `before` (default: now - retention). Returns rows deleted."""
        cutoff = before if before is not None else int(time.time()) - TOTP_REPLAY_RETENTION_SECONDS
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM used_totp_codes WHERE used_at < ?", (cutoff,))
            return cur.rowcount

    def record_auth_failure(self, *, from_addr: str, reason: str, at: int | None = None) -> None:
        at = at if at is not None else int(time.time())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO auth_failures (ts, from_addr, reason) VALUES (?, ?, ?)",
                (at, from_addr, reason),
            )

    def count_auth_failures_since(self, since: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM auth_failures WHERE ts >= ?", (since,)
            ).fetchone()
            return int(row["n"]) if row else 0

    def recent_auth_failures(self, *, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, from_addr, reason FROM auth_failures "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    # --- Pending audits (Build Step 3) -------------------------------------

    def queue_audit(
        self,
        *,
        primary_message_id: str | None,
        to_addr: str,
        subject: str,
        body: str,
        first_error: str | None = None,
        at: int | None = None,
    ) -> int:
        """Insert a row representing a failed audit copy that needs retry.

        `attempts` starts at 1 because we count the original failed
        attempt. Returns the new row id.
        """
        at = at if at is not None else int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO pending_audits (
                    primary_message_id, to_addr, subject, body,
                    created_at, attempts, last_attempt_at, last_error
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (primary_message_id, to_addr, subject, body, at, at, first_error),
            )
            return int(cur.lastrowid)

    def list_pending_audits(self, *, max_attempts: int = MAX_AUDIT_ATTEMPTS) -> list[dict]:
        """Return audits still under the retry budget, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, primary_message_id, to_addr, subject, body, "
                "       created_at, attempts, last_attempt_at, last_error "
                "FROM pending_audits "
                "WHERE attempts < ? "
                "ORDER BY created_at ASC",
                (max_attempts,),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_audit_attempt(
        self,
        *,
        audit_id: int,
        success: bool,
        error: str | None = None,
        at: int | None = None,
    ) -> None:
        """Record a retry outcome.

        On success the row is deleted (audit is delivered, no further
        action). On failure attempts is incremented and the error is
        stored; the row stays for the next retry pass or for principal
        diagnostic review if the attempt budget is exhausted.
        """
        at = at if at is not None else int(time.time())
        with self._connect() as conn:
            if success:
                conn.execute("DELETE FROM pending_audits WHERE id = ?", (audit_id,))
            else:
                conn.execute(
                    "UPDATE pending_audits "
                    "SET attempts = attempts + 1, last_attempt_at = ?, last_error = ? "
                    "WHERE id = ?",
                    (at, error, audit_id),
                )

    def count_pending_audits(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM pending_audits"
            ).fetchone()
            return int(row["n"]) if row else 0

    # --- Approvals (Build Step 4b) -----------------------------------------

    def queue_approval(
        self,
        *,
        token: str,
        message_id: str,
        verb: str,
        args: dict,
        tier: int,
        at: int | None = None,
        window_seconds: int = DEFAULT_APPROVAL_WINDOW_SECONDS,
    ) -> None:
        """Insert a PENDING approval row tied to a parsed verb.

        The token is the public handle the principal will see in the
        ping subject; we generate it at the call site so the call site
        also owns its uniqueness check.
        """
        at = at if at is not None else int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    token, message_id, verb, args_json, tier,
                    state, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    token, message_id, verb, json.dumps(args, sort_keys=True),
                    tier, at, at + window_seconds,
                ),
            )

    def get_approval(self, token: str) -> dict | None:
        """Fetch a single approval by token. Returns None if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT token, message_id, verb, args_json, tier, state, "
                "       created_at, expires_at, resolved_at, resolved_detail "
                "FROM approvals WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["args"] = json.loads(d["args_json"])
            return d

    def resolve_approval(
        self,
        *,
        token: str,
        outcome: str,
        detail: str | None = None,
        at: int | None = None,
    ) -> bool:
        """Move a PENDING approval to APPROVED, DENIED, or EXPIRED.

        Returns True if a PENDING row was resolved, False otherwise (no
        such token, or already resolved). The conditional UPDATE makes
        this safe against double-resolution from a duplicated reply.
        """
        if outcome not in ("APPROVED", "DENIED", "EXPIRED"):
            raise ValueError(f"invalid outcome: {outcome!r}")
        at = at if at is not None else int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE approvals SET state = ?, resolved_at = ?, resolved_detail = ? "
                "WHERE token = ? AND state = 'PENDING'",
                (outcome, at, detail, token),
            )
            return cur.rowcount > 0

    def list_pending_approvals(self, *, now: int | None = None) -> list[dict]:
        """Return PENDING, non-expired approvals oldest-first.

        Rows whose expires_at has passed are excluded; expire_approvals
        is the helper that flips them to EXPIRED. We exclude here too so
        readers aren't forced to call expire first.
        """
        now = now if now is not None else int(time.time())
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT token, message_id, verb, args_json, tier, state, "
                "       created_at, expires_at, resolved_at, resolved_detail "
                "FROM approvals "
                "WHERE state = 'PENDING' AND expires_at > ? "
                "ORDER BY created_at ASC",
                (now,),
            ).fetchall()
            out = []
            for row in rows:
                d = dict(row)
                d["args"] = json.loads(d["args_json"])
                out.append(d)
            return out

    def count_pending_approvals(self, *, now: int | None = None) -> int:
        """Active pending count (excludes expired-but-unflipped rows)."""
        now = now if now is not None else int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM approvals "
                "WHERE state = 'PENDING' AND expires_at > ?",
                (now,),
            ).fetchone()
            return int(row["n"]) if row else 0

    def expire_approvals(self, *, now: int | None = None) -> int:
        """Flip PENDING approvals past their expires_at to EXPIRED.

        Returns the count of rows flipped. Called by the watcher's
        periodic housekeeping pass; safe to call any time.
        """
        now = now if now is not None else int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE approvals SET state = 'EXPIRED', resolved_at = ? "
                "WHERE state = 'PENDING' AND expires_at <= ?",
                (now, now),
            )
            return cur.rowcount

    # --- Contact blocks (Build Step 4b) -------------------------------------

    def block_contact(
        self,
        *,
        contact_id: str,
        reason: str | None = None,
        at: int | None = None,
    ) -> bool:
        """Mark a contact as blocked. Returns True if newly blocked,
        False if already blocked (idempotent)."""
        at = at if at is not None else int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO contact_blocks (contact_id, blocked_at, reason) "
                "VALUES (?, ?, ?)",
                (contact_id, at, reason),
            )
            return cur.rowcount > 0

    def unblock_contact(self, *, contact_id: str) -> bool:
        """Lift a contact's block. Returns True if a row was removed,
        False if the contact wasn't blocked."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM contact_blocks WHERE contact_id = ?",
                (contact_id,),
            )
            return cur.rowcount > 0

    def is_contact_blocked(self, contact_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM contact_blocks WHERE contact_id = ?",
                (contact_id,),
            ).fetchone()
            return row is not None

    def list_blocked_contacts(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT contact_id, blocked_at, reason "
                "FROM contact_blocks ORDER BY blocked_at ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    # ---- Claude invocation ledger (V7) -----------------------------------

    def record_claude_invocation(
        self,
        *,
        purpose: str,
        contact_id: str | None,
        model: str,
        input_tokens: int,
        output_tokens: int,
        ok: bool,
        error_reason: str | None = None,
        ts: int | None = None,
    ) -> int:
        """Append one row to the spend ledger. Returns the new row id.

        `purpose` is a short identifier like "triage" or
        "principal_interpret" so the ledger can be sliced by use case
        when reasoning about cost. Never includes prompt content.
        """
        ts = ts if ts is not None else int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO claude_invocations "
                "(ts, purpose, contact_id, model, input_tokens, "
                " output_tokens, ok, error_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts, purpose, contact_id, model,
                    int(input_tokens), int(output_tokens),
                    1 if ok else 0, error_reason,
                ),
            )
            return int(cur.lastrowid)

    def count_claude_invocations_since(self, *, since_ts: int) -> int:
        """How many calls have been recorded since the given timestamp.

        Used by the in-daemon rate limit. The watcher calls this with
        `since_ts = now - 3600` and refuses a triage call if the count
        is at or above the per-hour cap from [claude].
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM claude_invocations WHERE ts >= ?",
                (int(since_ts),),
            ).fetchone()
            return int(row["n"]) if row is not None else 0

    def list_recent_claude_invocations(self, *, limit: int = 50) -> list[dict]:
        """Most recent ledger rows, newest first. For diagnostic surfaces
        like `[code] tail spend` (lands in a later step)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, purpose, contact_id, model, "
                "       input_tokens, output_tokens, ok, error_reason "
                "FROM claude_invocations "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    # ---- Outbound log (V8) -----------------------------------------------

    def record_outbound(
        self,
        *,
        channel: str,
        to_addr: str,
        subject: str,
        body: str,
        smtp_message_id: str | None,
        related_message_id: str | None,
        ok: bool,
        error: str | None = None,
        ts: int | None = None,
    ) -> int:
        """Append one row to the outbound register. Returns the row id.

        `channel` is "notify_principal" or "send_to_contact" (or "audit"
        for the audit copy in send_to_contact). `related_message_id` is
        the inbound message that triggered the send, when applicable
        (None for daemon-initiated sends like panic notifications).

        Body is stored verbatim. The footer added by send_to_contact is
        included in the stored body because it's part of what the
        contact actually saw. The principal can grep their own audit
        log to find every send Nightjar made.
        """
        ts = ts if ts is not None else int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO outbound_log "
                "(ts, channel, to_addr, subject, body, "
                " smtp_message_id, related_message_id, ok, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts, channel, to_addr, subject, body,
                    smtp_message_id, related_message_id,
                    1 if ok else 0, error,
                ),
            )
            return int(cur.lastrowid)

    def list_recent_outbound(self, *, limit: int = 50) -> list[dict]:
        """Most recent outbound rows, newest first. For `tail outbound`
        diagnostic surfaces."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, channel, to_addr, subject, body, "
                "       smtp_message_id, related_message_id, ok, error "
                "FROM outbound_log "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def count_outbound_since(self, *, since_ts: int) -> int:
        """How many outbound sends in the window starting at since_ts.
        For future rate limits / digest counts."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM outbound_log WHERE ts >= ?",
                (int(since_ts),),
            ).fetchone()
            return int(row["n"]) if row is not None else 0

    # --- Status-report accessors (Step 6g) ---------------------------------

    def in_flight_messages(
        self,
        *,
        states: tuple[str, ...] = (
            "RECEIVED", "TRIAGE_FAILED",
            "INTERPRET_FAILED", "INTERPRET_SKIPPED",
        ),
        older_than_seconds: int | None = None,
        now: int | None = None,
    ) -> list[dict]:
        """Return mid-pipeline messages: ones the daemon has acknowledged
        but that haven't reached approval / responded / executed yet.

        Steady state should be empty. Non-empty means triage stalled,
        the daemon hit an error, or something is wedged. The status
        report uses this to surface stuck mail.

        `older_than_seconds` filters to messages whose `updated_at`
        is at least that old. Used to avoid flagging brand-new mail
        that's just mid-flight normally; passing 600 (10 min) is the
        usual call from the status report.
        """
        now = now if now is not None else int(time.time())
        placeholders = ",".join("?" for _ in states)
        params: list = list(states)
        sql = (
            f"SELECT id, inbox, contact_id, from_addr, subject, "
            f"       received_at, state, updated_at "
            f"FROM messages "
            f"WHERE state IN ({placeholders})"
        )
        if older_than_seconds is not None:
            sql += " AND updated_at <= ?"
            params.append(now - int(older_than_seconds))
        sql += " ORDER BY updated_at ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def expiring_approvals(
        self,
        *,
        within_seconds: int,
        now: int | None = None,
    ) -> list[dict]:
        """Pending approvals whose expires_at falls inside the next
        `within_seconds` window. The status report calls this with
        24 * 3600 to surface tickets about to vanish.

        Already-expired rows are excluded (those flip to EXPIRED via
        expire_approvals). Sort: soonest-expiring first."""
        now = now if now is not None else int(time.time())
        upper_bound = now + int(within_seconds)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT token, message_id, verb, args_json, tier, "
                "       created_at, expires_at "
                "FROM approvals "
                "WHERE state = 'PENDING' "
                "  AND expires_at > ? AND expires_at <= ? "
                "ORDER BY expires_at ASC",
                (now, upper_bound),
            ).fetchall()
            out = []
            for row in rows:
                d = dict(row)
                d["args"] = json.loads(d["args_json"])
                out.append(d)
            return out

    def list_message_ids_in_db(
        self, *, inbox: str | None = None,
    ) -> set[str]:
        """Return all Message-IDs the daemon has on file. Used by the
        status report's IMAP walk to find out-of-band mail (in IMAP
        but not in messages table). `inbox` filters to one inbox if
        the caller wants per-inbox dedup."""
        with self._connect() as conn:
            if inbox is not None:
                rows = conn.execute(
                    "SELECT id FROM messages WHERE inbox = ?",
                    (inbox,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT id FROM messages").fetchall()
            return {row["id"] for row in rows}

    def first_message_received_at(self) -> int | None:
        """Earliest received_at across the messages table, or None if
        empty. Used to identify 'beyond daemon's lifetime' messages
        in the out-of-band section."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(received_at) AS first_ts FROM messages"
            ).fetchone()
            if row is None or row["first_ts"] is None:
                return None
            return int(row["first_ts"])

    def last_successful_claude_invocation_at(self) -> int | None:
        """Most recent ok=1 row in claude_invocations. None if no
        successful call ever. Used in the daemon-health block."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(ts) AS last_ts FROM claude_invocations "
                "WHERE ok = 1"
            ).fetchone()
            if row is None or row["last_ts"] is None:
                return None
            return int(row["last_ts"])

    def last_outbound_sent_at(self) -> int | None:
        """Most recent ok=1 row in outbound_log. None if nothing sent
        yet. Health-block input."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(ts) AS last_ts FROM outbound_log "
                "WHERE ok = 1"
            ).fetchone()
            if row is None or row["last_ts"] is None:
                return None
            return int(row["last_ts"])

    # ---- Step 7a: note proposals ----------------------------------------

    def enqueue_note_proposal(
        self,
        *,
        contact_id: str,
        section_heading: str,
        body: str,
        scope: str | None,
        proposed_at: int,
        ttl_seconds: int = DEFAULT_NOTE_PROPOSAL_TTL_SECONDS,
    ) -> int:
        """Insert a pending note proposal. Returns the assigned id.

        TTL bounds how long the proposal can wait before expire_old_note_proposals
        sweeps it out. Default 14 days, overridable for tests."""
        expires_at = proposed_at + ttl_seconds
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO note_proposals
                    (contact_id, proposed_at, scope, section_heading,
                     body, status, expires_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    contact_id,
                    proposed_at,
                    scope,
                    section_heading,
                    body,
                    expires_at,
                ),
            )
            return int(cur.lastrowid or 0)

    def get_note_proposal(self, proposal_id: int) -> dict | None:
        """Fetch a single proposal by id; None if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM note_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_pending_note_proposals(
        self,
        *,
        now: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Pending proposals whose TTL has not yet elapsed, oldest first.

        Pass `now` to filter against the current time without relying
        on system clock; expired-but-still-pending rows are excluded
        (they should be swept by expire_old_note_proposals separately,
        but the read path also filters defensively)."""
        now = now if now is not None else int(time.time())
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM note_proposals
                WHERE status = 'pending' AND expires_at > ?
                ORDER BY proposed_at ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_note_proposals_for_contact(
        self,
        contact_id: str,
        *,
        statuses: tuple[str, ...] = ("pending", "approved", "applied"),
        limit: int = 100,
    ) -> list[dict]:
        """All proposals for a contact in the given statuses, newest
        first. Default omits rejected/expired so the principal sees
        the live picture."""
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM note_proposals
                WHERE contact_id = ? AND status IN ({placeholders})
                ORDER BY proposed_at DESC
                LIMIT ?
                """,
                (contact_id, *statuses, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_note_proposal_approved(
        self,
        proposal_id: int,
        *,
        now: int,
    ) -> bool:
        """Move pending -> approved. Returns True if the row transitioned;
        False if no such pending row (already resolved or absent)."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE note_proposals
                SET status = 'approved', approved_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, proposal_id),
            )
            return cur.rowcount > 0

    def mark_note_proposal_applied(
        self,
        proposal_id: int,
        *,
        now: int,
    ) -> bool:
        """Move approved -> applied (terminal). Records that the
        notes_store.append_note write actually succeeded; the principal
        now sees the proposal as done.

        Also transitions directly from pending if the contact has
        auto_approve_notes=true (the daemon skipped the approval step)."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE note_proposals
                SET status = 'applied', applied_at = ?
                WHERE id = ? AND status IN ('approved', 'pending')
                """,
                (now, proposal_id),
            )
            return cur.rowcount > 0

    def mark_note_proposal_rejected(
        self,
        proposal_id: int,
        *,
        now: int,
    ) -> bool:
        """Move pending -> rejected (terminal). Approved-but-not-yet-applied
        proposals can also be rejected if something blocks the apply
        (e.g. write failure that the operator decides to forget)."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE note_proposals
                SET status = 'rejected', rejected_at = ?
                WHERE id = ? AND status IN ('pending', 'approved')
                """,
                (now, proposal_id),
            )
            return cur.rowcount > 0

    def expire_old_note_proposals(self, *, now: int) -> int:
        """Move any pending proposals past their TTL to status=expired.
        Returns the count moved. Caller is the periodic sweep in
        inbox_watcher (or a test)."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE note_proposals
                SET status = 'expired'
                WHERE status = 'pending' AND expires_at <= ?
                """,
                (now,),
            )
            return cur.rowcount

    def count_pending_note_proposals(
        self,
        *,
        now: int | None = None,
    ) -> int:
        """How many proposals are currently waiting on the principal.
        Used by the status report's health block."""
        now = now if now is not None else int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM note_proposals
                WHERE status = 'pending' AND expires_at > ?
                """,
                (now,),
            ).fetchone()
            return int(row["n"]) if row else 0
