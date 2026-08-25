"""Port for one closed, streamed, process-group-contained shell command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.contained_command import (
    ContainedCommandOutputPipeClose,
    ContainedCommandResult,
    ContainedCommandStarted,
)
from ..domain.posix_process import PosixDescriptorMapping


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
            raise ValueError("ContainedShellCommand.working_directory must be absolute")


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
    """Interpret one joined raw line without owning its durable transport."""

    def observe_line(self, line: str) -> None:
        """Consume one line synchronously after process-group containment."""
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


@runtime_checkable
class ContainedCommandOutputReader(Protocol):
    """Minimal owned binary descriptor surface required by the capture pump."""

    def fileno(self) -> int: ...

    def close(self) -> None: ...


@runtime_checkable
class ContainedCommandOutputPipe(Protocol):
    """Own one parent reader and child stdout/stderr mappings."""

    @property
    def descriptor_mappings(self) -> tuple[PosixDescriptorMapping, ...]: ...

    def open_reader_after_launch(self) -> ContainedCommandOutputReader:
        """Close the child endpoint and transfer the parent reader."""
        ...

    def close(self) -> ContainedCommandOutputPipeClose:
        """Attempt every descriptor cleanup and return exact evidence."""
        ...


@runtime_checkable
class ContainedCommandOutputPipeFactory(Protocol):
    """Acquire one all-or-closed captured-command output pipe."""

    def create(self) -> ContainedCommandOutputPipe: ...
