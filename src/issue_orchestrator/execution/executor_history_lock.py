# pyright: strict
"""POSIX adapter for executor learning-history synchronization."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from ..infra.posix_file_lock import (
    PosixFileLockAcquisition,
    PosixFileLockFilePresence,
    PosixFileLockMode,
    PosixFileLockOwner,
    PosixFileLockSpecification,
)


class PosixExecutorHistoryRetentionLock:
    """Own the advisory reader/writer lock guarding history and pruning."""

    def __init__(self, lock_path: Path) -> None:
        if not lock_path.is_absolute():
            raise ValueError(
                "PosixExecutorHistoryRetentionLock.lock_path must be absolute"
            )
        self._lock_path = lock_path
        self._file_locks = PosixFileLockOwner()
        self._shared_specification = PosixFileLockSpecification(
            lock_path,
            PosixFileLockMode.SHARED,
            PosixFileLockAcquisition.BLOCKING,
            PosixFileLockFilePresence.CREATE_IF_MISSING,
        )
        self._exclusive_specification = PosixFileLockSpecification(
            lock_path,
            PosixFileLockMode.EXCLUSIVE,
            PosixFileLockAcquisition.BLOCKING,
            PosixFileLockFilePresence.CREATE_IF_MISSING,
        )

    @contextmanager
    def shared(self) -> Generator[None]:
        """Hold a cross-process shared lock for the whole read transaction."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_locks.hold(self._shared_specification):
            yield

    @contextmanager
    def exclusive(self) -> Generator[None]:
        """Hold a cross-process exclusive lock for mutation and pruning."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_locks.hold(self._exclusive_specification):
            yield
