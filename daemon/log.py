"""Structured JSONL logging for Nightjar.

One JSON object per line, written to ~/nightjar/logs/nightjar-YYYY-MM-DD.jsonl
(rotated daily). Common fields on every event: ts, inbox (optional), event,
level, plus event-specific fields.

This is intentionally separate from Python's stdlib logging because the
output format is rigorously structured (downstream tooling will read it),
and stdlib logging's formatter system is overkill for one append-only
JSONL file.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JSONLLogger:
    def __init__(self, log_dir: Path, *, also_stderr: bool = True) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.also_stderr = also_stderr
        self._lock = threading.Lock()
        self._current_date: str | None = None
        self._fh = None

    def _path_for_today(self) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            if self._fh is not None:
                self._fh.close()
                self._fh = None
        if self._fh is None:
            path = self.log_dir / f"nightjar-{today}.jsonl"
            self._fh = open(path, "a", encoding="utf-8")
        return self.log_dir / f"nightjar-{today}.jsonl"

    def event(self, event: str, *, level: str = "info", **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "level": level,
        }
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._path_for_today()
            self._fh.write(line + "\n")
            self._fh.flush()
        if self.also_stderr:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


# Sentinel for "no logger configured yet" (used by modules that may be
# imported before the daemon initialises its logger).
_NULL_LOGGER: JSONLLogger | None = None


def get_null_logger() -> JSONLLogger:
    """For tests: a logger that swallows events without writing."""
    class _Null:
        def event(self, *args: Any, **kwargs: Any) -> None:
            pass
        def close(self) -> None:
            pass
    return _Null()  # type: ignore[return-value]
