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
            cur = conn.execute("SELECT version FROM schema_version")
            row = cur.fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (2)")
            elif row["version"] < 2:
                conn.execute("UPDATE schema_version SET version = 2")

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
