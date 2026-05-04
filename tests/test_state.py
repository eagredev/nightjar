"""SQLite state layer tests."""
from __future__ import annotations

from pathlib import Path

from daemon.state import State


def make_state(tmp_path: Path) -> State:
    return State(db_path=tmp_path / "state.db")


def test_record_message_inserts_once(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    inserted = s.record_message(
        message_id="<a@example.com>",
        inbox="nightjar",
        from_addr="someone@example.com",
        subject="hello",
        contact_id="composer",
        state="RECEIVED",
    )
    assert inserted is True
    assert s.message_exists("<a@example.com>")

    # Second insert with same Message-ID is a no-op.
    inserted_again = s.record_message(
        message_id="<a@example.com>",
        inbox="nightjar",
        from_addr="someone@example.com",
        subject="hello again",
        contact_id="composer",
        state="RECEIVED",
    )
    assert inserted_again is False


def test_count_by_state(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    for i, state in enumerate(["RECEIVED", "RECEIVED", "DROPPED", "TRIAGED"]):
        s.record_message(
            message_id=f"<m{i}@example.com>",
            inbox="nightjar",
            from_addr="x@example.com",
            subject=None,
            contact_id=None,
            state=state,
        )
    counts = s.count_by_state()
    assert counts == {"RECEIVED": 2, "DROPPED": 1, "TRIAGED": 1}


def test_transition_records_audit(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.record_message(
        message_id="<x@example.com>",
        inbox="nightjar",
        from_addr="x@example.com",
        subject=None,
        contact_id=None,
        state="RECEIVED",
    )
    s.transition(
        message_id="<x@example.com>",
        from_state="RECEIVED",
        to_state="TRIAGED",
        detail="mock triage",
    )
    counts = s.count_by_state()
    assert counts == {"TRIAGED": 1}


def test_heartbeat(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.last_heartbeat() is None
    s.heartbeat(ts=1_700_000_000)
    s.heartbeat(ts=1_700_000_060)
    assert s.last_heartbeat() == 1_700_000_060


# --- Auth state (Build Step 2) ---------------------------------------------


def test_panic_starts_clear(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.is_panicked() is False
    assert s.panic_info() is None


def test_trip_panic_persists_reason_and_at(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.trip_panic(reason="3 invalid TOTP attempts", at=1_700_000_000)
    assert s.is_panicked()
    info = s.panic_info()
    assert info is not None
    assert info["reason"] == "3 invalid TOTP attempts"
    assert info["at"] == 1_700_000_000


def test_clear_panic_resets_state(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.trip_panic(reason="x", at=1)
    s.clear_panic()
    assert s.is_panicked() is False
    assert s.panic_info() is None


def test_used_totp_codes_are_idempotent(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.totp_code_was_used("123456") is False
    assert s.mark_totp_code_used("123456", at=1_700_000_000) is True
    assert s.totp_code_was_used("123456") is True
    # Replaying the same code returns False.
    assert s.mark_totp_code_used("123456", at=1_700_000_001) is False


def test_prune_used_totp_codes_drops_old_rows(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.mark_totp_code_used("111111", at=1_000)
    s.mark_totp_code_used("222222", at=2_000)
    s.mark_totp_code_used("333333", at=3_000)
    deleted = s.prune_used_totp_codes(before=2_500)
    assert deleted == 2
    assert s.totp_code_was_used("111111") is False
    assert s.totp_code_was_used("222222") is False
    assert s.totp_code_was_used("333333") is True


def test_auth_failures_count_within_window(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.record_auth_failure(from_addr="p@example.com", reason="bad_totp_code", at=1_000)
    s.record_auth_failure(from_addr="p@example.com", reason="no_totp_code", at=2_000)
    s.record_auth_failure(from_addr="p@example.com", reason="totp_replay", at=3_000)
    assert s.count_auth_failures_since(0) == 3
    assert s.count_auth_failures_since(1_500) == 2
    assert s.count_auth_failures_since(3_001) == 0


def test_recent_auth_failures_orders_newest_first(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.record_auth_failure(from_addr="p@example.com", reason="r1", at=1_000)
    s.record_auth_failure(from_addr="p@example.com", reason="r2", at=2_000)
    s.record_auth_failure(from_addr="p@example.com", reason="r3", at=3_000)
    rows = s.recent_auth_failures(limit=2)
    assert [r["reason"] for r in rows] == ["r3", "r2"]


def test_hotp_counter_starts_at_zero(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.get_hotp_counter() == 0


def test_hotp_counter_round_trips(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.set_hotp_counter(42)
    assert s.get_hotp_counter() == 42
    s.set_hotp_counter(0)
    assert s.get_hotp_counter() == 0


def test_hotp_counter_rejects_negative(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        s.set_hotp_counter(-1)


# --- Pending audits (Build Step 3) -----------------------------------------


def test_pending_audits_starts_empty(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.count_pending_audits() == 0
    assert s.list_pending_audits() == []


def test_queue_audit_inserts_with_attempts_one(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    audit_id = s.queue_audit(
        primary_message_id="<msg1@nightjar>",
        to_addr="dylanmoir97@gmail.com",
        subject="[Nightjar Audit] To composer@example.com, hello",
        body="(audit body)",
        first_error="connection refused",
        at=1_700_000_000,
    )
    assert audit_id > 0
    rows = s.list_pending_audits()
    assert len(rows) == 1
    row = rows[0]
    assert row["to_addr"] == "dylanmoir97@gmail.com"
    assert row["attempts"] == 1
    assert row["last_error"] == "connection refused"


def test_mark_audit_attempt_success_deletes_row(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    audit_id = s.queue_audit(
        primary_message_id=None,
        to_addr="x@example.com",
        subject="x",
        body="x",
    )
    s.mark_audit_attempt(audit_id=audit_id, success=True)
    assert s.count_pending_audits() == 0


def test_mark_audit_attempt_failure_increments_attempts(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    audit_id = s.queue_audit(
        primary_message_id=None,
        to_addr="x@example.com",
        subject="x",
        body="x",
    )
    s.mark_audit_attempt(audit_id=audit_id, success=False, error="timeout")
    rows = s.list_pending_audits()
    assert rows[0]["attempts"] == 2
    assert rows[0]["last_error"] == "timeout"


def test_list_pending_audits_filters_exhausted(tmp_path: Path) -> None:
    """Audits at or above max_attempts are excluded from the retry queue."""
    s = make_state(tmp_path)
    audit_id = s.queue_audit(
        primary_message_id=None, to_addr="x@example.com", subject="x", body="x"
    )
    # Fail twice more to land at attempts=3 (the default max).
    s.mark_audit_attempt(audit_id=audit_id, success=False, error="e1")
    s.mark_audit_attempt(audit_id=audit_id, success=False, error="e2")
    assert s.list_pending_audits() == []  # exhausted; not in retry queue
    assert s.count_pending_audits() == 1  # but the row stays for diagnostics
