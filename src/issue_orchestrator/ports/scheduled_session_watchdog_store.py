"""Port for durably binding a scheduled outer watchdog to its session run."""

from __future__ import annotations

from typing import Protocol

from ..domain.session_run import SessionRunAssets
from ..domain.session_watchdog import ScheduledSessionWatchdog


class ScheduledSessionWatchdogStore(Protocol):
    """Persist the planner-owned watchdog before a terminal can be created."""

    def record_scheduled_watchdog(
        self,
        run: SessionRunAssets,
        watchdog: ScheduledSessionWatchdog,
    ) -> None: ...
