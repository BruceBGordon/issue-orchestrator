"""Typed deadline contract for one nested validation execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import math
from pathlib import Path
from types import MappingProxyType

from .executor import ExecutorBoundedDeadline
from .process_group import (
    ProcessGroupCompleted,
    ProcessGroupInterrupted,
    ProcessGroupSupervision,
    ProcessGroupTimedOut,
)


_OUTER_CONTAINMENT_MARGIN_SECONDS = 30


@dataclass(frozen=True, slots=True)
class ValidationGuardianClock:
    """Injected monotonic clock for one absolute guardian activation budget."""

    monotonic_now: Callable[[], float]

    def __post_init__(self) -> None:
        if not callable(self.monotonic_now):
            raise ValueError("ValidationGuardianClock.monotonic_now must be callable")


@dataclass(frozen=True, slots=True)
class ValidationDeadlineObservationClock:
    """Injected monotonic clock for authoritative validation deadline decisions."""

    monotonic_now: Callable[[], float]

    def __post_init__(self) -> None:
        if not callable(self.monotonic_now):
            raise ValueError(
                "ValidationDeadlineObservationClock.monotonic_now must be callable"
            )


@dataclass(frozen=True, slots=True)
class ValidationExecutionDeadline:
    """Queue-aware executor budget plus its outer containment watchdog."""

    executor_deadline: ExecutorBoundedDeadline
    outer_timeout_seconds: float

    def __post_init__(self) -> None:
        if type(self.executor_deadline) is not ExecutorBoundedDeadline:
            raise ValueError(
                "ValidationExecutionDeadline.executor_deadline must be "
                "ExecutorBoundedDeadline"
            )
        if type(self.outer_timeout_seconds) is not float or not math.isfinite(
            self.outer_timeout_seconds
        ):
            raise ValueError(
                "ValidationExecutionDeadline.outer_timeout_seconds must be finite"
            )
        if (
            self.outer_timeout_seconds
            <= self.executor_deadline.absolute_timeout_seconds
        ):
            raise ValueError(
                "ValidationExecutionDeadline.outer_timeout_seconds must exceed "
                "the nested absolute deadline"
            )

    @classmethod
    def for_active_timeout(
        cls,
        active_timeout_seconds: int | float,
    ) -> ValidationExecutionDeadline:
        """Give queue admission an equal budget without consuming active work."""
        if (
            type(active_timeout_seconds) not in (int, float)
            or not math.isfinite(active_timeout_seconds)
            or active_timeout_seconds <= 0
        ):
            raise ValueError("validation active timeout must be finite and positive")
        active_seconds = float(active_timeout_seconds)
        absolute_timeout_seconds = active_seconds * 2
        return cls(
            executor_deadline=ExecutorBoundedDeadline(
                active_timeout_seconds=active_seconds,
                absolute_timeout_seconds=float(absolute_timeout_seconds),
            ),
            outer_timeout_seconds=(
                absolute_timeout_seconds + float(_OUTER_CONTAINMENT_MARGIN_SECONDS)
            ),
        )


@dataclass(frozen=True, slots=True)
class ValidationCommandOutputCapture:
    """Durable stream journals plus the bounded diagnostic tail to retain."""

    stdout_path: Path
    stderr_path: Path
    retained_tail_bytes: int

    def __post_init__(self) -> None:
        for field_name, path in (
            ("stdout_path", self.stdout_path),
            ("stderr_path", self.stderr_path),
        ):
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError(
                    f"ValidationCommandOutputCapture.{field_name} must be "
                    "an absolute Path"
                )
        if self.stdout_path == self.stderr_path:
            raise ValueError("validation stdout and stderr journals must differ")
        if type(self.retained_tail_bytes) is not int or self.retained_tail_bytes <= 0:
            raise ValueError(
                "ValidationCommandOutputCapture.retained_tail_bytes must be positive"
            )


@dataclass(frozen=True, slots=True)
class ContainedValidationCommand:
    """Complete command request whose process tree must be closed on return."""

    command: str
    working_directory: Path
    environment: Mapping[str, str]
    deadline: ValidationExecutionDeadline
    output_capture: ValidationCommandOutputCapture

    def __post_init__(self) -> None:
        if type(self.command) is not str or not self.command:
            raise ValueError("ContainedValidationCommand.command must not be empty")
        if not self.working_directory.is_absolute():
            raise ValueError(
                "ContainedValidationCommand.working_directory must be absolute"
            )
        environment = dict(self.environment)
        if any(
            type(key) is not str
            or not key
            or "=" in key
            or "\0" in key
            or type(value) is not str
            or "\0" in value
            for key, value in environment.items()
        ):
            raise ValueError(
                "ContainedValidationCommand.environment must contain valid "
                "process strings"
            )
        object.__setattr__(self, "environment", MappingProxyType(environment))
        if type(self.deadline) is not ValidationExecutionDeadline:
            raise ValueError(
                "ContainedValidationCommand.deadline must be "
                "ValidationExecutionDeadline"
            )
        if type(self.output_capture) is not ValidationCommandOutputCapture:
            raise ValueError(
                "ContainedValidationCommand.output_capture must be "
                "ValidationCommandOutputCapture"
            )


@dataclass(frozen=True, slots=True)
class ValidationCommandExited:
    """The process-group leader was reaped with an exact exit status."""

    process_id: int
    exit_code: int

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError("ValidationCommandExited.process_id must be above 1")
        if type(self.exit_code) is not int:
            raise ValueError("ValidationCommandExited.exit_code must be int")


@dataclass(frozen=True, slots=True)
class ValidationCommandNotStarted:
    """No process group existed when launch failed."""

    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError(
                "ValidationCommandNotStarted.error must be a BaseException"
            )


@dataclass(frozen=True, slots=True)
class ValidationCommandExitUnknown:
    """Containment failed, so no leader exit status may be claimed."""

    process_id: int

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError("ValidationCommandExitUnknown.process_id must be above 1")


ValidationCommandChild = (
    ValidationCommandExited | ValidationCommandNotStarted | ValidationCommandExitUnknown
)


@dataclass(frozen=True, slots=True)
class ValidationCommandCompleted:
    """Natural leader completion followed by whole-group containment."""


class ValidationCommandTimeoutPhase(StrEnum):
    """Clock whose expiry caused validation process-tree containment."""

    ACTIVE = "active"
    OUTER = "outer"


@dataclass(frozen=True, slots=True)
class ValidationCommandDeadlinePending:
    """Neither validation clock has expired."""


@dataclass(frozen=True, slots=True)
class ValidationCommandDeadlineExceeded:
    """One exact validation clock has expired."""

    phase: ValidationCommandTimeoutPhase

    def __post_init__(self) -> None:
        if type(self.phase) is not ValidationCommandTimeoutPhase:
            raise ValueError(
                "ValidationCommandDeadlineExceeded.phase must be "
                "ValidationCommandTimeoutPhase"
            )


ValidationCommandDeadlineStatus = (
    ValidationCommandDeadlinePending | ValidationCommandDeadlineExceeded
)


@dataclass(frozen=True, slots=True)
class _ValidationCommandBeforeExecutorAcknowledgement:
    started_at_monotonic: float


@dataclass(frozen=True, slots=True)
class _ValidationCommandAfterExecutorAcknowledgement:
    acknowledged_at_monotonic: float


_ValidationCommandDeadlineClock = (
    _ValidationCommandBeforeExecutorAcknowledgement
    | _ValidationCommandAfterExecutorAcknowledgement
)


class ValidationCommandDeadlineTracker:
    """Own active-to-outer clock transfer at one exact executor acknowledgement."""

    def __init__(
        self,
        deadline: ValidationExecutionDeadline,
        started_at_monotonic: float,
    ) -> None:
        if type(deadline) is not ValidationExecutionDeadline:
            raise ValueError(
                "ValidationCommandDeadlineTracker.deadline must be "
                "ValidationExecutionDeadline"
            )
        _require_monotonic_time(started_at_monotonic, "validation start")
        self._deadline = deadline
        self._clock: _ValidationCommandDeadlineClock = (
            _ValidationCommandBeforeExecutorAcknowledgement(started_at_monotonic)
        )

    def acknowledge_executor(self, acknowledged_at_monotonic: float) -> None:
        """Transfer clocks only when the executor acknowledged before active expiry."""
        _require_monotonic_time(
            acknowledged_at_monotonic,
            "executor acknowledgement",
        )
        clock = self._clock
        if type(clock) is _ValidationCommandAfterExecutorAcknowledgement:
            return
        if type(clock) is not _ValidationCommandBeforeExecutorAcknowledgement:
            raise AssertionError("validation deadline clock is a closed union")
        if acknowledged_at_monotonic < clock.started_at_monotonic:
            raise ValueError("executor acknowledgement cannot precede validation start")
        if (
            acknowledged_at_monotonic - clock.started_at_monotonic
            >= self._deadline.executor_deadline.active_timeout_seconds
        ):
            return
        self._clock = _ValidationCommandAfterExecutorAcknowledgement(
            acknowledged_at_monotonic
        )

    def status(self, observed_at_monotonic: float) -> ValidationCommandDeadlineStatus:
        """Return the exact clock phase expired at one monotonic observation."""
        _require_monotonic_time(observed_at_monotonic, "deadline observation")
        clock = self._clock
        if type(clock) is _ValidationCommandBeforeExecutorAcknowledgement:
            if observed_at_monotonic < clock.started_at_monotonic:
                raise ValueError("deadline observation cannot precede validation start")
            if (
                observed_at_monotonic - clock.started_at_monotonic
                >= self._deadline.executor_deadline.active_timeout_seconds
            ):
                return ValidationCommandDeadlineExceeded(
                    ValidationCommandTimeoutPhase.ACTIVE
                )
            return ValidationCommandDeadlinePending()
        if type(clock) is _ValidationCommandAfterExecutorAcknowledgement:
            if observed_at_monotonic < clock.acknowledged_at_monotonic:
                raise ValueError(
                    "deadline observation cannot precede executor acknowledgement"
                )
            if (
                observed_at_monotonic - clock.acknowledged_at_monotonic
                >= self._deadline.outer_timeout_seconds
            ):
                return ValidationCommandDeadlineExceeded(
                    ValidationCommandTimeoutPhase.OUTER
                )
            return ValidationCommandDeadlinePending()
        raise AssertionError("validation deadline clock is a closed union")


def _require_monotonic_time(value: float, field_name: str) -> None:
    if type(value) is not float or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be a positive finite monotonic float")


@dataclass(frozen=True, slots=True)
class ValidationCommandTimedOut:
    """A typed validation deadline caused whole-group containment."""

    phase: ValidationCommandTimeoutPhase

    def __post_init__(self) -> None:
        if type(self.phase) is not ValidationCommandTimeoutPhase:
            raise ValueError(
                "ValidationCommandTimedOut.phase must be ValidationCommandTimeoutPhase"
            )


@dataclass(frozen=True, slots=True)
class ValidationCommandCleanupNotStarted:
    """No process group existed, so no cleanup was required."""


@dataclass(frozen=True, slots=True)
class ValidationCommandCleanupFailed:
    """Process containment, reaping, or output closure failed."""

    error: BaseException

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise ValueError(
                "ValidationCommandCleanupFailed.error must be an exception"
            )


@dataclass(frozen=True, slots=True)
class ValidationCommandTimedOutCleanupFailed:
    """A typed deadline expired and subsequent cleanup diagnostics failed."""

    phase: ValidationCommandTimeoutPhase
    error: BaseException

    def __post_init__(self) -> None:
        if type(self.phase) is not ValidationCommandTimeoutPhase:
            raise ValueError(
                "ValidationCommandTimedOutCleanupFailed.phase must be typed"
            )
        if not isinstance(self.error, BaseException):
            raise ValueError(
                "ValidationCommandTimedOutCleanupFailed.error must be an exception"
            )


ValidationCommandCleanup = (
    ValidationCommandCompleted
    | ValidationCommandTimedOut
    | ValidationCommandCleanupNotStarted
    | ValidationCommandCleanupFailed
    | ValidationCommandTimedOutCleanupFailed
)


def validation_cleanup_from_supervision(
    supervision: ProcessGroupSupervision,
    deadline_status: ValidationCommandDeadlineStatus,
) -> ValidationCommandCleanup:
    """Map the closed process-group result to validation cleanup evidence."""
    cleanup: ValidationCommandCleanup
    if type(supervision) is ProcessGroupCompleted:
        if type(deadline_status) is not ValidationCommandDeadlinePending:
            raise AssertionError("completed validation cannot have a timeout phase")
        cleanup = ValidationCommandCompleted()
    elif type(supervision) is ProcessGroupTimedOut:
        raise AssertionError("validation supervision uses cooperative typed deadlines")
    elif type(supervision) is ProcessGroupInterrupted:
        if type(deadline_status) is not ValidationCommandDeadlineExceeded:
            cleanup = ValidationCommandCleanupFailed(
                AssertionError("validation interruption requires a timeout phase")
            )
        else:
            cleanup = ValidationCommandTimedOut(deadline_status.phase)
    else:
        raise AssertionError("process-group supervision is a closed union")
    courtesy_failure = supervision.termination.courtesy_failure()
    if courtesy_failure is None:
        return cleanup
    return validation_cleanup_with_failure(
        cleanup,
        courtesy_failure.error,
        "validation deadline and courtesy shutdown observation both failed",
    )


def validation_cleanup_with_failure(
    cleanup: ValidationCommandCleanup,
    error: BaseException,
    message: str,
) -> ValidationCommandCleanup:
    """Add cleanup failure evidence without discarding a typed timeout fact."""
    if not isinstance(error, BaseException):
        raise ValueError("validation cleanup failure must be an exception")
    if type(message) is not str or not message:
        raise ValueError("validation cleanup failure message must not be empty")
    if type(cleanup) is ValidationCommandCompleted:
        return ValidationCommandCleanupFailed(error)
    if type(cleanup) is ValidationCommandTimedOut:
        return ValidationCommandTimedOutCleanupFailed(cleanup.phase, error)
    if type(cleanup) is ValidationCommandCleanupFailed:
        return ValidationCommandCleanupFailed(
            BaseExceptionGroup(message, (cleanup.error, error))
        )
    if type(cleanup) is ValidationCommandTimedOutCleanupFailed:
        return ValidationCommandTimedOutCleanupFailed(
            cleanup.phase,
            BaseExceptionGroup(message, (cleanup.error, error)),
        )
    if type(cleanup) is ValidationCommandCleanupNotStarted:
        raise ValueError("an unstarted validation cannot acquire cleanup failure")
    raise AssertionError("validation cleanup is a closed union")


@dataclass(frozen=True, slots=True)
class ValidationCommandOutput:
    """Captured standard streams decoded for durable validation evidence."""

    stdout: str
    stderr: str

    def __post_init__(self) -> None:
        if type(self.stdout) is not str or type(self.stderr) is not str:
            raise ValueError("ValidationCommandOutput streams must be text")


@dataclass(frozen=True, slots=True)
class ValidationCommandEvidence:
    """Control-ready validation result with complete diagnostic text."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError("ValidationCommandEvidence.exit_code must be int")
        if type(self.stdout) is not str or type(self.stderr) is not str:
            raise ValueError("ValidationCommandEvidence streams must be text")
        if type(self.timed_out) is not bool:
            raise ValueError("ValidationCommandEvidence.timed_out must be bool")


@dataclass(frozen=True, slots=True)
class ValidationCommandExecution:
    """Closed child, cleanup, and output facts for one validation command."""

    child: ValidationCommandChild
    cleanup: ValidationCommandCleanup
    output: ValidationCommandOutput

    def __post_init__(self) -> None:
        if type(self.child) not in (
            ValidationCommandExited,
            ValidationCommandNotStarted,
            ValidationCommandExitUnknown,
        ):
            raise ValueError("ValidationCommandExecution.child is not closed")
        if type(self.cleanup) not in (
            ValidationCommandCompleted,
            ValidationCommandTimedOut,
            ValidationCommandCleanupNotStarted,
            ValidationCommandCleanupFailed,
            ValidationCommandTimedOutCleanupFailed,
        ):
            raise ValueError("ValidationCommandExecution.cleanup is not closed")
        if type(self.output) is not ValidationCommandOutput:
            raise ValueError(
                "ValidationCommandExecution.output must be ValidationCommandOutput"
            )
        if type(self.child) is ValidationCommandNotStarted:
            if type(self.cleanup) is not ValidationCommandCleanupNotStarted:
                raise ValueError("an unstarted validation cannot require cleanup")
        elif type(self.cleanup) is ValidationCommandCleanupNotStarted:
            raise ValueError("a started validation must report cleanup")

    @property
    def exit_code(self) -> int:
        if type(self.cleanup) in (
            ValidationCommandTimedOut,
            ValidationCommandTimedOutCleanupFailed,
            ValidationCommandCleanupFailed,
            ValidationCommandCleanupNotStarted,
        ):
            return -1
        if type(self.child) is not ValidationCommandExited:
            raise AssertionError("completed validation must have a reaped child")
        return self.child.exit_code

    @property
    def timed_out(self) -> bool:
        return type(self.cleanup) in (
            ValidationCommandTimedOut,
            ValidationCommandTimedOutCleanupFailed,
        )

    @property
    def timeout_phase(self) -> ValidationCommandTimeoutPhase:
        """Return the exact expired clock or reject a non-timeout caller."""
        if type(self.cleanup) not in (
            ValidationCommandTimedOut,
            ValidationCommandTimedOutCleanupFailed,
        ):
            raise AssertionError("non-timeout validation has no timeout phase")
        return self.cleanup.phase

    def evidence(
        self, deadline: ValidationExecutionDeadline
    ) -> ValidationCommandEvidence:
        """Render every typed launch/cleanup fact into durable diagnostics."""
        if type(deadline) is not ValidationExecutionDeadline:
            raise ValueError(
                "validation evidence deadline must be ValidationExecutionDeadline"
            )
        stderr = self.output.stderr
        if type(self.child) is ValidationCommandNotStarted:
            stderr += f"\n\n[VALIDATION START FAILED: {self.child.error!r}]"
        if type(self.cleanup) in (
            ValidationCommandCleanupFailed,
            ValidationCommandTimedOutCleanupFailed,
        ):
            stderr += (
                f"\n\n[VALIDATION PROCESS-TREE CLEANUP FAILED: {self.cleanup.error!r}]"
            )
        if self.timed_out:
            timeout_seconds = (
                deadline.executor_deadline.active_timeout_seconds
                if self.timeout_phase is ValidationCommandTimeoutPhase.ACTIVE
                else deadline.outer_timeout_seconds
            )
            stderr += (
                "\n\n[VALIDATION TIMEOUT "
                f"phase={self.timeout_phase.value} after {timeout_seconds}s]"
            )
        return ValidationCommandEvidence(
            exit_code=self.exit_code,
            stdout=self.output.stdout,
            stderr=stderr,
            timed_out=self.timed_out,
        )
