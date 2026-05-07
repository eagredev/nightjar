"""End-to-end smoke test: classifier called through ClaudeCodePipeClient.

Calls the real `classify_two_axis` (via the daemon's existing code
path) but with the new ClaudeCodePipeClient backend instead of the
SDK. Confirms the toggle works in production code paths, not just in
unit tests.

Run:
    ~/nightjar/.venv/bin/python3 ~/nightjar/tools/cc_executor_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from daemon.cc_executor import ClaudeCodePipeClient
from daemon.config import ClaudeConfig, Contact
from daemon.scope_classifier import (
    ClassifierError,
    TwoAxisResult,
    classify_two_axis,
)


_FACETS_REGISTRY = {
    "calendar": "scheduling and availability",
    "communication-style": "tone and cadence",
}
_PROJECTS_REGISTRY = {
    "aurora": "the Aurora redesign",
    "aurora.music": "music for Aurora",
    "aurora.legal": "legal work for Aurora",
    "nightjar-dev": "the Nightjar codebase",
}
CONTACT = Contact(
    contact_id="fraser",
    addresses=("fraser@example.com",),
    display_name="Fraser",
    relationship="collaborator",
    daily_limit=3,
    is_principal=False,
    inboxes=("nightjar",),
    scopes=(),
    facets=("calendar", "communication-style"),
    projects=("aurora", "aurora.music", "aurora.legal", "nightjar-dev"),
)
# api_key is unused in claude_code_pipe mode but the dataclass requires
# the field. Set to a plausible-looking sentinel to avoid validation
# friction if anything inspects it.
CONFIG = ClaudeConfig(
    api_key="sk-ant-unused-in-pipe-mode-" + "x" * 40,
    backend="claude_code_pipe",
    scope_classifier_model="claude-haiku-4-5",
)


async def main() -> int:
    client = ClaudeCodePipeClient()
    print("--- end-to-end smoke (ClaudeCodePipeClient -> classify_two_axis) ---")
    result = await classify_two_axis(
        contact=CONTACT,
        sender="fraser@example.com",
        subject="Tuesday session for Aurora music?",
        body=(
            "Hey, can we lock in 2pm Tuesday at the studio for the Aurora "
            "music session? I want to track the new vocal pass."
        ),
        facets_registry=_FACETS_REGISTRY,
        projects_registry=_PROJECTS_REGISTRY,
        safe_notes="",
        config=CONFIG,
        client=client,
    )

    if isinstance(result, ClassifierError):
        print(f"FAIL: classifier returned error — {result.reason}: {result.detail}")
        return 1
    assert isinstance(result, TwoAxisResult)
    print(f"  facets:  {list(result.facets)}")
    print(f"  project: {result.project}")
    print(f"  tokens:  in={result.raw_input_tokens} out={result.raw_output_tokens}")
    print()
    print("PASS — classifier produced a valid TwoAxisResult via the CLI backend.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
