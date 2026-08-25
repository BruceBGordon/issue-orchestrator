"""Storage-boundary tests for adaptive executor learning retention."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from issue_orchestrator.control.executor_admission import (
    ExecutorAdmissionPolicy,
    ExecutorLearningPolicy,
    ExecutorResourceObservation,
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
from issue_orchestrator.execution.host_executor._history import (
    ExecutorWorkHistoryStore,
)
from issue_orchestrator.execution.host_executor._contracts import WorkHistoryRecord
from issue_orchestrator.execution.host_executor._types import (
    ExecutorRepositoryIdentity,
    ExecutorWorkIdentity,
    RecordedExecutorObservation,
)
from issue_orchestrator.execution.host_executor import _history as history_module


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


def _recorded_observation(*, recorded_at_unix: float) -> RecordedExecutorObservation:
    return RecordedExecutorObservation(
        resources=ExecutorResourceObservation(
            concurrency=1,
            wall_seconds=1.0,
            cpu_seconds=0.5,
            max_rss_bytes=1024,
            input_blocks=0,
            output_blocks=0,
        ),
        exit_code=0,
        recorded_at_unix=recorded_at_unix,
    )


def test_history_reader_holds_shared_lock_across_read_and_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer cannot prune a profile between its existence check and read."""
    history_dir = tmp_path / "history"
    store = ExecutorWorkHistoryStore(
        history_dir,
        ExecutorHistoryRetentionPolicy(
            maximum_profiles=1,
            maximum_observations_per_profile=2,
        ),
    )
    repository = ExecutorRepositoryIdentity(tmp_path.resolve(), "test-repository")
    first_identity = ExecutorWorkIdentity(
        repository,
        ExecutorWorkKey("history:first"),
    )
    second_identity = ExecutorWorkIdentity(
        repository,
        ExecutorWorkKey("history:second"),
    )
    store.record_successful(
        first_identity,
        _recorded_observation(recorded_at_unix=1.0),
    )

    reader_inside_record = threading.Event()
    release_reader = threading.Event()
    observe_writer_attempt = threading.Event()
    writer_attempting_exclusive_lock = threading.Event()
    original_read_record = ExecutorWorkHistoryStore._read_record
    original_flock = history_module.fcntl.flock
    [first_profile] = history_dir.glob("*.json")

    def synchronized_read(path: Path) -> WorkHistoryRecord:
        if path == first_profile:
            reader_inside_record.set()
            if not release_reader.wait(timeout=5.0):
                raise RuntimeError("history reader synchronization timed out")
        return original_read_record(path)

    def observed_flock(descriptor: int, operation: int) -> None:
        if (
            observe_writer_attempt.is_set()
            and operation == history_module.fcntl.LOCK_EX
        ):
            writer_attempting_exclusive_lock.set()
        original_flock(descriptor, operation)

    monkeypatch.setattr(
        ExecutorWorkHistoryStore,
        "_read_record",
        staticmethod(synchronized_read),
    )
    monkeypatch.setattr(history_module.fcntl, "flock", observed_flock)

    with ThreadPoolExecutor(max_workers=2) as workers:
        reader = workers.submit(store.successful_resources, first_identity)
        assert reader_inside_record.wait(timeout=5.0)
        observe_writer_attempt.set()
        writer = workers.submit(
            store.record_successful,
            second_identity,
            _recorded_observation(recorded_at_unix=2.0),
        )
        assert writer_attempting_exclusive_lock.wait(timeout=5.0)
        with pytest.raises(FutureTimeoutError):
            writer.result(timeout=0.05)

        release_reader.set()
        assert len(reader.result(timeout=5.0)) == 1
        writer.result(timeout=5.0)

        # The writer already owns LOCK_EX while reading its current profile;
        # this must use the explicit lock-assuming helper, not nest LOCK_SH.
        same_identity_writer = workers.submit(
            store.record_successful,
            second_identity,
            _recorded_observation(recorded_at_unix=3.0),
        )
        same_identity_writer.result(timeout=5.0)


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
