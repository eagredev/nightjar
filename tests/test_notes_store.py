"""Tests for daemon.notes_store — read, parse, append, scope-filter."""
from __future__ import annotations

from pathlib import Path

import pytest

from daemon import notes_store
from daemon.notes_store import (
    NotesParseError,
    ScopeContext,
    append_note,
    filtered_text,
    parse,
    prompt_text_two_axis,
    read_notes,
    read_notes_two_axis,
    read_safe_notes,
    safe_text,
)


# ---- Sample fixture -------------------------------------------------------


CANONICAL_SAMPLE = """---
contact_id: fraser
created_at: 2026-05-06T14:23:11Z
last_updated: 2026-05-06T18:42:03Z
---

## Aurora project [scopes: aurora]

- Working on track 3 of the OST.
- Prefers concrete examples. [scopes: aurora, music-tech]

## General

- Replies fastest in the evenings. [scopes: *]
- British English. [scopes: *]
"""


# ---- Frontmatter parsing --------------------------------------------------


def test_parse_extracts_frontmatter_keys() -> None:
    p = parse(CANONICAL_SAMPLE)
    assert p.frontmatter["contact_id"] == "fraser"
    assert p.frontmatter["created_at"] == "2026-05-06T14:23:11Z"
    assert p.frontmatter["last_updated"] == "2026-05-06T18:42:03Z"


def test_parse_handles_empty_input() -> None:
    p = parse("")
    assert p.frontmatter == {}
    assert p.sections == ()


def test_parse_handles_no_frontmatter() -> None:
    text = "## Section\n\n- bullet\n"
    p = parse(text)
    assert p.frontmatter == {}
    assert len(p.sections) == 1
    assert p.sections[0].heading == "Section"


def test_parse_unterminated_frontmatter_raises() -> None:
    # No closing `---`. The parser hits a non-key:value line before EOF
    # (the `## Section` heading) and raises. Distinct from the explicit
    # "not terminated" path below where the file ends mid-frontmatter
    # with no offending line.
    text = "---\nkey: value\n## Section\n"
    with pytest.raises(NotesParseError):
        parse(text)


def test_parse_eof_inside_frontmatter_raises() -> None:
    text = "---\nkey: value\n"
    with pytest.raises(NotesParseError, match="not terminated"):
        parse(text)


def test_parse_malformed_frontmatter_line_raises() -> None:
    text = "---\nthis is not key: value\nbut neither is this just words\n---\n"
    with pytest.raises(NotesParseError, match="key: value"):
        parse(text)


def test_parse_duplicate_frontmatter_key_raises() -> None:
    text = "---\nfoo: 1\nfoo: 2\n---\n"
    with pytest.raises(NotesParseError, match="duplicate"):
        parse(text)


# ---- Section / bullet parsing ---------------------------------------------


def test_parse_extracts_sections_and_scopes() -> None:
    p = parse(CANONICAL_SAMPLE)
    assert len(p.sections) == 2
    aurora = p.sections[0]
    assert aurora.heading == "Aurora project"
    assert aurora.scopes == ("aurora",)
    general = p.sections[1]
    assert general.heading == "General"
    assert general.scopes == ()


def test_parse_extracts_bullets_with_scope_overrides() -> None:
    p = parse(CANONICAL_SAMPLE)
    aurora_bullets = p.sections[0].bullets
    assert len(aurora_bullets) == 2
    assert aurora_bullets[0].text == "Working on track 3 of the OST."
    assert aurora_bullets[0].scopes == ()
    assert aurora_bullets[1].text == "Prefers concrete examples."
    assert aurora_bullets[1].scopes == ("aurora", "music-tech")


def test_parse_handles_wildcard_scope() -> None:
    p = parse(CANONICAL_SAMPLE)
    bullets = p.sections[1].bullets
    assert all(b.scopes == ("*",) for b in bullets)


def test_parse_empty_scope_tag_raises() -> None:
    with pytest.raises(NotesParseError, match="empty"):
        parse("## Section [scopes: ]\n\n- bullet\n")


def test_parse_only_commas_scope_tag_raises() -> None:
    with pytest.raises(NotesParseError, match="empty"):
        parse("## Section\n\n- bullet [scopes: , , ]\n")


# ---- Round-trip / idempotency --------------------------------------------


def test_serialize_canonical_input_roundtrips_byte_identical() -> None:
    parsed = parse(CANONICAL_SAMPLE)
    assert notes_store._serialize(parsed) == CANONICAL_SAMPLE


def test_serialize_is_idempotent() -> None:
    once = notes_store._serialize(parse(CANONICAL_SAMPLE))
    twice = notes_store._serialize(parse(once))
    assert once == twice


def test_serialize_normalizes_messy_blank_lines() -> None:
    # Same content as canonical but with extra blanks; should normalize.
    messy = """---
contact_id: fraser
created_at: 2026-05-06T14:23:11Z
last_updated: 2026-05-06T18:42:03Z
---



## Aurora project [scopes: aurora]



- Working on track 3 of the OST.
- Prefers concrete examples. [scopes: aurora, music-tech]



## General

- Replies fastest in the evenings. [scopes: *]
- British English. [scopes: *]
"""
    once = notes_store._serialize(parse(messy))
    assert once == CANONICAL_SAMPLE
    # And after one normalization, it stays put.
    twice = notes_store._serialize(parse(once))
    assert twice == once


# ---- Scope filtering ------------------------------------------------------


def test_filtered_text_active_scope_aurora() -> None:
    parsed = parse(CANONICAL_SAMPLE)
    out = filtered_text(parsed, "aurora")
    # All aurora-section bullets visible (section default is aurora)
    assert "Working on track 3" in out
    assert "Prefers concrete examples" in out
    # General section's wildcards visible
    assert "Replies fastest" in out
    assert "British English" in out


def test_filtered_text_active_scope_music_tech_drops_unscoped_aurora_bullet() -> None:
    parsed = parse(CANONICAL_SAMPLE)
    out = filtered_text(parsed, "music-tech")
    # First aurora bullet has no override and inherits aurora-only.
    assert "Working on track 3" not in out
    # Second aurora bullet explicitly tagged aurora,music-tech.
    assert "Prefers concrete examples" in out
    # Wildcards always visible.
    assert "Replies fastest" in out


def test_filtered_text_active_scope_personal_keeps_only_wildcards() -> None:
    parsed = parse(CANONICAL_SAMPLE)
    out = filtered_text(parsed, "personal")
    assert "Working on track 3" not in out
    assert "Prefers concrete examples" not in out
    assert "Replies fastest" in out
    assert "British English" in out


def test_filtered_text_none_returns_everything() -> None:
    parsed = parse(CANONICAL_SAMPLE)
    out = filtered_text(parsed, None)
    assert "Working on track 3" in out
    assert "Prefers concrete examples" in out
    assert "Replies fastest" in out
    assert "British English" in out


def test_filtered_text_drops_section_with_no_surviving_bullets() -> None:
    text = """---
contact_id: alice
---

## Work [scopes: work]

- Project plan.

## Personal [scopes: personal]

- Loves hiking.
"""
    parsed = parse(text)
    out = filtered_text(parsed, "work")
    assert "## Work" in out
    assert "## Personal" not in out
    assert "Project plan" in out
    assert "Loves hiking" not in out


def test_filtered_text_excludes_scope_tag_from_render() -> None:
    """Bullets emitted by filtered_text should not carry the visibility
    tag — the LLM doesn't need it."""
    parsed = parse(CANONICAL_SAMPLE)
    out = filtered_text(parsed, "aurora")
    assert "[scopes:" not in out


def test_filtered_text_omits_frontmatter() -> None:
    """Frontmatter is daemon metadata — never sent to the LLM."""
    parsed = parse(CANONICAL_SAMPLE)
    out = filtered_text(parsed, None)
    assert "contact_id:" not in out
    assert "created_at:" not in out


# ---- safe_text (classifier-only filter) -----------------------------------


def test_safe_text_keeps_unscoped_section_bullets() -> None:
    """A section without a scope tag is wildcard by default; its
    bullets without their own override are safe."""
    text = """---
contact_id: alice
---

## General

- Replies fastest in the evenings.
- Uses British English.
"""
    parsed = parse(text)
    out = safe_text(parsed)
    assert "Replies fastest" in out
    assert "British English" in out


def test_safe_text_drops_scoped_section_unscoped_bullet() -> None:
    """A bullet under a `[scopes: aurora]` section without its own
    override inherits aurora — NOT safe for the classifier."""
    text = """---
contact_id: fraser
---

## Aurora project [scopes: aurora]

- Working on track 3.
"""
    parsed = parse(text)
    out = safe_text(parsed)
    assert "Working on track 3" not in out
    assert "## Aurora project" not in out


def test_safe_text_keeps_wildcard_bullet_in_scoped_section() -> None:
    """A `[scopes: *]` bullet inside an otherwise-scoped section
    appears regardless of section scope."""
    text = """---
contact_id: fraser
---

## Aurora project [scopes: aurora]

- Specific aurora detail.
- General communication style. [scopes: *]
"""
    parsed = parse(text)
    out = safe_text(parsed)
    assert "Specific aurora detail" not in out
    assert "General communication style" in out
    # Section heading appears because at least one bullet survived.
    assert "## Aurora project" in out


def test_safe_text_drops_aurora_only_bullet_in_unscoped_section() -> None:
    """A bullet with its own [scopes: aurora] override in an unscoped
    section is aurora-only — NOT safe."""
    text = """---
contact_id: x
---

## General

- Uses British English. [scopes: *]
- Aurora deadline 2026-05-15. [scopes: aurora]
"""
    parsed = parse(text)
    out = safe_text(parsed)
    assert "Uses British English" in out
    assert "Aurora deadline" not in out


def test_safe_text_drops_entirely_scoped_files() -> None:
    """A file with only scoped content has empty safe_text."""
    text = """---
contact_id: x
---

## Aurora [scopes: aurora]

- Detail.

## Personal [scopes: personal]

- Detail.
"""
    parsed = parse(text)
    assert safe_text(parsed) == ""


def test_safe_text_excludes_scope_tag_from_render() -> None:
    text = """---
contact_id: x
---

## General

- Bullet one. [scopes: *]
"""
    parsed = parse(text)
    out = safe_text(parsed)
    assert "[scopes:" not in out


def test_read_safe_notes_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_safe_notes(tmp_path / "nope.md") == ""


def test_read_safe_notes_filters_real_file(tmp_path: Path) -> None:
    p = tmp_path / "fraser.md"
    p.write_text(CANONICAL_SAMPLE, encoding="utf-8")
    out = read_safe_notes(p)
    # Aurora-section content (incl. dual-scope bullet that contains aurora)
    # is NOT safe — aurora scope leaks would defeat the purpose.
    assert "Working on track 3" not in out
    assert "Prefers concrete examples" not in out
    # General section's wildcards survive.
    assert "Replies fastest" in out
    assert "British English" in out


def test_read_safe_notes_propagates_parse_errors(tmp_path: Path) -> None:
    p = tmp_path / "broken.md"
    p.write_text("---\nfoo\n---\n", encoding="utf-8")
    with pytest.raises(NotesParseError):
        read_safe_notes(p)


# ---- read_notes (with filesystem) -----------------------------------------


def test_read_notes_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_notes(tmp_path / "nope.md", "aurora") == ""


def test_read_notes_reads_and_filters(tmp_path: Path) -> None:
    p = tmp_path / "fraser.md"
    p.write_text(CANONICAL_SAMPLE, encoding="utf-8")
    out = read_notes(p, "aurora")
    assert "Working on track 3" in out
    assert "Replies fastest" in out


def test_read_notes_propagates_parse_errors(tmp_path: Path) -> None:
    p = tmp_path / "broken.md"
    p.write_text("---\nfoo\n---\n", encoding="utf-8")
    with pytest.raises(NotesParseError):
        read_notes(p, "any")


# ---- append_note ----------------------------------------------------------


def test_append_note_creates_file_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "alice.md"
    append_note(
        p,
        contact_id="alice",
        section_heading="Aurora project",
        body="Initial note.",
        scope="aurora",
        now_iso="2026-05-06T15:00:00Z",
    )
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "contact_id: alice" in text
    assert "created_at: 2026-05-06T15:00:00Z" in text
    assert "last_updated: 2026-05-06T15:00:00Z" in text
    assert "## Aurora project [scopes: aurora]" in text
    assert "- Initial note. [scopes: aurora]" in text


def test_append_note_appends_to_existing_section(tmp_path: Path) -> None:
    p = tmp_path / "fraser.md"
    p.write_text(CANONICAL_SAMPLE, encoding="utf-8")
    append_note(
        p,
        contact_id="fraser",
        section_heading="Aurora project",
        body="New deadline 2026-05-15.",
        scope="aurora",
        now_iso="2026-05-06T19:00:00Z",
    )
    text = p.read_text(encoding="utf-8")
    # All originals still present.
    assert "Working on track 3 of the OST." in text
    assert "Prefers concrete examples." in text
    # New bullet appended to Aurora.
    assert "- New deadline 2026-05-15. [scopes: aurora]" in text
    # last_updated bumped.
    assert "last_updated: 2026-05-06T19:00:00Z" in text
    # created_at preserved.
    assert "created_at: 2026-05-06T14:23:11Z" in text


def test_append_note_creates_new_section_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "fraser.md"
    p.write_text(CANONICAL_SAMPLE, encoding="utf-8")
    append_note(
        p,
        contact_id="fraser",
        section_heading="Personal",
        body="Loves hiking.",
        scope="personal",
        now_iso="2026-05-06T19:00:00Z",
    )
    text = p.read_text(encoding="utf-8")
    assert "## Personal [scopes: personal]" in text
    assert "- Loves hiking. [scopes: personal]" in text
    # Existing sections untouched.
    assert "## Aurora project [scopes: aurora]" in text


def test_append_note_no_scope_creates_unscoped_section(tmp_path: Path) -> None:
    p = tmp_path / "alice.md"
    append_note(
        p,
        contact_id="alice",
        section_heading="General",
        body="No scope on this one.",
        scope=None,
        now_iso="2026-05-06T15:00:00Z",
    )
    text = p.read_text(encoding="utf-8")
    assert "## General\n" in text
    assert "- No scope on this one.\n" in text
    # No spurious scope tag.
    assert "[scopes:" not in text


def test_append_note_writes_atomically_no_tmp_left_on_success(tmp_path: Path) -> None:
    """After a successful append, no .tmp file should remain in the
    directory. Catches the case where mkstemp's tmp leaks on the
    happy path."""
    p = tmp_path / "fraser.md"
    p.write_text(CANONICAL_SAMPLE, encoding="utf-8")
    append_note(
        p,
        contact_id="fraser",
        section_heading="Aurora project",
        body="Atomic check.",
        scope="aurora",
        now_iso="2026-05-06T19:00:00Z",
    )
    leftovers = [f for f in tmp_path.iterdir() if f.name.startswith(".")]
    assert leftovers == [], f"unexpected tmp files: {leftovers}"


def test_append_note_sets_chmod_600(tmp_path: Path) -> None:
    p = tmp_path / "alice.md"
    append_note(
        p,
        contact_id="alice",
        section_heading="General",
        body="A note.",
        scope=None,
        now_iso="2026-05-06T15:00:00Z",
    )
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_append_note_serialized_output_roundtrips(tmp_path: Path) -> None:
    """After appending, parse(read) should re-serialize to the same
    bytes — i.e. append produces canonical output."""
    p = tmp_path / "fraser.md"
    p.write_text(CANONICAL_SAMPLE, encoding="utf-8")
    append_note(
        p,
        contact_id="fraser",
        section_heading="Aurora project",
        body="Round-trip check.",
        scope="aurora",
        now_iso="2026-05-06T19:00:00Z",
    )
    text = p.read_text(encoding="utf-8")
    reparsed = notes_store._serialize(parse(text))
    assert reparsed == text


def test_append_note_sets_created_at_only_on_first_write(tmp_path: Path) -> None:
    p = tmp_path / "alice.md"
    append_note(
        p,
        contact_id="alice",
        section_heading="A",
        body="first",
        scope=None,
        now_iso="2026-05-06T10:00:00Z",
    )
    append_note(
        p,
        contact_id="alice",
        section_heading="A",
        body="second",
        scope=None,
        now_iso="2026-05-06T11:00:00Z",
    )
    text = p.read_text(encoding="utf-8")
    assert "created_at: 2026-05-06T10:00:00Z" in text
    assert "last_updated: 2026-05-06T11:00:00Z" in text


# ---- Provenance tagging (Step 7d+ red-team mitigation) -------------------


def test_append_note_writes_meta_tag_with_attribution_and_src(tmp_path: Path) -> None:
    """Provenance fields serialize as `[meta: src=...; attr=...]`
    BEFORE the existing scopes tag, so the on-disk shape is
    `text [meta: ...] [scopes: ...]`. Both are stripped from the
    LLM-facing render path; the principal-facing audit_text shows them.
    """
    p = tmp_path / "fraser.md"
    append_note(
        p,
        contact_id="fraser",
        section_heading="Project status",
        body="Sender claims the deadline moved.",
        scope="nightjar-dev",
        attribution="asserted",
        source_message_id="<abc123@example>",
        now_iso="2026-05-06T15:00:00Z",
    )
    text = p.read_text(encoding="utf-8")
    assert "## Project status [scopes: nightjar-dev]" in text
    # Tag order: text, then meta, then scopes.
    assert (
        "- Sender claims the deadline moved. "
        "[meta: src=<abc123@example>; attr=asserted] "
        "[scopes: nightjar-dev]"
    ) in text


def test_append_note_back_compat_writes_no_meta_when_unset(tmp_path: Path) -> None:
    """Legacy callers that don't pass attribution/src get the same
    on-disk shape as before."""
    p = tmp_path / "fraser.md"
    append_note(
        p,
        contact_id="fraser",
        section_heading="General",
        body="Legacy bullet.",
        scope=None,
        now_iso="2026-05-06T15:00:00Z",
    )
    text = p.read_text(encoding="utf-8")
    assert "[meta:" not in text
    assert "- Legacy bullet.\n" in text


def test_append_note_rejects_unknown_attribution(tmp_path: Path) -> None:
    p = tmp_path / "fraser.md"
    with pytest.raises(ValueError, match="attribution must be one of"):
        append_note(
            p,
            contact_id="fraser",
            section_heading="X",
            body="bad",
            scope=None,
            attribution="hearsay",  # not in the known set
            source_message_id="<x@y>",
            now_iso="2026-05-06T15:00:00Z",
        )


def test_parse_extracts_meta_tag_from_bullet() -> None:
    text = (
        "## Section [scopes: nightjar-dev]\n"
        "\n"
        "- Bullet text. [meta: src=<m1@example>; attr=observed] "
        "[scopes: nightjar-dev]\n"
    )
    p = parse(text)
    bullet = p.sections[0].bullets[0]
    assert bullet.text == "Bullet text."
    assert bullet.attribution == "observed"
    assert bullet.source_message_id == "<m1@example>"
    # Scopes still parsed correctly (existing behaviour preserved).
    assert bullet.scopes == ("nightjar-dev",)


def test_parse_handles_meta_only_without_scopes() -> None:
    text = (
        "## Section\n"
        "\n"
        "- Untagged. [meta: src=<m@x>; attr=self]\n"
    )
    p = parse(text)
    bullet = p.sections[0].bullets[0]
    assert bullet.text == "Untagged."
    assert bullet.attribution == "self"
    assert bullet.source_message_id == "<m@x>"
    assert bullet.scopes == ()


def test_parse_handles_legacy_bullet_without_meta() -> None:
    """Bullets predating provenance — no meta tag — parse with empty
    attribution and source_message_id fields. Back-compat invariant."""
    text = (
        "## Section\n"
        "\n"
        "- Plain bullet. [scopes: *]\n"
    )
    p = parse(text)
    bullet = p.sections[0].bullets[0]
    assert bullet.text == "Plain bullet."
    assert bullet.attribution == ""
    assert bullet.source_message_id == ""


def test_parse_unknown_attribution_falls_back_to_empty() -> None:
    """Hand-edited file with garbage attr — fail-soft, treat as legacy."""
    text = (
        "## Section\n"
        "\n"
        "- Bullet. [meta: src=<x@y>; attr=guesswork]\n"
    )
    p = parse(text)
    bullet = p.sections[0].bullets[0]
    assert bullet.attribution == ""
    # src still parses
    assert bullet.source_message_id == "<x@y>"


def test_filtered_text_strips_meta_tag_from_llm_render() -> None:
    """The LLM never sees provenance metadata — filtered_text drops
    both the meta tag and scopes tag."""
    text = (
        "## Section\n"
        "\n"
        "- A claim. [meta: src=<m@x>; attr=asserted] [scopes: *]\n"
    )
    p = parse(text)
    rendered = filtered_text(p, active_scope=None)
    assert "[meta:" not in rendered
    assert "[scopes:" not in rendered
    assert "src=" not in rendered
    assert "attr=" not in rendered
    assert "- A claim." in rendered


def test_safe_text_strips_meta_tag_from_classifier_render() -> None:
    """Pass-1 classifier render also strips provenance metadata."""
    text = (
        "## Section\n"
        "\n"
        "- Universal bullet. [meta: src=<m@x>; attr=observed]\n"
    )
    p = parse(text)
    rendered = safe_text(p)
    assert "[meta:" not in rendered
    assert "src=" not in rendered
    assert "- Universal bullet." in rendered


def test_audit_text_flags_asserted_bullets() -> None:
    """show-notes uses audit_text. Asserted bullets get a visible
    UNVERIFIED warning."""
    text = (
        "## Project [scopes: nightjar-dev]\n"
        "\n"
        "- Sender claimed Dylan approved X. [meta: src=<m1@x>; attr=asserted]"
        " [scopes: nightjar-dev]\n"
        "- Daemon observed terse style. [meta: src=<m2@x>; attr=observed]"
        " [scopes: nightjar-dev]\n"
    )
    p = parse(text)
    rendered = notes_store.audit_text(p)
    # Asserted bullet flagged.
    assert "Sender claimed Dylan approved X." in rendered
    assert "asserted by sender — unverified" in rendered
    assert "<m1@x>" in rendered
    # Observed bullet not flagged.
    assert "Daemon observed terse style." in rendered
    # No double-flagging — observed bullet doesn't carry the warning text.
    obs_line = [
        line for line in rendered.splitlines()
        if "Daemon observed terse style" in line
    ][0]
    assert "unverified" not in obs_line


def test_audit_text_flags_self_bullets() -> None:
    text = (
        "## Section\n"
        "\n"
        "- Sender said they prefer mornings. [meta: src=<m@x>; attr=self]\n"
    )
    p = parse(text)
    rendered = notes_store.audit_text(p)
    assert "self-asserted by sender — unverified" in rendered


def test_audit_text_does_not_flag_legacy_bullets() -> None:
    """Bullets without attribution metadata (legacy) render
    unannotated — back-compat."""
    text = (
        "## Section\n"
        "\n"
        "- Legacy bullet, no meta tag.\n"
    )
    p = parse(text)
    rendered = notes_store.audit_text(p)
    assert "- Legacy bullet, no meta tag." in rendered
    assert "unverified" not in rendered


# ---- prompt_text (wave 3b) -------------------------------------------------


def test_prompt_text_renders_observed_bare() -> None:
    """`attr=observed` bullets carry no provenance prefix — the daemon
    verified them, so no skepticism cue is needed."""
    text = (
        "## Style\n"
        "\n"
        "- Replies in evenings. [meta: src=<m1@x>; attr=observed]\n"
    )
    p = parse(text)
    rendered = notes_store.prompt_text(p, active_scope=None)
    assert "- Replies in evenings." in rendered
    assert "unverified" not in rendered
    # No square-bracket prefix in front of an observed bullet.
    assert "- [unverified" not in rendered


def test_prompt_text_renders_self_with_prefix() -> None:
    """`attr=self` bullets carry the sender's-own-claim prefix so the
    read-side skeptic rule has metadata to act on."""
    text = (
        "## Build state\n"
        "\n"
        "- TTL hardcoded at 600s. [meta: src=<m@x>; attr=self]\n"
    )
    p = parse(text)
    rendered = notes_store.prompt_text(p, active_scope=None)
    assert "- [unverified — sender's own claim] TTL hardcoded at 600s." in rendered


def test_prompt_text_renders_asserted_with_prefix() -> None:
    """`attr=asserted` bullets carry the third-party-claim prefix."""
    text = (
        "## Coordination\n"
        "\n"
        "- Dylan signed off on the merge. [meta: src=<m@x>; attr=asserted]\n"
    )
    p = parse(text)
    rendered = notes_store.prompt_text(p, active_scope=None)
    assert (
        "- [unverified — sender's claim about another party] "
        "Dylan signed off on the merge."
    ) in rendered


def test_prompt_text_renders_legacy_bullets_bare() -> None:
    """Legacy bullets (no meta tag) render bare. Back-compat: the
    on-disk file may carry pre-wave-2 bullets the principal hasn't
    re-tagged. The deterministic gate continues to read the file
    directly so legacy bullets aren't a free enumeration target."""
    text = (
        "## Section\n"
        "\n"
        "- Legacy bullet with no provenance.\n"
    )
    p = parse(text)
    rendered = notes_store.prompt_text(p, active_scope=None)
    assert "- Legacy bullet with no provenance." in rendered
    assert "unverified" not in rendered


def test_prompt_text_omits_source_message_id() -> None:
    """Source message-IDs are noise in the prompt — only audit_text
    renders them."""
    text = (
        "## Section\n"
        "\n"
        "- A claim. [meta: src=<msg42@example>; attr=self]\n"
    )
    p = parse(text)
    rendered = notes_store.prompt_text(p, active_scope=None)
    assert "msg42" not in rendered
    assert "src=" not in rendered


def test_prompt_text_honours_active_scope() -> None:
    """Same scope-policy as filtered_text: bullets/sections out of
    scope are dropped."""
    text = (
        "## Aurora [scopes: aurora]\n"
        "\n"
        "- Aurora bullet. [meta: src=<m1@x>; attr=observed]\n"
        "\n"
        "## Personal [scopes: personal]\n"
        "\n"
        "- Personal bullet. [meta: src=<m2@x>; attr=self]\n"
    )
    p = parse(text)
    rendered = notes_store.prompt_text(p, active_scope="aurora")
    assert "Aurora bullet." in rendered
    assert "Personal bullet." not in rendered
    # Personal bullet's prefix shouldn't survive the filter either.
    assert "sender's own claim" not in rendered


def test_prompt_text_drops_section_with_no_surviving_bullets() -> None:
    """If every bullet in a section is filtered out, the heading
    drops too."""
    text = (
        "## Aurora [scopes: aurora]\n"
        "\n"
        "- Aurora-only. [meta: src=<m@x>; attr=observed]\n"
    )
    p = parse(text)
    rendered = notes_store.prompt_text(p, active_scope="personal")
    assert rendered == ""


def test_prompt_text_omits_frontmatter() -> None:
    """Frontmatter is daemon metadata — never enters the prompt."""
    text = (
        "---\n"
        "contact_id: fraser\n"
        "created_at: 2026-05-06T14:23:11Z\n"
        "---\n"
        "\n"
        "## Section\n"
        "\n"
        "- A bullet. [meta: src=<m@x>; attr=observed]\n"
    )
    p = parse(text)
    rendered = notes_store.prompt_text(p, active_scope=None)
    assert "contact_id" not in rendered
    assert "fraser" not in rendered


def test_prompt_text_excludes_meta_and_scope_tags_from_render() -> None:
    """Per-bullet `[meta: ...]` and `[scopes: ...]` tags are stripped
    by the parser — they reach the prompt only as the leading
    provenance prefix, not as raw on-disk syntax."""
    text = (
        "## Project [scopes: nightjar-dev]\n"
        "\n"
        "- Bullet body. [meta: src=<m@x>; attr=self] [scopes: nightjar-dev]\n"
    )
    p = parse(text)
    rendered = notes_store.prompt_text(p, active_scope="nightjar-dev")
    assert "[meta:" not in rendered
    assert "[scopes:" not in rendered
    assert "attr=" not in rendered


# ---- read_notes wave-3b path verification ---------------------------------


def test_read_notes_carries_provenance_prefix_into_render(
    tmp_path: Path,
) -> None:
    """Wave 3b: the triage prompt path uses prompt_text, so read_notes
    output for a contact with provenance-tagged notes carries the
    sender-claim prefixes the read-side rule reasons about."""
    p = tmp_path / "sam.md"
    text = (
        "## Build state\n"
        "\n"
        "- TTL hardcoded at 600s. [meta: src=<m@x>; attr=self]\n"
        "- Dylan signed off. [meta: src=<m2@x>; attr=asserted]\n"
        "- Replies in evenings. [meta: src=<m3@x>; attr=observed]\n"
    )
    p.write_text(text, encoding="utf-8")
    rendered = read_notes(p, active_scope=None)
    assert "[unverified — sender's own claim] TTL hardcoded at 600s." in rendered
    assert (
        "[unverified — sender's claim about another party] Dylan signed off."
    ) in rendered
    # Observed bullet stays bare.
    obs_line = [
        line for line in rendered.splitlines()
        if "Replies in evenings" in line
    ][0]
    assert "unverified" not in obs_line


def test_append_then_parse_roundtrip_preserves_provenance(tmp_path: Path) -> None:
    """Round-trip: write with provenance → read back → fields intact."""
    p = tmp_path / "fraser.md"
    append_note(
        p,
        contact_id="fraser",
        section_heading="Project",
        body="Asserted claim.",
        scope="nightjar-dev",
        attribution="asserted",
        source_message_id="<round@trip>",
        now_iso="2026-05-06T15:00:00Z",
    )
    text = p.read_text(encoding="utf-8")
    parsed = parse(text)
    bullet = parsed.sections[0].bullets[0]
    assert bullet.text == "Asserted claim."
    assert bullet.attribution == "asserted"
    assert bullet.source_message_id == "<round@trip>"
    assert bullet.scopes == ("nightjar-dev",)


def test_append_note_with_provenance_idempotent_serialize(tmp_path: Path) -> None:
    """Re-serialising a parsed file with provenance produces
    byte-identical output — ensures the on-disk shape is canonical."""
    p = tmp_path / "fraser.md"
    append_note(
        p,
        contact_id="fraser",
        section_heading="Project",
        body="A note.",
        scope="nightjar-dev",
        attribution="self",
        source_message_id="<x@y>",
        now_iso="2026-05-06T15:00:00Z",
    )
    text = p.read_text(encoding="utf-8")
    re_serialized = notes_store._serialize(parse(text))
    assert re_serialized == text


# ===========================================================================
# Scope/sensitivity Part 1: two-axis visibility
# ===========================================================================


# A fixture that uses both facet-flat tags (calendar, communication-style)
# AND hierarchical project tags (aurora, aurora.music, aurora.legal).
TWO_AXIS_SAMPLE = """---
contact_id: fraser
created_at: 2026-05-06T14:23:11Z
last_updated: 2026-05-06T18:42:03Z
---

## Aurora overall [scopes: aurora]

- Project codename "Aurora", started Feb. [scopes: aurora]
- Generic context anyone with aurora access should see.

## Aurora music [scopes: aurora.music]

- Working on track 3 of the OST.
- Prefers reference tracks for feedback. [scopes: aurora.music]

## Aurora legal [scopes: aurora.legal]

- Contract review with the publisher pending. [scopes: aurora.legal]

## Scheduling [scopes: calendar]

- Available Tue/Thu evenings.
- Travelling 12-15 May. [scopes: calendar]

## Communication notes [scopes: communication-style]

- Prefers direct, short replies. [scopes: communication-style]

## General

- British English. [scopes: *]
- Wildcard fact visible to all scopes. [scopes: *]
"""


def _ctx(
    facets: tuple[str, ...] = (),
    projects: tuple[str, ...] = (),
) -> ScopeContext:
    return ScopeContext(
        facets=frozenset(facets),
        projects=frozenset(projects),
    )


# ---- prompt_text_two_axis: facets ----------------------------------------


def test_two_axis_facets_only_calendar() -> None:
    """Active calendar facet, no project: only calendar bullets +
    wildcards visible."""
    parsed = parse(TWO_AXIS_SAMPLE)
    out = prompt_text_two_axis(parsed, _ctx(facets=("calendar",)))
    assert "Available Tue/Thu evenings." in out
    assert "Travelling 12-15 May." in out
    assert "British English." in out
    assert "Wildcard fact visible to all scopes." in out
    # Project content not visible.
    assert "Working on track 3" not in out
    assert "Aurora music" not in out
    # Other-facet content not visible.
    assert "Prefers direct, short replies." not in out


def test_two_axis_multiple_facets() -> None:
    """Multiple facets active: union of their bullets visible."""
    parsed = parse(TWO_AXIS_SAMPLE)
    out = prompt_text_two_axis(
        parsed, _ctx(facets=("calendar", "communication-style")),
    )
    assert "Available Tue/Thu evenings." in out
    assert "Prefers direct, short replies." in out
    assert "British English." in out  # wildcard
    # No project content.
    assert "Working on track 3" not in out


# ---- prompt_text_two_axis: project hierarchy -----------------------------


def test_two_axis_project_subscope_sees_parent_content() -> None:
    """Active context = aurora.music. Bullet tagged `aurora` is
    visible (parent content shown to a child contact); bullet tagged
    `aurora.music` is visible (exact); bullet tagged `aurora.legal`
    is NOT (sibling)."""
    parsed = parse(TWO_AXIS_SAMPLE)
    out = prompt_text_two_axis(parsed, _ctx(projects=("aurora.music",)))
    assert 'Project codename "Aurora"' in out  # parent-tagged bullet
    assert "Working on track 3" in out  # exact-match bullet
    # Sibling sub-scope must NOT leak.
    assert "Contract review" not in out


def test_two_axis_parent_project_sees_all_subscopes() -> None:
    """Active context = aurora. ALL aurora.* sub-scopes' content is
    visible (parent subsumes children)."""
    parsed = parse(TWO_AXIS_SAMPLE)
    out = prompt_text_two_axis(parsed, _ctx(projects=("aurora",)))
    assert 'Project codename "Aurora"' in out
    assert "Working on track 3" in out
    assert "Contract review" in out  # sibling visible from parent
    # Wildcards still visible.
    assert "British English." in out


def test_two_axis_sibling_subscopes_isolated() -> None:
    """Active context = aurora.legal. aurora.music content is NOT
    visible (sibling sub-scopes do not see each other)."""
    parsed = parse(TWO_AXIS_SAMPLE)
    out = prompt_text_two_axis(parsed, _ctx(projects=("aurora.legal",)))
    assert "Contract review" in out
    assert 'Project codename "Aurora"' in out  # parent visible
    # Sibling NOT visible.
    assert "Working on track 3" not in out
    assert "Prefers reference tracks" not in out


def test_two_axis_combined_facet_and_project() -> None:
    """Active context combines a facet and a project — both axes'
    content is visible."""
    parsed = parse(TWO_AXIS_SAMPLE)
    out = prompt_text_two_axis(
        parsed,
        _ctx(facets=("calendar",), projects=("aurora.music",)),
    )
    # Project axis
    assert "Working on track 3" in out
    assert 'Project codename "Aurora"' in out
    # Facet axis
    assert "Available Tue/Thu evenings." in out
    # Wildcard
    assert "British English." in out
    # Other facet not active
    assert "Prefers direct, short replies." not in out
    # Sibling sub-scope not active
    assert "Contract review" not in out


# ---- prompt_text_two_axis: edge cases ------------------------------------


def test_two_axis_empty_context_keeps_only_wildcards() -> None:
    """Fail-closed shape: ScopeContext.empty() keeps only `*`-tagged
    content. Triage caller falls back to this when classifier fails."""
    parsed = parse(TWO_AXIS_SAMPLE)
    out = prompt_text_two_axis(parsed, ScopeContext.empty())
    assert "British English." in out
    assert "Wildcard fact visible to all scopes." in out
    # Nothing else.
    assert "Working on track 3" not in out
    assert "Available Tue/Thu evenings." not in out
    assert "Prefers direct, short replies." not in out


def test_two_axis_full_audit_returns_everything() -> None:
    """ScopeContext.make_full_audit() ignores tags — used by the
    principal-facing audit dump."""
    parsed = parse(TWO_AXIS_SAMPLE)
    out = prompt_text_two_axis(parsed, ScopeContext.make_full_audit())
    assert "Working on track 3" in out
    assert "Contract review" in out
    assert "Available Tue/Thu evenings." in out
    assert "Prefers direct, short replies." in out
    assert "British English." in out


def test_two_axis_drops_section_with_no_surviving_bullets() -> None:
    """A section whose every bullet is filtered out drops entirely
    — same convention as filtered_text."""
    parsed = parse(TWO_AXIS_SAMPLE)
    out = prompt_text_two_axis(parsed, _ctx(facets=("calendar",)))
    # The Aurora music heading must NOT appear; calendar is not
    # related, all bullets in that section are filtered.
    assert "## Aurora music" not in out


def test_two_axis_carries_provenance_prefix() -> None:
    """The two-axis renderer reuses prompt_text's per-bullet provenance
    prefix — wave-3b A's read-side metadata stays load-bearing."""
    text = """---
contact_id: x
created_at: 2026-05-06T14:23:11Z
last_updated: 2026-05-06T14:23:11Z
---

## Aurora work [scopes: aurora.music]

- They claim track 3 is done. [meta: src=msg-1; attr=self]
"""
    parsed = parse(text)
    out = prompt_text_two_axis(parsed, _ctx(projects=("aurora.music",)))
    assert "[unverified — sender's own claim]" in out


def test_two_axis_unrelated_project_not_visible() -> None:
    parsed = parse(TWO_AXIS_SAMPLE)
    out = prompt_text_two_axis(parsed, _ctx(projects=("nightjar-dev",)))
    # No aurora content, no calendar content; only wildcards.
    assert 'Project codename "Aurora"' not in out
    assert "Working on track 3" not in out
    assert "British English." in out


# ---- read_notes_two_axis (file IO + parse) -------------------------------


def test_read_notes_two_axis_missing_file_returns_empty(tmp_path: Path) -> None:
    out = read_notes_two_axis(
        tmp_path / "nonexistent.md",
        _ctx(projects=("aurora",)),
    )
    assert out == ""


def test_read_notes_two_axis_reads_and_filters(tmp_path: Path) -> None:
    p = tmp_path / "fraser.md"
    p.write_text(TWO_AXIS_SAMPLE, encoding="utf-8")
    out = read_notes_two_axis(p, _ctx(projects=("aurora.music",)))
    assert "Working on track 3" in out
    assert "Contract review" not in out  # sibling


def test_read_notes_two_axis_raises_on_malformed(tmp_path: Path) -> None:
    p = tmp_path / "fraser.md"
    p.write_text("---\nbad frontmatter\n", encoding="utf-8")
    with pytest.raises(NotesParseError):
        read_notes_two_axis(p, _ctx(projects=("aurora",)))


# ---- ScopeContext factories ----------------------------------------------


def test_scope_context_empty_factory() -> None:
    ctx = ScopeContext.empty()
    assert ctx.facets == frozenset()
    assert ctx.projects == frozenset()
    assert ctx.full_audit is False


def test_scope_context_full_audit_factory() -> None:
    ctx = ScopeContext.make_full_audit()
    assert ctx.full_audit is True


def test_scope_context_full_audit_overrides_empty_axes() -> None:
    """Even with empty facet/project sets, full_audit=True must
    return everything — the caller is the principal asking for an
    audit dump."""
    parsed = parse(TWO_AXIS_SAMPLE)
    out = prompt_text_two_axis(parsed, ScopeContext.make_full_audit())
    # Everything should be present.
    assert "Working on track 3" in out
    assert "Contract review" in out
