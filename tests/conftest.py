"""Shared test fixtures.

Most tests build their own tmp config in `tmp_path`; this conftest
just isolates them from the live operator's secrets and contacts
directories so a real `~/.config/nightjar/secrets.toml` (created by
the Step 6c migrator on first daemon start) doesn't bleed into
test runs and trip the "in both files" check.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_real_secrets_and_contacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Point DEFAULT_SECRETS_PATH at a non-existent path under tmp_path.

    Tests that explicitly want to exercise the secrets-splice path
    pass `secrets_path=...` to load_config() and override this. Tests
    that don't care just don't get bothered by the live file.
    """
    from daemon import config as config_module
    monkeypatch.setattr(
        config_module, "DEFAULT_SECRETS_PATH",
        tmp_path / "no-secrets-toml-here.toml",
    )
