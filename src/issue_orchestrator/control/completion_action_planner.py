"""Action planning policy for session completion outcomes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from ..domain.models import RETROSPECTIVE_REVIEW_TERMINAL_PREFIX, Session, SessionStatus
from ..infra.config import Config
from ..ports import RepositoryHost
from ..ports.tech_lead_authority import TechLeadAuthorityStore
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .open_issue_corpus import OpenIssueCorpusManager
from .tech_lead_completion import (
    generate_tech_lead_completion_actions,
    generate_tech_lead_decision_failure_actions,
    generate_tech_lead_failure_actions,
    has_tech_lead_decision_errors,
)
from .completion_types import (
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUBLISH_BLOCKED,
    ERROR_PREFIX_PUSH,
    ERROR_PREFIX_TECH_LEAD_AUTHORITY,
    ERROR_PREFIX_TECH_LEAD_DECISION,
    REVIEW_EXCHANGE_ERROR_PREFIX,
)
from .invalid_record_actions import (
    invalid_record_actions,
    invalid_record_allows_interrupted_retry,
)
from .label_manager import LabelManager
from .provider_availability import ProviderAvailabilityPolicy
from .provider_blocked_completion import provider_blocked_actions
from .reconciliation import ExpectedState, build_expected_for_mutation
from .tech_lead_session_policy import is_tech_lead_session
from ..ports.provider_resilience import ProviderErrorType
from .needs_human_block import NeedsHumanCause

logger = logging.getLogger(__name__)


def critical_processing_errors(
    processing_errors: Optional[list[str]],
    *,
    pr_url: str | None = None,
    issue_number: int | None = None,
    log_downgraded: bool = False,
    context: str = "completion",
) -> tuple[list[str], list[str]]:
    """Return (critical, downgraded) publish/finalize errors.

    A create_pr error is only critical if completion reconciliation cannot find
    a PR. GitHub can still surface a transient 422 even when the PR was
    ultimately created or an equivalent open PR is discoverable.
    """
    if not processing_errors:
        return [], []

    critical: list[str] = []
    downgraded: list[str] = []
    for error in processing_errors:
        if error.startswith(
            (
                ERROR_PREFIX_PUSH,
                ERROR_PREFIX_PUBLISH_BLOCKED,
                ERROR_PREFIX_TECH_LEAD_DECISION,
                ERROR_PREFIX_TECH_LEAD_AUTHORITY,
            )
        ):
            critical.append(error)
            continue
        if error.startswith(ERROR_PREFIX_CREATE_PR):
            if pr_url:
                downgraded.append(error)
            else:
                critical.append(error)
    if downgraded and log_downgraded and issue_number is not None:
        logger.info(
            "[COMPLETION] Ignoring non-blocking create_pr processing errors: "
            "context=%s issue=%d pr_url=%s errors=%s",
            context,
            issue_number,
            pr_url,
            downgraded,
        )
    return critical, downgraded


def has_review_exchange_errors(processing_errors: Optional[list[str]]) -> bool:
    """Check if processing_errors contains review exchange halt/failure markers."""
    if not processing_errors:
        return False
    return any(
        error.startswith(REVIEW_EXCHANGE_ERROR_PREFIX) for error in processing_errors
    )


class CompletionActionPlanner:
    """Plans label/comment actions for completion outcomes."""

    def __init__(
        self,
        config: Config,
        repository_host: RepositoryHost,
        label_manager: LabelManager,
        tech_lead_authority: TechLeadAuthorityStore,
        open_issue_corpus: OpenIssueCorpusManager,
        active_session_run_id: Callable[[int], str | None],
        provider_availability: "ProviderAvailabilityPolicy",
    ) -> None:
        self.config = config
        self.repository_host = repository_host
        self._lm = label_manager
        self._tech_lead_authority = tech_lead_authority
        self._open_issue_corpus = open_issue_corpus
        # The only way this planner is allowed to move the provider-blocked
        # label: through the owner command that carries the durable
        # issue-scoped record with it (#5980 F1 / #6999 F5/A2).
        self._provider_availability = provider_availability
        # Resolves the target issue's live session run id so a gated
        # kill_hung_session proposal binds approval to that generation (#6779 R1).
        self._active_session_run_id = active_session_run_id

    def _interrupted_retry_mode(self, session: Session) -> str | None:
        """Map session type to interrupted-retry mode."""
        if session.terminal_id.startswith(
            "issue-"
        ) or session.terminal_id.startswith("rework-"):
            return "coding"
        if session.terminal_id.startswith(
            ("review-", RETROSPECTIVE_REVIEW_TERMINAL_PREFIX)
        ):
            return "review"
        return None

    def _interrupted_retry_guard_label(self, mode: str) -> str:
        retry_cfg = self.config.retry.interrupted_sessions
        if mode == "coding":
            return retry_cfg.coding_guard_label
        return retry_cfg.review_guard_label

    def _is_interrupted_retry_enabled(self, mode: str) -> bool:
        retry_cfg = self.config.retry.interrupted_sessions
        if not retry_cfg.enabled:
            return False
        if mode == "coding":
            return retry_cfg.retry_coding
        if mode == "review":
            return retry_cfg.retry_review
        return False

    def _issue_has_label(self, issue_number: int, label: str) -> bool:
        """Best-effort label check from GitHub to guard retry loops."""
        try:
            issue = self.repository_host.get_issue(issue_number)
            if not issue:
                return False
            return label in issue.labels
        except Exception as exc:
            logger.warning(
                "[COMPLETION] Failed to read labels for issue #%d while evaluating interrupted retry: %s",
                issue_number,
                exc,
            )
            return False

    def _generate_interrupted_retry_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action] | None:
        """Generate auto-retry actions for interrupted sessions when configured."""
        mode = self._interrupted_retry_mode(session)
        if mode is None or not self._is_interrupted_retry_enabled(mode):
            return None

        guard_label = self._interrupted_retry_guard_label(mode)
        if self._issue_has_label(session.issue.number, guard_label):
            logger.info(
                "[COMPLETION] Interrupted auto-retry skipped for issue #%d (%s): guard label already present (%s)",
                session.issue.number,
                mode,
                guard_label,
            )
            return None

        session_kind = session.terminal_id.split("-", 1)[0]
        actions: list[Action] = [
            AddLabelAction(
                issue_number=session.issue.number,
                label=guard_label,
                reason=f"interrupted {mode} session auto-retry guard",
                expected=expected,
            ),
            AddCommentAction(
                number=session.issue.number,
                comment=(
                    f"🔁 **{session_kind.capitalize()} Session Interrupted**\n\n"
                    f"The {session_kind} session exited without a valid completion record "
                    "(`completion command`).\n\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                    f"- Session: `{session.terminal_id}`\n\n"
                    "Auto-retry is enabled, so this will be retried on the next scheduler cycle.\n"
                    f"A guard label (`{guard_label}`) was added to prevent retry loops."
                ),
                reason=f"Notify interrupted {mode} session auto-retry",
                expected=expected,
            ),
        ]
        if session.terminal_id.startswith("issue-"):
            actions.append(
                RemoveLabelAction(
                    issue_number=session.issue.number,
                    label=self._lm.in_progress,
                    reason="Interrupted issue session - releasing claim for auto-retry",
                    expected=expected,
                )
            )
        return actions

    def _is_tech_lead_session(self, session: Session) -> bool:
        """Check if this session is a tech_lead review session."""
        return is_tech_lead_session(
            self.config.tech_lead_review_agent, session.issue.agent_type
        )

    def _generate_tech_lead_actions(
        self, session: Session, expected: ExpectedState
    ) -> list[Action]:
        """Delegate batch-success tech_lead effects to the ADR-0031 owner module.

        Called only from the COMPLETED-without-critical-errors branch, so
        completed_ok is True by construction (tech_lead decision rejections are
        classified critical upstream and take the failure routing instead).
        """
        return generate_tech_lead_completion_actions(
            self.config,
            session,
            expected,
            completed_ok=True,
            labels=self._lm,
            tech_lead_authority=self._tech_lead_authority,
            open_issue_corpus=self._open_issue_corpus,
            active_session_run_id=self._active_session_run_id,
        )

    def _generate_tech_lead_failure_actions(
        self, session: Session, expected: ExpectedState
    ) -> list[Action]:
        """Delegate batch failure/timeout terminal effects to the owner module."""
        return generate_tech_lead_failure_actions(
            self.config, session, expected, tech_lead_authority=self._tech_lead_authority
        )

    def _generate_completed_with_critical_actions(
        self,
        session: Session,
        critical_errors: list[str],
        diagnostic_path: Optional[str],
        expected: ExpectedState,
    ) -> tuple[Action, ...]:
        """Route COMPLETED-with-critical-errors to the owning failure policy.

        Rejected tech_lead decision pairs (#6761 finding 3) go to the tech_lead
        owner (manifest tech-lead-failed labels, rejection surfacing,
        blocked-failed on the session's own issue) — publish-failure copy
        and publish-fail counters do not apply to them.
        """
        logger.info(
            "[COMPLETION] Agent said completed but processing failed: issue=%d errors=%s",
            session.issue.number,
            critical_errors,
        )
        if self._is_tech_lead_session(session) and has_tech_lead_decision_errors(
            critical_errors
        ):
            return tuple(
                generate_tech_lead_decision_failure_actions(
                    self.config,
                    session,
                    expected,
                    processing_errors=critical_errors,
                    labels=self._lm,
                    tech_lead_authority=self._tech_lead_authority,
                )
            )
        return tuple(
            self._generate_processing_failure_actions(
                session, critical_errors, diagnostic_path, expected
            )
        )

    def generate_completion_actions(
        self,
        session: Session,
        status: SessionStatus,
        processing_errors: Optional[list[str]] = None,
        diagnostic_path: Optional[str] = None,
        review_exchange_halted: bool = False,
        blocked_label: Optional[str] = None,
        blocked_reason: Optional[str] = None,
        pr_url: Optional[str] = None,
        completion_detail: Optional[dict[str, Any]] = None,
        provider_error_type: ProviderErrorType | None = None,
    ) -> tuple[Action, ...]:
        """Generate label/comment actions for session completion.

        This encapsulates the POLICY logic for what labels to add/remove
        when a session completes with various statuses.

        ``provider_error_type`` carries the typed verdict a provider-caused
        block ended on. It is what routes the block to the provider-impact
        owner instead of generic blocked handling, for every session kind.
        """
        expected = build_expected_for_mutation()

        # Check for critical processing errors (push/PR creation failures).
        critical_errors, _downgraded_errors = critical_processing_errors(
            processing_errors,
            pr_url=pr_url,
            issue_number=session.issue.number,
            log_downgraded=True,
            context="actions",
        )

        # If agent said "completed" but critical processing failed, treat as blocked-failed.
        if status == SessionStatus.COMPLETED and critical_errors:
            return self._generate_completed_with_critical_actions(
                session, critical_errors, diagnostic_path, expected
            )

        if status == SessionStatus.COMPLETED and review_exchange_halted:
            logger.info(
                "[COMPLETION] Review exchange halted - generating blocked-failed actions: issue=%d",
                session.issue.number,
            )
            return tuple(self._generate_review_exchange_halted_actions(session, expected))

        if status == SessionStatus.TIMED_OUT:
            timeout_actions = self._generate_timeout_actions(session, expected)
            timeout_actions.extend(self._generate_tech_lead_failure_actions(session, expected))
            return tuple(timeout_actions)

        if status == SessionStatus.FAILED:
            detail = completion_detail
            if malformed_actions := self._maybe_malformed_record_relaunch_actions(
                session,
                expected,
                detail,
            ):
                return tuple(malformed_actions)
            invalid_actions = invalid_record_actions(
                session=session,
                expected=expected,
                labels=self._lm,
                detail=completion_detail,
                diagnostic_path=diagnostic_path,
            )
            if invalid_actions is not None:
                return tuple(invalid_actions)
            # Interrupted auto-retry relaunches the session: not terminal, so
            # no tech_lead failure effects (the retry re-audits the same PRs).
            if retry_actions := self._generate_interrupted_retry_actions(session, expected):
                return tuple(retry_actions)
            failure_actions = self._generate_failure_actions(session, expected)
            failure_actions.extend(self._generate_tech_lead_failure_actions(session, expected))
            return tuple(failure_actions)

        if status == SessionStatus.BLOCKED:
            return tuple(
                self._generate_blocked_actions(
                    session,
                    expected,
                    blocked_label=blocked_label,
                    blocked_reason=blocked_reason,
                    provider_error_type=provider_error_type,
                )
            )

        if status == SessionStatus.COMPLETED:
            # POLICY: Completion -> release in-progress (claim maintained via pr-pending).
            actions: list[Action] = [
                RemoveLabelAction(
                    issue_number=session.issue.number,
                    label=self._lm.in_progress,
                    reason="Session completed successfully",
                    expected=expected,
                )
            ]
            actions.extend(self._generate_tech_lead_actions(session, expected))
            return tuple(actions)

        # NEEDS_HUMAN keeps in-progress to maintain the ownership claim.
        return ()

    def _generate_processing_failure_actions(
        self,
        session: Session,
        critical_errors: list[str],
        diagnostic_path: Optional[str],
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate actions when agent said completed but push/PR creation failed.

        Tracks consecutive publish failures via publish-fail-count-N labels.
        After max_consecutive_publish_failures, escalates to needs-human.
        """
        issue_number = session.issue.number
        in_progress_label = self._lm.in_progress

        # Count previous consecutive publish failures from issue labels.
        prev_count = self._lm.extract_publish_fail_count(session.issue.labels)
        new_count = prev_count + 1
        max_failures = self.config.max_consecutive_publish_failures

        first_error = critical_errors[0][:100] if critical_errors else "Unknown error"
        if len(first_error) == 100:
            first_error += "..."

        diagnostic_info = ""
        if diagnostic_path and session.worktree_path:
            worktree_name = Path(session.worktree_path).name
            diagnostic_info = (
                f"\n**Diagnostic file:** `{worktree_name}/{diagnostic_path}`\n"
            )

        if new_count >= max_failures:
            logger.info(
                "[COMPLETION] Publish failure count %d >= max %d, escalating to needs-human: issue=%d",
                new_count,
                max_failures,
                issue_number,
            )
            return [
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._lm.needs_human,
                    reason=f"Publishing failed {new_count} consecutive times — escalating to needs-human",
                    needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
                    expected=expected,
                ),
                AddCommentAction(
                    number=issue_number,
                    comment=(
                        "❌ **Publishing Failed — Escalated**\n\n"
                        f"Publishing has failed **{new_count} consecutive times** "
                        f"(max: {max_failures}). This issue needs human investigation.\n\n"
                        f"**Latest error:** {first_error}\n"
                        f"{diagnostic_info}\n"
                        f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                        f"- Session: `{session.terminal_id}`\n"
                    ),
                    reason="Escalate repeated publish failure to human",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=in_progress_label,
                    reason="Publishing failed - releasing claim",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=self._lm.needs_rework,
                    reason="Publishing failed - clearing needs-rework to prevent re-queuing loop",
                    expected=expected,
                ),
            ]

        actions: list[Action] = [
            AddLabelAction(
                issue_number=issue_number,
                label=self._lm.publish_failed,
                reason="Publishing failed after agent completion (push/PR creation failed)",
                expected=expected,
            ),
            AddCommentAction(
                number=issue_number,
                comment=(
                    f"❌ **Publishing Failed** (attempt {new_count}/{max_failures})\n\n"
                    "The agent completed its work, but the orchestrator could not push or create a PR.\n\n"
                    f"**Error:** {first_error}\n"
                    f"{diagnostic_info}\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                    f"- Session: `{session.terminal_id}`\n\n"
                    f"This issue has been marked as `{self._lm.publish_failed}` and will not be automatically retried.\n"
                    "Remove the label to retry."
                ),
                reason="Notify about processing failure",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=in_progress_label,
                reason="Processing failed - releasing claim",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=self._lm.needs_rework,
                reason="Publishing failed - clearing needs-rework to prevent re-queuing loop",
                expected=expected,
            ),
        ]

        if prev_count > 0:
            actions.append(
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=self._lm.publish_fail_count_label(prev_count),
                    reason="Updating publish failure count",
                    expected=expected,
                )
            )
        actions.append(
            AddLabelAction(
                issue_number=issue_number,
                label=self._lm.publish_fail_count_label(new_count),
                reason=f"Publish failure #{new_count}",
                expected=expected,
            )
        )

        return actions

    def _generate_timeout_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate actions when session timed out."""
        issue_number = session.issue.number
        in_progress_label = self._lm.in_progress
        is_issue_session = session.terminal_id.startswith("issue-")
        session_kind = session.terminal_id.split("-", 1)[0]

        if is_issue_session:
            timeout_mins = (
                session.agent_config.timeout_minutes
                if session.agent_config
                else "unknown"
            )
            return [
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._lm.blocked_failed,
                    reason=f"Session timed out after {session.runtime_minutes} minutes",
                    expected=expected,
                ),
                AddCommentAction(
                    number=issue_number,
                    comment=(
                        "⏱️ **Session Timed Out**\n\n"
                        f"The agent session exceeded the {timeout_mins} minute timeout limit.\n\n"
                        f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                        f"- Session: `{session.terminal_id}`\n\n"
                        f"This issue has been marked as `{self._lm.blocked_failed}` and will not be automatically retried.\n"
                        "Remove the label to allow reprocessing."
                    ),
                    reason="Notify about session timeout",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=in_progress_label,
                    reason="Session timed out - releasing claim",
                    expected=expected,
                ),
            ]
        return [
            AddCommentAction(
                number=issue_number,
                comment=(
                    f"⏱️ **{session_kind.capitalize()} Session Timed Out**\n\n"
                    f"The {session_kind} session exceeded its timeout and did not produce an outcome.\n\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                    f"- Session: `{session.terminal_id}`\n\n"
                    "The PR remains pending; review will be retried automatically."
                ),
                reason=f"Notify about {session_kind} session timeout",
                expected=expected,
            ),
        ]

    def _maybe_malformed_record_relaunch_actions(
        self,
        session: Session,
        expected: ExpectedState,
        detail: Optional[dict[str, Any]],
    ) -> list[Action] | None:
        """Return relaunch actions when malformed output matches interruption policy."""
        allowed = invalid_record_allows_interrupted_retry(detail)
        return self._generate_interrupted_retry_actions(session, expected) if allowed else None

    def _generate_failure_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate terminal actions when a session failed without a completion
        command (interrupted auto-retry is decided by the caller)."""
        issue_number = session.issue.number
        in_progress_label = self._lm.in_progress
        is_issue_session = session.terminal_id.startswith("issue-")
        session_kind = session.terminal_id.split("-", 1)[0]

        if is_issue_session:
            return [
                AddLabelAction(
                    issue_number=issue_number,
                    label=self._lm.needs_human,
                    reason="Session terminated without calling completion command (mandatory)",
                    needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
                    expected=expected,
                ),
                AddCommentAction(
                    number=issue_number,
                    comment=(
                        "🔍 **Session Needs Investigation**\n\n"
                        "The agent session terminated without calling the completion command "
                        "(`coding-done` or `reviewer-done`).\n\n"
                        "**This is unexpected** - the completion command is mandatory and must be called "
                        "to complete any session (completed, blocked, or needs_human).\n\n"
                        f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                        f"- Session: `{session.terminal_id}`\n\n"
                        "**Possible causes:**\n"
                        "- Agent crashed or was interrupted\n"
                        "- Orchestrator shutdown/restart interrupted the session lifecycle\n"
                        "- Agent ignored the mandatory completion command requirement\n"
                        "- Infrastructure issue prevented completion\n\n"
                        f"This issue has been marked as `{self._lm.needs_human}` for investigation.\n"
                        "Remove the label after investigating to allow reprocessing."
                    ),
                    reason="Notify about session needing human investigation",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=in_progress_label,
                    reason="Session failed - releasing claim",
                    expected=expected,
                ),
            ]
        return [
            AddCommentAction(
                number=issue_number,
                comment=(
                    f"🔍 **{session_kind.capitalize()} Session Needs Investigation**\n\n"
                    f"The {session_kind} session terminated without calling the completion command.\n\n"
                    "**This is unexpected** - the completion command is mandatory.\n\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                    f"- Session: `{session.terminal_id}`\n\n"
                    "Possible causes include orchestrator shutdown/restart, agent crash, or workflow interruption.\n\n"
                    "The PR remains pending; please investigate what happened."
                ),
                reason=f"Notify about {session_kind} session needing investigation",
                expected=expected,
            ),
        ]

    def _generate_blocked_actions(
        self,
        session: Session,
        expected: ExpectedState,
        blocked_label: Optional[str] = None,
        blocked_reason: Optional[str] = None,
        provider_error_type: ProviderErrorType | None = None,
    ) -> list[Action]:
        """Generate actions for a BLOCKED completion.

        Two routes, decided here rather than by the caller so "what a block
        means" has one owner: a typed provider verdict is an outage impacting
        the issue, and anything else is the agent reporting it cannot proceed.
        """
        if provider_error_type is not None:
            return provider_blocked_actions(
                session,
                expected,
                label_manager=self._lm,
                provider_availability=self._provider_availability,
            )
        is_issue_session = session.terminal_id.startswith("issue-")
        label = blocked_label or self._lm.blocked

        if is_issue_session:
            reason_text = (
                blocked_reason.strip() if blocked_reason else "No reason provided."
            )
            return [
                AddLabelAction(
                    issue_number=session.issue.number,
                    label=label,
                    reason="Agent reported issue as blocked",
                    expected=expected,
                ),
                AddCommentAction(
                    number=session.issue.number,
                    comment=(
                        "🚧 **Session Blocked**\n\n"
                        "The agent reported this issue as blocked.\n\n"
                        f"**Reason:** {reason_text}\n"
                        f"- Runtime: {session.runtime_minutes:.1f} minutes\n"
                        f"- Session: `{session.terminal_id}`\n\n"
                        f"This issue has been marked as `{label}` and will not be automatically retried.\n"
                        "Remove the label to allow reprocessing."
                    ),
                    reason="Notify about blocked session and reason",
                    expected=expected,
                ),
                RemoveLabelAction(
                    issue_number=session.issue.number,
                    label=self._lm.in_progress,
                    reason="Session blocked - releasing claim",
                    expected=expected,
                ),
            ]
        # Review/rework BLOCKED completions do not map to issue-blocking labels;
        # their parent workflows own any PR/review state transitions.
        return []

    def _generate_review_exchange_halted_actions(
        self,
        session: Session,
        expected: ExpectedState,
    ) -> list[Action]:
        """Generate hold actions when a review exchange halts without progress."""
        issue_number = session.issue.number
        return [
            AddLabelAction(
                issue_number=issue_number,
                label=self._lm.blocked_failed,
                reason="Review exchange halted with no progress",
                expected=expected,
            ),
            AddCommentAction(
                number=issue_number,
                comment=(
                    "⚠️ **Review Exchange Halted**\n\n"
                    "The automated review exchange stopped because it could not make further progress.\n\n"
                    f"- Session: `{session.terminal_id}`\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n\n"
                    f"This issue has been marked as `{self._lm.blocked_failed}` and will not be retried automatically.\n"
                    "Use Retry/Unblock when you want to run it again."
                ),
                reason="Notify that review exchange halted and issue is on hold",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=self._lm.in_progress,
                reason="Review exchange halted - releasing claim",
                expected=expected,
            ),
        ]
