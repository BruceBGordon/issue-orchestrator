"""Timeline store port for issue event traces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class TimelineRecord:
    event_id: str
    timestamp: str
    event: str
    data: dict[str, Any]
    source_event: str = ""  # internal event name before fan-out
    instance_id: str = ""  # orchestrator instance UUID (restart boundary)


class TimelineStore(Protocol):
    """Port for persisting and reading per-issue timeline records."""

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        """Append a record for an issue."""
        ...

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        """Read timeline records for an issue."""
        ...

    def delete(self, issue_number: int) -> int:
        """Delete all timeline records for an issue. Returns count deleted."""
        ...

    def references_run(self, issue_number: int, run_dir: Path) -> bool:
        """Return whether an issue Timeline references this exact run."""
        ...

    def relocate_run(
        self, issue_number: int, old_run_dir: Path, new_run_dir: Path
    ) -> int:
        """Rewrite all exact-run references after evidence is archived."""
        ...


class NullTimelineStore:
    """No-op timeline store for tests and disabled configurations."""

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        del issue_number, record
        return None

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        del issue_number, limit
        return []

    def delete(self, issue_number: int) -> int:
        del issue_number
        return 0

    def references_run(self, issue_number: int, run_dir: Path) -> bool:
        del issue_number, run_dir
        return False

    def relocate_run(
        self,
        issue_number: int,
        old_run_dir: Path,
        new_run_dir: Path,
    ) -> int:
        del issue_number, old_run_dir, new_run_dir
        return 0
