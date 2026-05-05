"""Watcher tests that don't need a live IMAP connection."""
from __future__ import annotations

from daemon.inbox_watcher import InboxWatcher


def test_extract_literal_typical_response() -> None:
    """The shape aioimaplib actually returns for a single-UID fetch."""
    payload = b"From: a@example.com\r\nSubject: hi\r\n\r\n"
    response = [
        b"1 FETCH (UID 1 BODY[HEADER] {%d}" % len(payload),
        bytearray(payload),
        b")",
        b"Success",
    ]
    assert InboxWatcher._extract_literal(response) == payload


def test_extract_literal_falls_back_to_largest_header_block() -> None:
    """If the {N} descriptor is missing, find the largest plausible blob."""
    payload = b"From: a@example.com\r\nSubject: hi\r\n\r\n"
    response = [
        b"some preamble",
        bytearray(payload),
        b")",
    ]
    assert InboxWatcher._extract_literal(response) == payload


def test_extract_literal_returns_none_when_no_payload() -> None:
    response = [b"1 FETCH ()", b"Success"]
    assert InboxWatcher._extract_literal(response) is None


def test_extract_literal_handles_only_bytes_no_bytearray() -> None:
    """Servers that return bytes everywhere instead of bytearray for the literal."""
    payload = b"From: a@example.com\r\nDate: Mon, 1 Jan 2026\r\n\r\n"
    response = [
        b"1 FETCH (UID 1 BODY[HEADER] {%d}" % len(payload),
        payload,
        b")",
    ]
    assert InboxWatcher._extract_literal(response) == payload


def test_decode_header_handles_plain_ascii() -> None:
    assert InboxWatcher._decode_header("Hello there") == "Hello there"


def test_decode_header_handles_none() -> None:
    assert InboxWatcher._decode_header(None) is None


def test_decode_header_handles_encoded_word() -> None:
    encoded = "=?utf-8?q?Hi=20there?="
    assert InboxWatcher._decode_header(encoded) == "Hi there"


# ---- Body extraction (Step 5b) --------------------------------------------


import email
from daemon.inbox_watcher import InboxWatcher, MAX_TRIAGE_BODY_BYTES


def _make_msg(blob: bytes):
    return email.message_from_bytes(blob)


def test_extract_plain_text_simple_text_message() -> None:
    msg = _make_msg(
        b"From: a@example.com\r\nContent-Type: text/plain\r\n\r\n"
        b"Hello there.\r\nLine two.\r\n"
    )
    text, truncated = InboxWatcher._extract_plain_text(msg)
    assert text is not None
    assert "Hello there" in text
    assert "Line two" in text
    assert truncated is False


def test_extract_plain_text_picks_text_part_from_multipart() -> None:
    """text/plain part inside a multipart/alternative is preferred."""
    boundary = "B"
    blob = (
        b"From: a@example.com\r\n"
        b"Content-Type: multipart/alternative; boundary=" + boundary.encode() + b"\r\n"
        b"\r\n"
        b"--" + boundary.encode() + b"\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"PLAINTEXT_BODY\r\n"
        b"--" + boundary.encode() + b"\r\n"
        b"Content-Type: text/html\r\n\r\n"
        b"<p>HTMLBODY</p>\r\n"
        b"--" + boundary.encode() + b"--\r\n"
    )
    text, truncated = InboxWatcher._extract_plain_text(_make_msg(blob))
    assert text is not None
    assert "PLAINTEXT_BODY" in text
    assert "HTMLBODY" not in text


def test_extract_plain_text_returns_none_for_html_only() -> None:
    """Per design, HTML-only messages are rejected: triage gets either a
    real plaintext body or nothing, never garbage stripped HTML."""
    blob = (
        b"From: a@example.com\r\nContent-Type: text/html\r\n\r\n"
        b"<p>only html here</p>\r\n"
    )
    text, _ = InboxWatcher._extract_plain_text(_make_msg(blob))
    assert text is None


def test_extract_plain_text_truncates_oversized_body() -> None:
    huge = b"a" * (MAX_TRIAGE_BODY_BYTES + 5000)
    blob = b"From: a@example.com\r\nContent-Type: text/plain\r\n\r\n" + huge
    text, truncated = InboxWatcher._extract_plain_text(_make_msg(blob))
    assert text is not None
    assert len(text.encode("utf-8")) <= MAX_TRIAGE_BODY_BYTES
    assert truncated is True


def test_build_reply_subject_adds_re_prefix() -> None:
    assert InboxWatcher._build_reply_subject("Hello there") == "Re: Hello there"


def test_build_reply_subject_does_not_double_prefix() -> None:
    assert InboxWatcher._build_reply_subject("Re: existing") == "Re: existing"
    assert InboxWatcher._build_reply_subject("re: lower") == "re: lower"
    assert InboxWatcher._build_reply_subject("Fwd: forwarded") == "Fwd: forwarded"


def test_build_reply_subject_handles_none_or_empty() -> None:
    assert InboxWatcher._build_reply_subject(None) == "Re: (no subject)"
    assert InboxWatcher._build_reply_subject("") == "Re: (no subject)"
    assert InboxWatcher._build_reply_subject("   ") == "Re: (no subject)"


# ---- Original-email audit block (Step 5b hardening) -----------------------


def test_format_original_email_block_renders_full_body() -> None:
    block = InboxWatcher._format_original_email_block(
        from_header="TORCH <eagre.dev@gmail.com>",
        subject="quick check",
        date_header="Mon, 4 May 2026 22:57:34 +0100",
        body_text="Hey, just wondering...\n",
        body_truncated=False,
    )
    assert "ORIGINAL EMAIL" in block
    assert "From:    TORCH <eagre.dev@gmail.com>" in block
    assert "Subject: quick check" in block
    assert "Date:    Mon, 4 May 2026 22:57:34 +0100" in block
    assert "Hey, just wondering...\n" in block
    assert "END ORIGINAL" in block
    assert "TRUNCATED" not in block


def test_format_original_email_block_marks_truncation() -> None:
    block = InboxWatcher._format_original_email_block(
        from_header="x@example.com",
        subject="long",
        date_header="now",
        body_text="aaa\n",
        body_truncated=True,
    )
    assert "[TRUNCATED at 32 KiB" in block


def test_format_original_email_block_handles_missing_body() -> None:
    """When body_text is None (cap_blocked, body_fetch_failed), the
    block still renders so the audit log is consistent."""
    block = InboxWatcher._format_original_email_block(
        from_header="x@example.com",
        subject="s",
        date_header="now",
        body_text=None,
        body_truncated=False,
    )
    assert "(body was not available to triage)" in block
    assert "ORIGINAL EMAIL" in block


def test_format_original_email_block_handles_missing_metadata() -> None:
    block = InboxWatcher._format_original_email_block(
        from_header="",
        subject=None,
        date_header="",
        body_text="hi\n",
        body_truncated=False,
    )
    assert "From:    (missing)" in block
    assert "Subject: (no subject)" in block
    assert "Date:    (missing)" in block


def test_format_original_email_block_appends_newline_when_body_lacks_one() -> None:
    """A body that ends mid-character (no trailing newline) shouldn't
    visually run into the END marker."""
    block = InboxWatcher._format_original_email_block(
        from_header="x@example.com",
        subject="s",
        date_header="now",
        body_text="no trailing newline",
        body_truncated=False,
    )
    assert "no trailing newline\n========== END ORIGINAL" in block


# ---- Forward subject building (Step 6) ------------------------------------


def test_build_forward_subject_adds_fwd_prefix() -> None:
    assert InboxWatcher._build_forward_subject("track ready") == "Fwd: track ready"


def test_build_forward_subject_does_not_double_prefix() -> None:
    assert InboxWatcher._build_forward_subject("Fwd: x") == "Fwd: x"
    assert InboxWatcher._build_forward_subject("FW: x") == "FW: x"


def test_build_forward_subject_keeps_re_prefix() -> None:
    """An existing Re: stays in place; Fwd: stacks in front."""
    assert InboxWatcher._build_forward_subject("Re: track") == "Fwd: Re: track"


def test_build_forward_subject_handles_none_or_empty() -> None:
    assert InboxWatcher._build_forward_subject(None) == "Fwd: (no subject)"
    assert InboxWatcher._build_forward_subject("") == "Fwd: (no subject)"
    assert InboxWatcher._build_forward_subject("   ") == "Fwd: (no subject)"


# ---- Message structure extraction (Step 6) --------------------------------


def test_extract_structure_plain_text_only() -> None:
    blob = (
        b"From: x@example.com\r\n"
        b"Subject: plain\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Just text.\r\n"
    )
    msg = _make_msg(blob)
    s = InboxWatcher._extract_message_structure(
        msg, raw_size=len(blob), body_truncated=False,
    )
    assert s.has_html_alternative is False
    assert s.attachment_count == 0
    assert s.attachment_names == ()
    assert s.inline_image_count == 0
    # Plain-text body bytes (after Content-Transfer-Encoding decode).
    # The Subject and From headers do NOT contribute — that's the
    # point of the new schema.
    assert s.plain_size_bytes == len(b"Just text.\r\n")
    assert s.html_size_bytes == 0
    assert s.body_truncated_in_prompt is False


def test_extract_structure_separates_plain_and_html_sizes() -> None:
    """Bug fix: a plain-text email arriving via Gmail has 5+ KB of
    MTA-injected headers. We must not let header bytes inflate the
    "size" we report to the LLM. Only the part bodies count."""
    plain_body = b"Hi, just a quick check-in.\r\n"
    html_body = b"<p>Hi, just a quick check-in.</p>\r\n"
    blob = (
        b"From: x@example.com\r\n"
        b"Subject: alt\r\n"
        # Imagine 4 KB of ARC/DKIM/Received headers here in practice.
        b"X-Spurious-Big-Header: " + b"A" * 4000 + b"\r\n"
        b"Content-Type: multipart/alternative; boundary=BOUND\r\n"
        b"\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        + plain_body +
        b"--BOUND\r\n"
        b"Content-Type: text/html\r\n\r\n"
        + html_body +
        b"--BOUND--\r\n"
    )
    msg = _make_msg(blob)
    s = InboxWatcher._extract_message_structure(
        msg, raw_size=len(blob), body_truncated=False,
    )
    # Header bloat does NOT contribute to either size.
    assert s.plain_size_bytes < 100
    assert s.html_size_bytes < 100
    # And both got measured.
    assert s.plain_size_bytes > 0
    assert s.html_size_bytes > 0


def test_extract_structure_detects_html_alternative() -> None:
    blob = (
        b"From: x@example.com\r\n"
        b"Subject: alt\r\n"
        b"Content-Type: multipart/alternative; boundary=BOUND\r\n"
        b"\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"plain version\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/html\r\n\r\n"
        b"<b>html version</b>\r\n"
        b"--BOUND--\r\n"
    )
    msg = _make_msg(blob)
    s = InboxWatcher._extract_message_structure(
        msg, raw_size=len(blob), body_truncated=False,
    )
    assert s.has_html_alternative is True
    assert s.attachment_count == 0


def test_extract_structure_counts_attachments() -> None:
    blob = (
        b"From: x@example.com\r\n"
        b"Subject: with attachments\r\n"
        b"Content-Type: multipart/mixed; boundary=BOUND\r\n"
        b"\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"see attached\r\n"
        b"--BOUND\r\n"
        b"Content-Type: application/pdf; name=\"contract.pdf\"\r\n"
        b"Content-Disposition: attachment; filename=\"contract.pdf\"\r\n\r\n"
        b"(pdf bytes)\r\n"
        b"--BOUND\r\n"
        b"Content-Type: image/jpeg; name=\"scan.jpg\"\r\n"
        b"Content-Disposition: attachment; filename=\"scan.jpg\"\r\n\r\n"
        b"(jpg bytes)\r\n"
        b"--BOUND--\r\n"
    )
    msg = _make_msg(blob)
    s = InboxWatcher._extract_message_structure(
        msg, raw_size=len(blob), body_truncated=False,
    )
    assert s.attachment_count == 2
    assert "contract.pdf" in s.attachment_names
    assert "scan.jpg" in s.attachment_names


def test_extract_structure_distinguishes_inline_image_from_attachment() -> None:
    """An image with disposition=inline (or no disposition inside a
    related multipart) is an inline image, not an attachment."""
    blob = (
        b"From: x@example.com\r\n"
        b"Subject: inline pic\r\n"
        b"Content-Type: multipart/related; boundary=BOUND\r\n"
        b"\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/html\r\n\r\n"
        b"<img src=\"cid:logo\">\r\n"
        b"--BOUND\r\n"
        b"Content-Type: image/png\r\n"
        b"Content-Disposition: inline; filename=\"logo.png\"\r\n"
        b"Content-ID: <logo>\r\n\r\n"
        b"(png bytes)\r\n"
        b"--BOUND--\r\n"
    )
    msg = _make_msg(blob)
    s = InboxWatcher._extract_message_structure(
        msg, raw_size=len(blob), body_truncated=False,
    )
    assert s.inline_image_count == 1
    assert s.attachment_count == 0


def test_extract_structure_propagates_body_truncation() -> None:
    blob = b"From: x@example.com\r\nSubject: s\r\n\r\nbody\r\n"
    msg = _make_msg(blob)
    s = InboxWatcher._extract_message_structure(
        msg, raw_size=len(blob), body_truncated=True,
    )
    assert s.body_truncated_in_prompt is True


def test_extract_structure_handles_unnamed_attachment() -> None:
    """Some senders emit attachments with no filename; we still count
    them but use a placeholder so the LLM sees the count is non-zero."""
    blob = (
        b"From: x@example.com\r\n"
        b"Subject: nameless\r\n"
        b"Content-Type: multipart/mixed; boundary=BOUND\r\n"
        b"\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"hi\r\n"
        b"--BOUND\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment\r\n\r\n"
        b"(bytes)\r\n"
        b"--BOUND--\r\n"
    )
    msg = _make_msg(blob)
    s = InboxWatcher._extract_message_structure(
        msg, raw_size=len(blob), body_truncated=False,
    )
    assert s.attachment_count == 1
    assert "(unnamed application/octet-stream)" in s.attachment_names


# ---- Step 7d (per Step 8): _handle_note_proposals -------------------------


def _make_watcher_for_notes(tmp_path):
    """Construct a minimal InboxWatcher just for testing
    _handle_note_proposals. No IMAP, no Claude, no real config —
    just enough state for the helper to write to the right paths
    and emit log events."""
    from daemon.config import (
        Config, Contact, DaemonConfig, InboxConfig,
    )
    from daemon.log import JSONLLogger
    from daemon.state import State

    daemon_cfg = DaemonConfig(
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        notes_dir=tmp_path / "notes",
    )
    daemon_cfg.state_dir.mkdir(parents=True)
    daemon_cfg.log_dir.mkdir(parents=True)
    daemon_cfg.notes_dir.mkdir(parents=True)
    contacts = {
        "fraser": Contact(
            contact_id="fraser",
            addresses=("fraser@example.com",),
            display_name="Fraser",
            relationship="collaborator",
            daily_limit=3,
            is_principal=False,
            inboxes=("nightjar",),
        ),
    }
    inbox = InboxConfig(
        name="nightjar",
        enabled=True,
        imap_host="imap.example.com",
        imap_port=993,
        imap_user="bot@example.com",
        imap_password="x",
        allowed_contacts=("fraser",),
        trusted_authserv="mx.google.com",
    )
    config = Config(
        daemon=daemon_cfg,
        contacts=contacts,
        inboxes={"nightjar": inbox},
    )
    state = State(db_path=daemon_cfg.state_dir / "state.db")
    logger = JSONLLogger(daemon_cfg.log_dir)
    return InboxWatcher(
        inbox=inbox, config=config, state=state, logger=logger,
    )


def _make_plan(*proposals):
    """Construct a TriagePlan stub carrying the given note_proposals."""
    from daemon.triage import NoteProposal, TriagePlan
    return TriagePlan(
        verb="reply", tier=3,
        args={"body": "ok"},
        summary="s", reasoning="r",
        risk_flags=(),
        notes="",
        raw_input_tokens=100, raw_output_tokens=10,
        note_proposals=tuple(
            NoteProposal(scope=p.get("scope"),
                         section_heading=p["section_heading"],
                         body=p["body"],
                         is_universal=p.get("is_universal", False),
                         attribution=p.get("attribution", "observed"))
            for p in proposals
        ),
    )


def test_handle_note_proposals_writes_directly_no_queue(tmp_path):
    """7d (per Step 8): proposals are applied autonomously to the
    contact's .md file. No state-db queue, no approval flow."""
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": None,
         "section_heading": "General",
         "body": "Replies fastest in evenings."},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    assert notes_path.exists()
    text = notes_path.read_text(encoding="utf-8")
    assert "## General" in text
    assert "Replies fastest in evenings" in text
    assert "contact_id: fraser" in text


def test_handle_note_proposals_no_proposals_is_noop(tmp_path):
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan()  # empty proposals
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    # No file created when no proposals.
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    assert not notes_path.exists()


def test_handle_note_proposals_appends_to_existing_file(tmp_path):
    watcher = _make_watcher_for_notes(tmp_path)
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    notes_path.write_text(
        "---\ncontact_id: fraser\n---\n\n"
        "## Existing\n\n- prior bullet\n",
        encoding="utf-8",
    )
    plan = _make_plan(
        {"scope": None, "section_heading": "Existing", "body": "new bullet"},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    text = notes_path.read_text(encoding="utf-8")
    assert "prior bullet" in text
    assert "new bullet" in text


def test_handle_note_proposals_writes_multiple_to_same_file(tmp_path):
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": None, "section_heading": "A", "body": "a-bullet"},
        {"scope": None, "section_heading": "B", "body": "b-bullet"},
        {"scope": None, "section_heading": "A", "body": "a-bullet-2"},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    text = notes_path.read_text(encoding="utf-8")
    assert "## A" in text
    assert "## B" in text
    assert "a-bullet" in text
    assert "a-bullet-2" in text
    assert "b-bullet" in text


def test_handle_note_proposals_logs_each_write(tmp_path):
    """JSONL log carries note_written events with full provenance —
    the audit trail since there's no state-db queue."""
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": None, "section_heading": "X", "body": "first"},
        {"scope": None, "section_heading": "Y", "body": "second"},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    log_files = list(watcher.config.daemon.log_dir.iterdir())
    assert log_files, "expected at least one JSONL log file"
    log_text = "\n".join(p.read_text() for p in log_files)
    assert log_text.count("note_written") == 2
    assert "fraser" in log_text


def test_handle_note_proposals_continues_on_partial_failure(tmp_path):
    """If one proposal write fails, others still apply. Error logged
    but does not propagate."""
    import os
    watcher = _make_watcher_for_notes(tmp_path)
    # Pre-create the .md file as something the parser can't read,
    # forcing the first append's parse to fail.
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    notes_path.write_text("---\nthis is malformed frontmatter\n",
                          encoding="utf-8")
    plan = _make_plan(
        {"scope": None, "section_heading": "X", "body": "first"},
    )
    # Should not raise.
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    # And the JSONL log records the failure.
    log_files = list(watcher.config.daemon.log_dir.iterdir())
    log_text = "\n".join(p.read_text() for p in log_files)
    assert "note_write_failed" in log_text


def test_handle_note_proposals_scoped_writes_with_scope_tag(tmp_path):
    """A scoped proposal (is_universal=False) writes the scope tag to
    disk, so the parser will scope-filter it correctly later."""
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": "nightjar-dev", "is_universal": False,
         "section_heading": "Project timeline",
         "body": "v1.0 target is September."},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    text = notes_path.read_text(encoding="utf-8")
    assert "[scopes: nightjar-dev]" in text, (
        f"expected scope tag in file, got:\n{text}"
    )
    assert "v1.0 target is September." in text


def test_handle_note_proposals_universal_writes_wildcard_tag(tmp_path):
    """A universal proposal (is_universal=True) writes a literal '*'
    tag so the bullet survives the scope filter regardless of which
    scope the future conversation runs under."""
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": "nightjar-dev", "is_universal": True,
         "section_heading": "Communication style",
         "body": "Prefers terse, direct replies."},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    text = notes_path.read_text(encoding="utf-8")
    assert "[scopes: *]" in text, (
        f"expected wildcard scope tag in file, got:\n{text}"
    )
    assert "Prefers terse, direct replies." in text


def test_handle_note_proposals_unscoped_contact_writes_no_tag(tmp_path):
    """For unscoped contacts, scope=None still writes with no tag —
    matching the pre-Step-7 behaviour for contacts whose .toml has no
    scopes field."""
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": None, "is_universal": False,
         "section_heading": "General",
         "body": "Replies fastest in evenings."},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    text = notes_path.read_text(encoding="utf-8")
    # No scope tag of any kind on the section heading or bullet.
    assert "[scopes:" not in text, (
        f"unscoped contact write should produce no scope tag, got:\n{text}"
    )


def test_handle_note_proposals_scoped_filtered_view_excludes_other_scope(tmp_path):
    """Belt-and-braces end-to-end: writing a scoped note with the
    correct on-disk tag means a future scope-filtered read for a
    DIFFERENT scope will not see it. This is the security invariant
    the Step 7 wave is supposed to provide; verify it rather than
    assuming the parser does the right thing."""
    from daemon import notes_store
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": "nightjar-dev", "is_universal": False,
         "section_heading": "Project timeline",
         "body": "v1.0 target is September."},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    # Reading under a different scope should yield empty.
    out = notes_store.read_notes(notes_path, active_scope="personal")
    assert "v1.0 target" not in out
    # Reading under the right scope should yield the bullet.
    out = notes_store.read_notes(notes_path, active_scope="nightjar-dev")
    assert "v1.0 target is September." in out
    # Reading the safe-only view (classifier path) should not see it.
    safe = notes_store.read_safe_notes(notes_path)
    assert "v1.0 target" not in safe


def test_handle_note_proposals_universal_filtered_view_includes_everywhere(tmp_path):
    """Mirror of the previous test for the is_universal case: a
    universal note must be visible under EVERY scope (including
    scopes the contact isn't even opted into) and in the safe-only
    classifier view."""
    from daemon import notes_store
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": "nightjar-dev", "is_universal": True,
         "section_heading": "Communication style",
         "body": "Prefers terse, direct replies."},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    out_dev = notes_store.read_notes(notes_path, active_scope="nightjar-dev")
    out_personal = notes_store.read_notes(notes_path, active_scope="personal")
    out_unrelated = notes_store.read_notes(notes_path, active_scope="random-other")
    safe = notes_store.read_safe_notes(notes_path)
    for view in (out_dev, out_personal, out_unrelated, safe):
        assert "Prefers terse, direct replies." in view, (
            f"universal note missing from a scope view:\n{view}"
        )


def test_handle_note_proposals_logs_is_universal_in_event(tmp_path):
    """The note_written JSONL event should carry is_universal so an
    operator grepping the audit log can distinguish scoped writes
    from cross-cutting writes without reading the .md file."""
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": "nightjar-dev", "is_universal": True,
         "section_heading": "Style", "body": "Terse."},
        {"scope": "nightjar-dev", "is_universal": False,
         "section_heading": "Project", "body": "v1.0 in September."},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<test-msg-id@example>",
    )
    log_files = list(watcher.config.daemon.log_dir.iterdir())
    log_text = "\n".join(p.read_text() for p in log_files)
    assert '"is_universal": true' in log_text
    assert '"is_universal": false' in log_text


# ---- Provenance: source_message_id + attribution threading ----------------


def test_handle_note_proposals_threads_source_message_id_to_disk(tmp_path):
    """The source_message_id arg lands in the on-disk meta tag so
    `show notes` can show which inbound mail produced each note."""
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": None, "section_heading": "G",
         "body": "An observation.",
         "attribution": "observed"},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<unique-msgid-123@example>",
    )
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    text = notes_path.read_text(encoding="utf-8")
    assert "<unique-msgid-123@example>" in text
    assert "attr=observed" in text


def test_handle_note_proposals_threads_attribution_through(tmp_path):
    """asserted attribution is preserved end-to-end (from validator
    output → watcher → on-disk meta tag)."""
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": None, "section_heading": "Project",
         "body": "Sender claimed Dylan approved X.",
         "attribution": "asserted"},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<m@x>",
    )
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    text = notes_path.read_text(encoding="utf-8")
    assert "attr=asserted" in text


def test_handle_note_proposals_logs_attribution_and_src_in_event(tmp_path):
    """Operators grepping JSONL must be able to filter by attribution
    type — e.g. find all 'asserted' note writes for audit."""
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": None, "section_heading": "X", "body": "Y.",
         "attribution": "asserted"},
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<grepme@x>",
    )
    log_files = list(watcher.config.daemon.log_dir.iterdir())
    log_text = "\n".join(p.read_text() for p in log_files)
    assert '"attribution": "asserted"' in log_text
    assert '"source_message_id": "<grepme@x>"' in log_text


def test_handle_note_proposals_default_observed_attribution(tmp_path):
    """If a NoteProposal is constructed without attribution (legacy
    test paths), it defaults to 'observed' and that lands on disk."""
    watcher = _make_watcher_for_notes(tmp_path)
    plan = _make_plan(
        {"scope": None, "section_heading": "X", "body": "Y."},  # no attribution
    )
    watcher._handle_note_proposals(
        contact_id="fraser", plan=plan, now=1000,
        source_message_id="<m@x>",
    )
    notes_path = watcher.config.daemon.notes_dir / "fraser.md"
    text = notes_path.read_text(encoding="utf-8")
    assert "attr=observed" in text
