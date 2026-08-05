"""Completion processor - handles agent completion records.

This controller reads CompletionRecords written by coding-done/reviewer-done and executes
the appropriate actions via adapters.

Architecture principle: The agent reports intent; the orchestrator decides and executes.

The agent does NOT:
- Push code
- Create PRs
- Post comments
- Mutate labels

All those actions are performed here after validating the completion record
as untrusted input.
"""

import json
import logging
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..domain.artifact_contracts import ValidationFailed
from ..domain.completion_finalization import (
    CompletionFinalizationCommand,
    CompletionFinalizationPlan,
    CompletionRuntimeState,
    ReviewExchangeRunningQuery,
    decide_completion_finalization,
)
from ..domain.models import (
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
    COMPLETION_RECORD_PATH,
)
from ..domain.events import EventBus, SessionEvent
from ..domain.review_artifacts import review_artifacts_from_exchange_result
from ..domain.review_exchange_run import ReviewExchangeRun, ReviewExchangeRunAssets
from ..domain.runtime_identity import RuntimeIdentity
from ..domain.session_run import SessionRunAssets, ValidationArtifactPaths
from ..events import EventContext, EventName
from ..ports import EventSink
from ..ports.review_artifact_reader import (
    ReviewArtifactReadCommand,
    ReviewArtifactReader,
)
from .background_job_supervisor import BackgroundJobSupervisor
from ..ports.event_sink import RunScopedEventPayload, make_run_scoped_event, make_trace_event
from ..infra.runtime_artifacts import (
    build_forbidden_runtime_artifact_reason,
    forbidden_branch_runtime_artifacts,
)
from ..infra.timeline_trace import is_timeline_trace_enabled
from ..infra.worktree_base import resolve_base_branch
from ..ports.review_exchange_runner import (
    NullReviewExchangeRunner,
    ReviewExchangeRunner,
)
from ..ports.session_output import SessionOutput, ValidationRecord
from .validation import PublishGate, ValidationRecordStore
from .completion_pr_collision import (
    create_pr_with_collision_handling,
    get_open_pr_for_issue,
    maybe_switch_branch_for_pr_collision,
)
from .completion_failure_reporting import (
    build_gate_failure_comment,
    build_processing_failure_comment,
)
from .completion_record_validation import (
    CompletionRecordLoadResult,
    CompletionRecordValidator,
    WorktreeValidationFailure,
    WorktreeValidationResult,
)
from .completion_result_artifacts import (
    build_pr_body,
    build_processing_result,
    cleanup_completion_record,
    preserve_completion_record,
    write_reviewer_feedback_file,
)
from .completion_review_exchange import CompletionReviewExchange
from .completion_ports import GitAdapter, LabelAdapter, PRAdapter
from .completion_types import (
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUBLISH_BLOCKED,
    ERROR_PREFIX_PUSH,
    ProcessingResult,
    REVIEW_EXCHANGE_ERROR_PREFIX,
)
from .pre_publish_gate import PrePublishGate, PrePublishGateResult
from .review_exchange_contracts import ReviewExchangeCanceller
from .review_cache_boundary import review_cache_boundary_started_at
from .review_exchange_pr_comment import (
    GITHUB_COMMENT_BODY_LIMIT,
    build_review_exchange_pr_comment_body,
)
from .test_skip_guard import added_test_paths, scan_added_test_skip_guards
from .tech_lead_approval_gate import build_tech_lead_decision_approval_gate
from .tech_lead_completion import tech_lead_decision_processing_error
from .tech_lead_session_policy import is_benign_tech_lead_no_commits, is_tech_lead_session, shape_requested_actions_for_tech_lead
from .worktree_head import current_worktree_head_sha
from ..ports.pull_request_tracker import PRInfo
from ..ports.working_copy import PushResult

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.agent_callback_endpoint import AgentCallbackEndpoint
    from ..ports.review_exchange_approval_gate import ReviewExchangeApprovalGate
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .stack_base import StackBaseDecision
    from .stack_publish_gate import StackBaseGate


class _MissingReviewArtifactReader:
    def read_review_artifact(self, command: ReviewArtifactReadCommand) -> Any:
        raise RuntimeError(
            "CompletionProcessor requires review_artifact_reader to read "
            f"review artifacts for issue #{command.issue_number}"
        )


class _MissingTechLeadAuthorityStore:
    """Fail-fast default: tech_lead completions require the wired port.

    Production always injects the SQLite-backed store from bootstrap; a test
    that exercises a tech_lead completion without wiring the port surfaces the
    misconfiguration immediately instead of silently fail-safing.
    """

    def _fail(self) -> Any:
        raise RuntimeError(
            "CompletionProcessor requires tech_lead_authority to process a "
            "tech_lead session completion (wired in entrypoints/bootstrap.py)"
        )

    def record(self, *, run_id: str, session_name: str, authority: Any) -> None:
        self._fail()

    def load(self, *, run_id: str, session_name: str) -> Any:
        self._fail()

    def discard(self, *, run_id: str, session_name: str) -> None:
        self._fail()


# Containment for agent-supplied validation record paths lives in its own
# owner module; the private aliases keep the processor's call sites stable.
from .validation_record_containment import (
    contain_validation_record_path as _contain_validation_record_path,
    copy_from_fd as _copy_from_fd,
    open_contained_validation_record as _open_contained_validation_record,
)


class CompletionProcessor:
    """Process agent completion records and execute requested actions.

    This is a control-plane component that:
    1. Reads completion records (untrusted input from agents)
    2. Validates the record and current worktree state
    3. Decides which actions to actually execute (may differ from requested)
    4. Executes actions via adapters (execution plane)

    The processor has AUTHORITY to reject or modify requested actions based on policy.
    """

    def __init__(
        self,
        label_adapter: LabelAdapter,
        pr_adapter: PRAdapter,
        git_adapter: GitAdapter,
        session_output: SessionOutput,
        review_exchange_runner: ReviewExchangeRunner | None = None,
        event_bus: EventBus | None = None,
        label_config: dict[str, str] | None = None,
        publish_gate: PublishGate | None = None,
        pre_publish_gate: PrePublishGate | None = None,
        config: "Config | None" = None,
        background_job_supervisor: "BackgroundJobSupervisor | None" = None,
        review_exchange_canceller: ReviewExchangeCanceller | None = None,
        *,
        # Required: an agent with no callback endpoint cannot report
        # anything back, so there is no sensible default to fall back to.
        agent_callback_endpoint: "AgentCallbackEndpoint",
        review_artifact_reader: ReviewArtifactReader | None = None,
        runtime_identity: RuntimeIdentity | None = None,
        tech_lead_authority: "TechLeadAuthorityStore | None" = None,
    ):
        """Initialize the processor with required adapters.

        Args:
            label_adapter: Adapter for label operations (add/remove labels).
            pr_adapter: Adapter for PR operations (create PR, add comment).
            git_adapter: Adapter for git operations (push).
            session_output: Session output storage for artifacts.
            event_bus: Optional EventBus for emitting processing events.
            label_config: Optional mapping of label names (e.g., {"blocked": "blocked"}).
            publish_gate: Optional PublishGate for validating before publish actions.
            pre_publish_gate: Optional gate that runs the worktree's effective
                pre-push hook before the authenticated push.
            background_job_supervisor: Owns failure-handling for the
                background runner. When omitted the processor uses a private
                supervisor wrapping :class:`NullBackgroundJobRunner` so the
                inline-execution path still works for tests; production MUST
                pass the shared supervisor from bootstrap so
                ``Orchestrator.tick`` drains its completions.
            review_exchange_canceller: Issue-scoped lifecycle hook used when
                the async review-exchange job reaches a terminal failure.
            review_artifact_reader: Reader for run-scoped review artifacts.
            runtime_identity: Running orchestrator identity to stamp on PRs.
            tech_lead_authority: Orchestrator-owned tech_lead launch-authority port
                (ADR-0031). Required whenever tech_lead completions are
                processed; the fail-fast default raises on first use.
        """
        self.label_adapter = label_adapter
        self.pr_adapter = pr_adapter
        self.git_adapter = git_adapter
        self.session_output = session_output
        self.event_bus = event_bus
        self._trace_events: EventSink | None = None
        self._event_context: EventContext | None = None
        self.label_config = label_config or {}
        self.publish_gate = publish_gate
        self.pre_publish_gate = pre_publish_gate
        self._config = config
        self._pr_collision_strategy = (
            config.worktree_remediation_pr_collision
            if config is not None
            else "new_branch"
        )
        self._push_rebase_retry = (
            config.worktree_remediation_push_rebase_retry
            if config is not None
            else True
        )
        # Production must always inject a real runner via bootstrap.
        # ``NullReviewExchangeRunner`` is the test-only default — its
        # ``run`` raises if any test that hasn't wired a real runner
        # actually enters the review-exchange path, so misuse surfaces
        # immediately rather than silently no-oping.
        self._review_exchange = CompletionReviewExchange(
            config=config,
            session_output=session_output,
            emit_review_started=self._emit_review_started,
            emit_review_outcome=self._emit_review_outcome,
            review_exchange_runner=review_exchange_runner or NullReviewExchangeRunner(),
            job_supervisor=background_job_supervisor,
            review_exchange_canceller=review_exchange_canceller,
            agent_callback_endpoint=agent_callback_endpoint,
        )
        # Per-(session, head_sha) consecutive validation-failed reroute count.
        # The reroute path can re-enter every tick when downstream rework
        # fails to advance the SHA. Without a budget, a permanently-failing
        # validation forms an infinite loop. Reusing review_exchange_max_rounds
        # so the catch-all ceiling matches the in-loop bound.
        self._validation_reroute_counts: dict[tuple[str, str], int] = {}
        self._record_validator = CompletionRecordValidator(
            config=config,
            git_adapter=git_adapter,
        )
        self._review_artifact_reader = review_artifact_reader or _MissingReviewArtifactReader()
        self._tech_lead_authority: "TechLeadAuthorityStore" = (
            tech_lead_authority or _MissingTechLeadAuthorityStore()
        )
        self._runtime_identity = runtime_identity
        # Optional stack publish-gate owner (ADR-0029 / #6596). When attached, a
        # Stack-after: successor's PR is based on its predecessor branch and a
        # blocked publish gate (e.g. an incompatible base-branch rule or a stale
        # successor) fails fast with a diagnostic. Absent -> non-stack behavior.
        self._stack_publish_gate: "StackBaseGate | None" = None

    def _emit(
        self,
        event_type: SessionEvent,
        issue_number: int,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Emit an event if event_bus is configured."""
        if self.event_bus:
            self.event_bus.publish(
                event_type,
                entity_id=issue_number,
                data=data or {},
                source="completion_processor",
            )

    def _add_issue_comment(self, issue_number: int, comment: str, *, context: str) -> None:
        try:
            self.pr_adapter.add_comment(issue_number, comment)
        except Exception as exc:
            logger.warning(
                "Failed to add %s comment for #%d: %s",
                context,
                issue_number,
                exc,
            )

    def set_event_emitter(self, events: EventSink, event_context: EventContext) -> None:
        """Attach TraceEvent emitter for review exchange events."""
        self._trace_events = events
        self._event_context = event_context

    def attach_stack_publish_gate(self, gate: "StackBaseGate") -> None:
        """Wire the stack publish-gate owner (ADR-0029 / #6596)."""
        self._stack_publish_gate = gate

    def _get_label(self, key: str) -> str:
        """Get label name from config, or use default."""
        defaults = {
            "blocked": "blocked",
            "needs_human": "needs-human",
            "code_reviewed": "code-reviewed",
            "needs_rework": "needs-rework",
            "code_review": "code-review",
            "in_progress": "in-progress",
            "validation_failed": "validation-failed",
        }
        return self.label_config.get(key, defaults.get(key, key))

    def _base_branch(self) -> str:
        if self._config is None:
            return "main"
        resolved = resolve_base_branch(
            self._config.repo_root,
            config_override=self._config.worktree_base_branch_override,
            default_branch_resolver=self.git_adapter.default_branch,
            log=logger,
        )
        return resolved.branch

    def read_completion_record(
        self, worktree: Path, completion_path: str | None = None
    ) -> CompletionRecord | None:
        return self._record_validator.read_completion_record(worktree, completion_path)

    def read_completion_record_result(
        self, worktree: Path, completion_path: str | None = None
    ) -> CompletionRecordLoadResult:
        return self._record_validator.read_completion_record_result(
            worktree, completion_path
        )

    def _resolve_agent_label_from_completion_path(
        self, completion_path: str | None
    ) -> tuple[str | None, str | None]:
        return self._record_validator.resolve_agent_label_from_completion_path(
            completion_path
        )

    def _is_tech_lead_session(self, agent_label: str | None) -> bool:
        """Tech Lead identity via the ADR-0031 owner (config-declared tech lead agent)."""
        tech_lead_agent = self._config.tech_lead_review_agent if self._config else None
        return is_tech_lead_session(tech_lead_agent, agent_label)

    def validate_worktree_state(
        self, worktree: Path, record: CompletionRecord
    ) -> WorktreeValidationResult:
        return self._record_validator.validate_worktree_state(worktree, record)

    def check_dirty_policy(self, worktree: Path) -> WorktreeValidationResult:
        return self._record_validator.check_dirty_policy(worktree)

    def deferred_review_exchange_result(self) -> ProcessingResult:
        """Construct the standard async review-exchange deferral result."""
        return ProcessingResult.for_review_exchange_deferred()

    def completion_finalization_plan(
        self,
        *,
        issue_number: int,
        session_name: str | None,
        run_id: str,
        outcome: CompletionOutcome,
        requested_actions: tuple[RequestedAction, ...],
        runtime_state: CompletionRuntimeState,
        validation_preflight_configured: bool,
    ) -> CompletionFinalizationPlan:
        """Build and decide the typed completion-finalization command."""
        query = ReviewExchangeRunningQuery(
            issue_number=issue_number,
            session_name=session_name,
            requested_actions=requested_actions,
            run_id=run_id,
        )
        running = self._review_exchange.is_review_exchange_running_for_completion(query)
        # Avoid the extra supervisor lookup on the happy path: the matrix
        # only consults ``review_exchange_within_deadline`` when the BG job
        # is actually running. When it isn't, the field stays at its default
        # (False) and no decision branch reads it.
        within_deadline = (
            running
            and self._review_exchange.is_review_exchange_within_deadline_for_completion(
                query
            )
        )
        command = CompletionFinalizationCommand(
            issue_number=issue_number,
            session_name=session_name,
            outcome=outcome,
            requested_actions=requested_actions,
            runtime_state=runtime_state,
            review_exchange_running=running,
            validation_preflight_configured=validation_preflight_configured,
            review_exchange_within_deadline=within_deadline,
        )
        return decide_completion_finalization(command)

    def _emit_review_comment_added(
        self,
        *,
        issue_number: int,
        pr_number: int,
        comment_url: str | None,
        comment_body: str,
        run_dir: Path | None = None,
    ) -> None:
        """Emit trace event for a posted review comment (if trace events are configured)."""
        if self._trace_events is None or self._event_context is None:
            return
        excerpt = comment_body.strip().replace("\n", " ")
        payload = {
            "issue_number": issue_number,
            "pr_number": pr_number,
            "comment_url": comment_url or "",
            "comment_excerpt": excerpt[:180] if excerpt else "",
            "summary": "Posted review comment",
        }
        if run_dir is not None:
            payload["run_dir"] = str(run_dir)
        self._trace_events.publish(
            make_trace_event(
                EventName.REVIEW_COMMENT_ADDED,
                self._event_context.enrich(payload),
            )
        )

    def _emit_publish_failed(
        self,
        *,
        issue_number: int,
        stage: str,
        error: str,
        retryable: bool | None = None,
        branch: str | None = None,
    ) -> None:
        """Surface the actual cause of a publish failure on the timeline.

        Without this event the timeline's only breadcrumb is a generic
        ``agent.failed — "Session ended without PR or status update"``; the
        real error (push hook timeout, non-fast-forward, PR collision, etc.)
        lives in ``failure-diagnostic-*.json`` on disk and the card's
        ``blocked_summary`` only says "Push or PR creation failed". Emitting
        here routes the diagnostic to the UI and to any consumer of the SSE
        stream.
        """
        if self._trace_events is None or self._event_context is None:
            # The whole point of this emitter is to close an observability
            # gap — if we're silently no-op'ing in production we want at
            # least a DEBUG breadcrumb naming the drop so operators can
            # audit it rather than wonder why the timeline is blank.
            logger.debug(
                "publish.failed not emitted (no event sink wired): "
                "issue=%d stage=%s error=%r",
                issue_number,
                stage,
                error[:120],
            )
            return
        payload: dict[str, Any] = {
            "issue_number": issue_number,
            "stage": stage,
            "error": error[:500],  # cap so SSE payloads stay small
        }
        if retryable is not None:
            payload["retryable"] = retryable
        if branch:
            payload["branch"] = branch
        self._trace_events.publish(
            make_trace_event(
                EventName.PUBLISH_FAILED,
                self._event_context.enrich(payload),
            )
        )

    def _emit_review_started(
        self,
        *,
        issue_number: int,
        reviewer_label: str | None,
        exchange_mode: str,
        run_dir: Path,
        cached: bool = False,
        review_cache_summary_path: str | None = None,
        review_cache_validation_record_path: str | None = None,
        review_cache_head_sha: str | None = None,
    ) -> None:
        """Emit trace event when local review exchange starts.

        When ``cached`` is True, the event represents a replay of a prior
        approval (no new reviewer was launched in this run); ``run_dir``
        then points at the original review-exchange session and is tagged
        so the timeline narrative does not claim a fresh review started.
        """
        if self._trace_events is None or self._event_context is None:
            return
        payload: RunScopedEventPayload = {
            "issue_number": issue_number,
            "task": "review",
            "agent": reviewer_label or "",
            "review_exchange_mode": exchange_mode,
            "run_id": str(self._event_context.run_id),
            "run_dir": str(run_dir),
        }
        if cached:
            payload["cached"] = True
        if review_cache_summary_path:
            payload["review_cache_summary_path"] = review_cache_summary_path
        if review_cache_validation_record_path:
            payload["review_cache_validation_record_path"] = review_cache_validation_record_path
        if review_cache_head_sha:
            payload["review_cache_head_sha"] = review_cache_head_sha
        self._trace_events.publish(make_run_scoped_event(EventName.REVIEW_STARTED, payload))

    def _emit_review_outcome(
        self,
        *,
        issue_number: int,
        reviewer_label: str | None,
        exchange_mode: str,
        approved: bool,
        rounds: int | None,
        summary: str,
        run_dir: Path | None = None,
        artifacts: list[dict[str, str]] | None = None,
        cached: bool = False,
        review_cache_summary_path: str | None = None,
        review_cache_validation_record_path: str | None = None,
        review_cache_head_sha: str | None = None,
    ) -> None:
        """Emit review terminal event from local exchange outcome.

        When ``cached`` is True, the approval/changes-requested event is
        a replay of a prior exchange (rounds happened in an earlier run).
        The ``cached`` flag is forwarded to the timeline narrative enricher
        so the event is narrated as a replay rather than a fresh review.
        """
        if self._trace_events is None or self._event_context is None:
            return
        payload: dict[str, Any] = {
            "issue_number": issue_number,
            "task": "review",
            "agent": reviewer_label or "",
            "review_exchange_mode": exchange_mode,
            "rounds": rounds,
            "summary": summary,
        }
        if run_dir is not None:
            payload["run_dir"] = str(run_dir)
        if artifacts:
            payload["artifacts"] = artifacts
        if cached:
            payload["cached"] = True
        if review_cache_summary_path:
            payload["review_cache_summary_path"] = review_cache_summary_path
        if review_cache_validation_record_path:
            payload["review_cache_validation_record_path"] = review_cache_validation_record_path
        if review_cache_head_sha:
            payload["review_cache_head_sha"] = review_cache_head_sha
        event_name = EventName.REVIEW_APPROVED if approved else EventName.REVIEW_CHANGES_REQUESTED
        self._trace_events.publish(
            make_trace_event(
                event_name,
                self._event_context.enrich(payload),
            )
        )

    def _requires_publish_gate(self, record: CompletionRecord) -> bool:
        """Check if the completion record requests actions that require publish gate.

        Args:
            record: The completion record to check.

        Returns:
            True if any requested action requires publish gate validation.
        """
        publish_actions = {RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR}
        return bool(set(record.requested_actions) & publish_actions)

    def _check_publish_gate(
        self,
        worktree: Path,
        session_output_dir: Path | None = None,
    ) -> tuple[bool, str, ValidationRecord | None]:
        """Check if publishing is allowed by the publish gate.

        Args:
            worktree: Path to the worktree.
            session_output_dir: If provided, validation output is written directly here.

        Returns:
            Tuple of (allowed, reason, record).
        """
        if self.publish_gate is None:
            # No gate configured = allowed
            return True, "", None

        result = self.publish_gate.check(session_output_dir=session_output_dir)
        if result.allowed:
            cache_note = " (cached)" if result.cache_hit else ""
            logger.info("Publish gate passed%s: %s", cache_note, result.reason)
            return True, result.reason, result.record
        else:
            logger.warning("Publish gate failed: %s", result.reason)
            return False, result.reason, result.record

    @staticmethod
    def _load_validation_record(record_path: Path) -> ValidationRecord | None:
        try:
            data = json.loads(record_path.read_text())
        except OSError:
            return None
        except json.JSONDecodeError:
            return None
        try:
            return ValidationRecord.from_dict(data)
        except TypeError:
            return None

    def _attach_validation_artifacts(
        self,
        worktree: Path,
        validation_artifacts: ValidationArtifactPaths,
        record: ValidationRecord | None = None,
        record_path: Path | None = None,
    ) -> None:
        """Attach validation artifacts to session output.

        Updates manifest with paths to validation files that should already exist
        in the session output directory (written directly by validation).
        """
        run_dir = validation_artifacts.run_dir
        if record_path is None and record is not None:
            record_path = ValidationRecordStore(worktree).get_record_path(record.head_sha)
        run_dir_record_path = validation_artifacts.record_path
        effective_record_path = self._materialize_validation_record(
            worktree=worktree,
            record_path=record_path,
            run_dir_record_path=run_dir_record_path,
        )
        if effective_record_path is not None:
            self.session_output.update_manifest(
                run_dir,
                {"validation_record_path": str(effective_record_path)},
            )
            try:
                (run_dir / "validation-record.path").write_text(str(effective_record_path))
            except OSError:
                logger.debug("Failed to write validation pointer for %s", run_dir)

        # Update manifest with validation output paths (files written by validation)
        updates: dict[str, str] = {}
        stdout_path = validation_artifacts.stdout_path
        stderr_path = validation_artifacts.stderr_path

        if stdout_path.exists():
            updates["validation_stdout"] = str(stdout_path)
        if stderr_path.exists():
            updates["validation_stderr"] = str(stderr_path)

        if updates:
            self.session_output.update_manifest(run_dir, updates)

    def _materialize_validation_record(
        self,
        *,
        worktree: Path,
        record_path: Path | None,
        run_dir_record_path: Path,
    ) -> Path | None:
        """Resolve the run-dir record's authoritative content and return its path.

        Precedence: when ``record_path`` is supplied, the caller is asking
        the helper to publish that source as the run-dir's authoritative
        record. Falls back to a pre-existing run-dir file ONLY when no
        source was supplied — refusing the caller's source and silently
        publishing a stale local snapshot would be the #6017 P2 path-leak
        class in reverse. Returns ``None`` when nothing can be attached.
        """
        if record_path is None or not record_path.exists():
            return run_dir_record_path if run_dir_record_path.exists() else None
        # Source/destination identity check. ``_copy_from_fd`` opens
        # ``dst`` with ``open(dst, "wb")`` which truncates the file
        # before reading completes, so a same-file copy ends up as empty
        # JSON. When the caller already wrote the authoritative record
        # into run_dir (the common case post-PublishGate fix), there's
        # nothing to copy — just attach.
        try:
            same_file = (
                record_path.resolve(strict=False)
                == run_dir_record_path.resolve(strict=False)
            )
        except OSError:
            same_file = False
        if same_file:
            return run_dir_record_path
        # Symlink-safe walk: opens the source under the worktree with
        # O_NOFOLLOW on every path component (#6017 re-review-4 P2),
        # never reopens by path string.
        src_fd = _open_contained_validation_record(str(record_path), worktree)
        if src_fd is not None and _copy_from_fd(src_fd, run_dir_record_path):
            return run_dir_record_path
        return None

    def process(
        self,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        *,
        run_assets: SessionRunAssets,
        pr_number: int | None = None,
        completion_path: str | None = None,
        agent_label: str | None = None,
    ) -> ProcessingResult:
        """Process a completion record and execute actions.

        Args:
            worktree: Path to the worktree containing the completion record.
            issue_number: The GitHub issue number this work is for.
            issue_title: The issue title (for PR creation).
            pr_number: Optional PR number for review sessions. When provided,
                label operations will target the PR instead of the issue.
            completion_path: Relative path to completion file. If None, uses legacy path.

        Returns:
            ProcessingResult with success status and details.
        """
        start_time = time.monotonic()
        # For review sessions, label operations target the PR
        label_target = pr_number if pr_number else issue_number
        actions_taken: list[str] = []
        errors: list[str] = []
        error_details: list[dict[str, Any]] = []  # Full diagnostic info per error
        pr_url: str | None = None

        # Read and validate completion record
        record, session_name, error_result = self._read_and_validate_record(
            worktree,
            completion_path,
            run_assets,
        )
        if error_result:
            return error_result
        assert record is not None  # Guaranteed if error_result is None

        if agent_label is None:
            agent_label, agent_error = self._resolve_agent_label_from_completion_path(
                completion_path
            )
            if agent_error:
                return ProcessingResult(
                    success=False, message=agent_error, errors=[agent_error]
                )
        # Tech Lead prompts promise no orchestrator comments (ADR-0031); the
        # record is untrusted intent, so shape it once at the door.
        if self._is_tech_lead_session(agent_label):
            record.requested_actions = list(
                shape_requested_actions_for_tech_lead(tuple(record.requested_actions))
            )
        requested_actions = tuple(record.requested_actions)
        running_query = ReviewExchangeRunningQuery(
            issue_number=issue_number,
            session_name=session_name,
            requested_actions=requested_actions,
            run_id=run_assets.run_id,
        )
        exchange_running = self._review_exchange.is_review_exchange_running_for_completion(
            running_query
        )
        if record.outcome is CompletionOutcome.COMPLETED and exchange_running:
            logger.info(
                "Completion deferred before pre-action policies: issue=%d "
                "session=%s reason=review exchange is already running",
                issue_number,
                session_name,
            )
            return ProcessingResult.for_review_exchange_deferred()

        pre_action_failure = self._check_pre_action_policies(
            worktree,
            record,
            session_name,
            issue_number,
            run_assets,
            agent_label=agent_label,
        )
        if pre_action_failure:
            return pre_action_failure

        # Get branch name for PR operations
        branch = self.git_adapter.get_current_branch(worktree)
        logger.info(
            "Completion worktree state: issue=%s branch=%s worktree=%s",
            issue_number,
            branch,
            worktree,
        )

        # Log what actions were requested
        logger.info(
            "Processing completion for #%d: outcome=%s, requested_actions=%s",
            issue_number,
            record.outcome.value,
            [a.value for a in record.requested_actions],
        )

        preserved_completion_path = preserve_completion_record(
            session_output=self.session_output,
            worktree=worktree,
            completion_path=completion_path,
            run_assets=run_assets,
        )

        # Execute requested actions in order
        (
            branch,
            pr_url,
            review_exchange_completed,
            deferred,
            early_result,
        ) = self._execute_actions(
            worktree=worktree,
            record=record,
            issue_number=issue_number,
            issue_title=issue_title,
            label_target=label_target,
            branch=branch,
            session_name=session_name,
            agent_label=agent_label,
            actions_taken=actions_taken,
            errors=errors,
            error_details=error_details,
            run_assets=run_assets,
        )
        if early_result is not None:
            return early_result

        if deferred:
            # Review exchange is running in the background. Leave the completion
            # record on disk so the next observation re-enters this pipeline,
            # and skip result artifacts / cleanup that would imply completion.
            logger.info(
                "Completion deferred (review exchange running): issue=%d session=%s",
                issue_number,
                session_name,
            )
            return ProcessingResult.for_review_exchange_deferred()

        # Write reviewer feedback to session run directory for rework sessions to use
        # This is only relevant for review sessions (pr_number provided) with feedback
        if pr_number and record.review_issues:
            write_reviewer_feedback_file(
                run_assets.run_dir,
                pr_number,
                record.review_issues,
            )

        # Build and return result
        total_duration = time.monotonic() - start_time
        return build_processing_result(
            session_output=self.session_output,
            worktree=worktree,
            record=record,
            session_name=session_name,
            issue_number=issue_number,
            issue_title=issue_title,
            branch=branch,
            pr_url=pr_url,
            review_exchange_completed=review_exchange_completed,
            actions_taken=actions_taken,
            errors=errors,
            error_details=error_details,
            total_duration=total_duration,
            completion_path=completion_path,
            preserved_completion_path=preserved_completion_path,
            run_assets=run_assets,
            emit_completion_event=self._emit,
            post_issue_comment=self._add_issue_comment,
            cleanup_completion_record_fn=self._cleanup_completion_record,
        )

    def _reject_tech_lead_completion_if_invalid(
        self,
        *,
        record: CompletionRecord,
        agent_label: str | None,
        issue_number: int,
        run_assets: SessionRunAssets,
    ) -> ProcessingResult | None:
        """Authoritative tech_lead authority + pair validation (ADR-0031).

        A COMPLETED tech_lead session must have a trusted launch-authority
        record (#6761 re-review F1) and a valid decision artifact pair
        (#6761 F3). Running here — in the pre-action policy phase, before
        the completion record is preserved and before ANY requested action
        executes (#6769 finding 1) — a rejection produces ZERO push/PR/
        comment calls and a failed processing result whose tagged error is
        classified critical, so history records FAILED for every flavor and
        the tech_lead failure labeling path fires downstream.
        """
        if self._config is None or not self._is_tech_lead_session(agent_label):
            return None
        if record.outcome is not CompletionOutcome.COMPLETED:
            return None
        tech_lead_error = tech_lead_decision_processing_error(
            self._config,
            tech_lead_authority=self._tech_lead_authority,
            run_dir=run_assets.run_dir,
            run_id=run_assets.run_id,
            session_name=run_assets.session_name,
        )
        if tech_lead_error is None:
            return None
        logger.warning(
            "Tech Lead completion rejected before any action for issue #%d: %s",
            issue_number,
            tech_lead_error,
        )
        return ProcessingResult(
            success=False,
            message=f"Tech Lead completion rejected: {tech_lead_error}",
            errors=[tech_lead_error],
        )

    def _review_exchange_approval_gate(
        self,
        *,
        agent_label: str | None,
        run_assets: SessionRunAssets,
    ) -> "ReviewExchangeApprovalGate | None":
        """Build the artifact gate used at the terminal reviewer boundary."""
        return build_tech_lead_decision_approval_gate(
            self._config,
            tech_lead_agent=(
                self._config.tech_lead_review_agent if self._config else None
            ),
            agent_label=agent_label,
            tech_lead_authority=self._tech_lead_authority,
            run_dir=run_assets.run_dir,
            run_id=run_assets.run_id,
            session_name=run_assets.session_name,
        )

    def _check_pre_action_policies(
        self,
        worktree: Path,
        record: CompletionRecord,
        session_name: str | None,
        issue_number: int,
        run_assets: SessionRunAssets,
        *,
        agent_label: str | None,
    ) -> ProcessingResult | None:
        """Run completion policies that must pass before any action executes."""
        # First: tech_lead scope/decision authority (#6769 finding 1). Checked
        # ahead of the worktree policies because a rejected tech_lead completion
        # must produce zero GitHub calls — the worktree handlers below post
        # diagnostic comments.
        tech_lead_rejection = self._reject_tech_lead_completion_if_invalid(
            record=record,
            agent_label=agent_label,
            issue_number=issue_number,
            run_assets=run_assets,
        )
        if tech_lead_rejection is not None:
            return tech_lead_rejection

        worktree_state = self.validate_worktree_state(worktree, record)
        if not worktree_state.ok:
            return self._handle_invalid_worktree_state(
                worktree,
                record,
                session_name,
                issue_number,
                worktree_state,
                run_assets,
            )

        test_skip_error = self._check_test_skip_guard_if_required(
            worktree,
            record,
            session_name,
            issue_number,
            run_assets,
        )
        if test_skip_error:
            return test_skip_error

        runtime_artifact_error = self._check_runtime_artifact_guard_if_required(
            worktree,
            record,
            session_name,
            issue_number,
            run_assets,
        )
        if runtime_artifact_error:
            return runtime_artifact_error

        return self._check_publish_gate_if_required(
            worktree,
            record,
            issue_number,
            run_assets,
        )

    def _read_and_validate_record(
        self,
        worktree: Path,
        completion_path: str | None,
        run_assets: SessionRunAssets,
    ) -> tuple[CompletionRecord | None, str | None, ProcessingResult | None]:
        """Read completion record and attach validation artifacts.

        Returns:
            Tuple of (record, session_name, error_result).
            If error_result is not None, caller should return it immediately.
        """
        record = self.read_completion_record(worktree, completion_path)
        if not record:
            return None, None, ProcessingResult(
                success=False,
                message="No completion record found",
                errors=["Completion record not found or invalid"],
            )

        session_name = self.session_output.session_name_from_path(completion_path) or record.session_id
        if record.validation_record_path and session_name:
            contained = _contain_validation_record_path(
                record.validation_record_path, worktree
            )
            if contained is not None:
                self._attach_validation_artifacts(
                    worktree,
                    run_assets.validation_artifacts,
                    record_path=contained,
                )
        self.session_output.attach_claude_log(run_assets.run_dir)

        return record, session_name, None

    def _handle_invalid_worktree_state(
        self,
        worktree: Path,
        record: CompletionRecord,
        session_name: str | None,
        issue_number: int,
        worktree_state: WorktreeValidationResult,
        run_assets: SessionRunAssets,
    ) -> ProcessingResult:
        logger.warning(
            "Completion worktree validation failed before publish: issue=%s reason=%s",
            issue_number,
            worktree_state.reason,
        )
        if worktree_state.failure == WorktreeValidationFailure.DIRTY_POLICY:
            return self._handle_gate_failure(
                worktree,
                record,
                session_name,
                issue_number,
                worktree_state.reason,
                gate_record=None,
                run_assets=run_assets,
            )

        tagged_reason = f"{ERROR_PREFIX_PUBLISH_BLOCKED}: {worktree_state.reason}"
        comment = build_processing_failure_comment(
            errors=[tagged_reason],
            actions_taken=[],
            diagnostic_path=None,
        )
        self._add_issue_comment(issue_number, comment, context="processing failure")
        return ProcessingResult(
            success=False,
            message=f"Validation failed: {worktree_state.reason}",
            errors=[tagged_reason],
        )

    def _check_test_skip_guard_if_required(
        self,
        worktree: Path,
        record: CompletionRecord,
        session_name: str | None,
        issue_number: int,
        run_assets: SessionRunAssets,
    ) -> ProcessingResult | None:
        """Reject newly added test-skip constructs before review/publish."""
        if not self._requires_publish_gate(record):
            return None

        base_ref = f"origin/{self._base_branch()}"
        diff_result = self.git_adapter.diff_against_base(worktree, base_ref)
        if not diff_result.success:
            return self._handle_gate_failure(
                worktree,
                record,
                session_name,
                issue_number,
                (
                    "Could not scan branch diff for banned test skips "
                    f"against {base_ref}: {diff_result.error or 'unknown git error'}"
                ),
                gate_record=None,
                run_assets=run_assets,
            )

        try:
            test_paths = added_test_paths(diff_result.diff_text)
        except ValueError as exc:
            return self._handle_gate_failure(
                worktree,
                record,
                session_name,
                issue_number,
                f"Could not parse branch diff for banned test skips: {exc}",
                gate_record=None,
                run_assets=run_assets,
            )
        if not test_paths:
            return None
        branch_files_result = self.git_adapter.read_branch_text_files(
            worktree, test_paths
        )
        if not branch_files_result.success:
            return self._handle_gate_failure(
                worktree,
                record,
                session_name,
                issue_number,
                (
                    "Could not read branch-tip test files for banned test-skip "
                    f"scan: {branch_files_result.error or 'unknown git error'}"
                ),
                gate_record=None,
                run_assets=run_assets,
            )
        try:
            scan = scan_added_test_skip_guards(
                diff_result.diff_text, branch_files_result.files
            )
        except ValueError as exc:
            return self._handle_gate_failure(
                worktree,
                record,
                session_name,
                issue_number,
                f"Could not scan branch-tip test files for banned test skips: {exc}",
                gate_record=None,
                run_assets=run_assets,
            )
        if scan.ok:
            return None
        return self._handle_gate_failure(
            worktree,
            record,
            session_name,
            issue_number,
            scan.reason(),
            gate_record=None,
            run_assets=run_assets,
        )

    def _check_runtime_artifact_guard_if_required(
        self,
        worktree: Path,
        record: CompletionRecord,
        session_name: str | None,
        issue_number: int,
        run_assets: SessionRunAssets,
    ) -> ProcessingResult | None:
        """Reject committed issue-orchestrator runtime artifacts before publish.

        Runtime outputs under ``.issue-orchestrator/`` (review-exchange prompts,
        persistent-pair recordings, validation records, …) must never enter an
        agent branch: when they do, the reviewer-worktree fast-forward checkout
        fails and a validated rework is turned into ``blocked-failed`` (#6659).
        Fail early here with an actionable message instead of letting the
        brittle reviewer-worktree checkout surface it opaquely mid-exchange.
        """
        if not self._requires_publish_gate(record):
            return None

        base_ref = f"origin/{self._base_branch()}"
        paths_result = self.git_adapter.branch_post_image_paths_against_base(
            worktree, base_ref
        )
        if not paths_result.success:
            return self._handle_gate_failure(
                worktree,
                record,
                session_name,
                issue_number,
                (
                    "Could not scan branch paths for runtime artifacts "
                    f"against {base_ref}: "
                    f"{paths_result.error or 'unknown git error'}"
                ),
                gate_record=None,
                run_assets=run_assets,
            )

        forbidden = forbidden_branch_runtime_artifacts(paths_result.paths)
        if not forbidden:
            return None
        return self._handle_gate_failure(
            worktree,
            record,
            session_name,
            issue_number,
            build_forbidden_runtime_artifact_reason(forbidden),
            gate_record=None,
            run_assets=run_assets,
        )

    def _check_publish_gate_if_required(
        self,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        run_assets: SessionRunAssets,
    ) -> ProcessingResult | None:
        """Check publish gate if actions require it.

        Returns:
            ProcessingResult if gate check failed, None if passed or not required.
        """
        if not self._requires_publish_gate(record):
            return None
        gate_session_name = run_assets.session_name
        session_output_dir = run_assets.run_dir

        gate_passed, gate_reason, gate_record = self._check_publish_gate(
            worktree, session_output_dir=session_output_dir
        )
        if not gate_passed:
            return self._handle_gate_failure(
                worktree,
                record,
                gate_session_name,
                issue_number,
                gate_reason,
                gate_record,
                run_assets=run_assets,
            )
        else:
            # Attach validation artifacts even on success
            if gate_record:
                record_path = ValidationRecordStore(worktree).get_record_path(gate_record.head_sha)
                self._attach_validation_artifacts(
                    worktree,
                    run_assets.validation_artifacts,
                    record=gate_record,
                    record_path=record_path,
                )
        return None

    def _handle_gate_failure(
        self,
        worktree: Path,
        record: CompletionRecord,
        session_name: str | None,
        issue_number: int,
        gate_reason: str,
        gate_record: ValidationRecord | None,
        *,
        run_assets: SessionRunAssets,
    ) -> ProcessingResult:
        """Handle publish gate failure."""
        if gate_record and session_name:
            record_path = ValidationRecordStore(worktree).get_record_path(gate_record.head_sha)
            self._attach_validation_artifacts(
                worktree,
                run_assets.validation_artifacts,
                record=gate_record,
                record_path=record_path,
            )
        # Was previously a free-form dict update with the legacy
        # `validation_failure_reason` (note the inconsistent name vs
        # `validation_reason` used elsewhere) and a bare
        # `validation_passed: False`. Route through the typed outcome so
        # the three legacy fields stay consistent and the typo doesn't drift
        # back in.
        self.session_output.update_validation_outcome(
            run_assets.run_dir,
            ValidationFailed(reason=gate_reason or "publish gate failed"),
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
        # Add validation-failed label so user knows why issue is stuck
        validation_failed_label = self._get_label("validation_failed")
        try:
            self.label_adapter.add_label(issue_number, validation_failed_label)
            logger.info(
                "Added '%s' label to issue #%d due to validation failure",
                validation_failed_label,
                issue_number,
            )
        except Exception as e:
            logger.warning(
                "Failed to add validation-failed label to issue #%d: %s",
                issue_number,
                e,
            )
        comment = build_gate_failure_comment(
            gate_reason=gate_reason,
            validation_failed_label=validation_failed_label,
        )
        self._add_issue_comment(issue_number, comment, context="validation failure")

        self._emit(
            SessionEvent.FAILED,
            issue_number,
            {
                "outcome": record.outcome.value,
                "gate_failure": gate_reason,
            },
        )
        return ProcessingResult(
            success=False,
            message=f"Validation failed: {gate_reason}",
            failure_kind="validation_failed",
            errors=[f"Validation: {gate_reason}"],
        )

    def _run_pre_publish_gate_if_required(
        self,
        *,
        plan: Any,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        issue_title: str,
        agent_label: str | None,
        actions_taken: list[str],
        errors: list[str],
        run_assets: SessionRunAssets,
    ) -> ProcessingResult | None:
        if self.pre_publish_gate is None:
            return None
        if RequestedAction.PUSH_BRANCH not in plan.ordered_actions:
            return None
        gate_session_name = run_assets.session_name

        result = self.pre_publish_gate.check(worktree)
        if not result.ran:
            return None
        if result.allowed:
            return None

        self._persist_pre_publish_failure_artifacts(
            run_assets=run_assets,
            result=result,
        )
        rerouted = self._reroute_pre_publish_validation_failure_if_possible(
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=gate_session_name,
            agent_label=agent_label,
            record=record,
            run_assets=run_assets,
        )
        if rerouted is not None:
            return rerouted
        return self._handle_gate_failure(
            worktree,
            record,
            gate_session_name,
            issue_number,
            result.reason,
            self._pre_publish_validation_record(run_assets, result),
            run_assets=run_assets,
        )

    def _reroute_pre_publish_validation_failure_if_possible(
        self,
        *,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        record: CompletionRecord,
        run_assets: SessionRunAssets,
    ) -> ProcessingResult | None:
        if session_name is None or agent_label is None:
            return None
        if RequestedAction.CREATE_PR not in record.requested_actions:
            return None

        validation_record_path = run_assets.validation_artifacts.record_path
        if not validation_record_path.exists():
            return None

        # Catch-all: bound consecutive reroutes per (session, head_sha) so a
        # permanently-failing validation can't form an infinite loop even if
        # the cache predicate is later weakened or bypassed. SHA advancing
        # naturally resets the counter (different key). Polling ticks
        # (background exchange still running from a prior submission) must
        # NOT consume the budget — they did no new work, and counting them
        # would halt a slow-but-progressing exchange before it finishes.
        if not self._review_exchange.is_review_exchange_running(
            issue_number=issue_number,
            session_name=session_name,
            run_id=run_assets.run_id,
        ):
            budget_exhausted_result = self._consume_validation_reroute_budget(
                session_name=session_name,
                validation_record_path=validation_record_path,
            )
            if budget_exhausted_result is not None:
                return budget_exhausted_result

        reroute_errors: list[str] = []
        reroute_actions: list[str] = []
        (
            exchange_mode,
            exchange_result,
            exchange_halt,
            deferred,
        ) = self._review_exchange.run_review_exchange_if_needed(
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            run_id=run_assets.run_id,
            agent_label=agent_label,
            initial_validation_record_path=validation_record_path,
            current_head_sha=current_worktree_head_sha(
                git_adapter=self.git_adapter,
                worktree=worktree,
            ),
            errors=reroute_errors,
            actions_taken=reroute_actions,
            run_review_exchange_loop=self._run_review_exchange_loop,
        )

        # The review-exchange helper enforces the configured max_rounds and
        # max_no_progress bounds. If it returns ``exchange_halt=True`` here,
        # this reroute must terminate loudly instead of re-entering forever.
        if exchange_halt:
            return ProcessingResult(
                success=False,
                message=(
                    "Validation failed after review approval and the follow-up "
                    "review exchange halted"
                ),
                errors=reroute_errors,
                actions_taken=reroute_actions,
                review_exchange_halted=True,
            )

        if deferred or (exchange_mode in {"via-mcp", "via-local-loop"} and exchange_result):
            resumed_actions = [
                "Validation failed; returned to coder rework via review exchange",
                *reroute_actions,
            ]
            return ProcessingResult(
                success=True,
                message=(
                    "Validation failed after review approval; "
                    "review exchange resumed to rework the failure"
                ),
                actions_taken=resumed_actions,
                errors=reroute_errors,
                review_exchange_deferred=True,
                validation_failed_rerouted=True,
            )

        return None

    def _consume_validation_reroute_budget(
        self,
        *,
        session_name: str,
        validation_record_path: Path,
    ) -> ProcessingResult | None:
        """Increment the per-(session, head_sha) reroute count and halt if exhausted.

        Returns a halting :class:`ProcessingResult` when the budget is spent,
        otherwise ``None`` to let the caller proceed.
        """
        head_sha = self._read_validation_head_sha(validation_record_path)
        if not head_sha:
            # No SHA on the failing record — can't key the counter, can't
            # safely bound the loop. Don't escalate from here; the in-loop
            # bounds (max_rounds / max_no_progress) still apply.
            return None
        max_attempts = (
            self._config.review_exchange_max_rounds if self._config is not None else 10
        )
        key = (session_name, head_sha)
        attempt = self._validation_reroute_counts.get(key, 0) + 1
        self._validation_reroute_counts[key] = attempt
        if attempt > max_attempts:
            logger.error(
                "[VALIDATION_REROUTE] budget exhausted: session=%s head_sha=%s "
                "attempts=%d max=%d — halting reroute",
                session_name,
                head_sha[:8],
                attempt,
                max_attempts,
            )
            return ProcessingResult(
                success=False,
                message=(
                    "Validation failed after review approval and the reroute "
                    f"budget is exhausted (attempts={attempt} max={max_attempts}); "
                    "halting to surface the failure"
                ),
                errors=[
                    f"validation_reroute: exhausted budget on {head_sha[:8]} "
                    f"(attempts={attempt}, max={max_attempts})"
                ],
                review_exchange_halted=True,
            )
        return None

    @staticmethod
    def _read_validation_head_sha(record_path: Path) -> str | None:
        try:
            data = json.loads(record_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        head_sha = data.get("head_sha")
        return head_sha if isinstance(head_sha, str) and head_sha else None

    def _persist_pre_publish_failure_artifacts(
        self,
        *,
        run_assets: SessionRunAssets,
        result: PrePublishGateResult,
    ) -> None:
        artifacts = run_assets.validation_artifacts
        run_dir = run_assets.run_dir
        stdout_path = artifacts.stdout_path
        stderr_path = artifacts.stderr_path
        stdout_path.write_text(result.stdout)
        stderr_path.write_text(result.stderr)
        record = self._pre_publish_validation_record(run_assets, result)
        record_path = artifacts.record_path
        record_path.write_text(json.dumps(record.to_dict(), indent=2) + "\n")
        self.session_output.update_manifest(
            run_dir,
            {
                "validation_record_path": str(record_path),
                "validation_stdout": str(stdout_path),
                "validation_stderr": str(stderr_path),
            },
        )

    def _pre_publish_validation_record(
        self,
        run_assets: SessionRunAssets,
        result: PrePublishGateResult,
    ) -> ValidationRecord:
        artifacts = run_assets.validation_artifacts
        return ValidationRecord(
            schema_version=1,
            suite="pre_publish_gate",
            head_sha=result.head_sha or "unknown",
            passed=False,
            exit_code=result.exit_code,
            command=result.command,
            started_at=result.started_at,
            ended_at=result.ended_at,
            timed_out=False,
            stdout_path=str(artifacts.stdout_path),
            stderr_path=str(artifacts.stderr_path),
        )

    def _execute_actions(
        self,
        *,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        issue_title: str,
        label_target: int,
        branch: str | None,
        session_name: str | None,
        agent_label: str | None,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        run_assets: SessionRunAssets,
    ) -> tuple[str | None, str | None, bool, bool, ProcessingResult | None]:
        """Execute all requested actions from completion record.

        Returns:
            Tuple of (final_branch, pr_url, review_exchange_completed, deferred, early_result).
            When ``deferred`` is True the review exchange is running in the
            background — callers must NOT treat the completion as finished.
        """
        pr_url: str | None = None
        requested_actions = tuple(record.requested_actions)
        cache_boundary_started_at = review_cache_boundary_started_at(
            session_output=self.session_output,
            run_assets=run_assets,
        )
        (
            plan,
            exchange_mode,
            exchange_result,
            review_exchange_completed,
            should_halt,
            deferred,
        ) = self._review_exchange.prepare_review_exchange(
            requested_actions=requested_actions,
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            run_id=run_assets.run_id,
            agent_label=agent_label,
            record=record,
            review_cache_boundary_started_at=cache_boundary_started_at,
            current_head_sha=current_worktree_head_sha(
                git_adapter=self.git_adapter,
                worktree=worktree,
            ),
            errors=errors,
            actions_taken=actions_taken,
            run_review_exchange_loop=self._run_review_exchange_loop,
            approval_gate=self._review_exchange_approval_gate(
                agent_label=agent_label,
                run_assets=run_assets,
            ),
        )
        if deferred:
            return branch, pr_url, review_exchange_completed, True, None
        if should_halt:
            return branch, pr_url, review_exchange_completed, False, None

        pre_publish_failure = self._run_pre_publish_gate_if_required(
            plan=plan,
            worktree=worktree,
            record=record,
            issue_number=issue_number,
            issue_title=issue_title,
            agent_label=agent_label,
            actions_taken=actions_taken,
            errors=errors,
            run_assets=run_assets,
        )
        if pre_publish_failure is not None:
            return branch, pr_url, review_exchange_completed, False, pre_publish_failure

        branch, pr_url, review_exchange_completed = self._execute_planned_actions(
            plan=plan,
            worktree=worktree,
            record=record,
            issue_number=issue_number,
            issue_title=issue_title,
            label_target=label_target,
            branch=branch,
            session_name=session_name,
            agent_label=agent_label,
            actions_taken=actions_taken,
            errors=errors,
            error_details=error_details,
            exchange_mode=exchange_mode,
            exchange_result=exchange_result,
            review_exchange_completed=review_exchange_completed,
        )
        return branch, pr_url, review_exchange_completed, False, None

    def _execute_planned_actions(
        self,
        *,
        plan: Any,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        issue_title: str,
        label_target: int,
        branch: str | None,
        session_name: str | None,
        agent_label: str | None,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        exchange_mode: str | None,
        exchange_result: Any | None,
        review_exchange_completed: bool,
    ) -> tuple[str | None, str | None, bool]:
        pr_url: str | None = None

        for action in plan.ordered_actions:
            result = self._execute_action_with_observability(
                action=action,
                worktree=worktree,
                record=record,
                issue_number=issue_number,
                issue_title=issue_title,
                label_target=label_target,
                branch=branch,
                session_name=session_name,
                agent_label=agent_label,
                actions_taken=actions_taken,
                errors=errors,
                error_details=error_details,
                exchange_mode=exchange_mode,
                exchange_result=exchange_result,
            )
            if result is None:
                continue
            if result.branch:
                branch = result.branch
            if result.pr_url:
                pr_url = result.pr_url
            if result.review_exchange_completed:
                review_exchange_completed = True
            if result.skip_remaining:
                continue
            if result.halt:
                logger.warning(
                    "Halting remaining actions for issue #%d due to action failure",
                    issue_number,
                )
                break
        return branch, pr_url, review_exchange_completed

    def _execute_action_with_observability(
        self,
        *,
        action: RequestedAction,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        issue_title: str,
        label_target: int,
        branch: str | None,
        session_name: str | None,
        agent_label: str | None,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        exchange_mode: str | None,
        exchange_result: Any | None,
    ) -> "_ActionResult | None":
        action_start = time.monotonic()
        logger.info("Executing action: %s for issue #%d", action.value, issue_number)
        if is_timeline_trace_enabled():
            logger.info(
                "[TIMELINE] completion.action_start issue=%s action=%s requested_actions=%s label_target=%s",
                issue_number,
                action.value,
                ",".join(a.value for a in record.requested_actions),
                label_target,
            )
        try:
            return self._execute_single_action(
                action=action,
                worktree=worktree,
                record=record,
                issue_number=issue_number,
                issue_title=issue_title,
                label_target=label_target,
                branch=branch,
                session_name=session_name,
                agent_label=agent_label,
                actions_taken=actions_taken,
                errors=errors,
                error_details=error_details,
                exchange_mode=exchange_mode,
                exchange_result=exchange_result,
            )
        except Exception as e:
            # A clean tech_lead audit has nothing to publish; that is success,
            # not publish-failure (ADR-0031 / #6768 B1).
            if self._is_tech_lead_session(agent_label) and is_benign_tech_lead_no_commits(action, e):
                logger.info("[tech_lead] clean audit, nothing to publish: issue=#%d", issue_number)
                return self._ActionResult(branch=branch)
            logger.exception(
                "Exception executing action %s for #%d: %s",
                action.value,
                issue_number,
                e,
            )
            errors.append(f"{action.value}: {e}")
            error_details.append({
                "action": action.value,
                "error": str(e),
                "exception_type": type(e).__name__,
                "traceback": traceback.format_exc(),
            })
            if action in {RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR}:
                self._emit_publish_failed(
                    issue_number=issue_number,
                    stage=action.value,
                    error=str(e),
                )
                return self._ActionResult(branch=branch, halt=True)
            return None
        finally:
            action_duration = time.monotonic() - action_start
            logger.info(
                "Action finished: %s for issue #%d in %.2fs",
                action.value,
                issue_number,
                action_duration,
            )
            if is_timeline_trace_enabled():
                logger.info(
                    "[TIMELINE] completion.action_end issue=%s action=%s elapsed=%.3f actions_taken=%s errors=%s",
                    issue_number,
                    action.value,
                    action_duration,
                    len(actions_taken),
                    len(errors),
                )

    @dataclass
    class _ActionResult:
        """Result of executing a single action."""

        halt: bool = False  # Stop processing remaining actions
        skip_remaining: bool = False  # Skip to next action (used by continue)
        branch: str | None = None  # Updated branch name
        pr_url: str | None = None  # PR URL if created
        review_exchange_completed: bool = False

    def _execute_single_action(
        self,
        *,
        action: RequestedAction,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        issue_title: str,
        label_target: int,
        branch: str | None,
        session_name: str | None,
        agent_label: str | None,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        exchange_mode: str | None,
        exchange_result: Any | None,
    ) -> "_ActionResult":
        """Execute a single action and return the result."""
        if action == RequestedAction.PUSH_BRANCH:
            return self._execute_push_action(
                worktree,
                issue_number,
                action,
                actions_taken,
                errors,
                error_details,
            )
        elif action == RequestedAction.CREATE_PR:
            return self._execute_create_pr_action(
                worktree=worktree,
                record=record,
                issue_number=issue_number,
                issue_title=issue_title,
                branch=branch,
                session_name=session_name,
                agent_label=agent_label,
                actions_taken=actions_taken,
                errors=errors,
                exchange_mode=exchange_mode,
                exchange_result=exchange_result,
            )
        elif action == RequestedAction.POST_COMMENT:
            return self._execute_post_comment_action(
                record=record,
                issue_number=issue_number,
                label_target=label_target,
                actions_taken=actions_taken,
            )
        else:
            label_result = self._execute_label_mutation_action(
                action=action,
                issue_number=issue_number,
                label_target=label_target,
                actions_taken=actions_taken,
            )
            if label_result is not None:
                return label_result

        return self._ActionResult()

    def _execute_post_comment_action(
        self,
        *,
        record: CompletionRecord,
        issue_number: int,
        label_target: int,
        actions_taken: list[str],
    ) -> "_ActionResult":
        """Execute post-comment action with optional review comment event."""
        if not record.comment_body:
            return self._ActionResult()

        comment_url = self.pr_adapter.add_comment(label_target, record.comment_body)
        actions_taken.append(f"Posted comment to #{label_target}")
        # If comment target differs from issue number, this is a PR-scoped review comment.
        if label_target != issue_number:
            self._emit_review_comment_added(
                issue_number=issue_number,
                pr_number=label_target,
                comment_url=comment_url,
                comment_body=record.comment_body,
            )
        return self._ActionResult()

    def _execute_label_mutation_action(
        self,
        *,
        action: RequestedAction,
        issue_number: int,
        label_target: int,
        actions_taken: list[str],
    ) -> "_ActionResult | None":
        """Execute label add/remove action variants."""
        label_actions: dict[RequestedAction, tuple[str, int, str]] = {
            RequestedAction.ADD_BLOCKED_LABEL: ("blocked", issue_number, "add"),
            RequestedAction.ADD_NEEDS_HUMAN_LABEL: ("needs_human", issue_number, "add"),
            RequestedAction.ADD_CODE_REVIEWED_LABEL: ("code_reviewed", label_target, "add"),
            RequestedAction.ADD_NEEDS_REWORK_LABEL: ("needs_rework", label_target, "add"),
            RequestedAction.REMOVE_NEEDS_REWORK_LABEL: ("needs_rework", label_target, "remove"),
            RequestedAction.REMOVE_CODE_REVIEW_LABEL: ("code_review", label_target, "remove"),
        }
        config = label_actions.get(action)
        if config is None:
            return None

        label_key, target_number, operation = config
        label = self._get_label(label_key)
        if is_timeline_trace_enabled():
            logger.info(
                "[TIMELINE] completion.label_mutation issue=%s action=%s operation=%s label_key=%s label=%s target=%s",
                issue_number,
                action.value,
                operation,
                label_key,
                label,
                target_number,
            )
        if operation == "add":
            self.label_adapter.add_label(target_number, label)
            if target_number == issue_number:
                actions_taken.append(f"Added '{label}' label")
            else:
                actions_taken.append(f"Added '{label}' label to #{target_number}")
        else:
            self.label_adapter.remove_label(target_number, label)
            actions_taken.append(f"Removed '{label}' label from #{target_number}")

        return self._ActionResult()

    def _execute_push_action(
        self,
        worktree: Path,
        issue_number: int,
        action: RequestedAction,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        *,
        skip_hooks: bool = False,
    ) -> "_ActionResult":
        """Execute push branch action."""
        skip_hooks = skip_hooks or os.environ.get("E2E_SKIP_PUSH_HOOKS") == "1"
        result = self.git_adapter.push(worktree, skip_hooks=skip_hooks)
        if result.success:
            actions_taken.append("Pushed branch to remote")
            logger.info("Push succeeded for #%d", issue_number)
            return self._ActionResult()

        # Handle push failure with potential rebase retry
        retry_result: PushResult | None = None
        if self._push_rebase_retry and self._is_non_fast_forward(result.message):
            retry_result = self._attempt_rebase_and_retry_push(
                worktree, issue_number, action, actions_taken, errors, error_details, skip_hooks
            )

        if retry_result and retry_result.success:
            actions_taken.append("Pushed branch to remote after rebase")
            logger.info("Push succeeded after rebase for #%d", issue_number)
            return self._ActionResult()

        # Push failed
        errors.append(f"{ERROR_PREFIX_PUSH}: Push failed: {result.message}")
        error_details.append({
            "action": action.value,
            "error": result.message,
            "retryable": result.retryable,
            "branch": result.branch,
            "remote": result.remote,
        })
        logger.error("Push failed for #%d: %s", issue_number, result.message)
        self._emit_publish_failed(
            issue_number=issue_number,
            stage=ERROR_PREFIX_PUSH,
            error=result.message,
            retryable=result.retryable,
            branch=result.branch,
        )
        return self._ActionResult(halt=True)

    def _resolve_push_rebase_base(
        self,
        issue_number: int,
        action: RequestedAction,
        errors: list[str],
        error_details: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        """Resolve the base a non-fast-forward push retry rebases onto (#6596).

        Returns ``(halt, base)``. For a stack successor the *work* gate (which
        omits the successor-vs-predecessor ancestry check, because the rebase is
        precisely what fixes ancestry) selects the predecessor base via the same
        stack owner the publish path uses. A blocked, ambiguous, or unreadable
        stack gate halts the retry fail-closed — the diagnostic is emitted — so
        the successor branch is never rebased onto and pushed on the default base
        before the publish gate can enforce stack correctness. Non-stack issues
        (and deployments with no gate) keep the normal base.
        """
        if self._stack_publish_gate is None:
            return False, self._base_branch()
        decision = self._stack_publish_gate.decide_work(issue_number)
        if not decision.allowed:
            reason = decision.reason or "stack work gate blocked"
            errors.append(f"{ERROR_PREFIX_PUSH}: {reason}")
            error_details.append(
                {"action": action.value, "error": reason, "stage": "stack_gate"}
            )
            logger.error("Stack push retry blocked for #%d: %s", issue_number, reason)
            self._emit_publish_failed(
                issue_number=issue_number,
                stage=ERROR_PREFIX_PUSH,
                error=reason,
                retryable=decision.retryable,
            )
            return True, ""
        if decision.base_branch:
            return False, decision.base_branch
        return False, self._base_branch()

    def _attempt_rebase_and_retry_push(
        self,
        worktree: Path,
        issue_number: int,
        action: RequestedAction,
        actions_taken: list[str],
        errors: list[str],
        error_details: list[dict[str, Any]],
        skip_hooks: bool,
    ) -> PushResult | None:
        """Attempt to rebase and retry push after non-fast-forward failure."""
        if self.git_adapter.has_uncommitted_changes(worktree):
            logger.warning(
                "Push retry skipped due to uncommitted changes: issue=%s",
                issue_number,
            )
            return None

        # Pick the rebase base through the stack base owner: a stack successor
        # rebases onto its predecessor branch (never the default base), and a
        # blocked/ambiguous/unreadable stack gate fails the retry closed.
        halt, rebase_base = self._resolve_push_rebase_base(
            issue_number, action, errors, error_details
        )
        if halt:
            return None

        rebase_result = self.git_adapter.rebase_on_branch(
            worktree,
            f"origin/{rebase_base}",
        )
        if rebase_result.success:
            actions_taken.append(f"Rebased onto origin/{rebase_base}")
            return self.git_adapter.push(worktree, skip_hooks=skip_hooks)

        errors.append(f"{ERROR_PREFIX_PUSH}: Rebase failed: {rebase_result.message}")
        error_details.append({
            "action": action.value,
            "error": rebase_result.message,
            "stage": "rebase",
            "conflicts": rebase_result.conflicts,
            "aborted": rebase_result.aborted,
        })
        return None

    def _resolve_publish_base(
        self,
        issue_number: int,
        worktree: Path,
        errors: list[str],
    ) -> tuple[bool, Callable[[], str], "StackBaseDecision | None"]:
        """Resolve the PR base for a (possibly stacked) successor (#6596).

        Returns ``(halt, base_resolver, decision)``. ``halt`` is True when the
        stack publish gate blocked publish (the diagnostic is already emitted),
        so the caller aborts PR creation. Otherwise ``base_resolver`` is the
        callable to pass as the PR base: the predecessor branch for a stack
        successor whose gate is open, or the processor's normal base for a
        non-stack issue (or when no gate is wired). ``decision`` is the raw gate
        verdict (``None`` when no gate is wired) so the reuse path can confirm an
        existing PR targets the same base the gate requires.

        The halt keys off ``not decision.allowed`` (not ``is_stack``): a
        fail-closed read error blocks publish even though the gate could not
        confirm the slice is a stack successor.
        """
        if self._stack_publish_gate is None:
            return False, self._base_branch, None
        decision = self._stack_publish_gate.decide_publish(issue_number, worktree)
        if not decision.allowed:
            reason = decision.reason or "stack publish gate blocked"
            errors.append(f"{ERROR_PREFIX_CREATE_PR}: {reason}")
            logger.error("Stack publish blocked for #%d: %s", issue_number, reason)
            self._emit_publish_failed(
                issue_number=issue_number,
                stage=ERROR_PREFIX_CREATE_PR,
                error=reason,
                retryable=decision.retryable,
            )
            return True, self._base_branch, decision
        if decision.base_branch:
            stacked_base = decision.base_branch
            logger.info(
                "Stack successor #%d PR will base on predecessor branch %s",
                issue_number,
                stacked_base,
            )
            return False, (lambda: stacked_base), decision
        return False, self._base_branch, decision

    def _execute_create_pr_action(
        self,
        *,
        worktree: Path,
        record: CompletionRecord,
        issue_number: int,
        issue_title: str,
        branch: str | None,
        session_name: str | None,
        agent_label: str | None,
        actions_taken: list[str],
        errors: list[str],
        exchange_mode: str | None,
        exchange_result: Any | None,
    ) -> "_ActionResult":
        """Execute create PR action."""
        if not branch:
            errors.append(f"{ERROR_PREFIX_CREATE_PR}: Cannot create PR - no branch")
            logger.error("Cannot create PR for #%d: no branch", issue_number)
            return self._ActionResult(skip_remaining=True)

        # Stack publish gate (ADR-0029 / #6596): for a Stack-after: successor,
        # base the PR on the predecessor branch and fail fast when the publish
        # gate is blocked. Non-stack issues keep the default base selection.
        halt, base_branch_resolver, stack_decision = self._resolve_publish_base(
            issue_number, worktree, errors
        )
        if halt:
            return self._ActionResult(halt=True)
        # Resolve the base once: it is both the base a fresh PR is created on and
        # the base an existing PR must already target before it can be reused.
        expected_base = base_branch_resolver()

        skip_hooks = os.environ.get("E2E_SKIP_PUSH_HOOKS") == "1"
        pr_title = f"#{issue_number}: {issue_title}"
        pr_body = build_pr_body(
            record,
            issue_number,
            runtime_identity=self._runtime_identity,
        )
        exchange_mode, exchange_resolution_failed = self._review_exchange.resolve_create_pr_exchange_mode(
            exchange_mode=exchange_mode,
            agent_label=agent_label,
            errors=errors,
        )
        if exchange_resolution_failed:
            return self._ActionResult(halt=True)
        if self._review_exchange.missing_review_exchange_outcome(exchange_mode, exchange_result):
            errors.append(
                f"{REVIEW_EXCHANGE_ERROR_PREFIX} missing exchange outcome before PR creation"
            )
            return self._ActionResult(halt=True)

        # Check for existing PR to reuse after review exchange succeeds.
        reused = self._reuse_existing_pr_if_available(
            issue_number=issue_number,
            branch=branch,
            exchange_mode=exchange_mode,
            exchange_result=exchange_result,
            actions_taken=actions_taken,
            errors=errors,
            expected_base=expected_base,
            stack_decision=stack_decision,
        )
        if reused is not None:
            return reused

        # Maybe switch branch for PR collision
        if self._pr_collision_strategy == "new_branch":
            branch = maybe_switch_branch_for_pr_collision(
                pr_adapter=self.pr_adapter,
                git_adapter=self.git_adapter,
                worktree=worktree,
                branch=branch,
                issue_number=issue_number,
                actions_taken=actions_taken,
                skip_hooks=skip_hooks,
            )

        # Create the PR
        logger.info("Creating PR for #%d: branch=%s", issue_number, branch)
        draft_pr = exchange_mode not in {"via-mcp", "via-local-loop"}
        pr = create_pr_with_collision_handling(
            pr_adapter=self.pr_adapter,
            git_adapter=self.git_adapter,
            base_branch=lambda: expected_base,
            pr_collision_strategy=self._pr_collision_strategy,
            worktree=worktree,
            pr_title=pr_title,
            pr_body=pr_body,
            branch=branch,
            issue_number=issue_number,
            actions_taken=actions_taken,
            skip_hooks=skip_hooks,
            draft=draft_pr,
        )

        if pr:
            # ``create_pr`` is idempotent by head branch, so it can return an
            # existing PR targeting the wrong base even when the issue-scoped
            # reuse preflight missed it. Enforce the stack base invariant on the
            # returned PR too — retarget through the owned op or fail closed —
            # before it inherits labels/review finalization (#6596 F2).
            base_failure = self._enforce_created_pr_base(
                pr=pr,
                stack_decision=stack_decision,
                expected_base=expected_base,
                issue_number=issue_number,
                actions_taken=actions_taken,
                errors=errors,
            )
            if base_failure is not None:
                return base_failure
            self._apply_pr_labels(pr, record, actions_taken)
            review_exchange_completed = False
            if exchange_mode in {"via-mcp", "via-local-loop"} and exchange_result:
                review_exchange_completed = True
                self._finalize_review_exchange_pr(
                    issue_number=issue_number,
                    pr_number=pr.number,
                    exchange_mode=exchange_mode,
                    exchange_result=exchange_result,
                    actions_taken=actions_taken,
                    run_assets=exchange_result.run_assets,
                )
            return self._ActionResult(
                branch=branch,
                pr_url=pr.url,
                review_exchange_completed=review_exchange_completed,
            )
        # Route None-without-raise through publish-failed observability, not a generic failure.
        reason = "PR creation returned no result"
        errors.append(f"{ERROR_PREFIX_CREATE_PR}: {reason}")
        logger.error("PR creation returned None for #%d: %s", issue_number, reason)
        self._emit_publish_failed(
            issue_number=issue_number,
            stage=ERROR_PREFIX_CREATE_PR,
            error=reason,
            branch=branch,
        )
        return self._ActionResult(branch=branch, halt=True)

    def _reuse_existing_pr_if_available(
        self,
        *,
        issue_number: int,
        branch: str,
        exchange_mode: str | None,
        exchange_result: Any | None,
        actions_taken: list[str],
        errors: list[str],
        expected_base: str,
        stack_decision: "StackBaseDecision | None",
    ) -> "_ActionResult | None":
        if self._pr_collision_strategy not in {"reuse_open", "new_branch"}:
            return None
        existing_pr = get_open_pr_for_issue(
            self.pr_adapter,
            issue_number,
            expected_branch=branch,
        )
        if not existing_pr:
            return None
        # Stack invariant (ADR-0029 / #6596): an existing PR may only be reused
        # if it already targets the base the stack publish gate requires. A
        # successor PR opened against ``main`` (or still on a now-merged
        # predecessor) must be retargeted through the owned repository operation
        # before it inherits review-completion labels and awaiting-merge
        # reconciliation; if the retarget cannot be performed, fail fast.
        if stack_decision is not None and stack_decision.is_stack:
            retarget = self._retarget_reused_pr_base(
                issue_number=issue_number,
                existing_pr=existing_pr,
                expected_base=expected_base,
                actions_taken=actions_taken,
                errors=errors,
            )
            if retarget is not None:
                return retarget
        actions_taken.append(f"Reused PR #{existing_pr.number}")
        logger.info(
            "Reused existing PR #%d for issue #%d: %s",
            existing_pr.number,
            issue_number,
            existing_pr.url,
        )
        review_exchange_completed = False
        if exchange_mode in {"via-mcp", "via-local-loop"} and exchange_result:
            review_exchange_completed = True
            self._finalize_review_exchange_pr(
                issue_number=issue_number,
                pr_number=existing_pr.number,
                exchange_mode=exchange_mode,
                exchange_result=exchange_result,
                actions_taken=actions_taken,
                run_assets=exchange_result.run_assets,
            )
        return self._ActionResult(
            pr_url=existing_pr.url,
            skip_remaining=True,
            review_exchange_completed=review_exchange_completed,
        )

    def _enforce_created_pr_base(
        self,
        *,
        pr: PRInfo,
        stack_decision: "StackBaseDecision | None",
        expected_base: str,
        issue_number: int,
        actions_taken: list[str],
        errors: list[str],
    ) -> "_ActionResult | None":
        """Enforce the stack base on a PR returned from the create/collision path.

        Returns ``None`` for a non-stack issue or when the PR already targets the
        gate's base (or was retargeted), and a halting :class:`_ActionResult` when
        a stack PR targets the wrong base and the owned retarget fails — closing
        the gap where an idempotent ``create_pr`` returns an existing wrong-base
        PR the issue-scoped reuse preflight missed (#6596 F2).
        """
        if stack_decision is None or not stack_decision.is_stack:
            return None
        return self._retarget_reused_pr_base(
            issue_number=issue_number,
            existing_pr=pr,
            expected_base=expected_base,
            actions_taken=actions_taken,
            errors=errors,
        )

    def _retarget_reused_pr_base(
        self,
        *,
        issue_number: int,
        existing_pr: PRInfo,
        expected_base: str,
        actions_taken: list[str],
        errors: list[str],
    ) -> "_ActionResult | None":
        """Ensure a reused stack-successor PR targets the gate's required base.

        Returns ``None`` when the PR already targets ``expected_base`` (reuse may
        proceed) or after a successful retarget. Returns a halting
        :class:`_ActionResult` when the PR targets the wrong base and the owned
        retarget operation fails — fail fast rather than reuse a PR pointed at
        the wrong branch.
        """
        if existing_pr.base_branch == expected_base:
            return None
        current = existing_pr.base_branch or "unknown"
        try:
            self.pr_adapter.set_pr_base(existing_pr.number, expected_base)
        except Exception as exc:
            reason = (
                f"existing PR #{existing_pr.number} targets base {current!r} but "
                f"stack publish gate requires {expected_base!r}; retarget failed: {exc}"
            )
            errors.append(f"{ERROR_PREFIX_CREATE_PR}: {reason}")
            logger.error("Stack PR reuse blocked for #%d: %s", issue_number, reason)
            self._emit_publish_failed(
                issue_number=issue_number,
                stage=ERROR_PREFIX_CREATE_PR,
                error=reason,
                retryable=False,
                branch=existing_pr.branch,
            )
            return self._ActionResult(halt=True)
        actions_taken.append(
            f"Retargeted PR #{existing_pr.number} base {current} -> {expected_base}"
        )
        logger.info(
            "Retargeted reused PR #%d for issue #%d: base %s -> %s",
            existing_pr.number,
            issue_number,
            current,
            expected_base,
        )
        return None

    def _finalize_review_exchange_pr(
        self,
        *,
        issue_number: int,
        pr_number: int,
        exchange_mode: str,
        exchange_result: Any,
        actions_taken: list[str],
        run_assets: ReviewExchangeRunAssets,
    ) -> None:
        """Apply review-exchange completion labels/comment to a PR."""
        label = self._get_label("code_reviewed")
        self.label_adapter.add_label(pr_number, label)
        actions_taken.append(f"Added '{label}' label to PR #{pr_number}")
        review_label = self._get_label("code_review")
        self.label_adapter.remove_label(pr_number, review_label)
        actions_taken.append(f"Removed '{review_label}' label from PR #{pr_number}")
        comment = (
            f"✅ Review completed via {exchange_mode} loop.\n\n"
            f"- Rounds: {exchange_result.rounds}\n"
            f"- Outcome: {exchange_result.reason}\n"
        )
        if self._config and self._config.review_exchange_require_validation:
            comment += "- Validation: required and passed\n"
        artifacts = review_artifacts_from_exchange_result(exchange_result)
        review_separator = "\n\n---\n\n"
        review_body_budget = GITHUB_COMMENT_BODY_LIMIT - len(
            comment.rstrip() + review_separator
        )
        review_body = build_review_exchange_pr_comment_body(
            issue_number=issue_number,
            run_dir=run_assets.run_dir,
            exchange_dir=run_assets.exchange_dir,
            artifacts=artifacts,
            review_artifact_reader=self._review_artifact_reader,
            max_chars=review_body_budget,
        )
        if review_body:
            comment = comment.rstrip() + review_separator + review_body
        comment_url = self.pr_adapter.add_comment(pr_number, comment)
        actions_taken.append(f"Posted review completion comment to PR #{pr_number}")
        self._emit_review_comment_added(
            issue_number=issue_number,
            pr_number=pr_number,
            comment_url=comment_url,
            comment_body=comment,
            run_dir=run_assets.run_dir,
        )

    def _resolve_review_exchange_mode(self, agent_label: str | None) -> str | None:
        return self._review_exchange.resolve_review_exchange_mode(agent_label)

    def _run_review_exchange_loop(
        self,
        *,
        exchange_run: ReviewExchangeRun,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        initial_validation_record_path: Path | None = None,
        approval_gate: "ReviewExchangeApprovalGate | None" = None,
    ) -> Any:
        return self._review_exchange.run_review_exchange_loop(
            exchange_run=exchange_run,
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=agent_label,
            initial_validation_record_path=initial_validation_record_path,
            approval_gate=approval_gate,
            events=self._trace_events,
            event_context=self._event_context,
        )

    def _apply_pr_labels(
        self,
        pr: PRInfo,
        record: CompletionRecord,
        actions_taken: list[str],
    ) -> None:
        """Apply extra labels to PR if specified."""
        actions_taken.append(f"Created PR #{pr.number}")
        logger.info("Created PR #%d: %s", pr.number, pr.url)

        # Skip for fake/dry-run PRs (numbers 90000-99999)
        is_dry_run_pr = 90000 <= pr.number <= 99999
        if record.pr_labels and not is_dry_run_pr:
            for label in record.pr_labels:
                self.label_adapter.add_label(pr.number, label)
                logger.info("Added label '%s' to PR #%d", label, pr.number)
            actions_taken.append(f"Added labels to PR: {record.pr_labels}")
        elif record.pr_labels and is_dry_run_pr:
            logger.info("[E2E_DRY_RUN] Skipping PR label addition for fake PR #%d", pr.number)

    def _cleanup_completion_record(
        self,
        worktree: Path,
        completion_path: str | None,
        issue_number: int,
    ) -> None:
        cleanup_completion_record(
            worktree=worktree,
            completion_path=completion_path,
            issue_number=issue_number,
            cleanup_record=self.cleanup_record,
            post_issue_comment=self._add_issue_comment,
        )

    def _is_non_fast_forward(self, message: str) -> bool:
        lower = message.lower()
        return any(
            marker in lower
            for marker in (
                "non-fast-forward",
                "fetch first",
                "rejected",
                "stale info",
            )
        )

    def cleanup_record(self, worktree: Path, completion_path: str | None = None) -> bool:
        """Remove the completion record after processing.

        Args:
            worktree: Path to the worktree.
            completion_path: Agent-specific path to completion.json (optional).

        Returns:
            True if successfully removed, False otherwise.
        """
        record_path = worktree / (completion_path or COMPLETION_RECORD_PATH)
        try:
            if record_path.exists():
                record_path.unlink()
                logger.debug(f"Removed completion record: {record_path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to remove completion record: {e}")
            return False
