"""Port for coordinating all self-containment owners of a terminal session."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.terminal_session_termination import (
    TerminalSessionContainmentReport,
    TerminalSessionProcess,
    UnregisteredTerminalSessionOwnership,
)


@runtime_checkable
class TerminalSessionContainmentOwner(Protocol):
    """Contain outer and nested executor groups through stable endpoints."""

    def contain(
        self,
        process: TerminalSessionProcess,
    ) -> TerminalSessionContainmentReport:
        """Return only after every current process-group owner releases."""
        ...

    def contain_unregistered(
        self,
        ownership: UnregisteredTerminalSessionOwnership,
    ) -> TerminalSessionContainmentReport:
        """Contain durable owners published before PID registry commit."""
        ...
