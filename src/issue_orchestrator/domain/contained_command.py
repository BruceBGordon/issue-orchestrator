"""Closed terminal facts for one locally contained shell command."""

from __future__ import annotations

import math
from dataclasses import dataclass


def _require_exact(owner: str, field: str, value: object, expected: type) -> None:
    if type(value) is not expected:
        raise ValueError(f"{owner}.{field} must have exact type {expected.__name__}")


def _require_exception(owner: str, field: str, value: object) -> None:
    if not isinstance(value, BaseException):
        raise ValueError(f"{owner}.{field} must be an exception")


@dataclass(frozen=True, slots=True)
class ContainedCommandOutputPolicy:
    """Bound output-pump responsiveness after a command group is contained."""

    poll_interval_seconds: float
    shutdown_timeout_seconds: float
    final_drain_byte_limit: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("poll_interval_seconds", self.poll_interval_seconds),
            ("shutdown_timeout_seconds", self.shutdown_timeout_seconds),
        ):
            if type(value) is not float or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"ContainedCommandOutputPolicy.{field_name} must be "
                    "finite and positive"
                )
        if self.shutdown_timeout_seconds <= self.poll_interval_seconds:
            raise ValueError(
                "ContainedCommandOutputPolicy.shutdown_timeout_seconds must "
                "exceed poll_interval_seconds"
            )
        if (
            type(self.final_drain_byte_limit) is not int
            or self.final_drain_byte_limit < 1
        ):
            raise ValueError(
                "ContainedCommandOutputPolicy.final_drain_byte_limit must be "
                "a positive integer"
            )


@dataclass(frozen=True, slots=True)
class ContainedCommandFailure:
    """The exact exception that interrupted capture or containment."""

    error: BaseException

    def __post_init__(self) -> None:
        _require_exception(type(self).__name__, "error", self.error)

    @property
    def error_type(self) -> str:
        return type(self.error).__name__

    @property
    def error_repr(self) -> str:
        return repr(self.error)


@dataclass(frozen=True, slots=True)
class ContainedCommandMetrics:
    """Raw stdout volume observed before the terminal result closed."""

    line_count: int
    byte_count: int

    def __post_init__(self) -> None:
        owner = type(self).__name__
        if type(self.line_count) is not int or self.line_count < 0:
            raise ValueError(f"{owner}.line_count must be a non-negative integer")
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ValueError(f"{owner}.byte_count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ContainedCommandStarted:
    """Identity of the process-group leader created for a command."""

    process_id: int

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError(
                "ContainedCommandStarted.process_id must be an integer above 1"
            )


@dataclass(frozen=True, slots=True)
class ContainedCommandExited:
    """Known reaped status of the command's process-group leader."""

    process_id: int
    exit_code: int

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError(
                "ContainedCommandExited.process_id must be an integer above 1"
            )
        if type(self.exit_code) is not int:
            raise ValueError("ContainedCommandExited.exit_code must be an integer")


@dataclass(frozen=True, slots=True)
class ContainedCommandNotStarted:
    """The command failed before a process group existed."""


@dataclass(frozen=True, slots=True)
class ContainedCommandExitUnknown:
    """Containment failed, so no child exit status may be claimed."""

    process_id: int

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError(
                "ContainedCommandExitUnknown.process_id must be an integer above 1"
            )


ContainedCommandChild = (
    ContainedCommandExited | ContainedCommandNotStarted | ContainedCommandExitUnknown
)


@dataclass(frozen=True, slots=True)
class ContainedCommandSupervised:
    """Natural leader completion followed by whole-group containment."""


@dataclass(frozen=True, slots=True)
class ContainedCommandCaptureAborted:
    """Capture interruption followed by successful whole-group containment."""


@dataclass(frozen=True, slots=True)
class ContainedCommandCleanupNotStarted:
    """No process group existed, so containment was unnecessary."""


ContainedCommandCleanup = (
    ContainedCommandSupervised
    | ContainedCommandCaptureAborted
    | ContainedCommandCleanupNotStarted
)


@dataclass(frozen=True, slots=True)
class ContainedCommandCaptureSucceeded:
    """Stdout capture itself had not failed when containment failed."""


@dataclass(frozen=True, slots=True)
class ContainedCommandCaptureInterrupted:
    """Stdout capture failed before or while containment failed."""

    failure: ContainedCommandFailure

    def __post_init__(self) -> None:
        _require_exact(
            type(self).__name__,
            "failure",
            self.failure,
            ContainedCommandFailure,
        )


ContainedCommandCapture = (
    ContainedCommandCaptureSucceeded | ContainedCommandCaptureInterrupted
)


@dataclass(frozen=True, slots=True)
class ContainedCommandCompleted:
    """Closed normal result: known child status and contained process group."""

    child: ContainedCommandExited
    metrics: ContainedCommandMetrics

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_exact(owner, "child", self.child, ContainedCommandExited)
        _require_exact(owner, "metrics", self.metrics, ContainedCommandMetrics)


@dataclass(frozen=True, slots=True)
class ContainedCommandCaptureFailed:
    """Capture failed, but the process group has a closed terminal fact."""

    child: ContainedCommandExited | ContainedCommandNotStarted
    cleanup: ContainedCommandCleanup
    failure: ContainedCommandFailure
    metrics: ContainedCommandMetrics

    def __post_init__(self) -> None:
        owner = type(self).__name__
        if type(self.child) not in (ContainedCommandExited, ContainedCommandNotStarted):
            raise ValueError(f"{owner}.child must be exited or not-started")
        if type(self.cleanup) not in (
            ContainedCommandSupervised,
            ContainedCommandCaptureAborted,
            ContainedCommandCleanupNotStarted,
        ):
            raise ValueError(f"{owner}.cleanup must be a closed cleanup fact")
        if type(self.child) is ContainedCommandNotStarted:
            _require_exact(
                owner,
                "cleanup",
                self.cleanup,
                ContainedCommandCleanupNotStarted,
            )
        elif type(self.cleanup) is ContainedCommandCleanupNotStarted:
            raise ValueError(f"{owner} cannot omit cleanup for a started command")
        _require_exact(owner, "failure", self.failure, ContainedCommandFailure)
        _require_exact(owner, "metrics", self.metrics, ContainedCommandMetrics)


@dataclass(frozen=True, slots=True)
class ContainedCommandCleanupFailed:
    """Containment failed; preserve capture and cleanup evidence without guessing."""

    child: ContainedCommandExitUnknown
    capture: ContainedCommandCapture
    cleanup_failure: ContainedCommandFailure
    metrics: ContainedCommandMetrics

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_exact(owner, "child", self.child, ContainedCommandExitUnknown)
        if type(self.capture) not in (
            ContainedCommandCaptureSucceeded,
            ContainedCommandCaptureInterrupted,
        ):
            raise ValueError(f"{owner}.capture must be a typed capture fact")
        _require_exact(
            owner,
            "cleanup_failure",
            self.cleanup_failure,
            ContainedCommandFailure,
        )
        _require_exact(owner, "metrics", self.metrics, ContainedCommandMetrics)


ContainedCommandResult = (
    ContainedCommandCompleted
    | ContainedCommandCaptureFailed
    | ContainedCommandCleanupFailed
)


class ContainedCommandCleanupError(RuntimeError):
    """Raised after a cleanup-failed result has been durably recorded."""

    def __init__(self, result: ContainedCommandCleanupFailed) -> None:
        _require_exact(
            type(self).__name__,
            "result",
            result,
            ContainedCommandCleanupFailed,
        )
        self.result = result
        capture_detail = (
            "capture had succeeded"
            if type(result.capture) is ContainedCommandCaptureSucceeded
            else (
                "capture failed with "
                f"{result.capture.failure.error_type}: "
                f"{result.capture.failure.error_repr}"
            )
        )
        super().__init__(
            f"contained command cleanup failed after {capture_detail}; "
            f"cleanup={result.cleanup_failure.error_type}: "
            f"{result.cleanup_failure.error_repr}"
        )
