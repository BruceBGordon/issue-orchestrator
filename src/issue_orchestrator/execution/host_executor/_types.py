# pyright: strict
"""Private typed records shared inside the host executor adapter."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ...control.executor_admission import (
    ExecutorAdmissionGrant,
    ExecutorResourceObservation,
)
from ...domain.executor import ExecutorCommandFinalizationFailure, ExecutorWorkKey


@dataclass(frozen=True, slots=True)
class ExecutorRepositoryIdentity:
    """Canonical Git common directory and a human-readable repository label."""

    common_directory: Path
    label: str

    def __post_init__(self) -> None:
        if not self.common_directory.is_absolute():
            raise ValueError(
                "ExecutorRepositoryIdentity.common_directory must be absolute"
            )
        if not self.label:
            raise ValueError("ExecutorRepositoryIdentity.label must not be empty")

    @property
    def key(self) -> str:
        """Stable key shared by every worktree of the repository."""
        return str(self.common_directory)


@dataclass(frozen=True, slots=True)
class ExecutorWorkIdentity:
    """Repository-scoped human work identity."""

    repository: ExecutorRepositoryIdentity
    work_key: ExecutorWorkKey


@dataclass(frozen=True, slots=True)
class RecordedExecutorObservation:
    """Resource observation plus command outcome and recording time."""

    resources: ExecutorResourceObservation
    exit_code: int
    recorded_at_unix: float

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError("RecordedExecutorObservation.exit_code must be an integer")
        if (
            not math.isfinite(self.recorded_at_unix)
            or self.recorded_at_unix <= 0
        ):
            raise ValueError(
                "RecordedExecutorObservation.recorded_at_unix must be finite and "
                "positive"
            )


@dataclass(frozen=True, slots=True)
class ExecutedExecutorCommand:
    """Private command outcome retained for learning and diagnostics."""

    exit_code: int
    admission_grant: ExecutorAdmissionGrant
    resources: ExecutorResourceObservation

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError("ExecutedExecutorCommand.exit_code must be an integer")
        if self.admission_grant.concurrency != self.resources.concurrency:
            raise ValueError(
                "ExecutedExecutorCommand grant and resource concurrency must match"
            )


@dataclass(frozen=True, slots=True)
class ExecutorCommandWithoutResourceObservation:
    """Exact command terminal result whose resource observation failed."""

    exit_code: int
    admission_grant: ExecutorAdmissionGrant
    cause: ExecutorCommandResourceObservationCause

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError(
                "ExecutorCommandWithoutResourceObservation.exit_code must be "
                "an integer"
            )
        if type(self.admission_grant) is not ExecutorAdmissionGrant:
            raise ValueError(
                "ExecutorCommandWithoutResourceObservation.admission_grant must "
                "be ExecutorAdmissionGrant"
            )
        if type(self.cause) not in (
            ExecutorCommandResourceObservationFailed,
            ExecutorCommandResourceObservationNotApplicable,
        ):
            raise ValueError(
                "ExecutorCommandWithoutResourceObservation.cause must be an "
                "ExecutorCommandResourceObservationCause"
            )


ExecutorCommandExecution = (
    ExecutedExecutorCommand | ExecutorCommandWithoutResourceObservation
)


class ExecutorResourceObservationOmissionReason(StrEnum):
    """Why command resource facts are intentionally unavailable."""

    DEADLINE = "deadline"
    INTERRUPTION = "interruption"


@dataclass(frozen=True, slots=True)
class ExecutorCommandResourceObservationFailed:
    """A required exact observation failed after command completion."""

    error: BaseException

    def __post_init__(self) -> None:
        _require_error(
            self.error,
            "ExecutorCommandResourceObservationFailed.error",
        )


@dataclass(frozen=True, slots=True)
class ExecutorCommandResourceObservationNotApplicable:
    """Containment ended work without a reap-attributable command observation."""

    reason: ExecutorResourceObservationOmissionReason

    def __post_init__(self) -> None:
        if type(self.reason) is not ExecutorResourceObservationOmissionReason:
            raise ValueError(
                "ExecutorCommandResourceObservationNotApplicable.reason must be "
                "ExecutorResourceObservationOmissionReason"
            )


ExecutorCommandResourceObservationCause = (
    ExecutorCommandResourceObservationFailed
    | ExecutorCommandResourceObservationNotApplicable
)


@dataclass(frozen=True, slots=True)
class FinalizableExecutorCommand:
    """Exact command outcome plus failures discovered before finalization."""

    command: ExecutorCommandExecution
    initial_failures: tuple[ExecutorCommandFinalizationFailure, ...]

    def __post_init__(self) -> None:
        if type(self.command) not in (
            ExecutedExecutorCommand,
            ExecutorCommandWithoutResourceObservation,
        ):
            raise ValueError(
                "FinalizableExecutorCommand.command must be an "
                "ExecutorCommandExecution"
            )
        if type(self.initial_failures) is not tuple or any(
            type(failure) is not ExecutorCommandFinalizationFailure
            for failure in self.initial_failures
        ):
            raise ValueError(
                "FinalizableExecutorCommand.initial_failures must contain only "
                "ExecutorCommandFinalizationFailure values"
            )


def _require_error(value: object, field_name: str) -> None:
    if not isinstance(value, BaseException):
        raise ValueError(f"{field_name} must be a BaseException")
