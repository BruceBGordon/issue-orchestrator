"""Session output port for session artifact storage.

This module defines the protocol for storing and retrieving session artifacts.
All artifacts for a session live in a single run directory:

    .issue-orchestrator/sessions/<run_id>__<session_name>/
        manifest.json           # Session metadata
        terminal-recording.jsonl # Canonical raw terminal recording
        validation-record.json  # Validation result
        validation-stdout.log   # Validation stdout
        validation-stderr.log   # Validation stderr
        validation-state.json   # Retry flow state
        validation-errors.txt   # Human-readable errors
        retry-prompt.md         # Retry prompt for agent
        failure-diagnostic-*.json  # Failure diagnostics
        worktree.json           # Worktree metadata
        ...

The principle is simple: all artifacts for a session go in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Protocol, TypeVar, cast


if TYPE_CHECKING:
    from ..domain.artifact_contracts import ValidationOutcome
    from ..domain.exchange_chapter import ExchangeChapterSidecar
    from ..domain.review_exchange_run import ReviewExchangeRun
    from ..domain.session_watchdog import ScheduledSessionWatchdog

from ..domain.review_exchange_run import ReviewExchangeRunAssets
from ..domain.review_exchange_summary import ReviewExchangeSummaryV1
from ..domain.session_run import SessionRunAssets


VALIDATION_RECORD_SCHEMA_VERSION = 1
_VALIDATION_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "suite",
        "head_sha",
        "passed",
        "exit_code",
        "command",
        "started_at",
        "ended_at",
        "timed_out",
        "stdout_path",
        "stderr_path",
    }
)
_ValidationField = TypeVar("_ValidationField")


def _required_validation_field(
    data: Mapping[str, object],
    name: str,
    expected_type: type[_ValidationField],
) -> _ValidationField:
    value = data[name]
    if type(value) is not expected_type:
        raise TypeError(
            f"ValidationRecord.{name} must be {expected_type.__name__}, "
            f"got {type(value).__name__}"
        )
    return cast(_ValidationField, value)


def _optional_validation_path(data: Mapping[str, object], name: str) -> str | None:
    value = data[name]
    if value is not None and type(value) is not str:
        raise TypeError(f"ValidationRecord.{name} must be str or None")
    return value


@dataclass(frozen=True)
class ValidationRecord:
    """Validation run result.

    Stored at: <run_dir>/validation-record.json
    """

    schema_version: int
    suite: str  # "publish_gate" or "agent_gate"
    head_sha: str
    passed: bool
    exit_code: int
    command: str
    started_at: str
    ended_at: str
    timed_out: bool = False
    stdout_path: str | None = None
    stderr_path: str | None = None

    def __post_init__(self) -> None:
        self._validate_exact_types()
        self._validate_identity_fields()
        self._validate_optional_paths()
        self._validate_timestamps()
        self._validate_result_consistency()

    def _validate_exact_types(self) -> None:
        exact_fields: tuple[tuple[str, object, type[object]], ...] = (
            ("schema_version", self.schema_version, cast(type[object], int)),
            ("suite", self.suite, cast(type[object], str)),
            ("head_sha", self.head_sha, cast(type[object], str)),
            ("passed", self.passed, cast(type[object], bool)),
            ("exit_code", self.exit_code, cast(type[object], int)),
            ("command", self.command, cast(type[object], str)),
            ("started_at", self.started_at, cast(type[object], str)),
            ("ended_at", self.ended_at, cast(type[object], str)),
            ("timed_out", self.timed_out, cast(type[object], bool)),
        )
        for name, value, expected_type in exact_fields:
            if type(value) is not expected_type:
                raise TypeError(
                    f"ValidationRecord.{name} must be {expected_type.__name__}"
                )

    def _validate_identity_fields(self) -> None:
        if self.schema_version != VALIDATION_RECORD_SCHEMA_VERSION:
            raise ValueError(
                "ValidationRecord.schema_version must be "
                f"{VALIDATION_RECORD_SCHEMA_VERSION}"
            )
        for name, value in (
            ("suite", self.suite),
            ("head_sha", self.head_sha),
            ("command", self.command),
        ):
            if not value.strip():
                raise ValueError(f"ValidationRecord.{name} must not be blank")

    def _validate_optional_paths(self) -> None:
        for name, value in (
            ("stdout_path", self.stdout_path),
            ("stderr_path", self.stderr_path),
        ):
            if value is not None and (type(value) is not str or not value):
                raise TypeError(f"ValidationRecord.{name} must be str or None")

    def _validate_timestamps(self) -> None:
        try:
            datetime.fromisoformat(self.started_at)
            datetime.fromisoformat(self.ended_at)
        except ValueError as exc:
            raise ValueError(
                "ValidationRecord timestamps must be ISO-8601 datetime strings"
            ) from exc

    def _validate_result_consistency(self) -> None:
        if self.passed is not (self.exit_code == 0):
            raise ValueError(
                "ValidationRecord.passed must exactly match exit_code == 0"
            )
        if self.timed_out and self.passed:
            raise ValueError("ValidationRecord.timed_out cannot be true when passed")

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for JSON serialization."""
        return {
            "schema_version": self.schema_version,
            "suite": self.suite,
            "head_sha": self.head_sha,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "command": self.command,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "timed_out": self.timed_out,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ValidationRecord:
        """Strictly validate and deserialize the durable record contract."""
        actual_fields = frozenset(data)
        if actual_fields != _VALIDATION_RECORD_FIELDS:
            missing = sorted(_VALIDATION_RECORD_FIELDS - actual_fields)
            extra = sorted(actual_fields - _VALIDATION_RECORD_FIELDS)
            raise ValueError(
                "ValidationRecord fields must match the schema exactly; "
                f"missing={missing} extra={extra}"
            )
        return cls(
            schema_version=_required_validation_field(data, "schema_version", int),
            suite=_required_validation_field(data, "suite", str),
            head_sha=_required_validation_field(data, "head_sha", str),
            passed=_required_validation_field(data, "passed", bool),
            exit_code=_required_validation_field(data, "exit_code", int),
            command=_required_validation_field(data, "command", str),
            started_at=_required_validation_field(data, "started_at", str),
            ended_at=_required_validation_field(data, "ended_at", str),
            timed_out=_required_validation_field(data, "timed_out", bool),
            stdout_path=_optional_validation_path(data, "stdout_path"),
            stderr_path=_optional_validation_path(data, "stderr_path"),
        )


@dataclass
class ValidationState:
    """Retry flow state for a session.

    Stored at: <run_dir>/validation-state.json
    """

    retry_count: int = 0  # Queued retry attempt number, not completed retry count.
    max_retries: int = 3
    validation_cmd: str | None = None
    last_error: str | None = None
    last_error_file: str | None = None
    original_prompt_file: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def retries_remaining(self) -> int:
        """Number of retries still available."""
        return max(0, self.max_retries - self.retry_count)

    @property
    def can_retry(self) -> bool:
        """Whether the queued retry attempt is within the retry budget."""
        return self.max_retries > 0 and self.retry_count <= self.max_retries


@dataclass(frozen=True)
class ReviewExchangeSummary:
    """Review exchange summary metadata.

    Stored at: <run_dir>/review-exchange/summary.json
    """

    summary: ReviewExchangeSummaryV1
    run_assets: ReviewExchangeRunAssets

    @property
    def run_dir(self) -> Path:
        return self.run_assets.run_dir

    @property
    def exchange_dir(self) -> Path:
        return self.run_assets.exchange_dir

    @property
    def summary_path(self) -> Path:
        return self.run_assets.summary_path

    @property
    def validation_record_path(self) -> Path:
        return self.run_assets.validation_record_path


class SessionOutput(Protocol):
    """Protocol for session artifact storage.

    All artifacts for a session are stored in a single run directory.
    This provides a unified interface for:
    - Run lifecycle (create, find, prune)
    - Manifest management
    - Validation artifacts
    - Retry state
    - Diagnostics
    - Session metadata
    """

    # -------------------------------------------------------------------------
    # Run Lifecycle
    # -------------------------------------------------------------------------

    def start_run(
        self,
        worktree_path: Path,
        session_name: str,
        issue_number: int | None = None,
        agent_label: str | None = None,
        backend: str | None = None,
        claude_log_dir: str | None = None,
        orchestrator_log: str | None = None,
        completion_path: str | None = None,
        retention_tier: str = "hot",
        retention_days: int = 7,
        retention_pinned: bool = False,
    ) -> SessionRunAssets:
        """Create a new run directory and initial manifest.

        Args:
            worktree_path: Path to the worktree
            session_name: Name of the session (e.g., "issue-42")
            issue_number: Issue number being worked on
            agent_label: Agent label (e.g., "agent:developer")
            backend: Terminal backend (e.g., "subprocess", "tmux")
            claude_log_dir: Path to Claude log directory
            orchestrator_log: Path to orchestrator log
            completion_path: Path where completion.json will be written
            retention_tier: Retention tier label persisted in manifest
            retention_days: Requested retention metadata in days. Zero records
                an immediate deadline; it does not preserve artifacts after
                their owning worktree is deleted.
            retention_pinned: Whether this run is pinned from retention cleanup

        Returns:
            SessionRunAssets with owned paths to the new run directory
        """
        ...

    def find_run_dir(
        self,
        worktree_path: Path,
        session_name: str | None = None,
    ) -> Path | None:
        """Find the latest run directory for a session.

        Args:
            worktree_path: Path to the worktree
            session_name: Session name to find, or None for any latest

        Returns:
            Path to run directory, or None if not found
        """
        ...

    def ensure_run_dir(
        self,
        worktree_path: Path,
        session_name: str,
    ) -> Path:
        """Get existing run dir or create a minimal one.

        Args:
            worktree_path: Path to the worktree
            session_name: Name of the session

        Returns:
            Path to the run directory
        """
        ...

    def prune_runs(
        self,
        worktree_path: Path,
        keep: int,
    ) -> list[Path]:
        """Delete old runs, keeping the last N.

        Args:
            worktree_path: Path to the worktree
            keep: Number of runs to keep

        Returns:
            List of paths that were deleted
        """
        ...

    # -------------------------------------------------------------------------
    # Manifest
    # -------------------------------------------------------------------------

    def update_manifest(
        self,
        run_dir: Path,
        updates: dict[str, Any],
    ) -> None:
        """Update the manifest with additional data.

        Args:
            run_dir: Path to the run directory
            updates: Dictionary of fields to update

        Implementations must reject any ``updates`` containing keys in
        ``RESERVED_VALIDATION_OUTCOME_FIELDS`` — those three fields are
        owned by ``update_validation_outcome``. Direct dict-merge writes
        of ``validation_passed`` / ``validation_status`` /
        ``validation_reason`` are how the stale-reason-on-success bug
        slipped in originally; the runtime guard prevents recurrence.
        """
        ...

    def record_scheduled_watchdog(
        self,
        run: SessionRunAssets,
        watchdog: "ScheduledSessionWatchdog",
    ) -> None:
        """Persist the planner-owned outer timeout before terminal creation."""
        ...

    def update_validation_outcome(
        self,
        run_dir: Path,
        outcome: "ValidationOutcome",
        *,
        ended_at: str | None = None,
        session_outcome_value: str | None = None,
    ) -> None:
        """Atomically write a typed validation outcome to the manifest.

        Replaces ad-hoc ``update_manifest({"validation_status": ...,
        "validation_reason": ...})`` calls. Always writes all three
        legacy fields together so the previous outcome's reason cannot
        survive into the new outcome. ``ValidationPassed`` explicitly
        clears any prior ``validation_reason`` to ``None``.

        Args:
            run_dir: Path to the run directory.
            outcome: The typed validation outcome.
            ended_at: Optional ISO-8601 timestamp to record alongside.
            session_outcome_value: Optional ``outcome`` enum value to record
                (the higher-level session outcome — completed, blocked,
                etc.). Kept here to consolidate the common
                "validation finished, set everything together" write.
        """
        ...

    def read_manifest(
        self,
        run_dir: Path,
    ) -> dict[str, Any] | None:
        """Read the manifest from a run directory.

        Args:
            run_dir: Path to the run directory

        Returns:
            Manifest dictionary, or None if not found
        """
        ...

    # -------------------------------------------------------------------------
    # Validation Artifacts
    # -------------------------------------------------------------------------

    def write_validation_record(
        self,
        run_dir: Path,
        record: ValidationRecord,
    ) -> Path:
        """Write a validation record to the run directory.

        Args:
            run_dir: Path to the run directory
            record: Validation record to write

        Returns:
            Path to the written record
        """
        ...

    def read_validation_record(
        self,
        run_dir: Path,
    ) -> ValidationRecord | None:
        """Read validation record from run directory.

        Args:
            run_dir: Path to the run directory

        Returns:
            ValidationRecord if found, None otherwise
        """
        ...

    def write_validation_output(
        self,
        run_dir: Path,
        stdout: str,
        stderr: str,
    ) -> tuple[Path, Path]:
        """Write validation stdout/stderr to run directory.

        Args:
            run_dir: Path to the run directory
            stdout: Standard output content
            stderr: Standard error content

        Returns:
            Tuple of (stdout_path, stderr_path)
        """
        ...

    # -------------------------------------------------------------------------
    # Retry State
    # -------------------------------------------------------------------------

    def write_validation_state(
        self,
        run_dir: Path,
        state: ValidationState,
    ) -> Path:
        """Write validation retry state to run directory.

        Args:
            run_dir: Path to the run directory
            state: Validation state to write

        Returns:
            Path to the written state file
        """
        ...

    def read_validation_state(
        self,
        run_dir: Path,
    ) -> ValidationState | None:
        """Read validation retry state from run directory.

        Args:
            run_dir: Path to the run directory

        Returns:
            ValidationState if found, None otherwise
        """
        ...

    def write_validation_errors(
        self,
        run_dir: Path,
        validation_cmd: str,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> Path:
        """Write human-readable validation errors to run directory.

        Args:
            run_dir: Path to the run directory
            validation_cmd: Command that failed
            stdout: Standard output
            stderr: Standard error
            exit_code: Exit code

        Returns:
            Path to the written errors file
        """
        ...

    def write_retry_prompt(
        self,
        run_dir: Path,
        content: str,
    ) -> Path:
        """Write retry prompt for agent to run directory.

        Args:
            run_dir: Path to the run directory
            content: Rendered retry prompt content

        Returns:
            Path to the written prompt file
        """
        ...

    def clear_retry_state(
        self,
        run_dir: Path,
    ) -> None:
        """Clear retry state files from run directory.

        Called when validation passes or max retries exhausted.

        Args:
            run_dir: Path to the run directory
        """
        ...

    # -------------------------------------------------------------------------
    # Diagnostics
    # -------------------------------------------------------------------------

    def write_diagnostic(
        self,
        run_dir: Path,
        diagnostic: dict[str, Any],
        prefix: str = "failure-diagnostic",
    ) -> Path:
        """Write a diagnostic file to run directory.

        Args:
            run_dir: Path to the run directory
            diagnostic: Diagnostic data to write
            prefix: Filename prefix (timestamp will be appended)

        Returns:
            Path to the written diagnostic file
        """
        ...

    # -------------------------------------------------------------------------
    # Review Exchange Artifacts
    # -------------------------------------------------------------------------

    def start_review_exchange_run(
        self,
        worktree_path: Path,
        *,
        issue_number: int,
        parent_session_name: str,
        agent_label: str,
    ) -> "ReviewExchangeRun":
        """Allocate a typed run directory for one review exchange."""
        ...

    def store_review_exchange_summary(
        self,
        review_run: "ReviewExchangeRun",
        summary: ReviewExchangeSummaryV1,
    ) -> ReviewExchangeSummary:
        """Persist review exchange summary artifacts for a session.

        Args:
            review_run: Typed review-exchange run owner.
            summary: Summary payload to store

        Returns:
            ReviewExchangeSummary describing stored artifacts
        """
        ...

    def load_review_exchange_summary(
        self,
        worktree_path: Path,
        session_name: str,
        *,
        not_before_started_at: str | None = None,
    ) -> ReviewExchangeSummary | None:
        """Load stored review exchange summary for a session.

        Args:
            worktree_path: Path to the worktree
            session_name: Session name to load summary
            not_before_started_at: Optional ISO timestamp boundary. When set,
                summaries from runs that started before this timestamp are
                ignored so fresh retry boundaries cannot reuse older reviews.

        Returns:
            ReviewExchangeSummary if found, None otherwise
        """
        ...

    def count_consecutive_review_exchange_no_completion(
        self,
        worktree_path: Path,
        session_name: str,
        *,
        not_before_started_at: str | None = None,
    ) -> int:
        """Count consecutive recent review-exchange summaries that ended in
        ``status=error reason=*_no_completion``, newest first.

        Stops counting at any of:

        - A summary whose status / reason does not match (a clean ``ok``
          exchange or a different error reason resets the count to zero).
        - A run_dir that is not a review-exchange (i.e. a coding/rework
          session). This treats each coding turn as a fresh attempt —
          failures from a prior coding session do not carry across a
          successful coder turn into the new session's quota.
        - Crossing the ``not_before_started_at`` boundary (typically the
          scratch-reset boundary).

        Powers the bound on the
        ``review_exchange.role_timeout → review_exchange.completed (error) →
        relaunch`` runaway loop: when a reviewer agent can't complete its
        round (e.g. its sandbox blocks the response-file write, or the
        prompt is unreachable), every retry produces another
        ``reviewer_no_completion`` summary. Without an upper bound this
        cycles for the lifetime of the orchestrator process.

        Args:
            worktree_path: Path to the worktree.
            session_name: Reserved for future per-coding-session scoping;
                today the function relies on the coding-session boundary
                detection (non-review-exchange run_dir stops the count)
                rather than name-based filtering, since review-exchange
                runs do not store their parent coding session in the
                manifest.
            not_before_started_at: Optional ISO timestamp boundary; runs
                that started before this timestamp do not contribute.

        Returns:
            Number of consecutive matching error summaries.
        """
        ...

    # -------------------------------------------------------------------------
    # Session Metadata
    # -------------------------------------------------------------------------

    def write_worktree_note(
        self,
        run_dir: Path,
        payload: dict[str, Any],
    ) -> Path:
        """Write worktree metadata to run directory.

        Args:
            run_dir: Path to the run directory
            payload: Worktree metadata

        Returns:
            Path to the written note file
        """
        ...

    def write_session_identity(
        self,
        run_dir: Path,
        payload: dict[str, Any],
    ) -> Path:
        """Write session identity to run directory.

        Args:
            run_dir: Path to the run directory
            payload: Session identity data

        Returns:
            Path to the written identity file
        """
        ...

    def append_cleaned_session_log(
        self,
        run_dir: Path,
        content: str,
        *,
        header: str | None = None,
    ) -> None:
        """Append cleaned display-safe content to the legacy text session log.

        Args:
            run_dir: Path to the run directory
            content: Body text to clean before writing
            header: Optional structured header written verbatim before the body
        """
        ...

    def record_exchange_chapter(
        self,
        run_dir: Path,
        *,
        role: str,
        exchange_run_id: str,
        issue_number: int,
        cycle_index: int,
        section: str,
        recording_event_index: int,
        recorded_at: str,
        label: str,
    ) -> Path:
        """Append a chapter entry to ``<run_dir>/<role>/chapters.json``.

        Returns the sidecar path. Creates the file if absent. Idempotent
        across role/exchange identity — supplying the same exchange_run_id
        and role consistently is the caller's responsibility.
        """
        ...

    def read_exchange_chapters(
        self,
        run_dir: Path,
        *,
        role: str,
    ) -> "ExchangeChapterSidecar | None":
        """Load the chapters sidecar for one role, or None if absent."""
        ...

    # -------------------------------------------------------------------------
    # Log Access
    # -------------------------------------------------------------------------

    def get_log_path(
        self,
        worktree_path: Path,
        session_name: str,
    ) -> Path | None:
        """Get the canonical session artifact path for a session.

        Args:
            worktree_path: Path to the worktree
            session_name: Name of the session

        Returns:
            Path to the most relevant run-scoped session artifact, or None if not found
        """
        ...

    def get_log_path_for_run_dir(
        self,
        run_dir: Path,
    ) -> Path | None:
        """Get the best available UI log path for a specific run directory."""
        ...

    def attach_claude_log(
        self,
        run_dir: Path,
    ) -> Path | None:
        """Find and attach Claude log to run directory.

        Creates a symlink and updates manifest with Claude log path.

        Args:
            run_dir: Path to the run directory

        Returns:
            Path to Claude log, or None if not found
        """
        ...

    def write_orchestrator_tail(
        self,
        run_dir: Path,
        log_path: Path,
        issue_number: int,
        session_name: str,
        max_lines: int = 400,
    ) -> Path | None:
        """Write filtered orchestrator log tail to run directory.

        Filters to entries relevant to this session.

        Args:
            run_dir: Path to the run directory
            log_path: Path to full orchestrator log
            issue_number: Issue number for filtering
            session_name: Session name for filtering
            max_lines: Maximum lines to include

        Returns:
            Path to written tail file, or None on failure
        """
        ...

    # -------------------------------------------------------------------------
    # Utility Methods
    # -------------------------------------------------------------------------

    def session_name_from_path(self, rel_path: str | None) -> str | None:
        """Extract session name from a path containing sessions directory.

        Args:
            rel_path: Relative path that may contain sessions directory

        Returns:
            Session name if found, None otherwise
        """
        ...

    # -------------------------------------------------------------------------
    # Review Feedback
    # -------------------------------------------------------------------------

    def save_review_feedback(
        self,
        worktree_path: Path,
        cycle: int,
        feedback: str,
        reviewer: str | None = None,
        pr_number: int | None = None,
    ) -> Path:
        """Save review feedback for a specific cycle.

        Each review cycle's feedback is stored as a separate file in:
        <worktree>/.issue-orchestrator/review-feedback/cycle-N.md

        Args:
            worktree_path: Path to the worktree
            cycle: Rework cycle number (1-based)
            feedback: The review feedback text
            reviewer: Optional reviewer label
            pr_number: Optional PR number

        Returns:
            Path to the written feedback file
        """
        ...
