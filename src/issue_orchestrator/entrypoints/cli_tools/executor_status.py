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

import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ...domain.lane_execution import LaneExecutorUnavailableError
from ...execution.lane_backends import (
    BACKEND_ENVIRONMENT_VARIABLE,
    BACKEND_NAMES,
    SelectedBackend,
    UnknownBackend,
    build_pool_inspector,
    select_backend,
)
from ...infra.config_paths import get_config_path, list_configs
from ...infra.env import get_env
from ...infra.lane_declarations import LANES_FILE_RELATIVE, load_lane_declarations
from ...observation.executor_status import (
    DEFAULT_RECENT_DISPATCH_LIMIT,
    DeclarationsRead,
    DeclarationsUnavailable,
    ExecutorStatusSnapshot,
    LaneRow,
    build_executor_status_snapshot,
)
from ...ports.machine_state import MachineState
from ...ports.executor_pool import (
    AnsweredPool,
    ForeignJobOrigin,
    LaneJobOrigin,
    PoolJob,
    PoolJobState,
    PoolOffline,
    PoolUnknownHealth,
)
from .lane_run import build_dispatch_journal_reader, build_runtime_history

_FAULT_EXIT_CODE = 70
# Matches lane-run: 78 is the configuration-error exit code.
_MISCONFIGURED_EXIT_CODE = 78
# Column indexes whose cells are numbers, so digits line up.
_JOB_NUMERIC_COLUMNS = frozenset({1, 2, 3})
_LANE_NUMERIC_COLUMNS = frozenset({1, 2, 5, 6, 7, 8, 9, 10})
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
    worktree = Path.cwd()
    backend = select_backend(
        explicit=arguments.backend,
        environment=os.environ,
        validation_commands=_configured_validation_commands(worktree),
    )
    try:
        snapshot = build_executor_status_snapshot(
            backend=backend,
            inspector_for=build_pool_inspector,
            declarations_reader=lambda: load_lane_declarations(worktree),
            declarations_location=str(worktree / LANES_FILE_RELATIVE),
            journal_reader=build_dispatch_journal_reader(),
            runtime_history=build_runtime_history(),
            captured_at=datetime.now(timezone.utc),
            recent_limit=int(arguments.scan),
        )
    except LaneExecutorUnavailableError as error:
        # A backend nobody implements is a misconfiguration, not a
        # missing pool: say which setting is wrong instead of printing a
        # snapshot of a backend that does not exist.
        print(f"executor-status: {error}", file=sys.stderr)
        return _MISCONFIGURED_EXIT_CODE
    print(render_executor_status(snapshot))
    if snapshot.is_degraded:
        return _FAULT_EXIT_CODE
    # An unestablished backend is a configuration gap the operator
    # closes with --backend, and the rest of the snapshot is still
    # printed above; it must not read as a clean run.
    return _MISCONFIGURED_EXIT_CODE if type(backend) is UnknownBackend else 0


def _configured_validation_commands(worktree: Path) -> tuple[str, ...]:
    """The repository's own gate commands, from the canonical config owner.

    This is what makes the reported backend the one the gate will
    actually use: the repository selects its backend by prefixing these
    commands, and reading them is the only way to see that without
    guessing. Best-effort by design — the pool is machine-wide and this
    command must still answer where no configuration loads — but a
    repository that HAS configured a backend is never overruled, because
    no default is left to overrule it with.

    Exactly one config is read (the mode's primary, or the one an
    explicit environment selection names). Reading every config in the
    mode would compare a repository's alternative deployments against
    each other and report them as a contradiction.
    """
    from ...infra.config import Config

    try:
        path = _active_config_path(worktree)
        if path is None:
            return ()
        validation = Config.load(path).validation
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        # An unreadable config establishes nothing. It must not crash a
        # status command whose other sources are fine, and it must not
        # invent a backend either — the selection owner reports unknown.
        return ()
    return tuple(
        command
        for command in (validation.quick.cmd, validation.publish.cmd)
        if type(command) is str and command
    )


def _active_config_path(worktree: Path) -> Path | None:
    """The config this repository would actually launch with."""
    explicit = get_env("CONFIG_PATH")
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_file() else None
    for repo_root in [worktree, *worktree.parents]:
        names = list_configs(repo_root)
        if names:
            return get_config_path(repo_root, names[0])
    return None


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
        default=None,
        help=(
            "Backend to report on. Left out, it is established from "
            f"${BACKEND_ENVIRONMENT_VARIABLE} or the repository's "
            "validation command; if neither says, the snapshot reports "
            "the backend as unknown rather than assuming one."
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
        f"Executor pool — {_render_backend(snapshot)}, "
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


def _render_backend(snapshot: ExecutorStatusSnapshot) -> str:
    """Name the backend AND what established it.

    The source is not trivia: an operator reading the wrong backend
    needs to know whether to change a flag, an environment variable, or
    the repository's gate command.
    """
    backend = snapshot.backend
    if type(backend) is UnknownBackend:
        return "backend UNKNOWN"
    if type(backend) is not SelectedBackend:
        raise AssertionError("backend selection is a closed union")
    return f"backend {backend.name} (from {backend.source.value})"


def _render_pool(snapshot: ExecutorStatusSnapshot) -> list[str]:
    pool = snapshot.pool
    if type(pool) is PoolOffline:
        # Loud, not silent: an absent pool gets a labelled line and its
        # reason, never an empty table that reads as "idle".
        return ["POOL: unavailable", f"  {pool.detail}"]
    if not isinstance(pool, AnsweredPool):
        raise AssertionError("pool state is a closed union")
    machines = "machine" if pool.capacity.machines == 1 else "machines"
    counts = (
        f"{pool.capacity.machines} {machines}, "
        f"{pool.capacity.total_cpus} cpus, "
        f"{pool.claimed_cpus} in use; {_render_job_counts(pool)}"
    )
    if type(pool) is PoolUnknownHealth:
        # Never "online": the pool answered, but nothing proved it can
        # run anything. The reason comes first, because the numbers
        # underneath it may describe a machine that is gone.
        header = f"POOL: health UNKNOWN — {pool.detail}"
        header = f"{header}\n  it reported: {counts}"
    else:
        header = f"POOL: online — {counts}"
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


def _render_job_counts(pool: AnsweredPool) -> str:
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


def _ordered_jobs(pool: AnsweredPool) -> list[PoolJob]:
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
    """One table per lane: how it is routed, and what it last cost."""
    declarations = snapshot.declarations
    if type(declarations) is DeclarationsRead:
        where = f"LANES: {declarations.path}"
    elif type(declarations) is DeclarationsUnavailable:
        # Loud: without declarations, "no routing" below means "not
        # known", not "not declared" — and every lane here is unrunnable
        # until the file is fixed.
        where = f"LANES: declarations UNREADABLE — {declarations.detail}"
    else:
        raise AssertionError("declarations state is a closed union")
    scanned = f"{snapshot.records_scanned} record(s) scanned"
    if snapshot.records_predating_schema:
        # Say how thin the history really is. These rows are readable
        # JSON that simply predates a column the record now requires, so
        # counting them silently would overstate the sample behind
        # every runtime below. Phrased by EFFECT rather than by which
        # column is missing: the operator's remedy is the same for every
        # epoch — those rows are gone, newer ones accrue — and naming a
        # specific one would go stale at the next widening.
        scanned += (
            f", {snapshot.records_predating_schema} skipped as older than "
            "the current record schema"
        )
    header = (
        f"{where}\n  dispatch journal: {snapshot.journal_location} ({scanned})"
    )
    if not snapshot.lanes:
        return [
            header,
            "  (no lanes: none declared, and none recorded in the journal)",
        ]
    rows = [
        (
            "LANE",
            "CPUS",
            "MEM MB",
            "FREEZE",
            "EXCLUSIVE",
            "RUNS",
            "LAST RUNTIME",
            "LAST QUEUE WAIT",
            "PRI",
            "EXIT",
            "IDLE",
            "BACKEND",
            "WHEN",
        ),
        *(_render_lane_row(lane) for lane in snapshot.lanes),
    ]
    return [
        f"{header}, highest dispatch priority first",
        *(f"  {row}" for row in _render_table(rows, _LANE_NUMERIC_COLUMNS)),
    ]


def _render_lane_row(lane: LaneRow) -> tuple[str, ...]:
    routing = lane.routing
    if routing is None:
        # Undeclared: `lane-run` refuses these, so the row exists to say
        # the lane cannot run, not merely that a column is empty.
        declared = ("—", "—", "undeclared", "—")
    else:
        declared = (
            str(routing.request_cpus),
            str(routing.memory_mb),
            routing.suspendability,
            ",".join(routing.exclusive) if routing.exclusive else "-",
        )
    history = lane.history
    if history is None:
        # Declared but never dispatched in the scanned window — a fact
        # only the declarations could contribute.
        return (
            lane.work_key.value,
            *declared,
            "0",
            "—",
            "—",
            "—",
            "—",
            "—",
            "—",
            "never",
        )
    return (
        lane.work_key.value,
        *declared,
        str(history.runs),
        _render_duration(history.last_runtime_seconds),
        _render_duration(history.last_queue_wait_seconds),
        str(history.learned_priority),
        str(history.last_exit_code),
        # How idle the host was when that runtime was measured. Printed
        # beside it deliberately: a duration read without its contention
        # is the ambiguity the envelope exists to end, and a runtime
        # measured on a pegged machine is not this lane's cost. Idle
        # share rather than load average — on macOS load counts parked
        # threads, so a host reading 12.5 can be 85% idle.
        _render_idle(history.last_machine_state),
        # The backend the lane actually last ran on. Printed because a
        # row that contradicts the header is the clearest possible
        # signal that the selected backend is not the one in use.
        history.last_backend,
        _render_timestamp(history.last_recorded_at),
    )


def _render_idle(state: MachineState) -> str:
    """The host's idle share when the reading was taken, or why not.

    A failed probe reads ``?`` rather than a number: an invented figure
    here would be worse than an admitted gap, which is the same rule the
    envelope itself follows.
    """
    if state.cpu_idle_percent is None:
        return "?"
    return f"{state.cpu_idle_percent:.0f}%"


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
