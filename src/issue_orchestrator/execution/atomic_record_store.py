# pyright: strict
"""One crash-safe atomic persistence owner for executor JSON records."""

from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from pydantic import BaseModel

from ..ports.atomic_path_replacement import AtomicPathReplacement


_CRASH_REMNANT_PREFIX = ".io-executor-atomic-"
_CRASH_REMNANT_SUFFIX = ".tmp"


@dataclass(frozen=True, slots=True)
class AtomicRecordPruneResult:
    """Exact crash remnants removed during one locked maintenance pass."""

    removed_paths: tuple[Path, ...]


class OsAtomicPathReplacement:
    """Production adapter for the operating system's atomic rename primitive."""

    def replace(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)


class ExecutorAtomicRecordStore:
    """Deep owner of temporary naming, sync, replacement, and crash pruning."""

    def __init__(
        self,
        directory: Path,
        replacement: AtomicPathReplacement,
    ) -> None:
        if not directory.is_absolute():
            raise ValueError(
                "ExecutorAtomicRecordStore.directory must be an absolute path"
            )
        self._directory = directory
        self._replacement = replacement
        self._lock_path = directory / "atomic-records.lock"

    def write(self, path: Path, record: BaseModel) -> None:
        """Replace one owned record after pruning debris under the same lock."""
        self._require_owned_path(path)
        self._directory.mkdir(parents=True, exist_ok=True)
        with self._locked():
            self._prune_crash_remnants_unlocked()
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=_CRASH_REMNANT_PREFIX,
                suffix=_CRASH_REMNANT_SUFFIX,
                dir=self._directory,
            )
            temporary = Path(temporary_name)
            descriptor_owned = True
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    descriptor_owned = False
                    handle.write(record.model_dump_json() + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self._replacement.replace(temporary, path)
                self._sync_directory()
            finally:
                if descriptor_owned:
                    os.close(descriptor)
                temporary_still_exists = temporary.exists()
                temporary.unlink(missing_ok=True)
                if temporary_still_exists:
                    self._sync_directory()

    def prune_crash_remnants(self) -> AtomicRecordPruneResult:
        """Remove only recognizable executor atomic debris under its owner lock."""
        self._directory.mkdir(parents=True, exist_ok=True)
        with self._locked():
            return self._prune_crash_remnants_unlocked()

    def _prune_crash_remnants_unlocked(self) -> AtomicRecordPruneResult:
        removed: list[Path] = []
        pattern = f"{_CRASH_REMNANT_PREFIX}*{_CRASH_REMNANT_SUFFIX}"
        for path in sorted(self._directory.glob(pattern)):
            path.unlink()
            removed.append(path)
        if removed:
            self._sync_directory()
        return AtomicRecordPruneResult(tuple(removed))

    def _require_owned_path(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("ExecutorAtomicRecordStore.path must be absolute")
        if path.parent != self._directory:
            raise ValueError(
                "ExecutorAtomicRecordStore.path must be a direct child of its "
                "owned directory"
            )

    @contextmanager
    def _locked(self) -> Generator[None]:
        with self._lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            yield

    def _sync_directory(self) -> None:
        descriptor = os.open(self._directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
