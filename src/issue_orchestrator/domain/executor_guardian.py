"""Typed lifecycle facts for the crash-resilient executor command guardian."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .executor import ExecutorDeadlineReason


def _require_exact(owner: str, field: str, value: object, expected: type) -> None:
    if type(value) is not expected:
        raise ValueError(f"{owner}.{field} must have exact type {expected.__name__}")


@dataclass(frozen=True, slots=True)
class ExecutorGuardianUnboundedBudget:
    """A command whose guardian waits for natural completion."""


@dataclass(frozen=True, slots=True)
class ExecutorGuardianBoundedBudget:
    """The exact post-admission duration and selected deadline reason."""

    timeout_seconds: float
    reason: ExecutorDeadlineReason

    def __post_init__(self) -> None:
        if (
            type(self.timeout_seconds) is not float
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0.0
        ):
            raise ValueError(
                "ExecutorGuardianBoundedBudget.timeout_seconds must be finite "
                "and positive"
            )
        _require_exact(
            type(self).__name__,
            "reason",
            self.reason,
            ExecutorDeadlineReason,
        )


ExecutorGuardianBudget = ExecutorGuardianUnboundedBudget | ExecutorGuardianBoundedBudget


@dataclass(frozen=True, slots=True)
class ExecutorGuardianTerminationPolicy:
    """Courtesy interval before an overdue command group is force-killed."""

    graceful_shutdown_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.graceful_shutdown_seconds) is not float
            or not math.isfinite(self.graceful_shutdown_seconds)
            or self.graceful_shutdown_seconds <= 0.0
        ):
            raise ValueError(
                "ExecutorGuardianTerminationPolicy.graceful_shutdown_seconds "
                "must be finite and positive"
            )


@dataclass(frozen=True, slots=True)
class ExecutorGuardianCommandResourceUsage:
    """Resources attributable to one naturally completed guarded command."""

    wall_seconds: float
    cpu_seconds: float
    guardian_process_lifetime_children_max_rss_bytes: int
    input_blocks: int
    output_blocks: int

    def __post_init__(self) -> None:
        if (
            type(self.wall_seconds) is not float
            or not math.isfinite(self.wall_seconds)
            or self.wall_seconds <= 0.0
        ):
            raise ValueError(
                "ExecutorGuardianCommandResourceUsage.wall_seconds must be "
                "finite and positive"
            )
        if (
            type(self.cpu_seconds) is not float
            or not math.isfinite(self.cpu_seconds)
            or self.cpu_seconds < 0.0
        ):
            raise ValueError(
                "ExecutorGuardianCommandResourceUsage.cpu_seconds must be "
                "finite and non-negative"
            )
        for field_name, value in (
            (
                "guardian_process_lifetime_children_max_rss_bytes",
                self.guardian_process_lifetime_children_max_rss_bytes,
            ),
            ("input_blocks", self.input_blocks),
            ("output_blocks", self.output_blocks),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"ExecutorGuardianCommandResourceUsage.{field_name} must "
                    "be non-negative"
                )


@dataclass(frozen=True, slots=True)
class ExecutorGuardianResourceObservationFailed:
    """Exact command completion whose isolated resource observation failed."""

    error_type: str
    error_repr: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("error_type", self.error_type),
            ("error_repr", self.error_repr),
        ):
            if type(value) is not str or not value:
                raise ValueError(
                    "ExecutorGuardianResourceObservationFailed."
                    f"{field_name} must not be empty"
                )


ExecutorGuardianCommandResources = (
    ExecutorGuardianCommandResourceUsage | ExecutorGuardianResourceObservationFailed
)


@dataclass(frozen=True, slots=True)
class ExecutorGuardianCommandCompleted:
    """The opaque command exited before its guardian budget expired."""

    exit_code: int
    resources: ExecutorGuardianCommandResources

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError(
                "ExecutorGuardianCommandCompleted.exit_code must be an integer"
            )
        if type(self.resources) not in (
            ExecutorGuardianCommandResourceUsage,
            ExecutorGuardianResourceObservationFailed,
        ):
            raise ValueError(
                "ExecutorGuardianCommandCompleted.resources must be an "
                "ExecutorGuardianCommandResources"
            )


@dataclass(frozen=True, slots=True)
class ExecutorGuardianCommandInterrupted:
    """The outer owner interrupted and contained a started guardian group."""

    signal_number: int

    def __post_init__(self) -> None:
        if type(self.signal_number) is not int or self.signal_number <= 0:
            raise ValueError(
                "ExecutorGuardianCommandInterrupted.signal_number must be positive"
            )


@dataclass(frozen=True, slots=True)
class ExecutorGuardianCommandTimedOut:
    """The guardian selected and enforced a post-admission deadline."""

    reason: ExecutorDeadlineReason

    def __post_init__(self) -> None:
        _require_exact(
            type(self).__name__,
            "reason",
            self.reason,
            ExecutorDeadlineReason,
        )


@dataclass(frozen=True, slots=True)
class ExecutorGuardianCommandStartFailed:
    """The guardian lived, but the opaque command could not be spawned."""

    error_type: str
    error_repr: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("error_type", self.error_type),
            ("error_repr", self.error_repr),
        ):
            if type(value) is not str or not value:
                raise ValueError(
                    f"ExecutorGuardianCommandStartFailed.{field_name} must not be empty"
                )


@dataclass(frozen=True, slots=True)
class ExecutorGuardianInternalFailed:
    """The guardian caught an internal failure after accepting its request."""

    error_type: str
    error_repr: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("error_type", self.error_type),
            ("error_repr", self.error_repr),
        ):
            if type(value) is not str or not value:
                raise ValueError(
                    f"ExecutorGuardianInternalFailed.{field_name} must not be empty"
                )


ExecutorGuardianTerminal = (
    ExecutorGuardianCommandCompleted
    | ExecutorGuardianCommandInterrupted
    | ExecutorGuardianCommandTimedOut
    | ExecutorGuardianCommandStartFailed
    | ExecutorGuardianInternalFailed
)


@dataclass(frozen=True, slots=True)
class ExecutorGuardianPostContainmentFailure:
    """One failed evidence seam after the guardian group was contained."""

    attempt_name: str
    error: BaseException

    def __post_init__(self) -> None:
        if type(self.attempt_name) is not str or not self.attempt_name:
            raise ValueError(
                "ExecutorGuardianPostContainmentFailure.attempt_name must not "
                "be empty"
            )
        if not isinstance(self.error, BaseException):
            raise ValueError(
                "ExecutorGuardianPostContainmentFailure.error must be a "
                "BaseException"
            )


class ExecutorGuardianPostContainmentError(RuntimeError):
    """Carry an exact guardian terminal beside post-containment failures."""

    def __init__(
        self,
        terminal: ExecutorGuardianTerminal,
        failures: tuple[ExecutorGuardianPostContainmentFailure, ...],
    ) -> None:
        if type(terminal) not in (
            ExecutorGuardianCommandCompleted,
            ExecutorGuardianCommandInterrupted,
            ExecutorGuardianCommandTimedOut,
            ExecutorGuardianCommandStartFailed,
            ExecutorGuardianInternalFailed,
        ):
            raise ValueError(
                "ExecutorGuardianPostContainmentError.terminal must be an "
                "ExecutorGuardianTerminal"
            )
        if type(failures) is not tuple or not failures or any(
            type(failure) is not ExecutorGuardianPostContainmentFailure
            for failure in failures
        ):
            raise ValueError(
                "ExecutorGuardianPostContainmentError.failures must contain "
                "ExecutorGuardianPostContainmentFailure values"
            )
        self.terminal = terminal
        self.failures = failures
        attempts = ", ".join(failure.attempt_name for failure in failures)
        super().__init__(
            "executor guardian post-containment evidence failed: " + attempts
        )


class ExecutorGuardianCommandStartError(RuntimeError):
    """Raised after typed command-start evidence reaches the outer executor."""

    def __init__(self, terminal: ExecutorGuardianCommandStartFailed) -> None:
        _require_exact(
            type(self).__name__,
            "terminal",
            terminal,
            ExecutorGuardianCommandStartFailed,
        )
        self.terminal = terminal
        super().__init__(
            "executor guardian could not start command: "
            f"{terminal.error_type}: {terminal.error_repr}"
        )


class ExecutorGuardianInternalError(RuntimeError):
    """Raised after a guardian reports an internal lifecycle failure."""

    def __init__(self, terminal: ExecutorGuardianInternalFailed) -> None:
        _require_exact(
            type(self).__name__,
            "terminal",
            terminal,
            ExecutorGuardianInternalFailed,
        )
        self.terminal = terminal
        super().__init__(
            "executor guardian failed internally: "
            f"{terminal.error_type}: {terminal.error_repr}"
        )


class ExecutorGuardianResourceObservationError(RuntimeError):
    """Raised after exact completion survives guardian observation failure."""

    def __init__(self, failure: ExecutorGuardianResourceObservationFailed) -> None:
        _require_exact(
            type(self).__name__,
            "failure",
            failure,
            ExecutorGuardianResourceObservationFailed,
        )
        self.failure = failure
        super().__init__(
            "executor guardian could not observe completed-command resources: "
            f"{failure.error_type}: {failure.error_repr}"
        )
