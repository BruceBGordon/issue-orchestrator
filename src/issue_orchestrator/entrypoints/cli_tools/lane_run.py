# pyright: strict
"""Run one validation lane through the LaneExecutor port.

Backend selection itself is owned by ``execution.lane_backends`` (one
mapping serves this and the gate's policy preflight). This entrypoint
builds the typed lane command, runs it, and translates the closed
outcome back into a process exit code for make.

Exit codes: the lane's own exit code on completion; 124 on deadline;
78 (configuration) when an opted-in backend is unavailable; 70
(software) when the backend faults mid-run or the dispatcher itself
crashes unclassified. Backend faults are never disguised as lane
results, so the mapping in `main` must stay total: any code this
module produces outside that set would be read as the lane's.

The converse does not hold and must not be forced: a lane owns the
whole 0-255 space and may itself exit 70, 78 or 124, which are passed
through unchanged rather than remapped — reporting a code the lane did
not return would be the worse lie.

NOTHING this module emits separates the two, and no code or doc here
may claim otherwise — three such claims have already been falsified.
Journal rows are best-effort in both directions (a fault before the
write leaves a completed lane with no row; one after it exits 70 over
a row recording the lane's own exit), and the `lane-run:` stderr
prefix is neither guaranteed nor unforgeable — writes can fail, and
the lane inherits this process's stderr, so it can print the prefix
itself. A real discriminator needs an invocation-correlated lifecycle
record with an explicit indeterminate state; this module has none.

Callers outside this repository reach the same `main` through the
installed `lane-run` console script (see docs/user/condor_lanes.md).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ...adapters.json_lane_runtime_history import JsonLaneRuntimeHistory
from ...adapters.jsonl_lane_dispatch_journal import (
    InertLaneDispatchJournal,
    JsonlLaneDispatchJournal,
)
from ...domain.lane_cpu_request import LaneCpuRequest
from ...domain.lane_execution import (
    LaneCommand,
    LaneCompleted,
    LaneDeadline,
    LaneExecutorError,
    LaneExecutorUnavailableError,
    LaneResources,
    LaneSuspendability,
    LaneTimedOut,
    LaneWorkKey,
)
from ...execution.lane_backends import (
    BACKEND_ENVIRONMENT_VARIABLE,
    BACKEND_NAMES,
    DIRECT_BACKEND,
    build_lane_executor,
)
from ...infra.lane_declarations import (
    LaneDeclaration,
    LaneDeclarationError,
    load_lane_declaration,
)
from ...infra.machine_state import (
    default_machine_state_sampler,
    sample_machine_state_from,
)
from ...infra.validation_timings import resolve_git_common_dir
from ...ports.lane_dispatch_journal import (
    LaneDispatchJournal,
    LaneDispatchJournalError,
    LaneDispatchJournalReader,
    LaneDispatchRecord,
)
from ...ports.lane_runtime_history import (
    LaneRuntimeHistory,
    LaneRuntimeHistoryError,
)
from ...ports.machine_state import MachineStateSampler

_UNAVAILABLE_EXIT_CODE = 78
_BACKEND_FAULT_EXIT_CODE = 70


def main(argv: Sequence[str] | None = None) -> int:
    """Total owner of the exit-code contract, for every caller.

    Both invocation forms — the installed ``lane-run`` console script
    (``sys.exit(main())``) and ``python -m ...lane_run`` — enter here,
    so the contract cannot differ between them and no wrapper may sit
    on one path only. Totality is the reason there is no ``safe_main``:
    an escaping exception would exit 1 under CPython, and 1 is a *lane*
    result code, so an unclassified dispatcher crash would read as
    "your tests failed". Classifying it as 70 keeps the promise that
    backend faults are never disguised as lane results; the traceback
    is printed verbatim, so nothing is softened, only named.

    Option-shape errors and ``--help`` stay argparse's: it exits
    directly, and SystemExit is not an ``Exception``, so those verdicts
    pass through this mapping rather than being restated by it.
    """
    try:
        return _dispatch(list(sys.argv[1:] if argv is None else argv))
    except Exception:
        _announce_internal_error()
        return _BACKEND_FAULT_EXIT_CODE


def _announce_internal_error() -> None:
    """Report the crash without letting the report become one.

    The exit code is the contract; the message is diagnostic. An
    unwritable stderr (full disk, closed pipe) raising out of THIS
    handler would escape `main` and exit 1 — a lane result code — so
    the diagnostic is what gets dropped, never the classification.
    """
    try:
        print("lane-run: internal error:", file=sys.stderr)
        traceback.print_exc()
    except OSError:
        pass


def _dispatch(raw: list[str]) -> int:
    separator = raw.index("--") if "--" in raw else None
    options = raw if separator is None else raw[:separator]
    if separator is None:
        if "-h" in options or "--help" in options:
            # On PATH this CLI's only discovery surface is --help, and
            # the separator rule would otherwise answer it with a usage
            # error. The real options go to argparse rather than a bare
            # print_help() so it still applies them IN ORDER: a
            # malformed option before --help must lose to argparse, as
            # it does when a separator is present. argparse exits for
            # both verdicts; the usage error below is what a
            # well-formed option list that merely forgot the separator
            # falls through to.
            _build_parser().parse_args(options)
        print(
            "lane-run: usage requires '--' before the lane command",
            file=sys.stderr,
        )
        return _UNAVAILABLE_EXIT_CODE
    command = raw[separator + 1 :]
    if not command:
        print("lane-run: no lane command after '--'", file=sys.stderr)
        return _UNAVAILABLE_EXIT_CODE
    arguments = _build_parser().parse_args(options)
    arguments.command = command
    try:
        executor = build_lane_executor(str(arguments.backend))
        history = build_runtime_history()
        journal = _build_journal()
        # Scheduling facts are declared in ONE place —
        # .issue-orchestrator/lanes.yaml — resolved here by the
        # lane's logical work key. The Makefile carries commands and
        # work keys only.
        work_key = LaneWorkKey(str(arguments.work_key))
        declaration = _load_declaration(work_key.value)
        dispatch = _decide_dispatch(
            work_key, str(arguments.backend), declaration, history
        )
        outcome = executor.run(
            _build_command(arguments),
            LaneResources(
                request_cpus=dispatch.cpu_request.request_cpus,
                exclusive=declaration.exclusive,
                priority=dispatch.priority,
                request_memory_mb=declaration.memory_mb,
                suspendability=LaneSuspendability(declaration.suspendability),
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
        return _conclude_completed(dispatch, history, journal, outcome)
    if type(outcome) is LaneTimedOut:
        print(
            f"lane-run: lane {arguments.work_key!r} exceeded its "
            f"{arguments.timeout_seconds:.0f}s deadline "
            f"(elapsed {outcome.elapsed_seconds:.1f}s)",
            file=sys.stderr,
        )
        return outcome.exit_code
    raise AssertionError("lane outcome is a closed union")


@dataclass(frozen=True, slots=True)
class _LaneDispatch:
    """Everything this invocation decided before the lane ran.

    Bundled rather than passed as loose scalars so the decision and
    its evidence travel together into the log, the journal, and the
    learning loop — the three consumers that must agree about what
    was submitted and why.
    """

    work_key: LaneWorkKey
    backend: str
    priority: int
    cpu_request: LaneCpuRequest


def _decide_dispatch(
    work_key: LaneWorkKey,
    backend: str,
    declaration: LaneDeclaration,
    history: LaneRuntimeHistory,
) -> _LaneDispatch:
    """Turn the declaration plus learned history into one submission.

    Both learned dimensions are consumed here and nowhere else:

    - Dispatch order is learned, never declared: the rolling median of
      this lane's past runtimes (LPT — longer lanes first). Zero
      history means priority 0, exactly the naive first run.
    - CPU demand starts at the declared value and may only be lowered
      by evidence. The declaration is the seed AND the ceiling; the
      policy itself lives in LaneCpuRequest so no caller can grow a
      second version of it.
    """
    return _LaneDispatch(
        work_key=work_key,
        backend=backend,
        priority=history.learned_priority(work_key),
        cpu_request=LaneCpuRequest.resolve(
            declaration.request_cpus, history.learned_busy_cores(work_key)
        ),
    )


def _conclude_completed(
    dispatch: _LaneDispatch,
    history: LaneRuntimeHistory,
    journal: LaneDispatchJournal,
    outcome: LaneCompleted,
) -> int:
    """Record what the completed lane teaches, then report its exit.

    The stderr line puts priority, queue wait, runtime, and the sizing
    decision in the gate log where a reader already is; the journal (a
    behavior-level port) owns persistence and its failure semantics.
    Failed lanes are journaled too — a kill's dispatch facts are
    diagnosis, even though only successes feed the learning loop.

    Everything below runs AFTER the lane has already finished, so every
    fault here returns 70 over a lane that completed. That is why the
    journal row cannot be read as a verdict: a failure before the write
    leaves a completed lane with no row, and one after it leaves a row
    whose exit_code is not what this process returns."""
    request = dispatch.cpu_request
    print(
        f"[lane-dispatch] {dispatch.work_key.value} backend={dispatch.backend} "
        f"priority={dispatch.priority} "
        f"queue_wait={outcome.queue_wait_seconds:.1f}s "
        f"runtime={outcome.observed_runtime_seconds:.1f}s "
        f"exit={outcome.exit_code} "
        f"request_cpus={request.request_cpus}/{request.declared_cpus} "
        f"busy_cores={_format_busy_cores(outcome.observed_busy_cores)}",
        file=sys.stderr,
    )
    try:
        journal.record(
            LaneDispatchRecord(
                work_key=dispatch.work_key,
                backend=dispatch.backend,
                priority=dispatch.priority,
                queue_wait_seconds=outcome.queue_wait_seconds,
                observed_runtime_seconds=outcome.observed_runtime_seconds,
                exit_code=outcome.exit_code,
                # Sampled after the lane concluded, so the reading
                # describes the machine the lane just competed on and
                # cannot itself perturb the lane's own timing. The
                # SEAM is passed, not its result: building the probe
                # happens inside the containment too, so nothing about
                # the probe can replace this lane's decided outcome.
                machine_state=sample_machine_state_from(
                    _build_machine_state_sampler
                ),
                cpu_request=request,
                observed_busy_cores=outcome.observed_busy_cores,
            )
        )
    except LaneDispatchJournalError as error:
        print(f"lane-run: {error}", file=sys.stderr)
        return _BACKEND_FAULT_EXIT_CODE
    if outcome.exit_code == 0:
        # Only successes teach: a failed run's duration is the
        # failure's, not the lane's. The CPU dimension rides the same
        # rule and the same call — a backend that did not measure
        # passes None, which records a runtime and nothing else.
        try:
            history.record_success(
                dispatch.work_key,
                outcome.observed_runtime_seconds,
                outcome.observed_busy_cores,
            )
        except LaneRuntimeHistoryError as error:
            print(f"lane-run: {error}", file=sys.stderr)
            return _BACKEND_FAULT_EXIT_CODE
    return outcome.exit_code


def _format_busy_cores(measured: float | None) -> str:
    """An unmeasured run says so; it never prints a 0.00 it did not see."""
    if measured is None:
        return "unmeasured"
    return f"{measured:.2f}"


def _build_parser() -> argparse.ArgumentParser:
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
        choices=BACKEND_NAMES,
        default=os.environ.get(BACKEND_ENVIRONMENT_VARIABLE, DIRECT_BACKEND),
        help=(
            "Execution backend; defaults to "
            f"${BACKEND_ENVIRONMENT_VARIABLE} or '{DIRECT_BACKEND}'."
        ),
    )
    return parser


def _load_declaration(work_key: str) -> LaneDeclaration:
    """The declared scheduling facts for this work key (test seam)."""
    return load_lane_declaration(Path.cwd(), work_key)


def _build_machine_state_sampler() -> MachineStateSampler:
    """The host probe stamped on this lane's dispatch record (test seam)."""
    return default_machine_state_sampler()


def _build_journal_adapter() -> JsonlLaneDispatchJournal | InertLaneDispatchJournal:
    """The repo-shared dispatch journal, or an inert one outside a repo
    (mirroring the runtime history's inertness).

    One constructor for both directions of the journal: the writer and
    the reader must never disagree about which file they mean."""
    common_dir = resolve_git_common_dir(Path.cwd())
    if common_dir is None:
        return InertLaneDispatchJournal()
    return JsonlLaneDispatchJournal(common_dir / "issue-orchestrator")


def _build_journal() -> LaneDispatchJournal:
    return _build_journal_adapter()


def build_dispatch_journal_reader() -> LaneDispatchJournalReader:
    """Read-only view of the same journal `lane-run` writes."""
    return _build_journal_adapter()


def build_runtime_history() -> LaneRuntimeHistory:
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

    def record_success(
        self,
        work_key: LaneWorkKey,
        runtime_seconds: float,
        busy_cores: float | None,
    ) -> None:
        del work_key, runtime_seconds, busy_cores

    def learned_priority(self, work_key: LaneWorkKey) -> int:
        del work_key
        return 0

    def learned_busy_cores(self, work_key: LaneWorkKey) -> float | None:
        del work_key
        # Nothing known: the declared seed answers, as on a first run.
        return None


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
