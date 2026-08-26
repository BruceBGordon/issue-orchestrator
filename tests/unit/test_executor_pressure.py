"""Deterministic pressure scenarios for the cross-process host executor."""

from pathlib import Path

import pytest

from issue_orchestrator.control.executor_admission import (
    ExecutorLearningPolicy,
    ExecutorWorkDemandEstimator,
)
from issue_orchestrator.domain.executor import (
    ExecutorConcurrencyRange,
    ExecutorFairnessGroup,
    ExecutorHistoryRetentionPolicy,
)
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorAllRepositories,
    ExecutorFairnessGroupEventsQuery,
    ExecutorRecentEventsQuery,
    ExecutorStatusQuery,
    ExecutorWorkAdmitted,
    ExecutorWorkCompleted,
    ExecutorWorkEnqueued,
    ExecutorWorkWaiting,
    ExecutorWaitReason,
)
from issue_orchestrator.execution.host_executor import HostExecutorMonitor
from issue_orchestrator.execution.atomic_record_store import OsAtomicPathReplacement
from issue_orchestrator.execution.executor_history_lock import (
    PosixExecutorHistoryRetentionLock,
)
from tests.unit.executor_pressure_dsl import (
    BoundedPressureDeadline,
    CloseFdsTreePressureCommand,
    HungPressureCommand,
    PressureJob,
    PressureRig,
    PressureWork,
)


# One outer escape hatch diagnoses a broken scenario protocol. Individual
# transitions are synchronized exclusively by pipes and process completion.
pytestmark = pytest.mark.timeout(180)


def _monitor(pool_dir: Path, host_cpu_slots: int) -> HostExecutorMonitor:
    return HostExecutorMonitor(
        pool_dir,
        host_cpu_slots,
        ExecutorWorkDemandEstimator(
            ExecutorLearningPolicy(
                cold_start_cores_per_concurrency=1.0,
                minimum_cores_per_concurrency=0.05,
                recent_observation_weight=0.3,
            )
        ),
        ExecutorHistoryRetentionPolicy(2048, 24),
        PosixExecutorHistoryRetentionLock(
            (pool_dir / "work-history" / "retention.lock").resolve()
        ),
        OsAtomicPathReplacement(),
    )


def test_pressure_many_groups_are_fair_and_event_history_stays_valid(
    tmp_path: Path,
) -> None:
    """Queue a cohort at once and drain it through one contended CPU slot."""
    pool_dir = tmp_path / "pool"
    groups = ("pressure-a", "pressure-b", "pressure-c", "pressure-d")
    with PressureRig(pool_dir, host_cpu_slots=1) as rig:
        blocker = rig.admit(PressureWork("BLOCKER", "pressure-blocker"))
        queued = rig.defer_all(
            tuple(
                PressureWork(f"WORK-{group}-{index}", group)
                for group in groups
                for index in range(3)
            )
        )
        remaining = list(queued)
        started_groups: list[str] = []
        rig.release(blocker)

        while remaining:
            started = rig.require_next_started(tuple(remaining))
            started_groups.append(started.work.group)
            remaining.remove(started)
            rig.release(started)

        service_rounds = tuple(
            started_groups[index : index + len(groups)]
            for index in range(0, len(started_groups), len(groups))
        )
        assert len(service_rounds) == 3
        assert all(
            set(service_round) == set(groups) for service_round in service_rounds
        )

        timeline = _monitor(pool_dir, 1).recent_events(
            ExecutorRecentEventsQuery(limit=1000)
        )
        pressure_admissions = tuple(
            event
            for event in timeline.events
            if isinstance(event, ExecutorWorkAdmitted)
            and event.work.work_key.value == "pressure:shared-work"
        )
        pressure_completions = tuple(
            event
            for event in timeline.events
            if isinstance(event, ExecutorWorkCompleted)
            and event.work.work_key.value == "pressure:shared-work"
        )
        assert len(pressure_admissions) == len(queued) + 1
        assert len(pressure_completions) == len(queued) + 1
        assert all(event.charged_cpu_slots == 1 for event in pressure_admissions)
        assert all(event.cpu_slots_before.total == 1 for event in pressure_admissions)


def test_group_event_query_excludes_interleaved_unrelated_work(
    tmp_path: Path,
) -> None:
    pool_dir = tmp_path / "pool"
    with PressureRig(pool_dir, host_cpu_slots=2) as rig:
        first = rig.admit(PressureWork("GROUP-A-1", "group-a"))
        second = rig.admit(PressureWork("GROUP-B-1", "group-b"))
        rig.release(second)
        third = rig.admit(PressureWork("GROUP-A-2", "group-a"))
        rig.release(first)
        rig.release(third)

    expected_group = ExecutorFairnessGroup("group-a")
    page = _monitor(pool_dir, 2).events_for_group(
        ExecutorFairnessGroupEventsQuery(expected_group, limit=1000)
    )

    assert page.total_matching_event_count == len(page.events)
    assert page.total_matching_event_count > 0
    assert all(event.work.fairness_group == expected_group for event in page.events)
    assert not any(
        event.work.fairness_group == ExecutorFairnessGroup("group-b")
        for event in page.events
    )


def test_pressure_exclusive_resource_remains_single_threaded(
    tmp_path: Path,
) -> None:
    """High CPU capacity must not permit overlap on one named resource."""
    pool_dir = tmp_path / "pool"
    with PressureRig(pool_dir, host_cpu_slots=4) as rig:
        blocker = rig.admit(
            PressureWork(
                "BLOCKER",
                "pressure-blocker",
                concurrency_range=ExecutorConcurrencyRange(4, 4),
            )
        )
        queued = rig.defer_all(
            tuple(
                PressureWork(
                    f"BROWSER-{index}",
                    f"pressure-browser-{index % 3}",
                    exclusive_resources=("browser",),
                )
                for index in range(6)
            )
        )
        remaining = list(queued)
        rig.release(blocker)

        while remaining:
            started = rig.require_next_started(tuple(remaining))
            remaining.remove(started)
            rig.require_none_started(tuple(remaining))
            rig.release(started)


def test_pressure_new_same_group_work_cannot_starve_an_old_wide_request(
    tmp_path: Path,
) -> None:
    """A narrow stream must let capacity drain for its older wide sibling."""
    pool_dir = tmp_path / "pool"
    with PressureRig(pool_dir, host_cpu_slots=2) as rig:
        blocker = rig.admit(PressureWork("BLOCKER", "pressure-existing"))
        wide = rig.defer(
            PressureWork(
                "WIDE",
                "pressure-shared",
                concurrency_range=ExecutorConcurrencyRange(2, 2),
            )
        )
        narrow = rig.defer_all(
            tuple(
                PressureWork(f"NARROW-{index}", "pressure-shared") for index in range(4)
            )
        )
        rig.require_none_started(narrow)

        rig.release(blocker)
        rig.require_started(wide)
        rig.require_none_started(narrow)

        rig.release(wide)
        rig.drain(narrow)


def test_pressure_guardian_retains_capacity_for_live_child_after_outer_crash(
    tmp_path: Path,
) -> None:
    """Killing the wrapper cannot release slots still used by its child."""
    pool_dir = tmp_path / "pool"
    with PressureRig(pool_dir, host_cpu_slots=1) as rig:
        crashed_parent = rig.admit(PressureWork("ORPHANED-CHILD", "pressure-crash"))
        rig.crash_parent(crashed_parent)
        follower = rig.defer(PressureWork("FOLLOWER", "pressure-follower"))

        rig.release_orphaned_child(crashed_parent)
        rig.require_started(follower)
        rig.release(follower)


def test_pressure_guardian_retains_capacity_until_close_fds_tree_is_contained(
    tmp_path: Path,
) -> None:
    """A disposable outer cannot orphan uncharged close-fds descendants."""
    pool_dir = tmp_path / "pool"
    tree = CloseFdsTreePressureCommand(
        guardian_pid_path=(tmp_path / "tree-guardian.pid").resolve(),
        descendant_pid_path=(tmp_path / "tree-descendant.pid").resolve(),
    )
    with PressureRig(pool_dir, host_cpu_slots=1) as rig:
        crashed_parent = rig.admit(
            PressureWork(
                "CLOSE-FDS-TREE",
                "pressure-guardian-crash",
                command=tree,
            )
        )
        rig.crash_parent(crashed_parent)
        tree.crash_guardian()
        follower = rig.defer(PressureWork("FOLLOWER", "pressure-follower"))
        rig.require_none_started((follower,))

        rig.release_orphaned_child(crashed_parent)
        rig.require_started(follower)
        tree.require_descendant_contained()
        rig.release(follower)


def test_pressure_guardian_retains_capacity_after_sentinel_hard_crash(
    tmp_path: Path,
) -> None:
    """A dead sentinel cannot uncharge work still owned by its guardian."""
    pool_dir = tmp_path / "pool"
    tree = CloseFdsTreePressureCommand(
        guardian_pid_path=(tmp_path / "sentinel-crash-guardian.pid").resolve(),
        descendant_pid_path=(tmp_path / "sentinel-crash-descendant.pid").resolve(),
    )
    with PressureRig(pool_dir, host_cpu_slots=1) as rig:
        holder = rig.admit(
            PressureWork(
                "SENTINEL-CRASH",
                "pressure-sentinel-crash",
                command=tree,
            )
        )
        follower = rig.defer(PressureWork("FOLLOWER", "pressure-follower"))
        rig.require_none_started((follower,))
        tree.require_descendant_executable()

        tree.crash_sentinel()
        rig.require_none_started((follower,))
        rig.require_started(follower)
        tree.require_descendant_contained()
        rig.require_failed(holder)
        rig.release(follower)


def test_pressure_guardian_enforces_deadline_after_executor_parent_crash(
    tmp_path: Path,
) -> None:
    """A bounded guardian releases capacity without its disposable outer."""
    pool_dir = tmp_path / "pool"
    hung = HungPressureCommand(
        guardian_pid_path=(tmp_path / "hung-guardian.pid").resolve(),
        command_pid_path=(tmp_path / "hung-command.pid").resolve(),
    )
    with PressureRig(pool_dir, host_cpu_slots=1) as rig:
        # Command activation legitimately spends real machinery time (guardian
        # and child-launcher interpreters); the bound must dwarf that cost so
        # the deadline under test measures the hung command, not startup.
        crashed_parent = rig.admit(
            PressureWork(
                "HUNG-COMMAND",
                "pressure-guardian-timeout",
                command=hung,
                deadline=BoundedPressureDeadline(10.0, 20.0),
            )
        )
        rig.crash_parent(crashed_parent)
        # The real deadline may expire before a contended host schedules this
        # process. The unbounded scenario above owns the pre-release invariant.
        follower = rig.submit(PressureWork("FOLLOWER", "pressure-follower"))

        rig.require_started(follower)
        hung.require_command_contained()
        rig.release(follower)


def test_pressure_killed_queued_parent_does_not_leave_a_phantom_request(
    tmp_path: Path,
) -> None:
    """A dead queue owner is pruned before it can distort later fairness."""
    pool_dir = tmp_path / "pool"
    with PressureRig(pool_dir, host_cpu_slots=1) as rig:
        blocker = rig.admit(PressureWork("BLOCKER", "pressure-blocker"))
        doomed = rig.defer(PressureWork("DOOMED", "pressure-doomed"))
        rig.crash_parent(doomed)

        follower = rig.defer(PressureWork("FOLLOWER", "pressure-follower"))
        rig.release(blocker)
        rig.require_started(follower)
        rig.release(follower)


def test_pressure_returning_fairness_group_starts_a_new_live_epoch(
    tmp_path: Path,
) -> None:
    """Completed service cannot penalize a group that later becomes live again."""
    pool_dir = tmp_path / "pool"
    with PressureRig(pool_dir, host_cpu_slots=1) as rig:
        for index in range(3):
            prior = rig.admit(PressureWork(f"PRIOR-{index}", "returning"))
            rig.release(prior)

        blocker = rig.admit(PressureWork("BLOCKER", "blocker"))
        returning = rig.defer(PressureWork("RETURNING", "returning"))
        fresh = rig.defer(PressureWork("FRESH", "fresh"))

        rig.release(blocker)
        rig.require_started(returning)
        rig.require_none_started((fresh,))
        rig.release(returning)
        rig.require_started(fresh)
        rig.release(fresh)


def test_pressure_opposite_exclusive_orders_make_progress_without_overlap(
    tmp_path: Path,
) -> None:
    """Non-blocking all-or-release acquisition prevents resource-order deadlock."""
    pool_dir = tmp_path / "pool"
    with PressureRig(pool_dir, host_cpu_slots=2) as rig:
        first = rig.admit(
            PressureWork(
                "FIRST",
                "pressure-first",
                exclusive_resources=("browser", "claude"),
            )
        )
        second = rig.defer(
            PressureWork(
                "SECOND",
                "pressure-second",
                exclusive_resources=("claude", "browser"),
            )
        )
        independent = rig.admit(
            PressureWork(
                "INDEPENDENT",
                "pressure-independent",
                exclusive_resources=("codex",),
            )
        )
        rig.require_none_started((second,))

        rig.release(first)
        rig.require_started(second)
        rig.release(second)
        rig.release(independent)


def test_pressure_simultaneous_completions_preserve_history_and_events(
    tmp_path: Path,
) -> None:
    """Concurrent writers must retain every observation without corrupting JSON."""
    pool_dir = tmp_path / "pool"
    with PressureRig(pool_dir, host_cpu_slots=4) as rig:
        processes = rig.submit_all(
            tuple(
                PressureWork(f"WRITER-{index}", f"pressure-writer-{index}")
                for index in range(20)
            )
        )
        remaining = list(processes)
        while remaining:
            cohort: list[PressureJob] = []
            while len(cohort) < min(4, len(remaining)):
                started = rig.require_next_started(
                    tuple(process for process in remaining if process not in cohort)
                )
                cohort.append(started)
            rig.complete_together(tuple(cohort))
            for job in cohort:
                remaining.remove(job)

        probe = rig.admit(PressureWork("PROBE", "pressure-probe"))
        rig.release(probe)

        timeline = _monitor(pool_dir, 4).recent_events(
            ExecutorRecentEventsQuery(limit=1000)
        )
        enqueued = tuple(
            event
            for event in timeline.events
            if isinstance(event, ExecutorWorkEnqueued)
            and event.work.work_key.value == "pressure:shared-work"
        )
        completed = tuple(
            event
            for event in timeline.events
            if isinstance(event, ExecutorWorkCompleted)
            and event.work.work_key.value == "pressure:shared-work"
        )
        assert len(enqueued) == len(processes) + 1
        assert len(completed) == len(processes) + 1
        assert enqueued[-1].successful_observation_count == len(processes)
        assert completed[-1].successful_observation_count == len(processes) + 1
        status = _monitor(pool_dir, 4).status(
            ExecutorStatusQuery(ExecutorAllRepositories(), 0, 20)
        )
        assert status.host_cpu_slots == 4
        assert status.learning.successful_observation_count == len(processes) + 1
        assert len(status.learning.learned_work) == 1
        assert status.learning.learned_work[0].work_key.value == (
            "pressure:shared-work"
        )
        assert len(status.learning.fingerprint_sha256) == 64


def test_pressure_queued_cohort_reserves_every_minimum_before_expansion(
    tmp_path: Path,
) -> None:
    """A queued cohort starts every fitting minimum before any job expands."""
    pool_dir = tmp_path / "pool"
    with PressureRig(pool_dir, host_cpu_slots=4) as rig:
        blocker = rig.admit(
            PressureWork(
                "BLOCKER",
                "pressure-blocker",
                concurrency_range=ExecutorConcurrencyRange(4, 4),
            )
        )
        processes = rig.defer_all(
            (
                PressureWork(
                    "WIDE",
                    "pressure-burst",
                    concurrency_range=ExecutorConcurrencyRange(2, 4),
                ),
                PressureWork(
                    "NARROW-A",
                    "pressure-burst",
                    concurrency_range=ExecutorConcurrencyRange(1, 4),
                ),
                PressureWork(
                    "NARROW-B",
                    "pressure-burst",
                    concurrency_range=ExecutorConcurrencyRange(1, 4),
                ),
            )
        )

        rig.release(blocker)
        rig.require_all_started(processes)
        rig.complete_together(processes)


def test_pressure_saturation_attenuates_then_recovers_without_restart(
    tmp_path: Path,
) -> None:
    """A queued command resumes after measured whole-host pressure clears."""
    pool_dir = tmp_path / "pool"
    with PressureRig(pool_dir, host_cpu_slots=4) as rig:
        rig.set_host_cpu_busy_percent(95.0)
        blocker = rig.admit(PressureWork("BLOCKER", "pressure-existing"))
        deferred = rig.defer(PressureWork("DEFERRED", "pressure-adaptive"))

        rig.set_host_cpu_busy_percent(0.0)
        rig.require_started(deferred)
        rig.release(blocker)
        rig.release(deferred)

        timeline = _monitor(pool_dir, 4).recent_events(
            ExecutorRecentEventsQuery(limit=100)
        )
        waiting = tuple(
            event
            for event in timeline.events
            if isinstance(event, ExecutorWorkWaiting)
            and event.work.fairness_group.value == deferred.work.group
        )
        assert any(
            event.reason is ExecutorWaitReason.HOST_PRESSURE
            and event.host_cpu_utilization.busy_percent == 95.0
            for event in waiting
        )
