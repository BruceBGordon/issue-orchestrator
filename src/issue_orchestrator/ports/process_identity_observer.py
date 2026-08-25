"""Port for collision-resistant kernel process identity observation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.process_group import ProcessIdentityObservation


@runtime_checkable
class ProcessIdentityObserver(Protocol):
    """Observe a PID's exact kernel birth token and process group."""

    def observe_process(self, process_id: int) -> ProcessIdentityObservation:
        """Return a closed exact identity observation for one PID."""
        ...
