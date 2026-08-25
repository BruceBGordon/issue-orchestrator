"""Port for waiting on and containing one explicitly owned process group."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupSupervision,
    ProcessGroupTermination,
    ProcessGroupWait,
)


@runtime_checkable
class ProcessGroupSupervisor(Protocol):
    """Keep the leader unreaped until every group member is contained."""

    def supervise(
        self,
        leader: OwnedProcessGroupLeader,
        wait: ProcessGroupWait,
    ) -> ProcessGroupSupervision:
        """Observe natural exit or timeout, contain the group, then reap."""
        ...

    def abort(self, leader: OwnedProcessGroupLeader) -> ProcessGroupTermination:
        """Immediately contain the group and reap its still-owned leader."""
        ...
