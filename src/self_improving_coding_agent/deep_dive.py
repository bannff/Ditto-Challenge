from __future__ import annotations

import os
import stat
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .contracts import RUN_ID_RE
from .scrub import scrub_text

_MAX_RECORD_BYTES = 4_096
_MAX_LOG_BYTES = 1_000_000


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: float = Field(ge=0)
    run_id: str
    kind: str
    status: str
    node: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    category: str | None = None
    score: float | None = None
    threshold: float | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    chain_length: int | None = Field(default=None, ge=0)
    chain_head: str | None = None
    outcome: str | None = None


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return scrub_text("".join(char if char.isprintable() else " " for char in value))[:80]


class DeepDiveWriter:
    """Best-effort, allowlisted demo evidence outside canonical artifact bundles.

    Writes ``<out>/<run_id>.jsonl`` into exactly the directory it is given. Callers keep
    it OUTSIDE canonical bundle roots: the offline verifier rejects non-canonical entries,
    so auxiliary evidence lives beside a bundle, never inside it."""

    def __init__(self, out: Path) -> None:
        self._out = out
        self._disabled = False

    def record(self, event: dict[str, Any]) -> None:
        if self._disabled:
            return
        try:
            record = self._record(event)
            if record is None:
                return
            self._append(record)
        except (OSError, ValueError, TypeError):
            self._disabled = True

    def _record(self, event: dict[str, Any]) -> _Record | None:
        run_id = event.get("run_id")
        kind = event.get("kind")
        status = event.get("status")
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            return None
        if kind not in {"run", "node", "tool", "evaluator", "detector", "terminal"}:
            return None
        if not isinstance(status, str):
            return None
        values: dict[str, Any] = {
            "timestamp": event.get("timestamp", time.time()),
            "run_id": run_id,
            "kind": kind,
            "status": _safe_text(status) or "unknown",
        }
        for field in ("node", "category", "chain_head", "outcome"):
            value = _safe_text(event.get(field))
            if value is not None:
                values[field] = value
        for field in ("attempt", "chain_length"):
            value = event.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                values[field] = value
        for field in ("score", "threshold"):
            value = event.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values[field] = float(value)
        duration = event.get("duration_ms")
        if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0:
            values["duration_ms"] = duration
        return _Record.model_validate(values)

    def _append(self, record: _Record) -> None:
        data = (record.model_dump_json() + "\n").encode()
        if len(data) > _MAX_RECORD_BYTES:
            return
        directory_fd = _open_artifact_directory(self._out)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(f"{record.run_id}.jsonl", flags, 0o600, dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size + len(data) > _MAX_LOG_BYTES:
                    raise ValueError("unsafe deep-dive log")
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)


def _open_artifact_directory(out: Path) -> int:
    """Create and open the destination without following any path component symlink."""
    current_fd = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        absolute = Path(os.path.abspath(out))
        for part in absolute.parts[1:-1]:
            child_fd = _open_child_directory(current_fd, part)
            os.close(current_fd)
            current_fd = child_fd
        return _open_child_directory(current_fd, absolute.parts[-1])
    finally:
        os.close(current_fd)


def _open_child_directory(parent_fd: int, name: str) -> int:
    with suppress(FileExistsError):
        os.mkdir(name, mode=0o755, dir_fd=parent_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    return os.open(name, flags, dir_fd=parent_fd)
