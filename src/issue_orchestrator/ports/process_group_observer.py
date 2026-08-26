"""Port for portable process and process-group identity observations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.process_group import (
    ProcessGroupObservation,
    ProcessBirthIdentity,
    ProcessIdentityObservation,
    ProcessSessionObservation,
)


@runtime_checkable
class ProcessGroupObserver(Protocol):
    """Observe process birth and executable group membership without signalling."""

    def observe_process(self, process_id: int) -> ProcessIdentityObservation:
        """Observe one PID and its process-group membership."""
        ...

    def observe_group(self, process_group_id: int) -> ProcessGroupObservation:
        """Classify one group as absent, zombies-only, executable, or denied."""
        ...

    def observe_session(
        self,
        process_id: int,
        expected_birth_identity: ProcessBirthIdentity,
    ) -> ProcessSessionObservation:
        """Observe the leader identity and group as one owner-level decision fact."""
        ...

    def observe_session_group_ids(self, session_id: int) -> tuple[int, ...]:
        """Enumerate the live process-group ids inside one session."""
        ...
