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
