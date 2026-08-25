"""Pause journal port — durable history of orchestrator pause transitions.

The timeline store is keyed by issue number and drops anything without one, so
orchestrator-scoped lifecycle events have no home there. Rather than overload an
issue timeline with a sentinel key, pauses get their own small append-only
journal: the history is tiny, is read by operators rather than by the issue UI,
and must outlive the process that wrote it.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.pause_state import PauseTransition


class PauseJournal(Protocol):
    """Port for durably recording pause/resume transitions."""

    def record(self, transition: PauseTransition) -> None:
        """Append one transition. Must never raise into the caller."""
        ...

    def recent(self, limit: int = 20) -> list[PauseTransition]:
        """Return the most recent transitions, newest last."""
        ...


class NullPauseJournal:
    """No-op journal for tests and disabled configurations."""

    def record(self, transition: PauseTransition) -> None:
        del transition

    def recent(self, limit: int = 20) -> list[PauseTransition]:
        del limit
        return []
