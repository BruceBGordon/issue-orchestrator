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
import sys
import time
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TextIO, cast

from ...domain.contained_command import (
    ContainedCommandCaptureAborted,
    ContainedCommandCaptureFailed,
    ContainedCommandCaptureInterrupted,
    ContainedCommandCaptureSucceeded,
    ContainedCommandCleanupError,
    ContainedCommandCleanupFailed,
    ContainedCommandCleanupNotStarted,
    ContainedCommandCompleted,
    ContainedCommandExited,
    ContainedCommandExitUnknown,
    ContainedCommandFailure,
    ContainedCommandFinalizationFailed,
    ContainedCommandMetrics,
    ContainedCommandNotStarted,
    ContainedCommandOutcomeUnavailable,
    ContainedCommandResult,
    ContainedCommandStarted,
    ContainedCommandSupervised,
)
from ...infra.env import get_env
from ...infra.validation_timings import (
    ValidateTimingRecorder,
)
from ...domain.validation_resource_sampling import (
    ValidationResourceSamplerStart,
    ValidationResourceSamplerStarted,
    ValidationResourceSamplerStartIndeterminate,
    ValidationResourceSamplerStartRejected,
    ValidationResourceSamplingPolicy,
    validation_resource_sampler_shutdown_failure,
)
from ...execution.validation_resource_sampling import (
    SystemValidationResourceProbe,
    ValidationResourceSampler,
)
from ...ports.contained_command import (
    ContainedCommandCapture,
    ContainedCommandLineObserver,
    ContainedCommandOutput,
    ContainedShellCommand,
)
from ...ports.retained_thread import RetainedThreadFactory
from ...ports.validation_host_probe import ValidationHostProbe

_RESOURCE_SAMPLING_POLICY = ValidationResourceSamplingPolicy(
    sample_interval_seconds=5.0,
    probe_timeout_seconds=1.0,
    shutdown_timeout_seconds=4.0,
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


def _require_validation_wall_datetime(value: object, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise ValueError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _require_validation_monotonic(value: object, field_name: str) -> float:
    if type(value) is not float or not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return value


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


@dataclass(frozen=True, slots=True)
class _ValidationTimingAvailable:
    """Complete monotonic and wall-clock evidence for one command."""

    duration_seconds: float
    wall_ended_at: datetime
    monotonic_ended_at: float

    def __post_init__(self) -> None:
        _require_validation_monotonic(
            self.duration_seconds,
            "validation timing duration",
        )
        _require_validation_wall_datetime(
            self.wall_ended_at,
            "validation wall end",
        )
        _require_validation_monotonic(
            self.monotonic_ended_at,
            "validation monotonic end",
        )


@dataclass(frozen=True, slots=True)
class _ValidationTimingUnavailable:
    """Exact failures that prevented a complete timing observation."""

    failures: tuple[ContainedCommandFailure, ...]

    def __post_init__(self) -> None:
        if type(self.failures) is not tuple or not self.failures:
            raise ValueError("unavailable validation timing requires failures")
        if any(
            type(failure) is not ContainedCommandFailure for failure in self.failures
        ):
            raise ValueError("validation timing failures must be typed")


_ValidationTiming = _ValidationTimingAvailable | _ValidationTimingUnavailable


class _ValidationTerminalLifecycle(StrEnum):
    """Exact lifecycle vocabulary emitted by the human terminal marker."""

    COMPLETED = "completed"
    CAPTURE_FAILED = "capture-failed"
    FINALIZATION_FAILED = "finalization-failed"
    CLEANUP_FAILED = "cleanup-failed"
    OUTCOME_UNAVAILABLE = "outcome-unavailable"


@dataclass(frozen=True, slots=True)
class _ValidationCommandResult:
    """One contained terminal fact paired with exact timing evidence."""

    command_result: ContainedCommandResult
    timing: _ValidationTiming

    def __post_init__(self) -> None:
        if type(self.command_result) not in (
            ContainedCommandCompleted,
            ContainedCommandCaptureFailed,
            ContainedCommandFinalizationFailed,
            ContainedCommandOutcomeUnavailable,
            ContainedCommandCleanupFailed,
        ):
            raise ValueError(
                "validation result requires a closed contained-command result"
            )
        if type(self.timing) not in (
            _ValidationTimingAvailable,
            _ValidationTimingUnavailable,
        ):
            raise ValueError("validation result requires closed timing evidence")

    @property
    def validation_exit_code(self) -> int:
        if type(self.command_result) is ContainedCommandCompleted:
            return self.command_result.child.exit_code
        return 1

    @property
    def passed(self) -> bool:
        """Whether validation completed with the success exit status."""
        return self.validation_exit_code == 0

    @property
    def elapsed_marker(self) -> str:
        if type(self.timing) is _ValidationTimingUnavailable:
            return "unavailable"
        if type(self.timing) is _ValidationTimingAvailable:
            return f"{self.timing.duration_seconds:.1f}s"
        raise AssertionError("validation timing evidence is a closed union")

    @property
    def duration_seconds(self) -> float:
        if type(self.timing) is _ValidationTimingUnavailable:
            raise AssertionError("failed validation timing has no duration")
        if type(self.timing) is _ValidationTimingAvailable:
            return self.timing.duration_seconds
        raise AssertionError("validation timing evidence is a closed union")


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
            raise ValueError("validation line observer requires ValidateTimingRecorder")

    def observe_line(self, line: str) -> None:
        self.recorder.process_line(line)


def _command_result_cleanup(result: ContainedCommandResult) -> str:
    if type(result) is ContainedCommandCompleted:
        return "supervised"
    if type(result) is ContainedCommandCleanupFailed:
        return "cleanup-failed"
    if type(result) is ContainedCommandOutcomeUnavailable:
        return "unknown"
    if type(result) is ContainedCommandFinalizationFailed:
        return _contained_cleanup_value(result.cleanup)
    if type(result) is ContainedCommandCaptureFailed:
        return _contained_cleanup_value(result.cleanup)
    raise AssertionError("closed command result has unknown cleanup fact")


def _contained_cleanup_value(
    cleanup: ContainedCommandSupervised
    | ContainedCommandCaptureAborted
    | ContainedCommandCleanupNotStarted,
) -> str:
    if type(cleanup) is ContainedCommandSupervised:
        return "supervised"
    if type(cleanup) is ContainedCommandCaptureAborted:
        return "capture-aborted"
    if type(cleanup) is ContainedCommandCleanupNotStarted:
        return "not-started"
    raise AssertionError("contained cleanup is a closed union")


def _command_result_child_exit(result: ContainedCommandResult) -> str:
    if type(result) is ContainedCommandOutcomeUnavailable:
        return "unavailable"
    closed_result = cast(
        ContainedCommandCompleted
        | ContainedCommandCaptureFailed
        | ContainedCommandFinalizationFailed
        | ContainedCommandCleanupFailed,
        result,
    )
    child = closed_result.child
    if type(child) is ContainedCommandExited:
        return str(child.exit_code)
    if type(child) is ContainedCommandNotStarted:
        return "not-started"
    if type(child) is ContainedCommandExitUnknown:
        return "unknown"
    raise AssertionError("closed command result has unknown child fact")


def _command_result_process_id(result: ContainedCommandResult) -> str:
    if type(result) is ContainedCommandOutcomeUnavailable:
        return "unavailable"
    closed_result = cast(
        ContainedCommandCompleted
        | ContainedCommandCaptureFailed
        | ContainedCommandFinalizationFailed
        | ContainedCommandCleanupFailed,
        result,
    )
    child = closed_result.child
    if type(child) is ContainedCommandNotStarted:
        return "not-started"
    if type(child) is ContainedCommandExited:
        return str(child.process_id)
    if type(child) is ContainedCommandExitUnknown:
        return str(child.process_id)
    raise AssertionError("closed command result has unknown child identity")


@dataclass(frozen=True, slots=True)
class _ValidationTerminalMarkers:
    eof: str
    terminal: str

    def __post_init__(self) -> None:
        if type(self.eof) is not str or not self.eof:
            raise ValueError("validation EOF marker must be non-empty")
        if type(self.terminal) is not str or not self.terminal:
            raise ValueError("validation terminal marker must be non-empty")


def _build_terminal_markers(
    result: _ValidationCommandResult,
) -> _ValidationTerminalMarkers:
    command_result = result.command_result
    lifecycle = _command_result_lifecycle(command_result)
    terminal_marker = _command_result_terminal_marker(command_result)
    metrics = _command_result_metrics(command_result)
    return _ValidationTerminalMarkers(
        eof=(
            "[validate_runner] stdout_eof "
            f"pid={_command_result_process_id(command_result)} "
            f"{metrics} "
            f"elapsed={result.elapsed_marker}\n"
        ),
        terminal=(
            f"[validate_runner] {terminal_marker} "
            f"pid={_command_result_process_id(command_result)} "
            f"child_exit_code={_command_result_child_exit(command_result)} "
            f"validation_exit_code={result.validation_exit_code} "
            f"lifecycle={lifecycle.value} "
            f"process_group_cleanup={_command_result_cleanup(command_result)} "
            f"elapsed={result.elapsed_marker} "
            f"{metrics}\n"
        ),
    )


def _append_terminal_file_markers(
    output_file: Path,
    markers: _ValidationTerminalMarkers,
) -> tuple[ContainedCommandFailure, ...]:
    failures: list[ContainedCommandFailure] = []
    try:
        output_handle = open(output_file, "a", encoding="utf-8")
    except BaseException as error:
        failures.append(ContainedCommandFailure(error))
    else:
        try:
            output_handle.write(markers.eof)
            output_handle.write(markers.terminal)
        except BaseException as error:
            failures.append(ContainedCommandFailure(error))
        finally:
            try:
                output_handle.close()
            except BaseException as error:
                failures.append(ContainedCommandFailure(error))
    return tuple(failures)


def _write_terminal_console_markers(
    markers: _ValidationTerminalMarkers,
) -> tuple[ContainedCommandFailure, ...]:
    failures: list[ContainedCommandFailure] = []
    try:
        sys.stdout.write(markers.eof)
        sys.stdout.write(markers.terminal)
        sys.stdout.flush()
    except BaseException as error:
        failures.append(ContainedCommandFailure(error))
    return tuple(failures)


def _command_result_lifecycle(
    result: ContainedCommandResult,
) -> _ValidationTerminalLifecycle:
    if type(result) is ContainedCommandCompleted:
        return _ValidationTerminalLifecycle.COMPLETED
    if type(result) is ContainedCommandCaptureFailed:
        return _ValidationTerminalLifecycle.CAPTURE_FAILED
    if type(result) is ContainedCommandFinalizationFailed:
        return _ValidationTerminalLifecycle.FINALIZATION_FAILED
    if type(result) is ContainedCommandCleanupFailed:
        return _ValidationTerminalLifecycle.CLEANUP_FAILED
    if type(result) is ContainedCommandOutcomeUnavailable:
        return _ValidationTerminalLifecycle.OUTCOME_UNAVAILABLE
    raise AssertionError("closed command result has unknown lifecycle fact")


def _command_result_terminal_marker(result: ContainedCommandResult) -> str:
    if type(result) is ContainedCommandOutcomeUnavailable:
        return "command_terminal"
    closed_result = cast(
        ContainedCommandCompleted
        | ContainedCommandCaptureFailed
        | ContainedCommandFinalizationFailed
        | ContainedCommandCleanupFailed,
        result,
    )
    return (
        "child_exited"
        if type(closed_result.child) is ContainedCommandExited
        else "command_terminal"
    )


def _command_result_metrics(result: ContainedCommandResult) -> str:
    if type(result) is ContainedCommandOutcomeUnavailable:
        return "lines=unknown bytes=unknown"
    closed_result = cast(
        ContainedCommandCompleted
        | ContainedCommandCaptureFailed
        | ContainedCommandFinalizationFailed
        | ContainedCommandCleanupFailed,
        result,
    )
    return (
        f"lines={closed_result.metrics.line_count} "
        f"bytes={closed_result.metrics.byte_count}"
    )


def _not_started_capture_failure(
    error: BaseException,
) -> ContainedCommandCaptureFailed:
    return ContainedCommandCaptureFailed(
        child=ContainedCommandNotStarted(),
        cleanup=ContainedCommandCleanupNotStarted(),
        failure=ContainedCommandFailure(error),
        metrics=ContainedCommandMetrics(line_count=0, byte_count=0),
    )


def _stop_validation_resource_sampler(
    *,
    sampler: ValidationResourceSampler,
    sampler_start: ValidationResourceSamplerStart,
    result: _ValidationCommandResult,
) -> _ValidationCommandResult:
    stopped_result = result
    if type(sampler_start) in (
        ValidationResourceSamplerStarted,
        ValidationResourceSamplerStartIndeterminate,
    ):
        try:
            shutdown = sampler.stop()
        except BaseException as error:
            shutdown_failure = error
        else:
            shutdown_failure = validation_resource_sampler_shutdown_failure(shutdown)
        if shutdown_failure is not None:
            stopped_result = _with_sampler_shutdown_failure(
                stopped_result,
                ContainedCommandFailure(shutdown_failure),
            )
    elif type(sampler_start) is not ValidationResourceSamplerStartRejected:
        raise AssertionError("validation resource sampler start is a closed union")
    return stopped_result


@dataclass(frozen=True, slots=True)
class _ValidationSummaryPublished:
    result: _ValidationCommandResult

    def __post_init__(self) -> None:
        if type(self.result) is not _ValidationCommandResult:
            raise ValueError("published validation summary result must be typed")


@dataclass(frozen=True, slots=True)
class _ValidationSummaryPublicationFailed:
    result: _ValidationCommandResult

    def __post_init__(self) -> None:
        if type(self.result) is not _ValidationCommandResult:
            raise ValueError("failed validation summary result must be typed")


_ValidationSummaryPublication = (
    _ValidationSummaryPublished | _ValidationSummaryPublicationFailed
)


@dataclass(frozen=True, slots=True)
class _ValidationTerminalEvidenceOwner:
    """Publish terminal/file facts before one final durable aggregate."""

    recorder: ValidateTimingRecorder
    output_file: Path
    wall_started_at: datetime
    monotonic_started_at: float

    def __post_init__(self) -> None:
        if type(self.recorder) is not ValidateTimingRecorder:
            raise ValueError("validation terminal owner recorder must be typed")
        if not isinstance(self.output_file, Path) or not self.output_file.is_absolute():
            raise ValueError("validation terminal owner output file must be absolute")
        _require_validation_wall_datetime(
            self.wall_started_at,
            "validation terminal owner wall start",
        )
        _require_validation_monotonic(
            self.monotonic_started_at,
            "validation terminal owner monotonic start",
        )

    def publish(self, result: _ValidationCommandResult) -> _ValidationCommandResult:
        published_result = result
        file_markers = _build_terminal_markers(published_result)
        for failure in _append_terminal_file_markers(
            self.output_file,
            file_markers,
        ):
            published_result = _with_post_execution_failure(
                published_result,
                failure,
                "validation execution and terminal-file reporting both failed",
            )
        console_markers = _build_terminal_markers(published_result)
        for failure in _write_terminal_console_markers(console_markers):
            published_result = _with_post_execution_failure(
                published_result,
                failure,
                "validation execution and terminal-console reporting both failed",
            )
        publication = self._publish_summary(published_result)
        if type(publication) is _ValidationSummaryPublished:
            return publication.result
        if type(publication) is not _ValidationSummaryPublicationFailed:
            raise AssertionError("validation summary publication is a closed union")
        reporting_result = publication.result
        reporting_markers = _build_terminal_markers(reporting_result)
        for failure in (
            *_append_terminal_file_markers(self.output_file, reporting_markers),
            *_write_terminal_console_markers(reporting_markers),
        ):
            reporting_result = _with_post_execution_failure(
                reporting_result,
                failure,
                "validation execution and final reporting both failed",
            )
        return reporting_result

    def _publish_summary(
        self,
        result: _ValidationCommandResult,
    ) -> _ValidationSummaryPublication:
        timing = result.timing
        if type(timing) is _ValidationTimingUnavailable:
            return _ValidationSummaryPublished(result)
        if type(timing) is not _ValidationTimingAvailable:
            raise AssertionError("validation timing evidence is a closed union")
        try:
            self.recorder.finalize(
                command_result=result.command_result,
                total_elapsed_seconds=timing.duration_seconds,
                wall_started_at=self.wall_started_at,
                monotonic_started_at=self.monotonic_started_at,
                wall_ended_at=timing.wall_ended_at,
                monotonic_ended_at=timing.monotonic_ended_at,
            )
        except BaseException as error:
            return _ValidationSummaryPublicationFailed(
                _with_post_execution_failure(
                    result,
                    ContainedCommandFailure(error),
                    "validation execution and timing-recorder finalization both failed",
                )
            )
        return _ValidationSummaryPublished(result)


def _with_sampler_shutdown_failure(
    result: _ValidationCommandResult,
    sampler_failure: ContainedCommandFailure,
) -> _ValidationCommandResult:
    return _with_post_execution_failure(
        result,
        sampler_failure,
        "validation execution and resource sampler shutdown both failed",
    )


def _with_post_execution_failure(
    result: _ValidationCommandResult,
    finalization_failure: ContainedCommandFailure,
    message: str,
) -> _ValidationCommandResult:
    command_result = result.command_result
    if type(command_result) is ContainedCommandCompleted:
        failed_result: ContainedCommandResult = ContainedCommandFinalizationFailed(
            child=command_result.child,
            capture=ContainedCommandCaptureSucceeded(),
            cleanup=ContainedCommandSupervised(),
            finalization_failure=finalization_failure,
            metrics=command_result.metrics,
        )
    elif type(command_result) is ContainedCommandCaptureFailed:
        cleanup = command_result.cleanup
        if type(command_result.child) is ContainedCommandExited and type(cleanup) in (
            ContainedCommandSupervised,
            ContainedCommandCaptureAborted,
        ):
            contained_cleanup = cast(
                ContainedCommandSupervised | ContainedCommandCaptureAborted,
                cleanup,
            )
            failed_result = ContainedCommandFinalizationFailed(
                child=command_result.child,
                capture=ContainedCommandCaptureInterrupted(command_result.failure),
                cleanup=contained_cleanup,
                finalization_failure=finalization_failure,
                metrics=command_result.metrics,
            )
        else:
            failed_result = ContainedCommandCaptureFailed(
                child=command_result.child,
                cleanup=command_result.cleanup,
                failure=_combine_command_failures(
                    command_result.failure,
                    finalization_failure,
                    message,
                ),
                metrics=command_result.metrics,
            )
    elif type(command_result) is ContainedCommandFinalizationFailed:
        failed_result = ContainedCommandFinalizationFailed(
            child=command_result.child,
            capture=command_result.capture,
            cleanup=command_result.cleanup,
            finalization_failure=_combine_command_failures(
                command_result.finalization_failure,
                finalization_failure,
                message,
            ),
            metrics=command_result.metrics,
        )
    elif type(command_result) is ContainedCommandCleanupFailed:
        failed_result = ContainedCommandCleanupFailed(
            child=command_result.child,
            capture=command_result.capture,
            cleanup_failure=_combine_command_failures(
                command_result.cleanup_failure,
                finalization_failure,
                message,
            ),
            metrics=command_result.metrics,
        )
    elif type(command_result) is ContainedCommandOutcomeUnavailable:
        failed_result = ContainedCommandOutcomeUnavailable(
            _combine_command_failures(
                command_result.failure,
                finalization_failure,
                message,
            )
        )
    else:
        raise AssertionError("contained command result is a closed union")
    return _ValidationCommandResult(
        command_result=failed_result,
        timing=result.timing,
    )


def _combine_command_failures(
    primary: ContainedCommandFailure,
    secondary: ContainedCommandFailure,
    message: str,
) -> ContainedCommandFailure:
    return ContainedCommandFailure(
        BaseExceptionGroup(message, (primary.error, secondary.error))
    )


def _observe_validation_end_timing(
    clock: ValidationRunnerClock,
    *,
    monotonic_started_at: float,
) -> _ValidationTiming:
    """Read both end clocks independently and return all-or-nothing evidence."""
    try:
        wall_read: datetime | ContainedCommandFailure = (
            _require_validation_wall_datetime(
                clock.wall_now(),
                "validation wall end",
            )
        )
    except BaseException as error:
        wall_read = ContainedCommandFailure(error)

    try:
        monotonic_read: float | ContainedCommandFailure = _require_validation_monotonic(
            clock.monotonic_now(),
            "validation monotonic end",
        )
    except BaseException as error:
        monotonic_read = ContainedCommandFailure(error)

    failures = tuple(
        read
        for read in (wall_read, monotonic_read)
        if type(read) is ContainedCommandFailure
    )
    if failures:
        return _ValidationTimingUnavailable(failures)
    if type(wall_read) is not datetime or type(monotonic_read) is not float:
        raise AssertionError("validation end clock reads are a closed union")
    try:
        return _ValidationTimingAvailable(
            duration_seconds=monotonic_read - monotonic_started_at,
            wall_ended_at=wall_read,
            monotonic_ended_at=monotonic_read,
        )
    except BaseException as error:
        return _ValidationTimingUnavailable((ContainedCommandFailure(error),))


@dataclass(frozen=True, slots=True)
class _ValidationCapturePhase:
    command_result: ContainedCommandResult
    sampler_start: ValidationResourceSamplerStart
    output_close_failure: ContainedCommandFailure | None

    def __post_init__(self) -> None:
        if type(self.command_result) not in (
            ContainedCommandCompleted,
            ContainedCommandCaptureFailed,
            ContainedCommandFinalizationFailed,
            ContainedCommandOutcomeUnavailable,
            ContainedCommandCleanupFailed,
        ):
            raise ValueError("validation capture phase command result must be closed")
        if type(self.sampler_start) not in (
            ValidationResourceSamplerStarted,
            ValidationResourceSamplerStartIndeterminate,
            ValidationResourceSamplerStartRejected,
        ):
            raise ValueError("validation capture phase sampler start must be closed")
        if (
            self.output_close_failure is not None
            and type(self.output_close_failure) is not ContainedCommandFailure
        ):
            raise ValueError("validation output close failure must be typed")


def _capture_validation_phase(
    *,
    command: str,
    worktree: Path,
    output_file: Path,
    is_orchestrated_run: bool,
    resource_sampler: ValidationResourceSampler,
    timing_recorder: ValidateTimingRecorder,
    contained_command_capture: ContainedCommandCapture,
) -> _ValidationCapturePhase:
    output_handle = open(output_file, "w", buffering=1)
    output_close_failure: ContainedCommandFailure | None = None
    try:
        try:
            sampler_start = resource_sampler.start()
        except BaseException as error:
            sampler_start = ValidationResourceSamplerStartIndeterminate(error)
        try:
            output_handle.write(
                f"[validate_runner] start pid={os.getpid()} cwd={worktree} "
                f"command={command}\n"
            )
            output_handle.flush()
            command_result = _capture_after_sampler_start(
                sampler_start,
                command,
                worktree,
                output_handle,
                is_orchestrated_run,
                timing_recorder,
                contained_command_capture,
            )
        except BaseException as error:
            command_result = ContainedCommandOutcomeUnavailable(
                ContainedCommandFailure(error)
            )
    finally:
        try:
            output_handle.close()
        except BaseException as error:
            output_close_failure = ContainedCommandFailure(error)
    return _ValidationCapturePhase(
        command_result,
        sampler_start,
        output_close_failure,
    )


def _capture_after_sampler_start(
    sampler_start: ValidationResourceSamplerStart,
    command: str,
    worktree: Path,
    output_handle: TextIO,
    is_orchestrated_run: bool,
    timing_recorder: ValidateTimingRecorder,
    contained_command_capture: ContainedCommandCapture,
) -> ContainedCommandResult:
    if type(sampler_start) is ValidationResourceSamplerStartRejected:
        return _not_started_capture_failure(sampler_start.error)
    if type(sampler_start) is ValidationResourceSamplerStartIndeterminate:
        return _not_started_capture_failure(sampler_start.error)
    if type(sampler_start) is not ValidationResourceSamplerStarted:
        raise AssertionError("validation resource sampler start is a closed union")
    return contained_command_capture.capture(
        ContainedShellCommand(command=command, working_directory=worktree),
        _ValidationCommandOutput(output_handle, is_orchestrated_run),
        _ValidationTimingLineObserver(timing_recorder),
    )


def _finalize_validation_run_evidence(
    *,
    clock: ValidationRunnerClock,
    capture_phase: _ValidationCapturePhase,
    resource_sampler: ValidationResourceSampler,
    timing_recorder: ValidateTimingRecorder,
    output_file: Path,
    wall_started_at: datetime,
    monotonic_started_at: float,
) -> _ValidationCommandResult:
    """Close each evidence seam independently around one exact command fact."""
    timing = _observe_validation_end_timing(
        clock,
        monotonic_started_at=monotonic_started_at,
    )
    result = _ValidationCommandResult(capture_phase.command_result, timing)
    if capture_phase.output_close_failure is not None:
        result = _with_post_execution_failure(
            result,
            capture_phase.output_close_failure,
            "validation execution and output-handle close both failed",
        )
    if type(timing) is _ValidationTimingUnavailable:
        for timing_failure in timing.failures:
            result = _with_post_execution_failure(
                result,
                timing_failure,
                "validation execution and end-clock observation both failed",
            )
    result = _stop_validation_resource_sampler(
        sampler=resource_sampler,
        sampler_start=capture_phase.sampler_start,
        result=result,
    )
    return _ValidationTerminalEvidenceOwner(
        recorder=timing_recorder,
        output_file=output_file.resolve(),
        wall_started_at=wall_started_at,
        monotonic_started_at=monotonic_started_at,
    ).publish(result)


def run_validation(
    command: str,
    output_dir: Path,
    worktree: Path,
    *,
    clock: ValidationRunnerClock,
    contained_command_capture: ContainedCommandCapture,
    retained_thread_factory: RetainedThreadFactory,
    host_probe: ValidationHostProbe,
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
    wall_start = _require_validation_wall_datetime(
        clock.wall_now(),
        "validation wall start",
    )
    start = _require_validation_monotonic(
        clock.monotonic_now(),
        "validation monotonic start",
    )
    timing_recorder = ValidateTimingRecorder(worktree=worktree, command=command)
    resource_probe = SystemValidationResourceProbe(
        worktree=worktree.resolve(),
        policy=_RESOURCE_SAMPLING_POLICY,
        host_probe=host_probe,
    )
    resource_sampler = ValidationResourceSampler(
        recorder=timing_recorder,
        probe=resource_probe,
        policy=_RESOURCE_SAMPLING_POLICY,
        thread_factory=retained_thread_factory,
    )

    print(f"Running: {command}")
    print(f"Output will be saved to: {output_file}")
    if is_orchestrated_run:
        print(
            "[orchestrated] full output -> file; terminal shows lifecycle markers only"
        )
    print()

    capture_phase = _capture_validation_phase(
        command=command,
        worktree=worktree,
        output_file=output_file,
        is_orchestrated_run=is_orchestrated_run,
        resource_sampler=resource_sampler,
        timing_recorder=timing_recorder,
        contained_command_capture=contained_command_capture,
    )

    result = _finalize_validation_run_evidence(
        clock=clock,
        capture_phase=capture_phase,
        resource_sampler=resource_sampler,
        timing_recorder=timing_recorder,
        output_file=output_file,
        wall_started_at=wall_start,
        monotonic_started_at=start,
    )

    finalized_command_result = result.command_result
    if type(finalized_command_result) is ContainedCommandCaptureFailed:
        raise finalized_command_result.failure.error
    if type(finalized_command_result) is ContainedCommandCleanupFailed:
        cleanup_error = ContainedCommandCleanupError(finalized_command_result)
        raise cleanup_error from finalized_command_result.cleanup_failure.error
    if type(finalized_command_result) is ContainedCommandFinalizationFailed:
        raise finalized_command_result.finalization_failure.error
    if type(finalized_command_result) is ContainedCommandOutcomeUnavailable:
        raise finalized_command_result.failure.error

    print()
    if result.passed:
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
    from ..bootstrap_executor import (
        build_retained_thread_factory,
        build_validation_host_probe,
    )

    exit_code = run_validation(
        command,
        output_dir,
        worktree,
        clock=SYSTEM_VALIDATION_RUNNER_CLOCK,
        contained_command_capture=build_contained_command_capture(),
        retained_thread_factory=build_retained_thread_factory(),
        host_probe=build_validation_host_probe(),
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
