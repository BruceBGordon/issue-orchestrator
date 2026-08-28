# pyright: strict
"""Run one validation lane through the LaneExecutor port.

This is the composition root for lane execution: it selects the backend
(direct subprocess by default; the scheduler backend when explicitly
opted in), builds the typed lane command, and translates the closed
outcome back into a process exit code for make.

Exit codes: the lane's own exit code on completion; 124 on deadline;
78 (configuration) when an opted-in backend is unavailable; 70
(software) when the backend itself faults mid-run. Backend faults are
never disguised as lane results.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

from ...adapters.direct_lane_executor import (
    DirectLaneExecutor,
    DirectLaneTerminationPolicy,
)
from ...adapters.json_lane_runtime_history import (
    JsonLaneRuntimeHistory,
    LaneRuntimeHistoryError,
)
from ...adapters.jsonl_lane_dispatch_journal import (
    InertLaneDispatchJournal,
    JsonlLaneDispatchJournal,
)
from ...domain.lane_execution import (
    LaneCommand,
    LaneCompleted,
    LaneDeadline,
    LaneExecutorError,
    LaneExecutorUnavailableError,
    LaneResources,
    LaneTimedOut,
    LaneWorkKey,
)
from ...infra.lane_declarations import (
    LaneDeclaration,
    LaneDeclarationError,
    load_lane_declaration,
)
from ...infra.validation_timings import resolve_git_common_dir
from ...ports.lane_dispatch_journal import (
    LaneDispatchJournal,
    LaneDispatchJournalError,
    LaneDispatchRecord,
)
from ...ports.lane_executor import LaneExecutor
from ...ports.lane_runtime_history import LaneRuntimeHistory

BACKEND_ENVIRONMENT_VARIABLE = "ISSUE_ORCHESTRATOR_LANE_EXECUTOR"
_DIRECT_BACKEND = "direct"
_CONDOR_BACKEND = "condor"
_UNAVAILABLE_EXIT_CODE = 78
_BACKEND_FAULT_EXIT_CODE = 70
_DIRECT_GRACEFUL_SHUTDOWN_SECONDS = 10.0


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" not in raw:
        print(
            "lane-run: usage requires '--' before the lane command",
            file=sys.stderr,
        )
        return _UNAVAILABLE_EXIT_CODE
    separator = raw.index("--")
    command = raw[separator + 1 :]
    if not command:
        print("lane-run: no lane command after '--'", file=sys.stderr)
        return _UNAVAILABLE_EXIT_CODE
    arguments = _parse_arguments(raw[:separator])
    arguments.command = command
    try:
        executor = _build_executor(arguments.backend)
        history = _build_history()
        journal = _build_journal()
        work_key = LaneWorkKey(str(arguments.work_key))
        # Scheduling facts are declared in ONE place —
        # .issue-orchestrator/lanes.yaml — resolved here by the
        # lane's logical work key. The Makefile carries commands and
        # work keys only.
        declaration = _load_declaration(work_key.value)
        # Dispatch order is learned, never declared: the rolling
        # median of this lane's past runtimes (LPT — longer
        # lanes first). Zero history means priority 0, exactly
        # the naive first run.
        priority = history.learned_priority(work_key)
        outcome = executor.run(
            _build_command(arguments),
            LaneResources(
                request_cpus=declaration.request_cpus,
                exclusive=declaration.exclusive,
                priority=priority,
                request_memory_mb=declaration.memory_mb,
                suspendable=declaration.suspendable,
            ),
        )
    except LaneDeclarationError as error:
        print(f"lane-run: {error}", file=sys.stderr)
        return _UNAVAILABLE_EXIT_CODE
    except LaneExecutorUnavailableError as error:
        print(f"lane-run: {error}", file=sys.stderr)
        return _UNAVAILABLE_EXIT_CODE
    except LaneExecutorError as error:
        print(f"lane-run: backend fault: {error}", file=sys.stderr)
        return _BACKEND_FAULT_EXIT_CODE
    except LaneRuntimeHistoryError as error:
        # Corrupt history is a bug in whatever wrote it — fail loudly
        # (the message names the file to delete); never guess a
        # priority from garbage.
        print(f"lane-run: {error}", file=sys.stderr)
        return _BACKEND_FAULT_EXIT_CODE
    if type(outcome) is LaneCompleted:
        return _conclude_completed(
            arguments, priority, history, journal, work_key, outcome
        )
    if type(outcome) is LaneTimedOut:
        print(
            f"lane-run: lane {arguments.work_key!r} exceeded its "
            f"{arguments.timeout_seconds:.0f}s deadline "
            f"(elapsed {outcome.elapsed_seconds:.1f}s)",
            file=sys.stderr,
        )
        return outcome.exit_code
    raise AssertionError("lane outcome is a closed union")


def _conclude_completed(
    arguments: argparse.Namespace,
    priority: int,
    history: LaneRuntimeHistory,
    journal: LaneDispatchJournal,
    work_key: LaneWorkKey,
    outcome: LaneCompleted,
) -> int:
    """Record what the completed lane teaches, then report its exit.

    The stderr line puts priority, queue wait, and runtime in the gate
    log where a reader already is; the journal (a behavior-level port)
    owns persistence and its failure semantics. Failed lanes are
    journaled too — a kill's dispatch facts are diagnosis, even though
    only successes feed the learning loop."""
    print(
        f"[lane-dispatch] {arguments.work_key} backend={arguments.backend} "
        f"priority={priority} queue_wait={outcome.queue_wait_seconds:.1f}s "
        f"runtime={outcome.observed_runtime_seconds:.1f}s "
        f"exit={outcome.exit_code}",
        file=sys.stderr,
    )
    try:
        journal.record(
            LaneDispatchRecord(
                work_key=work_key,
                backend=str(arguments.backend),
                priority=priority,
                queue_wait_seconds=outcome.queue_wait_seconds,
                observed_runtime_seconds=outcome.observed_runtime_seconds,
                exit_code=outcome.exit_code,
            )
        )
    except LaneDispatchJournalError as error:
        print(f"lane-run: {error}", file=sys.stderr)
        return _BACKEND_FAULT_EXIT_CODE
    if outcome.exit_code == 0:
        # Only successes teach: a failed run's duration is the
        # failure's, not the lane's.
        try:
            history.record_success(work_key, outcome.observed_runtime_seconds)
        except LaneRuntimeHistoryError as error:
            print(f"lane-run: {error}", file=sys.stderr)
            return _BACKEND_FAULT_EXIT_CODE
    return outcome.exit_code


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lane-run",
        description="Run one validation lane through the configured backend.",
    )
    parser.add_argument("--work-key", required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    # Scheduling facts (cpus, memory, suspendability, exclusives) are
    # NOT flags: they are declared once per work key in
    # .issue-orchestrator/lanes.yaml and resolved by name, so
    # no second configuration surface can drift from the first.
    parser.add_argument(
        "--backend",
        choices=(_DIRECT_BACKEND, _CONDOR_BACKEND),
        default=os.environ.get(BACKEND_ENVIRONMENT_VARIABLE, _DIRECT_BACKEND),
        help=(
            "Execution backend; defaults to "
            f"${BACKEND_ENVIRONMENT_VARIABLE} or '{_DIRECT_BACKEND}'."
        ),
    )
    return parser.parse_args(argv)


def _load_declaration(work_key: str) -> LaneDeclaration:
    """The declared scheduling facts for this work key (test seam)."""
    return load_lane_declaration(Path.cwd(), work_key)


def _build_journal() -> LaneDispatchJournal:
    """The repo-shared dispatch journal, or an inert one outside a repo
    (mirroring the runtime history's inertness)."""
    common_dir = resolve_git_common_dir(Path.cwd())
    if common_dir is None:
        return InertLaneDispatchJournal()
    return JsonlLaneDispatchJournal(common_dir / "issue-orchestrator")


def _build_history() -> LaneRuntimeHistory:
    """The repo-shared runtime history, or an inert one outside a repo.

    History lives with the repository (the git common dir), like the
    validation timings, so every worktree of one repo learns from the
    same runs. Outside a repository there is nothing to share and
    nothing worth learning across invocations, so the loop is inert:
    priority 0, record nowhere.
    """
    common_dir = resolve_git_common_dir(Path.cwd())
    if common_dir is None:
        return _InertLaneRuntimeHistory()
    return JsonLaneRuntimeHistory(
        common_dir / "issue-orchestrator" / "lane-runtime-history"
    )


class _InertLaneRuntimeHistory:
    """No-repo stand-in: always naive, never persists."""

    def record_success(self, work_key: LaneWorkKey, runtime_seconds: float) -> None:
        del work_key, runtime_seconds

    def learned_priority(self, work_key: LaneWorkKey) -> int:
        del work_key
        return 0


def _build_executor(backend: str) -> LaneExecutor:
    if backend == _DIRECT_BACKEND:
        return DirectLaneExecutor(
            DirectLaneTerminationPolicy(_DIRECT_GRACEFUL_SHUTDOWN_SECONDS)
        )
    if backend == _CONDOR_BACKEND:
        from ...adapters.condor import CondorLaneExecutor, CondorTools

        return CondorLaneExecutor(CondorTools.resolve())
    raise LaneExecutorUnavailableError(f"unknown lane backend {backend!r}")


def _build_command(arguments: argparse.Namespace) -> LaneCommand:
    argv0 = str(arguments.command[0])
    resolved = shutil.which(argv0)
    if resolved is None:
        raise LaneExecutorUnavailableError(
            f"lane command executable not found on PATH: {argv0!r}"
        )
    return LaneCommand(
        work_key=LaneWorkKey(str(arguments.work_key)),
        arguments=(
            str(Path(resolved).resolve()),
            *(str(token) for token in arguments.command[1:]),
        ),
        working_directory=Path.cwd().resolve(),
        deadline=LaneDeadline(float(arguments.timeout_seconds)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
