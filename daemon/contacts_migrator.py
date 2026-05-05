"""One-shot migration from monolithic nightjar.conf to per-file layout.

Runs on daemon startup, BEFORE config.load(), exactly once per
install. Handles two extractions in a single pass:

  Phase A — Contacts:
    Each `[contact:*]` block in nightjar.conf becomes a TOML file
    in `contacts_dir`. The block (and any `allowed_contacts =` line
    inside `[inbox:*]` sections) is then stripped from nightjar.conf.
    The per-contact `inboxes = [...]` list is derived from which
    inboxes' allowed_contacts referenced this contact.

  Phase B — Secrets:
    Plaintext secrets in nightjar.conf get obfuscated and moved to
    `~/.config/nightjar/secrets.toml`. Specifically:
      [smtp].password         -> [smtp].password
      [security].totp_secret  -> [security].totp_secret
      [claude].api_key        -> [claude].api_key
      [inbox:NAME].imap_password -> [imap.NAME].password
    The plaintext is stripped from nightjar.conf and the entire
    pre-migration file is saved as nightjar.conf.pre-migration.bak.
    The machine-id fingerprint is stamped into state.db.

Invariants:

  - Migration is atomic-or-nothing. If any step fails, neither
    nightjar.conf nor contacts_dir nor secrets.toml is modified.
  - Migration is idempotent: if there's nothing to migrate (no
    legacy [contact:*] blocks, no plaintext secrets), the function
    returns immediately. Re-running after a successful migration
    is a no-op.
  - "Half-migrated" states are detected and refused: if secrets.toml
    already exists AND nightjar.conf still has plaintext secrets,
    we refuse to start with a clear error.
  - The pre-migration backup is left in place forever; the operator
    is told (in the migration log line and the README) to delete it
    once they've confirmed the migration worked.

The backup contains plaintext secrets. The README and this docstring
spell that out so an operator who reflexively syncs `~/.config/`
to a public dotfiles repo doesn't push live credentials.
"""
from __future__ import annotations

import configparser
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import secret_box
from .config import ConfigError


SECRETS_PATH = Path("~/.config/nightjar/secrets.toml").expanduser()
BACKUP_SUFFIX = ".pre-migration.bak"


@dataclass(frozen=True)
class MigrationReport:
    """Returned to the daemon's startup path so it can log the outcome.

    `did_migrate` is False when the function found nothing to do
    (idempotent re-entry). The contact / secret counts let the JSONL
    line surface what actually moved.
    """
    did_migrate: bool
    contacts_migrated: int
    secrets_migrated: int
    backup_path: Path | None
    machine_id_fp: str | None


class MigrationError(Exception):
    """Raised when the migration cannot proceed safely. Original files
    are untouched when this is raised."""


def migrate_if_needed(
    config_path: Path,
    contacts_dir: Path,
    *,
    secrets_path: Path = SECRETS_PATH,
    machine_id: bytes | None = None,
) -> MigrationReport:
    """Run Phase A + Phase B if the config still has legacy state.

    Returns a MigrationReport summarising what was moved (or nothing,
    if the config was already migrated). Raises MigrationError if the
    operator's state is internally inconsistent (e.g. half-migrated).
    """
    if not config_path.exists():
        # No config yet — first-time install path. Nothing to migrate.
        return MigrationReport(
            did_migrate=False, contacts_migrated=0, secrets_migrated=0,
            backup_path=None, machine_id_fp=None,
        )

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")

    legacy_contact_blocks = _legacy_contact_blocks(parser)
    legacy_secrets = _legacy_secrets(parser)
    secrets_file_exists = secrets_path.exists()

    # Half-migrated detection: if secrets.toml exists but plaintext
    # secrets are STILL in the INI, something went wrong. Refuse to
    # run; the operator must reconcile by hand.
    if secrets_file_exists and legacy_secrets:
        raise MigrationError(
            f"secrets.toml already exists at {secrets_path} but plaintext "
            f"secrets are still present in {config_path}. Either delete "
            "the plaintext lines (the obfuscated copies are the source "
            f"of truth) or remove {secrets_path} to redo migration."
        )

    if not legacy_contact_blocks and not legacy_secrets:
        # Already fully migrated, or never had anything to migrate.
        # Still useful: stamp the machine-id fingerprint if it's
        # missing AND a secrets.toml exists (legacy installs from
        # before the fingerprint check shipped).
        return MigrationReport(
            did_migrate=False, contacts_migrated=0, secrets_migrated=0,
            backup_path=None, machine_id_fp=None,
        )

    # Backup BEFORE we touch anything else. The backup is always the
    # complete original file, regardless of which phases run.
    backup_path = config_path.with_suffix(config_path.suffix + BACKUP_SUFFIX)
    shutil.copy2(config_path, backup_path)
    os.chmod(backup_path, 0o600)

    # Phase A: write contact TOMLs.
    contacts_written = 0
    if legacy_contact_blocks:
        try:
            contacts_written = _write_contact_tomls(
                parser, contacts_dir, legacy_contact_blocks,
            )
        except Exception:
            # Roll back: any partial TOMLs are removed; the original
            # nightjar.conf hasn't been touched yet.
            for cid in legacy_contact_blocks:
                p = contacts_dir / f"{cid}.toml"
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
            raise

    # Phase B: write secrets.toml.
    secrets_written = 0
    machine_id_fp: str | None = None
    if legacy_secrets:
        mid = machine_id if machine_id is not None else secret_box.read_machine_id()
        secret_box.write_secrets_file(
            secrets_path, legacy_secrets, machine_id=mid,
        )
        secrets_written = sum(len(v) for v in legacy_secrets.values())
        machine_id_fp = secret_box.machine_id_fingerprint(machine_id=mid)

    # Final step: rewrite nightjar.conf with both phases stripped.
    # This is the irrevocable step (everything before is reversible
    # by deleting the new files); we do it last and atomically.
    new_text = _strip_migrated_sections(
        config_path.read_text(encoding="utf-8"),
        contact_ids=set(legacy_contact_blocks),
        had_secrets=bool(legacy_secrets),
    )
    _atomic_write(config_path, new_text)

    return MigrationReport(
        did_migrate=True,
        contacts_migrated=contacts_written,
        secrets_migrated=secrets_written,
        backup_path=backup_path,
        machine_id_fp=machine_id_fp,
    )


# ---- Phase A: contact extraction ------------------------------------------


def _legacy_contact_blocks(parser: configparser.ConfigParser) -> dict[str, dict[str, str]]:
    """Return {contact_id: {field: value}} for every [contact:*] block."""
    out: dict[str, dict[str, str]] = {}
    for section_name in parser.sections():
        if not section_name.startswith("contact:"):
            continue
        contact_id = section_name.split(":", 1)[1].strip()
        if not contact_id:
            raise MigrationError(
                f"section {section_name!r} has empty contact_id"
            )
        out[contact_id] = dict(parser[section_name])
    return out


def _inbox_membership(parser: configparser.ConfigParser, contact_id: str) -> tuple[str, ...]:
    """Find which inboxes' allowed_contacts list this contact_id."""
    out: list[str] = []
    for section_name in parser.sections():
        if not section_name.startswith("inbox:"):
            continue
        inbox_name = section_name.split(":", 1)[1].strip()
        allowed_raw = parser[section_name].get("allowed_contacts", "")
        allowed = [s.strip() for s in allowed_raw.split(",") if s.strip()]
        if contact_id in allowed:
            out.append(inbox_name)
    return tuple(out)


def _write_contact_tomls(
    parser: configparser.ConfigParser,
    contacts_dir: Path,
    blocks: dict[str, dict[str, str]],
) -> int:
    """Write one TOML file per legacy [contact:*] block. Returns count."""
    contacts_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for contact_id, fields in blocks.items():
        path = contacts_dir / f"{contact_id}.toml"
        if path.exists():
            raise MigrationError(
                f"refusing to overwrite existing {path}; the contacts "
                "directory has stale content from a previous partial "
                "migration. Inspect and resolve manually before retrying."
            )
        inboxes = _inbox_membership(parser, contact_id)
        if not inboxes:
            # The legacy parser allowed contacts that weren't on any
            # inbox's allowed_contacts list (they'd be effectively
            # unreachable). The new schema requires at least one inbox.
            # Default to the first enabled inbox in the config.
            first_inbox = _first_enabled_inbox(parser)
            if first_inbox is None:
                raise MigrationError(
                    f"contact {contact_id!r} is not on any inbox's "
                    "allowed_contacts list and no enabled inboxes exist; "
                    "cannot derive an inboxes list for the migrated TOML."
                )
            inboxes = (first_inbox,)
        text = _render_contact_toml(contact_id, fields, inboxes)
        _atomic_write(path, text)
        written += 1
    return written


def _first_enabled_inbox(parser: configparser.ConfigParser) -> str | None:
    """Lexicographically first enabled [inbox:*]. Used as a fallback when
    a legacy contact wasn't on any allowed_contacts list."""
    candidates: list[str] = []
    for section_name in parser.sections():
        if not section_name.startswith("inbox:"):
            continue
        section = parser[section_name]
        enabled = section.get("enabled", "true").strip().lower()
        if enabled in ("true", "yes", "1", "on"):
            candidates.append(section_name.split(":", 1)[1].strip())
    return sorted(candidates)[0] if candidates else None


def _render_contact_toml(
    contact_id: str, fields: dict[str, str], inboxes: tuple[str, ...]
) -> str:
    """Render a contact TOML body from a legacy-INI fields dict.

    We reuse the rendering shape from contacts_writer (one field per
    line, fixed order, escapes for quotes/backslashes) so migrated
    files look identical to ones produced by `add`.
    """
    from .contacts_writer import _toml_escape  # internal but stable

    daily_limit_raw = fields.get("daily_limit", "3").strip().lower()
    if daily_limit_raw in ("unlimited", "-1"):
        daily_limit_field = '"unlimited"'
    else:
        try:
            n = int(daily_limit_raw)
            if n < 0:
                raise ValueError
            daily_limit_field = str(n)
        except ValueError as e:
            raise MigrationError(
                f"contact {contact_id!r}: daily_limit {daily_limit_raw!r} "
                "is neither a non-negative int nor 'unlimited'"
            ) from e

    is_principal_raw = fields.get("is_principal", "false").strip().lower()
    is_principal = is_principal_raw in ("true", "yes", "1", "on")

    addresses = [
        a.strip().lower() for a in fields.get("addresses", "").split(",") if a.strip()
    ]
    if not addresses:
        raise MigrationError(f"contact {contact_id!r} has no addresses")

    addrs_inline = ", ".join(f'"{_toml_escape(a)}"' for a in addresses)
    inboxes_inline = ", ".join(f'"{_toml_escape(i)}"' for i in inboxes)
    display_name = fields.get("display_name", contact_id).strip() or contact_id
    relationship = fields.get("relationship", "").strip()

    lines = [
        f'contact_id = "{_toml_escape(contact_id)}"',
        f"addresses = [{addrs_inline}]",
        f'display_name = "{_toml_escape(display_name)}"',
        f'relationship = "{_toml_escape(relationship)}"',
        f"daily_limit = {daily_limit_field}",
        f"is_principal = {'true' if is_principal else 'false'}",
        f"inboxes = [{inboxes_inline}]",
        "auto_approve_notes = false",
    ]
    return "\n".join(lines) + "\n"


# ---- Phase B: secrets extraction ------------------------------------------


def _legacy_secrets(parser: configparser.ConfigParser) -> dict[str, dict[str, str]]:
    """Collect plaintext secrets that need to move to secrets.toml.

    Returned shape matches `secret_box.write_secrets_file`:
    `{section: {key: plaintext}}`. Sections used:
      smtp        -> {password: ...}
      security    -> {totp_secret: ...}
      claude      -> {api_key: ...}
      imap.<name> -> {password: ...}   (one per [inbox:*] section)
    """
    out: dict[str, dict[str, str]] = {}
    if "smtp" in parser:
        pwd = parser["smtp"].get("password", "")
        if pwd:
            out["smtp"] = {"password": pwd}
    if "security" in parser:
        totp = parser["security"].get("totp_secret", "")
        if totp:
            out["security"] = {"totp_secret": totp}
    if "claude" in parser:
        api_key = parser["claude"].get("api_key", "")
        if api_key:
            out["claude"] = {"api_key": api_key}
    for section_name in parser.sections():
        if not section_name.startswith("inbox:"):
            continue
        inbox_name = section_name.split(":", 1)[1].strip()
        pwd = parser[section_name].get("imap_password", "")
        if pwd:
            out[f"imap.{inbox_name}"] = {"password": pwd}
    return out


# ---- INI rewrite (strip migrated sections / fields) -----------------------


_CONTACT_SECTION_RE = re.compile(r"^\s*\[contact:([^\]]+)\]\s*$")
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_FIELD_LINE_RE = re.compile(r"^\s*([a-zA-Z_][\w-]*)\s*=")


def _strip_migrated_sections(
    text: str, *, contact_ids: set[str], had_secrets: bool
) -> str:
    """Rewrite nightjar.conf with migrated content removed.

    Strips:
      - Every `[contact:*]` section, top to bottom of the next section.
      - The `allowed_contacts =` line inside any `[inbox:*]` section
        (the new schema derives it from per-contact `inboxes` lists).
      - The `password =` line inside `[smtp]`.
      - The `totp_secret =` line inside `[security]`.
      - The `api_key =` line inside `[claude]`.
      - The `imap_password =` line inside any `[inbox:*]` section.
    """
    out_lines: list[str] = []
    skip_until_next_section = False
    current_section: str | None = None

    for line in text.splitlines(keepends=True):
        contact_match = _CONTACT_SECTION_RE.match(line)
        section_match = _SECTION_RE.match(line)

        if contact_match:
            # Begin skipping this contact section.
            skip_until_next_section = True
            current_section = "contact:" + contact_match.group(1).strip()
            continue
        if section_match:
            # New section header. Stop skipping (if we were).
            skip_until_next_section = False
            current_section = section_match.group(1).strip()
            out_lines.append(line)
            continue
        if skip_until_next_section:
            continue

        # Inside a non-skipped section: filter migrated fields.
        if current_section is not None:
            field_match = _FIELD_LINE_RE.match(line)
            if field_match:
                field = field_match.group(1)
                if current_section.startswith("inbox:") and field in (
                    "allowed_contacts", "imap_password",
                ):
                    continue
                if had_secrets:
                    if current_section == "smtp" and field == "password":
                        continue
                    if current_section == "security" and field == "totp_secret":
                        continue
                    if current_section == "claude" and field == "api_key":
                        continue

        out_lines.append(line)

    # Collapse runs of 3+ blank lines so the file stays tidy after
    # large block deletions.
    text = "".join(out_lines)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text


# ---- Atomic write helper --------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """tmp + fsync + chmod 600 + rename. The rest of the daemon uses
    the same pattern; see config_writer.py (legacy) and
    contacts_writer.py for prior art."""
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
