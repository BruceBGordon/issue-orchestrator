"""Narrow system seam for signal-safe ``posix_spawn`` activation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessEnvironment,
    PosixProcessGroupMode,
    PosixProcessProgram,
)


@dataclass(frozen=True, slots=True)
class PosixSpawnPrimitiveRequest:
    """Complete low-level request after the child wrapper is selected."""

    program: PosixProcessProgram
    environment: PosixProcessEnvironment
    group_mode: PosixProcessGroupMode
    descriptor_mappings: tuple[PosixDescriptorMapping, ...]

    def __post_init__(self) -> None:
        if type(self.program) is not PosixProcessProgram:
            raise ValueError("PosixSpawnPrimitiveRequest.program must be typed")
        if type(self.environment) is not PosixProcessEnvironment:
            raise ValueError("PosixSpawnPrimitiveRequest.environment must be typed")
        if type(self.group_mode) is not PosixProcessGroupMode:
            raise ValueError("PosixSpawnPrimitiveRequest.group_mode must be typed")
        if type(self.descriptor_mappings) is not tuple or any(
            type(mapping) is not PosixDescriptorMapping
            for mapping in self.descriptor_mappings
        ):
            raise ValueError(
                "PosixSpawnPrimitiveRequest.descriptor_mappings must be typed"
            )


@dataclass(frozen=True, slots=True)
class PosixSpawnPrimitiveStarted:
    """The syscall returned and its exact PID survived mask restoration."""

    process_id: int

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError("PosixSpawnPrimitiveStarted.process_id must be above 1")


@dataclass(frozen=True, slots=True)
class PosixSpawnPrimitiveRejected:
    """No PID was returned by the spawn syscall."""

    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError("PosixSpawnPrimitiveRejected.error must be an exception")


@dataclass(frozen=True, slots=True)
class PosixSpawnPrimitiveIndeterminate:
    """A PID was returned but post-spawn parent finalization failed."""

    process_id: int
    error: BaseException

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError(
                "PosixSpawnPrimitiveIndeterminate.process_id must be above 1"
            )
        if not isinstance(self.error, BaseException):
            raise ValueError(
                "PosixSpawnPrimitiveIndeterminate.error must be an exception"
            )


PosixSpawnPrimitiveResult = (
    PosixSpawnPrimitiveStarted
    | PosixSpawnPrimitiveRejected
    | PosixSpawnPrimitiveIndeterminate
)


@runtime_checkable
class PosixSpawnPrimitive(Protocol):
    """Perform one atomic spawn while retaining all parent cleanup facts."""

    def start(self, request: PosixSpawnPrimitiveRequest) -> PosixSpawnPrimitiveResult:
        """Return one closed primitive activation outcome."""
        ...
