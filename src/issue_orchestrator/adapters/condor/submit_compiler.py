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

from ...domain.lane_execution import LaneCommand, LaneResources

# Grace between the scheduler's soft kill and its hard kill when a job
# is removed (deadline or cancellation). Mirrors the direct backend's
# TERM-then-KILL escalation.
_REMOVAL_GRACE_SECONDS = 10


@dataclass(frozen=True, slots=True)
class CompiledSubmitDescription:
    """One complete, self-contained job description.

    The lane's exact argv is compiled into an executable shell shim
    (``exec_script_text``) rather than the scheduler's line-oriented
    ``arguments`` syntax, which cannot carry newlines — and lane
    commands legitimately contain them (``python -c`` scripts).
    """

    text: str
    exec_script_path: Path
    exec_script_text: str
    output_path: Path
    error_path: Path
    event_log_path: Path


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
    output_path = run_directory / "lane.out"
    error_path = run_directory / "lane.err"
    event_log_path = run_directory / "lane.events"
    exec_script_path = run_directory / "lane.exec"
    # Ceiling, never floor: a floored deadline would let the scheduler
    # remove a lane before its promised budget elapsed.
    timeout = max(1, math.ceil(command.deadline.timeout_seconds))
    lines = [
        f"# lane: {command.work_key.value}",
        "universe = vanilla",
        f"executable = {exec_script_path}",
        f"initialdir = {command.working_directory}",
        "getenv = true",
        f"output = {output_path}",
        f"error = {error_path}",
        f"log = {event_log_path}",
        f"request_cpus = {resources.request_cpus}",
        "should_transfer_files = NO",
        "notification = never",
        f"job_max_vacate_time = {_REMOVAL_GRACE_SECONDS}",
        "periodic_remove = (JobStatus == 2) && "
        f"((time() - JobCurrentStartDate) > {timeout})",
    ]
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
        exec_script_text=_compile_exec_script(command.arguments),
        output_path=output_path,
        error_path=error_path,
        event_log_path=event_log_path,
    )


def _compile_exec_script(arguments: tuple[str, ...]) -> str:
    """Compile the exact argv into a POSIX-sh exec shim.

    ``shlex.quote`` escaping survives spaces, quotes, and newlines,
    so the lane runs with byte-exact arguments.
    """
    quoted = " ".join(shlex.quote(argument) for argument in arguments)
    return f"#!/bin/sh\nexec {quoted}\n"
