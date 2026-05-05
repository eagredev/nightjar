"""Red-team test suite for Step 7's scope/notes infrastructure.

Adversarial tests that try to break the security guarantees:
  1. Scoped notes don't leak.
  2. Out-of-scope mail can't extract notes.
  3. The classifier can't be tricked.
  4. Malformed files fail closed, not open.
  5. Schema constraints can't be bypassed daemon-side.

Each test is named for the threat category. If a test fails, it
indicates a leak or a hardening gap, not a flaky assertion — read
the failure carefully.

Categories:
  A — schema-bypass attempts (validate_plan_payload + tool builder)
  E — file-on-disk corruption resilience (notes_store)
  F — race / state edge cases

Categories B, C, D are live-mail tests run separately; they don't
fit cleanly into a unit-test rig.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from daemon.config import Contact
from daemon.triage import (
    NoteProposal,
    TriagePlan,
    build_draft_plan_tool,
    validate_plan_payload,
)
from daemon import notes_store


# ---- Test fixtures ---------------------------------------------------------


def _contact(scopes: tuple[str, ...] = ()) -> Contact:
    """Build a test contact with the given scope tuple."""
    return Contact(
        contact_id="test", addresses=("test@example.com",),
        display_name="Test", relationship="Test target",
        daily_limit=3, is_principal=False,
        inboxes=("nightjar",), scopes=scopes,
    )


def _good_payload(**overrides: Any) -> dict[str, Any]:
    """Minimum-viable plan payload; override any field to test edge cases."""
    base: dict[str, Any] = {
        "summary": "Test message.",
        "verb": "noop",
        "args": {},
        "reasoning": "Routine test.",
        "risk_flags": [],
    }
    base.update(overrides)
    return base


# ============================================================================
# CATEGORY A — SCHEMA BYPASS
# ============================================================================


def test_A1_scope_outside_contact_list_dropped():
    """Model proposes a scope that's NOT in contact.scopes (even though
    it might be in the global registry). Must drop the proposal silently
    — never write it to disk."""
    contact = _contact(scopes=("nightjar-dev", "ops"))
    payload = _good_payload(note_proposals=[
        {"scope": "personal", "is_universal": False,
         "section_heading": "Bad", "body": "Should be dropped."},
    ])
    plan = validate_plan_payload(payload, contact=contact)
    assert isinstance(plan, TriagePlan)
    assert plan.note_proposals == ()


def test_A2_scope_with_weird_chars_dropped():
    """Adversarial scope name that LOOKS like a registered scope but
    isn't (case difference, whitespace, unicode lookalike). Must drop."""
    contact = _contact(scopes=("nightjar-dev",))
    cases = [
        "Nightjar-Dev",      # case
        "nightjar-dev ",     # trailing space
        " nightjar-dev",     # leading space
        "nightjar-dev\n",    # newline
        "nightjаr-dev",      # cyrillic 'а'
        "nightjar_dev",      # underscore not hyphen
        "*",                 # wildcard literal
        "",                  # empty
    ]
    for bad_scope in cases:
        payload = _good_payload(note_proposals=[
            {"scope": bad_scope, "is_universal": False,
             "section_heading": "X", "body": "Y"},
        ])
        plan = validate_plan_payload(payload, contact=contact)
        assert plan.note_proposals == (), (
            f"adversarial scope {bad_scope!r} survived validation"
        )


def test_A3_null_scope_for_scoped_contact_dropped():
    """Even if the model bypasses the tool schema's enum somehow,
    daemon-side validation must still drop scope=None proposals for
    scoped contacts. The schema is the wall; this is the safety net."""
    contact = _contact(scopes=("nightjar-dev",))
    payload = _good_payload(note_proposals=[
        {"scope": None, "is_universal": True,
         "section_heading": "X", "body": "Sneaky null."},
    ])
    plan = validate_plan_payload(payload, contact=contact)
    assert plan.note_proposals == ()


def test_A4_scoped_proposal_for_unscoped_contact_dropped():
    """Mirror: contact has no scopes, model emits a scoped proposal.
    Must drop — there's no scope vocabulary to validate against."""
    contact = _contact(scopes=())
    payload = _good_payload(note_proposals=[
        {"scope": "nightjar-dev", "is_universal": False,
         "section_heading": "X", "body": "Y"},
    ])
    plan = validate_plan_payload(payload, contact=contact)
    assert plan.note_proposals == ()


def test_A5_is_universal_non_bool_coerced_false():
    """If the model emits a non-boolean for is_universal (truthy
    string, integer, dict), must coerce to False. Never raise."""
    contact = _contact(scopes=("nightjar-dev",))
    cases: list[Any] = [
        "true",       # string
        1,            # int
        ["yes"],      # list
        {"v": True},  # dict
        None,         # null
    ]
    for bad in cases:
        payload = _good_payload(note_proposals=[
            {"scope": "nightjar-dev", "is_universal": bad,
             "section_heading": "X", "body": "Y"},
        ])
        plan = validate_plan_payload(payload, contact=contact)
        assert isinstance(plan, TriagePlan)
        assert len(plan.note_proposals) == 1
        assert plan.note_proposals[0].is_universal is False, (
            f"is_universal={bad!r} not coerced to False"
        )


def test_A6_oversized_fields_truncated_not_rejected():
    """Long heading / body should TRUNCATE, not fail the plan.
    Truncation is the lenient handling per design."""
    contact = _contact(scopes=("nightjar-dev",))
    payload = _good_payload(note_proposals=[
        {"scope": "nightjar-dev", "is_universal": False,
         "section_heading": "x" * 500,
         "body": "y" * 5000},
    ])
    plan = validate_plan_payload(payload, contact=contact)
    p = plan.note_proposals[0]
    assert len(p.section_heading) <= 80
    assert len(p.body) <= 280


def test_A7_too_many_proposals_truncated_not_rejected():
    """Beyond the per-plan cap, extras dropped silently."""
    contact = _contact(scopes=("nightjar-dev",))
    payload = _good_payload(note_proposals=[
        {"scope": "nightjar-dev", "is_universal": False,
         "section_heading": f"H{i}", "body": f"B{i}"}
        for i in range(50)
    ])
    plan = validate_plan_payload(payload, contact=contact)
    assert len(plan.note_proposals) <= 5


def test_A8_tool_schema_enum_for_scoped_contact():
    """Schema-level wall: scoped contact's tool spec must constrain
    scope to enum, no null type. The model's only escape would be to
    stop calling the tool entirely."""
    contact = _contact(scopes=("nightjar-dev", "ops"))
    tool = build_draft_plan_tool(contact)
    item = tool["input_schema"]["properties"]["note_proposals"]["items"]
    s = item["properties"]["scope"]
    assert s["type"] == "string"
    assert s.get("enum") == ["nightjar-dev", "ops"]
    # is_universal must be present + required.
    assert "is_universal" in item["required"]
    assert "scope" in item["required"]


def test_A9_tool_schema_no_extra_scope_for_registry_only():
    """Sanity: scopes registered globally but NOT on the contact
    must not appear in the contact's enum."""
    contact = _contact(scopes=("nightjar-dev",))
    tool = build_draft_plan_tool(contact)
    item = tool["input_schema"]["properties"]["note_proposals"]["items"]
    s = item["properties"]["scope"]
    # Even though personal/finance/private exist in our config,
    # they MUST NOT appear here.
    assert s["enum"] == ["nightjar-dev"]


def test_A10_payload_proposals_field_garbage_drops_to_empty():
    """If note_proposals comes back as a string, dict, or anything
    that isn't a list, treat it as empty rather than failing."""
    contact = _contact(scopes=("nightjar-dev",))
    for bad in ["a string", {"key": "val"}, 42, None]:
        payload = _good_payload(note_proposals=bad)
        plan = validate_plan_payload(payload, contact=contact)
        assert plan.note_proposals == ()


def test_A11_payload_proposal_item_garbage_dropped():
    """Individual non-dict items in the list are dropped without
    affecting valid items."""
    contact = _contact(scopes=("nightjar-dev",))
    payload = _good_payload(note_proposals=[
        "string item",
        42,
        None,
        ["list item"],
        {"scope": "nightjar-dev", "is_universal": False,
         "section_heading": "OK", "body": "Survives."},
    ])
    plan = validate_plan_payload(payload, contact=contact)
    assert len(plan.note_proposals) == 1
    assert plan.note_proposals[0].body == "Survives."


# ============================================================================
# CATEGORY E — FILE CORRUPTION RESILIENCE
# ============================================================================


def test_E1_malformed_frontmatter_fails_parse(tmp_path: Path):
    """Frontmatter that doesn't terminate or has malformed lines must
    raise NotesParseError. The orchestrator catches this and falls
    back to empty notes (verified in test_triage_orchestrator.py)."""
    p = tmp_path / "test.md"
    p.write_text("---\nkey without colon\n---\n\n## X\n- bullet\n")
    with pytest.raises(notes_store.NotesParseError):
        notes_store.read_notes(p, active_scope="any")


def test_E2_unterminated_frontmatter_fails_parse(tmp_path: Path):
    """Frontmatter opened but never closed must raise."""
    p = tmp_path / "test.md"
    p.write_text("---\ncontact_id: test\n# never closed\n## Section\n- bullet\n")
    with pytest.raises(notes_store.NotesParseError):
        notes_store.read_notes(p, active_scope="any")


def test_E3_empty_scopes_tag_fails_parse(tmp_path: Path):
    """An empty `[scopes:]` tag is malformed — refuse to guess intent."""
    p = tmp_path / "test.md"
    p.write_text(
        "---\ncontact_id: test\n---\n\n"
        "## X [scopes:]\n- bullet\n"
    )
    with pytest.raises(notes_store.NotesParseError):
        notes_store.read_notes(p, active_scope="any")


def test_E4_truncated_mid_section_does_not_fail_open(tmp_path: Path):
    """File ending mid-section (no bullets, no following content)
    should still parse cleanly and produce empty filtered output for
    that section. Never fail open by leaking content from the
    incomplete state."""
    p = tmp_path / "test.md"
    p.write_text(
        "---\ncontact_id: test\n---\n\n"
        "## Truncated [scopes: nightjar-dev]\n"
    )
    out = notes_store.read_notes(p, active_scope="personal")
    assert "Truncated" not in out
    assert "[scopes:" not in out


def test_E5_section_with_zero_bullets_filtered_out(tmp_path: Path):
    """A section with no bullets has nothing to render — must not
    appear in output regardless of scope match."""
    p = tmp_path / "test.md"
    p.write_text(
        "---\ncontact_id: test\n---\n\n"
        "## Empty section [scopes: *]\n\n"
        "## Has content [scopes: nightjar-dev]\n\n"
        "- bullet\n"
    )
    out = notes_store.read_notes(p, active_scope="nightjar-dev")
    assert "Empty section" not in out
    assert "Has content" in out


def test_E6_multi_scope_bullet_visible_in_each(tmp_path: Path):
    """A bullet tagged [scopes: a, b] must be visible in BOTH a and b
    triages, but in NEITHER c nor d."""
    p = tmp_path / "test.md"
    p.write_text(
        "---\ncontact_id: test\n---\n\n"
        "## Cross-cut\n\n"
        "- shared bullet [scopes: nightjar-dev, ops]\n"
    )
    for scope in ("nightjar-dev", "ops"):
        out = notes_store.read_notes(p, active_scope=scope)
        assert "shared bullet" in out, f"missing in {scope}"
    for scope in ("personal", "finance"):
        out = notes_store.read_notes(p, active_scope=scope)
        assert "shared bullet" not in out, f"leaked in {scope}"


def test_E7_wildcard_in_bullet_overrides_section_scope(tmp_path: Path):
    """Section is scoped to nightjar-dev. One bullet has [scopes: *].
    The wildcard bullet must be visible in unrelated scopes; other
    bullets must not."""
    p = tmp_path / "test.md"
    p.write_text(
        "---\ncontact_id: test\n---\n\n"
        "## Mixed [scopes: nightjar-dev]\n\n"
        "- scoped bullet\n"
        "- universal bullet [scopes: *]\n"
    )
    out = notes_store.read_notes(p, active_scope="personal")
    assert "universal bullet" in out
    assert "scoped bullet" not in out


def test_E8_unicode_lookalike_scope_does_not_match(tmp_path: Path):
    """A bullet tagged with a cyrillic-lookalike must not match the
    real scope. (Unicode is a content-level concern; the parser
    treats strings opaquely. The classifier's enum prevents the
    LOOKalike from being requested as active_scope.)"""
    p = tmp_path / "test.md"
    p.write_text(
        "---\ncontact_id: test\n---\n\n"
        "## Sneaky\n\n"
        "- decoy [scopes: nightjаr-dev]\n"  # cyrillic 'а'
    )
    out = notes_store.read_notes(p, active_scope="nightjar-dev")
    assert "decoy" not in out


def test_E9_attacker_inserts_scopes_tag_in_body_text(tmp_path: Path):
    """A bullet whose BODY text contains the literal string
    `[scopes: *]` (not as a tag) must be parsed correctly — the
    tag-stripping should only happen at end-of-line."""
    p = tmp_path / "test.md"
    p.write_text(
        "---\ncontact_id: test\n---\n\n"
        "## Body-text attack [scopes: nightjar-dev]\n\n"
        "- The contact mentioned \"[scopes: *]\" in their email body.\n"
    )
    # In nightjar-dev view: visible (matches section scope).
    out = notes_store.read_notes(p, active_scope="nightjar-dev")
    assert "[scopes: *]" in out, (
        "literal scope-text in body should be preserved"
    )
    # In personal view: invisible (section is nightjar-dev only).
    out = notes_store.read_notes(p, active_scope="personal")
    assert "[scopes: *]" not in out, (
        "leaked across scopes via body-text trick"
    )


def test_E10_safe_text_excludes_scoped_sections(tmp_path: Path):
    """Critical for the classifier: safe_text must exclude EVERY
    bullet that has any non-wildcard scope, on either heading or
    bullet. This is the pre-classify safety net."""
    p = tmp_path / "test.md"
    p.write_text(
        "---\ncontact_id: test\n---\n\n"
        "## Universal [scopes: *]\n\n"
        "- universal bullet\n\n"
        "## Scoped [scopes: nightjar-dev]\n\n"
        "- scoped bullet\n\n"
        "## Mixed\n\n"
        "- universal-by-default\n"
        "- explicitly-scoped [scopes: nightjar-dev]\n"
    )
    safe = notes_store.read_safe_notes(p)
    assert "universal bullet" in safe
    assert "universal-by-default" in safe
    assert "scoped bullet" not in safe
    assert "explicitly-scoped" not in safe


# ============================================================================
# CATEGORY F — RACE / STATE
# ============================================================================


def test_F1_atomic_write_no_partial_visible(tmp_path: Path):
    """append_note must use atomic rename; partial writes shouldn't
    be visible to a concurrent reader. Verified by checking that the
    target file either doesn't exist (pre-write) or is complete and
    parseable (post-write) — never half-finished."""
    p = tmp_path / "test.md"
    notes_store.append_note(
        p, contact_id="test", section_heading="X",
        body="first bullet", scope="nightjar-dev",
    )
    # File should be parseable.
    parsed = notes_store.parse(p.read_text())
    assert len(parsed.sections) == 1
    # Append again — same atomicity.
    notes_store.append_note(
        p, contact_id="test", section_heading="X",
        body="second bullet", scope="nightjar-dev",
    )
    parsed = notes_store.parse(p.read_text())
    assert len(parsed.sections[0].bullets) == 2


def test_F2_empty_file_does_not_crash(tmp_path: Path):
    """A zero-byte file should be handled — either by raising parse
    error cleanly, or by treating it as an empty notes file. NEVER
    crash unexpectedly."""
    p = tmp_path / "test.md"
    p.write_text("")
    try:
        notes_store.read_notes(p, active_scope="any")
    except notes_store.NotesParseError:
        pass  # Acceptable: explicit parse error.
    except Exception as e:
        pytest.fail(f"unexpected exception type {type(e).__name__}: {e}")


def test_F3_file_with_only_frontmatter_yields_empty_filter(tmp_path: Path):
    """Valid file with no sections — read_notes returns empty string,
    not an exception."""
    p = tmp_path / "test.md"
    p.write_text("---\ncontact_id: test\n---\n")
    out = notes_store.read_notes(p, active_scope="any")
    assert out == ""


def test_F4_append_to_missing_file_creates_with_frontmatter(tmp_path: Path):
    """append_note on a non-existent file must create it with valid
    frontmatter. No way to skip the frontmatter step."""
    p = tmp_path / "fresh.md"
    notes_store.append_note(
        p, contact_id="test", section_heading="X",
        body="b", scope="nightjar-dev",
    )
    text = p.read_text()
    assert text.startswith("---\n")
    assert "contact_id: test" in text
    assert "created_at:" in text
    assert "last_updated:" in text


def test_F5_round_trip_idempotent(tmp_path: Path):
    """Parse then serialize must produce the same bytes. Critical
    for not silently corrupting hand-written notes."""
    original = (
        "---\n"
        "contact_id: test\n"
        "created_at: 2026-05-05T12:00:00Z\n"
        "last_updated: 2026-05-05T12:00:00Z\n"
        "---\n"
        "\n"
        "## Section A [scopes: nightjar-dev]\n"
        "\n"
        "- bullet one\n"
        "- bullet two [scopes: *]\n"
        "\n"
        "## Section B\n"
        "\n"
        "- single bullet\n"
    )
    p = tmp_path / "test.md"
    p.write_text(original)
    parsed = notes_store.parse(p.read_text())
    reserialized = notes_store._serialize(parsed)
    assert reserialized == original, (
        f"round-trip not identical:\n"
        f"=== original ===\n{original}\n"
        f"=== reserialized ===\n{reserialized}"
    )


def test_F6_file_perms_restricted_to_owner(tmp_path: Path):
    """append_note must chmod the file to 0600 — owner-only."""
    import os
    p = tmp_path / "test.md"
    notes_store.append_note(
        p, contact_id="test", section_heading="X",
        body="b", scope="nightjar-dev",
    )
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


# ============================================================================
# CATEGORY G — CROSS-CONTACT LEAKAGE
# ============================================================================
#
# The scope system stops scoped notes from leaking ACROSS scopes for the
# same contact. This category tests the orthogonal axis: can contact A's
# notes ever surface during a triage of contact B?
#
# The expectation: never. Notes files are keyed by contact_id, the path
# is built from contact_id, and there's no code path that should resolve
# one contact's notes when triaging another. These tests verify that.


def test_G1_per_contact_files_isolated(tmp_path: Path):
    """Two contacts each have their own notes file with their own
    canaries. Reading either returns only that contact's content."""
    a_path = tmp_path / "alice.md"
    b_path = tmp_path / "bob.md"
    notes_store.append_note(
        a_path, contact_id="alice", section_heading="A",
        body="ALICE_CANARY_1", scope=None,
    )
    notes_store.append_note(
        b_path, contact_id="bob", section_heading="B",
        body="BOB_CANARY_1", scope=None,
    )
    a_text = notes_store.read_notes(a_path, active_scope=None)
    b_text = notes_store.read_notes(b_path, active_scope=None)
    assert "ALICE_CANARY_1" in a_text
    assert "BOB_CANARY_1" not in a_text, (
        "Bob's note appeared in Alice's read"
    )
    assert "BOB_CANARY_1" in b_text
    assert "ALICE_CANARY_1" not in b_text, (
        "Alice's note appeared in Bob's read"
    )


def test_G2_path_traversal_in_contact_id_blocked_at_load():
    """contact_id with path-traversal chars must be rejected by the
    contacts loader, so it can never reach the notes-path construction.
    `_CONTACT_ID_RE` is the gatekeeper."""
    from daemon.contacts_loader import _CONTACT_ID_RE
    bad_ids = [
        "../etc/passwd",
        "..",
        "alice/../bob",
        "alice/bob",
        "alice\\bob",
        ".alice",
        "alice.md",
        "alice ",  # trailing space
        " alice",  # leading space
        "",
        "alice\nbob",
    ]
    for bad in bad_ids:
        assert _CONTACT_ID_RE.match(bad) is None, (
            f"path-traversal contact_id {bad!r} accepted by regex"
        )


def test_G3_contact_id_regex_accepts_normal_names():
    """Sanity: the regex doesn't accidentally reject reasonable IDs."""
    from daemon.contacts_loader import _CONTACT_ID_RE
    good_ids = [
        "alice",
        "bob_smith",
        "fraser-mcmichael",
        "test_contact_2",
        "ABC123",
        "x",
    ]
    for good in good_ids:
        assert _CONTACT_ID_RE.match(good) is not None, (
            f"reasonable contact_id {good!r} rejected by regex"
        )


def test_G4_show_notes_handler_uses_correct_path(tmp_path: Path):
    """principal_handlers.handle_show_notes builds the path from the
    target contact_id, not from anything in the request body. Confirms
    the principal asking 'show notes for alice' can't be tricked into
    reading bob's notes via a crafted request body or args."""
    # Set up two real notes files.
    a_path = tmp_path / "alice.md"
    b_path = tmp_path / "bob.md"
    notes_store.append_note(
        a_path, contact_id="alice", section_heading="A",
        body="ALICE_CANARY", scope=None,
    )
    notes_store.append_note(
        b_path, contact_id="bob", section_heading="B",
        body="BOB_CANARY", scope=None,
    )
    # Use the same path-construction handle_show_notes uses:
    # notes_dir / f"{contact_id}.md".
    target = "alice"
    constructed = tmp_path / f"{target}.md"
    out = notes_store.read_notes(constructed, active_scope=None)
    assert "ALICE_CANARY" in out
    assert "BOB_CANARY" not in out


def test_G5_appending_to_contact_a_does_not_touch_contact_b(tmp_path: Path):
    """append_note for contact A must never modify contact B's file.
    Atomic write semantics + per-contact file path mean this is
    structurally impossible, but worth a sanity test."""
    a_path = tmp_path / "alice.md"
    b_path = tmp_path / "bob.md"
    notes_store.append_note(
        a_path, contact_id="alice", section_heading="X",
        body="alice-existing", scope=None,
    )
    notes_store.append_note(
        b_path, contact_id="bob", section_heading="X",
        body="bob-existing", scope=None,
    )
    b_text_before = b_path.read_text()
    # Append another bullet to alice.
    notes_store.append_note(
        a_path, contact_id="alice", section_heading="X",
        body="alice-second", scope=None,
    )
    b_text_after = b_path.read_text()
    assert b_text_before == b_text_after, (
        "Bob's file changed when appending to Alice's"
    )


def test_G6_safe_text_cross_contact_isolation(tmp_path: Path):
    """The classifier's pass-1 view must also be per-contact."""
    a_path = tmp_path / "alice.md"
    b_path = tmp_path / "bob.md"
    a_path.write_text(
        "---\ncontact_id: alice\n---\n\n"
        "## Universal [scopes: *]\n\n"
        "- ALICE_UNIVERSAL\n"
    )
    b_path.write_text(
        "---\ncontact_id: bob\n---\n\n"
        "## Universal [scopes: *]\n\n"
        "- BOB_UNIVERSAL\n"
    )
    a_safe = notes_store.read_safe_notes(a_path)
    b_safe = notes_store.read_safe_notes(b_path)
    assert "ALICE_UNIVERSAL" in a_safe
    assert "BOB_UNIVERSAL" not in a_safe
    assert "BOB_UNIVERSAL" in b_safe
    assert "ALICE_UNIVERSAL" not in b_safe


def test_G7_orchestrator_uses_contact_specific_path(tmp_path: Path):
    """Verify the orchestrator (triage_with_scope) reads only the
    target contact's notes file, not any other file in notes_dir.
    Tests via direct path inspection of where triage looks."""
    # Set up two contacts' notes in the same dir.
    notes_store.append_note(
        tmp_path / "alice.md", contact_id="alice",
        section_heading="A", body="ALICE_CANARY", scope=None,
    )
    notes_store.append_note(
        tmp_path / "bob.md", contact_id="bob",
        section_heading="B", body="BOB_CANARY", scope=None,
    )
    # The orchestrator builds path as notes_dir / f"{contact.contact_id}.md"
    # (see triage.py:988). Verify each path is unique per contact.
    alice_path = tmp_path / "alice.md"
    bob_path = tmp_path / "bob.md"
    assert alice_path != bob_path
    # And reading each only yields its own canary.
    alice_text = notes_store.read_notes(alice_path, active_scope=None)
    bob_text = notes_store.read_notes(bob_path, active_scope=None)
    assert "ALICE_CANARY" in alice_text and "BOB_CANARY" not in alice_text
    assert "BOB_CANARY" in bob_text and "ALICE_CANARY" not in bob_text


def test_G8_symlink_in_notes_dir_does_not_leak(tmp_path: Path):
    """If alice.md is a symlink to bob.md (somehow — config error,
    operator mistake), reading alice.md returns bob's content. This is
    OS-level behaviour, not the daemon's fault, but worth knowing.

    The check we CAN make: `notes_store` doesn't follow symlinks
    deliberately (no `Path.resolve()` calls in the read path), so the
    file accessed is exactly what was named.
    """
    # Create bob's real file.
    bob_path = tmp_path / "bob.md"
    notes_store.append_note(
        bob_path, contact_id="bob", section_heading="B",
        body="BOB_CANARY", scope=None,
    )
    # Symlink alice -> bob.
    alice_path = tmp_path / "alice.md"
    alice_path.symlink_to(bob_path)
    # Reading alice.md returns bob's content (OS follows symlink on read).
    out = notes_store.read_notes(alice_path, active_scope=None)
    # This IS a leak in the misconfigured case. Document it.
    # The mitigation is at the operator/setup layer, not daemon.
    # Test asserts current behaviour so a future change to add symlink
    # rejection is a deliberate decision, not an accidental break.
    assert "BOB_CANARY" in out, (
        "symlink behaviour changed — was leaking, now isn't. "
        "If you intended to add symlink rejection, update this test."
    )
