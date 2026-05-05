"""Tests for daemon.notes_store — read, parse, append, scope-filter."""
from __future__ import annotations

from pathlib import Path

import pytest

from daemon import notes_store
from daemon.notes_store import (
    NotesParseError,
    append_note,
    filtered_text,
    parse,
    read_notes,
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
