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

from ...domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupUnboundedWait,
)
from ...domain.validation_timing import (
    ValidationProcessGroupCleanup,
    ValidationRunLifecycle,
)
from ...infra.env import get_env
from ...infra.validation_timings import (
    ValidateTimingRecorder,
    ValidationDiskObservation,
    ValidationResourceSample,
    ValidationSwapUsage,
)
from ...ports.process_group_supervisor import ProcessGroupSupervisor

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
class _ValidationCommandCompleted:
    """Successful capture lifecycle with the contained child's real status."""

    child_exit_code: int
    duration_seconds: float
    wall_ended_at: datetime
    monotonic_ended_at: float

    @property
    def validation_exit_code(self) -> int:
        return self.child_exit_code

    @property
    def lifecycle(self) -> ValidationRunLifecycle:
        return ValidationRunLifecycle.COMPLETED

    @property
    def process_group_cleanup(self) -> ValidationProcessGroupCleanup:
        return ValidationProcessGroupCleanup.SUPERVISED


@dataclass(frozen=True, slots=True)
class _ValidationCaptureFailed:
    """Incomplete capture retained separately from the child's terminal facts."""

    child_exit_code: int
    process_group_cleanup: ValidationProcessGroupCleanup
    capture_error_type: str
    capture_error_message: str
    duration_seconds: float
    wall_ended_at: datetime
    monotonic_ended_at: float

    def __post_init__(self) -> None:
        if not self.capture_error_type or not self.capture_error_message:
            raise ValueError("validation capture failure requires error evidence")
        if type(self.process_group_cleanup) is not ValidationProcessGroupCleanup:
            raise ValueError(
                "validation capture failure requires typed process cleanup"
            )

    @property
    def validation_exit_code(self) -> int:
        return 1

    @property
    def lifecycle(self) -> ValidationRunLifecycle:
        return ValidationRunLifecycle.CAPTURE_FAILED


_ValidationCommandResult = _ValidationCommandCompleted | _ValidationCaptureFailed


@dataclass(slots=True)
class _ValidationCommandCapture:
    """Own one shell command, its output stream, and its process-group cleanup."""

    command: str
    worktree: Path
    output_file: Path
    timing_recorder: ValidateTimingRecorder
    is_orchestrated_run: bool
    wall_started_at: datetime
    monotonic_started_at: float
    clock: ValidationRunnerClock
    process_group_supervisor: ProcessGroupSupervisor
    process: subprocess.Popen[str] | None = field(default=None, init=False)
    line_count: int = field(default=0, init=False)
    byte_count: int = field(default=0, init=False)
    _execution_entered: bool = field(default=False, init=False)
    _finished: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("validation capture command must not be empty")
        if type(self.is_orchestrated_run) is not bool:
            raise ValueError("validation capture orchestration flag must be boolean")

    def execute(self) -> None:
        """Start, stream, and normally wait for the owned command."""
        if self._execution_entered:
            raise RuntimeError("validation command capture cannot execute twice")
        self._execution_entered = True
        with open(self.output_file, "w", buffering=1) as output_handle:
            output_handle.write(
                f"[validate_runner] start pid={os.getpid()} cwd={self.worktree} "
                f"command={self.command}\n"
            )
            output_handle.flush()
            self.process = subprocess.Popen(
                self.command,
                shell=True,
                cwd=self.worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=os.name == "posix",
            )
            self._emit_to_terminal_and_file(
                f"[validate_runner] child_started pid={self.process.pid}\n",
                output_handle,
            )
            self._stream_output(output_handle)
            self._emit_to_terminal_and_file(
                f"[validate_runner] stdout_eof pid={self.process.pid} "
                f"lines={self.line_count} bytes={self.byte_count} "
                f"elapsed={self._monotonic_elapsed_seconds():.1f}s\n",
                output_handle,
            )
            self._supervise_exit(output_handle)

    def finish_completed(self) -> _ValidationCommandCompleted:
        """Finalize a fully captured command after normal group supervision."""
        if self._finished:
            raise RuntimeError("validation command capture cannot finish twice")
        self._finished = True
        if self.process is None or self.process.returncode is None:
            raise RuntimeError(
                "completed validation capture requires a supervised child result"
            )
        wall_end = self.clock.wall_now()
        monotonic_end = self.clock.monotonic_now()
        result = _ValidationCommandCompleted(
            child_exit_code=self.process.returncode,
            duration_seconds=monotonic_end - self.monotonic_started_at,
            wall_ended_at=wall_end,
            monotonic_ended_at=monotonic_end,
        )
        self._record_terminal_marker(result)
        return result

    def finish_capture_failed(
        self,
        capture_error: BaseException,
    ) -> _ValidationCaptureFailed:
        """Abort incomplete capture and retain failure separately from child exit."""
        if self._finished:
            raise RuntimeError("validation command capture cannot finish twice")
        self._finished = True
        child_exit_code = 127
        process_group_cleanup = ValidationProcessGroupCleanup.NOT_STARTED
        if self.process is not None:
            if self.process.returncode is None:
                termination = self.process_group_supervisor.abort(
                    OwnedProcessGroupLeader(self.process.pid)
                )
                self.process.returncode = termination.leader_exit_code
                process_group_cleanup = (
                    ValidationProcessGroupCleanup.CAPTURE_ABORTED
                )
            else:
                process_group_cleanup = ValidationProcessGroupCleanup.SUPERVISED
            child_exit_code = self.process.returncode
        wall_end = self.clock.wall_now()
        monotonic_end = self.clock.monotonic_now()
        result = _ValidationCaptureFailed(
            child_exit_code=child_exit_code,
            process_group_cleanup=process_group_cleanup,
            capture_error_type=type(capture_error).__name__,
            capture_error_message=str(capture_error),
            duration_seconds=monotonic_end - self.monotonic_started_at,
            wall_ended_at=wall_end,
            monotonic_ended_at=monotonic_end,
        )
        self._record_terminal_marker(result)
        return result

    def _record_terminal_marker(self, result: _ValidationCommandResult) -> None:
        if self.process is not None:
            marker = (
                f"[validate_runner] child_exited pid={self.process.pid} "
                f"child_exit_code={result.child_exit_code} "
                f"validation_exit_code={result.validation_exit_code} "
                f"lifecycle={result.lifecycle.value} "
                f"process_group_cleanup={result.process_group_cleanup.value} "
                f"elapsed={result.duration_seconds:.1f}s "
                f"lines={self.line_count} bytes={self.byte_count}\n"
            )
            with open(self.output_file, "a", encoding="utf-8") as output_handle:
                output_handle.write(marker)
            sys.stdout.write(marker)
            sys.stdout.flush()

    def _stream_output(self, output_handle: TextIO) -> None:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("validation command did not expose stdout")
        for line in self.process.stdout:
            self.line_count += 1
            self.byte_count += len(line.encode("utf-8", errors="replace"))
            self.timing_recorder.process_line(line)
            if not self.is_orchestrated_run:
                sys.stdout.write(line)
                sys.stdout.flush()
            output_handle.write(line)
            output_handle.flush()

    def _supervise_exit(self, output_handle: TextIO) -> None:
        if self.process is None:
            raise RuntimeError("validation command was not started")
        self._emit_to_terminal_and_file(
            f"[validate_runner] supervising_process_group pid={self.process.pid} "
            f"elapsed={self._monotonic_elapsed_seconds():.1f}s "
            "after_stdout_eof\n",
            output_handle,
        )
        supervision = self.process_group_supervisor.supervise(
            OwnedProcessGroupLeader(self.process.pid),
            ProcessGroupUnboundedWait(),
        )
        if type(supervision) is not ProcessGroupCompleted:
            raise AssertionError("an unbounded process-group wait cannot time out")
        self.process.returncode = supervision.termination.leader_exit_code

    @staticmethod
    def _emit_to_terminal_and_file(marker: str, output_handle: TextIO) -> None:
        sys.stdout.write(marker)
        sys.stdout.flush()
        output_handle.write(marker)
        output_handle.flush()

    def _monotonic_elapsed_seconds(self) -> float:
        return self.clock.monotonic_now() - self.monotonic_started_at


def _finalize_validation_evidence(
    *,
    sampler: ResourceSampler,
    sampler_started: bool,
    recorder: ValidateTimingRecorder,
    result: _ValidationCommandResult,
    wall_started_at: datetime,
    monotonic_started_at: float,
) -> None:
    try:
        if sampler_started:
            sampler.stop()
    finally:
        recorder.finalize(
            lifecycle=result.lifecycle,
            process_group_cleanup=result.process_group_cleanup,
            exit_code=result.validation_exit_code,
            child_exit_code=result.child_exit_code,
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
    process_group_supervisor: ProcessGroupSupervisor,
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
    capture = _ValidationCommandCapture(
        command=command,
        worktree=worktree,
        output_file=output_file,
        timing_recorder=timing_recorder,
        is_orchestrated_run=is_orchestrated_run,
        wall_started_at=wall_start,
        monotonic_started_at=start,
        clock=clock,
        process_group_supervisor=process_group_supervisor,
    )
    sampler_started = False
    try:
        resource_sampler.start()
        sampler_started = True
        capture.execute()
    except BaseException as capture_error:
        result = capture.finish_capture_failed(capture_error)
        _finalize_validation_evidence(
            sampler=resource_sampler,
            sampler_started=sampler_started,
            recorder=timing_recorder,
            result=result,
            wall_started_at=wall_start,
            monotonic_started_at=start,
        )
        raise
    else:
        result = capture.finish_completed()
        _finalize_validation_evidence(
            sampler=resource_sampler,
            sampler_started=sampler_started,
            recorder=timing_recorder,
            result=result,
            wall_started_at=wall_start,
            monotonic_started_at=start,
        )

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

    from ..bootstrap import build_process_group_supervisor

    exit_code = run_validation(
        command,
        output_dir,
        worktree,
        clock=SYSTEM_VALIDATION_RUNNER_CLOCK,
        process_group_supervisor=build_process_group_supervisor(),
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
