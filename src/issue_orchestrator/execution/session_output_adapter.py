"""Filesystem-backed session output adapter.

Implements the SessionOutput port for local filesystem storage.
All session artifacts are stored in:

    <worktree>/.issue-orchestrator/sessions/<run_id>__<session_name>/
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from ..contracts.run_manifest import validate_run_manifest_payload
from ..domain.artifact_contracts import (
    RESERVED_VALIDATION_OUTCOME_FIELDS,
    ValidationOutcome,
    validation_outcome_to_manifest_fields,
)
from ..infra.terminal_cleaning import (
    clean_terminal_line,
    dedupe_consecutive_lines,
    is_spinner_fragment,
)
from ..infra.terminal_recording import append_output_event
from ..domain.review_exchange_manifest import ReviewExchangeManifestHeader
from ..domain.review_exchange_resume import is_no_completion_reason
from ..domain.review_exchange_run import ReviewExchangeRun, ReviewExchangeRunAssets
from ..domain.review_exchange_summary import ReviewExchangeSummaryV1
from ..domain.run_manifest import RunManifest
from ..domain.exchange_chapter import (
    CHAPTER_SCHEMA_VERSION,
    ChapterSidecarIdentityMismatch,
    ExchangeChapter,
    ExchangeChapterSidecar,
)
from .run_directory_artifacts import RunDirectoryArtifacts
from ..ports.session_output import (
    ReviewExchangeSummary,
    SessionRunAssets,
    ValidationRecord,
    ValidationState,
)
from .review_exchange_session_output import (
    start_review_exchange_run as _start_review_exchange_run,
    store_review_exchange_summary as _store_review_exchange_summary,
)
from . import session_output_log_artifacts as log_artifacts
from .session_output_log_artifacts import SessionLogArtifacts

logger = logging.getLogger(__name__)

# Directory and file names
SESSION_OUTPUT_DIR = "sessions"
TERMINAL_RECORDING_NAME = log_artifacts.TERMINAL_RECORDING_NAME
LEGACY_UI_LOG_NAME = log_artifacts.LEGACY_UI_LOG_NAME
PANE_LOG_NAME = log_artifacts.PANE_LOG_NAME
MANIFEST_NAME = "manifest.json"
LATEST_NAME = "latest.json"
INDEX_NAME = "index.json"
ROOT_LATEST_NAME = "session-latest.json"
ORCHESTRATOR_TAIL_NAME = log_artifacts.ORCHESTRATOR_TAIL_NAME
CLAUDE_SESSION_PATH_NAME = log_artifacts.CLAUDE_SESSION_PATH_NAME
CLAUDE_SESSION_LOG_NAME = log_artifacts.CLAUDE_SESSION_LOG_NAME

# Validation artifact names
VALIDATION_RECORD_NAME = "validation-record.json"
VALIDATION_STDOUT_NAME = "validation-stdout.log"
VALIDATION_STDERR_NAME = "validation-stderr.log"
VALIDATION_STATE_NAME = "validation-state.json"
VALIDATION_ERRORS_NAME = "validation-errors.txt"
RETRY_PROMPT_NAME = "retry-prompt.md"

# Other artifact names
WORKTREE_NOTE_NAME = "worktree.json"
SESSION_IDENTITY_NAME = "session-identity.json"
SESSION_PROMPT_NAME = "session-prompt.txt"

# Review exchange artifacts
REVIEW_EXCHANGE_DIR_NAME = "review-exchange"
REVIEW_EXCHANGE_SUMMARY_NAME = "summary.json"
REVIEW_EXCHANGE_TRANSCRIPT_NAME = "transcript.log"
EXCHANGE_CHAPTERS_NAME = "chapters.json"

# Review feedback artifacts (stored per-cycle for diagnostics)
REVIEW_FEEDBACK_DIR_NAME = "review-feedback"


class FileSystemSessionOutput(RunDirectoryArtifacts):
    """Filesystem-backed implementation of SessionOutput port.

    All artifacts for a session are stored in a single run directory:
        <worktree>/.issue-orchestrator/sessions/<run_id>__<session_name>/
    """

    def __init__(self) -> None:
        self._io_lock = threading.RLock()
        self._log_artifacts = SessionLogArtifacts(
            find_run_dir=lambda worktree_path, session_name: self.find_run_dir(
                worktree_path,
                session_name=session_name,
            ),
            read_manifest=self.read_manifest,
            update_manifest=self.update_manifest,
            write_text=lambda path, content: self._write_text(path, content),
            ensure_symlink=lambda symlink_path, target: self._ensure_symlink(
                symlink_path,
                target,
            ),
        )

    # -------------------------------------------------------------------------
    # Run Lifecycle
    # -------------------------------------------------------------------------

    def sessions_base_dir(self, worktree_path: Path) -> Path:
        """Get the base sessions directory for a worktree."""
        if isinstance(worktree_path, Mock) or not isinstance(worktree_path, Path):
            raise TypeError("worktree_path must be a pathlib.Path")
        return worktree_path / ".issue-orchestrator" / SESSION_OUTPUT_DIR

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
        """Create a new run directory and initial manifest."""
        with self._io_lock:
            run_id = self._run_timestamp()
            base_dir = self._ensure_base_dir(worktree_path)
            run_dir = base_dir / self._run_dir_name(session_name, run_id)
            run_dir.mkdir(parents=True, exist_ok=True)

            # Create symlink to latest run
            symlink_path = base_dir / session_name
            self._ensure_symlink(symlink_path, run_dir)

            log_path = run_dir / TERMINAL_RECORDING_NAME
            terminal_recording_path = run_dir / TERMINAL_RECORDING_NAME
            manifest_path = run_dir / MANIFEST_NAME
            started_at = datetime.now(timezone.utc).isoformat()
            retention_window_days = max(0, retention_days)

            manifest = {
                "session_name": session_name,
                "run_id": run_id,
                "started_at": started_at,
                "issue_number": issue_number,
                "agent_label": agent_label,
                "backend": backend,
                "worktree": str(worktree_path),
                "run_dir": str(run_dir),
                "log_path": str(terminal_recording_path),
                "claude_log_dir": claude_log_dir,
                "orchestrator_log": orchestrator_log,
                "completion_path": completion_path,
                "diagnostic_path": None,
                "retention_tier": retention_tier,
                "retention_days": retention_window_days,
                "retention_pinned": retention_pinned,
                "artifacts": {
                    "terminal_recording": {
                        "kind": "terminal_recording",
                        "path": str(terminal_recording_path),
                        "content_type": "application/x-ndjson",
                    },
                },
            }
            self._write_json(
                manifest_path,
                validate_run_manifest_payload(
                    manifest,
                    strict_required_artifacts=True,
                ),
            )
            if not terminal_recording_path.exists():
                terminal_recording_path.write_text("", encoding="utf-8")

            if claude_log_dir:
                self._write_text(run_dir / "claude-log.path", claude_log_dir)
            if orchestrator_log:
                self._write_text(run_dir / "orchestrator-log.path", orchestrator_log)

            self._update_latest(worktree_path, manifest)
            self._append_index(worktree_path, manifest)

        return SessionRunAssets.from_paths(
            session_name=session_name,
            run_id=run_id,
            worktree_path=worktree_path,
            run_dir=run_dir,
            terminal_recording_path=log_path,
            manifest_path=manifest_path,
            started_at=started_at,
        )

    def find_run_dir(
        self,
        worktree_path: Path,
        session_name: str | None = None,
    ) -> Path | None:
        """Find the latest run directory for a session."""
        base_dir = self.sessions_base_dir(worktree_path)
        if not base_dir.exists():
            return None

        if session_name:
            # Check for legacy non-timestamped directory
            legacy = base_dir / session_name
            if legacy.exists() and legacy.is_dir() and not legacy.is_symlink():
                return legacy

            # Find timestamped directories matching session name
            candidates = sorted(
                [
                    d
                    for d in base_dir.iterdir()
                    if d.is_dir() and d.name.endswith(f"__{session_name}")
                ],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            return candidates[0] if candidates else None

        # No session name - find most recent run
        latest = self._load_latest(worktree_path)
        if latest:
            run_dir = Path(latest["run_dir"])
            if run_dir.exists():
                return run_dir

        candidates = sorted(
            [d for d in base_dir.iterdir() if d.is_dir() and not d.is_symlink()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def ensure_run_dir(
        self,
        worktree_path: Path,
        session_name: str,
    ) -> Path:
        """Get existing run dir or create a minimal one."""
        existing = self.find_run_dir(worktree_path, session_name=session_name)
        if existing:
            return existing
        run = self.start_run(worktree_path, session_name)
        return run.run_dir

    def prune_runs(
        self,
        worktree_path: Path,
        keep: int,
    ) -> list[Path]:
        """Delete old runs, keeping the last N."""
        if keep <= 0:
            return []

        base_dir = self.sessions_base_dir(worktree_path)
        if not base_dir.exists():
            return []

        runs_with_mtime: list[tuple[Path, float]] = []
        for run_dir in base_dir.iterdir():
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            try:
                runs_with_mtime.append((run_dir, run_dir.stat().st_mtime))
            except FileNotFoundError:
                # Run directory vanished between listing and stat.
                continue

        runs = [
            run
            for run, _mtime in sorted(
                runs_with_mtime,
                key=lambda item: item[1],
                reverse=True,
            )
        ]

        removed: list[Path] = []
        now = datetime.now(timezone.utc)
        for run_dir in runs[keep:]:
            if RunManifest.has_retained_timeline_evidence(run_dir, now=now):
                continue
            try:
                self._delete_tree(run_dir)
                removed.append(run_dir)
            except OSError:
                continue

        if removed:
            self._prune_index(worktree_path, removed)
            self._refresh_latest(worktree_path)

        return removed

    def list_runs(
        self,
        worktree_path: Path,
    ) -> list[dict[str, Any]]:
        """List all runs in a worktree, sorted by start time (oldest first).

        Returns a list of run summaries from the index, each containing:
        - session_name: The phase name (e.g., "coding-1", "review-1")
        - run_id: Timestamp identifier
        - started_at: ISO timestamp
        - issue_number: Associated issue
        - run_dir: Path to run directory
        - agent_label: Agent type used
        - status: Derived status (completed, in_progress, failed)
        """
        index_path = self.sessions_base_dir(worktree_path) / INDEX_NAME
        index = self._read_json(index_path)
        if not index or "runs" not in index:
            return []

        runs = []
        for run_info in index["runs"]:
            run_dir = run_info.get("run_dir")
            if run_dir and Path(run_dir).exists():
                # Enhance with status from manifest
                manifest = self.read_manifest(Path(run_dir))
                if manifest:
                    run_info = {**run_info}  # Copy to avoid mutating index
                    run_info["status"] = self._derive_run_status(
                        manifest, Path(run_dir)
                    )
                    run_info["ended_at"] = manifest.get("ended_at")
                    run_info["outcome"] = manifest.get("outcome")
                    run_info["validation_passed"] = manifest.get("validation_passed")
                runs.append(run_info)

        # Sort by started_at (oldest first for linear display)
        runs.sort(key=lambda r: r.get("started_at", ""))
        return runs

    def _derive_run_status(self, manifest: dict[str, Any], run_dir: Path) -> str:
        """Derive the status of a run from its manifest and artifacts."""
        if manifest.get("ended_at"):
            outcome = manifest.get("outcome", "unknown")
            if outcome in ("completed", "blocked", "timeout", "timed_out"):
                validation = manifest.get("validation_passed")
                if validation is False:
                    return "validation_failed"
                return outcome
            return outcome
        # Check if session is still running (no completion)
        completion_path = manifest.get("completion_path")
        if completion_path:
            full_path = run_dir.parent.parent / completion_path
            if full_path.exists():
                return "completed"
        return "in_progress"

    # -------------------------------------------------------------------------
    # Manifest
    # -------------------------------------------------------------------------

    def update_manifest(
        self,
        run_dir: Path,
        updates: dict[str, Any],
    ) -> None:
        """Update the manifest with additional data.

        Rejects ad-hoc writes of the validation-outcome triple
        (``validation_passed`` / ``validation_status`` /
        ``validation_reason``) — those must go through
        ``update_validation_outcome`` so all three are written
        atomically and the previous outcome's reason cannot survive
        into a fresh ``passed`` outcome (PR #6299 follow-up bug).
        """
        reserved = RESERVED_VALIDATION_OUTCOME_FIELDS & updates.keys()
        if reserved:
            raise ValueError(
                "update_manifest() must not write validation outcome fields "
                f"directly: {sorted(reserved)}. Use update_validation_outcome("
                "ValidationPassed() | ValidationFailed(reason=...) | "
                "ValidationRetry(reason=...)) instead — that path writes all "
                "three legacy fields atomically and clears stale reasons."
            )
        self._update_manifest_unchecked(run_dir, updates)

    def _update_manifest_unchecked(
        self,
        run_dir: Path,
        updates: dict[str, Any],
    ) -> None:
        """Internal manifest writer that bypasses the validation-outcome
        guard. Only ``update_validation_outcome`` should call this with
        validation_* keys — every other caller must go through the
        public ``update_manifest`` so the guard is enforced."""
        with self._io_lock:
            manifest_path = run_dir / MANIFEST_NAME
            manifest = self._read_json(manifest_path) or {}
            self._bootstrap_manifest_identity(run_dir, manifest)
            manifest.update(updates)
            self._sync_manifest_artifacts(manifest)
            self._write_json(
                manifest_path,
                validate_run_manifest_payload(
                    manifest,
                    strict_required_artifacts=True,
                ),
            )

    def update_validation_outcome(
        self,
        run_dir: Path,
        outcome: ValidationOutcome,
        *,
        ended_at: str | None = None,
        session_outcome_value: str | None = None,
    ) -> None:
        """Atomically write a typed validation outcome to the manifest.

        See SessionOutput.update_validation_outcome docstring on the
        port for the rationale. The implementation projects the typed
        outcome through ``validation_outcome_to_manifest_fields`` —
        which always emits all three legacy fields together — and
        writes them via the unchecked merge.
        """
        updates: dict[str, Any] = dict(validation_outcome_to_manifest_fields(outcome))
        if ended_at is not None:
            updates["ended_at"] = ended_at
        if session_outcome_value is not None:
            updates["outcome"] = session_outcome_value
        self._update_manifest_unchecked(run_dir, updates)

    def read_manifest(
        self,
        run_dir: Path,
    ) -> dict[str, Any] | None:
        """Read the manifest from a run directory."""
        with self._io_lock:
            return self._read_json(run_dir / MANIFEST_NAME)

    # -------------------------------------------------------------------------
    # Validation Artifacts
    # -------------------------------------------------------------------------

    def write_validation_record(
        self,
        run_dir: Path,
        record: ValidationRecord,
    ) -> Path:
        """Write a validation record to the run directory."""
        record_path = run_dir / VALIDATION_RECORD_NAME
        self._write_json(record_path, record.to_dict())
        self.update_manifest(run_dir, {"validation_record_path": str(record_path)})
        logger.debug("Wrote validation record to %s", record_path)
        return record_path

    def read_validation_record(
        self,
        run_dir: Path,
    ) -> ValidationRecord | None:
        """Read validation record from run directory."""
        record_path = run_dir / VALIDATION_RECORD_NAME
        data = self._read_json(record_path)
        if not data:
            return None
        try:
            return ValidationRecord.from_dict(data)
        except (KeyError, TypeError) as e:
            logger.warning(
                "Failed to parse validation record at %s: %s", record_path, e
            )
            return None

    def write_validation_output(
        self,
        run_dir: Path,
        stdout: str,
        stderr: str,
    ) -> tuple[Path, Path]:
        """Write validation stdout/stderr to run directory."""
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / VALIDATION_STDOUT_NAME
        stderr_path = run_dir / VALIDATION_STDERR_NAME

        self._write_text(stdout_path, stdout)
        self._write_text(stderr_path, stderr)

        self.update_manifest(
            run_dir,
            {
                "validation_stdout": str(stdout_path),
                "validation_stderr": str(stderr_path),
            },
        )

        return stdout_path, stderr_path

    # -------------------------------------------------------------------------
    # Retry State
    # -------------------------------------------------------------------------

    def write_validation_state(
        self,
        run_dir: Path,
        state: ValidationState,
    ) -> Path:
        """Write validation retry state to run directory."""
        state_path = run_dir / VALIDATION_STATE_NAME

        data = asdict(state)
        if not data.get("created_at"):
            data["created_at"] = self._now_iso()
        data["updated_at"] = self._now_iso()

        self._write_json(state_path, data)
        logger.info(
            "Wrote validation state to %s (retry_count=%d)",
            state_path,
            state.retry_count,
        )
        return state_path

    def read_validation_state(
        self,
        run_dir: Path,
    ) -> ValidationState | None:
        """Read validation retry state from run directory."""
        state_path = run_dir / VALIDATION_STATE_NAME
        data = self._read_json(state_path)
        if not data:
            return None

        return ValidationState(
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            validation_cmd=data.get("validation_cmd"),
            last_error=data.get("last_error"),
            last_error_file=data.get("last_error_file"),
            original_prompt_file=data.get("original_prompt_file"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )

    def write_validation_errors(
        self,
        run_dir: Path,
        validation_cmd: str,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> Path:
        """Write human-readable validation errors to run directory."""
        errors_path = run_dir / VALIDATION_ERRORS_NAME

        content = f"""=== VALIDATION FAILED ===
Command: {validation_cmd}
Exit code: {exit_code}
Timestamp: {self._now_iso()}

=== STDERR ===
{stderr}

=== STDOUT ===
{stdout}
"""
        self._write_text(errors_path, content)
        logger.info("Wrote validation errors to %s", errors_path)
        return errors_path

    def write_retry_prompt(
        self,
        run_dir: Path,
        content: str,
    ) -> Path:
        """Write retry prompt for agent to run directory."""
        prompt_path = run_dir / RETRY_PROMPT_NAME
        self._write_text(prompt_path, content)
        logger.info("Wrote retry prompt to %s", prompt_path)
        return prompt_path

    def write_session_prompt(
        self,
        run_dir: Path,
        content: str,
    ) -> Path:
        """Write the run-scoped session prompt used to launch the agent."""
        prompt_path = run_dir / SESSION_PROMPT_NAME
        self._write_text(prompt_path, content)
        self.update_manifest(run_dir, {"session_prompt_path": str(prompt_path)})
        logger.info("Wrote session prompt to %s", prompt_path)
        return prompt_path

    def clear_retry_state(
        self,
        run_dir: Path,
    ) -> None:
        """Clear retry state files from run directory."""
        state_path = run_dir / VALIDATION_STATE_NAME
        prompt_path = run_dir / RETRY_PROMPT_NAME

        for path in [state_path, prompt_path]:
            if path.exists():
                try:
                    path.unlink()
                    logger.debug("Removed %s", path)
                except OSError as e:
                    logger.warning("Failed to remove %s: %s", path, e)

        # Keep validation-errors.txt for debugging

    # -------------------------------------------------------------------------
    # Diagnostics
    # -------------------------------------------------------------------------

    def write_diagnostic(
        self,
        run_dir: Path,
        diagnostic: dict[str, Any],
        prefix: str = "failure-diagnostic",
    ) -> Path:
        """Write a diagnostic file to run directory."""
        run_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        filename = f"{prefix}-{timestamp}.json"
        diagnostic_path = run_dir / filename

        self._write_json(diagnostic_path, diagnostic)
        self.update_manifest(run_dir, {"diagnostic_path": str(diagnostic_path)})
        logger.info("Wrote diagnostic to %s", diagnostic_path)
        return diagnostic_path

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
        retention_days: int = 7,
        retention_tier: str = "hot",
    ) -> ReviewExchangeRun:
        return _start_review_exchange_run(
            self.start_run,
            worktree_path,
            issue_number=issue_number,
            parent_session_name=parent_session_name,
            agent_label=agent_label,
            retention_days=retention_days,
            retention_tier=retention_tier,
        )

    def store_review_exchange_summary(
        self,
        review_run: ReviewExchangeRun,
        summary: ReviewExchangeSummaryV1,
    ) -> ReviewExchangeSummary:
        return _store_review_exchange_summary(
            review_run,
            summary,
            write_json=self._write_json,
            update_manifest=self.update_manifest,
            append_run_log_line=self._append_run_log_line,
        )

    def count_consecutive_review_exchange_no_completion(
        self,
        worktree_path: Path,
        session_name: str,  # noqa: ARG002 — kept for port signature symmetry
        *,
        not_before_started_at: str | None = None,
    ) -> int:
        """Count consecutive ``status=error reason=*_no_completion`` review
        summaries, newest-first, in this worktree's sessions/.

        See the port docstring for semantics. Walks the sessions dir in
        mtime-desc order and stops at the first of:

        - Crossing the ``not_before_started_at`` boundary (scratch reset).
        - A review-exchange summary with any other status/reason (clean
          run resets the streak).
        - A non-review-exchange run_dir (a coding/rework session). Each
          new coding turn is a fresh attempt — failures from an earlier
          coding session must NOT carry across a successful coder turn
          into the new session's quota. Otherwise two timeouts on
          coding session A, then a fresh coding session B, then one
          timeout on B would count as 3 consecutive failures and
          escalate B to needs-human after a single round.
        """
        base_dir = self.sessions_base_dir(worktree_path)
        if not base_dir.exists():
            return 0
        runs = sorted(
            (d for d in base_dir.iterdir() if d.is_dir() and not d.is_symlink()),
            key=self._session_run_chronology_key,
            reverse=True,
        )
        count = 0
        for run_dir in runs:
            manifest = self.read_manifest(run_dir) or {}
            # Explicit parent-session filter when the field is present
            # (PR #6271). Skipping a different-parent run with
            # ``continue`` (rather than ``break``) preserves the
            # legacy behavior where unrelated runs were silently
            # ignored on the way to finding the streak. Legacy runs
            # (no field) fall through to the existing
            # coding-boundary inference below.
            match = self._manifest_matches_parent_session(manifest, session_name)
            if match is False:
                continue
            if not_before_started_at is not None:
                started_at = manifest.get("started_at")
                if (
                    not isinstance(started_at, str)
                    or started_at < not_before_started_at
                ):
                    # Crossed the scratch-reset boundary; stop counting.
                    break
            manifest_dir = manifest.get("review_exchange_dir")
            exchange_dir = (
                Path(manifest_dir)
                if manifest_dir
                else run_dir / REVIEW_EXCHANGE_DIR_NAME
            )
            summary_path = exchange_dir / REVIEW_EXCHANGE_SUMMARY_NAME
            if not summary_path.exists():
                # Not a review-exchange run. The simplest reliable
                # discriminator: a coding/rework run produces a session
                # log + completion artifact but no review-exchange
                # summary. Anything we encounter newer than the current
                # streak that isn't a review-exchange is, by definition,
                # a coding boundary — stop the count so each coding
                # turn gets its own quota.
                break
            summary = self._read_review_exchange_summary(summary_path)
            if summary is None:
                # Unparseable summary — treat as "noise", not as a clean run
                # that resets the count. Keep going to find a definitive
                # signal, but don't increment.
                continue
            if summary.status == "error" and is_no_completion_reason(summary.reason):
                count += 1
                continue
            # First clean (non-no-completion) summary stops the streak —
            # the loop is no longer the no-completion runaway.
            break
        return count

    def _session_run_chronology_key(self, run_dir: Path) -> tuple[int, str, float]:
        """Sort runs by recorded start time, falling back to run id then mtime.

        Files inside a long-lived coding run can be updated after later
        review-exchange runs finish. Using directory mtime as the primary
        chronology signal makes the no-completion budget see the old coding
        run as "newer" and reset the streak after every timeout. The manifest's
        ``started_at`` timestamp is the run chronology contract; the run-dir
        timestamp is the fallback for partial manifests, and mtime is only a
        legacy fallback when neither durable timestamp exists.
        """
        manifest = self.read_manifest(run_dir) or {}
        started_at = manifest.get("started_at")
        if isinstance(started_at, str) and started_at:
            return (1, started_at, run_dir.stat().st_mtime)
        run_id_started_at = self._started_at_from_run_dir_name(run_dir.name)
        if run_id_started_at is not None:
            return (1, run_id_started_at, run_dir.stat().st_mtime)
        return (0, "", run_dir.stat().st_mtime)

    @staticmethod
    def _started_at_from_run_dir_name(name: str) -> str | None:
        run_id = name.split("__", 1)[0]
        if len(run_id) != 16 or not run_id.endswith("Z"):
            return None
        if run_id[8] not in {"-", "T"}:
            return None
        date_part = run_id[:8]
        time_part = run_id[9:15]
        if not (date_part.isdigit() and time_part.isdigit()):
            return None
        return (
            f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
            f"T{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}+00:00"
        )

    @staticmethod
    def _manifest_matches_parent_session(
        manifest: dict[str, Any],
        parent_session_name: str,
    ) -> bool | None:
        """Filter a candidate manifest by ``parent_session_name``.

        Returns:
            True  — manifest carries ``parent_session_name`` and it
                    matches; this candidate belongs to the requested
                    parent coding session.
            False — manifest carries ``parent_session_name`` but it
                    DOESN'T match; explicitly belongs to a different
                    parent — skip.
            None  — manifest has no ``parent_session_name`` (legacy
                    run, pre-PR #6271); caller falls back to the
                    older mtime-walk filtering.

        Implementation routes through
        ``ReviewExchangeManifestHeader.from_manifest`` so the
        manifest section's typed contract is honored on the read
        side too — readers and writers consume the same dataclass
        rather than poking at loose ``dict.get`` calls.
        """
        header = ReviewExchangeManifestHeader.from_manifest(manifest)
        if header is None or header.parent_session_name is None:
            return None
        return header.parent_session_name == parent_session_name

    def load_review_exchange_summary(
        self,
        worktree_path: Path,
        session_name: str,
        *,
        not_before_started_at: str | None = None,
    ) -> ReviewExchangeSummary | None:
        # Resolve against the newest run that has a review-exchange summary.
        # Pre-PR #6271 the only way to scope to a coding session was to
        # walk all runs in mtime order and infer boundaries from
        # run_dir name patterns. Now the runner stamps
        # ``parent_session_name`` on each review-exchange manifest, so
        # the filter is a direct dict lookup. The mtime walk remains
        # as a backwards-compat fallback for runs written before the
        # field existed.
        base_dir = self.sessions_base_dir(worktree_path)
        if not base_dir.exists():
            return None

        all_candidates = sorted(
            (d for d in base_dir.iterdir() if d.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not all_candidates:
            return None
        # Legacy fallback: candidates whose run_dir name ends with
        # ``__{session_name}`` (the pre-PR #6271 scoping heuristic).
        legacy_session_candidates = [
            d for d in all_candidates if d.name.endswith(f"__{session_name}")
        ]

        for candidates in (legacy_session_candidates, all_candidates):
            for run_dir in candidates:
                manifest = self.read_manifest(run_dir) or {}
                # Explicit parent-session filter when the field is
                # present. Skip candidates that explicitly belong to
                # a DIFFERENT parent. Legacy candidates (None) fall
                # through to the historical behavior.
                match = self._manifest_matches_parent_session(manifest, session_name)
                if match is False:
                    continue
                if not_before_started_at is not None:
                    started_at = manifest.get("started_at")
                    if (
                        not isinstance(started_at, str)
                        or started_at < not_before_started_at
                    ):
                        logger.info(
                            "[session_output] Ignoring review exchange summary before boundary: "
                            "session=%s run_dir=%s started_at=%s boundary=%s",
                            session_name,
                            run_dir,
                            started_at,
                            not_before_started_at,
                        )
                        continue
                manifest_dir = manifest.get("review_exchange_dir")
                exchange_dir = (
                    Path(manifest_dir)
                    if manifest_dir
                    else run_dir / REVIEW_EXCHANGE_DIR_NAME
                )
                summary_path = exchange_dir / REVIEW_EXCHANGE_SUMMARY_NAME
                if not summary_path.exists():
                    continue
                summary = self._read_review_exchange_summary(summary_path)
                if summary is None:
                    continue
                return ReviewExchangeSummary(
                    summary=summary,
                    run_assets=ReviewExchangeRunAssets.from_exchange_dir(exchange_dir),
                )
        return None

    def _read_review_exchange_summary(
        self,
        summary_path: Path,
    ) -> ReviewExchangeSummaryV1 | None:
        raw = self._read_json(summary_path)
        if not isinstance(raw, dict):
            return None
        try:
            return ReviewExchangeSummaryV1.from_payload(raw)
        except (TypeError, ValueError):
            logger.warning(
                "[session_output] Ignoring invalid review exchange summary: %s",
                summary_path,
                exc_info=True,
            )
            return None

    # -------------------------------------------------------------------------
    # Review Feedback (per-cycle storage for diagnostics)
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

        Creates a markdown file in review-feedback/ subdirectory:
            <worktree>/.issue-orchestrator/review-feedback/cycle-<N>.md

        Args:
            worktree_path: Path to the worktree.
            cycle: The rework cycle number (1, 2, 3, ...).
            feedback: The review feedback text.
            reviewer: Optional reviewer login.
            pr_number: Optional PR number.

        Returns:
            Path to the saved feedback file.
        """
        feedback_dir = worktree_path / ".issue-orchestrator" / REVIEW_FEEDBACK_DIR_NAME
        feedback_dir.mkdir(parents=True, exist_ok=True)

        filename = f"cycle-{cycle}.md"
        feedback_path = feedback_dir / filename

        # Build markdown content
        lines = [f"# Review Feedback - Cycle {cycle}"]
        if pr_number:
            lines.append(f"\n**PR:** #{pr_number}")
        if reviewer:
            lines.append(f"**Reviewer:** {reviewer}")
        lines.append(f"**Timestamp:** {datetime.now(timezone.utc).isoformat()}")
        lines.append("\n---\n")
        lines.append(feedback)

        feedback_path.write_text("\n".join(lines))
        logger.info("[session_output] Saved review feedback: %s", feedback_path)
        return feedback_path

    def list_review_feedback(self, worktree_path: Path) -> list[Path]:
        """List all review feedback files for a worktree.

        Returns:
            List of feedback file paths, sorted by cycle number.
        """
        feedback_dir = worktree_path / ".issue-orchestrator" / REVIEW_FEEDBACK_DIR_NAME
        if not feedback_dir.exists():
            return []

        files = sorted(feedback_dir.glob("cycle-*.md"))
        return files

    def load_all_review_feedback(self, worktree_path: Path) -> list[dict[str, Any]]:
        """Load all review feedback for a worktree.

        Returns:
            List of dicts with 'cycle', 'path', and 'content' keys.
        """
        result = []
        for path in self.list_review_feedback(worktree_path):
            try:
                # Extract cycle number from filename (cycle-N.md)
                cycle = int(path.stem.split("-")[1])
                result.append(
                    {
                        "cycle": cycle,
                        "path": str(path),
                        "content": path.read_text(),
                    }
                )
            except (ValueError, IndexError):
                logger.warning("[session_output] Invalid feedback filename: %s", path)
        return result

    # -------------------------------------------------------------------------
    # Session Metadata
    # -------------------------------------------------------------------------

    def write_worktree_note(
        self,
        run_dir: Path,
        payload: dict[str, Any],
    ) -> Path:
        """Write worktree metadata to run directory."""
        note_path = run_dir / WORKTREE_NOTE_NAME
        self._write_json(note_path, payload)
        return note_path

    def write_session_identity(
        self,
        run_dir: Path,
        payload: dict[str, Any],
    ) -> Path:
        """Write session identity to run directory."""
        identity_path = run_dir / SESSION_IDENTITY_NAME
        self._write_json(identity_path, payload)
        return identity_path

    def append_cleaned_session_log(
        self,
        run_dir: Path,
        content: str,
        *,
        header: str | None = None,
    ) -> None:
        """Append cleaned display-safe content to the canonical terminal recording."""
        cleaned_lines: list[str] = []
        for raw_line in dedupe_consecutive_lines(content.splitlines()):
            cleaned = clean_terminal_line(raw_line)
            if cleaned.strip() and not is_spinner_fragment(cleaned):
                cleaned_lines.append(cleaned)
        if not cleaned_lines:
            return
        chunks: list[str] = []
        if header:
            chunks.append(header.rstrip())
        chunks.append("\n".join(cleaned_lines).rstrip())
        payload = "\n".join(chunk for chunk in chunks if chunk).rstrip() + "\n\n"
        recording_path = run_dir / TERMINAL_RECORDING_NAME
        recording_path.parent.mkdir(parents=True, exist_ok=True)
        append_output_event(recording_path, payload)

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
        """Append a chapter to ``<run_dir>/<role>/chapters.json`` (read-modify-write)."""
        role_dir = run_dir / role
        role_dir.mkdir(parents=True, exist_ok=True)
        sidecar_path = role_dir / EXCHANGE_CHAPTERS_NAME
        new_chapter = ExchangeChapter(
            cycle_index=cycle_index,
            section=section,
            recording_event_index=recording_event_index,
            recorded_at=recorded_at,
            label=label,
        )
        with self._io_lock:
            sidecar = self._load_chapter_sidecar(
                sidecar_path,
                role=role,
                exchange_run_id=exchange_run_id,
                issue_number=issue_number,
            )
            updated = ExchangeChapterSidecar(
                schema_version=sidecar.schema_version,
                role=sidecar.role,
                exchange_run_id=sidecar.exchange_run_id,
                issue_number=sidecar.issue_number,
                chapters=[*sidecar.chapters, new_chapter],
            )
            self._write_json(sidecar_path, updated.to_payload())
        return sidecar_path

    def read_exchange_chapters(
        self,
        run_dir: Path,
        *,
        role: str,
    ) -> ExchangeChapterSidecar | None:
        sidecar_path = run_dir / role / EXCHANGE_CHAPTERS_NAME
        if not sidecar_path.exists():
            return None
        try:
            payload = json.loads(sidecar_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return ExchangeChapterSidecar.from_payload(payload)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "Malformed chapters sidecar at %s: %s",
                sidecar_path,
                exc,
            )
            return None

    def _load_chapter_sidecar(
        self,
        sidecar_path: Path,
        *,
        role: str,
        exchange_run_id: str,
        issue_number: int,
    ) -> ExchangeChapterSidecar:
        if not sidecar_path.exists():
            return ExchangeChapterSidecar(
                schema_version=CHAPTER_SCHEMA_VERSION,
                role=role,
                exchange_run_id=exchange_run_id,
                issue_number=issue_number,
                chapters=[],
            )
        try:
            payload = json.loads(sidecar_path.read_text())
            existing = ExchangeChapterSidecar.from_payload(payload)
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "Existing chapters sidecar at %s unreadable (%s); starting fresh",
                sidecar_path,
                exc,
            )
            return ExchangeChapterSidecar(
                schema_version=CHAPTER_SCHEMA_VERSION,
                role=role,
                exchange_run_id=exchange_run_id,
                issue_number=issue_number,
                chapters=[],
            )
        # Identity must match — appending a chapter for a different
        # (role, exchange_run_id, issue_number) into an existing sidecar
        # would corrupt the chapter contract that the session viewer
        # relies on. Surface the caller bug instead of silently merging.
        if (
            existing.role != role
            or existing.exchange_run_id != exchange_run_id
            or existing.issue_number != issue_number
        ):
            raise ChapterSidecarIdentityMismatch(
                f"chapters.json identity mismatch at {sidecar_path}: "
                f"existing=(role={existing.role!r}, "
                f"exchange_run_id={existing.exchange_run_id!r}, "
                f"issue_number={existing.issue_number}); "
                f"requested=(role={role!r}, exchange_run_id={exchange_run_id!r}, "
                f"issue_number={issue_number})"
            )
        return existing

    # -------------------------------------------------------------------------
    # Log Access
    # -------------------------------------------------------------------------

    def get_log_path(
        self,
        worktree_path: Path,
        session_name: str,
    ) -> Path | None:
        """Get the canonical session artifact path for a session."""
        return self._log_artifacts.get_log_path(worktree_path, session_name)

    def get_log_path_for_run_dir(self, run_dir: Path) -> Path | None:
        """Get the canonical session artifact path for a known run directory."""
        return self._log_artifacts.get_log_path_for_run_dir(run_dir)

    def attach_claude_log(
        self,
        run_dir: Path,
    ) -> Path | None:
        """Find and attach Claude log to run directory."""
        return self._log_artifacts.attach_claude_log(run_dir)

    def write_orchestrator_tail(  # noqa: C901
        self,
        run_dir: Path,
        log_path: Path,
        issue_number: int,
        session_name: str,
        max_lines: int = 400,
    ) -> Path | None:
        """Write filtered orchestrator log tail to run directory."""
        return self._log_artifacts.write_orchestrator_tail(
            run_dir,
            log_path,
            issue_number,
            session_name,
            max_lines=max_lines,
        )

    # -------------------------------------------------------------------------
    # Private Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _run_timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _run_dir_name(session_name: str, run_id: str) -> str:
        return f"{run_id}__{session_name}"

    @staticmethod
    def _session_name_from_run_dir(name: str) -> str | None:
        if "__" not in name:
            return name
        return name.split("__", 1)[1]

    def _ensure_base_dir(self, worktree_path: Path) -> Path:
        base_dir = self.sessions_base_dir(worktree_path)
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir

    @staticmethod
    def _ensure_symlink(symlink_path: Path, target: Path) -> None:
        try:
            if symlink_path.is_symlink() or symlink_path.exists():
                if symlink_path.resolve() == target.resolve():
                    return
                if symlink_path.is_dir() and not symlink_path.is_symlink():
                    return
                symlink_path.unlink()
            symlink_path.symlink_to(target, target_is_directory=True)
        except OSError:
            return

    def _update_latest(self, worktree_path: Path, manifest: dict[str, Any]) -> None:
        payload = {
            "session_name": manifest.get("session_name"),
            "run_id": manifest.get("run_id"),
            "started_at": manifest.get("started_at"),
            "issue_number": manifest.get("issue_number"),
            "run_dir": manifest.get("run_dir"),
            "log_path": manifest.get("log_path"),
        }
        latest_path = self.sessions_base_dir(worktree_path) / LATEST_NAME
        self._write_json(latest_path, payload)
        self._write_json(
            worktree_path / ".issue-orchestrator" / ROOT_LATEST_NAME, payload
        )

    def _load_latest(self, worktree_path: Path) -> dict[str, Any] | None:
        latest_path = self.sessions_base_dir(worktree_path) / LATEST_NAME
        return self._read_json(latest_path)

    def _append_index(self, worktree_path: Path, manifest: dict[str, Any]) -> None:
        index_path = self.sessions_base_dir(worktree_path) / INDEX_NAME
        index = self._read_json(index_path) or {"runs": []}
        index["runs"].append(
            {
                "session_name": manifest.get("session_name"),
                "run_id": manifest.get("run_id"),
                "started_at": manifest.get("started_at"),
                "issue_number": manifest.get("issue_number"),
                "run_dir": manifest.get("run_dir"),
                "backend": manifest.get("backend"),
                "agent_label": manifest.get("agent_label"),
            }
        )
        self._write_json(index_path, index)

    def _prune_index(self, worktree_path: Path, removed: list[Path]) -> None:
        index_path = self.sessions_base_dir(worktree_path) / INDEX_NAME
        index = self._read_json(index_path)
        if not index or "runs" not in index:
            return
        removed_set = {str(p) for p in removed}
        index["runs"] = [
            r for r in index["runs"] if r.get("run_dir") not in removed_set
        ]
        self._write_json(index_path, index)

    def _refresh_latest(self, worktree_path: Path) -> None:
        base_dir = self.sessions_base_dir(worktree_path)
        if not base_dir.exists():
            return

        runs = sorted(
            [d for d in base_dir.iterdir() if d.is_dir() and not d.is_symlink()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not runs:
            return

        latest_run = runs[0]
        manifest = self.read_manifest(latest_run)
        if manifest:
            self._update_latest(worktree_path, manifest)
            session_name = manifest.get("session_name")
            if session_name:
                self._ensure_symlink(base_dir / session_name, latest_run)

    def attach_claude_log_for_run(self, run_dir: Path) -> Path | None:
        """Attach the canonical Claude JSONL artifact for a run and return its path."""
        with self._io_lock:
            return self._log_artifacts.attach_claude_log_for_run(run_dir)

    # -------------------------------------------------------------------------
    # Utility Methods (for finding run dirs by issue, etc.)
    # -------------------------------------------------------------------------

    def find_run_dir_for_issue(
        self, worktree_path: Path, issue_number: int
    ) -> Path | None:
        """Find the most recent run dir for an issue inside a worktree."""
        base_dir = self.sessions_base_dir(worktree_path)
        if not base_dir.exists():
            return None

        suffixes = (
            f"__issue-{issue_number}",
            f"__review-{issue_number}",
            f"__rework-{issue_number}",
        )
        candidates: list[Path] = []

        for run_dir in base_dir.iterdir():
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            name = run_dir.name
            if name.endswith(suffixes):
                candidates.append(run_dir)
                continue
            manifest = self.read_manifest(run_dir)
            if manifest and manifest.get("issue_number") == issue_number:
                candidates.append(run_dir)

        if not candidates:
            return None

        return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]

    def session_name_from_path(self, rel_path: str | None) -> str | None:
        """Extract session name from a path containing sessions directory."""
        if not rel_path:
            return None
        parts = Path(rel_path).parts
        try:
            idx = parts.index(SESSION_OUTPUT_DIR)
        except ValueError:
            return None
        if idx + 1 >= len(parts):
            return None
        run_dir_name = parts[idx + 1]
        return self._session_name_from_run_dir(run_dir_name)

    def ensure_claude_log_attached(
        self,
        worktree_path: Path,
        session_name: str | None = None,
    ) -> Path | None:
        """Ensure Claude log is attached to the run directory.

        Finds the run directory first, then attaches the Claude log.

        Args:
            worktree_path: Path to the worktree
            session_name: Optional session name to find

        Returns:
            Path to Claude log, or None if not found
        """
        return self._log_artifacts.ensure_claude_log_attached(
            worktree_path,
            session_name=session_name,
        )

    def find_latest_session_log_path(self, worktree_path: Path) -> Path | None:
        """Find the most recently updated session log in a worktree.

        Args:
            worktree_path: Path to the worktree

        Returns:
            Path to session log, or None if not found
        """
        return self._log_artifacts.find_latest_session_log_path(worktree_path)

    @staticmethod
    def _ensure_manifest_artifact(
        manifest: dict[str, Any],
        *,
        name: str,
        kind: str,
        path: str | None,
        content_type: str | None = None,
    ) -> None:
        if not path:
            return
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            artifacts = {}
            manifest["artifacts"] = artifacts
        artifact: dict[str, Any] = {"kind": kind, "path": path}
        if content_type:
            artifact["content_type"] = content_type
        artifacts[name] = artifact

    def _sync_manifest_artifacts(self, manifest: dict[str, Any]) -> None:
        """Derive canonical artifact entries from known manifest path fields."""
        self._ensure_manifest_artifact(
            manifest,
            name="ui_log",
            kind="session_log",
            path=manifest.get("log_path"),
            content_type="text/plain",
        )
        self._ensure_manifest_artifact(
            manifest,
            name="agent_log",
            kind="session_log",
            path=manifest.get("log_path"),
            content_type="text/plain",
        )
        self._ensure_manifest_artifact(
            manifest,
            name="prompt",
            kind="session_prompt",
            path=manifest.get("session_prompt_path"),
            content_type="text/plain",
        )
        self._ensure_manifest_artifact(
            manifest,
            name="completion_record",
            kind="completion_record",
            path=manifest.get("completion_record_path"),
            content_type="application/json",
        )
        self._ensure_manifest_artifact(
            manifest,
            name="validation_record",
            kind="validation_record",
            path=manifest.get("validation_record_path"),
            content_type="application/json",
        )
        self._ensure_manifest_artifact(
            manifest,
            name="validation_stdout",
            kind="validation_stdout",
            path=manifest.get("validation_stdout"),
            content_type="text/plain",
        )
        self._ensure_manifest_artifact(
            manifest,
            name="validation_stderr",
            kind="validation_stderr",
            path=manifest.get("validation_stderr"),
            content_type="text/plain",
        )
        self._ensure_manifest_artifact(
            manifest,
            name="diagnostic",
            kind="diagnostic",
            path=manifest.get("diagnostic_path"),
            content_type="application/json",
        )
        self._ensure_manifest_artifact(
            manifest,
            name="claude_log",
            kind="claude_jsonl",
            path=manifest.get("claude_log_path"),
            content_type="application/json",
        )
        self._ensure_manifest_artifact(
            manifest,
            name="orchestrator_tail",
            kind="orchestrator_tail",
            path=manifest.get("orchestrator_tail"),
            content_type="text/plain",
        )
        self._ensure_manifest_artifact(
            manifest,
            name="review_exchange_summary",
            kind="review_exchange_summary",
            path=manifest.get("review_exchange_summary_path"),
            content_type="application/json",
        )
        self._ensure_manifest_artifact(
            manifest,
            name="review_exchange_transcript",
            kind="review_exchange_transcript",
            path=manifest.get("review_exchange_transcript_path"),
            content_type="text/plain",
        )

    def _bootstrap_manifest_identity(
        self, run_dir: Path, manifest: dict[str, Any]
    ) -> None:
        """Fill required manifest identity fields for standalone or legacy paths."""
        run_name = run_dir.name
        terminal_recording_path = run_dir / TERMINAL_RECORDING_NAME
        run_id = manifest.get("run_id")
        session_name = manifest.get("session_name")
        if "__" in run_name:
            parsed_run_id, parsed_session_name = run_name.split("__", 1)
            run_id = run_id or parsed_run_id
            session_name = session_name or parsed_session_name
        manifest.setdefault("run_id", run_id or run_name)
        manifest.setdefault("session_name", session_name or run_name)
        manifest.setdefault("run_dir", str(run_dir))
        manifest.setdefault("log_path", str(terminal_recording_path))
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            artifacts = {}
            manifest["artifacts"] = artifacts
        artifacts.setdefault(
            "terminal_recording",
            {
                "kind": "terminal_recording",
                "path": str(terminal_recording_path),
                "content_type": "application/x-ndjson",
            },
        )
        if not terminal_recording_path.exists():
            terminal_recording_path.write_text("", encoding="utf-8")


# -----------------------------------------------------------------------------
# Module-level utility functions
# -----------------------------------------------------------------------------


def find_run_dir_for_issue(
    worktree_bases: list[Path],
    repo_name: str,
    issue_number: int,
) -> tuple[Path | None, Path | None]:
    """Locate the latest run dir and worktree path for an issue.

    Searches across multiple worktree bases to find the most recent
    session run directory for a given issue.

    Args:
        worktree_bases: List of directories to search for worktrees
        repo_name: Repository name for worktree naming convention
        issue_number: Issue number to find

    Returns:
        Tuple of (run_dir, worktree_path), or (None, None) if not found
    """
    session_output = FileSystemSessionOutput()
    seen: set[Path] = set()

    for base in worktree_bases:
        if not base or not base.exists():
            continue
        base = base.resolve()
        if base in seen:
            continue
        seen.add(base)

        # Try direct match first
        candidate = base / f"{repo_name}-{issue_number}"
        if candidate.exists():
            run_dir = session_output.find_run_dir_for_issue(candidate, issue_number)
            if run_dir:
                return run_dir, candidate

        # Search all worktrees in this base
        for worktree_path in base.glob(f"{repo_name}-*"):
            if not worktree_path.is_dir():
                continue
            run_dir = session_output.find_run_dir_for_issue(worktree_path, issue_number)
            if run_dir:
                return run_dir, worktree_path

    return None, None


def session_output_dir(worktree_path: Path, session_name: str) -> Path:
    """Return the stable session directory (symlink to latest run).

    This is a convenience function for constructing session directory paths.

    Args:
        worktree_path: Path to the worktree
        session_name: Name of the session

    Returns:
        Path to the session directory
    """
    return worktree_path / ".issue-orchestrator" / SESSION_OUTPUT_DIR / session_name
