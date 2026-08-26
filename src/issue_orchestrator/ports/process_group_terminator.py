"""Port for terminating one explicitly owned subprocess group."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.process_group import OwnedProcessGroupLeader, ProcessGroupTermination


@runtime_checkable
class ProcessGroupTerminator(Protocol):
    """Terminate and reap a process group whose leader is still owned."""

    def terminate(
        self,
        leader: OwnedProcessGroupLeader,
    ) -> ProcessGroupTermination:
        """Stop the whole group before reaping its session leader."""
        ...
