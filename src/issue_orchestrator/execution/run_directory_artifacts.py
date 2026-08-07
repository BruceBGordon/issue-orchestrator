"""Atomic file I/O inside a session run directory.

Separated from :class:`~.session_output_adapter.FileSystemSessionOutput` so
"how a run-directory artifact is written safely" stops being interleaved with
"which artifacts a session run has". Everything here is generic: a path, some
bytes, and the write-to-temp-then-replace discipline that keeps a reader from
ever seeing a half-written artifact.

The pending-work claim (#6999 F4) is its first typed client and lives here
because it is exactly that — one small artifact belonging to one run, written
at launch and cleared at settlement.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..domain.pending_work import PendingWorkClaim
from ..infra.terminal_recording import (
    TERMINAL_RECORDING_FILENAME as TERMINAL_RECORDING_NAME,
    append_output_event,
)
from .pending_work_codec import (
    CLAIM_ARTIFACT_NAME,
    PendingWorkClaimDecodeError,
    decode_claim,
    encode_claim,
)


class RunDirectoryArtifacts:
    """Typed and generic artifact access for one run directory."""

    def write_pending_work_claim(
        self, run_dir: Path, claim: PendingWorkClaim
    ) -> None:
        """Record the queued request this run's session took at launch."""
        self._write_json(run_dir / CLAIM_ARTIFACT_NAME, encode_claim(claim))

    def read_pending_work_claim(self, run_dir: Path) -> PendingWorkClaim | None:
        """Rebuild this run's claim, or None when it holds none.

        A claim that exists but cannot be rebuilt is NOT reported as absent:
        that would drop the only record of the work while looking like a clean
        restart. It raises, and the restoration seam reports it.
        """
        claim_path = run_dir / CLAIM_ARTIFACT_NAME
        if not claim_path.is_file():
            return None
        try:
            payload = json.loads(claim_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PendingWorkClaimDecodeError(
                f"pending work claim at {claim_path} is unreadable: {exc}"
            ) from exc
        return decode_claim(payload)

    def clear_pending_work_claim(self, run_dir: Path) -> None:
        """Drop this run's claim once it has been settled."""
        (run_dir / CLAIM_ARTIFACT_NAME).unlink(missing_ok=True)

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
