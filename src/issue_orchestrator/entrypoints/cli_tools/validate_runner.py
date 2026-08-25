"""Validation runner with output capture.

This module provides a CLI that runs validation commands and captures
output to a known location, so agents can find failure details without
re-running tests.

Output location is determined by ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR.
If not set, defaults to .issue-orchestrator/diagnostics/ for direct runs.

On failure, prints the path to the output file so agents know where to look.

Usage:
    python -m issue_orchestrator.entrypoints.cli_tools.validate_runner
    python -m issue_orchestrator.entrypoints.cli_tools.validate_runner --command "pytest tests/"

Exit codes:
    Same as the underlying validation command
"""

import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from ...domain.contained_command import (
    ContainedCommandCaptureAborted,
    ContainedCommandCaptureFailed,
    ContainedCommandCleanupError,
    ContainedCommandCleanupFailed,
    ContainedCommandCleanupNotStarted,
    ContainedCommandCompleted,
    ContainedCommandExited,
    ContainedCommandExitUnknown,
    ContainedCommandFailure,
    ContainedCommandMetrics,
    ContainedCommandNotStarted,
    ContainedCommandResult,
    ContainedCommandStarted,
    ContainedCommandSupervised,
)
from ...infra.env import get_env
from ...infra.validation_timings import (
    ValidateTimingRecorder,
    ValidationDiskObservation,
    ValidationResourceSample,
    ValidationSwapUsage,
)
from ...ports.contained_command import (
    ContainedCommandCapture,
    ContainedCommandLineObserver,
    ContainedCommandOutput,
    ContainedShellCommand,
)

_MEMORY_FREE_RE = re.compile(r"System-wide memory free percentage:\s*(?P<percent>\d+)%")
_SWAP_RE = re.compile(
    r"total = (?P<total>[0-9.]+)M\s+used = (?P<used>[0-9.]+)M\s+free = (?P<free>[0-9.]+)M"
)


@dataclass(frozen=True, slots=True)
class ValidationRunnerClock:
    """Required wall and monotonic clocks for one validation invocation."""

    wall_now: Callable[[], datetime]
    monotonic_now: Callable[[], float]

    def __post_init__(self) -> None:
        if not callable(self.wall_now):
            raise ValueError("validation wall clock must be callable")
        if not callable(self.monotonic_now):
            raise ValueError("validation monotonic clock must be callable")


SYSTEM_VALIDATION_RUNNER_CLOCK = ValidationRunnerClock(
    wall_now=lambda: datetime.now(timezone.utc),
    monotonic_now=time.monotonic,
)


def find_worktree_root() -> Path:
    """Find the worktree root by looking for .git."""
    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        if (path / ".git").exists():
            return path
    return cwd


def get_output_dir(worktree: Path) -> Path:
    """Get the output directory for validation output.

    Checks ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR env var first,
    falls back to .issue-orchestrator/diagnostics/ for direct runs.

    Args:
        worktree: Path to the worktree root

    Returns:
        Path to the output directory
    """
    env_dir = get_env("VALIDATION_OUTPUT_DIR")
    if env_dir:
        return Path(env_dir)
    # Default location for direct runs (not orchestrator-managed).
    return worktree / ".issue-orchestrator" / "diagnostics"


def load_validation_cmd(worktree: Path) -> str | None:
    """Load quick validation command from config.

    Args:
        worktree: Path to the worktree root

    Returns:
        Validation command string, or None if not configured
    """
    from ...infra.config import load_runtime_validation_config

    validation_config = load_runtime_validation_config(worktree)
    quick_config = validation_config.get("quick", {}) or {}
    return quick_config.get("cmd")


def run_command_text(args: list[str], *, cwd: Path) -> str | None:
    """Best-effort subprocess wrapper for lightweight host probes."""
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def parse_memory_free_percent(output: str | None) -> int | None:
    """Parse `memory_pressure -Q` output."""
    if not output:
        return None
    match = _MEMORY_FREE_RE.search(output)
    if not match:
        return None
    return int(match.group("percent"))


def parse_swap_usage(output: str | None) -> ValidationSwapUsage | None:
    """Parse `sysctl vm.swapusage` output into MiB values."""
    if not output:
        return None
    match = _SWAP_RE.search(output)
    if not match:
        return None
    return ValidationSwapUsage(
        total_mb=float(match.group("total")),
        used_mb=float(match.group("used")),
        free_mb=float(match.group("free")),
    )


def parse_iostat_totals(output: str | None) -> ValidationDiskObservation | None:
    """Parse `iostat -Id disk0` cumulative transfer/MB totals."""
    if not output:
        return None
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) < 3:
        return None
    parts = lines[-1].split()
    if len(parts) < 3:
        return None
    try:
        xfrs = float(parts[-2])
        mb = float(parts[-1])
    except ValueError:
        return None
    return ValidationDiskObservation(
        transfers_total=xfrs,
        megabytes_total=mb,
        transfers_delta=None,
        megabytes_delta=None,
    )


@dataclass
class ResourceSampler:
    """Periodic host resource sampler for validate runs."""

    worktree: Path
    recorder: ValidateTimingRecorder
    sample_interval_seconds: float = 5.0
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _last_disk_totals: ValidationDiskObservation | None = field(
        default=None,
        init=False,
    )

    def start(self) -> None:
        self.recorder.append_resource_sample(self._collect_sample())
        self._thread = threading.Thread(
            target=self._run, name="validate-resource-sampler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.sample_interval_seconds + 1.0)

    def _run(self) -> None:
        while not self._stop_event.wait(self.sample_interval_seconds):
            self.recorder.append_resource_sample(self._collect_sample())

    def _collect_sample(self) -> ValidationResourceSample:
        loadavg_1m: float | None = None
        loadavg_5m: float | None = None
        loadavg_15m: float | None = None
        try:
            load1, load5, load15 = os.getloadavg()
            loadavg_1m = round(load1, 3)
            loadavg_5m = round(load5, 3)
            loadavg_15m = round(load15, 3)
        except OSError:
            pass

        # These probes are macOS-specific today. Linux validate runs still record
        # load averages, and we can add /proc-based probes later if CI analysis
        # needs the same memory/swap/disk visibility.
        memory_output = run_command_text(["memory_pressure", "-Q"], cwd=self.worktree)
        free_percent = parse_memory_free_percent(memory_output)

        swap_output = run_command_text(["sysctl", "vm.swapusage"], cwd=self.worktree)
        swap_usage = parse_swap_usage(swap_output)

        disk_output = run_command_text(["iostat", "-Id", "disk0"], cwd=self.worktree)
        disk_totals = parse_iostat_totals(disk_output)
        if disk_totals is not None:
            if self._last_disk_totals is not None:
                disk_totals = ValidationDiskObservation(
                    transfers_total=disk_totals.transfers_total,
                    megabytes_total=disk_totals.megabytes_total,
                    transfers_delta=round(
                        disk_totals.transfers_total
                        - self._last_disk_totals.transfers_total,
                        3,
                    ),
                    megabytes_delta=round(
                        disk_totals.megabytes_total
                        - self._last_disk_totals.megabytes_total,
                        3,
                    ),
                )
            self._last_disk_totals = disk_totals

        return ValidationResourceSample(
            recorded_at=datetime.now(timezone.utc).isoformat(),
            loadavg_1m=loadavg_1m,
            loadavg_5m=loadavg_5m,
            loadavg_15m=loadavg_15m,
            memory_free_percent=free_percent,
            swap=swap_usage,
            disk=disk_totals,
        )


@dataclass(frozen=True, slots=True)
class _TimedValidationCommandResult:
    """One contained terminal fact paired with monotonic/wall-clock evidence."""

    command_result: ContainedCommandResult
    duration_seconds: float
    wall_ended_at: datetime
    monotonic_ended_at: float

    def __post_init__(self) -> None:
        if type(self.command_result) not in (
            ContainedCommandCompleted,
            ContainedCommandCaptureFailed,
            ContainedCommandCleanupFailed,
        ):
            raise ValueError(
                "timed validation result requires a closed contained-command result"
            )
        if type(self.duration_seconds) is not float or self.duration_seconds < 0.0:
            raise ValueError(
                "timed validation result duration must be a non-negative float"
            )

    @property
    def validation_exit_code(self) -> int:
        if type(self.command_result) is ContainedCommandCompleted:
            return self.command_result.child.exit_code
        return 1


@dataclass(slots=True)
class _ValidationCommandOutput(ContainedCommandOutput):
    """Validation-specific terminal/file adapter for raw contained output."""

    output_handle: TextIO
    is_orchestrated_run: bool

    def __post_init__(self) -> None:
        if type(self.is_orchestrated_run) is not bool:
            raise ValueError("validation output orchestration flag must be boolean")

    def child_started(self, started: ContainedCommandStarted) -> None:
        if type(started) is not ContainedCommandStarted:
            raise ValueError("validation output requires ContainedCommandStarted")
        self._emit_to_terminal_and_file(
            f"[validate_runner] child_started pid={started.process_id}\n"
        )

    def write_line(self, line: str) -> None:
        if type(line) is not str:
            raise ValueError("validation output line must be text")
        if not self.is_orchestrated_run:
            sys.stdout.write(line)
            sys.stdout.flush()
        self.output_handle.write(line)
        self.output_handle.flush()

    def _emit_to_terminal_and_file(self, marker: str) -> None:
        sys.stdout.write(marker)
        sys.stdout.flush()
        self.output_handle.write(marker)
        self.output_handle.flush()


@dataclass(frozen=True, slots=True)
class _ValidationTimingLineObserver(ContainedCommandLineObserver):
    recorder: ValidateTimingRecorder

    def __post_init__(self) -> None:
        if type(self.recorder) is not ValidateTimingRecorder:
            raise ValueError(
                "validation line observer requires ValidateTimingRecorder"
            )

    def observe_line(self, line: str) -> None:
        self.recorder.process_line(line)


def _command_result_cleanup(result: ContainedCommandResult) -> str:
    if type(result) is ContainedCommandCompleted:
        return "supervised"
    if type(result) is ContainedCommandCleanupFailed:
        return "cleanup-failed"
    if type(result) is ContainedCommandCaptureFailed:
        if type(result.cleanup) is ContainedCommandSupervised:
            return "supervised"
        if type(result.cleanup) is ContainedCommandCaptureAborted:
            return "capture-aborted"
        if type(result.cleanup) is ContainedCommandCleanupNotStarted:
            return "not-started"
    raise AssertionError("closed command result has unknown cleanup fact")


def _command_result_child_exit(result: ContainedCommandResult) -> str:
    child = result.child
    if type(child) is ContainedCommandExited:
        return str(child.exit_code)
    if type(child) is ContainedCommandNotStarted:
        return "not-started"
    if type(child) is ContainedCommandExitUnknown:
        return "unknown"
    raise AssertionError("closed command result has unknown child fact")


def _command_result_process_id(result: ContainedCommandResult) -> str:
    child = result.child
    if type(child) is ContainedCommandNotStarted:
        return "not-started"
    if type(child) is ContainedCommandExited:
        return str(child.process_id)
    if type(child) is ContainedCommandExitUnknown:
        return str(child.process_id)
    raise AssertionError("closed command result has unknown child identity")


def _record_terminal_marker(
    output_file: Path,
    result: _TimedValidationCommandResult,
) -> None:
    command_result = result.command_result
    lifecycle = (
        "completed"
        if type(command_result) is ContainedCommandCompleted
        else "capture-failed"
    )
    terminal_marker = (
        "child_exited"
        if type(command_result.child) is ContainedCommandExited
        else "command_terminal"
    )
    marker = (
        f"[validate_runner] {terminal_marker} "
        f"pid={_command_result_process_id(command_result)} "
        f"child_exit_code={_command_result_child_exit(command_result)} "
        f"validation_exit_code={result.validation_exit_code} "
        f"lifecycle={lifecycle} "
        f"process_group_cleanup={_command_result_cleanup(command_result)} "
        f"elapsed={result.duration_seconds:.1f}s "
        f"lines={command_result.metrics.line_count} "
        f"bytes={command_result.metrics.byte_count}\n"
    )
    eof_marker = (
        "[validate_runner] stdout_eof "
        f"pid={_command_result_process_id(command_result)} "
        f"lines={command_result.metrics.line_count} "
        f"bytes={command_result.metrics.byte_count} "
        f"elapsed={result.duration_seconds:.1f}s\n"
    )
    with open(output_file, "a", encoding="utf-8") as output_handle:
        output_handle.write(eof_marker)
        output_handle.write(marker)
    sys.stdout.write(eof_marker)
    sys.stdout.write(marker)
    sys.stdout.flush()


def _not_started_capture_failure(
    error: BaseException,
) -> ContainedCommandCaptureFailed:
    return ContainedCommandCaptureFailed(
        child=ContainedCommandNotStarted(),
        cleanup=ContainedCommandCleanupNotStarted(),
        failure=ContainedCommandFailure(error),
        metrics=ContainedCommandMetrics(line_count=0, byte_count=0),
    )


def _finalize_validation_evidence(
    *,
    sampler: ResourceSampler,
    sampler_started: bool,
    recorder: ValidateTimingRecorder,
    result: _TimedValidationCommandResult,
    wall_started_at: datetime,
    monotonic_started_at: float,
) -> None:
    try:
        if sampler_started:
            sampler.stop()
    finally:
        recorder.finalize(
            command_result=result.command_result,
            total_elapsed_seconds=result.duration_seconds,
            wall_started_at=wall_started_at,
            monotonic_started_at=monotonic_started_at,
            wall_ended_at=result.wall_ended_at,
            monotonic_ended_at=result.monotonic_ended_at,
        )


def run_validation(
    command: str,
    output_dir: Path,
    worktree: Path,
    *,
    clock: ValidationRunnerClock,
    contained_command_capture: ContainedCommandCapture,
) -> int:
    """Run validation command and capture output.

    Args:
        command: Command to run
        output_dir: Directory to write output to
        worktree: Working directory for command

    Returns:
        Exit code from the command
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "validation-output.log"
    is_orchestrated_run = get_env("VALIDATION_OUTPUT_DIR") is not None
    timing_recorder = ValidateTimingRecorder(worktree=worktree, command=command)
    resource_sampler = ResourceSampler(worktree=worktree, recorder=timing_recorder)

    print(f"Running: {command}")
    print(f"Output will be saved to: {output_file}")
    if is_orchestrated_run:
        print(
            "[orchestrated] full output -> file; terminal shows lifecycle markers only"
        )
    print()

    wall_start = clock.wall_now()
    start = clock.monotonic_now()
    sampler_started = False
    with open(output_file, "w", buffering=1) as output_handle:
        output_handle.write(
            f"[validate_runner] start pid={os.getpid()} cwd={worktree} "
            f"command={command}\n"
        )
        output_handle.flush()
        try:
            resource_sampler.start()
        except BaseException as sampler_start_error:
            command_result: ContainedCommandResult = _not_started_capture_failure(
                sampler_start_error
            )
        else:
            sampler_started = True
            command_result = contained_command_capture.capture(
                ContainedShellCommand(command=command, working_directory=worktree),
                _ValidationCommandOutput(
                    output_handle=output_handle,
                    is_orchestrated_run=is_orchestrated_run,
                ),
                _ValidationTimingLineObserver(timing_recorder),
            )

    wall_end = clock.wall_now()
    monotonic_end = clock.monotonic_now()
    result = _TimedValidationCommandResult(
        command_result=command_result,
        duration_seconds=monotonic_end - start,
        wall_ended_at=wall_end,
        monotonic_ended_at=monotonic_end,
    )
    _finalize_validation_evidence(
        sampler=resource_sampler,
        sampler_started=sampler_started,
        recorder=timing_recorder,
        result=result,
        wall_started_at=wall_start,
        monotonic_started_at=start,
    )
    _record_terminal_marker(output_file, result)

    if type(command_result) is ContainedCommandCaptureFailed:
        raise command_result.failure.error
    if type(command_result) is ContainedCommandCleanupFailed:
        cleanup_error = ContainedCommandCleanupError(command_result)
        raise cleanup_error from command_result.cleanup_failure.error

    print()
    if result.validation_exit_code == 0:
        print(f"Validation PASSED (exit code 0) in {result.duration_seconds:.1f}s")
        print(f"Full output saved to: {output_file}")
    else:
        print("=" * 60)
        print(
            f"Validation FAILED (exit code {result.validation_exit_code}) in "
            f"{result.duration_seconds:.1f}s"
        )
        print("=" * 60)
        print()
        print("Full output saved to:")
        print(f"  {output_file}")
        print()
        print(f"To view: cat {output_file}")
        print("=" * 60)

    return result.validation_exit_code


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Run validation with output capture",
    )
    parser.add_argument(
        "--command",
        "-c",
        help="Validation command to run (default: from config)",
    )

    args = parser.parse_args()

    worktree = find_worktree_root()
    output_dir = get_output_dir(worktree)

    # Determine command to run
    command = args.command
    if not command:
        command = load_validation_cmd(worktree)
    if not command:
        print("ERROR: No validation command configured.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Either:", file=sys.stderr)
        print("  1. Pass --command 'your command here'", file=sys.stderr)
        print(
            "  2. Configure validation.quick.cmd in "
            ".issue-orchestrator/config/modes/<mode>/*.yaml",
            file=sys.stderr,
        )
        sys.exit(2)

    from ..bootstrap import build_contained_command_capture

    exit_code = run_validation(
        command,
        output_dir,
        worktree,
        clock=SYSTEM_VALIDATION_RUNNER_CLOCK,
        contained_command_capture=build_contained_command_capture(),
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
