"""Storage-boundary tests for adaptive executor learning retention."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time

import pytest

from issue_orchestrator.control.executor_admission import (
    ExecutorAdmissionPolicy,
    ExecutorLearningPolicy,
    ExecutorSaturationPolicy,
    ExecutorWorkDemandEstimator,
)
from issue_orchestrator.domain.executor import (
    ExecutorCommand,
    ExecutorConcurrencyRange,
    ExecutorFairnessGroup,
    ExecutorHistoryRetentionPolicy,
    ExecutorProcessTerminationPolicy,
    ExecutorRunSpecification,
    ExecutorUnboundedDeadline,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.executor_host import ExecutorHostCpuUtilization
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorAllRepositories,
    ExecutorStatusQuery,
)
from issue_orchestrator.execution.host_executor import (
    ExecutorRequestIdentityFactory,
    HostExecutor,
    HostExecutorMonitor,
)
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class _IdleHostCpuObserver:
    def reset(self) -> None:
        return None

    def observe(self) -> ExecutorHostCpuUtilization:
        return ExecutorHostCpuUtilization(0.0, 0.01)


def _demand_estimator() -> ExecutorWorkDemandEstimator:
    return ExecutorWorkDemandEstimator(
        ExecutorLearningPolicy(
            cold_start_cores_per_concurrency=1.0,
            minimum_cores_per_concurrency=0.05,
            recent_observation_weight=0.3,
        )
    )


def test_history_prunes_old_profiles_and_bounds_samples_per_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_dir = tmp_path / "pool"
    retention = ExecutorHistoryRetentionPolicy(
        maximum_profiles=3,
        maximum_observations_per_profile=2,
    )
    executor = HostExecutor(
        pool_dir=pool_dir,
        host_cpu_slots=2,
        admission_policy=ExecutorAdmissionPolicy(
            ExecutorSaturationPolicy(maximum_busy_percent=95)
        ),
        demand_estimator=_demand_estimator(),
        host_cpu_observer=_IdleHostCpuObserver(),
        request_identity_factory=ExecutorRequestIdentityFactory(
            wall_time_nanoseconds=time.time_ns,
            monotonic_nanoseconds=time.monotonic_ns,
            process_id=os.getpid,
            request_nonce=lambda: "a" * 32,
        ),
        process_group_terminator=PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.1,
                forceful_shutdown_seconds=0.1,
            )
        ),
        history_retention_policy=retention,
        queue_settle_seconds=0.001,
        queue_poll_seconds=0.001,
    )
    monkeypatch.chdir(REPO_ROOT)

    for index in range(4):
        repetitions = 3 if index == 3 else 1
        for _ in range(repetitions):
            result = executor.run(
                ExecutorRunSpecification(
                    work_key=ExecutorWorkKey(f"retention:work-{index}"),
                    fairness_group=ExecutorFairnessGroup(f"retention:group-{index}"),
                    concurrency_range=ExecutorConcurrencyRange(1, 1),
                    exclusive_resources=(),
                ),
                ExecutorCommand(
                    (sys.executable, "-c", "pass"),
                    ExecutorUnboundedDeadline(),
                ),
            )
            assert result.exit_code == 0

    monitor = HostExecutorMonitor(
        pool_dir,
        2,
        _demand_estimator(),
        retention,
    )
    status = monitor.status(
        ExecutorStatusQuery(ExecutorAllRepositories(), offset=0, limit=10)
    )

    assert status.learning.total_profile_count == 3
    assert tuple(item.work_key.value for item in status.learning.learned_work) == (
        "retention:work-1",
        "retention:work-2",
        "retention:work-3",
    )
    assert status.learning.learned_work[-1].successful_observation_count == 2
    assert status.learning.successful_observation_count == 4
