"""Durable owner for cached validation records."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..infra.atomic_json import atomic_write_json
from ..ports.session_output import ValidationRecord


logger = logging.getLogger(__name__)


class ValidationRecordStore:
    """Read and atomically write the one cached validation record per SHA."""

    VALIDATION_DIR = ".issue-orchestrator/validation"

    def __init__(self, worktree: Path) -> None:
        if not worktree.is_absolute():
            raise ValueError("ValidationRecordStore.worktree must be absolute")
        self.worktree = worktree
        self.base_dir = worktree / self.VALIDATION_DIR

    def get_record_path(self, sha: str) -> Path:
        """Return the canonical record path for one exact commit SHA."""
        return self.base_dir / f"{sha}.json"

    def write(self, record: ValidationRecord) -> Path:
        """Atomically publish one validation record for concurrent readers."""
        path = self.get_record_path(record.head_sha)
        atomic_write_json(path, record.to_dict())
        logger.debug("Wrote validation record to %s", path)
        return path

    def read(self, sha: str) -> ValidationRecord | None:
        """Read the canonical record, returning no record when unusable."""
        return self._read_path(self.get_record_path(sha))

    def read_legacy(self, suite: str, sha: str) -> ValidationRecord | None:
        """Read the historical per-suite location during cache migration."""
        return self._read_path(self.base_dir / suite / f"{sha}.json")

    @staticmethod
    def _read_path(path: Path) -> ValidationRecord | None:
        if not path.exists():
            return None
        try:
            with path.open() as stream:
                data = json.load(stream)
            return ValidationRecord.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Failed to read validation record at %s: %s", path, exc)
            return None
