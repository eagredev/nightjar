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
