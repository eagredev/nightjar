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
