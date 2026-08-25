"""Behavior-level port for containing one persisted terminal session."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.process_group import ProcessIdentityObservation
from ..domain.terminal_session_termination import (
    TerminalSessionProcess,
    TerminalSessionStatus,
    TerminalSessionTerminationOutcome,
)


@runtime_checkable
class TerminalSessionTerminator(Protocol):
    """Contain the outer session and any executor guardian it owns."""

    def identify(self, process_id: int) -> ProcessIdentityObservation:
        """Observe one process before its identity is persisted."""
        ...

    def status(self, process: TerminalSessionProcess) -> TerminalSessionStatus:
        """Observe whether the exact persisted process group is still active."""
        ...

    def terminate(
        self,
        process: TerminalSessionProcess,
    ) -> TerminalSessionTerminationOutcome:
        """Return only after the complete terminal session is contained."""
        ...
