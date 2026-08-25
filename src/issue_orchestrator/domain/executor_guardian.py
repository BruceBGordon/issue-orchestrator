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
class ExecutorGuardianCommandCompleted:
    """The opaque command exited before its guardian budget expired."""

    exit_code: int

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError(
                "ExecutorGuardianCommandCompleted.exit_code must be an integer"
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
    | ExecutorGuardianCommandTimedOut
    | ExecutorGuardianCommandStartFailed
    | ExecutorGuardianInternalFailed
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
