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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ...infra.env import get_env
from ...infra.validation_timings import ValidateTimingRecorder

_MEMORY_FREE_RE = re.compile(r"System-wide memory free percentage:\s*(?P<percent>\d+)%")
_SWAP_RE = re.compile(
    r"total = (?P<total>[0-9.]+)M\s+used = (?P<used>[0-9.]+)M\s+free = (?P<free>[0-9.]+)M"
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
    # Fallback for direct runs (not orchestrator-managed)
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


def parse_swap_usage(output: str | None) -> dict[str, float] | None:
    """Parse `sysctl vm.swapusage` output into MiB values."""
    if not output:
        return None
    match = _SWAP_RE.search(output)
    if not match:
        return None
    return {
        "swap_total_mb": float(match.group("total")),
        "swap_used_mb": float(match.group("used")),
        "swap_free_mb": float(match.group("free")),
    }


def parse_iostat_totals(output: str | None) -> dict[str, float] | None:
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
    return {
        "disk_xfrs_total": xfrs,
        "disk_mb_total": mb,
    }


@dataclass
class ResourceSampler:
    """Periodic host resource sampler for validate runs."""

    worktree: Path
    recorder: ValidateTimingRecorder
    sample_interval_seconds: float = 5.0
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _last_disk_totals: dict[str, float] | None = field(default=None, init=False)

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

    def _collect_sample(self) -> dict[str, object]:
        sample: dict[str, object] = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            load1, load5, load15 = os.getloadavg()
            sample["loadavg_1m"] = round(load1, 3)
            sample["loadavg_5m"] = round(load5, 3)
            sample["loadavg_15m"] = round(load15, 3)
        except OSError:
            pass

        # These probes are macOS-specific today. Linux validate runs still record
        # load averages, and we can add /proc-based probes later if CI analysis
        # needs the same memory/swap/disk visibility.
        memory_output = run_command_text(["memory_pressure", "-Q"], cwd=self.worktree)
        free_percent = parse_memory_free_percent(memory_output)
        if free_percent is not None:
            sample["memory_free_percent"] = free_percent

        swap_output = run_command_text(["sysctl", "vm.swapusage"], cwd=self.worktree)
        swap_usage = parse_swap_usage(swap_output)
        if swap_usage is not None:
            sample.update(swap_usage)

        disk_output = run_command_text(["iostat", "-Id", "disk0"], cwd=self.worktree)
        disk_totals = parse_iostat_totals(disk_output)
        if disk_totals is not None:
            sample.update(disk_totals)
            if self._last_disk_totals is not None:
                sample["disk_xfrs_delta"] = round(
                    disk_totals["disk_xfrs_total"]
                    - self._last_disk_totals["disk_xfrs_total"],
                    3,
                )
                sample["disk_mb_delta"] = round(
                    disk_totals["disk_mb_total"]
                    - self._last_disk_totals["disk_mb_total"],
                    3,
                )
            self._last_disk_totals = disk_totals

        return sample


def run_validation(command: str, output_dir: Path, worktree: Path) -> int:
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
    line_count = 0
    byte_count = 0
    timing_recorder = ValidateTimingRecorder(worktree=worktree, command=command)
    resource_sampler = ResourceSampler(worktree=worktree, recorder=timing_recorder)

    print(f"Running: {command}")
    print(f"Output will be saved to: {output_file}")
    if is_orchestrated_run:
        print(
            "[orchestrated] full output -> file; terminal shows lifecycle markers only"
        )
    print()

    wall_start = datetime.now(timezone.utc)
    start = time.monotonic()
    resource_sampler.start()

    # Run command, capturing output while also displaying it
    # Use line buffering (buffering=1) to ensure output is written immediately
    with open(output_file, "w", buffering=1) as f:
        f.write(
            f"[validate_runner] start pid={os.getpid()} cwd={worktree} command={command}\n"
        )
        f.flush()
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
        )
        child_pid = process.pid
        start_marker = f"[validate_runner] child_started pid={child_pid}\n"
        sys.stdout.write(start_marker)
        sys.stdout.flush()
        f.write(start_marker)
        f.flush()

        # Stream output to both file and terminal
        assert process.stdout is not None  # For type checker
        for line in process.stdout:
            line_count += 1
            byte_count += len(line.encode("utf-8", errors="replace"))
            timing_recorder.process_line(line)
            if not is_orchestrated_run:
                sys.stdout.write(line)
                sys.stdout.flush()
            f.write(line)
            f.flush()  # Ensure output is written even if process crashes

        eof_marker = (
            f"[validate_runner] stdout_eof pid={child_pid} "
            f"lines={line_count} bytes={byte_count} "
            f"elapsed={(datetime.now(timezone.utc) - wall_start).total_seconds():.1f}s\n"
        )
        sys.stdout.write(eof_marker)
        sys.stdout.flush()
        f.write(eof_marker)
        f.flush()

        while True:
            try:
                process.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                wait_marker = (
                    f"[validate_runner] waiting_for_exit pid={child_pid} "
                    f"elapsed={(datetime.now(timezone.utc) - wall_start).total_seconds():.1f}s after_stdout_eof\n"
                )
                sys.stdout.write(wait_marker)
                sys.stdout.flush()
                f.write(wait_marker)
                f.flush()

    duration = 0.0
    monotonic_duration = 0.0
    wall_end = wall_start
    monotonic_end = start
    exit_code = process.returncode if process.returncode is not None else 1
    try:
        wall_end = datetime.now(timezone.utc)
        monotonic_end = time.monotonic()
        duration = (wall_end - wall_start).total_seconds()
        monotonic_duration = monotonic_end - start
        exit_code = process.returncode if process.returncode is not None else 1
        exit_marker = (
            f"[validate_runner] child_exited pid={child_pid} exit_code={exit_code} "
            f"elapsed={duration:.1f}s monotonic_elapsed={monotonic_duration:.1f}s "
            f"lines={line_count} bytes={byte_count}\n"
        )
        # The exit marker is computed only after the child has fully exited and the
        # main streaming file handle is closed, so append it in a short final write.
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(exit_marker)
        sys.stdout.write(exit_marker)
        sys.stdout.flush()
    finally:
        try:
            resource_sampler.stop()
        finally:
            timing_recorder.finalize(
                exit_code=exit_code,
                total_elapsed_seconds=duration,
                wall_started_at=wall_start,
                monotonic_started_at=start,
                wall_ended_at=wall_end,
                monotonic_ended_at=monotonic_end,
            )

    print()
    if exit_code == 0:
        print(f"Validation PASSED (exit code 0) in {duration:.1f}s")
        print(f"Full output saved to: {output_file}")
    else:
        print("=" * 60)
        print(f"Validation FAILED (exit code {exit_code}) in {duration:.1f}s")
        print("=" * 60)
        print()
        print("Full output saved to:")
        print(f"  {output_file}")
        print()
        print(f"To view: cat {output_file}")
        print("=" * 60)

    return exit_code


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

    exit_code = run_validation(command, output_dir, worktree)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
