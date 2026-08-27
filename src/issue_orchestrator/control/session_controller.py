"""Session lifecycle controller.

This controller makes decisions about session outcomes based on:
1. Observations (session running, terminated, timed out)
2. Completion records (completion.json written by coding-done/reviewer-done)
3. Validation gate (optional post-completion validation)

Key principle: completion.json is the source of truth for agent intent.
The observer reports facts; this controller decides outcomes.

Example flows:
- Session terminated + completion.json exists -> process completion record
- Session terminated + no completion.json -> FAILED
- Session timed out + completion.json exists -> recover work, process completion
- Session timed out + no completion.json -> TIMED_OUT
- Completion processed + validation configured -> run validation gate
- Validation failed -> VALIDATION_FAILED
"""

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .completion_processor import CompletionProcessor, ProcessingResult
    from ..ports.command_runner import CommandRunner
    from ..ports.working_copy import WorkingCopy
    from ..domain.models import CompletionRecord
    from ..domain.attempt import AttemptKey
    from ..domain.issue_key import IssueKey
    from ..domain.session_key import TaskKind
    from ..ports.attempt_store import AttemptStore
    from ..ports.validation_attempt_key_factory import ValidationAttemptKeyFactory

from ..events import EventName
from ..domain.dirty_remediation import remediation_prompt_steps
from ..domain.artifact_contracts import (
    ValidationFailed,
    ValidationOutcome,
    ValidationPassed,
    ValidationRetry,
)
from ..domain.completion_finalization import (
    CompletionFinalizationDecision,
    CompletionRuntimeState,
)
from ..domain.models import SessionStatus, CompletionOutcome
from ..domain.session_run import SessionRunAssets
from ..ports.provider_resilience import ProviderErrorType
from ..infra.provider_resilience import ProviderStatus, read_provider_status
from ..infra.logging_config import issue_log
from ..infra.validation_state import (
    DEFAULT_RETRY_TEMPLATE,
    _truncate_with_tail,
)
from ..observation.observation import SessionObservation, SessionObservationResult
from ..ports import EventSink, make_trace_event
from ..ports.run_evidence import (
    NullValidationEvidenceRecorder,
    ValidationEvidenceRecorder,
)
from ..ports.session_output import SessionOutput, ValidationRecord, ValidationState
from .completion_types import REVIEW_EXCHANGE_ERROR_PREFIX
from .completion_record_validation import CompletionRecordLoadResult
from .absent_completion_record import (
    collect_completion_debug_context,
    log_completion_debug_context,
    write_no_completion_diagnostic,
)
from .invalid_completion_record import report_invalid_completion_record
from .review_exchange_contracts import ReviewExchangeCanceller
from .session_decision import (
    ProviderAuthOutcome,
    SessionDecision,
    provider_failure_from_status,
    provider_quota_failure_from_status,
    provider_success_from_status,
)
from .session_run_resolution import resolve_run_assets
from .validation import PublishGate

logger = logging.getLogger(__name__)


class ValidationFailureKind(str, Enum):
    """Typed reason for validation retry/exhaustion routing."""

    VALIDATION_COMMAND = "validation_command"
    DIRTY_BEFORE_VALIDATION = "dirty_before_validation"
    DIRTY_AFTER_VALIDATION = "dirty_after_validation"


@dataclass(frozen=True)
class ValidationFailureContext:
    worktree_path: Path
    run_dir: Path
    session_name: str
    issue_number: int
    issue_title: str
    retry_count: int
    error: str | None
    error_file: Path | None
    original_prompt: str | None
    retry_prompt_template: str | None
    repo_root: Path | None
    failure_kind: ValidationFailureKind
    dirty_files: tuple[str, ...]


@dataclass(frozen=True)
class SessionFinalizationContext:
    record: "CompletionRecord"
    worktree_path: Path
    issue_number: int
    issue_title: str
    session_name: str
    run_assets: SessionRunAssets
    validation_retry_count: int
    original_prompt: str | None
    retry_prompt_template: str | None
    repo_root: Path | None
    recovered: bool


@dataclass(frozen=True)
class ValidationGateDecision:
    status: SessionStatus
    passed: bool
    error: str | None
    error_file: Path | None


class SessionController:
    """Controller that decides session outcomes.

    Uses observations from SessionObserver and completion records from
    CompletionProcessor to determine the true outcome of a session.

    The key insight: a session may time out but still have completed work.
    If completion.json exists with outcome=completed, we should process it
    regardless of whether the session timed out or exited cleanly.

    Optionally runs a validation gate after completion processing.
    """

    def __init__(
        self,
        completion_processor: "CompletionProcessor",
        events: EventSink,
        session_output: SessionOutput,
        working_copy: "WorkingCopy",
        command_runner: Optional["CommandRunner"] = None,
        validation_cmd: Optional[str] = None,
        validation_timeout_seconds: int = 300,
        validation_junit_xml_paths: tuple[str, ...] | list[str] = (),
        validation_evidence_recorder: ValidationEvidenceRecorder | None = None,
        attempt_store: "AttemptStore | None" = None,
        validation_attempt_key_factory: "ValidationAttemptKeyFactory | None" = None,
        max_validation_retries: int = 0,
        review_exchange_canceller: ReviewExchangeCanceller | None = None,
    ):
        """Initialize the controller.

        Args:
            completion_processor: For reading/processing completion records
            events: For emitting trace events
            session_output: For session artifact storage
            working_copy: For git operations (required for validation cache)
            command_runner: For running validation commands (optional)
            validation_cmd: Validation command to run after completion (optional)
            validation_timeout_seconds: Timeout for validation command
            validation_junit_xml_paths: Report paths/globs emitted by validation
            validation_evidence_recorder: Owner for run-scoped validation evidence
            attempt_store: Owner for cross-run attempt-scoped validation facts
            validation_attempt_key_factory: Builds the issue-at-HEAD cache key
            max_validation_retries: Maximum number of validation retries (0 = no retries)
        """
        self.completion_processor = completion_processor
        self.events = events
        self.session_output = session_output
        self._working_copy = working_copy
        self._command_runner = command_runner
        self._max_validation_retries = max_validation_retries
        self._validation_cmd = validation_cmd
        self._validation_timeout = validation_timeout_seconds
        self._validation_junit_xml_paths = tuple(validation_junit_xml_paths)
        self._validation_evidence_recorder = (
            validation_evidence_recorder
            if validation_evidence_recorder is not None
            else NullValidationEvidenceRecorder()
        )
        self._attempt_store = attempt_store
        self._validation_attempt_key_factory = validation_attempt_key_factory
        self._review_exchange_canceller = review_exchange_canceller

    def decide_outcome(
        self,
        observation: SessionObservationResult,
        worktree_path: Path,
        issue_number: int,
        issue_title: str,
        session_name: str,
        completion_path: str | None = None,
        *,
        session_run_assets: SessionRunAssets,
        validation_retry_count: int = 0,
        original_prompt: str | None = None,
        retry_prompt_template: str | None = None,
        repo_root: Path | None = None,
        issue_key: "IssueKey | None" = None,
        task_kind: "TaskKind | None" = None,
    ) -> SessionDecision:
        """Decide the outcome of a session based on observation + completion.json.

        This is the core decision logic. For ANY non-running session, we check
        completion.json to determine the true outcome.

        ``task_kind`` is the originating session's task. Review-only tasks make
        no commits and publish nothing, so the code validation-retry gate does
        not apply to them (see ``_run_validation_phase_if_needed``).
        """
        # If still running, nothing to decide
        if observation.observation == SessionObservation.RUNNING:
            logger.debug(
                issue_log(issue_number, "Session still running: session=%s"),
                session_name,
            )
            return SessionDecision(
                status=SessionStatus.RUNNING, reason="Session still running"
            )

        completion_session_name = self.session_output.session_name_from_path(
            completion_path
        )
        validation_session_name = completion_session_name or session_name
        run_assets = self._resolve_run_assets(
            session_name,
            session_run_assets=session_run_assets,
        )
        run_dir = run_assets.run_dir
        provider_status = self._read_provider_status(run_dir)
        provider_success = provider_success_from_status(provider_status)

        # Log and look up completion record
        self._log_completion_lookup(
            worktree_path, issue_number, session_name, completion_path
        )
        load_result = self.completion_processor.read_completion_record_result(
            worktree_path, completion_path
        )
        record = load_result.record

        if record is None:
            # An unauthenticated provider outranks every absent-record outcome —
            # notably TIMED_OUT, which would mint a substance investigation for
            # a credential problem (#6999 F4). It does NOT outrank a record that
            # exists: completion.json is the agent's reported intent, and a
            # session that finished its work must never have that work discarded
            # because the credential expired afterwards.
            if observation.observation == SessionObservation.PROVIDER_AUTH_FAILED:
                return self._provider_auth_failed_decision(
                    observation, issue_number, session_name
                )
            decision = self._handle_absent_completion_record(
                observation=observation,
                worktree_path=worktree_path,
                issue_number=issue_number,
                session_name=session_name,
                run_dir=run_dir,
                completion_path=completion_path,
                load_result=load_result,
                provider_status=provider_status,
            )
            return replace(decision, provider_success=provider_success)

        # Recover completed work from timed-out sessions when possible.
        recovered = observation.observation == SessionObservation.TIMED_OUT
        if recovered:
            self._log_timeout_recovery(issue_number, session_name, record)

        finalization_decision = self._handle_completion_finalization_preconditions(
            SessionFinalizationContext(
                record=record,
                worktree_path=worktree_path,
                issue_number=issue_number,
                issue_title=issue_title,
                session_name=validation_session_name,
                run_assets=run_assets,
                validation_retry_count=validation_retry_count,
                original_prompt=original_prompt,
                retry_prompt_template=retry_prompt_template,
                repo_root=repo_root,
                recovered=recovered,
            )
        )
        if finalization_decision is not None:
            return replace(finalization_decision, provider_success=provider_success)

        # Process completion record
        pr_number = self._extract_pr_number_from_session_name(session_name)
        result = self.completion_processor.process(
            worktree_path,
            issue_number,
            issue_title,
            pr_number=pr_number,
            completion_path=completion_path,
            run_assets=run_assets,
        )
        deferred_decision = self._deferred_review_exchange_decision(
            result=result,
            run_dir=run_dir,
            session_name=validation_session_name,
            issue_number=issue_number,
            recovered=recovered,
        )
        if deferred_decision is not None:
            return replace(deferred_decision, provider_success=provider_success)
        self._emit_processing_completed_event(
            worktree_path, issue_number, session_name, run_dir, result
        )

        # Map outcome to status
        status = self._map_outcome_to_status(record)
        if result.failure_kind == "validation_failed":
            status = self._handle_pre_publish_validation_failure(
                run_dir=run_dir,
                session_name=validation_session_name,
                issue_number=issue_number,
                validation_reason=result.message,
            )
        blocked_reason = (
            record.blocked_reason if status == SessionStatus.BLOCKED else None
        )

        # Run validation if configured
        validation_passed, validation_error, validation_error_file = None, None, None
        validation_decision = self._run_validation_phase_if_needed(
            status=status,
            worktree_path=worktree_path,
            session_name=validation_session_name,
            run_dir=run_dir,
            issue_number=issue_number,
            issue_title=issue_title,
            outcome=record.outcome,
            validation_retry_count=validation_retry_count,
            original_prompt=original_prompt,
            retry_prompt_template=retry_prompt_template,
            repo_root=repo_root,
            issue_key=issue_key,
            task_kind=task_kind,
        )
        if validation_decision is not None:
            status = validation_decision.status
            validation_passed = validation_decision.passed
            validation_error = validation_decision.error
            validation_error_file = validation_decision.error_file

        # Enrich manifest with CompletionRecord detail
        self._enrich_manifest_from_completion(run_dir, record)

        # Build completion detail for trace event enrichment
        completion_detail = self._build_completion_detail(record)
        if result.completion_record_path:
            completion_detail["completion_path_absolute"] = (
                result.completion_record_path
            )

        # Log completion summary
        self._log_session_completion(
            issue_number, session_name, status, record, result, recovered
        )

        return SessionDecision(
            status=status,
            processing_result=result,
            completion_processed=True,
            recovered_from_timeout=recovered,
            reason=f"Processed completion record with outcome: {record.outcome.value}",
            validation_passed=validation_passed,
            validation_error=validation_error,
            validation_error_file=validation_error_file,
            blocked_reason=blocked_reason,
            completion_detail=completion_detail,
            provider_success=provider_success,
        )

    def _provider_auth_failed_decision(
        self,
        observation: SessionObservationResult,
        issue_number: int,
        session_name: str,
    ) -> SessionDecision:
        """Terminate an auth-dead session on its own, non-timeout outcome.

        The single announcement point for a live session killed by a credential
        outage. The observer that spotted it publishes nothing — it gathers
        facts — so this event fires exactly once per terminated session, and it
        is the *terminated* event rather than the launch-gate's parking one:
        this session did launch (#6999 F5).
        """
        outcome = ProviderAuthOutcome.from_readiness(observation.provider_readiness)
        logger.error(
            issue_log(
                issue_number,
                "SESSION COMPLETE: status=BLOCKED outcome=none "
                "reason=provider_auth_failed session=%s provider=%s detail=%s",
            ),
            session_name,
            outcome.provider,
            outcome.detail,
        )
        self._emit_event(
            EventName.SESSION_PROVIDER_AUTH_TERMINATED,
            outcome.event_payload(issue_number, session_name),
        )
        return outcome.as_decision()

    def _handle_absent_completion_record(
        self,
        *,
        observation: SessionObservationResult,
        worktree_path: Path,
        issue_number: int,
        session_name: str,
        run_dir: Path,
        completion_path: str | None,
        load_result: CompletionRecordLoadResult,
        provider_status: ProviderStatus | None,
    ) -> SessionDecision:
        """Route absent completion state without collapsing invalid into missing."""
        if load_result.invalid:
            return self._handle_invalid_completion_record(
                observation=observation,
                worktree_path=worktree_path,
                issue_number=issue_number,
                session_name=session_name,
                run_dir=run_dir,
                completion_path=completion_path,
                load_result=load_result,
            )
        return self._handle_no_completion_record(
            observation,
            worktree_path,
            issue_number,
            session_name,
            run_dir,
            completion_path,
            provider_status,
        )

    def _handle_completion_finalization_preconditions(
        self,
        context: SessionFinalizationContext,
    ) -> SessionDecision | None:
        has_validation = bool(self._validation_cmd and self._command_runner)
        finalization_plan = self.completion_processor.completion_finalization_plan(
            issue_number=context.issue_number,
            session_name=context.session_name,
            run_id=context.run_assets.run_id,
            outcome=context.record.outcome,
            requested_actions=tuple(context.record.requested_actions),
            runtime_state=(
                CompletionRuntimeState.TIMED_OUT
                if context.recovered
                else CompletionRuntimeState.TERMINATED
            ),
            validation_preflight_configured=has_validation,
        )
        if (
            finalization_plan.decision
            is CompletionFinalizationDecision.TERMINAL_REVIEW_EXCHANGE_TIMEOUT
        ):
            return self._terminal_review_exchange_timeout_decision(
                result=self.completion_processor.deferred_review_exchange_result(),
                session_name=context.session_name,
                issue_number=context.issue_number,
                timeout_reason=finalization_plan.reason,
            )

        if (
            finalization_plan.decision
            is CompletionFinalizationDecision.DEFER_REVIEW_EXCHANGE
        ):
            return self._deferred_review_exchange_decision(
                result=self.completion_processor.deferred_review_exchange_result(),
                run_dir=context.run_assets.run_dir,
                session_name=context.session_name,
                issue_number=context.issue_number,
                recovered=False,
            )

        if (
            finalization_plan.decision
            is CompletionFinalizationDecision.RUN_DIRTY_PREFLIGHT
        ):
            return self._run_dirty_preflight_before_validation(
                record=context.record,
                worktree_path=context.worktree_path,
                issue_number=context.issue_number,
                issue_title=context.issue_title,
                session_name=context.session_name,
                run_dir=context.run_assets.run_dir,
                validation_retry_count=context.validation_retry_count,
                original_prompt=context.original_prompt,
                retry_prompt_template=context.retry_prompt_template,
                repo_root=context.repo_root,
                recovered=context.recovered,
            )
        if finalization_plan.decision is CompletionFinalizationDecision.PROCESS:
            return None
        raise AssertionError(
            f"Unhandled completion finalization decision: {finalization_plan.decision}"
        )

    def _run_dirty_preflight_before_validation(
        self,
        *,
        record: "CompletionRecord",
        worktree_path: Path,
        issue_number: int,
        issue_title: str,
        session_name: str,
        run_dir: Path,
        validation_retry_count: int,
        original_prompt: str | None,
        retry_prompt_template: str | None,
        repo_root: Path | None,
        recovered: bool,
    ) -> SessionDecision | None:
        """Return a validation retry decision if push preconditions are dirty.

        The completion-finalization matrix decides whether this phase should
        run. This method only executes the dirty policy and routes failures.
        """
        dirty_policy = self.completion_processor.check_dirty_policy(worktree_path)
        if dirty_policy.ok:
            logger.info(
                issue_log(
                    issue_number,
                    "Dirty preflight passed before validation: session=%s worktree=%s",
                ),
                session_name,
                worktree_path,
            )
            return None

        validation_error = (
            "Validation blocked before running command because the worktree "
            f"is not publishable: {dirty_policy.reason}"
        )
        logger.warning(
            issue_log(
                issue_number,
                "Dirty preflight failed before validation: session=%s error=%s",
            ),
            session_name,
            validation_error,
        )

        failure_run_dir = run_dir
        self._record_validation_failure_manifest(
            run_dir=failure_run_dir,
            outcome=record.outcome,
            retry_count=validation_retry_count,
            validation_error=validation_error,
        )
        status = self._route_validation_failure(
            ValidationFailureContext(
                worktree_path=worktree_path,
                run_dir=failure_run_dir,
                session_name=session_name,
                issue_number=issue_number,
                issue_title=issue_title,
                retry_count=validation_retry_count,
                error=validation_error,
                error_file=None,
                original_prompt=original_prompt,
                retry_prompt_template=retry_prompt_template,
                repo_root=repo_root,
                failure_kind=ValidationFailureKind.DIRTY_BEFORE_VALIDATION,
                dirty_files=dirty_policy.blocking_paths,
            )
        )

        return SessionDecision(
            status=status,
            completion_processed=False,
            recovered_from_timeout=recovered,
            reason=validation_error,
            validation_passed=False,
            validation_error=validation_error,
            validation_error_file=None,
        )

    def _deferred_review_exchange_decision(
        self,
        *,
        result: "ProcessingResult",
        run_dir: Path,
        session_name: str,
        issue_number: int,
        recovered: bool,
    ) -> SessionDecision | None:
        if not result.review_exchange_deferred:
            return None

        if recovered:
            return self._terminal_review_exchange_timeout_decision(
                result=result,
                session_name=session_name,
                issue_number=issue_number,
                timeout_reason="Session timed out while review exchange was still running",
            )

        # Exchange is running in a background thread. Keep the session active
        # so the next tick re-observes, re-enters the pipeline, and resumes
        # publish once the summary is visible. Do not emit processing_completed:
        # the record is still pending.
        if result.validation_failed_rerouted:
            self._emit_pre_publish_validation_failure(
                run_dir=run_dir,
                session_name=session_name,
                issue_number=issue_number,
                validation_reason=result.message,
            )
        return SessionDecision(
            status=SessionStatus.RUNNING,
            processing_result=result,
            completion_processed=False,
            recovered_from_timeout=recovered,
            reason="Review exchange running in background; awaiting completion",
        )

    def _terminal_review_exchange_timeout_decision(
        self,
        *,
        result: "ProcessingResult",
        session_name: str,
        issue_number: int,
        timeout_reason: str,
    ) -> SessionDecision:
        errors = list(result.errors or [])
        errors.append(f"{REVIEW_EXCHANGE_ERROR_PREFIX} {timeout_reason}")
        cancel_error = self._cancel_deferred_review_exchange(
            issue_number=issue_number,
            session_name=session_name,
            reason="session-timeout",
        )
        if cancel_error:
            errors.append(f"{REVIEW_EXCHANGE_ERROR_PREFIX} {cancel_error}")
        # Timeout is an issue-lifetime boundary: once the visible coding run
        # is terminal, the hidden review-exchange pair/job must be terminal
        # too. Diagnostics already written to the run directory are preserved;
        # live subprocesses are not.
        halted_result = replace(
            result,
            errors=errors,
            review_exchange_deferred=False,
            review_exchange_halted=True,
        )
        return SessionDecision(
            status=SessionStatus.TIMED_OUT,
            processing_result=halted_result,
            completion_processed=True,
            recovered_from_timeout=True,
            reason=timeout_reason,
        )

    def _cancel_deferred_review_exchange(
        self,
        *,
        issue_number: int,
        session_name: str,
        reason: str,
    ) -> str | None:
        if self._review_exchange_canceller is None:
            logger.warning(
                "[REVIEW_EXCHANGE] no canceller configured for terminal "
                "deferred exchange issue=%d session=%s reason=%s",
                issue_number,
                session_name,
                reason,
            )
            return None
        try:
            cancellation = self._review_exchange_canceller(issue_number, reason)
        except Exception as exc:  # noqa: BLE001 - terminal path must still surface timeout
            logger.exception(
                "[REVIEW_EXCHANGE] failed to cancel terminal deferred exchange "
                "issue=%d session=%s reason=%s",
                issue_number,
                session_name,
                reason,
            )
            return f"failed to cancel runtime work: {exc}"
        cancelled_jobs = cancellation.cancelled_job_ids
        logger.info(
            "[REVIEW_EXCHANGE] cancelled terminal deferred exchange "
            "issue=%d session=%s reason=%s jobs=%s",
            issue_number,
            session_name,
            reason,
            ",".join(cancelled_jobs) if cancelled_jobs else "none",
        )
        return None

    def _handle_pre_publish_validation_failure(
        self,
        *,
        run_dir: Path,
        session_name: str,
        issue_number: int,
        validation_reason: str,
    ) -> SessionStatus:
        self._emit_pre_publish_validation_failure(
            run_dir=run_dir,
            session_name=session_name,
            issue_number=issue_number,
            validation_reason=validation_reason,
        )
        return SessionStatus.VALIDATION_FAILED

    def _run_validation_phase_if_needed(
        self,
        *,
        status: SessionStatus,
        worktree_path: Path,
        session_name: str,
        run_dir: Path,
        issue_number: int,
        issue_title: str,
        outcome: CompletionOutcome,
        validation_retry_count: int,
        original_prompt: str | None,
        retry_prompt_template: str | None,
        repo_root: Path | None,
        issue_key: "IssueKey | None",
        task_kind: "TaskKind | None" = None,
    ) -> ValidationGateDecision | None:
        if not (
            status == SessionStatus.COMPLETED
            and self._validation_cmd
            and self._command_runner
        ):
            return None
        # Review-only sessions (PR review / retrospective review) make no commits
        # and publish nothing, so the code validation-retry gate does not apply.
        # Running it relaunches the work as a coder retry that ultimately tries
        # to open a PR on an empty branch (see issue #6426).
        if task_kind is not None and task_kind.is_review_only:
            logger.debug(issue_log(issue_number, "Skipping code validation gate: review-only session"))
            return None
        return self._run_validation_gate(
            worktree_path,
            session_name,
            run_dir,
            issue_number,
            issue_title,
            outcome,
            validation_retry_count,
            original_prompt,
            retry_prompt_template,
            repo_root,
            issue_key,
        )

    def _emit_pre_publish_validation_failure(
        self,
        *,
        run_dir: Path,
        session_name: str,
        issue_number: int,
        validation_reason: str,
    ) -> None:
        self._emit_event(
            EventName.SESSION_VALIDATION_FAILED,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "validation_cmd": "pre_publish_hook",
                "run_dir": str(run_dir),
                "validation_reason": validation_reason,
                "artifacts": self._validation_record_artifacts(run_dir),
            },
        )

    def _log_completion_lookup(
        self,
        worktree_path: Path,
        issue_number: int,
        session_name: str,
        completion_path: str | None,
    ) -> None:
        """Log completion record lookup details."""
        full_path = (
            (worktree_path / completion_path).resolve()
            if completion_path
            else (worktree_path / ".issue-orchestrator/completion.json").resolve()
        )
        logger.info(
            issue_log(
                issue_number, "Session not running: session=%s checking_completion=%s"
            ),
            session_name,
            completion_path or ".issue-orchestrator/completion.json",
        )
        self._emit_event(
            EventName.COMPLETION_LOOKUP,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "worktree_path": str(worktree_path.resolve()),
                "completion_path": completion_path,
                "full_path": str(full_path),
                "file_exists": full_path.exists(),
            },
        )
        exists = full_path.exists()
        size = full_path.stat().st_size if exists else None
        logger.info(
            issue_log(issue_number, "Completion lookup: exists=%s size=%s path=%s"),
            exists,
            size,
            full_path,
        )

    def _handle_no_completion_record(
        self,
        observation: SessionObservationResult,
        worktree_path: Path,
        issue_number: int,
        session_name: str,
        run_dir: Path,
        completion_path: str | None,
        provider_status: ProviderStatus | None,
    ) -> SessionDecision:
        """Handle case where no completion record exists."""
        debug_context = collect_completion_debug_context(
            worktree_path=worktree_path,
            run_dir=run_dir,
            completion_path=completion_path,
        )
        write_no_completion_diagnostic(
            observation=observation,
            worktree_path=worktree_path,
            issue_number=issue_number,
            session_name=session_name,
            run_dir=run_dir,
            completion_path=completion_path,
            session_output=self.session_output,
            debug_context=debug_context,
        )
        session_log = self._get_session_log_tail(run_dir, session_name)

        payload = {
            "issue_number": issue_number,
            "session_name": session_name,
            "observation": observation.observation.value,
            "last_output": session_log[-500:] if session_log else "",
        }
        if run_dir:
            payload["run_dir"] = str(run_dir)
        self._emit_event(EventName.SESSION_NO_COMPLETION_RECORD, payload)

        if (
            provider_status
            and provider_status.error_type is ProviderErrorType.QUOTA
            and not provider_status.succeeded
        ):
            # Tested before the transient branch, and before the TIMED_OUT
            # fall-through below, because an exhausted account fails the way an
            # expired credential does: it renders a banner and waits out the
            # wall clock. Without a typed verdict here the session is recorded
            # as a timeout — the classification under which cleanup is entitled
            # to treat an unpushed worktree as abandoned work (#7096).
            quota_failure = provider_quota_failure_from_status(provider_status)
            return SessionDecision(
                status=SessionStatus.BLOCKED,
                reason="Provider quota exhausted",
                blocked_reason=quota_failure.error_summary,
                provider_error_type=ProviderErrorType.QUOTA,
                provider_quota_failure=quota_failure,
            )

        if (
            provider_status
            and provider_status.error_type == ProviderErrorType.TRANSIENT
            and not provider_status.succeeded
        ):
            return SessionDecision(
                status=SessionStatus.BLOCKED,
                reason="Provider unavailable",
                # Same rule as the auth path: the provider-blocked label is only
                # ever moved by the provider-impact owner, so it and the durable
                # record stay one transition. The typed verdict is what routes
                # it there (#5980 F1 / #6999 F5).
                blocked_reason=provider_status.last_error_summary
                or "Provider unavailable",
                provider_error_type=ProviderErrorType.TRANSIENT,
                provider_transient_failure=provider_failure_from_status(provider_status),
            )

        if observation.observation == SessionObservation.TIMED_OUT:
            logger.warning(
                issue_log(
                    issue_number,
                    "SESSION COMPLETE: status=TIMED_OUT outcome=none "
                    "reason=no_completion_record session=%s expected_completion=%s "
                    "marker_exists=%s nearby_completion_files=%d",
                ),
                session_name,
                debug_context["requested_completion_abs_path"],
                debug_context["agent_done_marker_exists"],
                len(debug_context["nearby_completion_candidates"]),
            )
            log_completion_debug_context(
                issue_number, session_name, debug_context
            )
            if session_log:
                logger.warning(issue_log(issue_number, "LAST OUTPUT:\n%s"), session_log)
            return SessionDecision(
                status=SessionStatus.TIMED_OUT,
                reason="Timed out without completion record",
            )

        logger.error(
            issue_log(
                issue_number,
                "SESSION COMPLETE: status=FAILED outcome=none "
                "reason=no_completion_record session=%s expected_completion=%s "
                "marker_exists=%s nearby_completion_files=%d",
            ),
            session_name,
            debug_context["requested_completion_abs_path"],
            debug_context["agent_done_marker_exists"],
            len(debug_context["nearby_completion_candidates"]),
        )
        log_completion_debug_context(issue_number, session_name, debug_context)
        if session_log:
            logger.error(issue_log(issue_number, "LAST OUTPUT:\n%s"), session_log)
        return SessionDecision(
            status=SessionStatus.FAILED, reason="Terminated without completion record"
        )

    def _handle_invalid_completion_record(
        self,
        *,
        observation: SessionObservationResult,
        worktree_path: Path,
        issue_number: int,
        session_name: str,
        run_dir: Path,
        completion_path: str | None,
        load_result: CompletionRecordLoadResult,
    ) -> SessionDecision:
        """Handle a present completion record that failed strict validation."""
        debug_context = collect_completion_debug_context(
            worktree_path=worktree_path,
            run_dir=run_dir,
            completion_path=completion_path,
        )
        report = report_invalid_completion_record(
            observation=observation,
            worktree_path=worktree_path,
            issue_number=issue_number,
            session_name=session_name,
            run_dir=run_dir,
            completion_path=completion_path,
            load_result=load_result,
            debug_context=debug_context,
            session_output=self.session_output,
            events=self.events,
        )
        return SessionDecision(
            status=SessionStatus.FAILED,
            reason=report.reason,
            completion_detail=report.completion_detail,
            diagnostic_path=report.diagnostic_path,
        )

    def _read_provider_status(self, run_dir: Path) -> ProviderStatus | None:
        return read_provider_status(run_dir)

    def _get_session_log_tail(self, run_dir: Path, session_name: str) -> str:
        """Get last 50 lines of session log for diagnostics."""
        log_path = self.session_output.get_log_path_for_run_dir(run_dir)
        if not (log_path and log_path.exists()):
            return ""
        try:
            content = log_path.read_text()
            lines = content.strip().split("\n")
            return "\n".join(lines[-50:])
        except Exception as e:
            logger.debug("Could not read session log: %s", e)
            return ""

    def _log_timeout_recovery(
        self, issue_number: int, session_name: str, record: "CompletionRecord"
    ) -> None:
        """Log and emit event for timeout recovery."""
        logger.info(
            issue_log(
                issue_number,
                "Session timed out but has completion.json - recovering work: outcome=%s",
            ),
            record.outcome.value,
        )
        self._emit_event(
            EventName.SESSION_TIMEOUT_RECOVERED,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "outcome": record.outcome.value,
            },
        )

    def _extract_pr_number_from_session_name(self, session_name: str) -> int | None:
        """Extract PR number from review session name."""
        if not session_name.startswith("review-"):
            return None
        try:
            pr_number = int(session_name.replace("review-", ""))
            logger.debug(f"Review session detected, PR number: {pr_number}")
            return pr_number
        except ValueError:
            logger.warning(
                f"Could not parse PR number from session name: {session_name}"
            )
            return None

    def _emit_processing_completed_event(
        self,
        worktree_path: Path,
        issue_number: int,
        session_name: str,
        run_dir: Path,
        result: "ProcessingResult",
    ) -> None:
        """Emit session processing completed event."""
        payload = {
            "issue_number": issue_number,
            "session_name": session_name,
            "success": result.success,
            "message": result.message,
            "actions_taken": result.actions_taken,
            "errors": result.errors,
            "pr_url": result.pr_url,
        }
        payload["run_dir"] = str(run_dir)
        self._emit_event(EventName.SESSION_PROCESSING_COMPLETED, payload)

    def _map_outcome_to_status(self, record: "CompletionRecord") -> SessionStatus:
        """Map completion outcome to session status."""
        from ..domain.models import CompletionOutcome

        outcome_to_status = {
            CompletionOutcome.COMPLETED: SessionStatus.COMPLETED,
            CompletionOutcome.BLOCKED: SessionStatus.BLOCKED,
            CompletionOutcome.NEEDS_HUMAN: SessionStatus.NEEDS_HUMAN,
            CompletionOutcome.REVIEW_APPROVED: SessionStatus.COMPLETED,
            CompletionOutcome.REVIEW_CHANGES_REQUESTED: SessionStatus.COMPLETED,
        }
        return outcome_to_status.get(record.outcome, SessionStatus.FAILED)

    def _run_validation_gate(
        self,
        worktree_path: Path,
        session_name: str,
        run_dir: Path,
        issue_number: int,
        issue_title: str,
        outcome: CompletionOutcome,
        validation_retry_count: int,
        original_prompt: str | None,
        retry_prompt_template: str | None,
        repo_root: Path | None,
        issue_key: "IssueKey | None",
    ) -> ValidationGateDecision:
        """Run validation gate and return updated status."""
        logger.info(
            issue_log(issue_number, "Running validation gate: cmd=%s timeout=%ds"),
            self._validation_cmd,
            self._validation_timeout,
        )
        validation_passed, validation_error, validation_error_file = (
            self._run_validation(
                worktree_path,
                session_name,
                issue_number,
                issue_title,
                run_dir,
                issue_key,
            )
        )

        if validation_passed:
            dirty_after_validation = self._handle_dirty_after_validation_if_needed(
                worktree_path=worktree_path,
                run_dir=run_dir,
                session_name=session_name,
                issue_number=issue_number,
                issue_title=issue_title,
                outcome=outcome,
                validation_retry_count=validation_retry_count,
                original_prompt=original_prompt,
                retry_prompt_template=retry_prompt_template,
                repo_root=repo_root,
            )
            if dirty_after_validation is not None:
                return dirty_after_validation

            logger.info(issue_log(issue_number, "Validation gate PASSED"))
            self.session_output.clear_retry_state(run_dir)
            # Typed write replaces the legacy partial-merge that omitted
            # `validation_reason` and let stale failure messages survive
            # into a fresh passed outcome (see ValidationOutcome
            # docstring in domain/artifact_contracts.py).
            self.session_output.update_validation_outcome(
                run_dir,
                ValidationPassed(),
                ended_at=datetime.now(timezone.utc).isoformat(),
                session_outcome_value=outcome.value,
            )
            self._emit_event(
                EventName.SESSION_VALIDATION_PASSED,
                {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "validation_cmd": self._validation_cmd,
                    "run_dir": str(run_dir),
                    "artifacts": self._validation_record_artifacts(
                        run_dir,
                        require_record=True,
                    ),
                },
            )
            return ValidationGateDecision(
                status=SessionStatus.COMPLETED,
                passed=validation_passed,
                error=validation_error,
                error_file=validation_error_file,
            )

        self._record_validation_failure_manifest(
            run_dir=run_dir,
            outcome=outcome,
            retry_count=validation_retry_count,
            validation_error=validation_error,
        )
        return ValidationGateDecision(
            status=self._route_validation_failure(
                ValidationFailureContext(
                    worktree_path=worktree_path,
                    run_dir=run_dir,
                    session_name=session_name,
                    issue_number=issue_number,
                    issue_title=issue_title,
                    retry_count=validation_retry_count,
                    error=validation_error,
                    error_file=validation_error_file,
                    original_prompt=original_prompt,
                    retry_prompt_template=retry_prompt_template,
                    repo_root=repo_root,
                    failure_kind=ValidationFailureKind.VALIDATION_COMMAND,
                    dirty_files=(),
                )
            ),
            passed=False,
            error=validation_error,
            error_file=validation_error_file,
        )

    def _handle_dirty_after_validation_if_needed(
        self,
        *,
        worktree_path: Path,
        run_dir: Path,
        session_name: str,
        issue_number: int,
        issue_title: str,
        outcome: CompletionOutcome,
        validation_retry_count: int,
        original_prompt: str | None,
        retry_prompt_template: str | None,
        repo_root: Path | None,
    ) -> ValidationGateDecision | None:
        dirty_policy = self.completion_processor.check_dirty_policy(worktree_path)
        if dirty_policy.ok:
            logger.info(
                issue_log(
                    issue_number,
                    "Dirty postflight passed after validation: session=%s worktree=%s",
                ),
                session_name,
                worktree_path,
            )
            return None

        validation_error = (
            "Validation command passed, but the worktree became dirty before "
            f"publish: {dirty_policy.reason}"
        )
        logger.warning(
            issue_log(
                issue_number,
                "Dirty postflight failed after validation: session=%s error=%s",
            ),
            session_name,
            validation_error,
        )
        self._record_validation_failure_manifest(
            run_dir=run_dir,
            outcome=outcome,
            retry_count=validation_retry_count,
            validation_error=validation_error,
        )
        status = self._route_validation_failure(
            ValidationFailureContext(
                worktree_path=worktree_path,
                run_dir=run_dir,
                session_name=session_name,
                issue_number=issue_number,
                issue_title=issue_title,
                retry_count=validation_retry_count,
                error=validation_error,
                error_file=None,
                original_prompt=original_prompt,
                retry_prompt_template=retry_prompt_template,
                repo_root=repo_root,
                failure_kind=ValidationFailureKind.DIRTY_AFTER_VALIDATION,
                dirty_files=dirty_policy.blocking_paths,
            )
        )
        return ValidationGateDecision(
            status=status,
            passed=False,
            error=validation_error,
            error_file=None,
        )

    def _record_validation_failure_manifest(
        self,
        *,
        run_dir: Path,
        outcome: CompletionOutcome,
        retry_count: int,
        validation_error: str | None,
    ) -> None:
        reason = validation_error or "validation failed"
        validation_outcome: "ValidationOutcome"
        if retry_count < self._max_validation_retries:
            validation_outcome = ValidationRetry(reason=reason)
        else:
            validation_outcome = ValidationFailed(reason=reason)
        self.session_output.update_validation_outcome(
            run_dir,
            validation_outcome,
            ended_at=datetime.now(timezone.utc).isoformat(),
            session_outcome_value=outcome.value,
        )

    def _route_validation_failure(
        self, failure: ValidationFailureContext
    ) -> SessionStatus:
        if failure.retry_count < self._max_validation_retries:
            return self._handle_validation_retry(failure)
        return self._handle_validation_exhausted(failure)

    def _handle_validation_retry(
        self,
        failure: ValidationFailureContext,
    ) -> SessionStatus:
        """Handle validation failure with retries remaining."""
        validation_summary = self._validation_error_summary(failure.error_file)
        logger.warning(
            issue_log(
                failure.issue_number,
                "Validation gate FAILED (retry %d/%d): cmd=%s error=%s summary=%s error_file=%s run_dir=%s",
            ),
            failure.retry_count + 1,
            self._max_validation_retries,
            self._validation_cmd,
            failure.error[:200] if failure.error else "none",
            validation_summary or "none",
            failure.error_file,
            failure.run_dir,
        )
        state = ValidationState(
            retry_count=failure.retry_count + 1,
            max_retries=self._max_validation_retries,
            validation_cmd=self._validation_cmd,
            last_error=failure.error[:2000] if failure.error else None,
            last_error_file=str(failure.error_file)
            if failure.error_file
            else None,
        )
        self.session_output.write_validation_state(failure.run_dir, state)

        task_prompt = failure.original_prompt or failure.issue_title
        retry_prompt_content = self._render_retry_prompt(
            task_prompt=task_prompt,
            validation_error=failure.error or "Unknown error",
            validation_error_file=failure.error_file,
            retry_count=failure.retry_count,
            max_retries=self._max_validation_retries,
            template_path=failure.retry_prompt_template,
            repo_root=failure.repo_root,
            failure_kind=failure.failure_kind,
            dirty_files=failure.dirty_files,
        )
        self.session_output.write_retry_prompt(failure.run_dir, retry_prompt_content)

        self._emit_event(
            EventName.SESSION_VALIDATION_RETRY_NEEDED,
            {
                "issue_number": failure.issue_number,
                "session_name": failure.session_name,
                "validation_cmd": self._validation_cmd,
                "error_file": str(failure.error_file)
                if failure.error_file
                else None,
                "validation_reason": failure.error,
                "validation_failure_kind": failure.failure_kind.value,
                "dirty_files": list(failure.dirty_files),
                "validation_error_summary": validation_summary,
                "retry_count": failure.retry_count,
                "max_retries": self._max_validation_retries,
                "run_dir": str(failure.run_dir),
                "artifacts": self._validation_record_artifacts(failure.run_dir),
            },
        )
        return SessionStatus.NEEDS_VALIDATION_RETRY

    def _handle_validation_exhausted(
        self,
        failure: ValidationFailureContext,
    ) -> SessionStatus:
        """Handle validation failure with max retries exhausted."""
        logger.warning(
            issue_log(
                failure.issue_number,
                "Validation gate FAILED (max retries %d exhausted): error=%s error_file=%s",
            ),
            self._max_validation_retries,
            failure.error[:200] if failure.error else "none",
            failure.error_file,
        )
        self.session_output.clear_retry_state(failure.run_dir)
        self._emit_event(
            EventName.SESSION_VALIDATION_FAILED,
            {
                "issue_number": failure.issue_number,
                "session_name": failure.session_name,
                "validation_cmd": self._validation_cmd,
                "error_file": str(failure.error_file)
                if failure.error_file
                else None,
                "validation_reason": failure.error,
                "validation_failure_kind": failure.failure_kind.value,
                "dirty_files": list(failure.dirty_files),
                "retry_count": failure.retry_count,
                "run_dir": str(failure.run_dir),
                "artifacts": self._validation_record_artifacts(failure.run_dir),
            },
        )
        return SessionStatus.VALIDATION_FAILED

    def _resolve_run_assets(
        self,
        session_name: str,
        *,
        session_run_assets: SessionRunAssets,
    ) -> SessionRunAssets:
        """Return the recorded run assets for the session being processed."""
        return resolve_run_assets(
            session_name=session_name,
            recorded_run_assets=session_run_assets,
        )

    def _enrich_manifest_from_completion(
        self,
        run_dir: Path,
        record: "CompletionRecord",
    ) -> None:
        """Write CompletionRecord detail into the run manifest."""
        from ..domain.run_manifest import RunManifest

        try:
            manifest = RunManifest.load(run_dir)
            manifest.enrich_from_completion_record(record)
            manifest.save()
        except Exception as exc:
            logger.warning(
                "[MANIFEST] Failed to enrich manifest for %s: %s",
                run_dir.name.split("__", 1)[-1]
                if "__" in run_dir.name
                else run_dir.name,
                exc,
            )

    @staticmethod
    def _build_completion_detail(record: "CompletionRecord") -> dict[str, Any]:
        """Extract curated fields from CompletionRecord for trace events."""
        detail: dict[str, Any] = {"outcome": record.outcome.value}
        for key in (
            "implementation",
            "problems",
            "attempted",
            "blocked_reason",
            "blocked_by",
            "question",
            "review_summary",
            "review_issues",
            "risk_level",
            "follow_up_issues",
        ):
            value = getattr(record, key, None)
            if value is not None:
                if key == "follow_up_issues":
                    detail[key] = [issue.to_dict() for issue in value]
                    continue
                detail[key] = value
        return detail

    def _log_session_completion(
        self,
        issue_number: int,
        session_name: str,
        status: SessionStatus,
        record: "CompletionRecord",
        result: "ProcessingResult",
        recovered: bool,
    ) -> None:
        """Log session completion summary."""
        pr_url = result.pr_url or "none"
        if result.success:
            logger.info(
                issue_log(
                    issue_number,
                    "SESSION COMPLETE: status=%s outcome=%s pr=%s recovered=%s session=%s",
                ),
                status.value,
                record.outcome.value,
                pr_url,
                recovered,
                session_name,
            )
        else:
            logger.error(
                issue_log(
                    issue_number,
                    "SESSION COMPLETE: status=%s outcome=%s pr=%s recovered=%s errors=%s session=%s",
                ),
                status.value,
                record.outcome.value,
                pr_url,
                recovered,
                result.errors,
                session_name,
            )

    def _run_validation(
        self,
        worktree_path: Path,
        session_name: str,
        issue_number: int,
        issue_title: str,
        run_dir: Path,
        issue_key: "IssueKey | None" = None,
    ) -> tuple[bool, Optional[str], Optional[Path]]:
        """Run validation command (with attempt-scoped caching) and return result.

        Uses PublishGate for caching. When an attempt identity is available,
        cached validation is scoped by issue identity and HEAD SHA. Older
        SHA-only caching remains available for callers that have no issue
        identity.

        Args:
            worktree_path: Path to the worktree
            session_name: Session name for output directory
            issue_number: Issue number for logging
            issue_title: Issue title for logging and retry context
            issue_key: Stable issue identity used for attempt-scoped caching

        Returns:
            Tuple of (passed, error_message, error_file_path)
        """
        if not self._command_runner or not self._validation_cmd:
            return True, None, None

        target_run_dir = run_dir

        # Get HEAD SHA for logging and attempt identity
        head_sha = self._working_copy.get_head_sha(worktree_path)
        sha_display = head_sha[:8] if head_sha else "unknown"
        attempt_key = self._validation_attempt_key(
            issue_key=issue_key,
            head_sha=head_sha,
        )

        # Use PublishGate for validation caching
        gate = PublishGate(
            worktree=worktree_path,
            command_runner=self._command_runner,
            working_copy=self._working_copy,
            command=self._validation_cmd,
            timeout_seconds=self._validation_timeout,
            attempt_store=self._attempt_store,
            attempt_key=attempt_key,
        )

        logger.info(
            issue_log(issue_number, "Running validation: cmd=%s worktree=%s sha=%s"),
            self._validation_cmd,
            worktree_path,
            sha_display,
        )

        result = gate.check(session_output_dir=target_run_dir)
        if result.cache_hit:
            if result.record is None:
                raise RuntimeError("validation cache hit did not include a record")
            self._materialize_cached_validation_record(target_run_dir, result.record)
        self._record_validation_evidence(
            worktree_path=worktree_path,
            run_dir=target_run_dir,
            record=result.record,
        )

        if result.allowed:
            if result.cache_hit:
                logger.info(
                    issue_log(
                        issue_number, "Validation cache hit: SHA=%s (skipped re-run)"
                    ),
                    sha_display,
                )
            else:
                logger.info(
                    issue_log(issue_number, "Validation passed: SHA=%s"),
                    sha_display,
                )
            return True, None, None

        # Validation failed - get error file path from record
        error_msg = result.reason
        error_file = self._resolve_error_file_path(worktree_path, result.record)

        return False, error_msg, error_file

    def _validation_attempt_key(
        self,
        *,
        issue_key: "IssueKey | None",
        head_sha: str | None,
    ) -> "AttemptKey | None":
        if head_sha is None or self._attempt_store is None:
            return None
        if self._validation_attempt_key_factory is None:
            raise RuntimeError(
                "attempt_store requires validation_attempt_key_factory"
            )
        if issue_key is None:
            raise RuntimeError(
                "attempt-scoped validation requires a stable IssueKey"
            )
        return self._validation_attempt_key_factory.for_validation_attempt(
            issue_key=issue_key,
            head_sha=head_sha,
        )

    def _record_validation_evidence(
        self,
        *,
        worktree_path: Path,
        run_dir: Path,
        record: ValidationRecord | None,
    ) -> None:
        self._validation_evidence_recorder.record_validation_evidence(
            run_dir=run_dir,
            worktree=worktree_path,
            record=record,
            record_path=run_dir / "validation-record.json",
            junit_xml_paths=self._validation_junit_xml_paths,
        )

    def _resolve_error_file_path(
        self,
        worktree_path: Path,
        record: Optional[Any],
    ) -> Optional[Path]:
        """Resolve the error file path from a validation record.

        Args:
            worktree_path: Path to the worktree
            record: ValidationRecord (or None)

        Returns:
            Absolute path to the error file, or None
        """
        if not record or not record.stderr_path:
            return None

        stderr_path = record.stderr_path
        if Path(stderr_path).is_absolute():
            return Path(stderr_path)
        return worktree_path / stderr_path

    @staticmethod
    def _materialize_cached_validation_record(run_dir: Path, record: Any) -> None:
        record_path = run_dir / "validation-record.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record.to_dict(), indent=2) + "\n")

    @staticmethod
    def _validation_error_summary(validation_error_file: Optional[Path]) -> str | None:
        """Return a short human-meaningful excerpt from validation stderr."""
        if validation_error_file is None or not validation_error_file.exists():
            return None
        try:
            for raw_line in validation_error_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                line = raw_line.strip()
                if line:
                    return line[:300]
        except OSError:
            return None
        return None

    def _render_retry_prompt(
        self,
        task_prompt: str,
        validation_error: str,
        validation_error_file: Optional[Path],
        retry_count: int,
        max_retries: int,
        failure_kind: ValidationFailureKind,
        template_path: Optional[str] = None,
        repo_root: Optional[Path] = None,
        dirty_files: tuple[str, ...] = (),
    ) -> str:
        """Render the retry prompt content.

        Args:
            task_prompt: The original task prompt
            validation_error: Error output (will be truncated, preserving tail)
            validation_error_file: Path to the full error file
            retry_count: Current retry attempt (0-based, displayed as 1-based)
            max_retries: Maximum allowed retries
            template_path: Optional path to custom template (relative to repo_root)
            repo_root: Repo root for resolving template_path

        Returns:
            Rendered retry prompt content.
        """
        if failure_kind in {
            ValidationFailureKind.DIRTY_BEFORE_VALIDATION,
            ValidationFailureKind.DIRTY_AFTER_VALIDATION,
        }:
            return self._render_dirty_worktree_retry_prompt(
                task_prompt=task_prompt,
                validation_error=validation_error,
                retry_count=retry_count,
                max_retries=max_retries,
                failure_kind=failure_kind,
                dirty_files=dirty_files,
            )

        # Load template - custom or default
        template = DEFAULT_RETRY_TEMPLATE
        if template_path and repo_root:
            template_full_path = repo_root / template_path
            if template_full_path.exists():
                try:
                    template = template_full_path.read_text()
                    logger.debug("Loaded retry template from %s", template_full_path)
                except OSError as e:
                    logger.warning(
                        "Failed to load retry template from %s: %s",
                        template_full_path,
                        e,
                    )
            else:
                logger.warning(
                    "Retry template not found at %s, using default", template_full_path
                )

        # Render template with variables
        # Note: retry_count is 0-based internally, display as 1-based
        display_count = retry_count + 1
        display_max = max_retries + 1
        return template.format(
            original_task=task_prompt,
            validation_cmd=self._validation_cmd or "",
            error_file=str(validation_error_file)
            if validation_error_file
            else "unknown",
            error_summary=_truncate_with_tail(validation_error),
            retry_count=display_count,
            max_retries=display_max,
            retries_remaining=display_max - display_count,
        )

    def _render_dirty_worktree_retry_prompt(
        self,
        *,
        task_prompt: str,
        validation_error: str,
        retry_count: int,
        max_retries: int,
        failure_kind: ValidationFailureKind,
        dirty_files: tuple[str, ...],
    ) -> str:
        """Render a dirty-worktree-specific retry prompt."""
        display_count = retry_count + 1
        display_max = max_retries + 1
        dirty_lines = (
            "\n".join(f"- `{path}`" for path in dirty_files)
            if dirty_files
            else "- See the worktree blocker text below."
        )
        if failure_kind is ValidationFailureKind.DIRTY_BEFORE_VALIDATION:
            timing = (
                "No validation command ran. The orchestrator blocked completion "
                "because the worktree was not publishable."
            )
        else:
            timing = (
                "The validation command passed, but the worktree became dirty "
                "before publish."
            )
        remediation = remediation_prompt_steps()
        return f"""# Dirty Worktree Retry (Attempt {display_count}/{display_max}) - {display_max - display_count} attempt(s) remaining after this

{timing}

## Required Fix

1. Run `git status --short`.
{remediation}
4. Run `prepush-check --dirty-only -v`; it must pass before `coding-done`. That check is the gate for *completing*; if you reached rung 3 and are escalating instead, go straight to the escalation command — it accepts the dirty tree on purpose.
5. Run `coding-done completed --implementation "describe what you fixed" --problems "any remaining issues"`.

Runtime note: orchestrator-managed metadata under `.issue-orchestrator/` and `.claude/` is ignored by the orchestrator dirty guard. Tracked project files, generated sources, lock files, and schemas that belong to this issue must still be committed; anything you did not create must be preserved, not deleted.

## Dirty Files

{dirty_lines}

## Original Task

{task_prompt}

## Worktree Blocker

```
{_truncate_with_tail(validation_error)}
```
"""

    def _emit_event(self, event_type: EventName, data: dict[str, Any]) -> None:
        """Emit a trace event."""
        self.events.publish(make_trace_event(event_type, data))

    @staticmethod
    def _validation_record_artifacts(
        run_dir: Path, *, require_record: bool = False
    ) -> list[dict[str, str]]:
        record_path = run_dir / "validation-record.json"
        if not record_path.exists():
            if require_record:
                raise FileNotFoundError(
                    f"validation-record.json missing for passed validation event: {record_path}"
                )
            return []
        return [
            {
                "type": "validation",
                "label": "Validation Record",
                "value": str(record_path),
            }
        ]
