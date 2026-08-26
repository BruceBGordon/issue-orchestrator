# pyright: strict
"""Typed owner for durable and human executor deadline evidence."""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import NoReturn, Protocol, runtime_checkable

from ...control.executor_admission import QueuedExecutorWork
from ...domain.executor import (
    ExecutorBoundedDeadline,
    ExecutorDeadlineExceededError,
    ExecutorDeadlinePhase,
    ExecutorDeadlineReason,
)
from ...domain.independent_cleanup import (
    CleanupAction,
    IndependentCleanupPlan,
    raise_primary_with_cleanup,
)
from ._types import ExecutorWorkIdentity


@dataclass(frozen=True, slots=True)
class ExecutorDeadlineReport:
    """One exact admission or command deadline decision."""

    identity: ExecutorWorkIdentity
    work: QueuedExecutorWork
    deadline: ExecutorBoundedDeadline
    reason: ExecutorDeadlineReason
    phase: ExecutorDeadlinePhase
    elapsed_seconds: float

    def __post_init__(self) -> None:
        if type(self.identity) is not ExecutorWorkIdentity:
            raise ValueError("ExecutorDeadlineReport.identity must be typed")
        if type(self.work) is not QueuedExecutorWork:
            raise ValueError("ExecutorDeadlineReport.work must be typed")
        if type(self.deadline) is not ExecutorBoundedDeadline:
            raise ValueError("ExecutorDeadlineReport.deadline must be typed")
        if type(self.reason) is not ExecutorDeadlineReason:
            raise ValueError("ExecutorDeadlineReport.reason must be typed")
        if type(self.phase) is not ExecutorDeadlinePhase:
            raise ValueError("ExecutorDeadlineReport.phase must be typed")
        if (
            type(self.elapsed_seconds) is not float
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0.0
        ):
            raise ValueError(
                "ExecutorDeadlineReport.elapsed_seconds must be finite and non-negative"
            )


@runtime_checkable
class ExecutorAdmissionDeadlineEvents(Protocol):
    """Durable event seam required by admission deadline finalization."""

    def admission_deadline_exceeded(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        deadline: ExecutorBoundedDeadline,
        elapsed_seconds: float,
    ) -> None: ...


@runtime_checkable
class ExecutorDeadlineReporter(Protocol):
    """Human evidence seam for one typed deadline decision."""

    def deadline_exceeded(self, report: ExecutorDeadlineReport) -> None: ...


@runtime_checkable
class ExecutorDeadlineOwner(Protocol):
    """Own deadline publication and exact admission-error preservation."""

    def admission_exceeded(
        self,
        report: ExecutorDeadlineReport,
        error: ExecutorDeadlineExceededError,
    ) -> NoReturn: ...

    def report(self, report: ExecutorDeadlineReport) -> None: ...


def _require_admission_deadline_events(
    value: object,
) -> ExecutorAdmissionDeadlineEvents:
    if not isinstance(value, ExecutorAdmissionDeadlineEvents):
        raise ValueError("deadline events must implement their port")
    return value


def _require_deadline_reporter(value: object) -> ExecutorDeadlineReporter:
    if not isinstance(value, ExecutorDeadlineReporter):
        raise ValueError("deadline reporter must implement its port")
    return value


class StderrExecutorDeadlineReporter:
    """Production human-readable deadline reporter."""

    def deadline_exceeded(self, report: ExecutorDeadlineReport) -> None:
        print(
            "[executor] deadline-exceeded "
            f"work={report.identity.work_key.value} "
            f"group={report.work.fairness_group.value} "
            f"phase={report.phase.value} "
            f"reason={report.reason.value} "
            f"active_timeout={report.deadline.active_timeout_seconds:.3f}s "
            f"absolute_timeout={report.deadline.absolute_timeout_seconds:.3f}s",
            file=sys.stderr,
            flush=True,
        )


class DurableExecutorDeadlineOwner:
    """Attempt both admission evidence seams before preserving the primary."""

    def __init__(
        self,
        events: ExecutorAdmissionDeadlineEvents,
        reporter: ExecutorDeadlineReporter,
    ) -> None:
        self._events = _require_admission_deadline_events(events)
        self._reporter = _require_deadline_reporter(reporter)

    def admission_exceeded(
        self,
        report: ExecutorDeadlineReport,
        error: ExecutorDeadlineExceededError,
    ) -> NoReturn:
        if type(error) is not ExecutorDeadlineExceededError:
            raise ValueError("admission deadline error must be exact")
        evidence = IndependentCleanupPlan(
            (
                CleanupAction(
                    "publish executor admission deadline",
                    lambda: self._events.admission_deadline_exceeded(
                        report.identity,
                        report.work,
                        report.deadline,
                        report.elapsed_seconds,
                    ),
                ),
                CleanupAction(
                    "report executor admission deadline",
                    lambda: self._reporter.deadline_exceeded(report),
                ),
            )
        ).run()
        raise_primary_with_cleanup(
            "executor admission deadline and evidence failures",
            error,
            evidence,
        )
        raise AssertionError("deadline finalization must preserve its primary")

    def report(self, report: ExecutorDeadlineReport) -> None:
        self._reporter.deadline_exceeded(report)
