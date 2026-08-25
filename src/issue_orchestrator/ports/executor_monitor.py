"""Read-only monitoring port for the machine-wide executor."""

from __future__ import annotations

from typing import Protocol

from ..domain.executor_monitoring import (
    ExecutorEventTimeline,
    ExecutorRecentEventsQuery,
    ExecutorStatus,
)


class ExecutorMonitor(Protocol):
    """Query typed activity without reading executor storage directly."""

    def recent_events(
        self,
        query: ExecutorRecentEventsQuery,
    ) -> ExecutorEventTimeline:
        """Return the newest durable events in chronological order."""
        ...

    def status(self) -> ExecutorStatus:
        """Return current host policy and retained learning evidence."""
        ...
