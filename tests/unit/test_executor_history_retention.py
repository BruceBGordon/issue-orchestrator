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
    ExecutorAggressiveness,
    ExecutorCommand,
    ExecutorCommandLifecycle,
    ExecutorConcurrencyRange,
    ExecutorFairnessGroup,
    ExecutorHistoryRetentionPolicy,
    ExecutorNoCommandCancellation,
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
from issue_orchestrator.execution.atomic_record_store import OsAtomicPathReplacement
from issue_orchestrator.execution.executor_history_lock import (
    PosixExecutorHistoryRetentionLock,
)
from issue_orchestrator.ports.executor_history_lock import (
    ExecutorHistoryRetentionLock,
)
from issue_orchestrator.ports.atomic_path_replacement import AtomicPathReplacement
from tests.unit.executor_guardian_helpers import executor_command_guardian
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG


REPO_ROOT = Path(__file__).resolve().parents[2]


pytestmark = pytest.mark.timeout(180)


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
    atomic_path_replacement: AtomicPathReplacement,
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
        command_guardian=executor_command_guardian(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.1,
                forceful_shutdown_seconds=0.1,
            )
        ),
        atomic_path_replacement=atomic_path_replacement,
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
            ExecutorCommandLifecycle.DETACHED,
            ExecutorNoCommandCancellation(),
        ),
    )


class _FailingAtomicPathReplacement:
    """Filesystem-port fake failing after the temporary record is durable."""

    def __init__(self) -> None:
        self.attempts: list[tuple[Path, Path]] = []

    def replace(self, source: Path, destination: Path) -> None:
        self.attempts.append((source, destination))
        raise OSError("simulated atomic replacement failure")


def test_atomic_replace_failure_removes_its_temporary_record(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    replacement = _FailingAtomicPathReplacement()
    executor = _executor(
        pool_dir,
        ExecutorHistoryRetentionPolicy(3, 2),
        PosixExecutorHistoryRetentionLock(
            (pool_dir / "work-history" / "retention.lock").resolve()
        ),
        request_nonce="e" * 32,
        atomic_path_replacement=replacement,
    )

    with pytest.raises(OSError, match="simulated atomic replacement failure"):
        executor.configure_policy(ExecutorAggressiveness(125))

    [attempt] = replacement.attempts
    assert attempt[1] == pool_dir / "policy.json"
    assert attempt[0].exists() is False


def test_executor_prunes_only_recognizable_atomic_crash_remnants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_dir = tmp_path / "pool"
    history_dir = pool_dir / "work-history"
    history_dir.mkdir(parents=True)
    current_pool_remnant = pool_dir / ".io-atomic-record-current.tmp"
    legacy_pool_remnant = pool_dir / ".io-executor-atomic-legacy.tmp"
    current_history_remnant = history_dir / ".io-atomic-record-current.tmp"
    legacy_history_remnant = history_dir / ".io-executor-atomic-legacy.tmp"
    unrelated_hidden_file = pool_dir / ".unrelated.tmp"
    crash_remnants = (
        current_pool_remnant,
        legacy_pool_remnant,
        current_history_remnant,
        legacy_history_remnant,
    )
    for path in (*crash_remnants, unrelated_hidden_file):
        path.write_text("crash debris", encoding="utf-8")
    executor = _executor(
        pool_dir,
        ExecutorHistoryRetentionPolicy(3, 2),
        PosixExecutorHistoryRetentionLock((history_dir / "retention.lock").resolve()),
        request_nonce="f" * 32,
        atomic_path_replacement=OsAtomicPathReplacement(),
    )
    monkeypatch.chdir(REPO_ROOT)

    assert _run_work(executor, "atomic:prune").exit_code == 0

    assert all(path.exists() is False for path in crash_remnants)
    assert unrelated_hidden_file.exists() is True


def test_retention_delete_sync_failure_surfaces_and_stops_later_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_dir = tmp_path / "pool"
    history_dir = pool_dir / "work-history"
    retention_lock = PosixExecutorHistoryRetentionLock(
        (history_dir / "retention.lock").resolve()
    )
    seed_executor = _executor(
        pool_dir,
        ExecutorHistoryRetentionPolicy(4, 2),
        retention_lock,
        request_nonce="1" * 32,
        atomic_path_replacement=OsAtomicPathReplacement(),
    )
    assert _run_work(seed_executor, "retention:first").exit_code == 0
    assert _run_work(seed_executor, "retention:second").exit_code == 0
    seeded_profiles = tuple(sorted(history_dir.glob("*.json")))
    assert len(seeded_profiles) == 2
    for position, path in enumerate(seeded_profiles, start=1):
        timestamp = position * 1_000_000_000
        os.utime(path, ns=(timestamp, timestamp))
    oldest_first = tuple(
        sorted(
            seeded_profiles,
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
    )

    pruning_executor = _executor(
        pool_dir,
        ExecutorHistoryRetentionPolicy(1, 2),
        retention_lock,
        request_nonce="2" * 32,
        atomic_path_replacement=OsAtomicPathReplacement(),
    )
    history_directory_identity = history_dir.stat()
    original_fsync = os.fsync
    history_directory_syncs = 0

    def fail_second_history_directory_sync(file_descriptor: int) -> None:
        nonlocal history_directory_syncs
        descriptor_identity = os.fstat(file_descriptor)
        if (
            descriptor_identity.st_dev == history_directory_identity.st_dev
            and descriptor_identity.st_ino == history_directory_identity.st_ino
        ):
            history_directory_syncs += 1
            if history_directory_syncs == 2:
                raise OSError("simulated retention directory sync failure")
        original_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", fail_second_history_directory_sync)

    with pytest.raises(OSError, match="simulated retention directory sync failure"):
        _run_work(pruning_executor, "retention:new")

    assert history_directory_syncs == 2
    assert oldest_first[0].exists() is False
    assert oldest_first[1].exists() is True
    assert len(tuple(history_dir.glob("*.json"))) == 2


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
                PROCESS_COMPLETION_WATCHDOG.wait_for_event(
                    self._release_shared,
                    operation="release the retained-history reader",
                )
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
        atomic_path_replacement=OsAtomicPathReplacement(),
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
        OsAtomicPathReplacement(),
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
        atomic_path_replacement=OsAtomicPathReplacement(),
    )

    with ThreadPoolExecutor(max_workers=2) as workers:
        reader = workers.submit(
            reader_monitor.status,
            ExecutorStatusQuery(ExecutorAllRepositories(), offset=0, limit=10),
        )
        try:
            PROCESS_COMPLETION_WATCHDOG.wait_for_event(
                reader_holds_shared,
                operation="history reader to acquire its shared lock",
            )
            writer = workers.submit(_run_work, writer_executor, "history:second")
            PROCESS_COMPLETION_WATCHDOG.wait_for_event(
                writer_attempting_exclusive_lock,
                operation="history writer to attempt its exclusive lock",
            )
            assert not writer_acquired_exclusive_lock.is_set()
        finally:
            release_reader.set()
        reader_result = PROCESS_COMPLETION_WATCHDOG.future_result(
            reader,
            operation="history reader result",
        )
        writer_result = PROCESS_COMPLETION_WATCHDOG.future_result(
            writer,
            operation="history writer result",
        )
        assert reader_result.learning.total_profile_count == 1
        assert writer_result.exit_code == 0

    # A second success for the retained identity proves the writer reads its
    # current profile inside the existing exclusive transaction without nesting.
    assert _run_work(writer_executor, "history:second").exit_code == 0
    status = HostExecutorMonitor(
        pool_dir,
        2,
        _demand_estimator(),
        retention,
        PosixExecutorHistoryRetentionLock(lock_path),
        OsAtomicPathReplacement(),
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
        command_guardian=executor_command_guardian(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.1,
                forceful_shutdown_seconds=0.1,
            )
        ),
        atomic_path_replacement=OsAtomicPathReplacement(),
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
                    ExecutorCommandLifecycle.DETACHED,
                    ExecutorNoCommandCancellation(),
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
        OsAtomicPathReplacement(),
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
