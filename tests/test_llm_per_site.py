"""Tests for per-call-site backend + model routing.

Covers:
  - ClaudeConfig.backend_for_site / model_for_site resolution
  - [llm.<site>] section parsing
  - build_claude_client_for picks the right backend per site
  - typo / unknown site → ConfigError
  - per-site override missing one field falls back partially
  - api_key requirement when a per-site override flips to anthropic_api
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from daemon.cc_executor import (
    ClaudeCodePipeClient,
    build_claude_client_for,
)
from daemon.config import (
    BACKEND_ANTHROPIC_API,
    BACKEND_CLAUDE_CODE_PIPE,
    KNOWN_LLM_SITES,
    LLM_SITE_PRINCIPAL_INTERPRET,
    LLM_SITE_SCOPE_CLASSIFIER,
    LLM_SITE_TRIAGE,
    ClaudeConfig,
    ConfigError,
    LlmSiteConfig,
)
from daemon.config import load as load_config

# Reuse the same helpers test_config.py uses so the encrypted-secrets
# dance is centralised.
from tests.test_config import (
    _SAMPLE_SECRET,
    _stash_machine_id,
    write_principal,
    write_conf,
)


_VALID_API_KEY = "sk-ant-" + "x" * 60


# ---- ClaudeConfig methods (no INI parsing) --------------------------------


def test_backend_for_site_inherits_global_when_no_override() -> None:
    cfg = ClaudeConfig(
        api_key=_VALID_API_KEY,
        backend=BACKEND_CLAUDE_CODE_PIPE,
        per_site={},
    )
    for site in KNOWN_LLM_SITES:
        assert cfg.backend_for_site(site) == BACKEND_CLAUDE_CODE_PIPE


def test_backend_for_site_uses_per_site_override() -> None:
    cfg = ClaudeConfig(
        api_key=_VALID_API_KEY,
        backend=BACKEND_ANTHROPIC_API,
        per_site={
            LLM_SITE_TRIAGE: LlmSiteConfig(backend=BACKEND_CLAUDE_CODE_PIPE),
        },
    )
    assert cfg.backend_for_site(LLM_SITE_TRIAGE) == BACKEND_CLAUDE_CODE_PIPE
    assert cfg.backend_for_site(LLM_SITE_SCOPE_CLASSIFIER) == BACKEND_ANTHROPIC_API
    assert cfg.backend_for_site(LLM_SITE_PRINCIPAL_INTERPRET) == BACKEND_ANTHROPIC_API


def test_backend_for_site_partial_override_falls_back_for_unset_field() -> None:
    cfg = ClaudeConfig(
        api_key=_VALID_API_KEY,
        backend=BACKEND_CLAUDE_CODE_PIPE,
        per_site={
            LLM_SITE_TRIAGE: LlmSiteConfig(model="claude-opus-4-7"),
        },
    )
    assert cfg.backend_for_site(LLM_SITE_TRIAGE) == BACKEND_CLAUDE_CODE_PIPE
    assert cfg.model_for_site(LLM_SITE_TRIAGE) == "claude-opus-4-7"


def test_model_for_site_inherits_default_for_triage() -> None:
    cfg = ClaudeConfig(
        api_key=_VALID_API_KEY,
        default_model="claude-haiku-4-5",
    )
    assert cfg.model_for_site(LLM_SITE_TRIAGE) == "claude-haiku-4-5"
    assert cfg.model_for_site(LLM_SITE_PRINCIPAL_INTERPRET) == "claude-haiku-4-5"


def test_model_for_site_inherits_classifier_default_for_classifier() -> None:
    cfg = ClaudeConfig(
        api_key=_VALID_API_KEY,
        default_model="claude-sonnet-4-6",
        scope_classifier_model="claude-haiku-4-5",
    )
    assert cfg.model_for_site(LLM_SITE_SCOPE_CLASSIFIER) == "claude-haiku-4-5"
    assert cfg.model_for_site(LLM_SITE_TRIAGE) == "claude-sonnet-4-6"


def test_model_for_site_per_site_override_wins() -> None:
    cfg = ClaudeConfig(
        api_key=_VALID_API_KEY,
        default_model="claude-haiku-4-5",
        per_site={
            LLM_SITE_PRINCIPAL_INTERPRET: LlmSiteConfig(model="claude-opus-4-7"),
        },
    )
    assert cfg.model_for_site(LLM_SITE_PRINCIPAL_INTERPRET) == "claude-opus-4-7"
    assert cfg.model_for_site(LLM_SITE_TRIAGE) == "claude-haiku-4-5"


# ---- build_claude_client_for routing --------------------------------------


def test_build_claude_client_for_routes_per_site() -> None:
    cfg = ClaudeConfig(
        api_key=_VALID_API_KEY,
        backend=BACKEND_ANTHROPIC_API,
        per_site={
            LLM_SITE_TRIAGE: LlmSiteConfig(backend=BACKEND_CLAUDE_CODE_PIPE),
        },
    )
    triage_client = build_claude_client_for(LLM_SITE_TRIAGE, cfg)
    interp_client = build_claude_client_for(LLM_SITE_PRINCIPAL_INTERPRET, cfg)
    assert isinstance(triage_client, ClaudeCodePipeClient)
    assert type(interp_client).__name__ == "AnthropicClient"


def test_build_claude_client_for_unknown_site_raises() -> None:
    cfg = ClaudeConfig(api_key=_VALID_API_KEY)
    with pytest.raises(ValueError, match="unknown LLM call site"):
        build_claude_client_for("sleep_cycle", cfg)


# ---- INI parsing for [llm.<site>] -----------------------------------------


def _setup_minimal_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_claude_api_key: bool = True,
) -> Path:
    """Write encrypted secrets.toml with minimum-viable content for a
    Claude-section config. Returns the secrets path."""
    _stash_machine_id(monkeypatch, bytes(16))
    write_principal(tmp_path)
    from daemon import secret_box
    secrets_path = tmp_path / "secrets.toml"
    payload: dict[str, dict[str, str]] = {
        "security": {"totp_secret": _SAMPLE_SECRET},
        "smtp": {"password": "smtp-pass"},
        "imap.nightjar": {"password": "imap-pass"},
    }
    if include_claude_api_key:
        payload["claude"] = {"api_key": _VALID_API_KEY}
    secret_box.write_secrets_file(
        secrets_path, payload, machine_id=bytes(16),
    )
    return secrets_path


_BASE_CONF = textwrap.dedent("""
    [daemon]
    state_dir = {tmp_path}/state
    log_dir = {tmp_path}/logs
    notes_dir = {tmp_path}/notes

    [inbox:nightjar]
    imap_host = imap.example.com
    imap_user = me@example.com
    trusted_authserv = mx.google.com

    [smtp]
    host = smtp.example.com
    port = 587
    user = me@example.com

    [claude]
    backend = {global_backend}
    default_model = claude-haiku-4-5
    scope_classifier_model = claude-haiku-4-5
""").lstrip()


def _suffix(extra: str) -> str:
    """Already-dedented suffix to append to BASE_CONF."""
    return textwrap.dedent(extra).lstrip()


def test_load_config_no_per_site_yields_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_path = _setup_minimal_secrets(tmp_path, monkeypatch)
    path = write_conf(
        tmp_path,
        _BASE_CONF.format(tmp_path=tmp_path, global_backend="anthropic_api"),
    )
    config = load_config(path, secrets_path=secrets_path)
    assert config.claude is not None
    assert config.claude.per_site == {}


def test_load_config_one_site_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_path = _setup_minimal_secrets(tmp_path, monkeypatch)
    body = _BASE_CONF.format(
        tmp_path=tmp_path, global_backend="anthropic_api",
    ) + _suffix("""
        [llm.triage]
        backend = claude_code_pipe
    """)
    path = write_conf(tmp_path, body)
    config = load_config(path, secrets_path=secrets_path)
    assert config.claude is not None
    assert LLM_SITE_TRIAGE in config.claude.per_site
    site = config.claude.per_site[LLM_SITE_TRIAGE]
    assert site.backend == BACKEND_CLAUDE_CODE_PIPE
    assert site.model is None
    assert config.claude.backend_for_site(LLM_SITE_TRIAGE) == BACKEND_CLAUDE_CODE_PIPE
    assert config.claude.backend_for_site(LLM_SITE_SCOPE_CLASSIFIER) == BACKEND_ANTHROPIC_API


def test_load_config_per_site_model_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_path = _setup_minimal_secrets(tmp_path, monkeypatch)
    body = _BASE_CONF.format(
        tmp_path=tmp_path, global_backend="anthropic_api",
    ) + _suffix("""
        [llm.principal_interpret]
        model = claude-opus-4-7
    """)
    path = write_conf(tmp_path, body)
    config = load_config(path, secrets_path=secrets_path)
    assert config.claude is not None
    assert config.claude.model_for_site(LLM_SITE_PRINCIPAL_INTERPRET) == "claude-opus-4-7"
    assert config.claude.backend_for_site(LLM_SITE_PRINCIPAL_INTERPRET) == BACKEND_ANTHROPIC_API


def test_load_config_unknown_site_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_path = _setup_minimal_secrets(tmp_path, monkeypatch)
    body = _BASE_CONF.format(
        tmp_path=tmp_path, global_backend="anthropic_api",
    ) + _suffix("""
        [llm.sleep_cycle]
        backend = claude_code_pipe
    """)
    path = write_conf(tmp_path, body)
    with pytest.raises(ConfigError, match="unknown LLM call site"):
        load_config(path, secrets_path=secrets_path)


def test_load_config_invalid_backend_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_path = _setup_minimal_secrets(tmp_path, monkeypatch)
    body = _BASE_CONF.format(
        tmp_path=tmp_path, global_backend="anthropic_api",
    ) + _suffix("""
        [llm.triage]
        backend = ollama
    """)
    path = write_conf(tmp_path, body)
    with pytest.raises(ConfigError, match="must be one of"):
        load_config(path, secrets_path=secrets_path)


def test_load_config_per_site_anthropic_without_global_key_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If an override flips to anthropic_api but the global has no
    api_key (because the global is on the pipe), config load fails."""
    secrets_path = _setup_minimal_secrets(
        tmp_path, monkeypatch, include_claude_api_key=False,
    )
    body = _BASE_CONF.format(
        tmp_path=tmp_path, global_backend="claude_code_pipe",
    ) + _suffix("""
        [llm.triage]
        backend = anthropic_api
    """)
    path = write_conf(tmp_path, body)
    with pytest.raises(ConfigError, match=r"requires \[claude\]\.api_key"):
        load_config(path, secrets_path=secrets_path)


def test_load_config_per_site_pipe_without_global_key_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All sites on pipe + global on pipe → no api_key required."""
    secrets_path = _setup_minimal_secrets(
        tmp_path, monkeypatch, include_claude_api_key=False,
    )
    body = _BASE_CONF.format(
        tmp_path=tmp_path, global_backend="claude_code_pipe",
    ) + _suffix("""
        [llm.triage]
        backend = claude_code_pipe
        model = claude-sonnet-4-6
    """)
    path = write_conf(tmp_path, body)
    config = load_config(path, secrets_path=secrets_path)
    assert config.claude is not None
    assert config.claude.api_key == ""
    assert config.claude.backend_for_site(LLM_SITE_TRIAGE) == BACKEND_CLAUDE_CODE_PIPE
    assert config.claude.model_for_site(LLM_SITE_TRIAGE) == "claude-sonnet-4-6"


def test_load_config_empty_model_override_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_path = _setup_minimal_secrets(tmp_path, monkeypatch)
    body = _BASE_CONF.format(
        tmp_path=tmp_path, global_backend="anthropic_api",
    ) + _suffix("""
        [llm.triage]
        model =
    """)
    path = write_conf(tmp_path, body)
    with pytest.raises(ConfigError, match="must not be empty"):
        load_config(path, secrets_path=secrets_path)
