"""Behavior-level port for durable Timeline run evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.timeline_evidence import (
    FinalizeTimelineEvidenceCommand,
    SetTimelineEvidencePinCommand,
    TimelineEvidenceIdentity,
    TimelineEvidenceState,
    TimelineEvidenceStatus,
)


class TimelineEvidence(Protocol):
    """Own archiving, retention, and pin state for exact session runs."""

    def describe(
        self, identity: TimelineEvidenceIdentity
    ) -> TimelineEvidenceState | None:
        """Describe evidence without mutating it, or return None if unmanaged."""
        ...

    def set_pinned(
        self, command: SetTimelineEvidencePinCommand
    ) -> TimelineEvidenceState:
        """Set one exact run's pin state and return its resulting state."""
        ...

    def finalize_terminal(
        self, command: FinalizeTimelineEvidenceCommand
    ) -> TimelineEvidenceState:
        """Finalize one run's terminal timestamp and configured expiry."""
        ...

    def archive_worktree(self, issue_number: int, worktree_path: Path) -> int:
        """Archive every managed run before a normal worktree is removed."""
        ...

    def prune_due(self) -> bool:
        """Return whether periodic archive pruning is due (read-only)."""
        ...

    def prune_expired(self) -> int:
        """Expire due, unpinned archived evidence and return the run count."""
        ...


class NullTimelineEvidence:
    """Inert implementation for disabled surfaces and isolated tests."""

    def describe(
        self, identity: TimelineEvidenceIdentity
    ) -> TimelineEvidenceState | None:
        del identity
        return None

    def set_pinned(
        self, command: SetTimelineEvidencePinCommand
    ) -> TimelineEvidenceState:
        del command
        raise RuntimeError("Timeline evidence retention is not configured")

    def finalize_terminal(
        self, command: FinalizeTimelineEvidenceCommand
    ) -> TimelineEvidenceState:
        return TimelineEvidenceState(
            identity=command.identity,
            status=TimelineEvidenceStatus.MISSING,
            label="Evidence unavailable",
            available=False,
            pinned=False,
            archived=False,
            help_text="Timeline evidence retention is not configured.",
        )

    def archive_worktree(self, issue_number: int, worktree_path: Path) -> int:
        del issue_number, worktree_path
        return 0

    def prune_due(self) -> bool:
        return False

    def prune_expired(self) -> int:
        return 0


NULL_TIMELINE_EVIDENCE = NullTimelineEvidence()
