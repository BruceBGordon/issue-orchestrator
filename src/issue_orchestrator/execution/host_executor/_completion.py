# pyright: strict
"""Deep owner for post-containment executor command finalization."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ...control.executor_admission import (
    ExecutorAdmissionGrant,
    ExecutorLearnedDemand,
    ExecutorResourceObservation,
    ExecutorWorkDemandEstimator,
    QueuedExecutorWork,
)
from ...domain.executor import (
    ExecutorCommandFinalizationError,
    ExecutorCommandFinalizationFailure,
    ExecutorConcurrencyGrant,
    ExecutorRunResult,
)
from ._history import ExecutorWorkHistoryStore
from ._host_observation import observe_host_load
from ._host_observation import ExecutorHostLoadObservation
from ._journal import ExecutorEventStore
from ._types import (
    ExecutedExecutorCommand,
    ExecutorWorkIdentity,
    RecordedExecutorObservation,
)


@dataclass(frozen=True, slots=True)
class ExecutorCommandCompletion:
    """All authoritative facts available after command containment."""

    identity: ExecutorWorkIdentity
    work: QueuedExecutorWork
    command: ExecutedExecutorCommand
    previous_demand: ExecutorLearnedDemand
    aggressiveness_percent: int

    def __post_init__(self) -> None:
        if type(self.identity) is not ExecutorWorkIdentity:
            raise ValueError(
                "ExecutorCommandCompletion.identity must be ExecutorWorkIdentity"
            )
        if type(self.work) is not QueuedExecutorWork:
            raise ValueError(
                "ExecutorCommandCompletion.work must be QueuedExecutorWork"
            )
        if type(self.command) is not ExecutedExecutorCommand:
            raise ValueError(
                "ExecutorCommandCompletion.command must be ExecutedExecutorCommand"
            )
        if type(self.previous_demand) is not ExecutorLearnedDemand:
            raise ValueError(
                "ExecutorCommandCompletion.previous_demand must be "
                "ExecutorLearnedDemand"
            )
        if (
            type(self.aggressiveness_percent) is not int
            or not 25 <= self.aggressiveness_percent <= 400
        ):
            raise ValueError(
                "ExecutorCommandCompletion.aggressiveness_percent must be "
                "25 through 400"
            )

    @property
    def public_result(self) -> ExecutorRunResult:
        return ExecutorRunResult(
            self.command.exit_code,
            ExecutorConcurrencyGrant(self.command.admission_grant.concurrency),
        )


@runtime_checkable
class ExecutorCompletionClock(Protocol):
    """Wall-clock source for persisted learning observations."""

    def observed_at_unix(self) -> float: ...


@runtime_checkable
class ExecutorCompletionReporter(Protocol):
    """Human diagnostic seam for command completion and finalization failure."""

    def completed(self, completion: ExecutorCommandCompletion) -> None: ...

    def finalization_failed(
        self,
        completion: ExecutorCommandCompletion,
        error: ExecutorCommandFinalizationError,
    ) -> None: ...


@runtime_checkable
class ExecutorCompletionHistory(Protocol):
    """Learning persistence used by command finalization."""

    def record_successful(
        self,
        identity: ExecutorWorkIdentity,
        observation: RecordedExecutorObservation,
    ) -> None: ...

    def successful_resources(
        self,
        identity: ExecutorWorkIdentity,
    ) -> tuple[ExecutorResourceObservation, ...]: ...


@runtime_checkable
class ExecutorCompletionEvents(Protocol):
    """Durable terminal-event publication used by command finalization."""

    def completed(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        grant: ExecutorAdmissionGrant,
        aggressiveness_percent: int,
        observation: RecordedExecutorObservation,
        previous_cores_per_concurrency: float,
        updated_cores_per_concurrency: float,
        successful_observation_count: int,
        host_load: ExecutorHostLoadObservation,
    ) -> None: ...

    def command_finalization_failed(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        result: ExecutedExecutorCommand,
        error: ExecutorCommandFinalizationError,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _FinalizationAttemptSucceeded:
    """One required finalization operation completed."""


@dataclass(frozen=True, slots=True)
class _FinalizationAttemptFailed:
    failure: ExecutorCommandFinalizationFailure


_FinalizationAttempt = _FinalizationAttemptSucceeded | _FinalizationAttemptFailed


@dataclass(frozen=True, slots=True)
class _RecordedObservationAvailable:
    observation: RecordedExecutorObservation


@dataclass(frozen=True, slots=True)
class _RecordedObservationUnavailable:
    failure: ExecutorCommandFinalizationFailure


_RecordedObservation = _RecordedObservationAvailable | _RecordedObservationUnavailable


@dataclass(frozen=True, slots=True)
class _LearnedHistoryAvailable:
    resources: tuple[ExecutorResourceObservation, ...]
    demand: ExecutorLearnedDemand


@dataclass(frozen=True, slots=True)
class _LearnedHistoryUnavailable:
    failure: ExecutorCommandFinalizationFailure


_LearnedHistory = _LearnedHistoryAvailable | _LearnedHistoryUnavailable


class SystemExecutorCompletionClock:
    """Production wall clock for learning observation timestamps."""

    def observed_at_unix(self) -> float:
        return time.time()


class StderrExecutorCompletionReporter:
    """Production human-readable terminal reporter."""

    def __init__(self, host_cpu_slots: int) -> None:
        if type(host_cpu_slots) is not int or host_cpu_slots < 1:
            raise ValueError(
                "StderrExecutorCompletionReporter.host_cpu_slots must be positive"
            )
        self._host_cpu_slots = host_cpu_slots

    def completed(self, completion: ExecutorCommandCompletion) -> None:
        command = completion.command
        print(
            f"[executor] completed work={completion.identity.work_key.value} "
            f"cpu_slots={command.admission_grant.cpu_slots}/"
            f"{self._host_cpu_slots} "
            f"concurrency={command.admission_grant.concurrency} "
            f"exit={command.exit_code} "
            f"wall={command.resources.wall_seconds:.3f}s "
            f"child_cpu={command.resources.cpu_seconds:.3f}s "
            "executor_process_lifetime_children_max_rss="
            f"{command.resources.executor_process_lifetime_children_max_rss_bytes}",
            file=sys.stderr,
            flush=True,
        )

    def finalization_failed(
        self,
        completion: ExecutorCommandCompletion,
        error: ExecutorCommandFinalizationError,
    ) -> None:
        failures = ",".join(failure.attempt_name for failure in error.failures)
        print(
            "[executor] finalization-failed "
            f"work={completion.identity.work_key.value} "
            f"concurrency={completion.command.admission_grant.concurrency} "
            f"command_exit={completion.command.exit_code} "
            f"failed_attempts={failures}",
            file=sys.stderr,
            flush=True,
        )


class ExecutorCommandFinalizer:
    """Finalize one exact command result without losing independent evidence."""

    def __init__(
        self,
        history: ExecutorCompletionHistory,
        events: ExecutorCompletionEvents,
        demand_estimator: ExecutorWorkDemandEstimator,
        clock: ExecutorCompletionClock,
        reporter: ExecutorCompletionReporter,
    ) -> None:
        _require_history(history)
        _require_events(events)
        if type(demand_estimator) is not ExecutorWorkDemandEstimator:
            raise ValueError(
                "ExecutorCommandFinalizer.demand_estimator must be "
                "ExecutorWorkDemandEstimator"
            )
        _require_clock(clock)
        _require_reporter(reporter)
        self._history = history
        self._events = events
        self._demand_estimator = demand_estimator
        self._clock = clock
        self._reporter = reporter

    def finalize(self, completion: ExecutorCommandCompletion) -> ExecutorRunResult:
        """Attempt all independent seams and fail with the exact command result."""
        if type(completion) is not ExecutorCommandCompletion:
            raise ValueError(
                "ExecutorCommandFinalizer.finalize.completion must be "
                "ExecutorCommandCompletion"
            )
        failures: list[ExecutorCommandFinalizationFailure] = []
        observation = self._construct_observation(completion)
        self._retain_observation_failure(observation, failures)

        report = self._attempt(
            "report command completion",
            lambda: self._reporter.completed(completion),
        )
        self._retain_attempt_failure(report, failures)

        if type(observation) is _RecordedObservationAvailable:
            history_write = self._record_history(completion, observation.observation)
            self._retain_attempt_failure(history_write, failures)
            learned_history = self._read_learned_history(completion)
            self._retain_history_failure(learned_history, failures)
            if (
                type(history_write) is _FinalizationAttemptSucceeded
                and type(learned_history) is _LearnedHistoryAvailable
            ):
                completed_event = self._publish_completed_event(
                    completion,
                    observation.observation,
                    learned_history,
                )
                self._retain_attempt_failure(completed_event, failures)
        elif type(observation) is not _RecordedObservationUnavailable:
            raise AssertionError("recorded observation is a closed union")

        if not failures:
            return completion.public_result
        error = ExecutorCommandFinalizationError(
            completion.public_result,
            tuple(failures),
        )
        failure_publication = self._attempt(
            "publish command finalization failure",
            lambda: self._events.command_finalization_failed(
                completion.identity,
                completion.work,
                completion.command,
                error,
            ),
        )
        self._retain_attempt_failure(failure_publication, failures)
        failure_report = self._attempt(
            "report command finalization failure",
            lambda: self._reporter.finalization_failed(completion, error),
        )
        self._retain_attempt_failure(failure_report, failures)
        raise ExecutorCommandFinalizationError(
            completion.public_result,
            tuple(failures),
        )

    def _construct_observation(
        self,
        completion: ExecutorCommandCompletion,
    ) -> _RecordedObservation:
        try:
            return _RecordedObservationAvailable(
                RecordedExecutorObservation(
                    resources=completion.command.resources,
                    exit_code=completion.command.exit_code,
                    recorded_at_unix=self._clock.observed_at_unix(),
                )
            )
        except BaseException as error:
            error.add_note(
                "executor finalization attempt failed: construct recorded observation"
            )
            return _RecordedObservationUnavailable(
                ExecutorCommandFinalizationFailure(
                    "construct recorded observation",
                    error,
                )
            )

    def _record_history(
        self,
        completion: ExecutorCommandCompletion,
        observation: RecordedExecutorObservation,
    ) -> _FinalizationAttempt:
        if observation.exit_code != 0:
            return _FinalizationAttemptSucceeded()
        return self._attempt(
            "record successful command history",
            lambda: self._history.record_successful(
                completion.identity,
                observation,
            ),
        )

    def _read_learned_history(
        self,
        completion: ExecutorCommandCompletion,
    ) -> _LearnedHistory:
        try:
            resources = self._history.successful_resources(completion.identity)
            return _LearnedHistoryAvailable(
                resources,
                self._demand_estimator.estimate(resources),
            )
        except BaseException as error:
            error.add_note(
                "executor finalization attempt failed: read learned command history"
            )
            return _LearnedHistoryUnavailable(
                ExecutorCommandFinalizationFailure(
                    "read learned command history",
                    error,
                )
            )

    def _publish_completed_event(
        self,
        completion: ExecutorCommandCompletion,
        observation: RecordedExecutorObservation,
        learned_history: _LearnedHistoryAvailable,
    ) -> _FinalizationAttempt:
        return self._attempt(
            "publish command completed event",
            lambda: self._events.completed(
                identity=completion.identity,
                work=completion.work,
                grant=completion.command.admission_grant,
                aggressiveness_percent=completion.aggressiveness_percent,
                observation=observation,
                previous_cores_per_concurrency=(
                    completion.previous_demand.cores_per_concurrency
                ),
                updated_cores_per_concurrency=(
                    learned_history.demand.cores_per_concurrency
                ),
                successful_observation_count=len(learned_history.resources),
                host_load=observe_host_load(),
            ),
        )

    @staticmethod
    def _attempt(
        name: str,
        operation: Callable[[], object],
    ) -> _FinalizationAttempt:
        if type(name) is not str or not name:
            raise ValueError("executor finalization attempt name must not be empty")
        try:
            operation()
        except BaseException as error:
            error.add_note(f"executor finalization attempt failed: {name}")
            return _FinalizationAttemptFailed(
                ExecutorCommandFinalizationFailure(name, error)
            )
        return _FinalizationAttemptSucceeded()

    @staticmethod
    def _retain_attempt_failure(
        attempt: _FinalizationAttempt,
        failures: list[ExecutorCommandFinalizationFailure],
    ) -> None:
        if type(attempt) is _FinalizationAttemptFailed:
            failures.append(attempt.failure)
        elif type(attempt) is not _FinalizationAttemptSucceeded:
            raise AssertionError("finalization attempt is a closed union")

    @staticmethod
    def _retain_observation_failure(
        observation: _RecordedObservation,
        failures: list[ExecutorCommandFinalizationFailure],
    ) -> None:
        if type(observation) is _RecordedObservationUnavailable:
            failures.append(observation.failure)
        elif type(observation) is not _RecordedObservationAvailable:
            raise AssertionError("recorded observation is a closed union")

    @staticmethod
    def _retain_history_failure(
        history: _LearnedHistory,
        failures: list[ExecutorCommandFinalizationFailure],
    ) -> None:
        if type(history) is _LearnedHistoryUnavailable:
            failures.append(history.failure)
        elif type(history) is not _LearnedHistoryAvailable:
            raise AssertionError("learned history is a closed union")


def build_executor_command_finalizer(
    history: ExecutorWorkHistoryStore,
    events: ExecutorEventStore,
    demand_estimator: ExecutorWorkDemandEstimator,
    host_cpu_slots: int,
) -> ExecutorCommandFinalizer:
    """Compose production finalization dependencies behind one constructor."""
    return ExecutorCommandFinalizer(
        history,
        events,
        demand_estimator,
        SystemExecutorCompletionClock(),
        StderrExecutorCompletionReporter(host_cpu_slots),
    )


def _require_history(value: object) -> None:
    if not isinstance(value, ExecutorCompletionHistory):
        raise ValueError(
            "ExecutorCommandFinalizer.history must implement "
            "ExecutorCompletionHistory"
        )


def _require_events(value: object) -> None:
    if not isinstance(value, ExecutorCompletionEvents):
        raise ValueError(
            "ExecutorCommandFinalizer.events must implement ExecutorCompletionEvents"
        )


def _require_clock(value: object) -> None:
    if not isinstance(value, ExecutorCompletionClock):
        raise ValueError(
            "ExecutorCommandFinalizer.clock must implement ExecutorCompletionClock"
        )


def _require_reporter(value: object) -> None:
    if not isinstance(value, ExecutorCompletionReporter):
        raise ValueError(
            "ExecutorCommandFinalizer.reporter must implement "
            "ExecutorCompletionReporter"
        )
