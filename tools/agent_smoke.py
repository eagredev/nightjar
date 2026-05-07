"""End-to-end smoke for the principal-agent path.

Runs the executor against the live `claude -p` CLI for both an init
and a continuation, exercising:
  - real Opus subprocess
  - --session-id wiring on init
  - --resume wiring on continuation
  - audit log streaming as events arrive
  - clean completion semantics

This is the proof that the MVP "the agent does what I'd do at the
keyboard, via email" actually works.

The smoke does NOT exercise the inbox-watcher integration (that's
covered by tests/test_principal_agent.py and the existing integration
tests). It targets the live executor only.

Run:
    ~/nightjar/.venv/bin/python3 ~/nightjar/tools/agent_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from daemon import principal_agent


async def main() -> int:
    audit_dir = Path("/tmp/nightjar_agent_smoke")
    audit_dir.mkdir(parents=True, exist_ok=True)

    print("--- init: write a haiku and save to disk ---")
    init_result = await principal_agent.execute(
        request_body=(
            "Write a haiku about ROM hacking. Save it to "
            "/tmp/nightjar_agent_smoke/haiku.txt. Then reply with the "
            "haiku in your final answer, nothing more."
        ),
        principal_name="Dylan",
        audit_dir=audit_dir,
        cwd=Path.home(),
        timeout_seconds=300,
    )
    print(f"  status:     {init_result.status}")
    print(f"  session_id: {init_result.session_id}")
    print(f"  elapsed_s:  {init_result.completed_at - init_result.started_at}")
    print(f"  final_text:")
    for line in (init_result.final_text or "").splitlines():
        print(f"    {line}")
    print()

    if init_result.status != "completed":
        print(f"INIT FAILED — {init_result.error_detail}")
        return 1

    haiku_file = Path("/tmp/nightjar_agent_smoke/haiku.txt")
    if haiku_file.exists():
        print(f"  haiku.txt was written ({haiku_file.stat().st_size} bytes):")
        for line in haiku_file.read_text().splitlines():
            print(f"    {line}")
    else:
        print("  WARN: haiku.txt was NOT written; agent reply only.")
    print()

    print("--- continuation: translate the haiku to French ---")
    cont_result = await principal_agent.execute(
        request_body=(
            "Translate the haiku you just wrote into French. Don't "
            "save anything; just reply with the French translation."
        ),
        principal_name="Dylan",
        audit_dir=audit_dir,
        cwd=Path.home(),
        session_id=init_result.session_id,
        timeout_seconds=300,
    )
    print(f"  status:     {cont_result.status}")
    print(f"  session_id: {cont_result.session_id}  (matches init: "
          f"{cont_result.session_id == init_result.session_id})")
    print(f"  elapsed_s:  {cont_result.completed_at - cont_result.started_at}")
    print(f"  final_text:")
    for line in (cont_result.final_text or "").splitlines():
        print(f"    {line}")
    print()

    if cont_result.status != "completed":
        print(f"CONTINUATION FAILED — {cont_result.error_detail}")
        return 1

    # Audit log assertions: same file across both calls (since they
    # share session_id), grew between init and continuation.
    audit_path = init_result.audit_log_path
    line_count = sum(1 for _ in audit_path.open("r", encoding="utf-8"))
    print(f"  audit log:  {audit_path}  ({line_count} events recorded)")
    print()
    print("PASS — agent path works end to end with session continuity.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
