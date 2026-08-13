"""CompletionHandler - session completion state-machine updates and events.

Owns the complex state updates when a session completes: state-machine
transitions (issue/session/review), trace-event emission, history entries,
and cleanup decisions.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional, Callable

if TYPE_CHECKING:
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine
    from ..domain.models import PendingReview, PendingRework, PendingTechLeadReview
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from ..ports.provider_resilience import ProviderErrorType
    from .open_issue_corpus import OpenIssueCorpusManager
    from .provider_availability import ProviderAvailabilityPolicy
    from .state_machine_manager import StateMachineManager
    from .label_manager import LabelManager

from ..domain.issue_key import StableIssueId
from ..domain.run_manifest import RunManifest
from ..infra.config import Config
from ..events import EventName
from ..infra.logging_config import log_context, get_repo_log_path
from ..domain.models import (
    Session,
    SessionStatus,
    SessionHistoryEntry,
    PendingCleanup,
    session_history_status_from_session_status,
)
from ..domain.session_key import TaskKind
from ..ports import EventSink,  make_trace_event, RepositoryHost, Issue
from ..ports.session_output import SessionOutput
from .actions import (
    Action,
    AddLabelAction,
    RemoveLabelAction,
)
from .completion_action_planner import (
    CompletionActionPlanner,
    has_review_exchange_errors,
)
from .tech_lead_completion import discard_tech_lead_authority_after_completion
from .invalid_record_actions import (
    failure_event_reason,
    invalid_record_event_fields,
)
from .reconciliation import build_expected_for_mutation
from .completion_history_status import resolve_history_status
from .retrospective_review_completion import retrospective_review_completion_actions
from .review_routing import should_queue_pr_review
from .session_run_resolution import resolve_session_run_dir
from pathlib import Path
from ..infra.run_audit import write_run_audit

logger = logging.getLogger(__name__)


class RunAuditTrigger(str, Enum):
    """How a run audit was requested."""

    LABEL = "label"
    TIMEOUT = "timeout"
    RUNTIME_THRESHOLD = "runtime-threshold"


class CleanupDisposition(str, Enum):
    """When completion should hand cleanup work to the planner."""

    DEFERRED = "deferred"
    IMMEDIATE = "immediate"
    NONE = "none"


@dataclass(frozen=True)
class CleanupDecision:
    """Cleanup disposition and its disposition-specific payload."""

    disposition: CleanupDisposition
    pending_cleanup: PendingCleanup | None = None

    def __post_init__(self) -> None:
        has_pending_cleanup = self.pending_cleanup is not None
        if has_pending_cleanup != (self.disposition is CleanupDisposition.DEFERRED):
            raise ValueError(
                "pending_cleanup must be present only for deferred cleanup"
            )

    @classmethod
    def deferred(cls, pending_cleanup: PendingCleanup) -> "CleanupDecision":
        return cls(CleanupDisposition.DEFERRED, pending_cleanup)

    @classmethod
    def immediate(cls) -> "CleanupDecision":
        return cls(CleanupDisposition.IMMEDIATE)

    @classmethod
    def none(cls) -> "CleanupDecision":
        return cls(CleanupDisposition.NONE)


@dataclass
class CompletionResult:
    """Result of processing a session completion."""

    history_entry: SessionHistoryEntry
    cleanup: CleanupDecision
    history_status: SessionStatus = SessionStatus.COMPLETED
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    should_queue_review: bool = False
    actions: tuple[Action, ...] = ()


class CompletionHandler:
    """Handles session completion state machine updates and event emission.

    Injected: config (cleanup/review settings), events (EventSink), repository_host,
    and the issue/session/review state-machine lookups.
    """

    def __init__(
        self,
        config: Config,
        events: EventSink,
        repository_host: RepositoryHost,
        get_issue_machine_fn: Callable[[Issue], Optional["IssueStateMachine"]],
        get_session_machine_fn: Callable[[str], Optional["SessionStateMachine"]],
        get_review_machine_fn: Callable[[int], Optional["ReviewStateMachine"]],
        session_output: SessionOutput,
        tech_lead_authority: "TechLeadAuthorityStore",
        open_issue_corpus: "OpenIssueCorpusManager",
        active_session_run_id: Callable[[int], str | None],
        provider_availability: "ProviderAvailabilityPolicy",
        remove_session_machine_fn: Callable[[str], None] | None = None,
        label_manager: "LabelManager | None" = None,
    ):
        self.config = config
        self.events = events
        self.repository_host = repository_host
        self._get_issue_machine = get_issue_machine_fn
        self._get_session_machine = get_session_machine_fn
        self._get_review_machine = get_review_machine_fn
        self._session_output = session_output
        self._tech_lead_authority = tech_lead_authority
        self._remove_session_machine = remove_session_machine_fn
        if label_manager is None:
            from .label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager
        self._action_planner = CompletionActionPlanner(
            config, repository_host, label_manager, tech_lead_authority,
            open_issue_corpus, active_session_run_id, provider_availability,
        )

    def mark_session_retry(self, session: Session, reason: str) -> None:
        """Mark a session terminal when it will be retried.

        Validation retries re-launch under the same name, so drive the existing
        machine to a terminal state first — the next launch builds a fresh one.
        """
        session_machine = self._get_session_machine(session.terminal_id)
        if not session_machine:
            return
        if session_machine.can_transition("fail"):
            logger.info(
                "[STATE_MACHINE] Session %s: RUNNING -> FAILED (reason: %s)",
                session.terminal_id,
                reason,
            )
            session_machine.fail(data={'reason': reason})  # type: ignore[attr-defined]
        if self._remove_session_machine is not None:
            self._remove_session_machine(session.terminal_id)

    def process_completion(
        self,
        session: Session,
        status: SessionStatus,
        pr_url_hint: Optional[str] = None,
        processing_errors: Optional[list[str]] = None,
        diagnostic_path: Optional[str] = None,
        review_exchange_completed: bool = False,
        review_exchange_halted: bool = False,
        blocked_label: Optional[str] = None,
        blocked_reason: Optional[str] = None,
        completion_detail: Optional[dict[str, Any]] = None,
        finalize_terminal: bool = True,
        provider_error_type: "ProviderErrorType | None" = None,
    ) -> CompletionResult:
        """Process a session completion and update all state machines.

        Returns a CompletionResult with the history entry and cleanup decision.
        With ``finalize_terminal=False`` the terminal trace event and the
        state-machine transition defer to ``finalize_terminal_outcome`` so the
        caller can drive both from the effective post-apply status (#6777).
        """
        start_time = time.monotonic()
        issue_key = session.key.issue.stable_id()
        logger.info(
            "Processing completion: issue=%s session=%s status=%s branch=%s worktree=%s",
            session.issue.number,
            session.terminal_id,
            status.value,
            session.branch_name,
            session.worktree_path,
            extra=log_context(issue_key=issue_key, session_id=session.terminal_id),
        )

        review_exchange_halted = review_exchange_halted or has_review_exchange_errors(processing_errors)

        # Fetch PR info if completed (or use hint from completion processor)
        pr_url, pr_number, pr_infos = self._fetch_pr_info(session, status, pr_url_hint=pr_url_hint)
        if pr_infos:
            self._emit_pr_view_changed(
                pr_infos[0],
                issue_key=session.key.issue.stable_id(),
                issue_number=session.issue.number,
            )
        elif pr_url and pr_number is not None:
            self._emit_pr_view_hint(
                pr_number,
                pr_url,
                issue_key=session.key.issue.stable_id(),
                issue_number=session.issue.number,
            )

        # What history shows is a policy of its own (a failed push makes a
        # self-reported "completed" a red dot), owned next door.
        history = resolve_history_status(
            status=status,
            issue_number=session.issue.number,
            pr_url=pr_url,
            processing_errors=processing_errors,
            review_exchange_halted=review_exchange_halted,
            completion_detail=completion_detail,
        )
        history_status = history.status
        history_entry = self._create_history_entry(
            session, history_status, pr_url, status_reason_override=history.reason
        )

        # The terminal trace event AND the cached state-machine transition are the
        # two terminal-outcome commits; both defer to finalize_terminal_outcome when
        # the caller finalizes post-apply from the EFFECTIVE status (#6777). Default
        # commits both here from history_status, exactly as before.
        if finalize_terminal:
            self.emit_trace_events(session, history_status, pr_url, pr_number, blocked_reason=blocked_reason, completion_detail=completion_detail)
            self._update_state_machines(session, history_status, pr_url)

        # Determine cleanup strategy
        cleanup = self._determine_cleanup_strategy(
            session, status, pr_url, pr_number
        )

        # Determine if we should queue code review
        should_queue_review = self._should_queue_review(
            session,
            status,
            pr_url,
            pr_number,
            review_exchange_completed=review_exchange_completed,
            review_exchange_halted=review_exchange_halted,
        )

        # Generate actions for label/comment changes (policy logic)
        completion_actions = list(
            self._action_planner.generate_completion_actions(
                session,
                status,
                processing_errors=processing_errors,
                diagnostic_path=diagnostic_path,
                review_exchange_halted=review_exchange_halted,
                blocked_label=blocked_label,
                blocked_reason=blocked_reason,
                pr_url=pr_url,
                completion_detail=completion_detail,
                provider_error_type=provider_error_type,
            )
        )
        completion_actions.extend(
            self._review_exchange_completion_actions(
                session,
                pr_url=pr_url,
                review_exchange_completed=review_exchange_completed,
            )
        )
        completion_actions.extend(
            retrospective_review_completion_actions(
                session=session,
                status=status,
                detail=completion_detail or {},
                config=self.config,
                label_manager=self._lm,
            )
        )
        completion_actions = tuple(completion_actions)

        if status in (
            SessionStatus.FAILED,
            SessionStatus.TIMED_OUT,
            SessionStatus.BLOCKED,
            SessionStatus.NEEDS_HUMAN,
        ) or processing_errors:
            log_path = get_repo_log_path(self.config.repo_root)
            run_dir = self._resolve_session_run_dir(session)
            self._session_output.write_orchestrator_tail(
                run_dir=run_dir,
                log_path=log_path,
                issue_number=session.issue.number,
                session_name=session.terminal_id,
            )

        # Enrich manifest with runtime context + log tail
        self._enrich_manifest_runtime(session, status)

        audit_actions = self._create_run_audit_and_actions(
            session,
            status,
            processing_errors=processing_errors,
        )
        if audit_actions:
            completion_actions = completion_actions + audit_actions

        # Retention (#6769 F3): completion finalization is this run's terminal
        # seam; publish-stage failures keep the row for Retry Publish.
        discard_tech_lead_authority_after_completion(
            self.config, self._tech_lead_authority, session,
            processing_errors=processing_errors,
        )

        result = CompletionResult(
            history_entry=history_entry,
            history_status=history_status,
            pr_url=pr_url,
            pr_number=pr_number,
            cleanup=cleanup,
            should_queue_review=should_queue_review,
            actions=completion_actions,
        )
        total_duration = time.monotonic() - start_time
        logger.info(
            "Completion processed: issue=%s session=%s status=%s pr_number=%s "
            "queue_review=%s cleanup=%s elapsed=%.2fs",
            session.issue.number,
            session.terminal_id,
            status.value,
            pr_number,
            should_queue_review,
            cleanup.disposition.value,
            total_duration,
            extra=log_context(issue_key=issue_key, session_id=session.terminal_id),
        )
        return result

    def _create_run_audit_and_actions(
        self,
        session: Session,
        status: SessionStatus,
        *,
        processing_errors: Optional[list[str]] = None,
    ) -> tuple[Action, ...]:
        """Persist a run audit and return any label actions it requires."""
        labels = self._fetch_issue_labels_for_audit(session.issue.number)
        run_dir = self._resolve_session_run_dir(session)

        trigger = self._resolve_run_audit_trigger(
            session.issue.number,
            status,
            run_dir,
            labels,
        )
        if trigger is None:
            return ()

        try:
            audit = write_run_audit(
                run_dir,
                issue_labels=labels,
                trigger_source=trigger.value,
                trigger_label=self._lm.run_audit_requested if trigger is RunAuditTrigger.LABEL else None,
                completion_label=self._lm.run_audit_completed if trigger is RunAuditTrigger.LABEL else None,
                trigger_threshold_minutes=(
                    self.config.review_run_audit_min_runtime_minutes
                    if trigger is RunAuditTrigger.RUNTIME_THRESHOLD
                    else None
                ),
                processing_errors=processing_errors,
            )
            self._session_output.update_manifest(run_dir, {"run_audit_path": str(audit.path)})
        except Exception:
            logger.warning(
                "[RUN_AUDIT] Failed for issue #%d run_dir=%s",
                session.issue.number,
                run_dir,
                exc_info=True,
            )
            return ()

        expected = build_expected_for_mutation()
        logger.info(
            "[RUN_AUDIT] Wrote %s for issue #%d status=%s trigger=%s",
            audit.path,
            session.issue.number,
            status.value,
            trigger.value,
        )
        if trigger is not RunAuditTrigger.LABEL:
            return ()
        return (
            RemoveLabelAction(
                issue_number=session.issue.number,
                label=self._lm.run_audit_requested,
                reason="Run audit captured for this session",
                expected=expected,
            ),
            AddLabelAction(
                issue_number=session.issue.number,
                label=self._lm.run_audit_completed,
                reason="Run audit captured for this session",
                expected=expected,
            ),
        )

    def _resolve_run_audit_trigger(
        self,
        issue_number: int,
        status: SessionStatus,
        run_dir: Path,
        labels: list[str],
    ) -> RunAuditTrigger | None:
        try:
            manifest = RunManifest.load(run_dir)
        except Exception as exc:
            logger.debug(
                "[RUN_AUDIT] Skipping audit trigger resolution for issue #%d run_dir=%s: %s",
                issue_number,
                run_dir,
                exc,
            )
            return None
        if manifest.run_audit_path and manifest.run_audit_path.strip():
            return None

        if (
            self._lm.run_audit_requested in labels
            and self._lm.run_audit_completed not in labels
        ):
            return RunAuditTrigger.LABEL

        if status is SessionStatus.TIMED_OUT and self.config.review_run_audit_on_timeout:
            return RunAuditTrigger.TIMEOUT

        threshold_minutes = self.config.review_run_audit_min_runtime_minutes
        if threshold_minutes <= 0:
            return None

        runtime_minutes = manifest.runtime_minutes
        if not isinstance(runtime_minutes, (int, float)):
            return None
        runtime_value = float(runtime_minutes)

        if runtime_value >= float(threshold_minutes):
            return RunAuditTrigger.RUNTIME_THRESHOLD

        logger.debug(
            "[RUN_AUDIT] Skipping automatic audit for issue #%d runtime=%.1f threshold=%d",
            issue_number,
            runtime_value,
            threshold_minutes,
        )
        return None

    def _fetch_issue_labels_for_audit(self, issue_number: int) -> list[str]:
        try:
            labels = self.repository_host.get_issue_labels_fresh(issue_number)
        except Exception as exc:
            logger.warning(
                "[RUN_AUDIT] Fresh label read failed for issue #%d: %s",
                issue_number,
                exc,
            )
            return []
        return [str(label) for label in labels]

    def _fetch_pr_info(
        self,
        session: Session,
        status: SessionStatus,
        pr_url_hint: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[int], Optional[list[Any]]]:
        """Fetch PR info for a completed session.

        Returns ``(pr_url, pr_number, prs_list)``; ``pr_url_hint`` short-circuits
        the branch lookup (dry-run mode).
        """
        pr_url = None
        pr_number = None
        prs = None

        if status != SessionStatus.COMPLETED:
            return pr_url, pr_number, prs

        if session.key.task == TaskKind.RETROSPECTIVE_REVIEW:
            return None, None, None

        if pr_url_hint:
            return self._fetch_pr_info_from_hint(session, pr_url_hint)

        return self._fetch_pr_info_from_branch_or_review_fallback(session)

    def _fetch_pr_info_from_hint(
        self,
        session: Session,
        pr_url_hint: str,
    ) -> tuple[Optional[str], Optional[int], Optional[list[Any]]]:
        pr_url = pr_url_hint
        pr_number: Optional[int] = None
        prs: Optional[list[Any]] = None

        match = re.search(r"/pull/(\d+)", pr_url)
        if match:
            pr_number = int(match.group(1))
            try:
                pr_info = self.repository_host.get_pr(pr_number)
            except Exception as e:
                logger.warning("Failed to fetch PR %s for PR hint: %s", pr_number, e)
            else:
                if pr_info:
                    prs = [pr_info]

        logger.info(
            "[PR_HINT] Using PR from completion processor: %s (number=%s)",
            pr_url,
            pr_number,
            extra=log_context(issue_key=session.key.issue.stable_id(), session_id=session.terminal_id),
        )
        return pr_url, pr_number, prs

    def _fetch_pr_info_from_branch_or_review_fallback(
        self,
        session: Session,
    ) -> tuple[Optional[str], Optional[int], Optional[list[Any]]]:
        logger.debug("[ADAPTER] Using GitHubAdapter for get_prs_for_branch")
        start = time.monotonic()
        pr_infos = self.repository_host.get_prs_for_branch(session.branch_name)
        duration = time.monotonic() - start
        logger.info(
            "Fetched PRs for branch in %.2fs: branch=%s count=%d",
            duration,
            session.branch_name,
            len(pr_infos),
            extra=log_context(issue_key=session.key.issue.stable_id(), session_id=session.terminal_id),
        )
        if pr_infos:
            return pr_infos[0].url, pr_infos[0].number, list(pr_infos)

        if session.pr_number is None:
            return None, None, None

        try:
            review_pr = self.repository_host.get_pr(session.pr_number)
        except Exception as e:
            logger.warning(
                "Failed to fetch PR %s for review session fallback: %s",
                session.pr_number,
                e,
            )
            return None, None, None

        if review_pr:
            return review_pr.url, review_pr.number, [review_pr]

        return None, None, None

    def _create_history_entry(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
        status_reason_override: Optional[str] = None,
    ) -> SessionHistoryEntry:
        """Create a session history entry.

        ``status_reason_override`` supplies the reason when the agent said
        completed but push/PR failed.
        """
        # Generate human-readable status reason
        status_reasons = {
            SessionStatus.COMPLETED: "Completed without PR",
            SessionStatus.BLOCKED: "Agent marked issue as blocked",
            SessionStatus.NEEDS_HUMAN: "Agent requested human input",
            SessionStatus.TIMED_OUT: f"Exceeded {session.agent_config.timeout_minutes} min timeout",
            SessionStatus.FAILED: "Session ended without PR or status update",
            SessionStatus.VALIDATION_FAILED: "Validation failed after session completion",
        }
        if status == SessionStatus.COMPLETED and pr_url:
            status_reasons[SessionStatus.COMPLETED] = "PR created successfully"
        status_reason = status_reason_override or status_reasons.get(status, "Unknown")

        return SessionHistoryEntry(
            issue_number=session.issue.number,
            title=session.issue.title,
            agent_type=session.issue.agent_type or "unknown",
            status=session_history_status_from_session_status(status),
            runtime_minutes=session.runtime_minutes,
            pr_url=pr_url,
            status_reason=status_reason,
            worktree_path=session.worktree_path,
            completed_at=datetime.now(timezone.utc),
            issue_labels=tuple(session.issue.labels),
        )

    def finalize_terminal_outcome(
        self,
        session: Session,
        effective_status: SessionStatus,
        pr_url: Optional[str],
        pr_number: Optional[int],
        *,
        blocked_reason: Optional[str] = None,
        completion_detail: Optional[dict[str, Any]] = None,
    ) -> None:
        """Commit BOTH terminal consumers from the ONE effective status post-apply.

        The terminal trace event and the cached ``SessionStateMachine`` transition
        are the two terminal-outcome commits. ``handle_session_completion`` defers
        both out of ``process_completion`` (``finalize_terminal=False``) and calls
        this once with ``effective_terminal_status(history_status, outcome)`` so a
        failed mandated reset ends the machine FAILED and emits one SESSION_FAILED —
        never a false COMPLETED neither consumer can retract (#6777).
        """
        self.emit_trace_events(
            session, effective_status, pr_url, pr_number,
            blocked_reason=blocked_reason, completion_detail=completion_detail,
        )
        self._update_state_machines(session, effective_status, pr_url)

    def emit_trace_events(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
        pr_number: Optional[int],
        *,
        blocked_reason: Optional[str] = None,
        completion_detail: Optional[dict[str, Any]] = None,
    ) -> None:
        """Emit the terminal/lifecycle trace event for a completion.

        Public so ``finalize_terminal_outcome`` drives it post-apply from the
        EFFECTIVE status (#6777).
        """
        detail = completion_detail or {}

        if status == SessionStatus.COMPLETED:
            self._emit_completed_events(session, pr_url, pr_number, detail)
        elif status in (SessionStatus.FAILED, SessionStatus.TIMED_OUT):
            self._emit_failure_event(session, status, detail)
        elif status == SessionStatus.BLOCKED:
            self._emit_blocked_event(session, blocked_reason, detail)
        elif status == SessionStatus.NEEDS_HUMAN:
            self._emit_needs_human_event(session, blocked_reason, detail)

    def _emit_completed_events(
        self,
        session: Session,
        pr_url: Optional[str],
        pr_number: Optional[int],
        detail: dict[str, Any],
    ) -> None:
        """Emit events for a completed session (coding/rework only)."""
        # Review sessions get their events from _publish_review_outcome().
        # Retrospective review sessions complete through label/state actions.
        if session.key.task in {TaskKind.REVIEW, TaskKind.RETROSPECTIVE_REVIEW}:
            return

        agent = session.agent_label
        task = session.key.task.value if session.key else None
        rework_cycle = session.rework_cycle

        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "session_id": session.terminal_id,
            "agent": agent,
            "task": task,
            "rework_cycle": rework_cycle,
            "pr_url": pr_url,
            "runtime_minutes": session.runtime_minutes,
        }
        completion_path_absolute = detail.get("completion_path_absolute")
        if isinstance(completion_path_absolute, str) and completion_path_absolute.strip():
            payload["completion_path_absolute"] = completion_path_absolute
        else:
            payload["completion_path_absolute"] = str((session.worktree_path / session.completion_path).resolve())
        run_dir = self._resolve_session_run_dir(session)
        payload["run_dir"] = str(run_dir)
        for key in ("implementation", "problems", "review_summary", "review_issues", "risk_level"):
            if detail.get(key):
                payload[key] = detail[key]
        self.events.publish(make_trace_event(EventName.SESSION_COMPLETED, payload))

        if pr_url and pr_number is not None:
            self.events.publish(make_trace_event(EventName.ISSUE_PR_CREATED, {
                "issue_number": session.issue.number,
                "pr_url": pr_url,
                "pr_number": pr_number,
                "agent": agent,
                "task": task,
                "rework_cycle": rework_cycle,
            }))

    def _emit_failure_event(
        self,
        session: Session,
        status: SessionStatus,
        detail: dict[str, Any],
    ) -> None:
        """Emit SESSION_FAILED event for failed or timed-out sessions."""
        reason = failure_event_reason(
            expired=status == SessionStatus.TIMED_OUT,
            timeout_minutes=session.agent_config.timeout_minutes,
            detail=detail,
        )
        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "session_id": session.terminal_id,
            "agent": session.agent_label,
            "task": session.key.task.value if session.key else None,
            "rework_cycle": session.rework_cycle,
            "error": reason,
            "runtime_minutes": session.runtime_minutes,
            "timeout_minutes": session.agent_config.timeout_minutes if session.agent_config else None,
        }
        payload.update(invalid_record_event_fields(detail))
        run_dir = self._resolve_session_run_dir(session)
        payload["run_dir"] = str(run_dir)
        self.events.publish(make_trace_event(EventName.SESSION_FAILED, payload))

    def _emit_blocked_event(
        self,
        session: Session,
        blocked_reason: Optional[str],
        detail: dict[str, Any],
    ) -> None:
        """Emit ISSUE_BLOCKED event."""
        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "agent": session.agent_label,
            "task": session.key.task.value if session.key else None,
            "rework_cycle": session.rework_cycle,
            "reason": blocked_reason or "Agent marked issue as blocked",
        }
        for key in ("attempted", "blocked_by"):
            if detail.get(key):
                payload[key] = detail[key]
        self.events.publish(make_trace_event(EventName.ISSUE_BLOCKED, payload))

    def _emit_needs_human_event(
        self,
        session: Session,
        blocked_reason: Optional[str],
        detail: dict[str, Any],
    ) -> None:
        """Emit ISSUE_NEEDS_HUMAN event."""
        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "agent": session.agent_label,
            "task": session.key.task.value if session.key else None,
            "rework_cycle": session.rework_cycle,
            "reason": blocked_reason or "Agent requested human input",
        }
        if detail.get("question"):
            payload["question"] = detail["question"]
        self.events.publish(make_trace_event(EventName.ISSUE_NEEDS_HUMAN, payload))

    def _update_state_machines(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
    ) -> None:
        """Update all relevant state machines for the session completion."""
        status_reason = {
            SessionStatus.COMPLETED: "PR created successfully",
            SessionStatus.BLOCKED: "Agent marked issue as blocked",
            SessionStatus.NEEDS_HUMAN: "Agent requested human input",
            SessionStatus.TIMED_OUT: f"Exceeded {session.agent_config.timeout_minutes} min timeout",
            SessionStatus.FAILED: "Session ended without PR or status update",
        }.get(status, "Unknown")

        logger.debug(f"[STATE_MACHINE] Triggering transitions for session {session.terminal_id}")

        # 1. Update session state machine
        self._update_session_machine(session, status, status_reason)

        # 2. Update issue state machine
        self._update_issue_machine(session, status, pr_url)

        # 3. Update review state machine
        is_review = session.terminal_id.startswith("review-")
        is_rework = session.terminal_id.startswith("rework-")
        if is_review and status == SessionStatus.COMPLETED:
            self._update_review_machine(session)
        elif is_rework and status == SessionStatus.COMPLETED:
            self._complete_rework_review_machine(session)

    def _update_session_machine(
        self,
        session: Session,
        status: SessionStatus,
        status_reason: str,
    ) -> None:
        """Update the session state machine."""
        session_machine = self._get_session_machine(session.terminal_id)
        if session_machine:
            logger.debug(f"[STATE_MACHINE] Found session machine for {session.terminal_id}")
            if status == SessionStatus.COMPLETED:
                logger.info(f"[STATE_MACHINE] Session {session.terminal_id}: RUNNING -> COMPLETED")
                session_machine.complete()  # type: ignore[attr-defined]
            elif status == SessionStatus.FAILED:
                logger.info(f"[STATE_MACHINE] Session {session.terminal_id}: RUNNING -> FAILED (reason: {status_reason})")
                session_machine.fail(data={'reason': status_reason})  # type: ignore[attr-defined]
            elif status == SessionStatus.TIMED_OUT:
                logger.info(f"[STATE_MACHINE] Session {session.terminal_id}: RUNNING -> TIMED_OUT")
                session_machine.timeout()  # type: ignore[attr-defined]
            elif status == SessionStatus.BLOCKED:
                logger.info(f"[STATE_MACHINE] Session {session.terminal_id}: RUNNING -> BLOCKED")
                session_machine.block()  # type: ignore[attr-defined]
            elif status == SessionStatus.NEEDS_HUMAN:
                logger.info(f"[STATE_MACHINE] Session {session.terminal_id}: RUNNING -> NEEDS_HUMAN")
                session_machine.needs_human()  # type: ignore[attr-defined]
        else:
            logger.debug(f"[STATE_MACHINE] No session machine found for {session.terminal_id} (may be restored session)")

    def _update_issue_machine(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
    ) -> None:
        """Update the issue state machine."""
        issue_machine = self._get_issue_machine(session.issue)
        if issue_machine:
            logger.debug(f"[STATE_MACHINE] Found issue machine for issue #{session.issue.number}")
            # Only trigger pr_created for issue sessions (not review/rework sessions)
            # Review/rework sessions work on issues that already have PRs
            is_issue_session = session.terminal_id.startswith("issue-")
            if status == SessionStatus.COMPLETED and pr_url and is_issue_session:
                if issue_machine.can_transition("pr_created"):
                    logger.info(
                        "[STATE_MACHINE] Issue #%d: IN_PROGRESS -> PR_PENDING (PR: %s)",
                        session.issue.number,
                        pr_url,
                    )
                    issue_machine.pr_created(data={'pr_url': pr_url})  # type: ignore[attr-defined]
                else:
                    logger.warning(
                        "[STATE_MACHINE] Issue #%d pr_created ignored (state=%s)",
                        session.issue.number,
                        issue_machine.get_state().value,
                    )
            elif status == SessionStatus.BLOCKED:
                logger.info(f"[STATE_MACHINE] Issue #{session.issue.number}: IN_PROGRESS -> BLOCKED")
                issue_machine.block()  # type: ignore[attr-defined]
            elif status == SessionStatus.NEEDS_HUMAN:
                logger.info(f"[STATE_MACHINE] Issue #{session.issue.number}: IN_PROGRESS -> NEEDS_HUMAN")
                issue_machine.needs_human()  # type: ignore[attr-defined]
        else:
            logger.debug(f"[STATE_MACHINE] No issue machine found for issue #{session.issue.number} (may be restored session)")

    def _update_review_machine(self, session: Session) -> None:
        """Update the review state machine for a completed review session."""
        match = re.match(r"review-(\d+)", session.terminal_id)
        if not match:
            return

        pr_number_review = int(match.group(1))
        review_machine = self._get_review_machine(pr_number_review)
        if not review_machine:
            logger.debug(f"[STATE_MACHINE] No review machine found for PR #{pr_number_review}")
            return

        logger.debug(f"[STATE_MACHINE] Found review machine for PR #{pr_number_review}")
        try:
            pr_info = self.repository_host.get_pr(pr_number_review)
            if pr_info:
                self._emit_pr_view_changed(pr_info, issue_key=session.key.issue.stable_id(), issue_number=session.issue.number)
                self._process_review_outcome(pr_info, pr_number_review, review_machine)
                # Publish review outcome events from state machine transitions
                self._publish_review_outcome(review_machine, session, pr_number_review)
        except Exception as e:
            logger.warning(f"Failed to check PR labels for review outcome: {e}")
            self.events.publish(make_trace_event(EventName.APPLY_FAILED, {
                "step_type": "review_outcome_check", "pr_number": pr_number_review,
                "issue_number": session.issue.number, "error": str(e),
            }))

    def _complete_rework_review_machine(self, session: Session) -> None:
        """Advance the review state machine after a rework session completes.

        Transitions: REWORK_PENDING → REWORK_IN_PROGRESS → IN_REVIEW
        This allows the next review to transition from IN_REVIEW properly.
        """
        pr_number = session.pr_number
        if not pr_number:
            logger.debug("[STATE_MACHINE] Rework session %s has no pr_number, skipping review machine update", session.terminal_id)
            return

        review_machine = self._get_review_machine(pr_number)
        if not review_machine:
            logger.debug("[STATE_MACHINE] No review machine found for PR #%d (rework session %s)", pr_number, session.terminal_id)
            return

        # Transition through start_rework and complete_rework
        if review_machine.can_transition("start_rework"):
            logger.info("[STATE_MACHINE] PR #%d: REWORK_PENDING -> REWORK_IN_PROGRESS", pr_number)
            review_machine.start_rework()  # type: ignore[attr-defined]
        if review_machine.can_transition("complete_rework"):
            logger.info("[STATE_MACHINE] PR #%d: REWORK_IN_PROGRESS -> IN_REVIEW", pr_number)
            review_machine.complete_rework()  # type: ignore[attr-defined]

    def _publish_review_outcome(
        self,
        review_machine: Any,
        session: Session,
        pr_number: int,
    ) -> None:
        """Publish review.approved or review.changes_requested events.

        These events are defined in EventName but were never emitted.
        The ReviewStateMachine stores the outcome in last_transition after
        approve() or request_changes() — we read it and publish.
        """
        tr = review_machine.last_transition
        if not tr:
            return

        _REVIEW_OUTCOME_EVENTS = {"review.approved", "review.changes_requested"}
        if tr.event_name not in _REVIEW_OUTCOME_EVENTS:
            return

        payload: dict[str, Any] = {
            **tr.data,
            "pr_number": pr_number,
            "reviewer_agent": session.agent_label,
            "rework_cycle": session.rework_cycle,
        }
        self.events.publish(make_trace_event(EventName(tr.event_name), payload))

    def _process_review_outcome(self, pr_info: Any, pr_number: int, review_machine: Any) -> None:
        """Process review outcome based on PR labels."""
        labels = pr_info.labels
        if self.config.code_reviewed_label and self.config.code_reviewed_label in labels:
            self._handle_review_approved(pr_info, pr_number, review_machine)
        elif self._lm.needs_rework in labels:
            self._handle_changes_requested(pr_number, review_machine)

    def _handle_review_approved(self, pr_info: Any, pr_number: int, review_machine: Any) -> None:
        """Handle approved review outcome."""
        logger.info(f"[STATE_MACHINE] PR #{pr_number}: IN_REVIEW -> APPROVED")
        if getattr(pr_info, "draft", None) is True:
            try:
                self.repository_host.set_pr_draft(pr_number, False)
                logger.info("[STATE_MACHINE] PR #%d marked ready for review", pr_number)
            except Exception as e:
                logger.warning("Failed to mark PR #%d ready for review: %s", pr_number, e)
        self._try_transition(review_machine, "approve", pr_number)

    def _handle_changes_requested(self, pr_number: int, review_machine: Any) -> None:
        """Handle changes requested review outcome."""
        logger.info(f"[STATE_MACHINE] PR #{pr_number}: IN_REVIEW -> CHANGES_REQUESTED")
        self._try_transition(review_machine, "request_changes", pr_number)
        if review_machine.can_transition("queue_rework"):
            logger.info(f"[STATE_MACHINE] PR #{pr_number}: CHANGES_REQUESTED -> REWORK_PENDING")
            review_machine.queue_rework()  # type: ignore[attr-defined]
        else:
            logger.warning("[STATE_MACHINE] PR #%d queue_rework ignored (state=%s)", pr_number, review_machine.get_state().value)

    def _try_transition(self, machine: Any, transition: str, pr_number: int) -> None:
        """Try to perform a state machine transition."""
        if machine.can_transition(transition):
            getattr(machine, transition)()
        else:
            logger.warning("[STATE_MACHINE] PR #%d %s ignored (state=%s)", pr_number, transition, machine.get_state().value)

    def _emit_pr_view_changed(
        self,
        pr_info: Any,
        issue_key: StableIssueId | None,
        issue_number: int | None,
    ) -> None:
        payload = {
            "pr_number": pr_info.number, "pr_url": getattr(pr_info, "url", None),
            "labels": list(getattr(pr_info, "labels", []) or []),
        }
        if issue_key is not None:
            payload["issue_key"] = issue_key
        if issue_number is not None:
            payload["issue_number"] = issue_number
        self.events.publish(make_trace_event(EventName.PR_VIEW_CHANGED, payload))

    def _emit_pr_view_hint(
        self,
        pr_number: int,
        pr_url: str,
        issue_key: StableIssueId,
        issue_number: int,
    ) -> None:
        payload = {
            "pr_number": pr_number, "labels": [], "pr_url": pr_url,
            "issue_key": issue_key, "issue_number": issue_number,
        }
        self.events.publish(make_trace_event(EventName.PR_VIEW_CHANGED, payload))

    def _determine_cleanup_strategy(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
        pr_number: Optional[int],
    ) -> CleanupDecision:
        """Select deferred, immediate, or no cleanup for this completion."""
        if status != SessionStatus.COMPLETED or session.scratch_worktree:
            return CleanupDecision.immediate()

        if not self._cleanup_actions_requested():
            return CleanupDecision.none()

        is_work_session = session.key.task not in {
            TaskKind.REVIEW,
            TaskKind.RETROSPECTIVE_REVIEW,
            TaskKind.REWORK,
        }

        if (
            is_work_session
            and pr_url
            and pr_number
            and self._should_wait_for_review_before_cleanup()
        ):
            pending_cleanup = PendingCleanup(
                issue=session.issue,
                pr_number=pr_number,
                pr_url=pr_url,
                branch_name=session.branch_name,
                terminal_id=session.terminal_id,
                worktree_path=session.worktree_path,
            )
            logger.info(f"[CLEANUP] Deferred cleanup for #{session.issue.number} until review completes")
            return CleanupDecision.deferred(pending_cleanup)

        return CleanupDecision.immediate()

    def _should_wait_for_review_before_cleanup(self) -> bool:
        """Return whether this workflow gates cleanup on review."""
        if self.config.tech_lead_enabled:
            return True
        if self.config.code_review_agent:
            return self.config.cleanup.without_tech_lead.wait_for_code_review
        return False

    def _cleanup_actions_requested(self) -> bool:
        """Return whether ordinary completion has a selected cleanup action."""
        if self.config.tech_lead_enabled:
            cleanup = self.config.cleanup.with_tech_lead
        else:
            cleanup = self.config.cleanup.without_tech_lead
        return cleanup.close_ai_session_tabs or cleanup.remove_worktrees

    def _should_queue_review(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: Optional[str],
        pr_number: Optional[int] = None,
        review_exchange_completed: bool = False,
        review_exchange_halted: bool = False,
    ) -> bool:
        """Determine if session should be added to discovered_reviews.

        Note: This returns True even for dry-run PRs (so pr-pending label gets added).
        The actual review queuing is controlled by the planner, which skips dry-run PRs.
        """
        is_review_session = session.key.task in {
            TaskKind.REVIEW,
            TaskKind.RETROSPECTIVE_REVIEW,
        }
        should_queue = should_queue_pr_review(
            has_pr=bool(pr_url),
            code_review_agent_configured=bool(self.config.code_review_agent),
            skip_review=session.agent_config.skip_review,
            is_review_session=is_review_session,
            review_exchange_completed=review_exchange_completed,
            review_exchange_halted=review_exchange_halted,
        )
        if review_exchange_completed:
            logger.info(
                "[REVIEW] Review exchange completed - skipping PR review queue",
            )
        elif review_exchange_halted:
            logger.info(
                "[REVIEW] Review exchange halted - skipping PR review queue",
            )
        elif should_queue:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed with PR, queuing code review")
        elif pr_url and is_review_session:
            logger.info(f"[REVIEW] Review session {session.terminal_id} completed - no re-queue needed")
        elif pr_url and not self.config.code_review_agent:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed but code review not configured")
        elif pr_url and session.agent_config.skip_review:
            logger.info(f"[REVIEW] Session #{session.issue.number} skipping review (skip_review=true)")
        elif not pr_url:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed but no PR found")

        return should_queue

    def _review_exchange_completion_actions(
        self,
        session: Session,
        *,
        pr_url: str | None,
        review_exchange_completed: bool,
    ) -> tuple[Action, ...]:
        """Return label actions after an approved local review exchange."""
        if not review_exchange_completed or not pr_url:
            return ()
        if session.key.task in {TaskKind.REVIEW, TaskKind.RETROSPECTIVE_REVIEW}:
            return ()
        return (
            AddLabelAction(
                issue_number=session.issue.number,
                label=self._lm.pr_pending,
                reason="review exchange completed - awaiting merge",
                expected=build_expected_for_mutation(),
            ),
        )

    def _enrich_manifest_runtime(
        self,
        session: Session,
        status: SessionStatus,
    ) -> None:
        """Write runtime context and log tail into the run manifest.

        Best-effort — failures are logged but never block completion.
        """
        from ..domain.run_manifest import RunManifest

        run_dir = self._resolve_session_run_dir(session)

        try:
            manifest = RunManifest.load(run_dir)
        except Exception as exc:
            logger.warning(
                "[MANIFEST] Failed to load manifest for runtime enrichment: %s", exc,
            )
            return

        manifest.runtime_minutes = session.runtime_minutes
        if session.agent_config:
            manifest.timeout_minutes = session.agent_config.timeout_minutes
        if manifest.outcome is None:
            manifest.outcome = status.value
        if manifest.ended_at is None:
            manifest.ended_at = datetime.now(timezone.utc).isoformat()

        # Capture log tail for all outcomes
        log_path = self._session_output.get_log_path_for_run_dir(run_dir)
        if isinstance(log_path, Path) and log_path.exists():
            try:
                content = log_path.read_text()
                lines = content.strip().split("\n")
                manifest.log_tail = "\n".join(lines[-20:])
            except Exception as exc:
                logger.debug("[MANIFEST] Could not read log tail: %s", exc)

        try:
            manifest.save()
        except Exception as exc:
            logger.warning("[MANIFEST] Failed to save runtime enrichment: %s", exc)

    def _resolve_session_run_dir(self, session: Session) -> Path:
        """Resolve run_dir for events/diagnostics."""
        return resolve_session_run_dir(self._session_output, session)

def launch_review_by_number(
    n: int, pending_reviews: list["PendingReview"],
    launch_review_session_fn: Callable[["PendingReview"], Optional["Session"]],
) -> Optional["Session"]:
    """Launch review session by number - moved per method table."""
    r = next((r for r in pending_reviews if r.pr_number == n), None)
    return launch_review_session_fn(r) if r else None


def launch_rework_by_number(
    n: int, pending_reworks: list["PendingRework"],
    launch_rework_session_fn: Callable[["PendingRework"], Optional["Session"]],
) -> Optional["Session"]:
    """Launch rework session by number - moved per method table."""
    r = next((r for r in pending_reworks if r.resolve_issue_number() == n), None)
    return launch_rework_session_fn(r) if r else None


def get_review_machine(pr: int, issue: int, state_machines: "StateMachineManager") -> Optional["ReviewStateMachine"]:
    """Get review state machine - moved per method table."""
    return state_machines.get_review_machine(pr, issue)


def launch_tech_lead_by_number(
    n: int, pending_tech_lead_reviews: list["PendingTechLeadReview"],
    launch_tech_lead_session_fn: Callable[["PendingTechLeadReview"], Optional["Session"]],
) -> Optional["Session"]:
    """Launch tech_lead session by number - moved per method table.

    Queue lifecycle (removal vs retention) is owned by the launch wrapper
    (``orchestrator_launch_tech_lead_session``), like the review/rework lookups.
    """
    t = next((t for t in pending_tech_lead_reviews if t.issue_number == n), None)
    return launch_tech_lead_session_fn(t) if t else None
