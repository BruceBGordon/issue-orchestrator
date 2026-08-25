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
from typing import Literal, cast

from issue_orchestrator.domain.executor import ExecutorPolicySource
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorAdmissionDeadlineExceeded,
    ExecutorCommandDeadlineExceeded,
    ExecutorCommandStartFailed,
    ExecutorEvent,
    ExecutorPolicyChanged,
    ExecutorRecentEventsQuery,
    ExecutorStatus,
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
EXECUTOR_AGGRESSIVENESS_ENV = (
    "ISSUE_ORCHESTRATOR_EXECUTOR_AGGRESSIVENESS_PERCENT"
)
AGGREGATE_TARGET = "validate-pr-raw"
AGGREGATE_LANE_VARIABLE = "VALIDATE_PR_LANES"
PROFILE_METHOD = "cold_then_learned_detached_HEAD_with_warm_external_caches"
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
    COMMAND_START_FAILED = "command-start-failed"
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
    possibly_truncated: bool
    events: tuple[ProfileExecutorEventRecord, ...]

    def __post_init__(self) -> None:
        if type(self.query_limit) is not int or self.query_limit < 1:
            raise ValueError(
                "ProfileExecutorEventCapture.query_limit must be positive"
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


@dataclass(frozen=True, slots=True)
class ValidateProfileReport:
    """Versioned JSON report written by the profiler."""

    schema_version: Literal[4]
    config: ValidateProfileConfiguration
    cold_validate_pr_raw_run: ProfileAggregateRun
    target_runs: tuple[CommandResult, ...]
    learned_validate_pr_raw_run: ProfileAggregateRun
    summary: ValidateProfileSummary


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
        log_handle.write(
            "[profile-command] argv=" + json.dumps(command) + "\n"
        )
        log_handle.write(f"[profile-command] cwd={cwd}\n")
        log_handle.write(f"[profile-command] started_at={started_at}\n")
        if environment is not None:
            for variable in (
                EXECUTOR_POOL_DIR_ENV,
                EXECUTOR_AGGRESSIVENESS_ENV,
            ):
                if variable in environment:
                    log_handle.write(
                        f"[profile-command] env.{variable}="
                        f"{environment[variable]}\n"
                    )
        log_handle.flush()
        if dry_run:
            exit_code = 0
        else:
            completed = subprocess.run(
                command,
                check=False,
                cwd=cwd,
                env=environment,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
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
    return tuple(
        dict.fromkeys(("_validate-static-lane", *lane_targets, "test-vscode"))
    )


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


def collect_failures(
    results: tuple[CommandResult, ...],
) -> tuple[CommandResult, ...]:
    failed = tuple(result for result in results if result.exit_code != 0)
    if failed:
        print("[profile] failed command(s):", file=sys.stderr)
        for result in failed:
            print(f"  - {result.name} (exit={result.exit_code})", file=sys.stderr)
    return failed


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
) -> CommandResult:
    """Provision, measure, and remove one isolated detached worktree."""
    temporary_root = Path(tempfile.mkdtemp(prefix="io-validate-profile-"))
    worktree = temporary_root / "wt"
    add_succeeded = False
    try:
        add_command = (
            "git",
            "-C",
            str(repo_root),
            "worktree",
            "add",
            "--detach",
            str(worktree),
            "HEAD",
        )
        add_result = run_command(
            name=f"{name}:worktree-add",
            command=add_command,
            dry_run=dry_run,
            cwd=None,
            worktree_path=None,
            artifacts=artifacts,
        )
        if add_result.exit_code != 0:
            return CommandResult(
                name=name,
                command=add_command,
                wall_seconds=add_result.wall_seconds,
                exit_code=add_result.exit_code,
                worktree_path=str(worktree),
                output_log_path=add_result.output_log_path,
            )
        add_succeeded = True

        setup_result = prepare_worktree(
            make_bin=make_bin,
            name=name,
            worktree=worktree,
            dry_run=dry_run,
            artifacts=artifacts,
        )
        if setup_result.exit_code != 0:
            return CommandResult(
                name=name,
                command=setup_result.command,
                wall_seconds=setup_result.wall_seconds,
                exit_code=setup_result.exit_code,
                worktree_path=str(worktree),
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
        return run_command(
            name=name,
            command=tuple(make_arguments),
            dry_run=dry_run,
            cwd=worktree,
            worktree_path=str(worktree),
            artifacts=artifacts,
            environment=environment,
        )
    finally:
        if add_succeeded:
            removal = run_command(
                name=f"{name}:worktree-remove",
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
            if removal.exit_code != 0:
                raise RuntimeError(
                    f"failed to remove profiler worktree {worktree}: "
                    f"exit={removal.exit_code}"
                )
        shutil.rmtree(temporary_root)


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
            "cannot inspect profiler source worktree: "
            f"{completed.stderr.strip()}"
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
        status = monitor.status()
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
    os.environ[EXECUTOR_AGGRESSIVENESS_ENV] = str(
        selected_aggressiveness.percent
    )
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
    recorded_since_unix: float,
) -> ProfileExecutorEventCapture:
    """Retain typed events produced during one aggregate measurement."""
    if type(recorded_since_unix) is not float or recorded_since_unix <= 0:
        raise ValueError("recorded_since_unix must be a positive float")
    with profiled_executor_monitor(
        executor_pool_dir,
        selected_aggressiveness,
    ) as monitor:
        unfiltered = monitor.recent_events(
            ExecutorRecentEventsQuery(EXECUTOR_EVENT_CAPTURE_LIMIT)
        )
    return ProfileExecutorEventCapture(
        query_limit=EXECUTOR_EVENT_CAPTURE_LIMIT,
        possibly_truncated=len(unfiltered.events) == EXECUTOR_EVENT_CAPTURE_LIMIT,
        events=tuple(
            ProfileExecutorEventRecord(event)
            for event in unfiltered.events
            if event.metadata.recorded_at_unix >= recorded_since_unix
        ),
    )


def profile_event_type(event: ExecutorEvent) -> ProfileExecutorEventType:
    """Map every closed executor-event variant to an explicit report tag."""
    event_types = (
        (ExecutorWorkEnqueued, ProfileExecutorEventType.ENQUEUED),
        (ExecutorWorkWaiting, ProfileExecutorEventType.WAITING),
        (ExecutorWorkAdmitted, ProfileExecutorEventType.ADMITTED),
        (
            ExecutorCommandStartFailed,
            ProfileExecutorEventType.COMMAND_START_FAILED,
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
        successful_observation_count=(
            status.learning.successful_observation_count
        ),
        learned_work=tuple(
            ProfileLearnedWork(
                repository_label=item.repository.label,
                work_key=item.work_key.value,
                successful_observation_count=(
                    item.successful_observation_count
                ),
                estimated_cores_per_concurrency=(
                    item.estimated_cores_per_concurrency
                ),
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
) -> ProfileAggregateRun:
    """Measure one aggregate with learning provenance on both boundaries."""
    recorded_since_unix = time.time()
    before = capture_executor_status(executor_pool_dir, aggressiveness)
    command_result = run_in_isolated_worktree(
        repo_root=repo_root,
        make_bin=make_bin,
        name=name,
        make_target=AGGREGATE_TARGET,
        dry_run=dry_run,
        jobs=jobs,
        executor_pool_dir=executor_pool_dir,
        executor_aggressiveness_percent=aggressiveness.percent,
        artifacts=artifacts,
    )
    after = capture_executor_status(executor_pool_dir, aggressiveness)
    events = capture_executor_events(
        executor_pool_dir,
        aggressiveness,
        recorded_since_unix=recorded_since_unix,
    )
    return ProfileAggregateRun(command_result, before, after, events)


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

    targets = arguments.targets or discover_validate_targets(
        repo_root,
        arguments.make_bin,
    )
    profile_root = Path(tempfile.mkdtemp(prefix="io-validate-profile-session-"))
    executor_pool_dir = profile_root / "executor-pool"
    try:
        cold_aggregate = run_profile_aggregate(
            repo_root=repo_root,
            make_bin=arguments.make_bin,
            name=f"cold-aggregate:{AGGREGATE_TARGET}",
            dry_run=arguments.dry_run,
            jobs=arguments.jobs,
            executor_pool_dir=executor_pool_dir,
            aggressiveness=aggressiveness,
            artifacts=artifacts,
        )
        target_results = tuple(
            run_in_isolated_worktree(
                repo_root=repo_root,
                make_bin=arguments.make_bin,
                name=f"target:{target}",
                make_target=target,
                dry_run=arguments.dry_run,
                jobs=None,
                executor_pool_dir=executor_pool_dir,
                executor_aggressiveness_percent=aggressiveness.percent,
                artifacts=artifacts,
            )
            for target in targets
        )
        learned_aggregate = run_profile_aggregate(
            repo_root=repo_root,
            make_bin=arguments.make_bin,
            name=f"learned-aggregate:{AGGREGATE_TARGET}",
            dry_run=arguments.dry_run,
            jobs=arguments.jobs,
            executor_pool_dir=executor_pool_dir,
            aggressiveness=aggressiveness,
            artifacts=artifacts,
        )
    finally:
        shutil.rmtree(profile_root)
    summary = summarize(
        target_results=target_results,
        cold_validate_pr_raw_result=cold_aggregate.command_result,
        learned_validate_pr_raw_result=learned_aggregate.command_result,
        jobs=arguments.jobs,
    )
    report = ValidateProfileReport(
        schema_version=4,
        config=ValidateProfileConfiguration(
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
        ),
        cold_validate_pr_raw_run=cold_aggregate,
        target_runs=target_results,
        learned_validate_pr_raw_run=learned_aggregate,
        summary=summary,
    )
    output_path.write_text(
        json.dumps(asdict(report), indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"report: {output_path}")

    failures = collect_failures(
        (
            cold_aggregate.command_result,
            *target_results,
            learned_aggregate.command_result,
        )
    )
    return failures[0].exit_code if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
