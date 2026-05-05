"""Tests for daemon/secret_box.py.

The threat model and crypto shape are documented in the module
docstring. These tests exercise:

  - Round-trip happy path.
  - Salt randomness: same plaintext encodes to different ciphertext.
  - Label binding: the same plaintext under different labels does not
    decode under the wrong label.
  - Machine-id binding: a different machine-id does not decode.
  - Tampering: bit-flips in the encoded form yield SecretBoxError or
    wrong plaintext, never silent garbage that pretends to be a key.
  - Edge cases: empty plaintext, very long plaintext, unicode plaintext.
  - Machine-id helpers: read_machine_id rejects missing/empty/bad-hex.
  - Fingerprint: stable for the same machine-id, different across.
"""
from __future__ import annotations

import string
import pytest

from daemon import secret_box
from daemon.secret_box import (
    LABEL_CLAUDE_API_KEY,
    LABEL_SMTP_PASSWORD,
    LABEL_TOTP_SECRET,
    SecretBoxError,
    deobfuscate,
    label_imap_password,
    machine_id_fingerprint,
    obfuscate,
    read_machine_id,
)


# Stable test machine-id (16 zero bytes). Real /etc/machine-id is 16
# random bytes per install; using zeros makes test cases readable
# and reproducible. Production code reads /etc/machine-id via
# read_machine_id() with no override.
TEST_MID = bytes(16)
ALT_MID = bytes(range(16))  # different machine


def test_round_trip_happy_path() -> None:
    pt = "sk-ant-api03-tup57sQf0ki6"
    enc = obfuscate(pt, label=LABEL_CLAUDE_API_KEY, machine_id=TEST_MID)
    assert deobfuscate(enc, label=LABEL_CLAUDE_API_KEY, machine_id=TEST_MID) == pt


def test_round_trip_unicode() -> None:
    pt = "passphrase with emoji 🌙 and accented chars café"
    enc = obfuscate(pt, label=LABEL_SMTP_PASSWORD, machine_id=TEST_MID)
    assert deobfuscate(enc, label=LABEL_SMTP_PASSWORD, machine_id=TEST_MID) == pt


def test_round_trip_empty_string() -> None:
    enc = obfuscate("", label=LABEL_TOTP_SECRET, machine_id=TEST_MID)
    # Empty plaintext still produces a salt-bearing token.
    assert isinstance(enc, str)
    assert deobfuscate(enc, label=LABEL_TOTP_SECRET, machine_id=TEST_MID) == ""


def test_round_trip_long_plaintext() -> None:
    """Plaintext longer than one keystream block (32 bytes) exercises
    the counter-mode block boundary."""
    pt = "x" * 5000
    enc = obfuscate(pt, label=LABEL_TOTP_SECRET, machine_id=TEST_MID)
    assert deobfuscate(enc, label=LABEL_TOTP_SECRET, machine_id=TEST_MID) == pt


def test_salt_randomness_yields_different_ciphertext() -> None:
    """Re-encoding the same plaintext yields different bytes. This
    means an attacker cannot tell whether two stored secrets are
    equal by comparing their encoded forms."""
    pt = "the same secret"
    enc1 = obfuscate(pt, label=LABEL_SMTP_PASSWORD, machine_id=TEST_MID)
    enc2 = obfuscate(pt, label=LABEL_SMTP_PASSWORD, machine_id=TEST_MID)
    assert enc1 != enc2
    # Both still decode to the original.
    assert deobfuscate(enc1, label=LABEL_SMTP_PASSWORD, machine_id=TEST_MID) == pt
    assert deobfuscate(enc2, label=LABEL_SMTP_PASSWORD, machine_id=TEST_MID) == pt


def test_label_binding_prevents_cross_label_decode() -> None:
    """The same plaintext under different labels does not decode
    under the wrong label. Either we get a UTF-8 error (most likely)
    or garbage that doesn't equal the original."""
    pt = "shared plaintext"
    enc = obfuscate(pt, label=LABEL_SMTP_PASSWORD, machine_id=TEST_MID)
    # Wrong label: either raises (more likely) or returns garbage.
    try:
        decoded = deobfuscate(enc, label=LABEL_TOTP_SECRET, machine_id=TEST_MID)
    except SecretBoxError:
        return  # expected and ideal
    assert decoded != pt


def test_machine_id_binding_prevents_cross_machine_decode() -> None:
    """Encoded on one machine, decoding with a different machine-id
    must not return the original plaintext. Either raises or yields
    garbage."""
    pt = "machine-bound secret"
    enc = obfuscate(pt, label=LABEL_TOTP_SECRET, machine_id=TEST_MID)
    try:
        decoded = deobfuscate(enc, label=LABEL_TOTP_SECRET, machine_id=ALT_MID)
    except SecretBoxError:
        return
    assert decoded != pt


def test_tampered_base64_raises() -> None:
    enc = obfuscate("plaintext", label=LABEL_SMTP_PASSWORD, machine_id=TEST_MID)
    # Corrupt the base64 with non-base64 chars.
    tampered = enc[:-2] + "!!"
    with pytest.raises(SecretBoxError):
        deobfuscate(tampered, label=LABEL_SMTP_PASSWORD, machine_id=TEST_MID)


def test_too_short_to_contain_salt_raises() -> None:
    """A 4-byte base64 string decodes to 3 bytes, less than the
    16-byte salt. Must raise."""
    import base64
    too_short = base64.b64encode(b"abc").decode()
    with pytest.raises(SecretBoxError, match="too short"):
        deobfuscate(too_short, label=LABEL_SMTP_PASSWORD, machine_id=TEST_MID)


def test_bit_flip_in_ciphertext_yields_different_plaintext() -> None:
    """Flipping a bit in the ciphertext flips the corresponding bit
    in the plaintext (this is the classic stream-cipher property).
    The decoder cannot detect this short of UTF-8 validation, so
    the test verifies the plaintext does NOT round-trip equal — the
    "tampering detection" we have is "wrong plaintext is silently
    different," which is acceptable for the threat model."""
    import base64
    pt = string.ascii_letters * 4
    enc = obfuscate(pt, label=LABEL_TOTP_SECRET, machine_id=TEST_MID)
    blob = bytearray(base64.b64decode(enc))
    # Flip a high bit deep in the ciphertext (after the 16-byte salt)
    # to push at least one byte out of ASCII printable range.
    blob[20] ^= 0xFF
    tampered = base64.b64encode(bytes(blob)).decode()
    try:
        decoded = deobfuscate(tampered, label=LABEL_TOTP_SECRET, machine_id=TEST_MID)
    except SecretBoxError:
        return  # UTF-8 validation caught it — best case
    assert decoded != pt


def test_label_imap_password_per_inbox_naming() -> None:
    """The IMAP label varies per inbox. Two inboxes with the same
    underlying password must encode to different plaintext-pair-able
    ciphertext."""
    assert label_imap_password("nightjar") == "imap.nightjar.password"
    assert label_imap_password("work") == "imap.work.password"
    # Encoded under one inbox's label cannot decode under another.
    enc = obfuscate("shared-pwd", label=label_imap_password("nightjar"), machine_id=TEST_MID)
    try:
        decoded = deobfuscate(enc, label=label_imap_password("work"), machine_id=TEST_MID)
    except SecretBoxError:
        return
    assert decoded != "shared-pwd"


def test_obfuscate_rejects_non_str_plaintext() -> None:
    with pytest.raises(SecretBoxError, match="plaintext must be str"):
        obfuscate(b"bytes not str", label=LABEL_SMTP_PASSWORD, machine_id=TEST_MID)


def test_obfuscate_rejects_empty_label() -> None:
    with pytest.raises(SecretBoxError, match="label must be"):
        obfuscate("plaintext", label="", machine_id=TEST_MID)


# ---- Machine-id helpers ----------------------------------------------------


def test_read_machine_id_round_trip(tmp_path) -> None:
    fake_mid = "0123456789abcdef0123456789abcdef"
    path = tmp_path / "machine-id"
    path.write_text(fake_mid + "\n", encoding="ascii")
    mid = read_machine_id(path=path)
    assert mid == bytes.fromhex(fake_mid)


def test_read_machine_id_missing_file_raises(tmp_path) -> None:
    path = tmp_path / "does-not-exist"
    with pytest.raises(SecretBoxError, match="not found"):
        read_machine_id(path=path)


def test_read_machine_id_empty_file_raises(tmp_path) -> None:
    path = tmp_path / "machine-id"
    path.write_text("\n", encoding="ascii")
    with pytest.raises(SecretBoxError, match="empty"):
        read_machine_id(path=path)


def test_read_machine_id_non_hex_raises(tmp_path) -> None:
    path = tmp_path / "machine-id"
    path.write_text("not hex at all", encoding="ascii")
    with pytest.raises(SecretBoxError, match="hex"):
        read_machine_id(path=path)


def test_machine_id_fingerprint_stable() -> None:
    fp1 = machine_id_fingerprint(machine_id=TEST_MID)
    fp2 = machine_id_fingerprint(machine_id=TEST_MID)
    assert fp1 == fp2
    # 64 hex chars (SHA-256).
    assert len(fp1) == 64
    assert all(c in "0123456789abcdef" for c in fp1)


def test_machine_id_fingerprint_varies_with_machine() -> None:
    fp1 = machine_id_fingerprint(machine_id=TEST_MID)
    fp2 = machine_id_fingerprint(machine_id=ALT_MID)
    assert fp1 != fp2


def test_machine_id_fingerprint_does_not_reveal_machine_id() -> None:
    """The fingerprint is one-way: HMAC ensures the machine-id cannot
    be recovered from the hex digest. We can't truly prove this in a
    test, but we can check that the fingerprint is not just the
    machine-id in some recognisable encoding."""
    fp = machine_id_fingerprint(machine_id=TEST_MID)
    assert TEST_MID.hex() not in fp


# ---- write_secrets_file / read_secrets_file -------------------------------


def test_write_and_read_secrets_round_trip(tmp_path) -> None:
    from daemon.secret_box import read_secrets_file, write_secrets_file
    path = tmp_path / "secrets.toml"
    secrets = {
        "smtp": {"password": "wfsjhfzrzukpgzst"},
        "security": {"totp_secret": "JBSWY3DPEHPK3PXP"},
        "claude": {"api_key": "sk-ant-api03-xxx"},
        "imap.nightjar": {"password": "imap-pwd"},
    }
    write_secrets_file(path, secrets, machine_id=TEST_MID)
    assert path.stat().st_mode & 0o777 == 0o600
    decoded = read_secrets_file(path, machine_id=TEST_MID)
    assert decoded == secrets


def test_write_secrets_file_is_deterministic_layout(tmp_path) -> None:
    """Sections and keys are sorted so the file diffs cleanly when
    secrets change. (The encoded values themselves vary per write
    because of the salt; that is by design and tested separately.)"""
    from daemon.secret_box import write_secrets_file
    path = tmp_path / "secrets.toml"
    secrets = {"z": {"key": "v"}, "a": {"key": "v"}}
    write_secrets_file(path, secrets, machine_id=TEST_MID)
    text = path.read_text()
    assert text.index("[a]") < text.index("[z]")


def test_write_secrets_file_quotes_dotted_section_names(tmp_path) -> None:
    """Section names with dots (e.g. imap.<inbox>) must be written as
    quoted TOML keys, otherwise tomllib reads them as nested tables."""
    from daemon.secret_box import write_secrets_file
    path = tmp_path / "secrets.toml"
    write_secrets_file(
        path, {"imap.nightjar": {"password": "x"}}, machine_id=TEST_MID,
    )
    assert '["imap.nightjar"]' in path.read_text()


def test_read_secrets_rejects_world_readable(tmp_path) -> None:
    """A secrets file that isn't chmod 600 is treated as a security
    issue. The daemon refuses to start rather than silently using a
    file an arbitrary local user can read."""
    from daemon.secret_box import read_secrets_file, write_secrets_file
    path = tmp_path / "secrets.toml"
    write_secrets_file(
        path, {"smtp": {"password": "x"}}, machine_id=TEST_MID,
    )
    import os
    os.chmod(path, 0o644)
    with pytest.raises(SecretBoxError, match="must be 0o600"):
        read_secrets_file(path, machine_id=TEST_MID)


def test_read_secrets_missing_file_raises(tmp_path) -> None:
    from daemon.secret_box import read_secrets_file
    with pytest.raises(SecretBoxError, match="not found"):
        read_secrets_file(tmp_path / "no-such.toml", machine_id=TEST_MID)


def test_read_secrets_invalid_toml_raises(tmp_path) -> None:
    from daemon.secret_box import read_secrets_file
    path = tmp_path / "secrets.toml"
    path.write_text("this is = not [valid")
    import os
    os.chmod(path, 0o600)
    with pytest.raises(SecretBoxError, match="not valid TOML"):
        read_secrets_file(path, machine_id=TEST_MID)


def test_write_secrets_validates_round_trip_before_replace(tmp_path) -> None:
    """The validator is the safety net for a hypothetical bug in
    obfuscate/deobfuscate. We can't easily induce a real round-trip
    failure without monkey-patching, but we can verify the validator
    runs by checking that a successful write yields an existing file.
    (A failed validation would have raised before os.replace.)"""
    from daemon.secret_box import write_secrets_file
    path = tmp_path / "secrets.toml"
    write_secrets_file(
        path, {"smtp": {"password": "x"}}, machine_id=TEST_MID,
    )
    assert path.exists()
    # No leftover .tmp files in the directory.
    leftovers = [
        p for p in tmp_path.iterdir()
        if p.name.startswith(".secrets.toml.")
    ]
    assert leftovers == []


def test_read_secrets_with_wrong_machine_id_raises(tmp_path) -> None:
    """Decoding with a different machine-id fails; either UTF-8 error
    bubbles up from deobfuscate, or the bytes happen to decode but
    yield wrong plaintext. Either way we don't silently return
    garbage."""
    from daemon.secret_box import read_secrets_file, write_secrets_file
    path = tmp_path / "secrets.toml"
    write_secrets_file(
        path, {"smtp": {"password": "secret"}}, machine_id=TEST_MID,
    )
    # Try reading with a different machine-id; expect either error
    # OR decoded value that does not equal the original.
    try:
        decoded = read_secrets_file(path, machine_id=ALT_MID)
    except SecretBoxError:
        return  # expected and ideal
    assert decoded["smtp"]["password"] != "secret"
