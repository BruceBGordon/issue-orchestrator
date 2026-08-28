# pyright: strict
"""Render a read-only snapshot of the machine-wide executor pool.

Answers one operator question — *why is validation work running or
waiting?* — by joining what the pool is doing right now with what recent
lanes have cost. Strictly read-only: nothing here submits, removes, or
reprioritizes anything.

The rendering is a pure function of the snapshot so the same facts can
later drive a UI panel without reimplementing the join, and so both
halves of the boundary (facts to snapshot, snapshot to output) are
testable on their own.

Exit codes: 0 when a snapshot was rendered, including a degraded one
whose missing pieces are simply absent; 70 (software) when an input was
*broken* rather than empty, because corrupt records mean something wrote
garbage and a status command must not shrug that off; 78
(configuration) when the selected backend does not exist at all.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Sequence

from ...domain.lane_execution import LaneExecutorUnavailableError
from ...execution.lane_backends import (
    BACKEND_ENVIRONMENT_VARIABLE,
    BACKEND_NAMES,
    DIRECT_BACKEND,
    build_pool_inspector,
)
from ...observation.executor_status import (
    DEFAULT_RECENT_DISPATCH_LIMIT,
    ExecutorStatusSnapshot,
    LaneDispatchSummary,
    build_executor_status_snapshot,
)
from ...ports.executor_pool import (
    ForeignJobOrigin,
    LaneJobOrigin,
    PoolJob,
    PoolJobState,
    PoolOffline,
    PoolOnline,
)
from .lane_run import build_dispatch_journal_reader, build_runtime_history

_FAULT_EXIT_CODE = 70
# Matches lane-run: 78 is the configuration-error exit code.
_MISCONFIGURED_EXIT_CODE = 78
# Column indexes whose cells are numbers, so digits line up.
_JOB_NUMERIC_COLUMNS = frozenset({1, 2, 3})
_LANE_NUMERIC_COLUMNS = frozenset({1, 2, 3, 4, 5})
# Ordered so the rows an operator is waiting on come first.
_JOB_STATE_ORDER = (
    PoolJobState.RUNNING,
    PoolJobState.SUSPENDED,
    PoolJobState.QUEUED,
    PoolJobState.HELD,
    PoolJobState.FINISHING,
)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    backend = str(arguments.backend)
    try:
        inspector = build_pool_inspector(backend)
    except LaneExecutorUnavailableError as error:
        # A backend nobody implements is a misconfiguration, not a
        # missing pool: say which setting is wrong instead of printing a
        # snapshot of a backend that does not exist.
        print(f"executor-status: {error}", file=sys.stderr)
        return _MISCONFIGURED_EXIT_CODE
    snapshot = build_executor_status_snapshot(
        inspector=inspector,
        journal_reader=build_dispatch_journal_reader(),
        runtime_history=build_runtime_history(),
        backend=backend,
        captured_at=datetime.now(timezone.utc),
        recent_limit=int(arguments.scan),
    )
    print(render_executor_status(snapshot))
    return _FAULT_EXIT_CODE if snapshot.is_degraded else 0


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="issue-orchestrator executor-status",
        description=(
            "Show the machine-wide validation-lane executor pool: capacity, "
            "what is running and queued, and what recent lanes have cost."
        ),
    )
    # Named exactly as the other lane entrypoints name it, defaulting
    # the same way, so the snapshot reports on the backend the gate
    # would actually use.
    parser.add_argument(
        "--backend",
        choices=BACKEND_NAMES,
        default=os.environ.get(BACKEND_ENVIRONMENT_VARIABLE, DIRECT_BACKEND),
        help=(
            "Backend to report on; defaults to "
            f"${BACKEND_ENVIRONMENT_VARIABLE} or '{DIRECT_BACKEND}'."
        ),
    )
    parser.add_argument(
        "--scan",
        type=_positive_integer,
        default=DEFAULT_RECENT_DISPATCH_LIMIT,
        metavar="RECORDS",
        help=(
            "How many of the most recent dispatch records to summarize "
            f"(default: {DEFAULT_RECENT_DISPATCH_LIMIT})."
        ),
    )
    return parser.parse_args(argv)


def _positive_integer(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive number of records")
    return value


def render_executor_status(snapshot: ExecutorStatusSnapshot) -> str:
    """Render the snapshot as plain text. Pure: no I/O, no clock."""
    if type(snapshot) is not ExecutorStatusSnapshot:
        raise ValueError("render_executor_status requires an ExecutorStatusSnapshot")
    lines = [
        f"Executor pool — backend {snapshot.backend}, "
        f"captured {_render_timestamp(snapshot.captured_at)}",
        "",
    ]
    lines.extend(_render_pool(snapshot))
    lines.append("")
    lines.extend(_render_history(snapshot))
    if snapshot.faults:
        lines.append("")
        lines.extend(_render_faults(snapshot))
    return "\n".join(lines)


def _render_pool(snapshot: ExecutorStatusSnapshot) -> list[str]:
    pool = snapshot.pool
    if type(pool) is PoolOffline:
        # Loud, not silent: an absent pool gets a labelled line and its
        # reason, never an empty table that reads as "idle".
        return ["POOL: unavailable", f"  {pool.detail}"]
    if type(pool) is not PoolOnline:
        raise AssertionError("pool state is a closed union")
    machines = "machine" if pool.capacity.machines == 1 else "machines"
    header = (
        f"POOL: online — {pool.capacity.machines} {machines}, "
        f"{pool.capacity.total_cpus} cpus, "
        f"{pool.claimed_cpus} in use; {_render_job_counts(pool)}"
    )
    if not pool.jobs:
        return [header, "  (nothing queued or running)"]
    rows = [
        ("STATE", "FOR", "CPUS", "PRI", "LANE", "SUBMITTED BY", "EXCLUSIVE"),
        *(_render_job_row(job) for job in _ordered_jobs(pool)),
    ]
    return [
        header,
        *(f"  {row}" for row in _render_table(rows, _JOB_NUMERIC_COLUMNS)),
    ]


def _render_job_counts(pool: PoolOnline) -> str:
    """Name only the states that actually have jobs in them.

    A held job is the single most important thing this line can say, so
    it must never be averaged into an undifferentiated job count.
    """
    counted = [
        f"{len(pool.in_state(state))} {state.value}"
        for state in _JOB_STATE_ORDER
        if pool.in_state(state)
    ]
    return ", ".join(counted) if counted else "nothing in the queue"


def _ordered_jobs(pool: PoolOnline) -> list[PoolJob]:
    """Running first, then waiting, longest in state first within each."""
    ordered: list[PoolJob] = []
    for state in _JOB_STATE_ORDER:
        ordered.extend(
            sorted(
                pool.in_state(state),
                key=lambda job: -job.seconds_in_state,
            )
        )
    return ordered


def _render_job_row(job: PoolJob) -> tuple[str, ...]:
    origin = job.origin
    if type(origin) is LaneJobOrigin:
        lane = origin.work_key.value
        submitter = origin.submitter_worktree
    elif type(origin) is ForeignJobOrigin:
        # Not one of ours, but it is holding the same cpus, so it is
        # part of the answer to "why is my lane waiting".
        lane = "(not a lane)"
        submitter = f"{origin.owner} (other user)"
    else:
        raise AssertionError("job origin is a closed union")
    return (
        job.state.value,
        _render_duration(job.seconds_in_state),
        str(job.request_cpus),
        str(job.priority),
        lane,
        submitter,
        ",".join(job.exclusive) if job.exclusive else "-",
    )


def _render_history(snapshot: ExecutorStatusSnapshot) -> list[str]:
    header = f"RECENT DISPATCH: {snapshot.journal_location}"
    if not snapshot.lanes:
        return [
            header,
            "  (no dispatch records — lanes record here as they complete)",
        ]
    rows = [
        ("LANE", "RUNS", "LAST RUNTIME", "LAST QUEUE WAIT", "PRI", "EXIT", "WHEN"),
        *(_render_lane_row(lane) for lane in snapshot.lanes),
    ]
    scanned = (
        f"{header} ({snapshot.records_scanned} record(s) scanned, "
        "highest dispatch priority first)"
    )
    return [
        scanned,
        *(f"  {row}" for row in _render_table(rows, _LANE_NUMERIC_COLUMNS)),
    ]


def _render_lane_row(lane: LaneDispatchSummary) -> tuple[str, ...]:
    return (
        lane.work_key.value,
        str(lane.runs),
        _render_duration(lane.last_runtime_seconds),
        _render_duration(lane.last_queue_wait_seconds),
        str(lane.learned_priority),
        str(lane.last_exit_code),
        _render_timestamp(lane.last_recorded_at),
    )


def _render_faults(snapshot: ExecutorStatusSnapshot) -> list[str]:
    return [
        "FAULTS: an input is broken, not merely empty — this snapshot is "
        "incomplete",
        *(f"  ! {fault.source.value}: {fault.detail}" for fault in snapshot.faults),
    ]


def _render_table(
    rows: Sequence[tuple[str, ...]], numeric: frozenset[int]
) -> list[str]:
    """Size each column to its widest cell; right-align the numbers.

    Aligned digits are what make two lanes' costs comparable at a
    glance, which is the entire point of printing them side by side.
    """
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    return [
        "  ".join(
            cell.rjust(width) if index in numeric else cell.ljust(width)
            for index, (cell, width) in enumerate(zip(row, widths))
        ).rstrip()
        for row in rows
    ]


def _render_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _render_timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
