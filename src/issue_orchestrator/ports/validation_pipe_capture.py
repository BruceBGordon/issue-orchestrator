"""Behavior boundary for validation output and executor-handshake capture."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.contained_command import ContainedCommandOutputPolicy
from ..domain.validation_execution import (
    ValidationCommandDeadlineStatus,
    ValidationCommandOutput,
    ValidationExecutionDeadline,
)
from .process_group_supervisor import ProcessGroupInterruption
from .posix_pipe import PosixPipeReader
from .validation_output_journal import ValidationOutputJournal


@dataclass(frozen=True, slots=True)
class ValidationPipeCaptureResult:
    """Complete buffered output plus any resource-finalization failure."""

    output: ValidationCommandOutput
    failure: BaseException | None

    def __post_init__(self) -> None:
        if type(self.output) is not ValidationCommandOutput:
            raise ValueError(
                "ValidationPipeCaptureResult.output must be ValidationCommandOutput"
            )
        if self.failure is not None and not isinstance(self.failure, BaseException):
            raise ValueError(
                "ValidationPipeCaptureResult.failure must be None or BaseException"
            )


@runtime_checkable
class ValidationPipeCapture(ProcessGroupInterruption, Protocol):
    """Capture owner used while the process-group owner supervises a child."""

    @property
    def deadline_status(self) -> ValidationCommandDeadlineStatus:
        """Return the exact timeout state observed during capture."""
        ...

    def finalize(self) -> ValidationPipeCaptureResult:
        """Close every resource and return a non-raising typed result."""
        ...


@runtime_checkable
class ValidationPipeCaptureFactory(Protocol):
    """Create one all-or-nothing validation pipe capture owner."""

    def create(
        self,
        stdout: PosixPipeReader,
        stderr: PosixPipeReader,
        handshake_reader: PosixPipeReader,
        policy: ContainedCommandOutputPolicy,
        deadline: ValidationExecutionDeadline,
        started_at_monotonic: float,
        output_journal: ValidationOutputJournal,
    ) -> ValidationPipeCapture:
        """Return a ready owner or raise only after closing partial resources."""
        ...
