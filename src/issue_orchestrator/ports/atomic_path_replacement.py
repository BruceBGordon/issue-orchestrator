"""Port for atomically replacing one durable filesystem path."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class AtomicPathReplacement(Protocol):
    """Replace a destination with a completely written sibling path."""

    def replace(self, source: Path, destination: Path) -> None: ...
