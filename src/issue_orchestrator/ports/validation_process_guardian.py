# pyright: strict
"""Port for crash-resilient validation process-group activation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessEnvironment,
    PosixProcessProgram,
)
from .posix_process import (
    PosixProcessExecRejected,
    PosixProcessHandle,
    PosixProcessLaunchRecovered,
    PosixProcessLaunchRecoveryFailed,
    PosixProcessLaunchRejected,
)


@runtime_checkable
class ValidationProcessParentLifetime(Protocol):
    """Parent-held lease whose closure triggers in-group containment."""

    def close(self) -> None:
        """Release the parent lifetime after containment is independently proven."""
        ...


@dataclass(frozen=True, slots=True)
class ValidationProcessGuardianStarted:
    """A process-group leader protected by a parent-lifetime sentinel."""

    process: PosixProcessHandle
    parent_lifetime: ValidationProcessParentLifetime

    def __post_init__(self) -> None:
        if not isinstance(self.process, PosixProcessHandle):
            raise ValueError("guarded validation process must implement its port")
        if not isinstance(self.parent_lifetime, ValidationProcessParentLifetime):
            raise ValueError("guarded validation parent lifetime must implement its port")


ValidationProcessGuardianLaunch = (
    ValidationProcessGuardianStarted
    | PosixProcessLaunchRejected
    | PosixProcessExecRejected
    | PosixProcessLaunchRecovered
    | PosixProcessLaunchRecoveryFailed
)


@runtime_checkable
class ValidationProcessGuardian(Protocol):
    """Launch one validation command behind a crash-containment sentinel."""

    def launch(
        self,
        program: PosixProcessProgram,
        working_directory: Path,
        environment: PosixProcessEnvironment,
        descriptor_mappings: tuple[PosixDescriptorMapping, ...],
    ) -> ValidationProcessGuardianLaunch:
        """Return a closed launch fact; started work retains a lifetime lease."""
        ...
