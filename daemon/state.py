"""SQLite state layer for Nightjar.

For Build Step 1 (watcher only), this initialises only the schema needed
to record observed messages: the messages table itself, the
daemon_heartbeat for cold-start detection, and a transitions audit log.

Future build steps add: principal_commands, principal_sessions,
used_totp_codes, auth_failures, daemon_state, rate_buckets,
contact_state, credit_ledger, pending_audits, cold_start_backlog.

The full schema lives in DESIGN.md "State persistence" and will be
introduced incrementally.
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
            cur = conn.execute("SELECT version FROM schema_version")
            row = cur.fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (1)")

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
