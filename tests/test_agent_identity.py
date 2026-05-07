"""Tests for the agent identity + personality features.

Coverage:
1. `build_system_prompt` injects the agent name in the identity sentence
   and tells the agent to refer to itself by that name.
2. `build_system_prompt` defaults the name to "Nightjar" when nothing
   else is supplied.
3. When `agent_personality` is provided, the prompt contains a fenced
   "voice and demeanour" block stating that personality is style-only
   and does not override security posture.
4. When `agent_personality` is None or empty, no personality block
   appears at all (default: no extra fence text).
5. `daemon.config.AgentConfig` defaults match the prompt defaults.
6. The `[agent]` INI section overrides both name and personality.
7. `principal_agent.execute()` threads `agent_name` and
   `agent_personality` through to the system prompt by inspecting
   the cmd captured by the fake subprocess.
"""
from __future__ import annotations

import asyncio
import configparser
import textwrap
from pathlib import Path
from typing import Any

import pytest

from daemon import config as config_mod
from daemon import principal_agent


# ---- Pure prompt-builder tests --------------------------------------------


def test_build_system_prompt_uses_default_agent_name(tmp_path: Path) -> None:
    """No agent_name argument → defaults to Nightjar in the identity line."""
    prompt = principal_agent.build_system_prompt(
        audit_log_path=tmp_path / "a.jsonl",
        principal_name="Dylan",
    )
    assert "You are Nightjar," in prompt
    assert "refer to yourself as Nightjar" in prompt


def test_build_system_prompt_uses_custom_agent_name(tmp_path: Path) -> None:
    """Custom name lands in identity sentence and self-reference rule."""
    prompt = principal_agent.build_system_prompt(
        audit_log_path=tmp_path / "a.jsonl",
        principal_name="Dylan",
        agent_name="Helper",
    )
    assert "You are Helper," in prompt
    assert "refer to yourself as Helper" in prompt
    # The default name must NOT appear when overridden.
    assert "You are Nightjar," not in prompt


def test_build_system_prompt_no_personality_no_voice_block(tmp_path: Path) -> None:
    """No personality → no voice-and-demeanour block at all."""
    prompt = principal_agent.build_system_prompt(
        audit_log_path=tmp_path / "a.jsonl",
        principal_name="Dylan",
    )
    assert "Voice and demeanour" not in prompt


def test_build_system_prompt_empty_personality_no_voice_block(tmp_path: Path) -> None:
    """Empty string → treat as None, no voice block."""
    prompt = principal_agent.build_system_prompt(
        audit_log_path=tmp_path / "a.jsonl",
        principal_name="Dylan",
        agent_personality="",
    )
    assert "Voice and demeanour" not in prompt


def test_build_system_prompt_personality_appears_fenced(tmp_path: Path) -> None:
    """Personality string lands inside a fenced block that disclaims
    security relevance. Both the personality text AND the disclaimer
    must be present so a downstream reviewer can confirm the fence."""
    pers = "Crisp and dry, mildly British, never performs enthusiasm."
    prompt = principal_agent.build_system_prompt(
        audit_log_path=tmp_path / "a.jsonl",
        principal_name="Dylan",
        agent_personality=pers,
    )
    assert pers in prompt
    assert "Voice and demeanour" in prompt
    # Fence: personality must explicitly NOT be allowed to override
    # security posture.
    assert "tone and surface style ONLY" in prompt
    assert (
        "not subject to override by the personality framing" in prompt
        or "not subject to override" in prompt
    )


def test_build_system_prompt_includes_state_db_body_caveat(tmp_path: Path) -> None:
    """Item 1 from agent self-feedback: messages table is metadata
    only; bodies are NOT persisted there. Surface this so the next
    agent doesn't burn time discovering it via schema queries."""
    prompt = principal_agent.build_system_prompt(
        audit_log_path=tmp_path / "a.jsonl",
        principal_name="Dylan",
    )
    # Some signal that bodies are not in the messages table:
    assert "METADATA" in prompt or "bodies are NOT persisted" in prompt


def test_build_system_prompt_includes_secret_box_path_hint(tmp_path: Path) -> None:
    """Item 2 from agent self-feedback: read_secrets_file expects a
    Path, not a str. Show it in the prompt."""
    prompt = principal_agent.build_system_prompt(
        audit_log_path=tmp_path / "a.jsonl",
        principal_name="Dylan",
    )
    assert "Path(" in prompt and "read_secrets_file" in prompt


def test_build_system_prompt_mentions_test_creds_for_alt_sender(tmp_path: Path) -> None:
    """Item 3 from agent self-feedback: alternate sender accounts live
    in test_creds.toml. Mention it so capability is discoverable."""
    prompt = principal_agent.build_system_prompt(
        audit_log_path=tmp_path / "a.jsonl",
        principal_name="Dylan",
    )
    assert "test_creds.toml" in prompt


def test_build_system_prompt_async_clarifier_phrasing(tmp_path: Path) -> None:
    """Item 5 from agent self-feedback: 'no interactive turn' was
    misleading; reality is async-not-zero-latency. New phrasing should
    permit clarifiers as their own short email."""
    prompt = principal_agent.build_system_prompt(
        audit_log_path=tmp_path / "a.jsonl",
        principal_name="Dylan",
    )
    assert "async, not interactive" in prompt or "send them as a short email" in prompt


# ---- AgentConfig defaults --------------------------------------------------


def test_agent_config_defaults_match_prompt_defaults() -> None:
    """The AgentConfig dataclass defaults are what build_system_prompt
    will fall back to. If they ever drift apart, we'd silently change
    behaviour for installs without an [agent] section."""
    cfg = config_mod.AgentConfig()
    assert cfg.name == config_mod.DEFAULT_AGENT_NAME == "Nightjar"
    # Personality default should be a non-trivial string with at least
    # one substantive descriptor.
    assert isinstance(cfg.personality, str)
    assert len(cfg.personality) > 20


# ---- INI parsing ----------------------------------------------------------


_PRINCIPAL_TOML = textwrap.dedent("""\
    contact_id = "principal"
    addresses = ["me@example.com"]
    display_name = "Operator"
    relationship = "self"
    daily_limit = "unlimited"
    is_principal = true
    inboxes = ["nightjar"]
    """)


def _write_minimal_config(
    tmp_path: Path, *, agent_section: str | None = None,
) -> Path:
    """Write a config file with [daemon] + an inbox + a principal
    contact TOML + an optional [agent] section."""
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    notes_dir = tmp_path / "notes"
    state_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    notes_dir.mkdir(parents=True)
    contacts_dir = tmp_path / "contacts"
    contacts_dir.mkdir(parents=True)
    (contacts_dir / "principal.toml").write_text(
        _PRINCIPAL_TOML, encoding="utf-8",
    )

    pieces = [
        textwrap.dedent(f"""\
        [daemon]
        state_dir = {state_dir}
        log_dir = {log_dir}
        notes_dir = {notes_dir}
        contacts_dir = {contacts_dir}

        [inbox:nightjar]
        enabled = true
        imap_host = imap.example.com
        imap_port = 993
        imap_user = bot@example.com
        imap_password = secret-imap-password
        trusted_authserv = mx.google.com
        """),
    ]
    if agent_section is not None:
        pieces.append(agent_section)

    cfg_path = tmp_path / "nightjar.conf"
    cfg_path.write_text("\n".join(pieces), encoding="utf-8")
    cfg_path.chmod(0o600)
    return cfg_path


def test_load_config_default_agent_when_section_missing(tmp_path: Path) -> None:
    cfg_path = _write_minimal_config(tmp_path, agent_section=None)
    cfg = config_mod.load(cfg_path)
    assert cfg.agent.name == "Nightjar"
    assert cfg.agent.personality == config_mod.DEFAULT_AGENT_PERSONALITY


def test_load_config_agent_name_override(tmp_path: Path) -> None:
    cfg_path = _write_minimal_config(
        tmp_path,
        agent_section="[agent]\nname = Sage\n",
    )
    cfg = config_mod.load(cfg_path)
    assert cfg.agent.name == "Sage"
    # personality stays default
    assert cfg.agent.personality == config_mod.DEFAULT_AGENT_PERSONALITY


def test_load_config_agent_personality_override(tmp_path: Path) -> None:
    cfg_path = _write_minimal_config(
        tmp_path,
        agent_section=(
            "[agent]\n"
            "personality = Talks like a sphinx. Riddles only.\n"
        ),
    )
    cfg = config_mod.load(cfg_path)
    assert cfg.agent.name == "Nightjar"  # default
    assert cfg.agent.personality == "Talks like a sphinx. Riddles only."


def test_load_config_agent_both_overrides(tmp_path: Path) -> None:
    cfg_path = _write_minimal_config(
        tmp_path,
        agent_section=(
            "[agent]\n"
            "name = Echo\n"
            "personality = Brief, factual, military-terse.\n"
        ),
    )
    cfg = config_mod.load(cfg_path)
    assert cfg.agent.name == "Echo"
    assert cfg.agent.personality == "Brief, factual, military-terse."


# ---- Executor wiring -------------------------------------------------------


class _StubStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _StubStdin:
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _StubProc:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _StubStream(lines)
        self.stderr = _StubStream([])
        self.stdin = _StubStdin()

    async def wait(self) -> int:
        return 0

    def send_signal(self, sig: int) -> None: ...
    def kill(self) -> None: ...


def test_execute_threads_agent_name_into_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cmd captured by the fake subprocess must contain
    --system-prompt with the configured agent name."""
    import json

    proc = _StubProc(
        lines=[
            (json.dumps({"type": "system", "subtype": "init"}) + "\n").encode(),
            (json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "ok"}]},
            }) + "\n").encode(),
            (json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "result": "", "usage": {"input_tokens": 1, "output_tokens": 1},
            }) + "\n").encode(),
        ],
    )
    captured: dict[str, Any] = {}

    async def fake_create(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(
        principal_agent.asyncio, "create_subprocess_exec", fake_create,
    )

    asyncio.run(principal_agent.execute(
        request_body="hello",
        principal_name="Dylan",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
        agent_name="Echo",
        agent_personality="Brief, factual.",
    ))

    cmd = list(captured["args"])
    # Find --system-prompt and inspect the next arg.
    sp_idx = cmd.index("--system-prompt")
    sp_value = cmd[sp_idx + 1]
    assert "You are Echo," in sp_value
    assert "Brief, factual." in sp_value
    assert "Voice and demeanour" in sp_value


def test_execute_default_agent_name_when_unspecified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No agent_name kwarg → 'Nightjar' lands in the prompt."""
    import json

    proc = _StubProc(
        lines=[
            (json.dumps({"type": "system", "subtype": "init"}) + "\n").encode(),
            (json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "ok"}]},
            }) + "\n").encode(),
            (json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "result": "", "usage": {"input_tokens": 1, "output_tokens": 1},
            }) + "\n").encode(),
        ],
    )
    captured: dict[str, Any] = {}

    async def fake_create(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(
        principal_agent.asyncio, "create_subprocess_exec", fake_create,
    )

    asyncio.run(principal_agent.execute(
        request_body="hello",
        principal_name="Dylan",
        audit_dir=tmp_path / "audit",
        cwd=tmp_path,
    ))

    cmd = list(captured["args"])
    sp_idx = cmd.index("--system-prompt")
    sp_value = cmd[sp_idx + 1]
    assert "You are Nightjar," in sp_value
    # No personality block when caller doesn't pass one.
    assert "Voice and demeanour" not in sp_value


# ---- Agent workspace + bootstrap CLAUDE.md --------------------------------


def test_daemon_config_agent_cwd_is_under_state_dir(tmp_path: Path) -> None:
    """The agent_cwd property must derive from state_dir so an
    operator who relocates state_dir gets a colocated agent
    workspace."""
    daemon = config_mod.DaemonConfig(
        state_dir=tmp_path / "weird-place",
        log_dir=tmp_path / "logs",
    )
    assert daemon.agent_cwd == tmp_path / "weird-place" / "agent-workspace"


def test_ensure_agent_workspace_creates_dir_and_seeds_claude_md(
    tmp_path: Path,
) -> None:
    """First call with a fresh path: makes the directory, writes
    starter CLAUDE.md."""
    workspace = tmp_path / "agent-workspace"
    assert not workspace.exists()
    principal_agent.ensure_agent_workspace(workspace)
    assert workspace.is_dir()
    claude_md = workspace / "CLAUDE.md"
    assert claude_md.exists()
    contents = claude_md.read_text()
    # Bootstrap-chain shape: must mention the canonical "how do I"
    # primitives Nightjar surfaced wanting in its first reply.
    assert "secret_box" in contents
    assert "test_creds.toml" in contents
    assert "state.db" in contents
    assert "imap.gmail.com" in contents


def test_ensure_agent_workspace_does_not_overwrite_existing_claude_md(
    tmp_path: Path,
) -> None:
    """Idempotent: the agent's edits to CLAUDE.md must survive
    subsequent dispatch calls."""
    workspace = tmp_path / "agent-workspace"
    workspace.mkdir()
    claude_md = workspace / "CLAUDE.md"
    user_text = "# my notes\n\nThings I've learned this week.\n"
    claude_md.write_text(user_text)

    # Call ensure twice (the dispatcher calls it on every turn).
    principal_agent.ensure_agent_workspace(workspace)
    principal_agent.ensure_agent_workspace(workspace)

    # User content survives untouched.
    assert claude_md.read_text() == user_text


def test_ensure_agent_workspace_idempotent_when_dir_exists_but_no_claude_md(
    tmp_path: Path,
) -> None:
    """Edge case: directory was created by some earlier path that
    didn't seed CLAUDE.md (e.g. operator made it manually). On next
    dispatch, seed it."""
    workspace = tmp_path / "agent-workspace"
    workspace.mkdir()
    assert not (workspace / "CLAUDE.md").exists()

    principal_agent.ensure_agent_workspace(workspace)
    assert (workspace / "CLAUDE.md").exists()


def test_system_prompt_mentions_workspace_and_claude_md(tmp_path: Path) -> None:
    """The system prompt must point the agent at its workspace and
    its editable CLAUDE.md so the bootstrap chains are discoverable."""
    prompt = principal_agent.build_system_prompt(
        audit_log_path=tmp_path / "a.jsonl",
        principal_name="Dylan",
    )
    assert "workspace" in prompt.lower()
    assert "CLAUDE.md" in prompt
