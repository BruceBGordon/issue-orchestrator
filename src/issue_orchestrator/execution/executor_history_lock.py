# pyright: strict
"""POSIX adapter for executor learning-history synchronization."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import fcntl
from pathlib import Path


class PosixExecutorHistoryRetentionLock:
    """Own the advisory reader/writer lock guarding history and pruning."""

    def __init__(self, lock_path: Path) -> None:
        if not lock_path.is_absolute():
            raise ValueError(
                "PosixExecutorHistoryRetentionLock.lock_path must be absolute"
            )
        self._lock_path = lock_path

    @contextmanager
    def shared(self) -> Generator[None]:
        """Hold a cross-process shared lock for the whole read transaction."""
        with self._locked(fcntl.LOCK_SH):
            yield

    @contextmanager
    def exclusive(self) -> Generator[None]:
        """Hold a cross-process exclusive lock for mutation and pruning."""
        with self._locked(fcntl.LOCK_EX):
            yield

    @contextmanager
    def _locked(self, operation: int) -> Generator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), operation)
            yield
