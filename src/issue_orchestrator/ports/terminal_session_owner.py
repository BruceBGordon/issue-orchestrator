"""Port for preparing one crash-recoverable terminal launch."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.terminal_session_termination import TerminalSessionOwnerCancellation


@runtime_checkable
class TerminalSessionLaunchLease(Protocol):
    """Parent-owned launch resources transferred to the terminal sentinel."""

    @property
    def command(self) -> tuple[str, ...]: ...

    @property
    def inherited_file_descriptors(self) -> tuple[int, ...]: ...

    def require_ready(self) -> None:
        """Prove ACTIVE publication and release parent descriptor copies."""
        ...

    def abandon_after_spawn_uncertainty(self) -> None:
        """Release parent copies without retiring possibly inherited ownership."""
        ...

    def retire_after_containment(self) -> None:
        """Retire only after the complete process group is contained."""
        ...


@runtime_checkable
class TerminalSessionOwner(Protocol):
    """Prepare durable ownership before spawning a terminal process."""

    def prepare(
        self,
        command: tuple[str, ...],
        cancellation: TerminalSessionOwnerCancellation,
    ) -> TerminalSessionLaunchLease:
        """Return one exact command and its parent-owned launch lease."""
        ...
