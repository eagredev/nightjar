"""Machine-id-bound secret obfuscation for Nightjar's secrets.toml.

This is OBFUSCATION, NOT ENCRYPTION. It defeats:

  - Casual disk inspection (the file looks like base64 noise, not a key).
  - Backup / sync leakage: a copy of the file alone is useless on
    another machine because the keystream is derived from this
    machine's `/etc/machine-id`.
  - Accidental exposure (screenshots, paste-into-issues, dotfile-sync
    of `~/.config/`): the bytes carry no recognisable shape.

It DOES NOT defeat:

  - An attacker who has root on this machine. They can read both
    `/etc/machine-id` and the obfuscated bytes; this code is
    open-source, so they can decode trivially.
  - An attacker who has snooped the live process (memory dumps, ptrace).
    Plaintext is briefly resident after `deobfuscate`.

The threat model is "stop secrets from leaking via accidental
exposure," not "stop a sophisticated local attacker." The README and
DESIGN.md spell this out for the operator.

Cryptographic shape: HMAC-SHA256 stream cipher.

  1. `key  = HMAC-SHA256(machine_id, label)`              (32 bytes)
  2. `salt = secrets.token_bytes(16)`                     (16 bytes, fresh per write)
  3. `keystream = HKDF-Expand(key, salt, len(plaintext))` (counter-mode SHA-256)
  4. `ciphertext = plaintext XOR keystream`
  5. `encoded = base64(salt || ciphertext)`

The `label` is a stable structural name like "smtp.password" or
"claude.api_key". It functions as domain separation: the same
plaintext under two different labels yields unrelated keystreams,
so an attacker can't compare ciphertext-equality across fields.

Labels are FROZEN. If we ever rename one, every operator's existing
secrets become un-decodable. Treat them as load-bearing strings.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from pathlib import Path

# ---- Frozen labels (load-bearing) -----------------------------------------
#
# These strings appear in derived keys. Renaming any of them silently
# breaks every operator's existing secrets.toml. If you genuinely need
# to add a new secret type, add a new constant; do NOT mutate an
# existing one.

LABEL_SMTP_PASSWORD = "smtp.password"
LABEL_TOTP_SECRET = "security.totp_secret"
LABEL_CLAUDE_API_KEY = "claude.api_key"

MACHINE_ID_PATH = Path("/etc/machine-id")
SALT_LEN = 16
KEY_LEN = 32  # HMAC-SHA256 output


def label_imap_password(inbox_name: str) -> str:
    """Per-inbox IMAP password label. Stable for a given inbox name.

    Renaming an inbox in nightjar.conf will require a re-migration of
    its IMAP password (the label changes, so the obfuscated bytes are
    no longer decodable). The migrator handles the first-time case;
    subsequent renames are an operator concern (out of scope for v1).
    """
    return f"imap.{inbox_name}.password"


# ---- Errors ---------------------------------------------------------------


class SecretBoxError(Exception):
    """Raised when obfuscation/deobfuscation cannot proceed.

    The exception message is safe to log; it never contains plaintext
    or the machine-id (which would compromise the binding). Callers
    should not include the original encoded bytes in any user-facing
    error either, since a tampered file shouldn't reflect its own
    bytes back into a log line.
    """


# ---- Machine-id --------------------------------------------------------


def read_machine_id(*, path: Path = MACHINE_ID_PATH) -> bytes:
    """Read /etc/machine-id and return it as raw bytes.

    `/etc/machine-id` is a 32-character lowercase hex string set once
    by systemd at first boot (see `man machine-id`). It is stable for
    the lifetime of the install and unique to this OS install (not to
    the hardware). The daemon refuses to start if it is missing or
    empty; that means we are either on a non-systemd system (out of
    scope for v1) or someone has tampered with it.

    Returns the raw 16 bytes (decoded from the hex), not the hex
    string. This means an attacker who has only the obfuscated
    file but knows it is a Nightjar config still has to know the
    OS install's machine-id to make any progress.
    """
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError as e:
        raise SecretBoxError(
            f"machine-id file not found at {path}; secrets cannot be "
            "decoded. This typically means a non-systemd OS or a "
            "tampered system."
        ) from e
    except OSError as e:
        raise SecretBoxError(f"cannot read {path}: {e}") from e
    if not text:
        raise SecretBoxError(
            f"{path} is empty; secrets cannot be decoded. Re-run "
            "`systemd-machine-id-setup` and re-migrate."
        )
    try:
        return bytes.fromhex(text)
    except ValueError as e:
        raise SecretBoxError(
            f"{path} contents do not look like a machine-id "
            "(expected lowercase hex). Refusing to derive keys."
        ) from e


def machine_id_fingerprint(*, machine_id: bytes | None = None) -> str:
    """A non-invertible witness of the current machine-id.

    Stored on first daemon start after migration; checked on every
    subsequent start. If it changes, the secrets.toml file is no
    longer decodable on this machine and the daemon must refuse to
    run rather than emit garbage plaintext into SMTP / IMAP / API
    calls.

    The fingerprint is HMAC-SHA256(machine_id, "fingerprint-v1") in
    hex. It does not reveal the machine-id (HMAC is one-way) and is
    safe to write into state.db.
    """
    mid = machine_id if machine_id is not None else read_machine_id()
    return hmac.new(mid, b"nightjar-machine-fingerprint-v1", hashlib.sha256).hexdigest()


# ---- HKDF-style keystream ---------------------------------------------


def _derive_key(machine_id: bytes, label: str) -> bytes:
    """Derive a per-label 32-byte key from the machine-id.

    HMAC-SHA256(machine_id, label) gives us domain-separated keys
    without needing a separate salt-per-label table. The label is
    public; the machine-id is the secret-binder.
    """
    return hmac.new(machine_id, label.encode("utf-8"), hashlib.sha256).digest()


def _keystream(key: bytes, salt: bytes, length: int) -> bytes:
    """Generate `length` keystream bytes using SHA-256 counter mode.

    This is HKDF-Expand without the truncation logic: each block is
    HMAC-SHA256(key, salt || counter), concatenated until we have
    enough bytes. SHA-256 yields 32 bytes per block. Each block
    incorporates a 4-byte big-endian counter to avoid keystream
    repetition for plaintexts longer than one block.

    The salt makes each `obfuscate()` call yield different
    ciphertext for the same plaintext, so an attacker cannot tell
    whether two stored secrets are equal by comparing their
    encoded forms.
    """
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            key,
            salt + counter.to_bytes(4, "big"),
            hashlib.sha256,
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


# ---- Public API -------------------------------------------------------


def obfuscate(plaintext: str, *, label: str, machine_id: bytes | None = None) -> str:
    """Obfuscate `plaintext` for the given `label`, return base64 string.

    The encoded form is `base64(salt || ciphertext)` and is what gets
    written into secrets.toml. A fresh 16-byte random salt is
    generated per call; re-obfuscating the same plaintext will
    yield a different encoded form.

    Args:
        plaintext: the secret in plain text.
        label: a stable structural name (see module-level constants).
        machine_id: override for tests. Production callers leave this None.

    Raises:
        SecretBoxError: if /etc/machine-id is unreadable.
    """
    if not isinstance(plaintext, str):
        raise SecretBoxError("plaintext must be str")
    if not isinstance(label, str) or not label:
        raise SecretBoxError("label must be a non-empty str")

    mid = machine_id if machine_id is not None else read_machine_id()
    key = _derive_key(mid, label)
    salt = secrets.token_bytes(SALT_LEN)
    pt_bytes = plaintext.encode("utf-8")
    ks = _keystream(key, salt, len(pt_bytes))
    ct = bytes(p ^ k for p, k in zip(pt_bytes, ks, strict=True))
    return base64.b64encode(salt + ct).decode("ascii")


def write_secrets_file(
    path: Path,
    secrets_map: dict[str, dict[str, str]],
    *,
    machine_id: bytes | None = None,
) -> None:
    """Atomically write a secrets.toml file.

    `secrets_map` is `{section_name: {key: plaintext}}`. The label
    used for obfuscation is `f"{section_name}.{key}"`. Inbox sections
    are conventionally named `imap.<inbox_name>` (the section name);
    the key is `password`. So an IMAP password ends up labelled
    `imap.<inbox_name>.password`, matching `label_imap_password()`.

    The output file:
      - Has chmod 600.
      - Starts with a banner comment explaining what the file is and
        why it is bound to this machine.
      - Has each section header on its own line, key = "<base64>"
        underneath. tomllib reads this back without trouble.
    """
    mid = machine_id if machine_id is not None else read_machine_id()
    lines: list[str] = []
    lines.append("# Nightjar secrets file.")
    lines.append("# OBFUSCATED, NOT ENCRYPTED. Do not back this file up; do not")
    lines.append("# paste it anywhere. A copy of this file is useless on another")
    lines.append("# machine because the keystream is bound to /etc/machine-id.")
    lines.append("# See README and DESIGN.md for the threat model.")
    lines.append("")

    # Sort sections and keys so the file is deterministic across writes.
    # Section names containing dots (e.g. "imap.nightjar") must be
    # quoted in the header so TOML does not treat them as nested
    # tables — we want flat sections keyed exactly by the section
    # string as given.
    for section in sorted(secrets_map):
        if "." in section or " " in section:
            lines.append(f'["{section}"]')
        else:
            lines.append(f"[{section}]")
        for key in sorted(secrets_map[section]):
            label = f"{section}.{key}"
            encoded = obfuscate(
                secrets_map[section][key], label=label, machine_id=mid
            )
            # TOML strings: double-quoted, with backslash escapes. Our
            # base64 alphabet is ASCII without backslashes or quotes,
            # so no escaping is needed.
            lines.append(f'{key} = "{encoded}"')
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"

    # Atomic write with chmod 600 and post-write validation: if the
    # newly-written file does not parse OR any obfuscated value does
    # not round-trip, we discard it and leave any existing file
    # untouched.
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    import tempfile, os
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
        # Validate by re-reading and round-tripping.
        _validate_secrets_round_trip(tmp_path, secrets_map, mid)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _validate_secrets_round_trip(
    path: Path, expected: dict[str, dict[str, str]], machine_id: bytes
) -> None:
    """Re-read a freshly-written secrets file and verify every value
    deobfuscates to the original plaintext. Any drift = SecretBoxError.

    This is the safety net that catches a write bug: if the file we
    just wrote does not decode back to what we put in, we want to
    know NOW (before atomic-replace), not three days later when
    the daemon comes up and starts emailing garbage to SMTP.
    """
    import tomllib
    with path.open("rb") as f:
        data = tomllib.load(f)
    for section, kvs in expected.items():
        if section not in data:
            raise SecretBoxError(
                f"validation: section [{section}] missing in tmp file"
            )
        for key, plaintext in kvs.items():
            encoded = data[section].get(key)
            if not isinstance(encoded, str):
                raise SecretBoxError(
                    f"validation: [{section}].{key} is not a string in tmp file"
                )
            label = f"{section}.{key}"
            try:
                decoded = deobfuscate(encoded, label=label, machine_id=machine_id)
            except SecretBoxError as e:
                raise SecretBoxError(
                    f"validation: [{section}].{key} did not round-trip: {e}"
                ) from e
            if decoded != plaintext:
                raise SecretBoxError(
                    f"validation: [{section}].{key} round-trip mismatch"
                )


def read_secrets_file(
    path: Path,
    *,
    machine_id: bytes | None = None,
) -> dict[str, dict[str, str]]:
    """Read and deobfuscate every secret in a secrets.toml file.

    Returns the same shape as `write_secrets_file` consumes: a nested
    `{section: {key: plaintext}}` dict. Raises SecretBoxError on
    chmod-not-600, malformed TOML, or any individual value failing to
    deobfuscate.

    Callers (config.load) splice the returned plaintexts into the
    appropriate Config dataclasses and let the dict fall out of scope.
    """
    import os
    import tomllib

    if not path.exists():
        raise SecretBoxError(f"secrets file not found at {path}")
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise SecretBoxError(
            f"secrets file {path} has mode {oct(mode)}; must be 0o600. "
            "Run: chmod 600 " + str(path)
        )

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SecretBoxError(f"secrets file {path}: not valid TOML: {e}") from e
    except OSError as e:
        raise SecretBoxError(f"cannot read {path}: {e}") from e

    mid = machine_id if machine_id is not None else read_machine_id()
    out: dict[str, dict[str, str]] = {}
    for section, kvs in data.items():
        if not isinstance(kvs, dict):
            raise SecretBoxError(
                f"{path}: [{section}] is not a section (got {type(kvs).__name__})"
            )
        decoded_section: dict[str, str] = {}
        for key, encoded in kvs.items():
            if not isinstance(encoded, str):
                raise SecretBoxError(
                    f"{path}: [{section}].{key} must be a string, "
                    f"got {type(encoded).__name__}"
                )
            label = f"{section}.{key}"
            decoded_section[key] = deobfuscate(
                encoded, label=label, machine_id=mid
            )
        out[section] = decoded_section
    return out


def deobfuscate(encoded: str, *, label: str, machine_id: bytes | None = None) -> str:
    """Reverse `obfuscate`, returning the original plaintext.

    Raises SecretBoxError on malformed input (bad base64, too short
    to contain a salt, label/machine-id mismatch yielding non-UTF-8
    plaintext). The non-UTF-8 case is the closest we have to a
    "tampering detected" signal: a correctly-bound deobfuscation of
    a UTF-8 secret always yields valid UTF-8, so a UTF-8 error
    almost certainly means either tampering or a wrong machine-id.
    """
    if not isinstance(encoded, str):
        raise SecretBoxError("encoded must be str")
    if not isinstance(label, str) or not label:
        raise SecretBoxError("label must be a non-empty str")

    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        raise SecretBoxError(f"encoded value is not valid base64") from e
    if len(blob) < SALT_LEN:
        raise SecretBoxError(
            f"encoded value is too short to contain a {SALT_LEN}-byte salt"
        )
    salt, ct = blob[:SALT_LEN], blob[SALT_LEN:]

    mid = machine_id if machine_id is not None else read_machine_id()
    key = _derive_key(mid, label)
    ks = _keystream(key, salt, len(ct))
    pt_bytes = bytes(c ^ k for c, k in zip(ct, ks, strict=True))
    try:
        return pt_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        # Almost certainly: wrong machine-id, wrong label, or bytes
        # were modified between write and read. Don't echo the
        # plaintext bytes; they could be partial-correct from a
        # related label and contain real secret material.
        raise SecretBoxError(
            "deobfuscated bytes are not valid UTF-8; the secrets file "
            "is corrupted, was written under a different machine-id, "
            "or the label is mismatched"
        ) from e
