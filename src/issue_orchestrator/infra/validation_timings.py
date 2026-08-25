"""Observe and persist strongly typed validation timing evidence."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..domain.validation_timing import (
    PrepushGateTimingSummary,
    PublishGateTimingSummary,
    ValidationConfiguration,
    ValidationConfigurationEntry,
    ValidationDiskObservation,
    ValidationHostContext,
    ValidationResourceSample,
    ValidationResourceTiming,
    ValidationRunTimingContext,
    ValidationRunTimingSummary,
    ValidationSwapUsage,
    ValidationTargetTiming,
    ValidationTimingProtocolFailure,
    ValidationTimingProtocolFailureKind,
    ValidationTimingEnvelope,
    ValidationTimingPayload,
    ValidationTimingScalar,
    merge_validation_timing_fields,
)
from ..domain.contained_command import ContainedCommandResult


_CONFIG_FIELD_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*=\S+"
_CONFIG_RE = re.compile(
    rf"^\[validate-timing\] CONFIG "
    rf"(?P<fields>{_CONFIG_FIELD_PATTERN}(?:\s+{_CONFIG_FIELD_PATTERN})*)\s*$"
)
_START_RE = re.compile(
    r"^\[validate-timing\] START target=(?P<target>\S+) at=(?P<at>\S+)$"
)
_END_RE = re.compile(
    r"^\[validate-timing\] END target=(?P<target>\S+) "
    r"status=(?P<status>-?\d+) elapsed=(?P<elapsed>\d+)s at=(?P<at>\S+)"
    r"$"
)
_TIMING_MARKER_PREFIX = "[validate-timing] "
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ValidationTimingClock:
    """Explicit clock dependency for validation timing owners."""

    wall_now: Callable[[], datetime]
    monotonic_now: Callable[[], float]

    def __post_init__(self) -> None:
        if not callable(self.wall_now):
            raise ValueError("validation timing wall clock must be callable")
        if not callable(self.monotonic_now):
            raise ValueError("validation timing monotonic clock must be callable")


SYSTEM_VALIDATION_TIMING_CLOCK = ValidationTimingClock(
    wall_now=lambda: datetime.now(timezone.utc),
    monotonic_now=time.monotonic,
)


def new_validation_run_id() -> str:
    """Create a human-traceable identity that remains unique under concurrency."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{timestamp}-pid{os.getpid()}-{uuid.uuid4().hex}"


def resolve_git_dir(worktree: Path) -> Path | None:
    """Resolve the Git directory for a worktree without shelling out."""
    dot_git = worktree / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    content = dot_git.read_text(encoding="utf-8").strip()
    prefix = "gitdir: "
    if not content.startswith(prefix):
        return None
    git_dir = Path(content[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    return git_dir


def resolve_git_common_dir(worktree: Path) -> Path | None:
    """Resolve the repository's shared Git directory across worktrees."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    commondir_file = git_dir / "commondir"
    if not commondir_file.exists():
        return git_dir
    common_dir = Path(commondir_file.read_text(encoding="utf-8").strip())
    if not common_dir.is_absolute():
        common_dir = (git_dir / common_dir).resolve()
    return common_dir


def read_head_ref_name(git_dir: Path) -> str | None:
    """Read the branch name from HEAD when it points at a ref."""
    head_file = git_dir / "HEAD"
    if not head_file.exists():
        return None
    head = head_file.read_text(encoding="utf-8").strip()
    prefix = "ref: refs/heads/"
    if not head.startswith(prefix):
        return None
    return head[len(prefix) :]


def read_branch_name(worktree: Path) -> str | None:
    """Read branch identity for a diagnostic record without a subprocess."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    branch = read_head_ref_name(git_dir)
    if branch:
        return branch
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is not None:
        return read_head_ref_name(common_dir)
    return None


def current_branch_name(worktree: Path) -> str | None:
    """Return the branch name used in timing diagnostics."""
    return read_branch_name(worktree)


def read_head_sha(worktree: Path) -> str | None:
    """Read the worktree's HEAD SHA from Git files without a subprocess."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head or None
    ref = head[len("ref:") :].strip()
    common = resolve_git_common_dir(worktree) or git_dir
    try:
        sha = (common / ref).read_text(encoding="utf-8").strip()
        if sha:
            return sha
    except OSError:
        pass
    try:
        packed_refs = (common / "packed-refs").read_text(encoding="utf-8")
        for line in packed_refs.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "^")):
                continue
            sha, _, name = stripped.partition(" ")
            if name == ref:
                return sha
    except OSError:
        pass
    return None


def get_shared_timings_file(worktree: Path) -> Path | None:
    """Return the shared JSONL timing file for this repository."""
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is None:
        return None
    return common_dir / "issue-orchestrator" / "validate-timings.jsonl"


def _append_jsonl(path: Path | None, payload: ValidationTimingPayload) -> None:
    """Append one typed JSON object atomically on local POSIX filesystems."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(payload.timing_fields(), sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = os.write(descriptor, line)
        if written != len(line):
            raise OSError(f"short JSONL write to {path}: {written} of {len(line)}")
    finally:
        os.close(descriptor)


def build_timing_envelope(
    *,
    wall_started_at: datetime,
    monotonic_started_at: float,
    wall_ended_at: datetime | None = None,
    monotonic_ended_at: float | None = None,
) -> ValidationTimingEnvelope:
    """Build elapsed evidence from monotonic and wall clocks."""
    if wall_ended_at is None:
        wall_ended_at = SYSTEM_VALIDATION_TIMING_CLOCK.wall_now()
    if monotonic_ended_at is None:
        monotonic_ended_at = SYSTEM_VALIDATION_TIMING_CLOCK.monotonic_now()
    return ValidationTimingEnvelope(
        monotonic_elapsed_seconds=round(
            monotonic_ended_at - monotonic_started_at,
            3,
        ),
        wall_started_at=wall_started_at.isoformat(),
        wall_ended_at=wall_ended_at.isoformat(),
        wall_elapsed_seconds=round(
            (wall_ended_at - wall_started_at).total_seconds(),
            3,
        ),
    )


def build_host_context() -> ValidationHostContext:
    """Identify the hardware that produced a potentially migrated timing."""
    memory_bytes: int | None = None
    try:
        memory_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return ValidationHostContext(
        name=platform.node() or None,
        system=platform.system() or None,
        release=platform.release() or None,
        machine=platform.machine() or None,
        cpu_count=os.cpu_count(),
        memory_bytes=memory_bytes,
    )


@dataclass(frozen=True, slots=True)
class _ContextualValidationTiming:
    """Serialization envelope adding worktree and hardware context once."""

    worktree: Path
    branch: str | None
    recorded_at: str
    host: ValidationHostContext
    payload: ValidationTimingPayload

    def __post_init__(self) -> None:
        if not isinstance(self.worktree, Path):
            raise ValueError("_ContextualValidationTiming.worktree must be a Path")
        if self.branch is not None and not self.branch:
            raise ValueError(
                "_ContextualValidationTiming.branch must be non-empty or None"
            )
        if not self.recorded_at:
            raise ValueError(
                "_ContextualValidationTiming.recorded_at must not be empty"
            )
        if type(self.host) is not ValidationHostContext:
            raise ValueError(
                "_ContextualValidationTiming.host must be ValidationHostContext"
            )
        if not isinstance(self.payload, ValidationTimingPayload):
            raise ValueError(
                "_ContextualValidationTiming.payload must be ValidationTimingPayload"
            )

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return merge_validation_timing_fields(
            {
                "worktree": str(self.worktree),
                "branch": self.branch,
                "recorded_at": self.recorded_at,
            },
            self.host,
            self.payload,
        )


def append_validation_timing(
    worktree: Path,
    payload: ValidationTimingPayload,
) -> None:
    """Append one timing record with shared worktree context."""
    contextual = _ContextualValidationTiming(
        worktree=worktree,
        branch=current_branch_name(worktree),
        recorded_at=datetime.now(timezone.utc).isoformat(),
        host=build_host_context(),
        payload=payload,
    )
    _append_jsonl(get_shared_timings_file(worktree), contextual)


def record_gate_timings(
    suite: str,
    worktree: Path,
    command: str,
    stdout: str,
    stderr: str,
) -> None:
    """Record target timings from captured publish-gate output."""
    if suite != "publish_gate":
        return
    recorder = ValidateTimingRecorder(worktree=worktree, command=command)
    recorder.process_output(stdout)
    recorder.process_output(stderr)


@dataclass
class ValidateTimingRecorder:
    """Own marker parsing and persistence for one validation invocation."""

    worktree: Path
    command: str
    run_id: str = field(default_factory=new_validation_run_id)
    branch: str | None = field(init=False)
    output_path: Path | None = field(init=False)
    host_context: ValidationHostContext = field(init=False)
    configuration: ValidationConfiguration = field(init=False)
    _starts: dict[str, str] = field(default_factory=dict, init=False)
    _invalid_targets: set[str] = field(default_factory=set, init=False)
    _protocol_failure_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.worktree, Path):
            raise ValueError("ValidateTimingRecorder.worktree must be a Path")
        if not self.command:
            raise ValueError("ValidateTimingRecorder.command must not be empty")
        if not self.run_id:
            raise ValueError("ValidateTimingRecorder.run_id must not be empty")
        self.branch = current_branch_name(self.worktree)
        self.output_path = get_shared_timings_file(self.worktree)
        self.host_context = build_host_context()
        self.configuration = ValidationConfiguration.empty()

    @property
    def context(self) -> ValidationRunTimingContext:
        return ValidationRunTimingContext(
            run_id=self.run_id,
            command=self.command,
            worktree=self.worktree,
            branch=self.branch,
            host=self.host_context,
        )

    def process_output(self, output: str) -> None:
        """Process captured output containing validation timing markers."""
        for line in output.splitlines():
            self.process_line(line)

    def process_line(self, line: str) -> None:
        """Apply one configuration, start, end, or unrelated output line."""
        canonical_line = line.rstrip("\r\n")
        config_match = _CONFIG_RE.fullmatch(canonical_line)
        if config_match:
            try:
                configuration = ValidationConfiguration.parse(
                    config_match.group("fields")
                )
            except ValueError:
                self._record_protocol_failure(
                    ValidationTimingProtocolFailureKind.MALFORMED_MARKER,
                    canonical_line,
                    None,
                )
                return
            self.configuration = configuration
            return

        start_match = _START_RE.fullmatch(canonical_line)
        if start_match:
            target = start_match.group("target")
            if target in self._starts:
                self._starts.pop(target)
                self._invalid_targets.add(target)
                self._record_protocol_failure(
                    ValidationTimingProtocolFailureKind.DUPLICATE_START,
                    canonical_line,
                    target,
                )
                return
            if target in self._invalid_targets:
                return
            self._starts[target] = start_match.group("at")
            return

        end_match = _END_RE.fullmatch(canonical_line)
        if not end_match:
            if canonical_line.startswith(_TIMING_MARKER_PREFIX):
                self._record_protocol_failure(
                    ValidationTimingProtocolFailureKind.MALFORMED_MARKER,
                    canonical_line,
                    None,
                )
            return
        target = end_match.group("target")
        if target in self._invalid_targets:
            return
        started_at = self._starts.pop(target, None)
        if started_at is None:
            self._record_protocol_failure(
                ValidationTimingProtocolFailureKind.END_WITHOUT_START,
                canonical_line,
                target,
            )
            return
        try:
            timing = ValidationTargetTiming(
                context=self.context,
                configuration=self.configuration,
                target=target,
                status=int(end_match.group("status")),
                elapsed_seconds=int(end_match.group("elapsed")),
                started_at=started_at,
                ended_at=end_match.group("at"),
            )
        except ValueError:
            self._invalid_targets.add(target)
            self._record_protocol_failure(
                ValidationTimingProtocolFailureKind.MALFORMED_MARKER,
                canonical_line,
                target,
            )
            return
        _append_jsonl(self.output_path, timing)

    def _record_protocol_failure(
        self,
        failure_kind: ValidationTimingProtocolFailureKind,
        line: str,
        target: str | None,
    ) -> None:
        failure = ValidationTimingProtocolFailure(
            context=self.context,
            configuration=self.configuration,
            failure_kind=failure_kind,
            line=line,
            target=target,
        )
        self._protocol_failure_count += 1
        _append_jsonl(self.output_path, failure)
        logger.warning(
            "Validation timing protocol failure: run_id=%s kind=%s target=%r line=%r",
            self.run_id,
            failure_kind.value,
            target,
            line,
        )

    def finalize(
        self,
        *,
        command_result: ContainedCommandResult,
        total_elapsed_seconds: float,
        wall_started_at: datetime,
        monotonic_started_at: float,
        wall_ended_at: datetime,
        monotonic_ended_at: float,
    ) -> None:
        """Persist the total run summary after marker collection ends."""
        summary = ValidationRunTimingSummary(
            context=self.context,
            configuration=self.configuration,
            command_result=command_result,
            total_elapsed_seconds=round(total_elapsed_seconds, 3),
            recorded_at=datetime.now(timezone.utc).isoformat(),
            envelope=build_timing_envelope(
                wall_started_at=wall_started_at,
                monotonic_started_at=monotonic_started_at,
                wall_ended_at=wall_ended_at,
                monotonic_ended_at=monotonic_ended_at,
            ),
            timing_protocol_failure_count=self._protocol_failure_count,
        )
        _append_jsonl(self.output_path, summary)

    def append_resource_sample(self, sample: ValidationResourceSample) -> None:
        """Persist one periodic host resource sample."""
        timing = ValidationResourceTiming(
            context=self.context,
            configuration=self.configuration,
            sample=sample,
        )
        _append_jsonl(self.output_path, timing)


__all__ = [
    "PrepushGateTimingSummary",
    "PublishGateTimingSummary",
    "ValidateTimingRecorder",
    "ValidationConfiguration",
    "ValidationConfigurationEntry",
    "ValidationDiskObservation",
    "ValidationHostContext",
    "ValidationResourceSample",
    "ValidationRunTimingContext",
    "ValidationSwapUsage",
    "ValidationTimingEnvelope",
    "ValidationTimingClock",
    "SYSTEM_VALIDATION_TIMING_CLOCK",
    "append_validation_timing",
    "build_host_context",
    "build_timing_envelope",
    "current_branch_name",
    "get_shared_timings_file",
    "new_validation_run_id",
    "read_branch_name",
    "read_head_ref_name",
    "read_head_sha",
    "record_gate_timings",
    "resolve_git_common_dir",
    "resolve_git_dir",
]
