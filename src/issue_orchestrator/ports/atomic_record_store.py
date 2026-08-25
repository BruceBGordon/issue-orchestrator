"""Ports for durable atomic JSON record persistence."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


@runtime_checkable
class AtomicRecordPersistence(Protocol):
    """Persist and delete direct-child JSON records durably."""

    def write(self, path: Path, record: BaseModel) -> None: ...

    def delete(self, path: Path) -> bool: ...


@runtime_checkable
class AtomicRecordStoreFactory(Protocol):
    """Build one persistence owner for an absolute record directory."""

    def create(self, directory: Path) -> AtomicRecordPersistence: ...
