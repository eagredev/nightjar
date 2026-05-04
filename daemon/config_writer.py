"""Atomic writer for nightjar.conf.

The principal can issue tier-4 verbs ('add', 'remove') that mutate the
config file. This module owns those mutations. It does NOT round-trip
the file through configparser; round-tripping would mangle whitespace,
comments, and alignment that the operator may have curated by hand.
Instead, we do targeted line edits and append-only section writes,
preserving the file's surface layout.

Atomicity guarantee:

  1. Compute the new file contents in memory.
  2. Write to a sibling tmp file, fsync, chmod 600.
  3. Re-parse the tmp file with daemon.config.load() to confirm it
     still parses and validates.
  4. os.replace() the tmp into place. On POSIX this is atomic per
     directory, so concurrent reads either see the old file or the
     new file, never a torn write.

If validation fails the tmp is unlinked and the original is untouched.

Reload semantics: after a successful write, callers should mutate the
in-memory Config to match. We expose helper functions (apply_add /
apply_remove) so the executor can do this in a single place rather
than open-coding dict mutation.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config as config_module
from .config import Config, ConfigError, Contact


class ConfigWriteError(Exception):
    """Raised when a mutation would leave the file invalid or
    self-contradictory. Original file is untouched when this is raised."""


@dataclass(frozen=True)
class AddRequest:
    contact_id: str
    address: str
    display_name: str
    relationship: str
    daily_limit: int  # -1 = unlimited
    inbox_name: str   # which [inbox:*] gets this contact_id appended to allowed_contacts


# ---- public API -----------------------------------------------------------


def add_contact(
    *,
    request: AddRequest,
    config: Config,
    config_path: Path = config_module.DEFAULT_CONFIG_PATH,
) -> None:
    """Append a new [contact:*] section and extend the inbox's
    allowed_contacts list. Validates and atomically writes."""
    if request.contact_id in config.contacts:
        raise ConfigWriteError(f"contact_id {request.contact_id!r} already exists")
    if request.address.lower() in config.address_index:
        raise ConfigWriteError(
            f"address {request.address!r} is already claimed by "
            f"{config.address_index[request.address.lower()]!r}"
        )
    if request.inbox_name not in config.inboxes:
        raise ConfigWriteError(f"inbox {request.inbox_name!r} does not exist")
    if not _is_valid_contact_id(request.contact_id):
        raise ConfigWriteError(
            f"contact_id {request.contact_id!r} must match [a-zA-Z0-9_-]+"
        )
    if "@" not in request.address:
        raise ConfigWriteError(f"address {request.address!r} is not an email")

    text = config_path.read_text(encoding="utf-8")
    text = _append_contact_section(text, request)
    text = _add_to_allowed_contacts(text, request.inbox_name, request.contact_id)
    _atomic_write_and_validate(config_path, text)


def remove_contact(
    *,
    contact_id: str,
    config: Config,
    config_path: Path = config_module.DEFAULT_CONFIG_PATH,
) -> None:
    """Remove a [contact:*] section and strip the contact_id from any
    inbox's allowed_contacts. Refuses to remove the principal."""
    if contact_id not in config.contacts:
        raise ConfigWriteError(f"contact_id {contact_id!r} does not exist")
    if config.contacts[contact_id].is_principal:
        raise ConfigWriteError(
            f"refusing to remove principal contact {contact_id!r}; "
            "the principal is the daemon's only authenticated identity"
        )

    text = config_path.read_text(encoding="utf-8")
    text = _remove_contact_section(text, contact_id)
    text = _strip_from_allowed_contacts(text, contact_id)
    _atomic_write_and_validate(config_path, text)


def apply_add(*, request: AddRequest, config: Config) -> None:
    """Update an in-memory Config to reflect a successful add_contact()
    write. Mutates config.contacts and config.address_index in place;
    the surrounding Config dataclass remains frozen but its dicts are
    mutable. Inbox.allowed_contacts is a tuple, so we replace the
    InboxConfig entry."""
    daily_limit = request.daily_limit
    contact = Contact(
        contact_id=request.contact_id,
        addresses=(request.address.lower(),),
        display_name=request.display_name,
        relationship=request.relationship,
        daily_limit=daily_limit,
        is_principal=False,
    )
    config.contacts[request.contact_id] = contact
    config.address_index[request.address.lower()] = request.contact_id
    inbox = config.inboxes[request.inbox_name]
    if request.contact_id not in inbox.allowed_contacts:
        # Tuples are immutable; rebuild the InboxConfig with the
        # extended list. inboxes is a regular dict, so reassignment
        # is fine.
        from dataclasses import replace
        config.inboxes[request.inbox_name] = replace(
            inbox,
            allowed_contacts=inbox.allowed_contacts + (request.contact_id,),
        )


def apply_remove(*, contact_id: str, config: Config) -> None:
    """Update an in-memory Config to reflect a successful remove_contact()
    write."""
    contact = config.contacts.pop(contact_id, None)
    if contact is not None:
        for addr in contact.addresses:
            config.address_index.pop(addr, None)
    from dataclasses import replace
    for name, inbox in list(config.inboxes.items()):
        if contact_id in inbox.allowed_contacts:
            config.inboxes[name] = replace(
                inbox,
                allowed_contacts=tuple(c for c in inbox.allowed_contacts if c != contact_id),
            )


# ---- text mutation -------------------------------------------------------

_CONTACT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _is_valid_contact_id(s: str) -> bool:
    return bool(_CONTACT_ID_RE.match(s))


def _append_contact_section(text: str, req: AddRequest) -> str:
    """Append a new [contact:foo] block to the end of the file."""
    daily_limit_str = "unlimited" if req.daily_limit == -1 else str(req.daily_limit)
    block = (
        f"\n[contact:{req.contact_id}]\n"
        f"addresses = {req.address}\n"
        f"display_name = {req.display_name}\n"
        f"relationship = {req.relationship}\n"
        f"daily_limit = {daily_limit_str}\n"
    )
    if not text.endswith("\n"):
        text += "\n"
    return text + block


def _remove_contact_section(text: str, contact_id: str) -> str:
    """Strip the [contact:contact_id] block from text. The block runs
    from its section header to the next section header (or EOF)."""
    lines = text.splitlines(keepends=True)
    target = f"[contact:{contact_id}]"
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == target:
            skipping = True
            continue
        if skipping and stripped.startswith("[") and stripped.endswith("]"):
            skipping = False
        if not skipping:
            out.append(line)
    # Collapse the trailing blank line that often precedes the removed
    # section header, so we don't accumulate blank-line drift each
    # remove cycle. Only collapse runs of 3+ blank lines down to 2.
    text = "".join(out)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text


_ALLOWED_CONTACTS_RE = re.compile(
    r"^(\s*allowed_contacts\s*=\s*)(.*)$",
    re.MULTILINE,
)


def _add_to_allowed_contacts(text: str, inbox_name: str, contact_id: str) -> str:
    """Append contact_id to the named inbox's allowed_contacts list."""
    return _edit_allowed_contacts(
        text, inbox_name, lambda items: items + [contact_id] if contact_id not in items else items,
    )


def _strip_from_allowed_contacts(text: str, contact_id: str) -> str:
    """Remove contact_id from every inbox's allowed_contacts list."""
    out: list[str] = []
    in_inbox = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_inbox = stripped.startswith("[inbox:")
            out.append(line)
            continue
        if in_inbox:
            m = _ALLOWED_CONTACTS_RE.match(line)
            if m:
                items = [s.strip() for s in m.group(2).split(",") if s.strip()]
                items = [c for c in items if c != contact_id]
                line = f"{m.group(1)}{', '.join(items)}\n"
        out.append(line)
    return "".join(out)


def _edit_allowed_contacts(text: str, inbox_name: str, edit_fn) -> str:
    """Rewrite the allowed_contacts line under [inbox:inbox_name].
    edit_fn takes the current list and returns the new list."""
    target_section = f"[inbox:{inbox_name}]"
    out: list[str] = []
    in_target = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_target = (stripped == target_section)
            out.append(line)
            continue
        if in_target:
            m = _ALLOWED_CONTACTS_RE.match(line)
            if m:
                items = [s.strip() for s in m.group(2).split(",") if s.strip()]
                items = edit_fn(items)
                line = f"{m.group(1)}{', '.join(items)}\n"
        out.append(line)
    return "".join(out)


# ---- atomic write --------------------------------------------------------


def _atomic_write_and_validate(path: Path, text: str) -> None:
    """Write text to a tmp file in the same directory, validate it
    parses, then atomically rename into place. Preserves chmod 600."""
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
        # Validate by re-parsing. Any ConfigError here means the
        # mutation produced an invalid file; we do not promote it.
        try:
            config_module.load(tmp_path)
        except ConfigError as e:
            raise ConfigWriteError(
                f"post-write validation failed: {e}"
            ) from e
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
