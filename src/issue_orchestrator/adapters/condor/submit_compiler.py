# pyright: strict
"""Outbound half of the anti-corruption layer: lane spec → submit description.

The scheduler's job description language (quoting rules, knob names,
ClassAd expressions) is compiled here and nowhere else.
"""

from __future__ import annotations

import math
import shlex
from dataclasses import dataclass
from pathlib import Path

from ...domain.lane_execution import (
    LaneCommand,
    LaneResources,
    LaneSuspendability,
)
from .rusage_report import RUSAGE_FILE_NAME, compile_rusage_capture

# Grace between the scheduler's soft kill and its hard kill when a job
# is removed (deadline or cancellation). Mirrors the direct backend's
# TERM-then-KILL escalation.
_REMOVAL_GRACE_SECONDS = 10

# The signals the shim must outlive so the scheduler's hard kill still
# arrives (see _compile_exec_script). TERM is the configured soft kill;
# HUP and INT are here because the rule is about the whole class of
# catchable terminations, not the one signal that exposed it.
_SOFT_KILL_SIGNALS = "TERM HUP INT"


@dataclass(frozen=True, slots=True)
class CompiledSubmitDescription:
    """One complete, self-contained job description.

    The lane's exact argv is compiled into an executable shell shim
    (``exec_script_text``) rather than the scheduler's line-oriented
    ``arguments`` syntax, which cannot carry newlines — and lane
    commands legitimately contain them (``python -c`` scripts).

    ``rusage_path`` is where that shim leaves the lane's CPU report,
    collected from the run directory exactly like the event log.
    """

    text: str
    exec_script_path: Path
    exec_script_text: str
    output_path: Path
    error_path: Path
    event_log_path: Path
    rusage_path: Path


def compile_submit_description(
    command: LaneCommand,
    resources: LaneResources,
    run_directory: Path,
) -> CompiledSubmitDescription:
    """Compile one lane invocation into scheduler language.

    The deadline compiles to a ``periodic_remove`` over *runtime*
    (``JobCurrentStartDate``), so queue wait — pure scheduling
    machinery — is never billed against the lane's budget.
    """
    if type(command) is not LaneCommand:
        raise ValueError("compile_submit_description requires a LaneCommand")
    if type(resources) is not LaneResources:
        raise ValueError("compile_submit_description requires LaneResources")
    if not run_directory.is_absolute():
        raise ValueError("compile_submit_description run_directory must be absolute")
    submitter = command.working_directory.name
    if any(character in submitter for character in ('"', "\\", "\n")):
        # ClassAd string literals would need escaping for these; no
        # legitimate worktree directory contains them, so refuse loudly
        # instead of compiling a description that misparses.
        raise ValueError(
            f"working directory name unusable as submitter tag: {submitter!r}"
        )
    output_path = run_directory / "lane.out"
    error_path = run_directory / "lane.err"
    event_log_path = run_directory / "lane.events"
    exec_script_path = run_directory / "lane.exec"
    rusage_path = run_directory / RUSAGE_FILE_NAME
    # Ceiling, never floor: a floored deadline would let the scheduler
    # remove a lane before its promised budget elapsed.
    timeout = max(1, math.ceil(command.deadline.timeout_seconds))
    lines = [
        f"# lane: {command.work_key.value}",
        # The work key doubles as the job's batch name: queue tooling
        # (and tests) can then address exactly this lane's job by
        # constraint instead of pool-wide operations.
        f"batch_name = {command.work_key.value}",
        "universe = vanilla",
        f"executable = {exec_script_path}",
        f"initialdir = {command.working_directory}",
        "getenv = true",
        f"output = {output_path}",
        f"error = {error_path}",
        f"log = {event_log_path}",
        f"request_cpus = {resources.request_cpus}",
        f"request_memory = {resources.request_memory_mb}",
        "should_transfer_files = NO",
        "notification = never",
        f"job_max_vacate_time = {_REMOVAL_GRACE_SECONDS}",
        # The deadline charges executing time only: suspension (machine
        # load backoff freezing the job) must not burn the budget, or a
        # long freeze manufactures a timeout the lane never earned. The
        # ?: guard keeps the expression defined before any suspension.
        "periodic_remove = (JobStatus == 2) && "
        "((time() - JobCurrentStartDate - (CumulativeSuspensionTime ?: 0)) "
        f"> {timeout})",
        # Load-backoff eligibility is declared per lane, all three
        # classifications explicitly — policy-by-absence would let a
        # new live lane silently opt into freezing. The value is the
        # classification name itself so the pool's suspension policy
        # can gate each class differently; an older policy comparing
        # against a boolean sees a string, matches nothing, and
        # freezes nothing — degradation lands on the fail-safe side.
        f'+SuspendableLane = "{resources.suspendability.value}"',
        # The pool is shared by every worktree of every repo on the
        # machine (concurrent gates are normal, not an anomaly), so
        # each job names its submitter. Without this, attributing a
        # job means digging through Iwd paths after the fact.
        f'+LaneSubmitter = "{submitter}"',
    ]
    if resources.suspendability is LaneSuspendability.COOPERATIVE:
        # A cooperative lane starts UNSAFE and advertises safe windows
        # via chirp (WantIOProxy is the starter-side prerequisite).
        # NOTE: the pool's suspension policy currently holds
        # cooperative CLOSED — never freeze-eligible — because runtime
        # chirp updates provably reach only the schedd's ad, not the
        # startd copy that evaluates SUSPEND (disproven live
        # 2026-08-29; #7139 tracks a startd-visible channel). The
        # attributes ship anyway so the lane-side contract is
        # exercised end-to-end and #7139 can open eligibility without
        # touching jobs.
        lines.append("+SafeToSuspend = False")
        lines.append("+WantIOProxy = True")
    if resources.priority > 0:
        lines.append(f"priority = {resources.priority}")
    if resources.exclusive:
        # Exclusive tokens rely on the pool being configured with
        # CONCURRENCY_LIMIT_DEFAULT = 1 (the personal-pool helper does
        # this), making every named limit a machine-wide mutex.
        lines.append(f"concurrency_limits = {','.join(resources.exclusive)}")
    lines.append("queue")
    return CompiledSubmitDescription(
        text="\n".join(lines) + "\n",
        exec_script_path=exec_script_path,
        exec_script_text=_compile_exec_script(command.arguments, rusage_path),
        output_path=output_path,
        error_path=error_path,
        event_log_path=event_log_path,
        rusage_path=rusage_path,
    )


def _compile_exec_script(arguments: tuple[str, ...], rusage_path: Path) -> str:
    """Compile the exact argv into a POSIX-sh shim that also measures.

    ``shlex.quote`` escaping survives spaces, quotes, and newlines,
    so the lane runs with byte-exact arguments.

    The shim runs the lane instead of ``exec``-ing it, because a
    process that has replaced itself cannot report anything afterwards
    and the CPU measurement has to come from the shell that waited for
    the lane. Everything else about the shim exists to make that
    invisible:

    - **The lane's exit status is re-raised verbatim**, signal deaths
      (128+N) included.
    - **The lane's stderr is byte-identical.** A surviving shell
      announces its dead child ("Killed: 9") on its own stderr, which
      is the lane's error file — output the lane never produced (B,
      #7136 review). So the real stderr is duplicated to fd 3, the
      shell's own stderr goes to ``/dev/null``, and the lane is
      handed fd 3 as its stderr (closing the spare in the child, so
      the lane inherits no descriptor it did not have before). The
      split is exactly right: a failed ``execve`` is reported by the
      already-redirected CHILD and still reaches the lane's error
      file, while the job-status notice comes from the parent and is
      discarded.
    - **The shim outlives the soft kill; the lane does not have to.**
      This is the subtle one, and it was caught only against a live
      pool. The scheduler's deadline removal sends a soft kill, waits
      out ``job_max_vacate_time``, then hard-kills the job's whole
      family. A plain shell dies instantly on the soft kill, the
      scheduler sees the job's primary process gone, and the hard kill
      that would have reaped a signal-resistant descendant never
      comes — the lane's process tree survives its own deadline. So
      the shim ignores the soft-kill signals and keeps waiting, which
      is exactly the lifetime an ``exec``-ed lane had. The lane must
      NOT inherit that immunity (a lane deserves its chance to shut
      down gracefully), and it does not: ignored dispositions survive
      fork and exec, so the subshell resets them before ``exec``-ing.
      Measured against the live pool: 5/5 containment with this, 1/3
      and 2/3 for shapes without it, 3/3 for the plain ``exec`` shim
      it restores parity with. ``SIGKILL`` is unaffected — it cannot
      be ignored, and needs no help.
    - **The subshell ``exec``s the lane**, so it costs a fork but no
      resident process: the running tree is the shim plus the lane,
      exactly one process more than before.
    - **The whole preamble shares line 2 with the lane's ``exec``.**
      When ``execve`` itself fails — a lane binary that is missing or
      not executable — the shell's diagnostic quotes the script's LINE
      NUMBER (``lane.exec: line 2: ...: Permission denied``, and the
      dash and zsh spellings of the same). Written as separate lines
      the preamble pushed the ``exec`` to line 4 and changed that text,
      which is a difference in the lane's error file however cosmetic
      it looks (B round 2, #7136 review). Semicolons put the redirect,
      the trap, and the ``exec`` on one physical line, so the ``exec``
      sits on line 2 exactly where the pre-measurement shim's did.
      Anything inserted ahead of it re-breaks this, which is why the
      equivalence is pinned by comparing against that earlier shim
      rather than against a copied string.

    The contract this holds, in full: for any lane, the shim's exit
    status, stdout, and stderr are byte-identical to the pre-measurement
    shim's — clean exits, non-zero exits, signal deaths, and lane
    binaries that cannot be executed alike. Verified empirically on
    ``bash`` 3.2 (macOS ``/bin/sh``), ``bash`` 5, ``dash`` (Linux
    ``/bin/sh``), and ``zsh``, along with the lane's signal
    dispositions left at their defaults and the shim surviving a soft
    kill to report its lane's real status.
    """
    quoted = " ".join(shlex.quote(argument) for argument in arguments)
    return (
        "#!/bin/sh\n"
        # One line, by construction — see the docstring. The lane's
        # exec must stay on line 2.
        "exec 3>&2 2>/dev/null; "
        f"trap '' {_SOFT_KILL_SIGNALS}; "
        f"( trap - {_SOFT_KILL_SIGNALS}; exec {quoted} 2>&3 3>&- ) 2>/dev/null\n"
        "__lane_status=$?\n"
        f"{compile_rusage_capture(rusage_path)}"
        'exit "$__lane_status"\n'
    )
