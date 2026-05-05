"""Per-file contact loader.

Each contact lives in its own TOML file at
`~/.config/nightjar/contacts/<id>.toml`. The directory is the source
of truth for who Nightjar talks to; the legacy `[contact:*]` blocks
in `nightjar.conf` are migrated out by `contacts_migrator.py` on
first daemon start after this module ships.

Why per-file:

  - `add` / `remove` mutate one file each, not the whole config.
    Tier 2 verbs (single approval) instead of tier 4 — the blast
    radius is one contact, not the entire authentication-bearing
    config file.
  - Rapport notes (Step 7) live in a parallel `~/nightjar/contacts/`
    directory, one Markdown file per contact. Symmetric layout: the
    "spec" lives in `.toml`, the "memory" lives in `.md`.
  - Operators can browse, version, or back up contacts in the
    obvious filesystem-shaped way.

TOML schema (one file per contact):

    contact_id          = "composer"
    addresses           = ["fmcmichael@hotmail.co.uk"]
    display_name        = "Composer"
    relationship        = "Composer for the project"
    daily_limit         = 3                  # int >= 0, or "unlimited"
    is_principal        = false              # default false
    auto_approve_notes  = false              # default false (Step 7)
    inboxes             = ["nightjar"]       # which inboxes accept this contact

Cross-file invariants (validated at load time):

  - Exactly one contact has is_principal=true.
  - No two contacts share an address.
  - No two contacts share a contact_id (filename uniqueness gives this
    for free, but we double-check after stripping the .toml suffix).
  - contact_id matches the filename stem.
  - Every contact has at least one address and at least one inbox.

The loader does NOT validate that referenced inbox names exist; that
cross-check happens after both contacts and inboxes are loaded by
`config.load`. Doing it here would create a load-order coupling
(loader needs to know inbox names before it can validate contacts).
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import Contact, ConfigError


# contact_id format. Same regex used by config_writer.py for the legacy
# INI section name validation; keep them aligned.
_CONTACT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Scope name format. Mirror of the registry-side regex in config.py;
# kept duplicated to avoid importing config from the loader (load order
# is loader-then-config; the cross-validation happens in config.load).
_SCOPE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True)
class LoadResult:
    """Outcome of `load_all`. Both fields are populated together so
    callers don't have to invert one to get the other."""
    contacts: dict[str, Contact]
    address_index: dict[str, str]


def load_all(contacts_dir: Path) -> LoadResult:
    """Load every contact TOML file in `contacts_dir`.

    Returns a `LoadResult` with the contacts dict (keyed by
    contact_id) and a flattened address_index (lowercased addr →
    contact_id). Raises ConfigError on any validation failure;
    the daemon refuses to start in that case rather than running
    with a partially-loaded directory.

    A non-existent directory yields an empty result (the daemon may
    legitimately have no contacts yet, e.g. just-installed). A
    non-empty directory with NO valid TOML files is still empty:
    the loader skips anything that does not end in `.toml` and
    raises only on TOML files that fail validation.
    """
    contacts: dict[str, Contact] = {}
    address_index: dict[str, str] = {}
    principal_id: str | None = None

    if not contacts_dir.exists():
        return LoadResult(contacts={}, address_index={})

    if not contacts_dir.is_dir():
        raise ConfigError(
            f"contacts_dir {contacts_dir!s} exists but is not a directory"
        )

    # sorted() so error messages are deterministic across machines.
    for path in sorted(contacts_dir.iterdir()):
        if not path.is_file():
            continue
        if path.suffix != ".toml":
            continue
        contact = _load_one(path)

        # Cross-file invariants: principal uniqueness, contact_id
        # uniqueness, address uniqueness.
        if contact.is_principal:
            if principal_id is not None:
                raise ConfigError(
                    f"is_principal=true on multiple contacts: "
                    f"{principal_id!r} and {contact.contact_id!r}. Exactly "
                    "one contact must be the principal."
                )
            principal_id = contact.contact_id

        if contact.contact_id in contacts:
            # Should be impossible if the filename matches contact_id
            # (filename uniqueness handles this), but defensive.
            raise ConfigError(
                f"duplicate contact_id {contact.contact_id!r} in "
                f"{contacts_dir!s}"
            )
        contacts[contact.contact_id] = contact

        for addr in contact.addresses:
            if addr in address_index:
                raise ConfigError(
                    f"address {addr!r} is claimed by both "
                    f"{address_index[addr]!r} (already loaded) and "
                    f"{contact.contact_id!r} ({path.name})"
                )
            address_index[addr] = contact.contact_id

    return LoadResult(contacts=contacts, address_index=address_index)


def _load_one(path: Path) -> Contact:
    """Parse and validate a single contact TOML file.

    The filename's stem (without `.toml`) is the canonical contact_id
    and must match the in-file `contact_id` field. This redundancy is
    intentional: it means renaming a file is a deliberate two-step
    edit, not a silent rename, and prevents two files from claiming
    the same id without colliding at the filesystem level.
    """
    expected_id = path.stem
    if not _CONTACT_ID_RE.match(expected_id):
        raise ConfigError(
            f"contact filename {path.name!r}: stem {expected_id!r} must "
            f"match {_CONTACT_ID_RE.pattern}"
        )

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: not valid TOML: {e}") from e
    except OSError as e:
        raise ConfigError(f"{path}: cannot read: {e}") from e

    contact_id = _require_str(data, "contact_id", path)
    if contact_id != expected_id:
        raise ConfigError(
            f"{path}: contact_id {contact_id!r} does not match "
            f"filename stem {expected_id!r}. Either rename the file "
            f"to {contact_id}.toml or update contact_id to {expected_id!r}."
        )

    addresses_raw = _require_list_of_str(data, "addresses", path)
    if not addresses_raw:
        raise ConfigError(f"{path}: addresses list is empty")
    # Lowercased and de-duplicated (within this file). Cross-file
    # uniqueness is checked by load_all().
    seen: set[str] = set()
    addresses: list[str] = []
    for addr in addresses_raw:
        addr = addr.strip().lower()
        if not addr or "@" not in addr:
            raise ConfigError(f"{path}: address {addr!r} is not an email")
        if addr in seen:
            raise ConfigError(
                f"{path}: duplicate address {addr!r} within this file"
            )
        seen.add(addr)
        addresses.append(addr)

    display_name = _optional_str(data, "display_name", contact_id, path)
    relationship = _optional_str(data, "relationship", "", path)
    daily_limit = _parse_daily_limit(data.get("daily_limit", 3), path)
    is_principal = _parse_bool(data.get("is_principal", False), "is_principal", path)
    auto_approve_notes = _parse_bool(
        data.get("auto_approve_notes", False), "auto_approve_notes", path
    )

    inboxes_raw = data.get("inboxes")
    if inboxes_raw is None:
        raise ConfigError(
            f"{path}: inboxes list is required (which inbox(es) accept "
            "mail from this contact). Use inboxes = [\"nightjar\"] for "
            "the default single-inbox setup."
        )
    if not isinstance(inboxes_raw, list) or not all(
        isinstance(i, str) for i in inboxes_raw
    ):
        raise ConfigError(f"{path}: inboxes must be a list of strings")
    if not inboxes_raw:
        raise ConfigError(f"{path}: inboxes list is empty")
    inboxes = tuple(s.strip() for s in inboxes_raw if s.strip())

    # Step 7b: scopes list. Default empty (= unrestricted). The names
    # are syntactically validated here; cross-validation against the
    # [scopes] registry happens in config.load().
    scopes_raw = data.get("scopes", [])
    if not isinstance(scopes_raw, list) or not all(
        isinstance(s, str) for s in scopes_raw
    ):
        raise ConfigError(f"{path}: scopes must be a list of strings")
    scopes: list[str] = []
    seen_scopes: set[str] = set()
    for raw_scope in scopes_raw:
        scope = raw_scope.strip()
        if not scope:
            continue
        if not _SCOPE_NAME_RE.match(scope):
            raise ConfigError(
                f"{path}: scope name {scope!r} is invalid; must match "
                f"{_SCOPE_NAME_RE.pattern}"
            )
        if scope in seen_scopes:
            raise ConfigError(
                f"{path}: duplicate scope {scope!r}"
            )
        seen_scopes.add(scope)
        scopes.append(scope)

    return Contact(
        contact_id=contact_id,
        addresses=tuple(addresses),
        display_name=display_name.strip(),
        relationship=relationship.strip(),
        daily_limit=daily_limit,
        is_principal=is_principal,
        inboxes=inboxes,
        auto_approve_notes=auto_approve_notes,
        scopes=tuple(scopes),
    )


# ---- Field parsers --------------------------------------------------------


def _require_str(data: dict, key: str, path: Path) -> str:
    if key not in data:
        raise ConfigError(f"{path}: required field {key!r} missing")
    val = data[key]
    if not isinstance(val, str):
        raise ConfigError(f"{path}: {key!r} must be a string, got {type(val).__name__}")
    return val


def _require_list_of_str(data: dict, key: str, path: Path) -> list[str]:
    if key not in data:
        raise ConfigError(f"{path}: required field {key!r} missing")
    val = data[key]
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise ConfigError(f"{path}: {key!r} must be a list of strings")
    return val


def _optional_str(data: dict, key: str, default: str, path: Path) -> str:
    val = data.get(key, default)
    if not isinstance(val, str):
        raise ConfigError(f"{path}: {key!r} must be a string, got {type(val).__name__}")
    return val


def _parse_daily_limit(raw, path: Path) -> int:
    """Accept int >= 0 or the string "unlimited" (sentinel for -1).

    The dataclass uses -1 internally for unlimited; the TOML form
    uses the string for readability. Negative ints are rejected.
    """
    if isinstance(raw, str):
        if raw.strip().lower() in ("unlimited", "-1"):
            return -1
        # A bare int-as-string is a TOML quirk we don't tolerate;
        # the operator should write `daily_limit = 3` not `"3"`.
        raise ConfigError(
            f"{path}: daily_limit string must be 'unlimited', "
            f"got {raw!r}. Use a bare int for numeric values."
        )
    if isinstance(raw, bool):
        # bool is a subclass of int in Python, so we have to check
        # for it before the int branch.
        raise ConfigError(f"{path}: daily_limit must not be a bool")
    if not isinstance(raw, int):
        raise ConfigError(
            f"{path}: daily_limit must be an int or 'unlimited', "
            f"got {type(raw).__name__}"
        )
    if raw < 0:
        raise ConfigError(
            f"{path}: daily_limit must be >= 0 or 'unlimited', got {raw}"
        )
    return raw


def _parse_bool(raw, field_name: str, path: Path) -> bool:
    if isinstance(raw, bool):
        return raw
    raise ConfigError(
        f"{path}: {field_name!r} must be true or false, got "
        f"{type(raw).__name__} ({raw!r})"
    )
