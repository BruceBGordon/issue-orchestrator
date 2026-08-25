"""Storage-boundary tests for adaptive executor learning retention."""

from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
import os
from pathlib import Path
import sys
import threading
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
    ExecutorRunResult,
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
from issue_orchestrator.execution.executor_history_lock import (
    PosixExecutorHistoryRetentionLock,
)
from issue_orchestrator.ports.executor_history_lock import (
    ExecutorHistoryRetentionLock,
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


def _executor(
    pool_dir: Path,
    retention: ExecutorHistoryRetentionPolicy,
    retention_lock: ExecutorHistoryRetentionLock,
    *,
    request_nonce: str,
) -> HostExecutor:
    return HostExecutor(
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
            request_nonce=lambda: request_nonce,
        ),
        process_group_terminator=PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.1,
                forceful_shutdown_seconds=0.1,
            )
        ),
        history_retention_lock=retention_lock,
        history_retention_policy=retention,
        queue_settle_seconds=0.001,
        queue_poll_seconds=0.001,
    )


def _run_work(executor: HostExecutor, work_key: str) -> ExecutorRunResult:
    return executor.run(
        ExecutorRunSpecification(
            work_key=ExecutorWorkKey(work_key),
            fairness_group=ExecutorFairnessGroup(f"group:{work_key}"),
            concurrency_range=ExecutorConcurrencyRange(1, 1),
            exclusive_resources=(),
        ),
        ExecutorCommand(
            (sys.executable, "-c", "pass"),
            ExecutorUnboundedDeadline(),
        ),
    )


class _BlockingFirstSharedRetentionLock:
    """Test adapter that pauses one reader while it owns the real lock."""

    def __init__(
        self,
        delegate: ExecutorHistoryRetentionLock,
        shared_acquired: threading.Event,
        release_shared: threading.Event,
    ) -> None:
        self._delegate = delegate
        self._shared_acquired = shared_acquired
        self._release_shared = release_shared
        self._first_shared = True

    @contextmanager
    def shared(self) -> Generator[None]:
        with self._delegate.shared():
            if self._first_shared:
                self._first_shared = False
                self._shared_acquired.set()
                if not self._release_shared.wait(timeout=5.0):
                    raise RuntimeError("history reader synchronization timed out")
            yield

    def exclusive(self) -> AbstractContextManager[None]:
        raise AssertionError("read-only monitor must not request an exclusive lock")


class _ObservedExclusiveRetentionLock:
    """Test adapter exposing the real writer-lock acquisition boundary."""

    def __init__(
        self,
        delegate: ExecutorHistoryRetentionLock,
        exclusive_attempted: threading.Event,
        exclusive_acquired: threading.Event,
    ) -> None:
        self._delegate = delegate
        self._exclusive_attempted = exclusive_attempted
        self._exclusive_acquired = exclusive_acquired

    def shared(self) -> AbstractContextManager[None]:
        return self._delegate.shared()

    @contextmanager
    def exclusive(self) -> Generator[None]:
        self._exclusive_attempted.set()
        with self._delegate.exclusive():
            self._exclusive_acquired.set()
            yield


def test_history_reader_holds_shared_lock_across_read_and_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer cannot prune a profile between its existence check and read."""
    pool_dir = tmp_path / "pool"
    history_dir = pool_dir / "work-history"
    lock_path = (history_dir / "retention.lock").resolve()
    retention = ExecutorHistoryRetentionPolicy(
        maximum_profiles=1,
        maximum_observations_per_profile=2,
    )
    monkeypatch.chdir(REPO_ROOT)

    seed_executor = _executor(
        pool_dir,
        retention,
        PosixExecutorHistoryRetentionLock(lock_path),
        request_nonce="b" * 32,
    )
    assert _run_work(seed_executor, "history:first").exit_code == 0

    reader_holds_shared = threading.Event()
    release_reader = threading.Event()
    writer_attempting_exclusive_lock = threading.Event()
    writer_acquired_exclusive_lock = threading.Event()
    reader_monitor = HostExecutorMonitor(
        pool_dir,
        2,
        _demand_estimator(),
        retention,
        _BlockingFirstSharedRetentionLock(
            PosixExecutorHistoryRetentionLock(lock_path),
            reader_holds_shared,
            release_reader,
        ),
    )
    writer_executor = _executor(
        pool_dir,
        retention,
        _ObservedExclusiveRetentionLock(
            PosixExecutorHistoryRetentionLock(lock_path),
            writer_attempting_exclusive_lock,
            writer_acquired_exclusive_lock,
        ),
        request_nonce="c" * 32,
    )

    with ThreadPoolExecutor(max_workers=2) as workers:
        reader = workers.submit(
            reader_monitor.status,
            ExecutorStatusQuery(ExecutorAllRepositories(), offset=0, limit=10),
        )
        try:
            assert reader_holds_shared.wait(timeout=5.0)
            writer = workers.submit(_run_work, writer_executor, "history:second")
            assert writer_attempting_exclusive_lock.wait(timeout=5.0)
            assert not writer_acquired_exclusive_lock.is_set()
        finally:
            release_reader.set()
        assert reader.result(timeout=5.0).learning.total_profile_count == 1
        assert writer.result(timeout=5.0).exit_code == 0

    # A second success for the retained identity proves the writer reads its
    # current profile inside the existing exclusive transaction without nesting.
    assert _run_work(writer_executor, "history:second").exit_code == 0
    status = HostExecutorMonitor(
        pool_dir,
        2,
        _demand_estimator(),
        retention,
        PosixExecutorHistoryRetentionLock(lock_path),
    ).status(ExecutorStatusQuery(ExecutorAllRepositories(), offset=0, limit=10))
    assert tuple(work.work_key.value for work in status.learning.learned_work) == (
        "history:second",
    )
    assert status.learning.successful_observation_count == 2


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
        history_retention_lock=PosixExecutorHistoryRetentionLock(
            (pool_dir / "work-history" / "retention.lock").resolve()
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
        PosixExecutorHistoryRetentionLock(
            (pool_dir / "work-history" / "retention.lock").resolve()
        ),
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
