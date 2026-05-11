"""Tests for daemon.config_inspect (--show-config / --validate-config)."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from daemon import config_inspect

# Reuse the existing test_config helpers — they already build minimal
# valid configs we can hand to the loader.
from tests.test_config import (
    _SAMPLE_SECRET,
    _minimal_inbox_block,
    write_conf,
    write_principal,
)


# ---- --validate-config ---------------------------------------------------


def test_validate_happy_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path))
    rc = config_inspect.validate(path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK:" in out
    assert "contacts" in out
    assert "1 principal" in out
    assert "dispatch defer : off" in out


def test_validate_reports_dispatch_on_when_enabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [agent.dispatch]
        defer_when_gaming_mode = true
        defer_when_load_above = 4.0
    """)
    rc = config_inspect.validate(path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "dispatch defer : ON" in out
    assert "gamescope" in out
    assert "load_1m > 4.0" in out


def test_validate_returns_2_on_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A broken section value yields exit 2 + a friendly error."""
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [agent.dispatch]
        defer_when_load_above = nonsense
    """)
    rc = config_inspect.validate(path)
    assert rc == 2
    err = capsys.readouterr().err
    assert "config invalid" in err
    assert "defer_when_load_above" in err


def test_validate_returns_2_on_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = config_inspect.validate(tmp_path / "nonexistent.conf")
    assert rc == 2


# ---- --show-config -------------------------------------------------------


def test_show_basic_layout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid minimal config dumps cleanly with all expected
    section headers and never leaks the TOTP secret."""
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + f"""
        [security]
        totp_secret = {_SAMPLE_SECRET}
    """)
    rc = config_inspect.show(path)
    assert rc == 0
    out = capsys.readouterr().out
    # Each major section header is present.
    for header in (
        "[daemon]", "[security]", "[agent]", "[agent.dispatch]",
        "[inbox:nightjar]",
    ):
        assert header in out, f"missing section: {header}"
    # The TOTP secret VALUE is never printed — only its presence flag.
    assert _SAMPLE_SECRET not in out
    assert "totp_secret" in out
    assert "(set)" in out
    # Defaults are tagged.
    assert "(default)" in out


def test_show_marks_default_vs_from_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Operator-set values are tagged '(from file)', untouched values
    are tagged '(default)'."""
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [agent]
        name = Crow
    """)
    rc = config_inspect.show(path)
    assert rc == 0
    out = capsys.readouterr().out
    # Custom name should carry the from-file tag.
    assert re.search(r"name\s+'Crow'\s+\(from file\)", out)
    # Untouched fields carry the default tag (e.g. agent.dispatch).
    assert re.search(r"defer_when_gaming_mode\s+False\s+\(default\)", out)


def test_show_returns_2_on_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    write_principal(tmp_path)
    path = write_conf(tmp_path, _minimal_inbox_block(tmp_path) + """
        [agent.dispatch]
        defer_when_load_above = oops
    """)
    rc = config_inspect.show(path)
    assert rc == 2
    err = capsys.readouterr().err
    assert "cannot show config" in err
