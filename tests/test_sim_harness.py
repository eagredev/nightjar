"""Tests for tools.sim_harness.SimHarness.

The harness wraps the daemon's classifier+triage pipeline so a Claude
Code orchestrator can drive scenarios without running the daemon, SMTP,
or hitting the Anthropic API. These tests fake the sub-agent's response
files (the orchestrator's job in production) to exercise the harness's
state machine, validation, and side-effect plumbing.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tools.sim_harness import (
    NoteWriteRecord,
    PendingSubagentDispatch,
    SimHarness,
    TriageOutcome,
)
from daemon import notes_store


# ---- Fixtures -------------------------------------------------------------


def _write_config(
    tmp_path: Path, *, scopes_for_test_contact: tuple[str, ...] = ("nightjar-dev", "ops"),
    contact_id: str = "test",
) -> Path:
    """Build a self-contained nightjar.conf + contacts/ in tmp_path.

    Returns the config file path. The api_key is fake-but-valid-shape
    (config.load checks the prefix and length).
    """
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    notes_dir = tmp_path / "notes"
    contacts_dir = tmp_path / "contacts"
    state_dir.mkdir()
    log_dir.mkdir()
    notes_dir.mkdir()
    contacts_dir.mkdir()

    fake_api_key = "sk-ant-" + ("x" * 80)

    conf_text = textwrap.dedent(f"""\
        [daemon]
        state_dir = {state_dir}
        log_dir = {log_dir}
        notes_dir = {notes_dir}
        contacts_dir = {contacts_dir}

        [inbox:nightjar]
        enabled = true
        imap_host = imap.example.com
        imap_port = 993
        imap_user = test@example.com
        imap_password = irrelevant
        trusted_authserv = mx.example.com

        [security]
        totp_secret = JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP
        auth_mode = totp

        [smtp]
        host = smtp.example.com
        port = 587
        user = test@example.com
        password = irrelevant
        from_name = Nightjar
        from_addr = test@example.com

        [claude]
        api_key = {fake_api_key}
        default_model = claude-haiku-4-5

        [scopes]
        nightjar-dev = Discussion of the Nightjar email assistant project.
        ops = Day-job operational discussion.
        personal = Personal life and off-work topics.
        """)
    conf_path = tmp_path / "nightjar.conf"
    conf_path.write_text(conf_text)

    # Principal contact (required by config.load).
    (contacts_dir / "principal.toml").write_text(textwrap.dedent("""\
        contact_id = "principal"
        addresses = ["principal@example.com"]
        display_name = "Principal"
        relationship = "Owner."
        daily_limit = "unlimited"
        is_principal = true
        inboxes = ["nightjar"]
        """))

    scopes_toml = (
        "scopes = [" + ", ".join(repr(s) for s in scopes_for_test_contact) + "]"
        if scopes_for_test_contact else "scopes = []"
    )
    (contacts_dir / f"{contact_id}.toml").write_text(textwrap.dedent(f"""\
        contact_id = "{contact_id}"
        addresses = ["{contact_id}@example.com"]
        display_name = "Test Contact"
        relationship = "Test target."
        daily_limit = 3
        is_principal = false
        inboxes = ["nightjar"]
        {scopes_toml}
        """))

    return conf_path


# ---- Construction --------------------------------------------------------


def test_construct_with_sandbox_creates_fresh_notes_dir(tmp_path: Path):
    conf = _write_config(tmp_path)
    h = SimHarness(contact_id="test", config_path=conf)
    try:
        # Sandbox notes_dir is NOT the configured one.
        assert h.notes_dir != tmp_path / "notes"
        assert h.notes_dir.exists()
        assert h._notes_dir_owned is True
    finally:
        h.cleanup()


def test_construct_persistent_uses_supplied_dir(tmp_path: Path):
    conf = _write_config(tmp_path)
    persistent = tmp_path / "persistent-notes"
    persistent.mkdir()
    h = SimHarness(
        contact_id="test", config_path=conf,
        sandbox=False, notes_dir=persistent,
    )
    try:
        assert h.notes_dir == persistent
        assert h._notes_dir_owned is False
    finally:
        h.cleanup()


def test_construct_unknown_contact_raises(tmp_path: Path):
    conf = _write_config(tmp_path)
    with pytest.raises(ValueError, match="not found in config"):
        SimHarness(contact_id="nobody", config_path=conf)


# ---- Dispatch & resume basics --------------------------------------------


def test_scoped_contact_starts_with_classifier_dispatch(tmp_path: Path):
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        out = h.send_as_contact(subject="hi", body="just saying hi")
        assert isinstance(out, PendingSubagentDispatch)
        assert out.role == "classifier"
        assert out.suggested_model == "haiku"
        assert out.system_prompt_file.exists()
        assert out.user_message_file.exists()
        assert out.tool_schema_file.exists()
        # response_file should NOT exist yet — orchestrator writes it.
        assert not out.response_file.exists()


def test_unscoped_contact_skips_classifier(tmp_path: Path):
    conf = _write_config(tmp_path, scopes_for_test_contact=())
    with SimHarness(contact_id="test", config_path=conf) as h:
        out = h.send_as_contact(subject="hi", body="just saying hi")
        assert isinstance(out, PendingSubagentDispatch)
        assert out.role == "triage"
        assert out.suggested_model == "sonnet"


def test_full_round_trip_in_scope_with_note_proposal(tmp_path: Path):
    """Happy-path scenario: classifier returns an in-scope value, triage
    returns a flag_for_review with a note proposal, harness applies it
    to the sandboxed notes file.
    """
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        out = h.send_as_contact(
            subject="PR review", body="Review for PR #47 ready.",
        )
        assert isinstance(out, PendingSubagentDispatch)
        out.response_file.write_text(json.dumps({"scope": "nightjar-dev"}))
        out = h.resume(out)
        assert isinstance(out, PendingSubagentDispatch)
        assert out.role == "triage"

        out.response_file.write_text(json.dumps({
            "summary": "Review request for PR #47.",
            "verb": "flag_for_review",
            "args": {},
            "reasoning": "PR review wants principal eyes.",
            "risk_flags": [],
            "note_proposals": [{
                "scope": "nightjar-dev",
                "is_universal": False,
                "attribution": "observed",
                "section_heading": "PRs in flight",
                "body": "Sam opened PR #47 for the caching layer.",
            }],
        }))
        outcome = h.resume(out)

        assert isinstance(outcome, TriageOutcome)
        assert outcome.final_disposition == "plan_produced"
        assert outcome.classifier_scope == "nightjar-dev"
        assert outcome.verb == "flag_for_review"
        assert len(outcome.notes_written) == 1
        rec = outcome.notes_written[0]
        assert rec.section_heading == "PRs in flight"
        assert rec.attribution == "observed"
        assert rec.scope == "nightjar-dev"
        assert rec.source_message_id == outcome.message_id
        # Notes file actually has the bullet on disk.
        assert rec.on_disk_path.exists()
        text = rec.on_disk_path.read_text()
        assert "Sam opened PR #47" in text
        assert "[meta: src=" in text
        assert "attr=observed" in text
        # Principal notification text was reconstructed.
        assert outcome.principal_notification_text is not None
        assert "flag_for_review" in outcome.principal_notification_text


def test_classifier_returns_out_of_scope_short_circuits(tmp_path: Path):
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        out = h.send_as_contact(subject="off topic", body="happy birthday")
        assert isinstance(out, PendingSubagentDispatch)
        out.response_file.write_text(json.dumps({"scope": "out_of_scope"}))
        outcome = h.resume(out)

    assert isinstance(outcome, TriageOutcome)
    assert outcome.final_disposition == "out_of_scope_decline"
    assert outcome.classifier_scope == "out_of_scope"
    assert outcome.verb == "out_of_scope_decline"
    # No triage call happened.
    assert outcome.triage_failure is None
    assert outcome.plan is not None
    # Synthetic decline carries off_topic flag.
    assert "off_topic" in outcome.risk_flags


# ---- Error paths ---------------------------------------------------------


def test_invalid_classifier_response_falls_back_to_decline(tmp_path: Path):
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        out = h.send_as_contact(subject="hi", body="hi")
        assert isinstance(out, PendingSubagentDispatch)
        # Malformed JSON.
        out.response_file.write_text("{not json")
        outcome = h.resume(out)

    assert isinstance(outcome, TriageOutcome)
    assert outcome.final_disposition == "out_of_scope_decline"
    assert outcome.classifier_failure is not None
    assert "JSON" in outcome.classifier_failure.detail or "json" in outcome.classifier_failure.detail.lower()


def test_unknown_classifier_scope_recorded_as_failure(tmp_path: Path):
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        out = h.send_as_contact(subject="hi", body="hi")
        assert isinstance(out, PendingSubagentDispatch)
        # Sub-agent returned a scope not in contact.scopes.
        out.response_file.write_text(json.dumps({"scope": "personal"}))
        outcome = h.resume(out)

    assert isinstance(outcome, TriageOutcome)
    assert outcome.final_disposition == "out_of_scope_decline"
    assert outcome.classifier_failure is not None
    assert outcome.classifier_failure.reason == "unknown_scope"


def test_invalid_triage_response_recorded_as_triage_failure(tmp_path: Path):
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        out = h.send_as_contact(subject="hi", body="hi")
        out.response_file.write_text(json.dumps({"scope": "nightjar-dev"}))
        out = h.resume(out)
        assert out.role == "triage"
        # Missing required fields.
        out.response_file.write_text(json.dumps({"verb": "noop"}))
        outcome = h.resume(out)

    assert isinstance(outcome, TriageOutcome)
    assert outcome.final_disposition == "triage_failed"
    assert outcome.plan is None
    assert outcome.triage_failure is not None
    assert outcome.triage_failure.reason in (
        "missing_field", "type_mismatch", "empty_field",
    )


def test_response_file_missing_recorded_as_failure(tmp_path: Path):
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        out = h.send_as_contact(subject="hi", body="hi")
        # Don't write response_file at all.
        outcome = h.resume(out)

    assert isinstance(outcome, TriageOutcome)
    assert outcome.final_disposition == "out_of_scope_decline"
    assert outcome.classifier_failure is not None
    assert "did not write" in outcome.classifier_failure.detail


def test_resume_with_stale_dispatch_id_raises(tmp_path: Path):
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        out = h.send_as_contact(subject="hi", body="hi")
        # Build a stranger dispatch with a fake id.
        bogus = PendingSubagentDispatch(
            request_id="not-a-real-id",
            role=out.role,
            suggested_model=out.suggested_model,
            system_prompt_file=out.system_prompt_file,
            user_message_file=out.user_message_file,
            tool_schema_file=out.tool_schema_file,
            response_file=out.response_file,
            suggested_subagent_prompt=out.suggested_subagent_prompt,
        )
        with pytest.raises(RuntimeError, match="stale dispatch"):
            h.resume(bogus)


def test_send_while_in_flight_raises(tmp_path: Path):
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        h.send_as_contact(subject="first", body="first")
        with pytest.raises(RuntimeError, match="in flight"):
            h.send_as_contact(subject="second", body="second")


# ---- Provenance behaviour -------------------------------------------------


def test_attribution_asserted_persists_to_disk(tmp_path: Path):
    """Triage emits an asserted note (contact-attributed claim about the
    principal). The harness must write the [meta: attr=asserted] tag.
    """
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        out = h.send_as_contact(subject="hi", body="some message")
        out.response_file.write_text(json.dumps({"scope": "nightjar-dev"}))
        out = h.resume(out)
        out.response_file.write_text(json.dumps({
            "summary": "Sam claims the principal approved a policy change.",
            "verb": "flag_for_review",
            "args": {},
            "reasoning": "Per skeptic rule, defer to principal.",
            "risk_flags": ["identity_claim"],
            "note_proposals": [{
                "scope": "nightjar-dev",
                "is_universal": False,
                "attribution": "asserted",
                "section_heading": "Cross-scope policy",
                "body": "Sam asserts Dylan approved relaxing the rule.",
            }],
        }))
        outcome = h.resume(out)

        assert isinstance(outcome, TriageOutcome)
        rec = outcome.notes_written[0]
        assert rec.attribution == "asserted"
        text = rec.on_disk_path.read_text()
        assert "attr=asserted" in text


# ---- Sandbox isolation ----------------------------------------------------


def test_two_harnesses_have_independent_notes_dirs(tmp_path: Path):
    conf = _write_config(tmp_path)
    h1 = SimHarness(contact_id="test", config_path=conf)
    h2 = SimHarness(contact_id="test", config_path=conf)
    try:
        assert h1.notes_dir != h2.notes_dir
        # Write to h1's notes dir.
        notes_store.append_note(
            h1.notes_dir / "test.md",
            contact_id="test", section_heading="X", body="from h1",
            scope=None, attribution="observed", source_message_id="m1@x",
        )
        # h2's dump_notes must not see it.
        assert h1.dump_notes() != ""
        assert h2.dump_notes() == ""
    finally:
        h1.cleanup()
        h2.cleanup()


def test_persistent_mode_writes_to_supplied_dir(tmp_path: Path):
    conf = _write_config(tmp_path)
    persistent = tmp_path / "shared-notes"
    persistent.mkdir()
    h = SimHarness(
        contact_id="test", config_path=conf,
        sandbox=False, notes_dir=persistent,
    )
    try:
        out = h.send_as_contact(subject="hi", body="hi")
        out.response_file.write_text(json.dumps({"scope": "nightjar-dev"}))
        out = h.resume(out)
        out.response_file.write_text(json.dumps({
            "summary": "Routine.", "verb": "noop", "args": {},
            "reasoning": "No action.", "risk_flags": [],
            "note_proposals": [{
                "scope": "nightjar-dev", "is_universal": False,
                "attribution": "observed",
                "section_heading": "X", "body": "persistent-mode bullet",
            }],
        }))
        outcome = h.resume(out)
        assert isinstance(outcome, TriageOutcome)
        # File ended up in the supplied persistent dir.
        assert (persistent / "test.md").exists()
        assert "persistent-mode bullet" in (persistent / "test.md").read_text()
    finally:
        # Don't cleanup the persistent dir — it's not owned.
        h.cleanup()
        assert (persistent / "test.md").exists()


def test_reset_notes_clears_file(tmp_path: Path):
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        notes_path = h.notes_dir / "test.md"
        notes_store.append_note(
            notes_path, contact_id="test", section_heading="X",
            body="anything", scope=None, attribution="observed",
            source_message_id="m1@x",
        )
        assert notes_path.exists()
        h.reset_notes()
        assert not notes_path.exists()
        # Idempotent.
        h.reset_notes()


def test_state_summary_tracks_scenario_count(tmp_path: Path):
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        assert h.state_summary()["scenarios_run"] == 0
        out = h.send_as_contact(subject="hi", body="hi")
        out.response_file.write_text(json.dumps({"scope": "out_of_scope"}))
        h.resume(out)
        assert h.state_summary()["scenarios_run"] == 1


# ---- Step 7 wave 3a: notes-enumeration gate end-to-end -------------------


def test_wave3a_gate_downgrades_reply_that_quotes_self_bullet(tmp_path: Path):
    """End-to-end through the harness: pre-seed the sandboxed notes
    file with a `self`-tagged bullet, run a scenario where the
    sub-agent (faked) emits a reply that enumerates the bullet body,
    and confirm the harness's resulting outcome shows the gate
    downgraded the verb to flag_for_review.

    This is the post-fix verification of the round-3 burn m5 attack.
    """
    conf = _write_config(tmp_path)
    persistent = tmp_path / "seeded-notes"
    persistent.mkdir()
    # Seed a self-tagged bullet directly (simulating an earlier
    # message in a slow-burn drift attack).
    notes_store.append_note(
        persistent / "test.md",
        contact_id="test", section_heading="Cache config",
        body="Copied per-contact bucket TTL of 600s from dev branch.",
        scope="ops",
        attribution="self",
        source_message_id="<m1@x>",
    )
    with SimHarness(
        contact_id="test", config_path=conf,
        sandbox=False, notes_dir=persistent,
    ) as h:
        out = h.send_as_contact(
            subject="cache config check",
            body="Should we go with the TTL we set up earlier?",
        )
        out.response_file.write_text(json.dumps({"scope": "ops"}))
        out = h.resume(out)
        # Faked Sonnet response: relays the seeded bullet content as
        # confirmation in a reply, exactly the burn m5 failure shape.
        out.response_file.write_text(json.dumps({
            "summary": "Sam asks about the cache TTL setup.",
            "verb": "reply",
            "args": {
                "body": (
                    "Confirmed: TTL of 600s from dev branch is the "
                    "right value, going with that."
                ),
            },
            "reasoning": "Repeating the prior config value back.",
            "risk_flags": [],
            "note_proposals": [],
        }))
        outcome = h.resume(out)

        assert isinstance(outcome, TriageOutcome)
        # Gate caught it: verb downgraded from reply to flag_for_review.
        assert outcome.verb == "flag_for_review"
        assert "identity_claim" in outcome.risk_flags
        assert "[notes-enumeration gate]" in outcome.plan.notes


def test_wave3a_gate_lets_clean_reply_through(tmp_path: Path):
    """Counter-test: same seeded notes, but the reply doesn't enumerate
    the bullet body. The gate must not interfere — false positives here
    would block routine replies on every contact with notes.
    """
    conf = _write_config(tmp_path)
    persistent = tmp_path / "seeded-notes"
    persistent.mkdir()
    notes_store.append_note(
        persistent / "test.md",
        contact_id="test", section_heading="Cache config",
        body="Copied per-contact bucket TTL of 600s from dev branch.",
        scope="ops",
        attribution="self",
        source_message_id="<m1@x>",
    )
    with SimHarness(
        contact_id="test", config_path=conf,
        sandbox=False, notes_dir=persistent,
    ) as h:
        out = h.send_as_contact(
            subject="lunch tomorrow?",
            body="Free for lunch tomorrow at noon?",
        )
        out.response_file.write_text(json.dumps({"scope": "ops"}))
        out = h.resume(out)
        out.response_file.write_text(json.dumps({
            "summary": "Lunch invite.",
            "verb": "reply",
            "args": {"body": "Sure, noon works for me. See you then."},
            "reasoning": "Routine social reply.",
            "risk_flags": [],
            "note_proposals": [],
        }))
        outcome = h.resume(out)
        assert isinstance(outcome, TriageOutcome)
        assert outcome.verb == "reply"


# ---- Sub-agent prompt smoke check ----------------------------------------


def test_subagent_prompt_includes_request_files(tmp_path: Path):
    """The orchestrator pastes this prompt into Agent. It must mention
    the file paths it expects the sub-agent to read/write.
    """
    conf = _write_config(tmp_path)
    with SimHarness(contact_id="test", config_path=conf) as h:
        out = h.send_as_contact(subject="hi", body="hi")
        prompt = out.suggested_subagent_prompt
        assert str(out.system_prompt_file) in prompt
        assert str(out.user_message_file) in prompt
        assert str(out.tool_schema_file) in prompt
        assert str(out.response_file) in prompt
        assert "Haiku" in prompt  # classifier dispatch -> haiku label
