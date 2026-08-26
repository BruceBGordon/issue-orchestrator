"""Observe and persist strongly typed validation timing evidence."""

from __future__ import annotations

import json
import logging
import locale
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
    ValidationDiskDeltaStatus,
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
from ..domain.validation_execution import ValidationCommandOutputCapture
from ..domain.independent_cleanup import (
    CleanupAction,
    CleanupOutcome,
    IndependentCleanupPlan,
    raise_cleanup_failures,
    raise_primary_with_cleanup,
)
from .posix_file_lock import (
    PosixFileLockAcquisition,
    PosixFileLockFilePresence,
    PosixFileLockMode,
    PosixFileLockOwner,
    PosixFileLockSpecification,
)


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
_TIMING_MARKER_READ_CHARS = 65_536
_TIMING_MARKER_MAX_LINE_CHARS = 16_384
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

    @classmethod
    def required(cls, value: ValidationTimingClock) -> ValidationTimingClock:
        """Reject untyped clock injection at the owner of the contract."""
        if type(value) is not cls:
            raise ValueError("validation timing clock must be exact")
        return value


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
    """Read the exact worktree branch, returning none for detached HEAD."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    return read_head_ref_name(git_dir)


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


class ValidationTimingJournalUnavailableError(RuntimeError):
    """The caller did not provide a worktree with a durable timing location."""


class ValidationTimingJournalCorruptionError(RuntimeError):
    """A complete timing record is corrupt and cannot be repaired safely."""

    def __init__(self, path: Path, line_number: int) -> None:
        if not isinstance(path, Path):
            raise ValueError(
                "ValidationTimingJournalCorruptionError.path must be a Path"
            )
        if type(line_number) is not int or line_number < 1:
            raise ValueError(
                "ValidationTimingJournalCorruptionError.line_number must be positive"
            )
        self.path = path
        self.line_number = line_number
        super().__init__(f"invalid validation timing record at {path}:{line_number}")


@dataclass(frozen=True, slots=True)
class ValidationTimingJournalAudit:
    """Exact count of complete records validated by one locked full scan."""

    record_count: int

    def __post_init__(self) -> None:
        if type(self.record_count) is not int or self.record_count < 0:
            raise ValueError(
                "ValidationTimingJournalAudit.record_count must be non-negative"
            )


@dataclass(frozen=True, slots=True)
class _ValidationTimingJournalFramed:
    """The journal is empty or already ends at a complete record boundary."""


@dataclass(frozen=True, slots=True)
class _ValidationTimingJournalTail:
    """One unterminated final record and its absolute byte boundary."""

    boundary: int
    payload: bytes

    def __post_init__(self) -> None:
        if type(self.boundary) is not int or self.boundary < 0:
            raise ValueError(
                "_ValidationTimingJournalTail.boundary must be non-negative"
            )
        if type(self.payload) is not bytes or not self.payload:
            raise ValueError("_ValidationTimingJournalTail.payload must not be empty")


_ValidationTimingJournalFraming = (
    _ValidationTimingJournalFramed | _ValidationTimingJournalTail
)


class _ValidationTimingRecordShapeError(ValueError):
    """Decoded journal JSON was not one exact object record."""


class ValidationTimingJournal:
    """Deep owner of synchronized, durable validation timing appends."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise ValueError("ValidationTimingJournal.path must be a Path")
        if not path.is_absolute():
            raise ValueError("ValidationTimingJournal.path must be absolute")
        if not path.name:
            raise ValueError("ValidationTimingJournal.path must name a file")
        self._path = path
        self._lock_path = path.with_suffix(".lock")
        self._file_locks = PosixFileLockOwner()
        self._shared_lock = self._lock_specification(PosixFileLockMode.SHARED)
        self._exclusive_lock = self._lock_specification(PosixFileLockMode.EXCLUSIVE)

    def append(self, payload: ValidationTimingPayload) -> None:
        """Append and durably commit one complete typed JSON line."""
        if not isinstance(payload, ValidationTimingPayload):
            raise ValueError(
                "ValidationTimingJournal.payload must be ValidationTimingPayload"
            )
        line = (json.dumps(payload.timing_fields(), sort_keys=True) + "\n").encode(
            "utf-8"
        )
        self._ensure_directory()
        with self._file_locks.hold(self._exclusive_lock):
            self._repair_torn_tail()
            self._append_line(line)

    def audit(self) -> ValidationTimingJournalAudit:
        """Validate every complete record under the journal's shared lock."""
        self._ensure_directory()
        with self._file_locks.hold(self._shared_lock):
            if not self._path.exists():
                return ValidationTimingJournalAudit(0)
            payload = self._read_all()
            lines = payload.splitlines(keepends=True)
            for line_number, line in enumerate(lines, start=1):
                if not line.endswith(b"\n"):
                    raise ValidationTimingJournalCorruptionError(
                        self._path,
                        line_number,
                    )
                self._validate_record(line, line_number)
            return ValidationTimingJournalAudit(len(lines))

    def _lock_specification(
        self,
        mode: PosixFileLockMode,
    ) -> PosixFileLockSpecification:
        return PosixFileLockSpecification(
            self._lock_path,
            mode,
            PosixFileLockAcquisition.BLOCKING,
            PosixFileLockFilePresence.CREATE_IF_MISSING,
        )

    def _append_line(self, line: bytes) -> None:
        journal_existed = self._path.exists()
        descriptor = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
            0o644,
        )
        try:
            self._write_all(descriptor, line)
            os.fsync(descriptor)
        except BaseException as append_error:
            raise_primary_with_cleanup(
                "validation timing write and finalization failures",
                append_error,
                self._finalize_journal_descriptor(
                    descriptor,
                    sync_directory=not journal_existed,
                ),
            )
        raise_cleanup_failures(
            "validation timing write finalization failures",
            self._finalize_journal_descriptor(
                descriptor,
                sync_directory=not journal_existed,
            ),
        )

    def _repair_torn_tail(self) -> None:
        if not self._path.exists():
            return
        framing = self._inspect_framing_tail()
        if type(framing) is _ValidationTimingJournalFramed:
            return
        if type(framing) is not _ValidationTimingJournalTail:
            raise AssertionError("validation timing framing is a closed union")
        try:
            self._require_record_shape(framing.payload)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            _ValidationTimingRecordShapeError,
        ):
            self._truncate_torn_tail(framing.boundary)
            logger.warning(
                "Truncated torn final validation timing record before append: "
                "path=%s removed_bytes=%d",
                self._path,
                len(framing.payload),
            )
            return
        self._complete_valid_tail()

    def _inspect_framing_tail(self) -> _ValidationTimingJournalFraming:
        descriptor = os.open(self._path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            size = os.fstat(descriptor).st_size
            if size == 0 or self._pread_exact(descriptor, 1, size - 1) == b"\n":
                result: _ValidationTimingJournalFraming = (
                    _ValidationTimingJournalFramed()
                )
            else:
                chunks: list[bytes] = []
                cursor = size
                boundary = 0
                while cursor > 0:
                    read_size = min(64 * 1024, cursor)
                    cursor -= read_size
                    chunk = self._pread_exact(descriptor, read_size, cursor)
                    separator = chunk.rfind(b"\n")
                    if separator >= 0:
                        boundary = cursor + separator + 1
                        chunks.append(chunk[separator + 1 :])
                        break
                    chunks.append(chunk)
                result = _ValidationTimingJournalTail(
                    boundary,
                    b"".join(reversed(chunks)),
                )
        except BaseException as inspection_error:
            raise_primary_with_cleanup(
                "validation timing tail inspection and cleanup failures",
                inspection_error,
                self._close_descriptor(
                    descriptor,
                    "close validation timing inspection descriptor",
                ),
            )
        raise_cleanup_failures(
            "validation timing inspection descriptor cleanup failures",
            self._close_descriptor(
                descriptor,
                "close validation timing inspection descriptor",
            ),
        )
        return result

    def _read_all(self) -> bytes:
        descriptor = os.open(self._path, os.O_RDONLY | os.O_CLOEXEC)
        chunks: list[bytes] = []
        try:
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        except BaseException as read_error:
            raise_primary_with_cleanup(
                "validation timing audit read and cleanup failures",
                read_error,
                self._close_descriptor(
                    descriptor,
                    "close validation timing audit descriptor",
                ),
            )
        raise_cleanup_failures(
            "validation timing audit descriptor cleanup failures",
            self._close_descriptor(
                descriptor,
                "close validation timing audit descriptor",
            ),
        )
        return b"".join(chunks)

    def _validate_record(self, line: bytes, line_number: int) -> None:
        try:
            self._require_record_shape(line)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            _ValidationTimingRecordShapeError,
        ) as error:
            raise ValidationTimingJournalCorruptionError(
                self._path,
                line_number,
            ) from error

    @staticmethod
    def _require_record_shape(payload: bytes) -> None:
        record: object = json.loads(payload)
        if type(record) is not dict:
            raise _ValidationTimingRecordShapeError(
                "validation timing journal record must be a JSON object"
            )

    @staticmethod
    def _pread_exact(descriptor: int, size: int, offset: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        current_offset = offset
        while remaining:
            chunk = os.pread(descriptor, remaining, current_offset)
            if not chunk:
                raise OSError("validation timing tail read made no progress")
            chunks.append(chunk)
            remaining -= len(chunk)
            current_offset += len(chunk)
        return b"".join(chunks)

    def _truncate_torn_tail(self, boundary: int) -> None:
        descriptor = os.open(
            self._path,
            os.O_RDWR | os.O_CLOEXEC,
        )
        try:
            os.ftruncate(descriptor, boundary)
            os.fsync(descriptor)
        except BaseException as repair_error:
            raise_primary_with_cleanup(
                "validation timing tail repair and finalization failures",
                repair_error,
                self._finalize_journal_descriptor(
                    descriptor,
                    sync_directory=True,
                ),
            )
        raise_cleanup_failures(
            "validation timing tail repair finalization failures",
            self._finalize_journal_descriptor(
                descriptor,
                sync_directory=True,
            ),
        )

    def _complete_valid_tail(self) -> None:
        descriptor = os.open(
            self._path,
            os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC,
        )
        try:
            self._write_all(descriptor, b"\n")
            os.fsync(descriptor)
        except BaseException as repair_error:
            raise_primary_with_cleanup(
                "validation timing tail completion and finalization failures",
                repair_error,
                self._finalize_journal_descriptor(
                    descriptor,
                    sync_directory=True,
                ),
            )
        raise_cleanup_failures(
            "validation timing tail completion finalization failures",
            self._finalize_journal_descriptor(
                descriptor,
                sync_directory=True,
            ),
        )

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("validation timing append made no progress")
            remaining = remaining[written:]

    def _finalize_journal_descriptor(
        self,
        descriptor: int,
        *,
        sync_directory: bool,
    ) -> CleanupOutcome:
        actions = [
            CleanupAction(
                "close validation timing journal descriptor",
                lambda: os.close(descriptor),
            )
        ]
        if sync_directory:
            actions.append(
                CleanupAction(
                    "sync validation timing directory after durable mutation",
                    lambda: self._sync_directory(self._path.parent),
                )
            )
        return IndependentCleanupPlan(tuple(actions)).run()

    @staticmethod
    def _close_descriptor(descriptor: int, action_name: str) -> CleanupOutcome:
        return IndependentCleanupPlan(
            (CleanupAction(action_name, lambda: os.close(descriptor)),)
        ).run()

    def _ensure_directory(self) -> None:
        missing_directories: list[Path] = []
        candidate = self._path.parent
        while not candidate.exists():
            missing_directories.append(candidate)
            parent = candidate.parent
            if parent == candidate:
                raise RuntimeError(
                    "cannot find existing ancestor for validation timing journal: "
                    f"{self._path}"
                )
            candidate = parent
        if not candidate.is_dir():
            raise NotADirectoryError(candidate)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        for created_directory in reversed(missing_directories):
            self._sync_directory(created_directory.parent)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(
            directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(descriptor)
        except BaseException as sync_error:
            raise_primary_with_cleanup(
                "validation timing directory sync and cleanup failures",
                sync_error,
                IndependentCleanupPlan(
                    (
                        CleanupAction(
                            "close validation timing directory descriptor",
                            lambda: os.close(descriptor),
                        ),
                    )
                ).run(),
            )
        raise_cleanup_failures(
            "validation timing directory descriptor cleanup failures",
            IndependentCleanupPlan(
                (
                    CleanupAction(
                        "close validation timing directory descriptor",
                        lambda: os.close(descriptor),
                    ),
                )
            ).run(),
        )


def require_shared_timings_file(worktree: Path) -> Path:
    """Return the timing path or fail before claiming evidence was recorded."""
    path = get_shared_timings_file(worktree)
    if path is None:
        raise ValidationTimingJournalUnavailableError(
            f"validation timing worktree has no Git common directory: {worktree}"
        )
    return path


def _append_jsonl(path: Path, payload: ValidationTimingPayload) -> None:
    """Append through the validation timing journal owner."""
    ValidationTimingJournal(path).append(payload)


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
    _append_jsonl(require_shared_timings_file(worktree), contextual)


def record_gate_timing_journals(
    suite: str,
    worktree: Path,
    command: str,
    output_capture: ValidationCommandOutputCapture,
) -> None:
    """Stream publish-gate markers from the complete durable output journals."""
    if suite != "publish_gate":
        return
    if type(output_capture) is not ValidationCommandOutputCapture:
        raise ValueError(
            "record_gate_timing_journals.output_capture must be "
            "ValidationCommandOutputCapture"
        )
    recorder = ValidateTimingRecorder(worktree=worktree, command=command)
    parser = _ValidationTimingMarkerStream(recorder)
    for path in (output_capture.stdout_path, output_capture.stderr_path):
        with path.open(
            "r",
            encoding=locale.getpreferredencoding(False),
            errors="replace",
        ) as journal:
            while chunk := journal.read(_TIMING_MARKER_READ_CHARS):
                parser.consume(chunk)
        parser.finish_stream()


@dataclass(slots=True)
class _ValidationTimingMarkerStream:
    """Bounded line filter feeding one marker recorder across read chunks."""

    recorder: ValidateTimingRecorder
    _candidate: str = field(default="", init=False)
    _discarding_line: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if type(self.recorder) is not ValidateTimingRecorder:
            raise ValueError(
                "_ValidationTimingMarkerStream.recorder must be ValidateTimingRecorder"
            )

    def consume(self, chunk: str) -> None:
        """Consume one non-empty journal chunk without retaining noisy lines."""
        if type(chunk) is not str or not chunk:
            raise ValueError(
                "validation timing journal chunk must be a non-empty string"
            )
        cursor = 0
        while cursor < len(chunk):
            line_end = chunk.find("\n", cursor)
            if line_end < 0:
                self._consume_line_fragment(chunk[cursor:])
                return
            self._consume_line_fragment(chunk[cursor:line_end])
            self._finish_line()
            cursor = line_end + 1

    def finish_stream(self) -> None:
        """Complete the final unterminated line and reset for another stream."""
        self._finish_line()

    def _consume_line_fragment(self, fragment: str) -> None:
        if self._discarding_line or not fragment:
            return
        remaining_capacity = _TIMING_MARKER_MAX_LINE_CHARS - len(self._candidate)
        candidate = self._candidate + fragment[:remaining_capacity]
        if len(candidate) <= len(_TIMING_MARKER_PREFIX):
            if _TIMING_MARKER_PREFIX.startswith(candidate):
                self._candidate = candidate
                return
            self._discarding_line = True
            self._candidate = ""
            return
        if not candidate.startswith(_TIMING_MARKER_PREFIX):
            self._discarding_line = True
            self._candidate = ""
            return
        if len(fragment) > remaining_capacity:
            self.recorder.record_oversized_marker(candidate)
            self._discarding_line = True
            self._candidate = ""
            return
        self._candidate = candidate

    def _finish_line(self) -> None:
        if not self._discarding_line and self._candidate:
            self.recorder.process_line(self._candidate)
        self._candidate = ""
        self._discarding_line = False


@dataclass
class ValidateTimingRecorder:
    """Own marker parsing and persistence for one validation invocation."""

    worktree: Path
    command: str
    run_id: str = field(default_factory=new_validation_run_id)
    branch: str | None = field(init=False)
    output_path: Path = field(init=False)
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
        self.output_path = require_shared_timings_file(self.worktree)
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

    def process_line(self, line: str) -> None:
        """Apply one configuration, start, end, or unrelated output line."""
        canonical_line = line.rstrip("\r\n")
        if self._record_if_oversized_marker(canonical_line):
            return
        self._process_bounded_line(canonical_line)

    def _process_bounded_line(self, canonical_line: str) -> None:
        """Interpret one admitted line whose diagnostic size is already bounded."""
        config_match = _CONFIG_RE.fullmatch(canonical_line)
        if config_match:
            self._process_configuration_fields(
                config_match.group("fields"),
                canonical_line,
            )
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
                    False,
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
                    False,
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
                False,
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
                False,
                target,
            )
            return
        _append_jsonl(self.output_path, timing)

    def _record_if_oversized_marker(self, line: str) -> bool:
        if not line.startswith(_TIMING_MARKER_PREFIX):
            return False
        if len(line) <= _TIMING_MARKER_MAX_LINE_CHARS:
            return False
        self.record_oversized_marker(line[:_TIMING_MARKER_MAX_LINE_CHARS])
        return True

    def _process_configuration_fields(self, fields: str, line: str) -> None:
        try:
            configuration = ValidationConfiguration.parse(fields)
        except ValueError:
            self._record_protocol_failure(
                ValidationTimingProtocolFailureKind.MALFORMED_MARKER,
                line,
                False,
                None,
            )
            return
        self.configuration = configuration

    def record_oversized_marker(self, line_prefix: str) -> None:
        """Record one bounded diagnostic for an oversized marker-prefixed line."""
        if (
            type(line_prefix) is not str
            or not line_prefix.startswith(_TIMING_MARKER_PREFIX)
            or len(line_prefix) != _TIMING_MARKER_MAX_LINE_CHARS
        ):
            raise ValueError(
                "oversized timing marker evidence must be one exact bounded prefix"
            )
        self._record_protocol_failure(
            ValidationTimingProtocolFailureKind.MALFORMED_MARKER,
            line_prefix,
            True,
            None,
        )

    def _record_protocol_failure(
        self,
        failure_kind: ValidationTimingProtocolFailureKind,
        line: str,
        line_truncated: bool,
        target: str | None,
    ) -> None:
        failure = ValidationTimingProtocolFailure(
            context=self.context,
            configuration=self.configuration,
            failure_kind=failure_kind,
            line=line,
            line_truncated=line_truncated,
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
    "ValidationDiskDeltaStatus",
    "ValidationDiskObservation",
    "ValidationHostContext",
    "ValidationResourceSample",
    "ValidationRunTimingContext",
    "ValidationSwapUsage",
    "ValidationTimingEnvelope",
    "ValidationTimingClock",
    "ValidationTimingJournal",
    "ValidationTimingJournalAudit",
    "ValidationTimingJournalCorruptionError",
    "ValidationTimingJournalUnavailableError",
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
    "require_shared_timings_file",
    "record_gate_timing_journals",
    "resolve_git_common_dir",
    "resolve_git_dir",
]
