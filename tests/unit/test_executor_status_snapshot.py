"""Fact sources → snapshot: the join, and what happens when one breaks.

Producer side of the command surface. Every input is a port double, so
what is under test is the owner that joins them and its degradation
policy — not any adapter's storage or transport.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from issue_orchestrator.domain.lane_cpu_request import LaneCpuRequest
from issue_orchestrator.domain.lane_execution import LaneWorkKey
from issue_orchestrator.execution.lane_backends import (
    BackendSource,
    SelectedBackend,
    UnknownBackend,
)
from issue_orchestrator.infra.lane_declarations import (
    LaneDeclaration,
    LaneDeclarationError,
    LaneDeclarations,
)
from issue_orchestrator.observation.executor_status import (
    DeclarationsRead,
    DeclarationsUnavailable,
    FactSource,
    build_executor_status_snapshot,
)
from issue_orchestrator.ports.executor_pool import (
    LaneJobOrigin,
    PoolCapacity,
    PoolInspectionError,
    PoolJob,
    PoolJobState,
    PoolOffline,
    PoolOnline,
    PoolState,
)
from issue_orchestrator.ports.lane_dispatch_journal import (
    LaneDispatchEntry,
    LaneDispatchHistory,
    LaneDispatchJournalError,
    LaneDispatchRecord,
)
from issue_orchestrator.ports.lane_runtime_history import LaneRuntimeHistoryError
from issue_orchestrator.ports.machine_state import MachineState

_NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

_MACHINE_STATE = MachineState(
    sampled_at=datetime(2026, 8, 28, 11, 59, 0, tzinfo=timezone.utc),
    loadavg_1m=7.91,
    loadavg_5m=12.51,
    loadavg_15m=9.0,
    cpu_idle_percent=85.68,
    cpu_idle_source="host_statistics(HOST_CPU_LOAD_INFO) over 0.1s",
    physical_cores=18,
    probe_error=None,
)



class _StaticInspector:
    def __init__(self, state: PoolState) -> None:
        self._state = state

    def inspect(self) -> PoolState:
        return self._state


class _RaisingInspector:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def inspect(self) -> PoolState:
        raise self._error


class _StaticJournalReader:
    def __init__(self, history: LaneDispatchHistory) -> None:
        self._history = history
        self.limits: list[int] = []

    def read_recent(self, limit: int) -> LaneDispatchHistory:
        self.limits.append(limit)
        return self._history


class _RaisingJournalReader:
    def read_recent(self, limit: int) -> LaneDispatchHistory:
        del limit
        raise LaneDispatchJournalError("line 4 is corrupt")


class _StaticRuntimeHistory:
    def __init__(self, priorities: dict[str, int] | None = None) -> None:
        self._priorities = priorities or {}

    def record_success(self, work_key: LaneWorkKey, runtime_seconds: float) -> None:
        raise AssertionError("a read-only snapshot must never write history")

    def learned_priority(self, work_key: LaneWorkKey) -> int:
        return self._priorities.get(work_key.value, 0)


class _BrokenRuntimeHistory(_StaticRuntimeHistory):
    def learned_priority(self, work_key: LaneWorkKey) -> int:
        raise LaneRuntimeHistoryError(f"history for {work_key.value} is corrupt")


def _entry(
    work_key: str,
    *,
    minutes_ago: int = 0,
    runtime: float = 30.0,
    queue_wait: float = 0.0,
    exit_code: int = 0,
    priority: int = 0,
) -> LaneDispatchEntry:
    return LaneDispatchEntry(
        recorded_at=_NOW - timedelta(minutes=minutes_ago),
        worktree="issue-orchestrator-wt-alpha",
        record=LaneDispatchRecord(
            work_key=LaneWorkKey(work_key),
            backend="condor",
            priority=priority,
            queue_wait_seconds=queue_wait,
            observed_runtime_seconds=runtime,
            exit_code=exit_code,
            machine_state=_MACHINE_STATE,
            # The snapshot summarizes runtimes and dispatch order, not
            # the sizing decision, so these carry the shape a real
            # record has rather than anything this file asserts on.
            cpu_request=LaneCpuRequest.resolve(4, None),
            observed_busy_cores=None,
        ),
    )


def _history(*entries: LaneDispatchEntry) -> LaneDispatchHistory:
    return LaneDispatchHistory(
        location="/repo/.git/lane-dispatch.jsonl", entries=entries
    )


_CONDOR = SelectedBackend(name="condor", source=BackendSource.ENVIRONMENT)


def _declaration(request_cpus: int = 2) -> LaneDeclaration:
    return LaneDeclaration(
        request_cpus=request_cpus, memory_mb=1024, suspendability="anywhere", exclusive=()
    )


def _declarations(*work_keys: str) -> LaneDeclarations:
    return LaneDeclarations(lanes={key: _declaration() for key in work_keys})


def _refuse_declarations() -> LaneDeclarations:
    raise LaneDeclarationError("lane declarations file not found: /repo/lanes.yaml")


def _build(
    *,
    pool: PoolState | None = None,
    inspector: object | None = None,
    journal: object | None = None,
    runtime_history: object | None = None,
    declarations=None,
    backend=None,
    recent_limit: int = 50,
):
    resolved = inspector or _StaticInspector(pool or PoolOffline("no pool here"))
    return build_executor_status_snapshot(
        backend=_CONDOR if backend is None else backend,
        inspector_for=lambda name: resolved,
        declarations_reader=(
            declarations if callable(declarations) else lambda: _declarations()
        ),
        declarations_location="/repo/.issue-orchestrator/lanes.yaml",
        journal_reader=journal or _StaticJournalReader(_history()),
        runtime_history=runtime_history or _StaticRuntimeHistory(),
        captured_at=_NOW,
        recent_limit=recent_limit,
    )


def _online(*jobs: PoolJob) -> PoolOnline:
    return PoolOnline(
        capacity=PoolCapacity(machines=1, total_cpus=18), jobs=tuple(jobs)
    )


def _job(work_key: str, state: PoolJobState, seconds: float, cpus: int) -> PoolJob:
    return PoolJob(
        origin=LaneJobOrigin(
            work_key=LaneWorkKey(work_key), submitter_worktree="wt-alpha"
        ),
        state=state,
        seconds_in_state=seconds,
        request_cpus=cpus,
        priority=0,
        exclusive=(),
    )


def test_the_pool_and_the_journal_are_joined_into_one_snapshot() -> None:
    snapshot = _build(
        pool=_online(_job("test-unit", PoolJobState.RUNNING, 12.0, 3)),
        journal=_StaticJournalReader(
            _history(_entry("test-unit", runtime=64.0, queue_wait=3.5))
        ),
        runtime_history=_StaticRuntimeHistory({"test-unit": 59}),
    )

    assert snapshot.backend == _CONDOR
    assert snapshot.captured_at == _NOW
    assert type(snapshot.pool) is PoolOnline
    assert snapshot.journal_location == "/repo/.git/lane-dispatch.jsonl"
    assert snapshot.records_scanned == 1
    assert snapshot.faults == ()
    assert snapshot.is_degraded is False
    lane = snapshot.lanes[0].history
    assert lane.work_key == LaneWorkKey("test-unit")
    assert lane.runs == 1
    assert lane.last_runtime_seconds == 64.0
    assert lane.last_queue_wait_seconds == 3.5
    assert lane.learned_priority == 59


def test_each_lane_is_summarized_from_its_most_recent_record() -> None:
    """Repeated runs collapse to one row: the newest facts plus a count."""
    snapshot = _build(
        journal=_StaticJournalReader(
            _history(
                _entry("test-unit", minutes_ago=30, runtime=10.0, exit_code=1),
                _entry("typecheck", minutes_ago=20, runtime=5.0),
                _entry("test-unit", minutes_ago=10, runtime=64.0, exit_code=0),
            )
        ),
        runtime_history=_StaticRuntimeHistory({"test-unit": 59, "typecheck": 12}),
    )

    lanes = {row.work_key.value: row.history for row in snapshot.lanes}
    assert lanes["test-unit"].runs == 2
    assert lanes["test-unit"].last_runtime_seconds == 64.0
    assert lanes["test-unit"].last_exit_code == 0
    assert lanes["test-unit"].last_recorded_at == _NOW - timedelta(minutes=10)
    assert lanes["typecheck"].runs == 1


def test_lanes_are_ordered_the_way_the_next_gate_will_dispatch_them() -> None:
    """Highest learned priority first — the LPT order lane-run uses."""
    snapshot = _build(
        journal=_StaticJournalReader(
            _history(_entry("typecheck"), _entry("test-unit"), _entry("test-web"))
        ),
        runtime_history=_StaticRuntimeHistory(
            {"typecheck": 12, "test-unit": 59, "test-web": 59}
        ),
    )

    # Equal priorities break ties by name so repeated snapshots are stable.
    assert [lane.work_key.value for lane in snapshot.lanes] == [
        "test-unit",
        "test-web",
        "typecheck",
    ]


def test_the_scan_window_is_passed_through_to_the_journal() -> None:
    reader = _StaticJournalReader(_history())
    _build(journal=reader, recent_limit=7)
    assert reader.limits == [7]


def test_an_absent_pool_still_yields_the_journal_facts() -> None:
    """The whole point: no pool must not mean no answer."""
    snapshot = _build(
        pool=PoolOffline("no scheduler tools on PATH"),
        journal=_StaticJournalReader(_history(_entry("test-unit"))),
    )

    assert type(snapshot.pool) is PoolOffline
    assert snapshot.pool.detail == "no scheduler tools on PATH"
    assert [lane.work_key.value for lane in snapshot.lanes] == ["test-unit"]
    # Absence is not a fault: an opt-in backend that is off is normal.
    assert snapshot.faults == ()
    assert snapshot.is_degraded is False


def test_an_unintelligible_pool_is_a_fault_and_the_journal_survives() -> None:
    snapshot = _build(
        inspector=_RaisingInspector(PoolInspectionError("job status 99 is unknown")),
        journal=_StaticJournalReader(_history(_entry("test-unit"))),
    )

    assert snapshot.is_degraded is True
    assert [fault.source for fault in snapshot.faults] == [FactSource.POOL]
    assert "job status 99" in snapshot.faults[0].detail
    # The offline reason repeats the cause rather than reading as "idle".
    assert type(snapshot.pool) is PoolOffline
    assert "unintelligibly" in snapshot.pool.detail
    assert [lane.work_key.value for lane in snapshot.lanes] == ["test-unit"]


def test_a_corrupt_journal_is_a_fault_and_the_pool_survives() -> None:
    snapshot = _build(
        pool=_online(_job("test-unit", PoolJobState.RUNNING, 12.0, 3)),
        journal=_RaisingJournalReader(),
    )

    assert type(snapshot.pool) is PoolOnline
    assert snapshot.lanes == ()
    assert snapshot.records_scanned == 0
    assert [fault.source for fault in snapshot.faults] == [
        FactSource.DISPATCH_JOURNAL
    ]
    assert "line 4 is corrupt" in snapshot.journal_location


def test_a_broken_learning_store_drops_the_rows_rather_than_faking_order() -> None:
    """Printing priority 0 for every lane would be a lie about the order.

    The next gate will not dispatch in that order, so the rows are
    withheld and the reason is stated instead.
    """
    snapshot = _build(
        journal=_StaticJournalReader(_history(_entry("test-unit"))),
        runtime_history=_BrokenRuntimeHistory(),
    )

    assert snapshot.lanes == ()
    assert [fault.source for fault in snapshot.faults] == [
        FactSource.RUNTIME_HISTORY
    ]
    assert "corrupt" in snapshot.faults[0].detail


def test_an_empty_journal_is_not_a_fault() -> None:
    snapshot = _build(journal=_StaticJournalReader(_history()))

    assert snapshot.lanes == ()
    assert snapshot.records_scanned == 0
    assert snapshot.faults == ()


def test_the_captured_moment_must_be_unambiguous() -> None:
    with pytest.raises(ValueError, match="captured_at"):
        build_executor_status_snapshot(
            backend=_CONDOR,
            inspector_for=lambda name: _StaticInspector(PoolOffline("none")),
            declarations_reader=_declarations,
            declarations_location="/repo/lanes.yaml",
            journal_reader=_StaticJournalReader(_history()),
            runtime_history=_StaticRuntimeHistory(),
            captured_at=datetime(2026, 8, 28, 12, 0, 0),
        )


def test_a_meaningless_scan_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="recent_limit"):
        _build(recent_limit=0)


def test_snapshot_faults_must_name_a_source_and_a_reason() -> None:
    from issue_orchestrator.observation.executor_status import SnapshotFault

    with pytest.raises(ValueError, match="detail"):
        SnapshotFault(source=FactSource.POOL, detail="")
    with pytest.raises(ValueError, match="FactSource"):
        SnapshotFault(source="pool", detail="broken")  # type: ignore[arg-type]


# --- #7138 round 1, finding 2: declarations never observed -----------


def test_a_declared_lane_that_never_ran_still_appears(tmp_path: Path) -> None:
    """Only the declarations can mention a lane with no history.

    A journal-only view silently omits every lane that has not run —
    exactly the lanes an operator is most likely to be asking about
    (finding 2, #7138).
    """
    snapshot = _build(
        declarations=lambda: _declarations("test-unit", "execenv.memory-oom"),
        journal=_StaticJournalReader(_history(_entry("test-unit"))),
    )

    rows = {row.work_key.value: row for row in snapshot.lanes}
    assert set(rows) == {"test-unit", "execenv.memory-oom"}
    assert rows["execenv.memory-oom"].history is None
    assert rows["execenv.memory-oom"].routing is not None
    # Never-run lanes sort last: nothing is known about their cost.
    assert snapshot.lanes[-1].work_key.value == "execenv.memory-oom"


def test_routing_facts_come_from_the_canonical_declarations() -> None:
    """cpus/memory/suspendability/exclusives are shown, not re-derived."""
    declaration = LaneDeclaration(
        request_cpus=8, memory_mb=6144, suspendability="never", exclusive=("codexlogin",)
    )
    snapshot = _build(
        declarations=lambda: LaneDeclarations(lanes={"test-unit": declaration}),
        journal=_StaticJournalReader(_history(_entry("test-unit"))),
    )

    routing = snapshot.lanes[0].routing
    assert routing is not None
    assert routing.request_cpus == 8
    assert routing.memory_mb == 6144
    assert routing.suspendability == "never"
    assert routing.exclusive == ("codexlogin",)


def test_unreadable_declarations_are_a_loud_fault_not_an_empty_repo() -> None:
    """Missing or malformed declarations must be distinguishable from
    an unused journal: every lane is unrunnable until it is fixed
    (finding 2, #7138)."""
    snapshot = _build(
        declarations=_refuse_declarations,
        journal=_StaticJournalReader(_history(_entry("test-unit"))),
    )

    assert type(snapshot.declarations) is DeclarationsUnavailable
    assert [fault.source for fault in snapshot.faults] == [
        FactSource.LANE_DECLARATIONS
    ]
    assert "not found" in snapshot.faults[0].detail
    assert snapshot.is_degraded is True
    # The journal side still survives.
    assert [row.work_key.value for row in snapshot.lanes] == ["test-unit"]


def test_readable_declarations_say_where_they_were_read() -> None:
    snapshot = _build(declarations=lambda: _declarations("test-unit"))
    assert type(snapshot.declarations) is DeclarationsRead
    assert snapshot.declarations.path == "/repo/.issue-orchestrator/lanes.yaml"


def test_a_journaled_lane_missing_from_declarations_is_still_shown() -> None:
    """It ran once and cannot run again — that is worth a row."""
    snapshot = _build(
        declarations=lambda: _declarations(),
        journal=_StaticJournalReader(_history(_entry("test-unit"))),
    )

    assert snapshot.lanes[0].work_key.value == "test-unit"
    assert snapshot.lanes[0].routing is None
    assert snapshot.lanes[0].history is not None


# --- #7138 round 1, finding 1: wrong active backend ------------------


def test_an_unknown_backend_yields_no_pool_and_says_why() -> None:
    """No guess, and no silently empty pool section either."""
    snapshot = _build(backend=UnknownBackend(reason="nothing establishes it"))

    assert type(snapshot.backend) is UnknownBackend
    assert type(snapshot.pool) is PoolOffline
    assert "nothing establishes it" in snapshot.pool.detail


def test_the_pool_is_inspected_for_the_selected_backend() -> None:
    """The snapshot must not inspect a backend nobody selected."""
    asked: list[str] = []

    def inspector_for(name: str):
        asked.append(name)
        return _StaticInspector(PoolOffline("none"))

    build_executor_status_snapshot(
        backend=SelectedBackend(name="condor", source=BackendSource.FLAG),
        inspector_for=inspector_for,
        declarations_reader=_declarations,
        declarations_location="/repo/lanes.yaml",
        journal_reader=_StaticJournalReader(_history()),
        runtime_history=_StaticRuntimeHistory(),
        captured_at=_NOW,
    )

    assert asked == ["condor"]
