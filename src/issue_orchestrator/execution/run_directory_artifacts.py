"""Atomic file I/O inside a session run directory.

Separated from :class:`~.session_output_adapter.FileSystemSessionOutput` so
"how a run-directory artifact is written safely" stops being interleaved with
"which artifacts a session run has". Everything here is generic: a path, some
bytes, and the write-to-temp-then-replace discipline that keeps a reader from
ever seeing a half-written artifact.

Note what is deliberately NOT here: the pending-work claim. A run directory
lives inside the session worktree and the launched agent can write to it, so
nothing the orchestrator later trusts as authority may be stored through this
class (#6999 F7).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..infra.terminal_recording import (
    TERMINAL_RECORDING_FILENAME as TERMINAL_RECORDING_NAME,
    append_output_event,
)

class RunDirectoryArtifacts:
    """Atomic file access for one run directory."""

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            if not path.exists():
                return None
            return json.loads(path.read_text())
        except Exception:
            return None

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        temp_path.replace(path)

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(content)
        temp_path.replace(path)

    @staticmethod
    def _append_run_log_line(run_dir: Path, line: str) -> None:
        log_path = run_dir / TERMINAL_RECORDING_NAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        append_output_event(log_path, f"{line}\n")

    @staticmethod
    def _delete_tree(path: Path) -> None:
        for child in path.iterdir():
            if child.is_dir():
                RunDirectoryArtifacts._delete_tree(child)
            else:
                child.unlink()
        path.rmdir()


__all__ = ["RunDirectoryArtifacts"]
