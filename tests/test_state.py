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


# --- Approvals (Build Step 4b) ---------------------------------------------


def test_approvals_starts_empty(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.list_pending_approvals() == []
    assert s.count_pending_approvals() == 0
    assert s.get_approval("nonexistent") is None


def test_queue_approval_persists_args_and_tier(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.queue_approval(
        token="abc123",
        message_id="<m1@example.com>",
        verb="block",
        args={"contact": "composer"},
        tier=2,
        at=1_700_000_000,
    )
    row = s.get_approval("abc123")
    assert row is not None
    assert row["verb"] == "block"
    assert row["args"] == {"contact": "composer"}
    assert row["tier"] == 2
    assert row["state"] == "PENDING"
    assert row["created_at"] == 1_700_000_000
    # Default 7-day window
    assert row["expires_at"] == 1_700_000_000 + 7 * 24 * 60 * 60


def test_resolve_approval_marks_approved(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.queue_approval(
        token="t1", message_id="<m@x>", verb="block",
        args={"contact": "c"}, tier=2,
    )
    assert s.resolve_approval(token="t1", outcome="APPROVED", detail="yes") is True
    row = s.get_approval("t1")
    assert row["state"] == "APPROVED"
    assert row["resolved_detail"] == "yes"
    # Already resolved: second resolve is a no-op.
    assert s.resolve_approval(token="t1", outcome="DENIED") is False


def test_resolve_approval_rejects_invalid_outcome(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.queue_approval(
        token="t1", message_id="<m@x>", verb="block",
        args={}, tier=2,
    )
    import pytest
    with pytest.raises(ValueError):
        s.resolve_approval(token="t1", outcome="MAYBE")


def test_resolve_approval_unknown_token_returns_false(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.resolve_approval(token="ghost", outcome="APPROVED") is False


def test_list_pending_approvals_orders_oldest_first(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.queue_approval(
        token="a", message_id="<1@x>", verb="block",
        args={}, tier=2, at=1_000,
    )
    s.queue_approval(
        token="b", message_id="<2@x>", verb="forget",
        args={}, tier=2, at=2_000,
    )
    rows = s.list_pending_approvals(now=2_500)
    assert [r["token"] for r in rows] == ["a", "b"]


def test_pending_approvals_excludes_resolved(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.queue_approval(
        token="a", message_id="<1@x>", verb="block",
        args={}, tier=2,
    )
    s.queue_approval(
        token="b", message_id="<2@x>", verb="forget",
        args={}, tier=2,
    )
    s.resolve_approval(token="a", outcome="APPROVED")
    rows = s.list_pending_approvals()
    assert [r["token"] for r in rows] == ["b"]
    assert s.count_pending_approvals() == 1


def test_expire_approvals_flips_past_expiry(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.queue_approval(
        token="old", message_id="<1@x>", verb="block",
        args={}, tier=2, at=1_000, window_seconds=10,
    )
    s.queue_approval(
        token="new", message_id="<2@x>", verb="block",
        args={}, tier=2, at=10_000, window_seconds=10,
    )
    flipped = s.expire_approvals(now=1_500)
    assert flipped == 1
    assert s.get_approval("old")["state"] == "EXPIRED"
    assert s.get_approval("new")["state"] == "PENDING"


def test_pending_approvals_excludes_expired_pre_flip(tmp_path: Path) -> None:
    """list_pending_approvals filters by expires_at without needing
    expire_approvals to have run yet -- so it's safe to read at any time."""
    s = make_state(tmp_path)
    s.queue_approval(
        token="old", message_id="<1@x>", verb="block",
        args={}, tier=2, at=1_000, window_seconds=10,
    )
    rows = s.list_pending_approvals(now=2_000)
    assert rows == []
    assert s.count_pending_approvals(now=2_000) == 0


# --- Contact blocks (Build Step 4b) ----------------------------------------


def test_contact_blocks_starts_empty(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.is_contact_blocked("composer") is False
    assert s.list_blocked_contacts() == []


def test_block_contact_is_idempotent(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.block_contact(contact_id="composer", at=1_000) is True
    assert s.block_contact(contact_id="composer", at=2_000) is False
    assert s.is_contact_blocked("composer") is True


def test_unblock_contact_returns_true_when_was_blocked(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.block_contact(contact_id="composer", at=1_000)
    assert s.unblock_contact(contact_id="composer") is True
    assert s.is_contact_blocked("composer") is False


def test_unblock_contact_returns_false_when_not_blocked(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.unblock_contact(contact_id="composer") is False


def test_list_blocked_contacts_orders_by_blocked_at(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.block_contact(contact_id="b", at=2_000)
    s.block_contact(contact_id="a", at=1_000)
    rows = s.list_blocked_contacts()
    assert [r["contact_id"] for r in rows] == ["a", "b"]


# ---- V7: claude_invocations ledger ----------------------------------------


def test_claude_ledger_starts_empty(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.count_claude_invocations_since(since_ts=0) == 0
    assert s.list_recent_claude_invocations() == []


def test_record_claude_invocation_returns_row_id(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    rid = s.record_claude_invocation(
        purpose="triage",
        contact_id="composer",
        model="claude-haiku-4-5",
        input_tokens=2400,
        output_tokens=180,
        ok=True,
        ts=1_000,
    )
    assert rid >= 1


def test_count_claude_invocations_within_window(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    # Three calls in the last hour, two more from yesterday.
    for ts in (1_000, 2_000, 3_000):
        s.record_claude_invocation(
            purpose="triage", contact_id="x", model="m",
            input_tokens=100, output_tokens=10, ok=True, ts=ts,
        )
    for ts in (10, 20):
        s.record_claude_invocation(
            purpose="triage", contact_id="x", model="m",
            input_tokens=100, output_tokens=10, ok=True, ts=ts,
        )
    # Window starts at 500, so only the three recent calls count.
    assert s.count_claude_invocations_since(since_ts=500) == 3


def test_claude_ledger_records_failures_separately(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.record_claude_invocation(
        purpose="triage", contact_id="x", model="m",
        input_tokens=100, output_tokens=0, ok=False,
        error_reason="sdk_error", ts=1_000,
    )
    rows = s.list_recent_claude_invocations()
    assert len(rows) == 1
    assert rows[0]["ok"] == 0
    assert rows[0]["error_reason"] == "sdk_error"


def test_claude_ledger_orders_newest_first(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.record_claude_invocation(
        purpose="triage", contact_id="x", model="m",
        input_tokens=10, output_tokens=1, ok=True, ts=1_000,
    )
    s.record_claude_invocation(
        purpose="triage", contact_id="y", model="m",
        input_tokens=10, output_tokens=1, ok=True, ts=3_000,
    )
    s.record_claude_invocation(
        purpose="triage", contact_id="z", model="m",
        input_tokens=10, output_tokens=1, ok=True, ts=2_000,
    )
    rows = s.list_recent_claude_invocations()
    assert [r["contact_id"] for r in rows] == ["y", "z", "x"]


def test_claude_ledger_respects_limit(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    for i in range(5):
        s.record_claude_invocation(
            purpose="triage", contact_id=f"c{i}", model="m",
            input_tokens=10, output_tokens=1, ok=True, ts=1_000 + i,
        )
    rows = s.list_recent_claude_invocations(limit=2)
    assert len(rows) == 2


def test_claude_ledger_does_not_store_prompt_or_key(tmp_path: Path) -> None:
    """The schema only has metadata columns. This test pins that
    contract by inspecting the row keys returned by the lister."""
    s = make_state(tmp_path)
    s.record_claude_invocation(
        purpose="triage", contact_id="x", model="m",
        input_tokens=10, output_tokens=1, ok=True, ts=1_000,
    )
    row = s.list_recent_claude_invocations()[0]
    forbidden_keys = {"api_key", "prompt", "system", "user", "response", "tool_input"}
    assert not (set(row.keys()) & forbidden_keys)


# ---- V8: outbound_log register --------------------------------------------


def test_outbound_log_starts_empty(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.list_recent_outbound() == []
    assert s.count_outbound_since(since_ts=0) == 0


def test_record_outbound_returns_row_id(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    rid = s.record_outbound(
        channel="notify_principal",
        to_addr="me@example.com",
        subject="hi",
        body="hello there",
        smtp_message_id="<abc@example.com>",
        related_message_id=None,
        ok=True,
        ts=1_000,
    )
    assert rid >= 1


def test_outbound_log_records_failure(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.record_outbound(
        channel="send_to_contact",
        to_addr="contact@example.com",
        subject="reply",
        body="text",
        smtp_message_id="<x@y>",
        related_message_id="<inbound@x>",
        ok=False,
        error="SMTPRecipientsRefused",
        ts=1_000,
    )
    rows = s.list_recent_outbound()
    assert len(rows) == 1
    assert rows[0]["ok"] == 0
    assert rows[0]["error"] == "SMTPRecipientsRefused"
    assert rows[0]["channel"] == "send_to_contact"


def test_outbound_log_orders_newest_first(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    for i, addr in enumerate(["a", "b", "c"]):
        s.record_outbound(
            channel="notify_principal", to_addr=f"{addr}@x", subject="s",
            body="b", smtp_message_id=None, related_message_id=None,
            ok=True, ts=1_000 + i,
        )
    rows = s.list_recent_outbound()
    assert [r["to_addr"] for r in rows] == ["c@x", "b@x", "a@x"]


def test_outbound_log_respects_limit(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    for i in range(5):
        s.record_outbound(
            channel="notify_principal", to_addr=f"{i}@x", subject="s",
            body="b", smtp_message_id=None, related_message_id=None,
            ok=True, ts=1_000 + i,
        )
    rows = s.list_recent_outbound(limit=2)
    assert len(rows) == 2


def test_count_outbound_since_window(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    for ts in (10, 20, 1_000, 2_000):
        s.record_outbound(
            channel="notify_principal", to_addr="x@y", subject="s",
            body="b", smtp_message_id=None, related_message_id=None,
            ok=True, ts=ts,
        )
    assert s.count_outbound_since(since_ts=500) == 2
    assert s.count_outbound_since(since_ts=0) == 4


def test_outbound_log_does_not_have_secret_columns(tmp_path: Path) -> None:
    """Pin contract: outbound_log stores body verbatim but no
    api_key / totp_secret / smtp_password columns. The body itself
    cannot contain secrets unless an upstream caller put them
    there - which would be the actual bug."""
    s = make_state(tmp_path)
    s.record_outbound(
        channel="notify_principal", to_addr="x@y", subject="s",
        body="body content", smtp_message_id=None,
        related_message_id=None, ok=True, ts=1_000,
    )
    row = s.list_recent_outbound()[0]
    forbidden = {"api_key", "totp_secret", "smtp_password", "password"}
    assert not (set(row.keys()) & forbidden)


def test_outbound_log_preserves_related_message_id(tmp_path: Path) -> None:
    """The link from outbound to the inbound mail that triggered it
    must round-trip so the audit trail can be joined."""
    s = make_state(tmp_path)
    s.record_outbound(
        channel="send_to_contact", to_addr="x@y", subject="s",
        body="b", smtp_message_id="<a@b>",
        related_message_id="<inbound-msgid@x>",
        ok=True, ts=1_000,
    )
    row = s.list_recent_outbound()[0]
    assert row["related_message_id"] == "<inbound-msgid@x>"


def test_outbound_log_body_is_searchable(tmp_path: Path) -> None:
    """Sanity: the body is stored verbatim and fully retrievable.
    This is the contract: the outbound register is the truth, not
    an LLM summary, not a redacted version."""
    s = make_state(tmp_path)
    full_body = "Hi composer,\n\nThanks for sending the file.\n\n--Footer goes here--\n"
    s.record_outbound(
        channel="send_to_contact", to_addr="x@y", subject="Re: file",
        body=full_body, smtp_message_id="<a@b>",
        related_message_id=None, ok=True, ts=1_000,
    )
    row = s.list_recent_outbound()[0]
    assert row["body"] == full_body


# --- Step 6e: catchup watermark (inbox_state) ------------------------------


def test_get_last_catchup_at_returns_none_when_unset(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    assert s.get_last_catchup_at("nightjar") is None


def test_set_then_get_last_catchup_at_round_trips(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.set_last_catchup_at("nightjar", 1_700_000_000)
    assert s.get_last_catchup_at("nightjar") == 1_700_000_000


def test_set_last_catchup_at_overwrites(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.set_last_catchup_at("nightjar", 1_700_000_000)
    s.set_last_catchup_at("nightjar", 1_700_001_000)
    assert s.get_last_catchup_at("nightjar") == 1_700_001_000


def test_last_catchup_at_is_per_inbox(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.set_last_catchup_at("nightjar", 1_700_000_000)
    s.set_last_catchup_at("notes", 1_700_500_000)
    assert s.get_last_catchup_at("nightjar") == 1_700_000_000
    assert s.get_last_catchup_at("notes") == 1_700_500_000


def test_set_last_catchup_at_rejects_negative(tmp_path: Path) -> None:
    import pytest
    s = make_state(tmp_path)
    with pytest.raises(ValueError):
        s.set_last_catchup_at("nightjar", -1)


def test_schema_version_is_11(tmp_path: Path) -> None:
    """Step 7a bumped schema to V11 (note_proposals)."""
    import sqlite3
    s = make_state(tmp_path)
    conn = sqlite3.connect(s.db_path)
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row[0] == 11
    finally:
        conn.close()


# ---- Step 7a: note_proposals -----------------------------------------------


def test_enqueue_note_proposal_round_trips(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    pid = s.enqueue_note_proposal(
        contact_id="fraser",
        section_heading="Aurora",
        body="Asked for deadline extension to 5/15.",
        scope="aurora",
        proposed_at=1000,
    )
    assert pid > 0
    row = s.get_note_proposal(pid)
    assert row is not None
    assert row["contact_id"] == "fraser"
    assert row["section_heading"] == "Aurora"
    assert row["scope"] == "aurora"
    assert row["status"] == "pending"
    assert row["proposed_at"] == 1000
    # Default TTL is 14 days.
    assert row["expires_at"] == 1000 + 14 * 24 * 60 * 60


def test_enqueue_note_proposal_accepts_null_scope(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    pid = s.enqueue_note_proposal(
        contact_id="alice",
        section_heading="General",
        body="Replies in evenings.",
        scope=None,
        proposed_at=1000,
    )
    row = s.get_note_proposal(pid)
    assert row["scope"] is None


def test_list_pending_excludes_expired_via_now(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.enqueue_note_proposal(
        contact_id="a", section_heading="x", body="b",
        scope=None, proposed_at=1000, ttl_seconds=100,
    )
    # now=1500 > 1100 (expires_at), so excluded.
    pending = s.list_pending_note_proposals(now=1500)
    assert pending == []


def test_list_pending_orders_oldest_first(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    p1 = s.enqueue_note_proposal(
        contact_id="a", section_heading="x", body="b1",
        scope=None, proposed_at=1000,
    )
    p2 = s.enqueue_note_proposal(
        contact_id="a", section_heading="x", body="b2",
        scope=None, proposed_at=2000,
    )
    p3 = s.enqueue_note_proposal(
        contact_id="a", section_heading="x", body="b3",
        scope=None, proposed_at=1500,
    )
    pending = s.list_pending_note_proposals(now=2100)
    assert [r["id"] for r in pending] == [p1, p3, p2]


def test_list_for_contact_filters_by_status(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    p1 = s.enqueue_note_proposal(
        contact_id="alice", section_heading="x", body="b",
        scope=None, proposed_at=1000,
    )
    p2 = s.enqueue_note_proposal(
        contact_id="alice", section_heading="y", body="b",
        scope=None, proposed_at=1100,
    )
    s.mark_note_proposal_rejected(p1, now=1200)
    # Default omits rejected.
    rows = s.list_note_proposals_for_contact("alice")
    assert [r["id"] for r in rows] == [p2]
    # Explicit statuses=("rejected",) yields the rejected one.
    rows = s.list_note_proposals_for_contact("alice", statuses=("rejected",))
    assert [r["id"] for r in rows] == [p1]


def test_mark_approved_then_applied(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    pid = s.enqueue_note_proposal(
        contact_id="a", section_heading="x", body="b",
        scope=None, proposed_at=1000,
    )
    assert s.mark_note_proposal_approved(pid, now=1100) is True
    row = s.get_note_proposal(pid)
    assert row["status"] == "approved"
    assert row["approved_at"] == 1100
    assert s.mark_note_proposal_applied(pid, now=1200) is True
    row = s.get_note_proposal(pid)
    assert row["status"] == "applied"
    assert row["applied_at"] == 1200


def test_mark_applied_directly_from_pending_for_auto_approve(tmp_path: Path) -> None:
    """auto_approve_notes contacts skip the approved step."""
    s = make_state(tmp_path)
    pid = s.enqueue_note_proposal(
        contact_id="a", section_heading="x", body="b",
        scope=None, proposed_at=1000,
    )
    assert s.mark_note_proposal_applied(pid, now=1100) is True
    row = s.get_note_proposal(pid)
    assert row["status"] == "applied"
    # approved_at stays None — we skipped that step.
    assert row["approved_at"] is None


def test_mark_rejected_works_from_pending(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    pid = s.enqueue_note_proposal(
        contact_id="a", section_heading="x", body="b",
        scope=None, proposed_at=1000,
    )
    assert s.mark_note_proposal_rejected(pid, now=1100) is True
    row = s.get_note_proposal(pid)
    assert row["status"] == "rejected"


def test_mark_returns_false_when_no_transition(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    pid = s.enqueue_note_proposal(
        contact_id="a", section_heading="x", body="b",
        scope=None, proposed_at=1000,
    )
    s.mark_note_proposal_applied(pid, now=1100)
    # Already applied; further transitions are no-ops returning False.
    assert s.mark_note_proposal_approved(pid, now=1200) is False
    assert s.mark_note_proposal_rejected(pid, now=1200) is False


def test_expire_old_sweeps_pending_only(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    pid_pending = s.enqueue_note_proposal(
        contact_id="a", section_heading="x", body="b",
        scope=None, proposed_at=1000, ttl_seconds=100,
    )
    pid_approved = s.enqueue_note_proposal(
        contact_id="a", section_heading="y", body="b",
        scope=None, proposed_at=1000, ttl_seconds=100,
    )
    s.mark_note_proposal_approved(pid_approved, now=1050)
    moved = s.expire_old_note_proposals(now=1200)
    # Only the pending one should expire.
    assert moved == 1
    assert s.get_note_proposal(pid_pending)["status"] == "expired"
    # Approved one is unaffected — once a principal has approved it,
    # expiring the apply step is a separate concern (out of scope here).
    assert s.get_note_proposal(pid_approved)["status"] == "approved"


def test_count_pending_matches_list(tmp_path: Path) -> None:
    s = make_state(tmp_path)
    s.enqueue_note_proposal(
        contact_id="a", section_heading="x", body="b1",
        scope=None, proposed_at=1000,
    )
    s.enqueue_note_proposal(
        contact_id="b", section_heading="x", body="b2",
        scope=None, proposed_at=1100,
    )
    pid_resolved = s.enqueue_note_proposal(
        contact_id="c", section_heading="x", body="b3",
        scope=None, proposed_at=1200,
    )
    s.mark_note_proposal_rejected(pid_resolved, now=1300)
    assert s.count_pending_note_proposals(now=1400) == 2
    assert len(s.list_pending_note_proposals(now=1400)) == 2
