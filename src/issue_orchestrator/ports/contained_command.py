"""Port for one closed, streamed, process-group-contained shell command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.contained_command import (
    ContainedCommandResult,
    ContainedCommandStarted,
)


def _require_path(owner: str, field: str, value: object) -> None:
    if not isinstance(value, Path):
        raise ValueError(f"{owner}.{field} must be a Path")


@dataclass(frozen=True, slots=True)
class ContainedShellCommand:
    """Required invocation data for one locally contained shell command."""

    command: str
    working_directory: Path

    def __post_init__(self) -> None:
        if type(self.command) is not str or not self.command:
            raise ValueError("ContainedShellCommand.command must not be empty")
        _require_path(type(self).__name__, "working_directory", self.working_directory)
        if not self.working_directory.is_absolute():
            raise ValueError(
                "ContainedShellCommand.working_directory must be absolute"
            )


@runtime_checkable
class ContainedCommandOutput(Protocol):
    """Streaming output boundary kept separate from line interpretation."""

    def child_started(self, started: ContainedCommandStarted) -> None:
        """Observe the exact process-group leader after successful spawn."""
        ...

    def write_line(self, line: str) -> None:
        """Write one raw stdout/stderr line."""
        ...


@runtime_checkable
class ContainedCommandLineObserver(Protocol):
    """Interpret one raw line without owning its durable output transport."""

    def observe_line(self, line: str) -> None:
        """Consume one line; raising interrupts capture and triggers containment."""
        ...


@runtime_checkable
class ContainedCommandCapture(Protocol):
    """Deep owner for spawn, output pumping, containment, and terminal facts."""

    def capture(
        self,
        command: ContainedShellCommand,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> ContainedCommandResult:
        """Return one closed result for every operational subprocess outcome."""
        ...
