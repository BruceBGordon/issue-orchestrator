# pyright: strict
"""One crash-safe atomic persistence owner for strict JSON records."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from ..ports.atomic_path_replacement import AtomicPathReplacement
from ..ports.atomic_record_store import AtomicRecordPersistence
from ..domain.independent_cleanup import (
    CleanupAction,
    IndependentCleanupPlan,
    raise_cleanup_failures,
    raise_primary_with_cleanup,
)
from ..infra.posix_file_lock import (
    PosixFileLockAcquisition,
    PosixFileLockFilePresence,
    PosixFileLockMode,
    PosixFileLockOwner,
    PosixFileLockSpecification,
)


_CRASH_REMNANT_PREFIX = ".io-atomic-record-"
_LEGACY_CRASH_REMNANT_PREFIX = ".io-executor-atomic-"
_CRASH_REMNANT_SUFFIX = ".tmp"


@dataclass(frozen=True, slots=True)
class AtomicRecordPruneResult:
    """Exact crash remnants removed during one locked maintenance pass."""

    removed_paths: tuple[Path, ...]


class OsAtomicPathReplacement:
    """Production adapter for the operating system's atomic rename primitive."""

    def replace(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)


class AtomicRecordStore:
    """Deep owner of temporary naming, sync, replacement, and crash pruning."""

    def __init__(
        self,
        directory: Path,
        replacement: AtomicPathReplacement,
    ) -> None:
        if not directory.is_absolute():
            raise ValueError("AtomicRecordStore.directory must be an absolute path")
        self._directory = directory
        self._replacement = replacement
        self._lock_path = directory / "atomic-records.lock"
        self._file_locks = PosixFileLockOwner()
        self._lock_specification = PosixFileLockSpecification(
            self._lock_path,
            PosixFileLockMode.EXCLUSIVE,
            PosixFileLockAcquisition.BLOCKING,
            PosixFileLockFilePresence.CREATE_IF_MISSING,
        )

    def write(self, path: Path, record: BaseModel) -> None:
        """Replace one owned record after pruning debris under the same lock."""
        self._require_owned_path(path)
        self._directory.mkdir(parents=True, exist_ok=True)
        with self._file_locks.hold(self._lock_specification):
            self._prune_crash_remnants_unlocked()
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=_CRASH_REMNANT_PREFIX,
                suffix=_CRASH_REMNANT_SUFFIX,
                dir=self._directory,
            )
            temporary = Path(temporary_name)
            descriptor_owned = True
            handle = None
            try:
                handle = os.fdopen(descriptor, "w", encoding="utf-8")
                descriptor_owned = False
                handle.write(record.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                handle = None
                self._replacement.replace(temporary, path)
                self._sync_directory()
            except BaseException as write_error:
                actions: list[CleanupAction] = []
                if descriptor_owned:
                    actions.append(
                        CleanupAction(
                            "close atomic-record descriptor",
                            lambda: os.close(descriptor),
                        )
                    )
                if handle is not None:
                    actions.append(
                        CleanupAction(
                            "close atomic-record stream",
                            handle.close,
                        )
                    )
                actions.extend(
                    (
                        CleanupAction(
                            "unlink atomic-record temporary",
                            lambda: temporary.unlink(missing_ok=True),
                        ),
                        CleanupAction(
                            "sync atomic-record directory after cleanup",
                            self._sync_directory,
                        ),
                    )
                )
                raise_primary_with_cleanup(
                    "atomic record write and cleanup failures",
                    write_error,
                    IndependentCleanupPlan(tuple(actions)).run(),
                )

    def prune_crash_remnants(self) -> AtomicRecordPruneResult:
        """Remove only recognizable atomic-record debris under its owner lock."""
        self._directory.mkdir(parents=True, exist_ok=True)
        with self._file_locks.hold(self._lock_specification):
            return self._prune_crash_remnants_unlocked()

    def delete(self, path: Path) -> bool:
        """Durably remove one owned record under the atomic-record lock."""
        self._require_owned_path(path)
        self._directory.mkdir(parents=True, exist_ok=True)
        with self._file_locks.hold(self._lock_specification):
            existed = path.exists()
            path.unlink(missing_ok=True)
            if existed:
                self._sync_directory()
            return existed

    def _prune_crash_remnants_unlocked(self) -> AtomicRecordPruneResult:
        removed: list[Path] = []
        for prefix in (_CRASH_REMNANT_PREFIX, _LEGACY_CRASH_REMNANT_PREFIX):
            pattern = f"{prefix}*{_CRASH_REMNANT_SUFFIX}"
            for path in sorted(self._directory.glob(pattern)):
                path.unlink()
                removed.append(path)
        if removed:
            self._sync_directory()
        return AtomicRecordPruneResult(tuple(removed))

    def _require_owned_path(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("AtomicRecordStore.path must be absolute")
        if path.parent != self._directory:
            raise ValueError(
                "AtomicRecordStore.path must be a direct child of its owned directory"
            )

    def _sync_directory(self) -> None:
        descriptor = os.open(self._directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        except BaseException as sync_error:
            raise_primary_with_cleanup(
                "atomic-record directory sync and cleanup failures",
                sync_error,
                IndependentCleanupPlan(
                    (
                        CleanupAction(
                            "close atomic-record directory descriptor",
                            lambda: os.close(descriptor),
                        ),
                    )
                ).run(),
            )
        raise_cleanup_failures(
            "atomic-record directory descriptor cleanup failures",
            IndependentCleanupPlan(
                (
                    CleanupAction(
                        "close atomic-record directory descriptor",
                        lambda: os.close(descriptor),
                    ),
                )
            ).run(),
        )


class OsAtomicRecordStoreFactory:
    """Production composition adapter for crash-safe atomic JSON stores."""

    def create(self, directory: Path) -> AtomicRecordPersistence:
        return AtomicRecordStore(directory, OsAtomicPathReplacement())
