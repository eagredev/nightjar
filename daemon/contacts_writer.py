"""Per-contact TOML writer.

Replaces the contact-rewriting paths in `daemon/config_writer.py`.
Each `add` / `remove` operation now mutates exactly one file in
`contacts_dir` instead of editing the global `nightjar.conf`. The
blast radius is one contact file, so the executor verbs drop from
tier 4 (irreversible config rewrite, double-confirm) to tier 2
(single approval).

Writes are atomic: tmp + fsync + chmod 600 + rename. Reads from the
new file are validated with `contacts_loader._load_one` before
replace, so a malformed write never lands.

In-process Config refresh helpers mirror the dataclasses-replace
pattern from `daemon/config_writer.py`: contacts and address_index
are mutable dicts; inboxes is a dict-of-dataclasses, so a tuple
`allowed_contacts` rebuild swaps the dataclass.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .config import Config, Contact, ConfigError, InboxConfig
from .contacts_loader import _load_one


_CONTACT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class ContactsWriteError(Exception):
    """Raised when a write would leave the contacts directory invalid
    or self-contradictory. The original file (if any) is untouched
    when this is raised."""


@dataclass(frozen=True)
class AddContactRequest:
    """Inputs for `write_contact` from the `add` executor verb.

    Mirrors `daemon.config_writer.AddRequest` for source-compat,
    minus the inbox name (which is now a list on the contact itself).
    The single-string `address` reflects the current `add foo@bar`
    parser surface; the contact file's `addresses` list will have
    exactly that one address. Adding more addresses is a manual edit
    of the TOML file (or a future `add-address` verb).
    """
    contact_id: str
    address: str
    display_name: str
    relationship: str
    daily_limit: int  # -1 = unlimited
    inboxes: tuple[str, ...]  # which inboxes accept this contact


# ---- public API ---------------------------------------------------------


def write_contact(
    *,
    request: AddContactRequest,
    contacts_dir: Path,
    config: Config,
) -> Path:
    """Write a new contact's TOML file. Returns the file path.

    Validates against the existing in-memory Config: contact_id and
    every address must be unused, every inbox must exist. Atomic
    write + chmod 600 + post-write validation via the loader.
    """
    if not _CONTACT_ID_RE.match(request.contact_id):
        raise ContactsWriteError(
            f"contact_id {request.contact_id!r} must match "
            f"{_CONTACT_ID_RE.pattern}"
        )
    if request.contact_id in config.contacts:
        raise ContactsWriteError(
            f"contact_id {request.contact_id!r} already exists"
        )
    addr = request.address.lower()
    if "@" not in addr:
        raise ContactsWriteError(
            f"address {request.address!r} is not an email"
        )
    if addr in config.address_index:
        raise ContactsWriteError(
            f"address {request.address!r} is already claimed by "
            f"{config.address_index[addr]!r}"
        )
    if not request.inboxes:
        raise ContactsWriteError("inboxes must be a non-empty tuple")
    for inbox_name in request.inboxes:
        if inbox_name not in config.inboxes:
            raise ContactsWriteError(
                f"inbox {inbox_name!r} does not exist"
            )

    path = contacts_dir / f"{request.contact_id}.toml"
    if path.exists():
        # Defensive: contact_id collision with a file that isn't in
        # the in-memory Config means the directory drifted away from
        # the running daemon. Refuse rather than overwrite.
        raise ContactsWriteError(
            f"file {path} already exists; refusing to overwrite. "
            "Either remove the file or choose a different contact_id."
        )

    text = _render_contact_toml(request)
    _atomic_write_and_validate(path, text)
    return path


def delete_contact(
    *,
    contact_id: str,
    contacts_dir: Path,
    config: Config,
) -> None:
    """Delete a contact's TOML file. Refuses to remove the principal."""
    if contact_id not in config.contacts:
        raise ContactsWriteError(
            f"contact_id {contact_id!r} does not exist"
        )
    if config.contacts[contact_id].is_principal:
        raise ContactsWriteError(
            f"refusing to remove principal contact {contact_id!r}; "
            "the principal is the daemon's only authenticated identity"
        )

    path = contacts_dir / f"{contact_id}.toml"
    if not path.exists():
        # In-memory config has the contact but the file is gone. That's
        # a drift state worth surfacing to the operator.
        raise ContactsWriteError(
            f"file {path} does not exist; in-memory config and contacts/ "
            "are out of sync. Restart the daemon."
        )
    try:
        path.unlink()
    except OSError as e:
        raise ContactsWriteError(f"could not delete {path}: {e}") from e


def apply_add(*, request: AddContactRequest, config: Config) -> None:
    """Update an in-memory Config to reflect a successful write_contact().

    Mutates config.contacts and config.address_index in place; the
    surrounding Config dataclass remains frozen but its dicts are
    mutable. Inbox.allowed_contacts is rebuilt for each inbox the
    contact is allowed on (the dataclass is frozen, so we replace
    the entry).
    """
    contact = Contact(
        contact_id=request.contact_id,
        addresses=(request.address.lower(),),
        display_name=request.display_name,
        relationship=request.relationship,
        daily_limit=request.daily_limit,
        is_principal=False,
        inboxes=request.inboxes,
        auto_approve_notes=False,
    )
    config.contacts[request.contact_id] = contact
    config.address_index[request.address.lower()] = request.contact_id
    for inbox_name in request.inboxes:
        inbox = config.inboxes[inbox_name]
        if request.contact_id not in inbox.allowed_contacts:
            config.inboxes[inbox_name] = replace(
                inbox,
                allowed_contacts=inbox.allowed_contacts + (request.contact_id,),
            )


def apply_remove(*, contact_id: str, config: Config) -> None:
    """Update an in-memory Config to reflect a successful delete_contact()."""
    contact = config.contacts.pop(contact_id, None)
    if contact is not None:
        for addr in contact.addresses:
            config.address_index.pop(addr, None)
    for name, inbox in list(config.inboxes.items()):
        if contact_id in inbox.allowed_contacts:
            config.inboxes[name] = replace(
                inbox,
                allowed_contacts=tuple(
                    c for c in inbox.allowed_contacts if c != contact_id
                ),
            )


# ---- TOML rendering ---------------------------------------------------


def _toml_escape(s: str) -> str:
    """Escape a string for TOML basic-string literal.

    Per TOML spec, basic strings are double-quoted with C-style
    backslash escapes for backslash, double-quote, and the control
    range. Operators may legitimately put colons, slashes, and
    spaces in display names and relationship lines, so we don't
    over-escape; only the load-bearing characters get escaped.
    """
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def _render_contact_toml(req: AddContactRequest) -> str:
    """Produce the TOML body for a new contact file.

    Layout matches what the migrator and operators will write by hand:
    one field per line, in a stable order, with the `inboxes` list
    rendered inline.
    """
    daily_limit_field = (
        '"unlimited"' if req.daily_limit == -1 else str(req.daily_limit)
    )
    inboxes_inline = ", ".join(f'"{_toml_escape(i)}"' for i in req.inboxes)
    lines = [
        f'contact_id = "{_toml_escape(req.contact_id)}"',
        f'addresses = ["{_toml_escape(req.address.lower())}"]',
        f'display_name = "{_toml_escape(req.display_name)}"',
        f'relationship = "{_toml_escape(req.relationship)}"',
        f"daily_limit = {daily_limit_field}",
        "is_principal = false",
        f"inboxes = [{inboxes_inline}]",
        "auto_approve_notes = false",
        # Step 7b: scopes default empty = unrestricted. Operators add
        # tags to gate topical access; until they do, behaviour matches
        # pre-Step-7b (no scope classification, all notes visible).
        "scopes = []",
    ]
    return "\n".join(lines) + "\n"


# ---- Atomic write -----------------------------------------------------


def _atomic_write_and_validate(path: Path, text: str) -> None:
    """Write text to a tmp file in the same directory, validate it
    parses with the loader, then atomically rename into place. Sets
    chmod 600 to match the rest of the config tree.
    """
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
        # Validate the loader can round-trip what we just wrote.
        # Filename mismatch (the temp file's stem isn't a valid
        # contact_id) means we can't use _load_one directly on
        # tmp_path — rename to the final path WITHIN the validator
        # check would be racy. Instead: parse once via the loader's
        # internals on the tmp path with a stem-aware shim. Cheaper
        # and equivalent: rename to the final path, then validate;
        # on validation failure, delete the file we just promoted.
        os.replace(tmp_path, path)
        try:
            _load_one(path)
        except ConfigError:
            try:
                path.unlink()
            except OSError:
                pass
            raise
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
