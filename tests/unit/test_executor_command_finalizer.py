"""Public fault proofs for executor command finalization ownership."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import pytest

from issue_orchestrator.control.executor_admission import (
    ExecutorAdmissionGrant,
    ExecutorLearningPolicy,
    ExecutorLearnedDemand,
    ExecutorResourceObservation,
    ExecutorWorkDemandEstimator,
    QueuedExecutorWork,
)
from issue_orchestrator.domain.executor import (
    ExecutorAggressiveness,
    ExecutorCommandFinalizationError,
    ExecutorConcurrencyRange,
    ExecutorFairnessGroup,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.executor_monitoring import ExecutorRequestId
from issue_orchestrator.execution.host_executor._completion import (
    ExecutorCommandCompletion,
    ExecutorCommandFinalizer,
)
from issue_orchestrator.execution.host_executor._host_observation import (
    ExecutorHostLoadObservation,
)
from issue_orchestrator.execution.host_executor._types import (
    ExecutedExecutorCommand,
    ExecutorRepositoryIdentity,
    ExecutorWorkIdentity,
    RecordedExecutorObservation,
)


class _FinalizationFault(StrEnum):
    CLOCK = "clock"
    HISTORY_WRITE = "history-write"
    HISTORY_READ = "history-read"
    COMPLETED_EVENT = "completed-event"
    COMPLETION_REPORT = "completion-report"


@dataclass(frozen=True, slots=True)
class _Scenario:
    fault: _FinalizationFault
    expected_attempt: str


@dataclass(slots=True)
class _ScenarioClock:
    fault: _FinalizationFault
    calls: int = 0

    def observed_at_unix(self) -> float:
        self.calls += 1
        if self.fault is _FinalizationFault.CLOCK:
            raise RuntimeError("injected completion clock failure")
        return 1_800_000_000.0


@dataclass(slots=True)
class _ScenarioHistory:
    fault: _FinalizationFault
    resources: tuple[ExecutorResourceObservation, ...]
    calls: list[str] = field(default_factory=list)

    def record_successful(
        self,
        identity: ExecutorWorkIdentity,
        observation: RecordedExecutorObservation,
    ) -> None:
        del identity, observation
        self.calls.append("write")
        if self.fault is _FinalizationFault.HISTORY_WRITE:
            raise OSError("injected history write failure")

    def successful_resources(
        self,
        identity: ExecutorWorkIdentity,
    ) -> tuple[ExecutorResourceObservation, ...]:
        del identity
        self.calls.append("read")
        if self.fault is _FinalizationFault.HISTORY_READ:
            raise OSError("injected history read failure")
        return self.resources


@dataclass(slots=True)
class _ScenarioEvents:
    fault: _FinalizationFault
    completed_calls: int = 0
    finalization_errors: list[ExecutorCommandFinalizationError] = field(
        default_factory=list
    )

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
    ) -> None:
        del (
            identity,
            work,
            grant,
            aggressiveness_percent,
            observation,
            previous_cores_per_concurrency,
            updated_cores_per_concurrency,
            successful_observation_count,
            host_load,
        )
        self.completed_calls += 1
        if self.fault is _FinalizationFault.COMPLETED_EVENT:
            raise OSError("injected completed event failure")

    def command_finalization_failed(
        self,
        identity: ExecutorWorkIdentity,
        work: QueuedExecutorWork,
        result: ExecutedExecutorCommand,
        error: ExecutorCommandFinalizationError,
    ) -> None:
        del identity, work
        assert result.exit_code == error.command_result.exit_code
        assert result.admission_grant.concurrency == (
            error.command_result.grant.concurrency
        )
        self.finalization_errors.append(error)


@dataclass(slots=True)
class _ScenarioReporter:
    fault: _FinalizationFault
    completed_calls: int = 0
    finalization_errors: list[ExecutorCommandFinalizationError] = field(
        default_factory=list
    )

    def completed(self, completion: ExecutorCommandCompletion) -> None:
        assert completion.command.exit_code == 0
        self.completed_calls += 1
        if self.fault is _FinalizationFault.COMPLETION_REPORT:
            raise OSError("injected completion report failure")

    def finalization_failed(
        self,
        completion: ExecutorCommandCompletion,
        error: ExecutorCommandFinalizationError,
    ) -> None:
        assert completion.public_result == error.command_result
        self.finalization_errors.append(error)


def _resource_observation() -> ExecutorResourceObservation:
    return ExecutorResourceObservation(
        concurrency=3,
        wall_seconds=4.0,
        cpu_seconds=6.0,
        executor_process_lifetime_children_max_rss_bytes=1024,
        input_blocks=5,
        output_blocks=7,
    )


def _completion(tmp_path: Path) -> ExecutorCommandCompletion:
    work_key = ExecutorWorkKey("io:finalization-proof")
    return ExecutorCommandCompletion(
        identity=ExecutorWorkIdentity(
            ExecutorRepositoryIdentity(
                (tmp_path / ".git").resolve(),
                "finalization-proof",
            ),
            work_key,
        ),
        work=QueuedExecutorWork(
            request_id=ExecutorRequestId("request-finalization-proof"),
            sequence=1,
            work_key=work_key,
            fairness_group=ExecutorFairnessGroup("validation:proof"),
            concurrency_range=ExecutorConcurrencyRange(1, 3),
            learned_demand=ExecutorLearnedDemand(1.0),
            aggressiveness=ExecutorAggressiveness(100),
            exclusive_resources=(),
        ),
        command=ExecutedExecutorCommand(
            exit_code=0,
            admission_grant=ExecutorAdmissionGrant(3, 2),
            resources=_resource_observation(),
        ),
        previous_demand=ExecutorLearnedDemand(1.0),
        aggressiveness_percent=100,
    )


def _demand_estimator() -> ExecutorWorkDemandEstimator:
    return ExecutorWorkDemandEstimator(
        ExecutorLearningPolicy(
            cold_start_cores_per_concurrency=1.0,
            minimum_cores_per_concurrency=0.05,
            recent_observation_weight=0.3,
        )
    )


@pytest.mark.parametrize(
    "scenario",
    (
        _Scenario(
            _FinalizationFault.CLOCK,
            "construct recorded observation",
        ),
        _Scenario(
            _FinalizationFault.HISTORY_WRITE,
            "record successful command history",
        ),
        _Scenario(
            _FinalizationFault.HISTORY_READ,
            "read learned command history",
        ),
        _Scenario(
            _FinalizationFault.COMPLETED_EVENT,
            "publish command completed event",
        ),
        _Scenario(
            _FinalizationFault.COMPLETION_REPORT,
            "report command completion",
        ),
    ),
)
def test_finalizer_preserves_exact_result_and_attempts_independent_failure_seams(
    tmp_path: Path,
    scenario: _Scenario,
) -> None:
    resources = (_resource_observation(),)
    history = _ScenarioHistory(scenario.fault, resources)
    events = _ScenarioEvents(scenario.fault)
    reporter = _ScenarioReporter(scenario.fault)
    finalizer = ExecutorCommandFinalizer(
        history,
        events,
        _demand_estimator(),
        _ScenarioClock(scenario.fault),
        reporter,
    )

    with pytest.raises(ExecutorCommandFinalizationError) as raised:
        finalizer.finalize(_completion(tmp_path))

    assert raised.value.command_result.exit_code == 0
    assert raised.value.command_result.grant.concurrency == 3
    assert tuple(
        failure.attempt_name for failure in raised.value.failures
    ) == (scenario.expected_attempt,)
    assert reporter.completed_calls == 1
    assert len(reporter.finalization_errors) == 1
    assert len(events.finalization_errors) == 1
    if scenario.fault is _FinalizationFault.CLOCK:
        assert history.calls == []
        assert events.completed_calls == 0
    elif scenario.fault in (
        _FinalizationFault.HISTORY_WRITE,
        _FinalizationFault.HISTORY_READ,
    ):
        assert history.calls == ["write", "read"]
        assert events.completed_calls == 0
    else:
        assert history.calls == ["write", "read"]
        assert events.completed_calls == 1


def test_finalizer_returns_only_after_all_completion_seams_succeed(
    tmp_path: Path,
) -> None:
    resources = (_resource_observation(),)
    history = _ScenarioHistory(_FinalizationFault.CLOCK, resources)
    clock = _ScenarioClock(_FinalizationFault.HISTORY_WRITE)
    events = _ScenarioEvents(_FinalizationFault.CLOCK)
    reporter = _ScenarioReporter(_FinalizationFault.CLOCK)
    finalizer = ExecutorCommandFinalizer(
        history,
        events,
        _demand_estimator(),
        clock,
        reporter,
    )

    result = finalizer.finalize(_completion(tmp_path))

    assert result.exit_code == 0
    assert result.grant.concurrency == 3
    assert clock.calls == 1
    assert history.calls == ["write", "read"]
    assert events.completed_calls == 1
    assert events.finalization_errors == []
    assert reporter.completed_calls == 1
    assert reporter.finalization_errors == []
