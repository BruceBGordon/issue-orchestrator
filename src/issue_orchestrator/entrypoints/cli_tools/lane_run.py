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
from ...ports.lane_executor import LaneExecutor

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
        outcome = executor.run(
            _build_command(arguments),
            LaneResources(
                request_cpus=arguments.request_cpus,
                exclusive=tuple(arguments.exclusive),
                priority=arguments.priority,
            ),
        )
    except LaneExecutorUnavailableError as error:
        print(f"lane-run: {error}", file=sys.stderr)
        return _UNAVAILABLE_EXIT_CODE
    except LaneExecutorError as error:
        print(f"lane-run: backend fault: {error}", file=sys.stderr)
        return _BACKEND_FAULT_EXIT_CODE
    if type(outcome) is LaneCompleted:
        return outcome.exit_code
    if type(outcome) is LaneTimedOut:
        print(
            f"lane-run: lane {arguments.work_key!r} exceeded its "
            f"{arguments.timeout_seconds:.0f}s deadline "
            f"(elapsed {outcome.elapsed_seconds:.1f}s)",
            file=sys.stderr,
        )
        return outcome.exit_code
    raise AssertionError("lane outcome is a closed union")


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lane-run",
        description="Run one validation lane through the configured backend.",
    )
    parser.add_argument("--work-key", required=True)
    parser.add_argument("--request-cpus", type=int, required=True)
    parser.add_argument(
        "--exclusive",
        action="append",
        default=[],
        help="Machine-wide mutual-exclusion token (repeatable).",
    )
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument(
        "--priority",
        type=int,
        default=0,
        help="Expected duration in seconds; scheduling backends start longer lanes first.",
    )
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
