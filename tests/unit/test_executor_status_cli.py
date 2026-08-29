"""Snapshot → operator output, and the exit code that goes with it.

Consumer side of the command surface. The renderer is a pure function of
a snapshot, so every state an operator can meet — busy pool, empty pool,
no pool, no journal, corrupt journal — is exercised directly rather than
through whatever the machine happens to have installed.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from issue_orchestrator.domain.lane_execution import LaneWorkKey
from issue_orchestrator.entrypoints.cli_tools import executor_status
from issue_orchestrator.entrypoints.cli_tools.executor_status import (
    main,
    render_executor_status,
)
from issue_orchestrator.execution.lane_backends import (
    BackendSource,
    SelectedBackend,
    UnknownBackend,
)
from issue_orchestrator.observation.executor_status import (
    DeclarationsRead,
    DeclarationsState,
    DeclarationsUnavailable,
    ExecutorStatusSnapshot,
    FactSource,
    LaneDispatchSummary,
    LaneRouting,
    LaneRow,
    SnapshotFault,
)
from issue_orchestrator.ports.executor_pool import (
    ForeignJobOrigin,
    LaneJobOrigin,
    PoolCapacity,
    PoolJob,
    PoolJobState,
    PoolOffline,
    PoolOnline,
    PoolState,
    PoolUnknownHealth,
)
from issue_orchestrator.ports.lane_dispatch_journal import LaneDispatchHistory
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



def _lane_job(
    work_key: str,
    state: PoolJobState,
    *,
    seconds: float = 30.0,
    cpus: int = 2,
    priority: int = 0,
    submitter: str = "issue-orchestrator-wt-alpha",
    exclusive: tuple[str, ...] = (),
) -> PoolJob:
    return PoolJob(
        origin=LaneJobOrigin(
            work_key=LaneWorkKey(work_key), submitter_worktree=submitter
        ),
        state=state,
        seconds_in_state=seconds,
        request_cpus=cpus,
        priority=priority,
        exclusive=exclusive,
    )


def _snapshot(
    *,
    pool: PoolState | None = None,
    lanes: tuple[LaneRow, ...] = (),
    faults: tuple[SnapshotFault, ...] = (),
    journal_location: str = "/repo/.git/issue-orchestrator/lane-dispatch.jsonl",
    declarations: DeclarationsState | None = None,
    records_scanned: int | None = None,
    records_predating_envelope: int = 0,
    backend=None,
) -> ExecutorStatusSnapshot:
    return ExecutorStatusSnapshot(
        captured_at=_NOW,
        backend=backend
        or SelectedBackend(name="condor", source=BackendSource.ENVIRONMENT),
        pool=pool or PoolOffline("no pool on this machine"),
        declarations=declarations
        or DeclarationsRead(path="/repo/.issue-orchestrator/lanes.yaml"),
        journal_location=journal_location,
        lanes=lanes,
        records_scanned=len(lanes) if records_scanned is None else records_scanned,
        records_predating_envelope=records_predating_envelope,
        faults=faults,
    )


def _row(
    work_key: str,
    *,
    routing: LaneRouting | None = None,
    history: LaneDispatchSummary | None = None,
) -> LaneRow:
    return LaneRow(
        work_key=LaneWorkKey(work_key),
        routing=routing
        if routing is not None
        else LaneRouting(
            request_cpus=2, memory_mb=1024, suspendability="anywhere", exclusive=()
        ),
        history=history,
    )


def _summary(
    work_key: str,
    *,
    runs: int = 3,
    runtime: float = 64.0,
    queue_wait: float = 3.5,
    exit_code: int = 0,
    learned_priority: int = 59,
) -> LaneDispatchSummary:
    return LaneDispatchSummary(
        work_key=LaneWorkKey(work_key),
        runs=runs,
        last_recorded_at=_NOW - timedelta(minutes=5),
        last_backend="condor",
        last_runtime_seconds=runtime,
        last_queue_wait_seconds=queue_wait,
        last_exit_code=exit_code,
        last_machine_state=_MACHINE_STATE,
        learned_priority=learned_priority,
    )


def test_a_busy_pool_shows_who_is_running_and_who_is_waiting_why() -> None:
    rendered = render_executor_status(
        _snapshot(
            pool=PoolOnline(
                capacity=PoolCapacity(machines=1, total_cpus=18),
                jobs=(
                    _lane_job(
                        "test-integration-core-local",
                        PoolJobState.QUEUED,
                        seconds=95.0,
                        priority=82,
                        submitter="issue-orchestrator-wt-beta",
                        exclusive=("codexlogin",),
                    ),
                    _lane_job(
                        "test-unit",
                        PoolJobState.RUNNING,
                        seconds=185.0,
                        cpus=3,
                        priority=59,
                    ),
                ),
            )
        )
    )

    assert (
        "POOL: online — 1 machine, 18 cpus, 3 in use; 1 running, 1 queued" in rendered
    )
    lines = rendered.splitlines()
    # Running first: it is what the queued job is waiting behind.
    running_line = next(line for line in lines if line.startswith("  running"))
    queued_line = next(line for line in lines if line.startswith("  queued"))
    assert lines.index(running_line) < lines.index(queued_line)
    assert "test-unit" in running_line
    assert "3m05s" in running_line
    assert "issue-orchestrator-wt-alpha" in running_line
    # The reason the other one is waiting is on its own row.
    assert "codexlogin" in queued_line
    assert "1m35s" in queued_line
    assert "issue-orchestrator-wt-beta" in queued_line


def test_a_held_job_is_named_rather_than_averaged_into_a_job_count() -> None:
    rendered = render_executor_status(
        _snapshot(
            pool=PoolOnline(
                capacity=PoolCapacity(machines=2, total_cpus=36),
                jobs=(_lane_job("test-unit", PoolJobState.HELD, seconds=7200.0),),
            )
        )
    )

    assert "2 machines" in rendered
    assert "0 in use; 1 held" in rendered
    assert "2h00m" in rendered


def test_a_foreign_job_is_shown_because_it_holds_the_same_cpus() -> None:
    rendered = render_executor_status(
        _snapshot(
            pool=PoolOnline(
                capacity=PoolCapacity(machines=1, total_cpus=8),
                jobs=(
                    PoolJob(
                        origin=ForeignJobOrigin(owner="someone-else"),
                        state=PoolJobState.RUNNING,
                        seconds_in_state=40.0,
                        request_cpus=4,
                        priority=0,
                        exclusive=(),
                    ),
                ),
            )
        )
    )

    assert "(not a lane)" in rendered
    assert "someone-else (other user)" in rendered
    assert "4 in use" in rendered


def test_an_idle_pool_says_so_instead_of_printing_an_empty_table() -> None:
    rendered = render_executor_status(
        _snapshot(
            pool=PoolOnline(
                capacity=PoolCapacity(machines=1, total_cpus=18), jobs=()
            )
        )
    )

    assert "nothing in the queue" in rendered
    assert "(nothing queued or running)" in rendered


def test_no_pool_is_announced_with_its_reason_not_left_blank() -> None:
    """Degrade loudly: an absent pool must never read as an idle one."""
    rendered = render_executor_status(
        _snapshot(pool=PoolOffline("the direct backend has no machine-wide pool"))
    )

    assert "POOL: unavailable" in rendered
    assert "the direct backend has no machine-wide pool" in rendered
    assert "online" not in rendered


def test_dispatch_history_names_its_file_and_the_cost_of_each_lane() -> None:
    rendered = render_executor_status(
        _snapshot(
            lanes=(
                _row(
                    "test-unit",
                    history=_summary("test-unit", runs=9, runtime=64.0, queue_wait=3.5),
                ),
                _row(
                    "typecheck",
                    history=_summary(
                        "typecheck",
                        runs=9,
                        runtime=11.0,
                        queue_wait=52.0,
                        learned_priority=12,
                        exit_code=1,
                    ),
                ),
            ),
            records_scanned=18,
        )
    )

    assert "/repo/.git/issue-orchestrator/lane-dispatch.jsonl" in rendered
    assert "18 record(s) scanned" in rendered
    unit_line = next(line for line in rendered.splitlines() if "test-unit" in line)
    assert "1m04s" in unit_line
    assert "3.5s" in unit_line
    assert "59" in unit_line
    typecheck_line = next(line for line in rendered.splitlines() if "typecheck" in line)
    assert "52.0s" in typecheck_line
    assert "2026-08-28 11:55:00Z" in typecheck_line


def test_an_empty_journal_says_where_records_will_appear() -> None:
    """No journal → say so, and say which file to look at later."""
    rendered = render_executor_status(_snapshot(lanes=()))

    assert "/repo/.git/issue-orchestrator/lane-dispatch.jsonl" in rendered
    assert "no lanes" in rendered


def test_no_repository_reports_that_nothing_is_persisted() -> None:
    rendered = render_executor_status(
        _snapshot(journal_location=LaneDispatchHistory(
            location="not persisted: this is not a repository", entries=()
        ).location)
    )

    assert "not persisted: this is not a repository" in rendered


def test_faults_are_printed_as_faults_not_folded_into_the_tables() -> None:
    rendered = render_executor_status(
        _snapshot(
            lanes=(_row("test-unit", history=_summary("test-unit")),),
            faults=(
                SnapshotFault(
                    source=FactSource.DISPATCH_JOURNAL, detail="line 4 is corrupt"
                ),
            ),
        )
    )

    assert "FAULTS:" in rendered
    assert "! dispatch journal: line 4 is corrupt" in rendered
    # The facts that did survive are still shown.
    assert "test-unit" in rendered


def test_the_backend_in_play_is_stated_up_front() -> None:
    """The backend AND what established it: an operator reading the
    wrong one needs to know which knob to turn (finding 1, #7138)."""
    rendered = render_executor_status(
        _snapshot(
            backend=SelectedBackend(
                name="condor", source=BackendSource.VALIDATION_COMMAND
            )
        )
    )
    assert rendered.splitlines()[0] == (
        "Executor pool — backend condor (from repository validation command), "
        "captured 2026-08-28 12:00:00Z"
    )


def test_the_renderer_refuses_anything_that_is_not_a_snapshot() -> None:
    with pytest.raises(ValueError, match="ExecutorStatusSnapshot"):
        render_executor_status({"pool": "online"})  # type: ignore[arg-type]


class _StubInspector:
    def inspect(self) -> PoolState:
        return PoolOffline("no pool here")


class _StubJournalReader:
    def read_recent(self, limit: int) -> LaneDispatchHistory:
        del limit
        return LaneDispatchHistory(location="/repo/lane-dispatch.jsonl", entries=())


class _BrokenJournalReader:
    def read_recent(self, limit: int):
        del limit
        from issue_orchestrator.ports.lane_dispatch_journal import (
            LaneDispatchJournalError,
        )

        raise LaneDispatchJournalError("line 2 is corrupt")


class _StubRuntimeHistory:
    def record_success(self, work_key: LaneWorkKey, runtime_seconds: float) -> None:
        raise AssertionError("a read-only command must never write history")

    def learned_priority(self, work_key: LaneWorkKey) -> int:
        del work_key
        return 0


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire the command to doubles, never to this machine's real pool."""
    monkeypatch.setattr(
        executor_status, "build_pool_inspector", lambda backend: _StubInspector()
    )
    monkeypatch.setattr(
        executor_status, "build_dispatch_journal_reader", _StubJournalReader
    )
    monkeypatch.setattr(
        executor_status, "build_runtime_history", _StubRuntimeHistory
    )


def test_a_rendered_snapshot_exits_zero_even_when_degraded(
    wired: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Absence is the normal state of an opt-in backend, not a failure."""
    assert main([]) == 0
    assert "POOL: unavailable" in capsys.readouterr().out


def test_a_broken_input_exits_nonzero_so_a_script_notices(
    wired: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        executor_status, "build_dispatch_journal_reader", _BrokenJournalReader
    )

    assert main([]) == 70
    assert "line 2 is corrupt" in capsys.readouterr().out


def test_an_unknown_backend_is_a_configuration_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A misspelled backend setting must name itself, not crash."""
    from issue_orchestrator.execution.lane_backends import (
        BACKEND_ENVIRONMENT_VARIABLE,
    )

    monkeypatch.setenv(BACKEND_ENVIRONMENT_VARIABLE, "slurm")

    assert main([]) == 78
    assert "slurm" in capsys.readouterr().err


def test_the_scan_window_is_a_flag_and_must_be_positive(
    wired: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--scan", "5"]) == 0
    with pytest.raises(SystemExit):
        main(["--scan", "0"])
    assert "positive" in capsys.readouterr().err


# --- #7138 round 1: what the operator must be able to READ -----------


def test_an_unknown_backend_is_printed_as_unknown_never_guessed() -> None:
    """`backend direct` on a scheduler-backed repo is worse than
    admitting ignorance, because it looks like an answer (finding 1)."""
    rendered = render_executor_status(
        _snapshot(backend=UnknownBackend(reason="nothing establishes it"))
    )

    assert "backend UNKNOWN" in rendered.splitlines()[0]
    assert "direct" not in rendered.splitlines()[0]


def test_the_backend_each_lane_last_ran_on_is_visible() -> None:
    """History that contradicts the header is the clearest signal that
    the selected backend is not the one in use (finding 1)."""
    rendered = render_executor_status(
        _snapshot(
            backend=SelectedBackend(name="direct", source=BackendSource.FLAG),
            lanes=(_row("test-unit", history=_summary("test-unit")),),
        )
    )

    lane_line = next(
        line for line in rendered.splitlines() if line.startswith("  test-unit")
    )
    assert "condor" in lane_line, "the lane's actual backend must be shown"


def test_a_declared_lane_that_never_ran_is_rendered_as_never() -> None:
    rendered = render_executor_status(
        _snapshot(
            lanes=(
                _row(
                    "execenv.memory-oom",
                    routing=LaneRouting(
                        request_cpus=1,
                        memory_mb=128,
                        suspendability="never",
                        exclusive=(),
                    ),
                ),
            )
        )
    )

    line = next(
        line
        for line in rendered.splitlines()
        if line.startswith("  execenv.memory-oom")
    )
    assert "128" in line, "declared memory must be visible"
    assert "never" in line, "a lane that never ran must say so"


def test_routing_facts_are_rendered_for_each_lane() -> None:
    rendered = render_executor_status(
        _snapshot(
            lanes=(
                _row(
                    "test-unit",
                    routing=LaneRouting(
                        request_cpus=8,
                        memory_mb=6144,
                        suspendability="never",
                        exclusive=("codexlogin",),
                    ),
                    history=_summary("test-unit"),
                ),
            )
        )
    )

    line = next(
        line for line in rendered.splitlines() if line.startswith("  test-unit")
    )
    assert "8" in line and "6144" in line and "codexlogin" in line
    assert "never" in line, "a non-suspendable lane must say it never freezes"


def test_unreadable_declarations_are_announced_in_the_output() -> None:
    """A missing lanes.yaml must not read like an unused journal."""
    rendered = render_executor_status(
        _snapshot(
            declarations=DeclarationsUnavailable(
                detail="lane declarations file not found: /repo/lanes.yaml"
            ),
            faults=(
                SnapshotFault(
                    source=FactSource.LANE_DECLARATIONS,
                    detail="lane declarations file not found: /repo/lanes.yaml",
                ),
            ),
        )
    )

    assert "declarations UNREADABLE" in rendered
    assert "lane declarations: lane declarations file not found" in rendered


def test_a_pool_of_unknown_health_is_never_rendered_as_online() -> None:
    """Stale or empty capacity must not read as an idle pool (finding 3)."""
    rendered = render_executor_status(
        _snapshot(
            pool=PoolUnknownHealth(
                capacity=PoolCapacity(machines=1, total_cpus=18),
                jobs=(),
                detail="this record is STALE and its capacity may belong to a "
                "machine that is gone",
            )
        )
    )

    assert "POOL: health UNKNOWN" in rendered
    assert "STALE" in rendered
    assert "POOL: online" not in rendered
    # The numbers it did report are still shown, marked as its claim.
    assert "it reported: 1 machine, 18 cpus" in rendered


# --- properties that held in round 1 and must keep holding -----------


def test_rendering_mutates_nothing_it_was_given() -> None:
    """A status command that edits its own inputs is a status command
    nobody can run twice."""
    import copy

    snapshot = _snapshot(
        pool=PoolOnline(
            capacity=PoolCapacity(machines=1, total_cpus=18),
            jobs=(_lane_job("test-unit", PoolJobState.RUNNING),),
        ),
        lanes=(_row("test-unit", history=_summary("test-unit")),),
    )
    before = copy.deepcopy(snapshot)

    render_executor_status(snapshot)
    render_executor_status(snapshot)

    assert snapshot == before


# --- the widened journal schema (#7135) reaches the operator ---------


def test_the_contention_a_runtime_was_measured_under_is_shown_beside_it() -> None:
    """A duration without its covariate is the ambiguity #7135 ended.

    This row's headline number is a RUNTIME, so dropping the machine
    state at the summary boundary would rebuild that ambiguity in the
    operator's view — the reader would be back to guessing whether a
    slow lane was slow or merely crowded.
    """
    rendered = render_executor_status(
        _snapshot(lanes=(_row("test-unit", history=_summary("test-unit")),))
    )

    line = next(
        line for line in rendered.splitlines() if line.startswith("  test-unit")
    )
    assert "86%" in line, "the host idle share must travel with the runtime"
    assert "IDLE" in rendered


def test_a_failed_probe_reads_as_unknown_not_as_a_number() -> None:
    """The envelope's own rule: an invented figure is worse than a gap."""
    blind = MachineState(
        sampled_at=_NOW,
        loadavg_1m=None,
        loadavg_5m=None,
        loadavg_15m=None,
        cpu_idle_percent=None,
        cpu_idle_source="probe failed",
        physical_cores=None,
        probe_error="probe host melted",
    )
    summary = dataclasses.replace(
        _summary("test-unit"), last_machine_state=blind
    )
    rendered = render_executor_status(
        _snapshot(lanes=(_row("test-unit", history=summary),))
    )

    line = next(
        line for line in rendered.splitlines() if line.startswith("  test-unit")
    )
    assert "?" in line
    assert "0%" not in line, "a failed probe must not read as a pegged host"


def test_the_three_valued_freeze_classification_is_shown_verbatim() -> None:
    """`cooperative` is a different promise from `anywhere` (#7134)."""
    rendered = render_executor_status(
        _snapshot(
            lanes=(
                _row(
                    "test-unit",
                    routing=LaneRouting(
                        request_cpus=2,
                        memory_mb=1024,
                        suspendability="cooperative",
                        exclusive=(),
                    ),
                    history=_summary("test-unit"),
                ),
            )
        )
    )

    assert "cooperative" in rendered


def test_contention_never_reorders_the_lanes() -> None:
    """The envelope is a measurement: nothing may order on it (#7135).

    A crowded lane and an idle one with the same learned priority must
    keep the name tiebreak, not sort by how busy the host happened to be.
    """
    crowded = dataclasses.replace(
        _summary("aaa-lane", learned_priority=50),
        last_machine_state=dataclasses.replace(
            _MACHINE_STATE, cpu_idle_percent=2.0
        ),
    )
    idle = dataclasses.replace(_summary("zzz-lane", learned_priority=50))
    rendered = render_executor_status(
        _snapshot(
            lanes=(
                _row("aaa-lane", history=crowded),
                _row("zzz-lane", history=idle),
            )
        )
    )

    lines = [line for line in rendered.splitlines() if line.startswith("  aaa") or line.startswith("  zzz")]
    assert lines[0].startswith("  aaa-lane"), "order must stay the name tiebreak"


def test_history_thinned_by_older_rows_says_so() -> None:
    """Counting skipped rows into the scan total without saying they were
    skipped would overstate the sample behind every runtime shown."""
    rendered = render_executor_status(
        _snapshot(
            lanes=(_row("test-unit", history=_summary("test-unit")),),
            records_scanned=400,
            records_predating_envelope=360,
        )
    )

    assert "400 record(s) scanned" in rendered
    assert "360 skipped as older than the machine-state envelope" in rendered


def test_a_journal_with_nothing_older_says_nothing_extra() -> None:
    rendered = render_executor_status(
        _snapshot(
            lanes=(_row("test-unit", history=_summary("test-unit")),),
            records_scanned=40,
        )
    )

    assert "40 record(s) scanned" in rendered
    assert "skipped" not in rendered
