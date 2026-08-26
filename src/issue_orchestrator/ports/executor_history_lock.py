# pyright: strict
"""Synchronization port for executor learning-history retention."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable


@runtime_checkable
class ExecutorHistoryRetentionLock(Protocol):
    """Coordinate readers and pruning writers across executor processes."""

    def shared(self) -> AbstractContextManager[None]:
        """Hold shared ownership until the returned context exits."""
        ...

    def exclusive(self) -> AbstractContextManager[None]:
        """Hold exclusive ownership until the returned context exits."""
        ...
