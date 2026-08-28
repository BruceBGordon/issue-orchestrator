"""Snapshot → operator output, and the exit code that goes with it.

Consumer side of the command surface. The renderer is a pure function of
a snapshot, so every state an operator can meet — busy pool, empty pool,
no pool, no journal, corrupt journal — is exercised directly rather than
through whatever the machine happens to have installed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_orchestrator.domain.lane_execution import LaneWorkKey
from issue_orchestrator.entrypoints.cli_tools import executor_status
from issue_orchestrator.entrypoints.cli_tools.executor_status import (
    main,
    render_executor_status,
)
from issue_orchestrator.observation.executor_status import (
    ExecutorStatusSnapshot,
    FactSource,
    LaneDispatchSummary,
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
)
from issue_orchestrator.ports.lane_dispatch_journal import LaneDispatchHistory

_NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)


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
    lanes: tuple[LaneDispatchSummary, ...] = (),
    faults: tuple[SnapshotFault, ...] = (),
    journal_location: str = "/repo/.git/issue-orchestrator/lane-dispatch.jsonl",
    records_scanned: int | None = None,
    backend: str = "condor",
) -> ExecutorStatusSnapshot:
    return ExecutorStatusSnapshot(
        captured_at=_NOW,
        backend=backend,
        pool=pool or PoolOffline("no pool on this machine"),
        journal_location=journal_location,
        lanes=lanes,
        records_scanned=len(lanes) if records_scanned is None else records_scanned,
        faults=faults,
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
                _summary("test-unit", runs=9, runtime=64.0, queue_wait=3.5),
                _summary(
                    "typecheck",
                    runs=9,
                    runtime=11.0,
                    queue_wait=52.0,
                    learned_priority=12,
                    exit_code=1,
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
    assert "no dispatch records" in rendered


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
            lanes=(_summary("test-unit"),),
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
    rendered = render_executor_status(_snapshot(backend="direct"))
    assert rendered.splitlines()[0] == (
        "Executor pool — backend direct, captured 2026-08-28 12:00:00Z"
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
