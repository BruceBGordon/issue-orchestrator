#!/usr/bin/env python3
"""Profile cold and learned PR-validation on one exact committed revision.

Each measured command executes in a detached HEAD worktree. One fresh host
executor pool is shared by the whole profile: the first aggregate is genuinely
cold, per-lane runs populate learning, and the final aggregate measures the
learned state whose exact fingerprint and sample inventory are recorded.
Uncommitted changes are intentionally excluded. Package-manager, compiler, OS,
and other external caches are intentionally preserved: this measures a normal
prepared developer machine, not an artificially empty machine.

Every worktree is provisioned with ``make worktree-setup`` before measurement.
The reported job count controls both the aggregate GNU make graph and its inner
validation-lane fan-out.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable

from issue_orchestrator.domain.executor import (
    ExecutorFairnessGroup,
    ExecutorPolicySource,
)
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorAllRepositories,
    ExecutorAdmissionDeadlineExceeded,
    ExecutorCommandDeadlineExceeded,
    ExecutorCommandLifecycleFailed,
    ExecutorEvent,
    ExecutorFairnessGroupEventsQuery,
    ExecutorPolicyChanged,
    ExecutorStatus,
    ExecutorStatusQuery,
    ExecutorWorkAdmitted,
    ExecutorWorkCompleted,
    ExecutorWorkEnqueued,
    ExecutorWorkWaiting,
)
from issue_orchestrator.entrypoints.bootstrap import (
    build_executor,
    build_executor_monitor,
)
from issue_orchestrator.infra.validation_timings import build_host_context
from issue_orchestrator.ports.executor_monitor import ExecutorMonitor


EXECUTOR_POOL_DIR_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"
EXECUTOR_AGGRESSIVENESS_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_AGGRESSIVENESS_PERCENT"
EXECUTOR_GROUP_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_GROUP"
AGGREGATE_TARGET = "validate-pr-raw"
AGGREGATE_LANE_VARIABLE = "VALIDATE_PR_LANES"
PROFILE_METHOD = "cold_then_learned_pinned_commit_with_warm_external_caches"
EXECUTOR_EVENT_CAPTURE_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class CommandResult:
    """One measured command and its exact execution result."""

    name: str
    command: tuple[str, ...]
    wall_seconds: float
    exit_code: int
    worktree_path: str | None
    output_log_path: str


@dataclass(frozen=True, slots=True)
class ProfileArtifactStore:
    """Own durable profiler artifacts outside disposable worktrees."""

    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise ValueError("ProfileArtifactStore.root must be a Path")

    def initialize(self) -> None:
        """Create a new artifact directory without overwriting prior evidence."""
        self.root.mkdir(parents=True, exist_ok=False)

    def command_log_path(self, command_name: str) -> Path:
        """Return the deterministic log path for one uniquely named command."""
        if type(command_name) is not str or not command_name:
            raise ValueError("profile command name must not be empty")
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", command_name).strip("-")
        if not filename:
            raise ValueError("profile command name must contain a filename character")
        return self.root / f"{filename}.log"


@dataclass(frozen=True, slots=True)
class ProfileArguments:
    """Validated profiler invocation."""

    make_bin: str
    jobs: int
    output: Path | None
    dry_run: bool
    targets: tuple[str, ...] | None
    repo_root: Path
    aggressiveness_percent: int | None


@dataclass(frozen=True, slots=True)
class ProfileHost:
    """Non-null host identity attached to reproducible calibration evidence."""

    name: str
    system: str
    release: str
    machine: str
    cpu_count: int
    memory_bytes: int


@dataclass(frozen=True, slots=True)
class ProfileAggressiveness:
    """Selected calibration dial and the authority used to select it."""

    percent: int
    selection_source: str


@dataclass(frozen=True, slots=True)
class ProfileLearnedWork:
    """Compact public projection of one retained executor profile."""

    repository_label: str
    work_key: str
    successful_observation_count: int
    estimated_cores_per_concurrency: float


@dataclass(frozen=True, slots=True)
class ProfileExecutorStatus:
    """Effective host policy and retained learning at one profile boundary."""

    host_cpu_slots: int
    aggressiveness_percent: int
    policy_source: str
    learning_fingerprint_sha256: str
    successful_observation_count: int
    learned_work: tuple[ProfileLearnedWork, ...]


class ProfileExecutorEventType(StrEnum):
    """Stable discriminator for a retained executor-domain event."""

    ENQUEUED = "enqueued"
    WAITING = "waiting"
    ADMITTED = "admitted"
    COMMAND_LIFECYCLE_FAILED = "command-lifecycle-failed"
    ADMISSION_DEADLINE_EXCEEDED = "admission-deadline-exceeded"
    COMMAND_DEADLINE_EXCEEDED = "command-deadline-exceeded"
    COMPLETED = "completed"
    POLICY_CHANGED = "policy-changed"


@dataclass(frozen=True, slots=True)
class ProfileExecutorEventRecord:
    """Self-discriminating serialization wrapper for one domain event."""

    event: ExecutorEvent
    event_type: ProfileExecutorEventType = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", profile_event_type(self.event))


@dataclass(frozen=True, slots=True)
class ProfileExecutorEventCapture:
    """Typed executor events retained before the temporary pool is removed."""

    query_limit: int
    total_matching_event_count: int
    possibly_truncated: bool
    events: tuple[ProfileExecutorEventRecord, ...]

    def __post_init__(self) -> None:
        if type(self.query_limit) is not int or self.query_limit < 1:
            raise ValueError("ProfileExecutorEventCapture.query_limit must be positive")
        if (
            type(self.total_matching_event_count) is not int
            or self.total_matching_event_count < 0
        ):
            raise ValueError(
                "ProfileExecutorEventCapture.total_matching_event_count must be "
                "non-negative"
            )
        if type(self.possibly_truncated) is not bool:
            raise ValueError(
                "ProfileExecutorEventCapture.possibly_truncated must be a boolean"
            )
        if type(self.events) is not tuple or any(
            type(event) is not ProfileExecutorEventRecord for event in self.events
        ):
            raise ValueError(
                "ProfileExecutorEventCapture.events must contain only "
                "ProfileExecutorEventRecord values"
            )


@dataclass(frozen=True, slots=True)
class ProfileAggregateRun:
    """One aggregate result with exact learning state before and after it."""

    command_result: CommandResult
    executor_before: ProfileExecutorStatus
    executor_after: ProfileExecutorStatus
    executor_events: ProfileExecutorEventCapture
    cleanup_failures: tuple[ProfileCleanupFailure, ...]


@dataclass(frozen=True, slots=True)
class ValidateProfileConfiguration:
    """Configuration required to reproduce and interpret one profile."""

    make_bin: str
    repo_root: str
    jobs: int
    dry_run: bool
    targets: tuple[str, ...]
    aggregate_target: str
    method: str
    profiled_commit_sha: str
    source_worktree_dirty: bool
    host: ProfileHost
    aggressiveness: ProfileAggressiveness
    executor_learning: str
    external_caches: str
    artifact_directory: str


@dataclass(frozen=True, slots=True)
class ValidateProfileInitialization:
    """Typed profiler context available before target discovery completes."""

    make_bin: str
    repo_root: str
    jobs: int
    dry_run: bool
    profiled_commit_sha: str
    source_worktree_dirty: bool
    host: ProfileHost
    aggressiveness: ProfileAggressiveness
    artifact_directory: str


@dataclass(frozen=True, slots=True)
class ProfileMeasurementRequest:
    """Complete non-null input to one disposable profiling session."""

    repo_root: Path
    make_bin: str
    jobs: int
    dry_run: bool
    targets: tuple[str, ...]
    executor_pool_dir: Path
    aggressiveness: ProfileAggressiveness
    artifacts: ProfileArtifactStore
    profiled_commit_sha: str
    configuration: ValidateProfileConfiguration


@dataclass(frozen=True, slots=True)
class ValidateProfileSummary:
    """Derived bottleneck measurements for one profile."""

    timestamp_utc: str
    jobs: int
    cold_validate_pr_raw_seconds: float
    learned_validate_pr_raw_seconds: float
    learned_minus_cold_seconds: float
    fresh_worktree_target_sum_seconds: float
    fresh_worktree_slowest_target_seconds: float
    validate_pr_raw_minus_slowest_target_seconds: float
    top_targets: tuple[CommandResult, ...]


class ProfileStage(StrEnum):
    """Ordered profiler stage whose failure terminates later measurement."""

    TARGET_DISCOVERY = "target-discovery"
    COLD_AGGREGATE = "cold-aggregate"
    TARGET = "target"
    LEARNED_AGGREGATE = "learned-aggregate"
    SUMMARY = "summary"
    PROFILE_SESSION_CLEANUP = "profile-session-cleanup"


class ProfileCleanupOperation(StrEnum):
    """Cleanup boundary whose failure remains part of stage evidence."""

    WORKTREE_REMOVE = "worktree-remove"
    WORKTREE_REGISTRATION_QUERY = "worktree-registration-query"
    TEMPORARY_ROOT_REMOVE = "temporary-root-remove"
    PROFILE_SESSION_ROOT_REMOVE = "profile-session-root-remove"


@dataclass(frozen=True, slots=True)
class ProfileCleanupCommandFailure:
    """Failed cleanup command with its complete retained command evidence."""

    operation: ProfileCleanupOperation
    command_result: CommandResult

    def __post_init__(self) -> None:
        if type(self.operation) is not ProfileCleanupOperation:
            raise ValueError(
                "ProfileCleanupCommandFailure.operation must be ProfileCleanupOperation"
            )
        if type(self.command_result) is not CommandResult:
            raise ValueError(
                "ProfileCleanupCommandFailure.command_result must be CommandResult"
            )
        if self.command_result.exit_code == 0:
            raise ValueError(
                "ProfileCleanupCommandFailure.command_result must have failed"
            )


@dataclass(frozen=True, slots=True)
class ProfileCleanupFilesystemFailure:
    """Failed local cleanup operation that did not execute a command."""

    operation: ProfileCleanupOperation
    error_type: str
    error_message: str

    def __post_init__(self) -> None:
        if type(self.operation) is not ProfileCleanupOperation:
            raise ValueError(
                "ProfileCleanupFilesystemFailure.operation must be "
                "ProfileCleanupOperation"
            )
        if type(self.error_type) is not str or not self.error_type:
            raise ValueError(
                "ProfileCleanupFilesystemFailure.error_type must not be empty"
            )
        if type(self.error_message) is not str or not self.error_message:
            raise ValueError(
                "ProfileCleanupFilesystemFailure.error_message must not be empty"
            )


ProfileCleanupFailure = ProfileCleanupCommandFailure | ProfileCleanupFilesystemFailure


@dataclass(frozen=True, slots=True)
class IsolatedWorktreeRun:
    """Primary command plus every cleanup outcome for one isolated worktree."""

    command_result: CommandResult
    cleanup_failures: tuple[ProfileCleanupFailure, ...]

    def __post_init__(self) -> None:
        if type(self.command_result) is not CommandResult:
            raise ValueError("IsolatedWorktreeRun.command_result must be CommandResult")
        if type(self.cleanup_failures) is not tuple or any(
            type(failure)
            not in (ProfileCleanupCommandFailure, ProfileCleanupFilesystemFailure)
            for failure in self.cleanup_failures
        ):
            raise ValueError(
                "IsolatedWorktreeRun.cleanup_failures must contain typed failures"
            )


class ProfileAggregateProgressKind(StrEnum):
    """Last durable aggregate boundary reached before observation failed."""

    COMMAND_COMPLETED = "command-completed"
    AFTER_STATUS_CAPTURED = "after-status-captured"


@dataclass(frozen=True, slots=True)
class ProfileAggregateCommandCompleted:
    """Aggregate command and cleanup evidence retained before post-observation."""

    executor_before: ProfileExecutorStatus
    isolated_run: IsolatedWorktreeRun
    progress: ProfileAggregateProgressKind = field(
        default=ProfileAggregateProgressKind.COMMAND_COMPLETED,
        init=False,
    )

    def __post_init__(self) -> None:
        if type(self.executor_before) is not ProfileExecutorStatus:
            raise ValueError(
                "ProfileAggregateCommandCompleted.executor_before must be "
                "ProfileExecutorStatus"
            )
        if type(self.isolated_run) is not IsolatedWorktreeRun:
            raise ValueError(
                "ProfileAggregateCommandCompleted.isolated_run must be "
                "IsolatedWorktreeRun"
            )

    def with_executor_after(
        self,
        executor_after: ProfileExecutorStatus,
    ) -> ProfileAggregateAfterStatusCaptured:
        """Advance only after the post-command status is fully captured."""
        return ProfileAggregateAfterStatusCaptured(
            self.executor_before,
            self.isolated_run,
            executor_after,
        )


@dataclass(frozen=True, slots=True)
class ProfileAggregateAfterStatusCaptured:
    """Command evidence plus both executor snapshots, awaiting event capture."""

    executor_before: ProfileExecutorStatus
    isolated_run: IsolatedWorktreeRun
    executor_after: ProfileExecutorStatus
    progress: ProfileAggregateProgressKind = field(
        default=ProfileAggregateProgressKind.AFTER_STATUS_CAPTURED,
        init=False,
    )

    def __post_init__(self) -> None:
        if type(self.executor_before) is not ProfileExecutorStatus:
            raise ValueError(
                "ProfileAggregateAfterStatusCaptured.executor_before must be "
                "ProfileExecutorStatus"
            )
        if type(self.isolated_run) is not IsolatedWorktreeRun:
            raise ValueError(
                "ProfileAggregateAfterStatusCaptured.isolated_run must be "
                "IsolatedWorktreeRun"
            )
        if type(self.executor_after) is not ProfileExecutorStatus:
            raise ValueError(
                "ProfileAggregateAfterStatusCaptured.executor_after must be "
                "ProfileExecutorStatus"
            )

    def with_executor_events(
        self,
        executor_events: ProfileExecutorEventCapture,
    ) -> ProfileAggregateRun:
        """Complete the aggregate after its exact events are captured."""
        if type(executor_events) is not ProfileExecutorEventCapture:
            raise ValueError(
                "with_executor_events requires ProfileExecutorEventCapture"
            )
        return ProfileAggregateRun(
            self.isolated_run.command_result,
            self.executor_before,
            self.executor_after,
            executor_events,
            self.isolated_run.cleanup_failures,
        )


ProfileIncompleteAggregateRun = (
    ProfileAggregateCommandCompleted | ProfileAggregateAfterStatusCaptured
)


class ProfileAggregateObservationOperation(StrEnum):
    """Post-command observation that failed after work evidence existed."""

    EXECUTOR_AFTER_STATUS = "executor-after-status"
    EXECUTOR_EVENTS = "executor-events"


class ProfileAggregateObservationError(RuntimeError):
    """Typed control signal retaining post-command aggregate progress."""

    def __init__(
        self,
        operation: ProfileAggregateObservationOperation,
        progress: ProfileIncompleteAggregateRun,
        primary_error: BaseException,
    ) -> None:
        if type(operation) is not ProfileAggregateObservationOperation:
            raise ValueError(
                "ProfileAggregateObservationError.operation must be typed"
            )
        if type(progress) not in (
            ProfileAggregateCommandCompleted,
            ProfileAggregateAfterStatusCaptured,
        ):
            raise ValueError(
                "ProfileAggregateObservationError.progress must be typed"
            )
        if not isinstance(primary_error, BaseException):
            raise ValueError(
                "ProfileAggregateObservationError.primary_error is required"
            )
        self.operation = operation
        self.progress = progress
        self.primary_error = primary_error
        super().__init__(
            f"{operation.value} raised {type(primary_error).__name__}: "
            f"{_exception_message(primary_error)} after {progress.progress.value}"
        )


@dataclass(frozen=True, slots=True)
class ProfileFailure:
    """Typed failed result retained without manufacturing comparisons."""

    stage: ProfileStage
    command_result: CommandResult
    cleanup_failures: tuple[ProfileCleanupFailure, ...]

    def __post_init__(self) -> None:
        if type(self.stage) is not ProfileStage:
            raise ValueError("ProfileFailure.stage must be ProfileStage")
        if type(self.command_result) is not CommandResult:
            raise ValueError("ProfileFailure.command_result must be CommandResult")
        if type(self.cleanup_failures) is not tuple or any(
            type(failure)
            not in (ProfileCleanupCommandFailure, ProfileCleanupFilesystemFailure)
            for failure in self.cleanup_failures
        ):
            raise ValueError(
                "ProfileFailure.cleanup_failures must contain typed failures"
            )
        if self.command_result.exit_code == 0 and not self.cleanup_failures:
            raise ValueError("ProfileFailure requires a command or cleanup failure")

    @property
    def exit_code(self) -> int:
        """Return the primary failure status without hiding cleanup failures."""
        if self.command_result.exit_code != 0:
            return self.command_result.exit_code
        first_cleanup = self.cleanup_failures[0]
        if type(first_cleanup) is ProfileCleanupCommandFailure:
            return first_cleanup.command_result.exit_code
        return 1


class ProfileStageFailed(RuntimeError):
    """Fail-fast control signal carrying complete typed stage evidence."""

    def __init__(self, failure: ProfileFailure) -> None:
        if type(failure) is not ProfileFailure:
            raise ValueError("ProfileStageFailed requires ProfileFailure")
        self.failure = failure
        super().__init__(
            f"{failure.stage.value} failed: {failure.command_result.name} "
            f"exit={failure.exit_code} cleanup_failures="
            f"{len(failure.cleanup_failures)}"
        )


@dataclass(frozen=True, slots=True)
class ProfileUnexpectedFailure:
    """Unexpected stage failure retained without manufacturing command evidence."""

    stage: ProfileStage
    operation_name: str
    error_type: str
    error_message: str
    cleanup_failures: tuple[ProfileCleanupFailure, ...]

    def __post_init__(self) -> None:
        if type(self.stage) is not ProfileStage:
            raise ValueError("ProfileUnexpectedFailure.stage must be ProfileStage")
        if type(self.operation_name) is not str or not self.operation_name:
            raise ValueError(
                "ProfileUnexpectedFailure.operation_name must not be empty"
            )
        if type(self.error_type) is not str or not self.error_type:
            raise ValueError("ProfileUnexpectedFailure.error_type must not be empty")
        if type(self.error_message) is not str or not self.error_message:
            raise ValueError(
                "ProfileUnexpectedFailure.error_message must not be empty"
            )
        if type(self.cleanup_failures) is not tuple or any(
            type(failure)
            not in (ProfileCleanupCommandFailure, ProfileCleanupFilesystemFailure)
            for failure in self.cleanup_failures
        ):
            raise ValueError(
                "ProfileUnexpectedFailure.cleanup_failures must contain typed "
                "failures"
            )


class IsolatedProfileWorktreeError(RuntimeError):
    """Unexpected worktree operation failure plus unconditional cleanup evidence."""

    def __init__(
        self,
        operation_name: str,
        worktree: Path,
        primary_error: BaseException,
        cleanup_failures: tuple[ProfileCleanupFailure, ...],
    ) -> None:
        if type(operation_name) is not str or not operation_name:
            raise ValueError("IsolatedProfileWorktreeError.operation_name is required")
        if type(cleanup_failures) is not tuple:
            raise ValueError(
                "IsolatedProfileWorktreeError.cleanup_failures must be a tuple"
            )
        if not isinstance(worktree, Path) or not worktree.is_absolute():
            raise ValueError(
                "IsolatedProfileWorktreeError.worktree must be an absolute Path"
            )
        self.operation_name = operation_name
        self.worktree = worktree
        self.primary_error = primary_error
        self.cleanup_failures = cleanup_failures
        super().__init__(
            f"{operation_name} raised {type(primary_error).__name__}: "
            f"{_exception_message(primary_error)}; cleanup_failures="
            f"{len(cleanup_failures)}"
        )


@runtime_checkable
class ProfileDirectoryRemover(Protocol):
    """Remove one profiler-owned directory or raise its filesystem error."""

    def remove(self, directory: Path) -> None: ...


class ShutilProfileDirectoryRemover:
    """Production directory-removal adapter for profiler lifecycle ownership."""

    def remove(self, directory: Path) -> None:
        shutil.rmtree(directory)


def _exception_message(error: BaseException) -> str:
    message = str(error)
    return message if message else repr(error)


def _filesystem_cleanup_failure(
    operation: ProfileCleanupOperation,
    error: BaseException,
) -> ProfileCleanupFilesystemFailure:
    return ProfileCleanupFilesystemFailure(
        operation,
        type(error).__name__,
        _exception_message(error),
    )


class ProfileWorktreeRegistration(StrEnum):
    """Exact Git registration state for one isolated worktree path."""

    ABSENT = "absent"
    REGISTERED = "registered"


class _ProfileWorktreeRegistrationState(StrEnum):
    """Knowledge retained by the worktree owner across add publication."""

    NOT_ATTEMPTED = "not-attempted"
    INDETERMINATE = "indeterminate"
    CONFIRMED = "confirmed"


@runtime_checkable
class ProfileWorktreeRegistrationObserver(Protocol):
    """Observe whether Git still registers one exact canonical worktree path."""

    def observe(
        self,
        repo_root: Path,
        worktree: Path,
    ) -> ProfileWorktreeRegistration: ...


class GitProfileWorktreeRegistrationObserver:
    """Read Git's porcelain registry without inferring from filesystem state."""

    def observe(
        self,
        repo_root: Path,
        worktree: Path,
    ) -> ProfileWorktreeRegistration:
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "list",
                "--porcelain",
                "-z",
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "git returned no diagnostic"
            raise RuntimeError(f"cannot query Git worktree registration: {detail}")
        expected = worktree.resolve()
        registered_paths = tuple(
            Path(field.removeprefix("worktree ")).resolve()
            for field in completed.stdout.split("\0")
            if field.startswith("worktree ")
        )
        if expected in registered_paths:
            return ProfileWorktreeRegistration.REGISTERED
        return ProfileWorktreeRegistration.ABSENT


@runtime_checkable
class ProfileWorktreeCommandRunner(Protocol):
    """Execute logged Git registration changes for an isolated worktree."""

    def add(
        self,
        *,
        repo_root: Path,
        worktree: Path,
        profiled_commit_sha: str,
        operation_name: str,
        dry_run: bool,
        artifacts: ProfileArtifactStore,
    ) -> CommandResult: ...

    def remove(
        self,
        *,
        repo_root: Path,
        worktree: Path,
        operation_name: str,
        dry_run: bool,
        artifacts: ProfileArtifactStore,
    ) -> CommandResult: ...


class LoggedProfileWorktreeCommandRunner:
    """Production adapter retaining every Git mutation in profiler artifacts."""

    def add(
        self,
        *,
        repo_root: Path,
        worktree: Path,
        profiled_commit_sha: str,
        operation_name: str,
        dry_run: bool,
        artifacts: ProfileArtifactStore,
    ) -> CommandResult:
        return run_command(
            name=f"{operation_name}:worktree-add",
            command=(
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "add",
                "--detach",
                str(worktree),
                profiled_commit_sha,
            ),
            dry_run=dry_run,
            cwd=None,
            worktree_path=None,
            artifacts=artifacts,
        )

    def remove(
        self,
        *,
        repo_root: Path,
        worktree: Path,
        operation_name: str,
        dry_run: bool,
        artifacts: ProfileArtifactStore,
    ) -> CommandResult:
        return run_command(
            name=f"{operation_name}:worktree-remove",
            command=(
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "remove",
                "--force",
                str(worktree),
            ),
            dry_run=dry_run,
            cwd=None,
            worktree_path=None,
            artifacts=artifacts,
        )


@dataclass(slots=True)
class IsolatedProfileWorktree:
    """Deep owner for one registered Git worktree and its temporary root."""

    repo_root: Path
    operation_name: str
    profiled_commit_sha: str
    dry_run: bool
    artifacts: ProfileArtifactStore
    temporary_root: Path
    directory_remover: ProfileDirectoryRemover
    registration_observer: ProfileWorktreeRegistrationObserver
    command_runner: ProfileWorktreeCommandRunner
    _registration_state: _ProfileWorktreeRegistrationState = field(
        default=_ProfileWorktreeRegistrationState.NOT_ATTEMPTED,
        init=False,
    )
    _closed: bool = field(default=False, init=False)
    _retained_cleanup_failures: tuple[ProfileCleanupFailure, ...] = field(
        default=(),
        init=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.repo_root, Path) or not self.repo_root.is_absolute():
            raise ValueError("IsolatedProfileWorktree.repo_root must be absolute")
        if type(self.operation_name) is not str or not self.operation_name:
            raise ValueError(
                "IsolatedProfileWorktree.operation_name must not be empty"
            )
        if (
            type(self.profiled_commit_sha) is not str
            or len(self.profiled_commit_sha) != 40
        ):
            raise ValueError(
                "IsolatedProfileWorktree.profiled_commit_sha must be a full SHA"
            )
        if type(self.dry_run) is not bool:
            raise ValueError("IsolatedProfileWorktree.dry_run must be boolean")
        if type(self.artifacts) is not ProfileArtifactStore:
            raise ValueError(
                "IsolatedProfileWorktree.artifacts must be ProfileArtifactStore"
            )
        if (
            not isinstance(self.temporary_root, Path)
            or not self.temporary_root.is_absolute()
        ):
            raise ValueError(
                "IsolatedProfileWorktree.temporary_root must be absolute"
            )
        if not isinstance(self.directory_remover, ProfileDirectoryRemover):
            raise ValueError(
                "IsolatedProfileWorktree.directory_remover must implement "
                "ProfileDirectoryRemover"
            )
        if not isinstance(
            self.registration_observer,
            ProfileWorktreeRegistrationObserver,
        ):
            raise ValueError(
                "IsolatedProfileWorktree.registration_observer must implement "
                "ProfileWorktreeRegistrationObserver"
            )
        if not isinstance(self.command_runner, ProfileWorktreeCommandRunner):
            raise ValueError(
                "IsolatedProfileWorktree.command_runner must implement "
                "ProfileWorktreeCommandRunner"
            )

    @classmethod
    def create(
        cls,
        *,
        repo_root: Path,
        operation_name: str,
        profiled_commit_sha: str,
        dry_run: bool,
        artifacts: ProfileArtifactStore,
        directory_remover: ProfileDirectoryRemover,
        registration_observer: ProfileWorktreeRegistrationObserver,
        command_runner: ProfileWorktreeCommandRunner,
        temporary_prefix: str,
    ) -> IsolatedProfileWorktree:
        """Allocate the temporary root before any Git registration exists."""
        if type(temporary_prefix) is not str or not temporary_prefix:
            raise ValueError("isolated worktree temporary prefix must not be empty")
        temporary_root = Path(tempfile.mkdtemp(prefix=temporary_prefix)).resolve()
        return cls(
            repo_root.resolve(),
            operation_name,
            profiled_commit_sha,
            dry_run,
            artifacts,
            temporary_root,
            directory_remover,
            registration_observer,
            command_runner,
        )

    @property
    def worktree(self) -> Path:
        """Return the sole worktree path owned by this lifecycle."""
        return self.temporary_root / "wt"

    def add(self) -> CommandResult:
        """Attempt registration once and retain whether Git accepted it."""
        if self._registration_state is not _ProfileWorktreeRegistrationState.NOT_ATTEMPTED:
            raise RuntimeError("isolated profile worktree add was attempted twice")
        if self._closed:
            raise RuntimeError("isolated profile worktree is already closed")
        self._registration_state = _ProfileWorktreeRegistrationState.INDETERMINATE
        result = self.command_runner.add(
            repo_root=self.repo_root,
            worktree=self.worktree,
            profiled_commit_sha=self.profiled_commit_sha,
            operation_name=self.operation_name,
            dry_run=self.dry_run,
            artifacts=self.artifacts,
        )
        if result.exit_code == 0:
            self._registration_state = _ProfileWorktreeRegistrationState.CONFIRMED
        return result

    def close(self) -> tuple[ProfileCleanupFailure, ...]:
        """Attempt Git and filesystem cleanup independently and retain all failures."""
        if self._closed:
            return self._retained_cleanup_failures
        failures: list[ProfileCleanupFailure] = []
        if self._requires_git_removal(failures):
            self._remove_registered_worktree(failures)
        try:
            self.directory_remover.remove(self.temporary_root)
        except BaseException as error:
            failures.append(
                _filesystem_cleanup_failure(
                    ProfileCleanupOperation.TEMPORARY_ROOT_REMOVE,
                    error,
                )
            )
        self._retained_cleanup_failures = tuple(failures)
        self._closed = True
        return self._retained_cleanup_failures

    def _requires_git_removal(
        self,
        failures: list[ProfileCleanupFailure],
    ) -> bool:
        if self._registration_state is _ProfileWorktreeRegistrationState.NOT_ATTEMPTED:
            return False
        if self._registration_state is _ProfileWorktreeRegistrationState.CONFIRMED:
            return True
        try:
            observation = self.registration_observer.observe(
                self.repo_root,
                self.worktree,
            )
        except BaseException as error:
            failures.append(
                _filesystem_cleanup_failure(
                    ProfileCleanupOperation.WORKTREE_REGISTRATION_QUERY,
                    error,
                )
            )
            return True
        return observation is ProfileWorktreeRegistration.REGISTERED

    def _remove_registered_worktree(
        self,
        failures: list[ProfileCleanupFailure],
    ) -> None:
        try:
            removal = self.command_runner.remove(
                repo_root=self.repo_root,
                worktree=self.worktree,
                operation_name=self.operation_name,
                dry_run=self.dry_run,
                artifacts=self.artifacts,
            )
        except BaseException as error:
            failures.append(
                _filesystem_cleanup_failure(
                    ProfileCleanupOperation.WORKTREE_REMOVE,
                    error,
                )
            )
            return
        if removal.exit_code != 0:
            failures.append(
                ProfileCleanupCommandFailure(
                    ProfileCleanupOperation.WORKTREE_REMOVE,
                    removal,
                )
            )


class ProfileSessionCleanupError(RuntimeError):
    """Cleanup failure retained while propagating an unexpected profiler bug."""

    def __init__(self, failures: tuple[ProfileCleanupFailure, ...]) -> None:
        if not failures:
            raise ValueError("ProfileSessionCleanupError requires cleanup failures")
        self.failures = failures
        super().__init__(
            "profile session cleanup failed during an unexpected profiler error: "
            f"{len(failures)} failure(s)"
        )


@dataclass(frozen=True, slots=True)
class ValidateProfileReport:
    """Versioned JSON report written by the profiler."""

    schema_version: Literal[7]
    outcome: Literal["complete"]
    config: ValidateProfileConfiguration
    cold_validate_pr_raw_run: ProfileAggregateRun
    target_runs: tuple[CommandResult, ...]
    learned_validate_pr_raw_run: ProfileAggregateRun
    summary: ValidateProfileSummary


@dataclass(frozen=True, slots=True)
class ColdAggregateFailureReport:
    """Partial report when the first aggregate fails."""

    schema_version: Literal[7]
    outcome: Literal["failed"]
    config: ValidateProfileConfiguration
    failed_aggregate: ProfileAggregateRun
    failure: ProfileFailure


@dataclass(frozen=True, slots=True)
class TargetFailureReport:
    """Partial report when one isolated target fails."""

    schema_version: Literal[7]
    outcome: Literal["failed"]
    config: ValidateProfileConfiguration
    cold_validate_pr_raw_run: ProfileAggregateRun
    completed_target_runs: tuple[CommandResult, ...]
    failure: ProfileFailure


@dataclass(frozen=True, slots=True)
class LearnedAggregateFailureReport:
    """Partial report when the final aggregate fails."""

    schema_version: Literal[7]
    outcome: Literal["failed"]
    config: ValidateProfileConfiguration
    cold_validate_pr_raw_run: ProfileAggregateRun
    target_runs: tuple[CommandResult, ...]
    failed_aggregate: ProfileAggregateRun
    failure: ProfileFailure


@dataclass(frozen=True, slots=True)
class ProfileSessionCleanupFailureReport:
    """Complete measurements retained when final session cleanup fails."""

    schema_version: Literal[7]
    outcome: Literal["failed"]
    config: ValidateProfileConfiguration
    cold_validate_pr_raw_run: ProfileAggregateRun
    target_runs: tuple[CommandResult, ...]
    learned_validate_pr_raw_run: ProfileAggregateRun
    summary: ValidateProfileSummary
    failure: ProfileFailure


@dataclass(frozen=True, slots=True)
class UnexpectedProfileFailureReport:
    """Partial measurements retained when profiler code raises unexpectedly."""

    schema_version: Literal[7]
    outcome: Literal["failed"]
    config: ValidateProfileConfiguration
    completed_aggregate_runs: tuple[ProfileAggregateRun, ...]
    incomplete_aggregate_runs: tuple[ProfileIncompleteAggregateRun, ...]
    completed_target_runs: tuple[CommandResult, ...]
    failure: ProfileUnexpectedFailure

    def __post_init__(self) -> None:
        if type(self.completed_aggregate_runs) is not tuple or any(
            type(run) is not ProfileAggregateRun
            for run in self.completed_aggregate_runs
        ):
            raise ValueError(
                "UnexpectedProfileFailureReport.completed_aggregate_runs must "
                "contain ProfileAggregateRun values"
            )
        if type(self.incomplete_aggregate_runs) is not tuple or any(
            type(run)
            not in (
                ProfileAggregateCommandCompleted,
                ProfileAggregateAfterStatusCaptured,
            )
            for run in self.incomplete_aggregate_runs
        ):
            raise ValueError(
                "UnexpectedProfileFailureReport.incomplete_aggregate_runs must "
                "contain typed partial aggregate values"
            )
        if type(self.completed_target_runs) is not tuple or any(
            type(run) is not CommandResult for run in self.completed_target_runs
        ):
            raise ValueError(
                "UnexpectedProfileFailureReport.completed_target_runs must contain "
                "CommandResult values"
            )
        if type(self.failure) is not ProfileUnexpectedFailure:
            raise ValueError(
                "UnexpectedProfileFailureReport.failure must be "
                "ProfileUnexpectedFailure"
            )


@dataclass(frozen=True, slots=True)
class ProfileDiscoveryFailureReport:
    """Typed startup evidence when pinned target discovery fails."""

    schema_version: Literal[7]
    outcome: Literal["failed"]
    initialization: ValidateProfileInitialization
    failure: ProfileUnexpectedFailure

    def __post_init__(self) -> None:
        if type(self.initialization) is not ValidateProfileInitialization:
            raise ValueError(
                "ProfileDiscoveryFailureReport.initialization must be "
                "ValidateProfileInitialization"
            )
        if type(self.failure) is not ProfileUnexpectedFailure:
            raise ValueError(
                "ProfileDiscoveryFailureReport.failure must be "
                "ProfileUnexpectedFailure"
            )


ValidateProfileArtifact = (
    ValidateProfileReport
    | ColdAggregateFailureReport
    | TargetFailureReport
    | LearnedAggregateFailureReport
    | ProfileSessionCleanupFailureReport
    | UnexpectedProfileFailureReport
    | ProfileDiscoveryFailureReport
)


def detect_jobs() -> int:
    """Return the detected CPU count or fail when the OS cannot supply it."""
    cpu_count = os.cpu_count()
    if cpu_count is None or cpu_count <= 0:
        raise RuntimeError("cannot determine a positive CPU count for profiling")
    return cpu_count


def positive_integer(raw: str) -> int:
    """Parse one canonical positive base-ten integer for argparse."""
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1 or str(parsed) != raw:
        raise argparse.ArgumentTypeError(
            "must be a positive base-ten integer without padding"
        )
    return parsed


def aggressiveness_percent(raw: str) -> int:
    """Parse the executor's published aggressiveness percentage range."""
    parsed = positive_integer(raw)
    if not 25 <= parsed <= 400:
        raise argparse.ArgumentTypeError("must be between 25 and 400")
    return parsed


def configured_default_jobs() -> int:
    """Resolve the profiler default without silently replacing invalid input."""
    configured = os.environ.get("VALIDATE_JOBS")
    return detect_jobs() if configured is None else positive_integer(configured)


def run_command(
    *,
    name: str,
    command: tuple[str, ...],
    dry_run: bool,
    cwd: Path | None,
    worktree_path: str | None,
    artifacts: ProfileArtifactStore,
    environment: dict[str, str] | None = None,
) -> CommandResult:
    """Execute and measure one exact argv while retaining combined output."""
    cwd_info = f" (cwd={cwd})" if cwd is not None else ""
    print(f"[profile] {name}: {' '.join(command)}{cwd_info}")
    output_log = artifacts.command_log_path(name)
    started_at = datetime.now(tz=UTC).isoformat()
    started = time.monotonic()
    with output_log.open("x", encoding="utf-8") as log_handle:
        log_handle.write(f"[profile-command] name={name}\n")
        log_handle.write("[profile-command] argv=" + json.dumps(command) + "\n")
        log_handle.write(f"[profile-command] cwd={cwd}\n")
        log_handle.write(f"[profile-command] started_at={started_at}\n")
        if environment is not None:
            for variable in (
                EXECUTOR_POOL_DIR_ENV,
                EXECUTOR_AGGRESSIVENESS_ENV,
            ):
                if variable in environment:
                    log_handle.write(
                        f"[profile-command] env.{variable}={environment[variable]}\n"
                    )
        log_handle.flush()
        if dry_run:
            exit_code = 0
        else:
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    cwd=cwd,
                    env=environment,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
            except OSError as exc:
                exit_code = 127
                log_handle.write(
                    f"[profile-command] start_error={type(exc).__name__}: {exc}\n"
                )
            else:
                exit_code = completed.returncode
        wall_seconds = time.monotonic() - started
        log_handle.write(
            f"[profile-command] exit={exit_code} elapsed={wall_seconds:.6f}s "
            f"ended_at={datetime.now(tz=UTC).isoformat()}\n"
        )
    print(
        f"[profile] {name}: exit={exit_code} elapsed={wall_seconds:.2f}s "
        f"log={output_log}"
    )
    return CommandResult(
        name=name,
        command=command,
        wall_seconds=wall_seconds,
        exit_code=exit_code,
        worktree_path=worktree_path,
        output_log_path=str(output_log),
    )


def discover_validate_targets(repo_root: Path, make_bin: str) -> tuple[str, ...]:
    """Read the aggregate PR lanes from GNU make's database."""
    completed = subprocess.run(
        (make_bin, "-pn"),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "cannot inspect GNU make validation targets: "
            f"exit={completed.returncode} stderr={completed.stderr.strip()}"
        )

    lane_targets: tuple[str, ...] = ()
    lane_prefix = f"{AGGREGATE_LANE_VARIABLE} :="
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith(lane_prefix):
            _, _, value = line.partition(":=")
            lane_targets = tuple(value.split())

    if not lane_targets:
        raise RuntimeError(
            f"GNU make did not declare {AGGREGATE_LANE_VARIABLE} targets"
        )
    return tuple(dict.fromkeys(("_validate-static-lane", *lane_targets, "test-vscode")))


def discover_validate_targets_at_commit(
    repo_root: Path,
    make_bin: str,
    profiled_commit_sha: str,
    artifacts: ProfileArtifactStore,
) -> tuple[str, ...]:
    """Discover lanes from the immutable tree used by every measurement."""
    worktree_owner = IsolatedProfileWorktree.create(
        repo_root=repo_root,
        operation_name="target-discovery",
        profiled_commit_sha=profiled_commit_sha,
        dry_run=False,
        artifacts=artifacts,
        directory_remover=ShutilProfileDirectoryRemover(),
        registration_observer=GitProfileWorktreeRegistrationObserver(),
        command_runner=LoggedProfileWorktreeCommandRunner(),
        temporary_prefix="io-validate-profile-discovery-",
    )
    try:
        add_result = worktree_owner.add()
        if add_result.exit_code != 0:
            raise RuntimeError(
                "cannot create pinned discovery worktree: "
                f"exit={add_result.exit_code} log={add_result.output_log_path}"
            )
        targets = discover_validate_targets(worktree_owner.worktree, make_bin)
    except BaseException as error:
        cleanup_failures = worktree_owner.close()
        raise IsolatedProfileWorktreeError(
            "target-discovery",
            worktree_owner.worktree,
            error,
            cleanup_failures,
        ) from error
    cleanup_failures = worktree_owner.close()
    if cleanup_failures:
        raise IsolatedProfileWorktreeError(
            "target-discovery",
            worktree_owner.worktree,
            RuntimeError("pinned discovery worktree cleanup failed"),
            cleanup_failures,
        )
    return targets


def parse_target_override(raw: str | None) -> tuple[str, ...] | None:
    """Parse an optional explicit target list without inventing empty work."""
    if raw is None:
        return None
    targets = tuple(target.strip() for target in raw.split(",") if target.strip())
    if not targets:
        raise argparse.ArgumentTypeError("--targets must contain at least one target")
    return tuple(dict.fromkeys((*targets, "test-vscode")))


def parse_args() -> ProfileArguments:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--aggressiveness",
        type=aggressiveness_percent,
        help=(
            "Executor aggressiveness percentage. If omitted, use the current "
            "effective machine policy and record its source."
        ),
    )
    parser.add_argument(
        "--make-bin",
        default=os.environ.get("GMAKE", "make"),
        help="GNU make executable to use (default: GMAKE env or make)",
    )
    parser.add_argument(
        "--jobs",
        type=positive_integer,
        default=configured_default_jobs(),
        help="Job count for both aggregate make and inner validation lanes",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Write JSON report to this path "
            "(default: <repo-root>/.issue-orchestrator/diagnostics/"
            "validate-profile-<timestamp>.json)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands and write a zero-duration report without executing",
    )
    parser.add_argument(
        "--targets",
        help=(
            "Comma-separated target override. If omitted, targets are discovered "
            "from the aggregate PR-lane declaration."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root (default: inferred from this script location)",
    )
    namespace = parser.parse_args()
    return ProfileArguments(
        make_bin=cast(str, namespace.make_bin),
        jobs=cast(int, namespace.jobs),
        output=cast(Path | None, namespace.output),
        dry_run=cast(bool, namespace.dry_run),
        targets=parse_target_override(cast(str | None, namespace.targets)),
        repo_root=cast(Path, namespace.repo_root),
        aggressiveness_percent=cast(int | None, namespace.aggressiveness),
    )


def default_output_path(repo_root: Path) -> Path:
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        repo_root
        / ".issue-orchestrator/diagnostics"
        / f"validate-profile-{timestamp}.json"
    )


def require_stage_success(
    stage: ProfileStage,
    result: CommandResult,
    cleanup_failures: tuple[ProfileCleanupFailure, ...],
) -> None:
    """Stop the profile at its first failed measurement stage."""
    if type(stage) is not ProfileStage:
        raise ValueError("require_stage_success requires ProfileStage")
    if type(result) is not CommandResult:
        raise ValueError("require_stage_success requires CommandResult")
    if type(cleanup_failures) is not tuple:
        raise ValueError("require_stage_success requires a cleanup failure tuple")
    if result.exit_code != 0 or cleanup_failures:
        raise ProfileStageFailed(ProfileFailure(stage, result, cleanup_failures))


def write_profile_artifact(
    output_path: Path,
    artifact: ValidateProfileArtifact,
) -> None:
    """Serialize one closed report variant to the requested durable path."""
    supported = (
        ValidateProfileReport,
        ColdAggregateFailureReport,
        TargetFailureReport,
        LearnedAggregateFailureReport,
        ProfileSessionCleanupFailureReport,
        UnexpectedProfileFailureReport,
        ProfileDiscoveryFailureReport,
    )
    if type(artifact) not in supported:
        raise ValueError("write_profile_artifact requires a profile report variant")
    output_path.write_text(
        json.dumps(asdict(artifact), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"report: {output_path}")


def remove_profile_session_root(
    profile_root: Path,
    directory_remover: ProfileDirectoryRemover,
) -> tuple[ProfileCleanupFailure, ...]:
    """Return typed terminal cleanup evidence instead of raising it."""
    try:
        directory_remover.remove(profile_root)
    except BaseException as error:
        return (
            _filesystem_cleanup_failure(
                ProfileCleanupOperation.PROFILE_SESSION_ROOT_REMOVE,
                error,
            ),
        )
    return ()


def _extend_profile_failure(
    failure: ProfileFailure,
    cleanup_failures: tuple[ProfileCleanupFailure, ...],
) -> ProfileFailure:
    return ProfileFailure(
        failure.stage,
        failure.command_result,
        (*failure.cleanup_failures, *cleanup_failures),
    )


def _extend_unexpected_profile_failure(
    failure: ProfileUnexpectedFailure,
    cleanup_failures: tuple[ProfileCleanupFailure, ...],
) -> ProfileUnexpectedFailure:
    return ProfileUnexpectedFailure(
        failure.stage,
        failure.operation_name,
        failure.error_type,
        failure.error_message,
        (*failure.cleanup_failures, *cleanup_failures),
    )


def _retain_profile_session_cleanup(
    artifact: ValidateProfileArtifact,
    cleanup_failures: tuple[ProfileCleanupFailure, ...],
) -> ValidateProfileArtifact:
    if not cleanup_failures:
        return artifact
    if type(artifact) is ValidateProfileReport:
        return ProfileSessionCleanupFailureReport(
            schema_version=7,
            outcome="failed",
            config=artifact.config,
            cold_validate_pr_raw_run=artifact.cold_validate_pr_raw_run,
            target_runs=artifact.target_runs,
            learned_validate_pr_raw_run=artifact.learned_validate_pr_raw_run,
            summary=artifact.summary,
            failure=ProfileFailure(
                ProfileStage.PROFILE_SESSION_CLEANUP,
                artifact.learned_validate_pr_raw_run.command_result,
                cleanup_failures,
            ),
        )
    if type(artifact) is ColdAggregateFailureReport:
        return ColdAggregateFailureReport(
            schema_version=7,
            outcome="failed",
            config=artifact.config,
            failed_aggregate=artifact.failed_aggregate,
            failure=_extend_profile_failure(artifact.failure, cleanup_failures),
        )
    if type(artifact) is TargetFailureReport:
        return TargetFailureReport(
            schema_version=7,
            outcome="failed",
            config=artifact.config,
            cold_validate_pr_raw_run=artifact.cold_validate_pr_raw_run,
            completed_target_runs=artifact.completed_target_runs,
            failure=_extend_profile_failure(artifact.failure, cleanup_failures),
        )
    if type(artifact) is LearnedAggregateFailureReport:
        return LearnedAggregateFailureReport(
            schema_version=7,
            outcome="failed",
            config=artifact.config,
            cold_validate_pr_raw_run=artifact.cold_validate_pr_raw_run,
            target_runs=artifact.target_runs,
            failed_aggregate=artifact.failed_aggregate,
            failure=_extend_profile_failure(artifact.failure, cleanup_failures),
        )
    if type(artifact) is UnexpectedProfileFailureReport:
        return UnexpectedProfileFailureReport(
            schema_version=7,
            outcome="failed",
            config=artifact.config,
            completed_aggregate_runs=artifact.completed_aggregate_runs,
            incomplete_aggregate_runs=artifact.incomplete_aggregate_runs,
            completed_target_runs=artifact.completed_target_runs,
            failure=_extend_unexpected_profile_failure(
                artifact.failure,
                cleanup_failures,
            ),
        )
    if type(artifact) is ProfileDiscoveryFailureReport:
        raise ValueError(
            "target-discovery artifacts do not own a profile session root"
        )
    if type(artifact) is ProfileSessionCleanupFailureReport:
        raise ValueError("profile session cleanup can only be finalized once")
    raise AssertionError("ValidateProfileArtifact is a closed union")


def _profile_artifact_exit_code(artifact: ValidateProfileArtifact) -> int:
    if type(artifact) is ValidateProfileReport:
        return 0
    if type(artifact) is UnexpectedProfileFailureReport:
        return 1
    if type(artifact) is ProfileDiscoveryFailureReport:
        return 1
    if type(artifact) is ColdAggregateFailureReport:
        return artifact.failure.exit_code
    if type(artifact) is TargetFailureReport:
        return artifact.failure.exit_code
    if type(artifact) is LearnedAggregateFailureReport:
        return artifact.failure.exit_code
    if type(artifact) is ProfileSessionCleanupFailureReport:
        return artifact.failure.exit_code
    raise AssertionError("ValidateProfileArtifact is a closed union")


def finalize_profile_session(
    *,
    output_path: Path,
    profile_root: Path,
    artifact: ValidateProfileArtifact,
    directory_remover: ProfileDirectoryRemover,
) -> int:
    """Remove the transient session, retain its outcome, and write one report."""
    cleanup_failures = remove_profile_session_root(profile_root, directory_remover)
    final_artifact = _retain_profile_session_cleanup(artifact, cleanup_failures)
    write_profile_artifact(output_path, final_artifact)
    return _profile_artifact_exit_code(final_artifact)


def summarize(
    *,
    target_results: tuple[CommandResult, ...],
    cold_validate_pr_raw_result: CommandResult,
    learned_validate_pr_raw_result: CommandResult,
    jobs: int,
) -> ValidateProfileSummary:
    if not target_results:
        raise ValueError("a validate profile requires at least one target result")
    target_sum = sum(result.wall_seconds for result in target_results)
    slowest_target = max(result.wall_seconds for result in target_results)
    cold_total = cold_validate_pr_raw_result.wall_seconds
    learned_total = learned_validate_pr_raw_result.wall_seconds
    bottleneck_gap = learned_total - slowest_target
    top_targets = tuple(
        sorted(target_results, key=lambda result: result.wall_seconds, reverse=True)[:3]
    )
    summary = ValidateProfileSummary(
        timestamp_utc=datetime.now(tz=UTC).isoformat(),
        jobs=jobs,
        cold_validate_pr_raw_seconds=cold_total,
        learned_validate_pr_raw_seconds=learned_total,
        learned_minus_cold_seconds=learned_total - cold_total,
        fresh_worktree_target_sum_seconds=target_sum,
        fresh_worktree_slowest_target_seconds=slowest_target,
        validate_pr_raw_minus_slowest_target_seconds=bottleneck_gap,
        top_targets=top_targets,
    )

    print()
    print("Validate PR Profile Summary")
    print("---------------------------")
    print(f"jobs: {jobs}")
    print(f"cold validate-pr-raw: {cold_total:.2f}s")
    print(f"learned validate-pr-raw: {learned_total:.2f}s")
    print(f"learned minus cold: {learned_total - cold_total:.2f}s")
    print(f"fresh-worktree target sum: {target_sum:.2f}s")
    print(f"fresh-worktree slowest target: {slowest_target:.2f}s")
    print(f"validate-pr-raw minus slowest target: {bottleneck_gap:.2f}s")
    print("top targets:")
    for result in top_targets:
        print(f"  - {result.name}: {result.wall_seconds:.2f}s")
    return summary


def prepare_worktree(
    *,
    make_bin: str,
    name: str,
    worktree: Path,
    dry_run: bool,
    artifacts: ProfileArtifactStore,
) -> CommandResult:
    """Prepare a fresh worktree exactly like a real agent or user worktree."""
    return run_command(
        name=f"{name}:worktree-setup",
        command=(make_bin, "worktree-setup"),
        dry_run=dry_run,
        cwd=worktree,
        worktree_path=str(worktree),
        artifacts=artifacts,
    )


def run_in_isolated_worktree(
    *,
    repo_root: Path,
    make_bin: str,
    name: str,
    make_target: str,
    dry_run: bool,
    jobs: int | None,
    executor_pool_dir: Path,
    executor_aggressiveness_percent: int,
    artifacts: ProfileArtifactStore,
    profiled_commit_sha: str,
    fairness_group: ExecutorFairnessGroup,
) -> IsolatedWorktreeRun:
    """Provision, measure, and remove one isolated detached worktree."""
    worktree_owner = IsolatedProfileWorktree.create(
        repo_root=repo_root,
        operation_name=name,
        profiled_commit_sha=profiled_commit_sha,
        dry_run=dry_run,
        artifacts=artifacts,
        directory_remover=ShutilProfileDirectoryRemover(),
        registration_observer=GitProfileWorktreeRegistrationObserver(),
        command_runner=LoggedProfileWorktreeCommandRunner(),
        temporary_prefix="io-validate-profile-",
    )
    try:
        command_result = _measure_in_isolated_worktree(
            worktree_owner=worktree_owner,
            make_bin=make_bin,
            name=name,
            make_target=make_target,
            dry_run=dry_run,
            jobs=jobs,
            executor_pool_dir=executor_pool_dir,
            executor_aggressiveness_percent=executor_aggressiveness_percent,
            fairness_group=fairness_group,
        )
    except BaseException as error:
        cleanup_failures = worktree_owner.close()
        raise IsolatedProfileWorktreeError(
            name,
            worktree_owner.worktree,
            error,
            cleanup_failures,
        ) from error
    return IsolatedWorktreeRun(command_result, worktree_owner.close())


def _measure_in_isolated_worktree(
    *,
    worktree_owner: IsolatedProfileWorktree,
    make_bin: str,
    name: str,
    make_target: str,
    dry_run: bool,
    jobs: int | None,
    executor_pool_dir: Path,
    executor_aggressiveness_percent: int,
    fairness_group: ExecutorFairnessGroup,
) -> CommandResult:
    add_result = worktree_owner.add()
    if add_result.exit_code != 0:
        return CommandResult(
            name=name,
            command=add_result.command,
            wall_seconds=add_result.wall_seconds,
            exit_code=add_result.exit_code,
            worktree_path=str(worktree_owner.worktree),
            output_log_path=add_result.output_log_path,
        )
    setup_result = prepare_worktree(
        make_bin=make_bin,
        name=name,
        worktree=worktree_owner.worktree,
        dry_run=dry_run,
        artifacts=worktree_owner.artifacts,
    )
    if setup_result.exit_code != 0:
        return CommandResult(
            name=name,
            command=setup_result.command,
            wall_seconds=setup_result.wall_seconds,
            exit_code=setup_result.exit_code,
            worktree_path=str(worktree_owner.worktree),
            output_log_path=setup_result.output_log_path,
        )
    make_arguments = [make_bin]
    if jobs is not None:
        make_arguments.extend(
            (
                f"-j{jobs}",
                "--output-sync=target",
                f"VALIDATE_LANE_JOBS={jobs}",
            )
        )
    make_arguments.append(make_target)
    environment = os.environ.copy()
    environment[EXECUTOR_POOL_DIR_ENV] = str(executor_pool_dir)
    environment[EXECUTOR_AGGRESSIVENESS_ENV] = str(
        executor_aggressiveness_percent
    )
    environment[EXECUTOR_GROUP_ENV] = fairness_group.value
    return run_command(
        name=name,
        command=tuple(make_arguments),
        dry_run=dry_run,
        cwd=worktree_owner.worktree,
        worktree_path=str(worktree_owner.worktree),
        artifacts=worktree_owner.artifacts,
        environment=environment,
    )


def resolve_output_path(arguments: ProfileArguments, repo_root: Path) -> Path:
    if arguments.output is None:
        return default_output_path(repo_root)
    if arguments.output.is_absolute():
        return arguments.output
    return repo_root / arguments.output


def resolve_profiled_commit(repo_root: Path) -> str:
    """Resolve the exact committed tree profiled by detached worktrees."""
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    commit_sha = completed.stdout.strip()
    if completed.returncode != 0 or len(commit_sha) != 40:
        detail = completed.stderr.strip() or commit_sha or "git returned no SHA"
        raise RuntimeError(f"cannot resolve profiled commit: {detail}")
    return commit_sha


def source_worktree_is_dirty(repo_root: Path) -> bool:
    """Report whether HEAD intentionally excludes local source changes."""
    completed = subprocess.run(
        ("git", "status", "--porcelain"),
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cannot inspect profiler source worktree: {completed.stderr.strip()}"
        )
    return bool(completed.stdout)


def profile_host() -> ProfileHost:
    """Capture required host facts or fail instead of weakening provenance."""
    host = build_host_context()
    required = (
        host.name,
        host.system,
        host.release,
        host.machine,
        host.cpu_count,
        host.memory_bytes,
    )
    if any(value is None for value in required):
        raise RuntimeError("validation profiler requires complete host identity")
    return ProfileHost(
        name=cast(str, host.name),
        system=cast(str, host.system),
        release=cast(str, host.release),
        machine=cast(str, host.machine),
        cpu_count=cast(int, host.cpu_count),
        memory_bytes=cast(int, host.memory_bytes),
    )


def resolve_aggressiveness(arguments: ProfileArguments) -> ProfileAggressiveness:
    """Select one explicit dial while retaining its original authority."""
    if arguments.aggressiveness_percent is not None:
        return ProfileAggressiveness(
            percent=arguments.aggressiveness_percent,
            selection_source="command-line",
        )
    policy = build_executor().policy()
    return ProfileAggressiveness(
        percent=policy.aggressiveness.percent,
        selection_source=f"machine-{policy.source.value}",
    )


def capture_executor_status(
    executor_pool_dir: Path,
    selected_aggressiveness: ProfileAggressiveness,
) -> ProfileExecutorStatus:
    """Query the executor through its monitor port under the profiled policy."""
    with profiled_executor_monitor(
        executor_pool_dir,
        selected_aggressiveness,
    ) as monitor:
        status = monitor.status(ExecutorStatusQuery(ExecutorAllRepositories(), 0, 1000))
    return project_executor_status(status)


@contextmanager
def profiled_executor_monitor(
    executor_pool_dir: Path,
    selected_aggressiveness: ProfileAggressiveness,
) -> Iterator[ExecutorMonitor]:
    """Bind one monitor to the temporary pool and restore process state."""
    previous_pool = os.environ.get(EXECUTOR_POOL_DIR_ENV)
    previous_aggressiveness = os.environ.get(EXECUTOR_AGGRESSIVENESS_ENV)
    os.environ[EXECUTOR_POOL_DIR_ENV] = str(executor_pool_dir)
    os.environ[EXECUTOR_AGGRESSIVENESS_ENV] = str(selected_aggressiveness.percent)
    try:
        yield build_executor_monitor()
    finally:
        if previous_pool is None:
            del os.environ[EXECUTOR_POOL_DIR_ENV]
        else:
            os.environ[EXECUTOR_POOL_DIR_ENV] = previous_pool
        if previous_aggressiveness is None:
            del os.environ[EXECUTOR_AGGRESSIVENESS_ENV]
        else:
            os.environ[EXECUTOR_AGGRESSIVENESS_ENV] = previous_aggressiveness


def capture_executor_events(
    executor_pool_dir: Path,
    selected_aggressiveness: ProfileAggressiveness,
    *,
    fairness_group: ExecutorFairnessGroup,
) -> ProfileExecutorEventCapture:
    """Retain typed events for one exact aggregate fairness identity."""
    if type(fairness_group) is not ExecutorFairnessGroup:
        raise ValueError("capture_executor_events requires ExecutorFairnessGroup")
    with profiled_executor_monitor(
        executor_pool_dir,
        selected_aggressiveness,
    ) as monitor:
        page = monitor.events_for_group(
            ExecutorFairnessGroupEventsQuery(
                fairness_group,
                EXECUTOR_EVENT_CAPTURE_LIMIT,
            )
        )
    return ProfileExecutorEventCapture(
        query_limit=EXECUTOR_EVENT_CAPTURE_LIMIT,
        total_matching_event_count=page.total_matching_event_count,
        possibly_truncated=(
            page.total_matching_event_count > EXECUTOR_EVENT_CAPTURE_LIMIT
        ),
        events=tuple(ProfileExecutorEventRecord(event) for event in page.events),
    )


def profile_event_type(event: ExecutorEvent) -> ProfileExecutorEventType:
    """Map every closed executor-event variant to an explicit report tag."""
    event_types = (
        (ExecutorWorkEnqueued, ProfileExecutorEventType.ENQUEUED),
        (ExecutorWorkWaiting, ProfileExecutorEventType.WAITING),
        (ExecutorWorkAdmitted, ProfileExecutorEventType.ADMITTED),
        (
            ExecutorCommandLifecycleFailed,
            ProfileExecutorEventType.COMMAND_LIFECYCLE_FAILED,
        ),
        (
            ExecutorAdmissionDeadlineExceeded,
            ProfileExecutorEventType.ADMISSION_DEADLINE_EXCEEDED,
        ),
        (
            ExecutorCommandDeadlineExceeded,
            ProfileExecutorEventType.COMMAND_DEADLINE_EXCEEDED,
        ),
        (ExecutorWorkCompleted, ProfileExecutorEventType.COMPLETED),
        (ExecutorPolicyChanged, ProfileExecutorEventType.POLICY_CHANGED),
    )
    for event_class, event_type in event_types:
        if type(event) is event_class:
            return event_type
    raise ValueError(f"unsupported executor event: {type(event).__name__}")


def project_executor_status(status: ExecutorStatus) -> ProfileExecutorStatus:
    """Project the public monitor domain into the versioned report contract."""
    if status.policy.source is not ExecutorPolicySource.ENVIRONMENT:
        raise RuntimeError(
            "profiled executor policy must be the explicit environment value"
        )
    return ProfileExecutorStatus(
        host_cpu_slots=status.host_cpu_slots,
        aggressiveness_percent=status.policy.aggressiveness.percent,
        policy_source=status.policy.source.value,
        learning_fingerprint_sha256=status.learning.fingerprint_sha256,
        successful_observation_count=(status.learning.successful_observation_count),
        learned_work=tuple(
            ProfileLearnedWork(
                repository_label=item.repository.label,
                work_key=item.work_key.value,
                successful_observation_count=(item.successful_observation_count),
                estimated_cores_per_concurrency=(item.estimated_cores_per_concurrency),
            )
            for item in status.learning.learned_work
        ),
    )


def run_profile_aggregate(
    *,
    repo_root: Path,
    make_bin: str,
    name: str,
    dry_run: bool,
    jobs: int,
    executor_pool_dir: Path,
    aggressiveness: ProfileAggressiveness,
    artifacts: ProfileArtifactStore,
    profiled_commit_sha: str,
    fairness_group: ExecutorFairnessGroup,
) -> ProfileAggregateRun:
    """Measure one aggregate with learning provenance on both boundaries."""
    before = capture_executor_status(executor_pool_dir, aggressiveness)
    isolated_run = run_in_isolated_worktree(
        repo_root=repo_root,
        make_bin=make_bin,
        name=name,
        make_target=AGGREGATE_TARGET,
        dry_run=dry_run,
        jobs=jobs,
        executor_pool_dir=executor_pool_dir,
        executor_aggressiveness_percent=aggressiveness.percent,
        artifacts=artifacts,
        profiled_commit_sha=profiled_commit_sha,
        fairness_group=fairness_group,
    )
    command_completed = ProfileAggregateCommandCompleted(before, isolated_run)
    try:
        after = capture_executor_status(executor_pool_dir, aggressiveness)
    except BaseException as error:
        raise ProfileAggregateObservationError(
            ProfileAggregateObservationOperation.EXECUTOR_AFTER_STATUS,
            command_completed,
            error,
        ) from error
    after_status_captured = command_completed.with_executor_after(after)
    try:
        events = capture_executor_events(
            executor_pool_dir,
            aggressiveness,
            fairness_group=fairness_group,
        )
    except BaseException as error:
        raise ProfileAggregateObservationError(
            ProfileAggregateObservationOperation.EXECUTOR_EVENTS,
            after_status_captured,
            error,
        ) from error
    return after_status_captured.with_executor_events(events)


def profile_fairness_group(
    profiled_commit_sha: str,
    stage_label: str,
) -> ExecutorFairnessGroup:
    """Build a human-traceable identity unique to this profiler process."""
    if type(profiled_commit_sha) is not str or len(profiled_commit_sha) != 40:
        raise ValueError("profile_fairness_group requires a full commit SHA")
    if type(stage_label) is not str or not stage_label:
        raise ValueError("profile_fairness_group.stage_label must not be empty")
    return ExecutorFairnessGroup(
        f"validate-profile:{profiled_commit_sha[:12]}:pid-{os.getpid()}:{stage_label}"
    )


def profile_unexpected_failure(
    *,
    stage: ProfileStage,
    operation_name: str,
    error: BaseException,
) -> ProfileUnexpectedFailure:
    """Convert one exception and nested worktree cleanup into typed evidence."""
    if type(error) is IsolatedProfileWorktreeError:
        primary_error = error.primary_error
        cleanup_failures = error.cleanup_failures
        operation_name = error.operation_name
    elif type(error) is ProfileAggregateObservationError:
        primary_error = error.primary_error
        cleanup_failures = error.progress.isolated_run.cleanup_failures
        operation_name = f"{operation_name}:{error.operation.value}"
    else:
        primary_error = error
        cleanup_failures = ()
    return ProfileUnexpectedFailure(
        stage=stage,
        operation_name=operation_name,
        error_type=type(primary_error).__name__,
        error_message=_exception_message(primary_error),
        cleanup_failures=cleanup_failures,
    )


def unexpected_profile_failure_report(
    *,
    request: ProfileMeasurementRequest,
    stage: ProfileStage,
    operation_name: str,
    error: BaseException,
    completed_aggregate_runs: tuple[ProfileAggregateRun, ...],
    incomplete_aggregate_runs: tuple[ProfileIncompleteAggregateRun, ...],
    completed_target_runs: tuple[CommandResult, ...],
) -> UnexpectedProfileFailureReport:
    """Convert an unexpected measurement exception into durable evidence."""
    failure = profile_unexpected_failure(
        stage=stage,
        operation_name=operation_name,
        error=error,
    )
    print(
        "[profile] unexpected failure: "
        f"stage={stage.value} operation={failure.operation_name} "
        f"error={failure.error_type}: {failure.error_message} "
        f"cleanup_failures={len(failure.cleanup_failures)}",
        file=sys.stderr,
    )
    return UnexpectedProfileFailureReport(
        schema_version=7,
        outcome="failed",
        config=request.configuration,
        completed_aggregate_runs=completed_aggregate_runs,
        incomplete_aggregate_runs=incomplete_aggregate_runs,
        completed_target_runs=completed_target_runs,
        failure=failure,
    )


def incomplete_aggregate_evidence(
    error: BaseException,
) -> tuple[ProfileIncompleteAggregateRun, ...]:
    """Project only typed post-command progress from an aggregate failure."""
    if type(error) is ProfileAggregateObservationError:
        return (error.progress,)
    return ()


def measure_profile(request: ProfileMeasurementRequest) -> ValidateProfileArtifact:
    """Run every measurement and return the complete or first-failure artifact."""
    try:
        cold_aggregate = run_profile_aggregate(
            repo_root=request.repo_root,
            make_bin=request.make_bin,
            name=f"cold-aggregate:{AGGREGATE_TARGET}",
            dry_run=request.dry_run,
            jobs=request.jobs,
            executor_pool_dir=request.executor_pool_dir,
            aggressiveness=request.aggressiveness,
            artifacts=request.artifacts,
            profiled_commit_sha=request.profiled_commit_sha,
            fairness_group=profile_fairness_group(
                request.profiled_commit_sha,
                "cold",
            ),
        )
    except BaseException as error:
        return unexpected_profile_failure_report(
            request=request,
            stage=ProfileStage.COLD_AGGREGATE,
            operation_name=f"cold-aggregate:{AGGREGATE_TARGET}",
            error=error,
            completed_aggregate_runs=(),
            incomplete_aggregate_runs=incomplete_aggregate_evidence(error),
            completed_target_runs=(),
        )
    try:
        require_stage_success(
            ProfileStage.COLD_AGGREGATE,
            cold_aggregate.command_result,
            cold_aggregate.cleanup_failures,
        )
    except ProfileStageFailed as failed:
        print(f"[profile] {failed}", file=sys.stderr)
        return ColdAggregateFailureReport(
            schema_version=7,
            outcome="failed",
            config=request.configuration,
            failed_aggregate=cold_aggregate,
            failure=failed.failure,
        )

    completed_targets: list[CommandResult] = []
    for target in request.targets:
        try:
            target_run = run_in_isolated_worktree(
                repo_root=request.repo_root,
                make_bin=request.make_bin,
                name=f"target:{target}",
                make_target=target,
                dry_run=request.dry_run,
                jobs=None,
                executor_pool_dir=request.executor_pool_dir,
                executor_aggressiveness_percent=request.aggressiveness.percent,
                artifacts=request.artifacts,
                profiled_commit_sha=request.profiled_commit_sha,
                fairness_group=profile_fairness_group(
                    request.profiled_commit_sha,
                    f"target:{target}",
                ),
            )
        except BaseException as error:
            return unexpected_profile_failure_report(
                request=request,
                stage=ProfileStage.TARGET,
                operation_name=f"target:{target}",
                error=error,
                completed_aggregate_runs=(cold_aggregate,),
                incomplete_aggregate_runs=(),
                completed_target_runs=tuple(completed_targets),
            )
        try:
            require_stage_success(
                ProfileStage.TARGET,
                target_run.command_result,
                target_run.cleanup_failures,
            )
        except ProfileStageFailed as failed:
            print(f"[profile] {failed}", file=sys.stderr)
            return TargetFailureReport(
                schema_version=7,
                outcome="failed",
                config=request.configuration,
                cold_validate_pr_raw_run=cold_aggregate,
                completed_target_runs=tuple(completed_targets),
                failure=failed.failure,
            )
        completed_targets.append(target_run.command_result)
    target_results = tuple(completed_targets)

    try:
        learned_aggregate = run_profile_aggregate(
            repo_root=request.repo_root,
            make_bin=request.make_bin,
            name=f"learned-aggregate:{AGGREGATE_TARGET}",
            dry_run=request.dry_run,
            jobs=request.jobs,
            executor_pool_dir=request.executor_pool_dir,
            aggressiveness=request.aggressiveness,
            artifacts=request.artifacts,
            profiled_commit_sha=request.profiled_commit_sha,
            fairness_group=profile_fairness_group(
                request.profiled_commit_sha,
                "learned",
            ),
        )
    except BaseException as error:
        return unexpected_profile_failure_report(
            request=request,
            stage=ProfileStage.LEARNED_AGGREGATE,
            operation_name=f"learned-aggregate:{AGGREGATE_TARGET}",
            error=error,
            completed_aggregate_runs=(cold_aggregate,),
            incomplete_aggregate_runs=incomplete_aggregate_evidence(error),
            completed_target_runs=target_results,
        )
    try:
        require_stage_success(
            ProfileStage.LEARNED_AGGREGATE,
            learned_aggregate.command_result,
            learned_aggregate.cleanup_failures,
        )
    except ProfileStageFailed as failed:
        print(f"[profile] {failed}", file=sys.stderr)
        return LearnedAggregateFailureReport(
            schema_version=7,
            outcome="failed",
            config=request.configuration,
            cold_validate_pr_raw_run=cold_aggregate,
            target_runs=target_results,
            failed_aggregate=learned_aggregate,
            failure=failed.failure,
        )

    try:
        summary = summarize(
            target_results=target_results,
            cold_validate_pr_raw_result=cold_aggregate.command_result,
            learned_validate_pr_raw_result=learned_aggregate.command_result,
            jobs=request.jobs,
        )
    except BaseException as error:
        return unexpected_profile_failure_report(
            request=request,
            stage=ProfileStage.SUMMARY,
            operation_name="profile-summary",
            error=error,
            completed_aggregate_runs=(cold_aggregate, learned_aggregate),
            incomplete_aggregate_runs=(),
            completed_target_runs=target_results,
        )
    return ValidateProfileReport(
        schema_version=7,
        outcome="complete",
        config=request.configuration,
        cold_validate_pr_raw_run=cold_aggregate,
        target_runs=target_results,
        learned_validate_pr_raw_run=learned_aggregate,
        summary=summary,
    )


def main() -> int:
    arguments = parse_args()
    repo_root = arguments.repo_root.resolve()
    output_path = resolve_output_path(arguments, repo_root)
    profiled_commit_sha = resolve_profiled_commit(repo_root)
    dirty = source_worktree_is_dirty(repo_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    artifacts = ProfileArtifactStore(
        output_path.parent / f"{output_path.stem}-artifacts"
    )
    artifacts.initialize()
    host = profile_host()
    aggressiveness = resolve_aggressiveness(arguments)
    initialization = ValidateProfileInitialization(
        make_bin=arguments.make_bin,
        repo_root=str(repo_root),
        jobs=arguments.jobs,
        dry_run=arguments.dry_run,
        profiled_commit_sha=profiled_commit_sha,
        source_worktree_dirty=dirty,
        host=host,
        aggressiveness=aggressiveness,
        artifact_directory=str(artifacts.root),
    )
    if arguments.targets is not None:
        targets = arguments.targets
    else:
        try:
            targets = discover_validate_targets_at_commit(
                repo_root,
                arguments.make_bin,
                profiled_commit_sha,
                artifacts,
            )
        except BaseException as error:
            failure = profile_unexpected_failure(
                stage=ProfileStage.TARGET_DISCOVERY,
                operation_name="target-discovery",
                error=error,
            )
            print(
                "[profile] target discovery failed: "
                f"error={failure.error_type}: {failure.error_message} "
                f"cleanup_failures={len(failure.cleanup_failures)}",
                file=sys.stderr,
            )
            artifact = ProfileDiscoveryFailureReport(
                schema_version=7,
                outcome="failed",
                initialization=initialization,
                failure=failure,
            )
            write_profile_artifact(output_path, artifact)
            return _profile_artifact_exit_code(artifact)
    configuration = ValidateProfileConfiguration(
        make_bin=arguments.make_bin,
        repo_root=str(repo_root),
        jobs=arguments.jobs,
        dry_run=arguments.dry_run,
        targets=targets,
        aggregate_target=AGGREGATE_TARGET,
        method=PROFILE_METHOD,
        profiled_commit_sha=profiled_commit_sha,
        source_worktree_dirty=dirty,
        host=host,
        aggressiveness=aggressiveness,
        executor_learning=(
            "one fresh pool: cold aggregate, lane training, learned aggregate"
        ),
        external_caches="preserved",
        artifact_directory=str(artifacts.root),
    )
    profile_root = Path(tempfile.mkdtemp(prefix="io-validate-profile-session-"))
    executor_pool_dir = profile_root / "executor-pool"
    directory_remover = ShutilProfileDirectoryRemover()
    try:
        artifact = measure_profile(
            ProfileMeasurementRequest(
                repo_root=repo_root,
                make_bin=arguments.make_bin,
                jobs=arguments.jobs,
                dry_run=arguments.dry_run,
                targets=targets,
                executor_pool_dir=executor_pool_dir,
                aggressiveness=aggressiveness,
                artifacts=artifacts,
                profiled_commit_sha=profiled_commit_sha,
                configuration=configuration,
            )
        )
    except BaseException as exc:
        cleanup_failures = remove_profile_session_root(
            profile_root,
            directory_remover,
        )
        if cleanup_failures:
            raise ProfileSessionCleanupError(cleanup_failures) from exc
        raise
    return finalize_profile_session(
        output_path=output_path,
        profile_root=profile_root,
        artifact=artifact,
        directory_remover=directory_remover,
    )


if __name__ == "__main__":
    raise SystemExit(main())
