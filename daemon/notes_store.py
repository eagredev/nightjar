"""Per-contact rapport notes — read, parse, append.

Notes are markdown files at `config.daemon.notes_dir / <contact_id>.md`,
one per contact. They hold the daemon's accumulated memory of who the
contact is, what they're working on, and what context triage should
have access to during interactions.

File shape (the Step 7 design):

    ---
    contact_id: fraser
    created_at: 2026-05-06T14:23:11Z
    last_updated: 2026-05-06T18:42:03Z
    ---

    ## Aurora project [scopes: aurora]

    - Working on track 3 of the OST as of early May.
    - Prefers concrete examples in feedback. [scopes: aurora, music-tech]

    ## General

    - Replies fastest in the evenings. [scopes: *]

Visibility resolution: each section heading carries an optional
`[scopes: ...]` tag. Each bullet under a section may carry its own
`[scopes: ...]` overriding the section default. `[scopes: *]` means
visible regardless of active scope. A heading or bullet without an
explicit tag inherits — for sections, from the file default (or `*`
if none); for bullets, from the section.

`read_notes(path, active_scope)` returns the prompt-ready filtered
view: only sections / bullets whose scopes include `active_scope` (or
`*`) survive. The unselected content is *omitted*, not redacted —
triage never sees it.

`append_note(path, section, body, scope)` writes atomically (tmp +
fsync + chmod 600 + rename, matching contacts_writer / setup_auth).
The function appends a bullet under an existing section heading or
creates the section if it doesn't exist; either way the
`last_updated` frontmatter field is bumped to `now`.

Why hand-rolled instead of `markdown` / PyYAML: stdlib-only constraint.
The format is intentionally narrow (frontmatter + h2 sections + bullet
list), so the parser is small and the round-trip property is testable.
"""
from __future__ import annotations

import datetime
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


# ---- Errors ---------------------------------------------------------------


class NotesParseError(ValueError):
    """Raised when a notes file is malformed in a way the parser refuses
    to guess at. The daemon fails closed: triage sees no notes for that
    contact rather than a partial parse."""


# ---- Parsed shape ---------------------------------------------------------


@dataclass(frozen=True)
class Bullet:
    """A single bullet under a section.

    `text` is the bullet body without the leading `- ` and without any
    trailing `[scopes: ...]` annotation. `scopes` is the resolved scope
    tuple (empty tuple = inherit from section). `raw_line` preserves
    the exact source line for round-trip serialization."""
    text: str
    scopes: tuple[str, ...]
    raw_line: str


@dataclass(frozen=True)
class Section:
    """A `## heading` section.

    `heading` is the heading text without the `## ` prefix and without
    the trailing `[scopes: ...]` annotation. `scopes` is the resolved
    scope tuple for the section as a whole (empty tuple = file
    default). `bullets` are the bullet lines under it. `trailing_blank`
    captures whether the section ended with a blank line in the source
    (preserved on round-trip)."""
    heading: str
    scopes: tuple[str, ...]
    bullets: tuple[Bullet, ...]
    raw_heading_line: str


@dataclass(frozen=True)
class ParsedNotes:
    """In-memory representation of a notes file.

    `frontmatter` is an ordered dict of the YAML-ish key:value pairs.
    `preamble_lines` are any text lines between the closing `---` and
    the first `## ` section (typically empty, but preserved verbatim
    if present). `sections` are the parsed h2 sections. `raw_text` is
    the original source — used by the round-trip property test."""
    frontmatter: dict[str, str]
    preamble_lines: tuple[str, ...]
    sections: tuple[Section, ...]
    raw_text: str = ""


# ---- Scope-tag parsing ----------------------------------------------------


_SCOPES_TAG_RE = re.compile(r"\s*\[scopes:\s*([^\]]*)\]\s*$")


def _split_scopes_tag(line: str) -> tuple[str, tuple[str, ...]]:
    """Strip a trailing `[scopes: a, b]` tag if present.

    Returns (line_without_tag, scope_tuple). Empty tuple = no tag.
    The scope `*` is preserved as-is (caller decides what wildcard
    means)."""
    m = _SCOPES_TAG_RE.search(line)
    if not m:
        return line.rstrip(), ()
    raw = m.group(1).strip()
    if not raw:
        # `[scopes:]` is malformed — refuse to guess.
        raise NotesParseError(
            f"empty scopes tag in line: {line!r}"
        )
    parts = tuple(s.strip() for s in raw.split(",") if s.strip())
    if not parts:
        raise NotesParseError(
            f"empty scopes tag (only commas) in line: {line!r}"
        )
    head = line[: m.start()].rstrip()
    return head, parts


def _format_scopes_tag(scopes: tuple[str, ...]) -> str:
    """Inverse of _split_scopes_tag for serialization."""
    if not scopes:
        return ""
    return f" [scopes: {', '.join(scopes)}]"


# ---- Parsing --------------------------------------------------------------


_FRONTMATTER_DELIM = "---"
_FRONTMATTER_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
_HEADING_RE = re.compile(r"^##\s+(.*)$")
_BULLET_RE = re.compile(r"^-\s+(.*)$")


def parse(text: str) -> ParsedNotes:
    """Parse a notes file's text into ParsedNotes.

    Raises NotesParseError on malformed frontmatter or scope tags.
    Empty input yields ParsedNotes with empty frontmatter / sections."""
    lines = text.splitlines()
    i = 0

    # Frontmatter: optional, starts and ends with `---` on its own line.
    frontmatter: dict[str, str] = {}
    if lines and lines[0].rstrip() == _FRONTMATTER_DELIM:
        i = 1
        while i < len(lines) and lines[i].rstrip() != _FRONTMATTER_DELIM:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            m = _FRONTMATTER_LINE_RE.match(line)
            if not m:
                raise NotesParseError(
                    f"frontmatter line {i + 1} not key: value: {line!r}"
                )
            key, value = m.group(1), m.group(2).strip()
            if key in frontmatter:
                raise NotesParseError(
                    f"duplicate frontmatter key {key!r} at line {i + 1}"
                )
            frontmatter[key] = value
            i += 1
        if i >= len(lines):
            raise NotesParseError("frontmatter not terminated by '---'")
        i += 1  # skip closing ---

    # Preamble: any lines before the first ## heading. We strip
    # leading and trailing blanks because the serializer always emits
    # the canonical blank line after frontmatter and before the first
    # heading; preserving the source's blanks here would compound on
    # round-trip and break idempotency.
    preamble_start = i
    while i < len(lines) and not _HEADING_RE.match(lines[i]):
        i += 1
    preamble_raw = lines[preamble_start:i]
    while preamble_raw and not preamble_raw[0].strip():
        preamble_raw.pop(0)
    while preamble_raw and not preamble_raw[-1].strip():
        preamble_raw.pop()
    preamble_lines = tuple(preamble_raw)

    # Sections.
    sections: list[Section] = []
    while i < len(lines):
        heading_line = lines[i]
        m = _HEADING_RE.match(heading_line)
        if not m:
            # We only enter the loop on a heading; defensive.
            i += 1
            continue
        heading_body = m.group(1)
        heading_text, heading_scopes = _split_scopes_tag(heading_body)
        i += 1

        # Collect lines until next heading or EOF.
        section_lines: list[str] = []
        while i < len(lines) and not _HEADING_RE.match(lines[i]):
            section_lines.append(lines[i])
            i += 1

        bullets = _parse_bullets(section_lines)
        sections.append(Section(
            heading=heading_text,
            scopes=heading_scopes,
            bullets=bullets,
            raw_heading_line=heading_line,
        ))

    return ParsedNotes(
        frontmatter=frontmatter,
        preamble_lines=preamble_lines,
        sections=tuple(sections),
        raw_text=text,
    )


def _parse_bullets(lines: list[str]) -> tuple[Bullet, ...]:
    """Extract `- ...` bullet lines from a section body. Non-bullet
    lines are ignored (preserved into the round-trip via raw_text but
    not surfaced as Bullets)."""
    bullets: list[Bullet] = []
    for line in lines:
        m = _BULLET_RE.match(line)
        if not m:
            continue
        body = m.group(1)
        text, scopes = _split_scopes_tag(body)
        bullets.append(Bullet(text=text, scopes=scopes, raw_line=line))
    return tuple(bullets)


# ---- Visibility resolution ------------------------------------------------


_WILDCARD_SCOPE = "*"


def _scope_matches(scopes: tuple[str, ...], active_scope: str | None) -> bool:
    """A bullet/section is visible when:
      - active_scope is None (caller wants everything), OR
      - scopes contains '*' (always-visible), OR
      - scopes contains active_scope.
    Empty scopes tuple means inherit — callers should resolve
    inheritance before calling this."""
    if active_scope is None:
        return True
    if _WILDCARD_SCOPE in scopes:
        return True
    return active_scope in scopes


def filtered_text(parsed: ParsedNotes, active_scope: str | None) -> str:
    """Render `parsed` to a prompt-ready string, including only content
    whose resolved scopes include `active_scope` (or `*`).

    Frontmatter is omitted (it's metadata for the daemon, not for the
    LLM). Sections with no surviving bullets are dropped entirely.
    `active_scope=None` returns everything (including unscoped content)
    and is what the `notes` principal verb uses for full audit dumps."""
    out: list[str] = []
    for section in parsed.sections:
        # Section default: if heading has scopes, those are the floor;
        # otherwise the section is unscoped (== `*` for visibility).
        section_scopes = section.scopes if section.scopes else (_WILDCARD_SCOPE,)

        kept_bullets: list[str] = []
        for bullet in section.bullets:
            effective = bullet.scopes if bullet.scopes else section_scopes
            if _scope_matches(effective, active_scope):
                # Render the bullet without the scope tag, since the
                # LLM doesn't care about visibility metadata.
                kept_bullets.append(f"- {bullet.text}")

        if not kept_bullets:
            continue

        out.append(f"## {section.heading}")
        out.append("")
        out.extend(kept_bullets)
        out.append("")

    # Trim trailing blank line.
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def safe_text(parsed: ParsedNotes) -> str:
    """Render only the truly-safe subset of the notes: content visible
    to every scope. This is what the pass-1 scope classifier sees, so
    that scope-tagged content cannot leak into a classification round.

    A bullet survives iff its *effective* scope (own scopes if set,
    otherwise inherited from the section) is wildcard or empty. A
    section heading without a scope tag inherits the wildcard for
    bullet inheritance — but in practice we render section headings
    only when at least one bullet survives.

    Concretely: a bullet from a `## Personal [scopes: personal]`
    section will NEVER appear in safe_text, even if the bullet itself
    has no scope tag (it inherits `personal`). A `[scopes: *]` bullet
    appears regardless of section. An entirely-unscoped section's
    bullets all appear (they inherit the wildcard).
    """
    out: list[str] = []
    for section in parsed.sections:
        # If the section heading has a non-wildcard scope, every bullet
        # under it inherits that scope (unless the bullet overrides),
        # so a non-overriding bullet is unsafe by inheritance.
        section_is_wildcard = (
            not section.scopes
            or _WILDCARD_SCOPE in section.scopes
        )

        kept_bullets: list[str] = []
        for bullet in section.bullets:
            if bullet.scopes:
                # Bullet overrides — safe iff wildcard.
                if _WILDCARD_SCOPE in bullet.scopes:
                    kept_bullets.append(f"- {bullet.text}")
            else:
                # Bullet inherits section. Safe iff section is wildcard.
                if section_is_wildcard:
                    kept_bullets.append(f"- {bullet.text}")

        if not kept_bullets:
            continue

        out.append(f"## {section.heading}")
        out.append("")
        out.extend(kept_bullets)
        out.append("")

    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def read_safe_notes(path: Path) -> str:
    """Read and safe-filter a contact's notes file (classifier-friendly).
    Returns "" when the file doesn't exist. Raises NotesParseError on
    malformed content; callers fail closed (no notes block in the
    classifier prompt) rather than guessing."""
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    parsed = parse(text)
    return safe_text(parsed)


# ---- Public reads ---------------------------------------------------------


def read_notes(path: Path, active_scope: str | None) -> str:
    """Read and scope-filter a contact's notes file.

    Returns "" when the file doesn't exist (legitimate: a contact may
    have no notes yet). Raises NotesParseError on malformed content —
    callers should fail closed (i.e. omit the notes block from the
    triage prompt) rather than silently include unfiltered text."""
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    parsed = parse(text)
    return filtered_text(parsed, active_scope)


# ---- Append ---------------------------------------------------------------


def _serialize(parsed: ParsedNotes) -> str:
    """Round-trip a ParsedNotes back to text.

    Canonical output shape: frontmatter (when present), one blank
    line, optional preamble lines, sections separated by blank lines,
    each section formatted as `## heading\\n\\n- bullet\\n- bullet\\n`.
    The format is idempotent — re-serializing a parsed canonical file
    is a no-op.

    Bullet raw_lines are preserved verbatim so human formatting
    (including indentation quirks within a bullet) survives."""
    out: list[str] = []
    if parsed.frontmatter:
        out.append(_FRONTMATTER_DELIM)
        for key, value in parsed.frontmatter.items():
            out.append(f"{key}: {value}")
        out.append(_FRONTMATTER_DELIM)
        out.append("")  # blank line after frontmatter
    if parsed.preamble_lines:
        out.extend(parsed.preamble_lines)
        # Don't add a forced blank — the preamble may already have one.
    for section in parsed.sections:
        out.append(section.raw_heading_line)
        out.append("")  # canonical: blank line under heading
        for bullet in section.bullets:
            out.append(bullet.raw_line)
        out.append("")  # blank line between sections
    text = "\n".join(out)
    # Collapse any accidental double-blank-trailing into a single
    # newline; files end with exactly one trailing newline.
    text = text.rstrip("\n") + "\n"
    return text


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _atomic_write(path: Path, text: str) -> None:
    """Tmp + fsync + chmod 600 + rename. Matches contacts_writer /
    setup_auth / secret_box."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def append_note(
    path: Path,
    *,
    contact_id: str,
    section_heading: str,
    body: str,
    scope: str | None,
    now_iso: str | None = None,
) -> None:
    """Append a bullet to a section in a contact's notes file.

    If the file doesn't exist, it's created with frontmatter
    (`contact_id`, `created_at`, `last_updated`) and the new section.
    If the section heading exists, the bullet is appended to it; the
    section's existing scope tag is left alone.
    If the section doesn't exist, it's created with the supplied scope
    (when not None) or unscoped (visible everywhere) when scope is None.

    `body` is the bullet text *without* the leading `- `; this function
    adds the prefix and the optional `[scopes: ...]` tag.

    `scope` is a single scope name or None. Multi-scope bullets aren't
    supported here — callers wanting multi-scope can edit the file
    manually or call this multiple times under different sections.

    Atomic: tmp + fsync + chmod 600 + rename. On crash mid-write, the
    file is unchanged."""
    now = now_iso if now_iso is not None else _now_iso()

    if path.exists():
        text = path.read_text(encoding="utf-8")
        parsed = parse(text)
    else:
        parsed = ParsedNotes(
            frontmatter={
                "contact_id": contact_id,
                "created_at": now,
                "last_updated": now,
            },
            preamble_lines=(),
            sections=(),
        )

    # Bump last_updated. Leave created_at as-is if present.
    new_frontmatter = dict(parsed.frontmatter)
    new_frontmatter.setdefault("contact_id", contact_id)
    new_frontmatter.setdefault("created_at", now)
    new_frontmatter["last_updated"] = now

    bullet_scopes: tuple[str, ...] = (scope,) if scope else ()
    bullet_text = body.strip()
    bullet_raw = f"- {bullet_text}{_format_scopes_tag(bullet_scopes)}"
    new_bullet = Bullet(
        text=bullet_text, scopes=bullet_scopes, raw_line=bullet_raw,
    )

    new_sections: list[Section] = []
    appended = False
    for section in parsed.sections:
        if section.heading == section_heading and not appended:
            new_sections.append(Section(
                heading=section.heading,
                scopes=section.scopes,
                bullets=section.bullets + (new_bullet,),
                raw_heading_line=section.raw_heading_line,
            ))
            appended = True
        else:
            new_sections.append(section)

    if not appended:
        section_scopes: tuple[str, ...] = (scope,) if scope else ()
        heading_raw = (
            f"## {section_heading}{_format_scopes_tag(section_scopes)}"
        )
        new_sections.append(Section(
            heading=section_heading,
            scopes=section_scopes,
            bullets=(new_bullet,),
            raw_heading_line=heading_raw,
        ))

    new_parsed = ParsedNotes(
        frontmatter=new_frontmatter,
        preamble_lines=parsed.preamble_lines,
        sections=tuple(new_sections),
    )
    _atomic_write(path, _serialize(new_parsed))
