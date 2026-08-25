"""Behavior-level port for containing one persisted terminal session."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.terminal_session_termination import TerminalSessionProcess


@runtime_checkable
class TerminalSessionTerminator(Protocol):
    """Contain the outer session and any executor guardian it owns."""

    def terminate(self, process: TerminalSessionProcess) -> None:
        """Return only after the complete terminal session is contained."""
        ...
