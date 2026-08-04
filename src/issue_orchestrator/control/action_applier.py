"""ActionApplier - executes actions via ports/adapters.

This is the IO boundary for the orchestrator. It:
1. Takes Action objects (the plan)
2. Executes them via injected ports
3. Emits trace events for each action
4. Returns ActionResults

When reconciliation is enabled (reconcile=True with fresh_issue_reader provided):
- Before any label mutation, fetches current labels
- Verifies current state is as expected
- Aborts with ReconciliationRequired if mismatch
- Only proceeds with mutation if state matches

Usage:
    applier = ActionApplier(
        labels=label_set,
        sessions=session_manager,
        events=event_sink,
        repository_host=github_adapter,  # For issue creation, label sync
        worktree_manager=git_worktree_manager,  # For worktree removal
        fresh_issue_reader=github_fresh_reader,  # Optional, for reconciliation
        reconcile=True,  # Enable reconciliation
    )
    results = applier.apply_all(actions)
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Optional, Sequence, TypeVar

from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink,  make_trace_event
from ..ports.label_set import LabelSet
from ..ports.fresh_issue_reader import FreshIssueReader
from ..ports.repository_host import RepositoryHost
from ..ports.worktree_manager import WorktreeManager
from ..domain.models import RETROSPECTIVE_REVIEW_TERMINAL_PREFIX, Session

if TYPE_CHECKING:
    from .background_job_supervisor import BackgroundJobSupervisor
    from .label_manager import LabelManager
    from .review_exchange_lifecycle import IssueRuntimeTermination
    from .review_exchange_lifecycle import PublishRetryAbandoner
    from .review_exchange_lifecycle import ReviewExchangeCancellation
    from ..ports.label_store import LabelStore
    from ..ports.persistent_exchange_pair_registry import (
        PersistentExchangePairRegistry,
    )
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .retry_history_state import ExpediteLane
    from .session_history import SessionHistoryOwner
    from .tech_lead_kill_session import TechLeadKillSessionExecutor
    from .tech_lead_reset_retry import TechLeadResetRetryExecutor
from .reconciliation import (
    ExternalSnapshot,
    ReconciliationRequired,
    require_reconciliation,
)
from .claim_gate import ClaimGate, ClaimLostError
from .review_exchange_lifecycle import (
    cancel_issue_review_exchange,
    terminate_issue_runtime,
)
from .close_on_merge import run_close_on_merge_fallback
from .actions import (
    Action,
    ActionResult,
    ActionType,
    AddLabelAction,
    RemoveLabelAction,
    SyncLabelsAction,
    ShedRecoveredWorkflowLabelsAction,
    LaunchSessionAction,
    LaunchValidationRetryAction,
    StopSessionAction,
    QueueReviewAction,
    EnqueueToMergeQueueAction,
    EscalateToHumanAction,
    AddCommentAction,
    SupersedePullRequestAction,
    CloseIssueAction,
    SetIssueStateAction,
    CreateTechLeadIssueAction,
    KillHungSessionAction,
    SurfaceTechLeadProposalAction,
    CleanupSessionAction,
    RemoveWorktreeAction,
    ReconcileHistoryEntryAction,
    RecoverTerminalIssueAction,
    ResetRetryIssueAction,
)
from .session_manager import SessionManager, SessionRef, SessionType, SessionContext
from .tech_lead_applier_handlers import tech_lead_action_handlers
from .tech_lead_issue_creation import apply_create_tech_lead_issue
from .history_reconciliation import apply_history_reconciliation
from .tech_lead_proposals import execute_approved_tech_lead_op
from .tech_lead_reset_retry import apply_surface_tech_lead_proposal

logger = logging.getLogger(__name__)

# Type alias for session launcher callback
# Takes (session_type, number) and returns Optional[Session]
# This allows orchestrator to inject entity lookup + SessionLauncher
SessionLauncherCallback = Callable[[SessionType, int], Optional[Session]]
ValidationRetryLauncherCallback = Callable[[int], Optional[Session]]

# Type alias for lease_id lookup callback
# Takes issue_number and returns lease_id if active session exists
LeaseIdLookup = Callable[[int], str | None]
# Act-level tech_lead op actions share one dispatch shape (#6764/#6778).
_TechLeadOpAction = TypeVar("_TechLeadOpAction", ResetRetryIssueAction, KillHungSessionAction)
LabelMutationStatField = Literal[
    "label_add_attempted",
    "label_add_applied",
    "label_add_noop",
    "label_remove_attempted",
    "label_remove_applied",
    "label_remove_noop",
    "label_mutation_failed",
]


@dataclass
class _LabelMutationStats:
    """Per-batch label mutation counters for churn observability."""

    label_add_attempted: int = 0
    label_add_applied: int = 0
    label_add_noop: int = 0
    label_remove_attempted: int = 0
    label_remove_applied: int = 0
    label_remove_noop: int = 0
    label_mutation_failed: int = 0

    @property
    def attempted(self) -> int:
        return self.label_add_attempted + self.label_remove_attempted

    @property
    def applied(self) -> int:
        return self.label_add_applied + self.label_remove_applied

    @property
    def noop(self) -> int:
        return self.label_add_noop + self.label_remove_noop

    def to_payload(self) -> dict[str, int]:
        return {
            "label_add_attempted": self.label_add_attempted,
            "label_add_applied": self.label_add_applied,
            "label_add_noop": self.label_add_noop,
            "label_remove_attempted": self.label_remove_attempted,
            "label_remove_applied": self.label_remove_applied,
            "label_remove_noop": self.label_remove_noop,
            "label_mutation_attempted": self.attempted,
            "label_mutation_applied": self.applied,
            "label_mutation_noop": self.noop,
            "label_mutation_failed": self.label_mutation_failed,
        }


@dataclass
class ActionApplier:
    """Applies actions via ports/adapters.

    This is the IO boundary - all external calls go through here.
    Each action type has a handler that knows how to execute it.

    When reconciliation is enabled (reconcile=True):
    - Before label mutations, fetches current labels from fresh_issue_reader
    - Verifies state hasn't changed unexpectedly
    - Emits reconciliation events for traceability
    """

    labels: LabelSet
    sessions: SessionManager
    events: EventSink
    repository_host: Optional[RepositoryHost] = None  # For issue creation, labels
    worktree_manager: Optional[WorktreeManager] = None  # For worktree operations
    fresh_issue_reader: Optional[FreshIssueReader] = None
    reconcile: bool = False  # If True, verify state before mutations
    # Session launcher callback - handles entity lookup + launching
    # Injected by orchestrator, allows ActionApplier to launch sessions without
    # knowing about Issue/PendingReview/PendingRework entities
    session_launcher: Optional[SessionLauncherCallback] = None
    validation_retry_launcher: Optional[ValidationRetryLauncherCallback] = None
    # Claim/lease verification for multi-orchestrator coordination
    claim_gate: Optional[ClaimGate] = None
    # Callback to look up lease_id for an issue from active sessions
    lease_id_lookup: Optional[LeaseIdLookup] = None
    # Optional label persistence store for write-through tracking
    label_store: Optional["LabelStore"] = None
    # Label policy owner. Required to apply ShedRecoveredWorkflowLabelsAction,
    # which decides the labels to remove from the issue's live labels at apply
    # time. Optional so unrelated tests need not wire it.
    label_manager: Optional["LabelManager"] = None
    # Issue-scoped persistent coder/reviewer subprocess pair registry.
    # Used with the background supervisor to terminate hidden review-exchange
    # runtime work at issue lifecycle boundaries. ADR 0026 / B2.
    pair_registry: Optional["PersistentExchangePairRegistry"] = None
    # Shared background-job supervisor. Used with pair_registry to make
    # issue/rework cancellation a terminal review-exchange lifecycle event.
    background_job_supervisor: Optional["BackgroundJobSupervisor"] = None
    # Publish-retry owner, abandoned at issue terminal boundaries via the shared
    # runtime terminator so a late republish cannot repopulate a terminated
    # issue. Wired post-construction (PublishRecoveryService needs this applier).
    publish_recovery: Optional["PublishRetryAbandoner"] = None
    # Callback for worktree removal notifications
    # Used by async completion processing to mark jobs as WORKTREE_GONE
    # Returns the number of jobs marked as worktree_gone
    on_worktree_removed: Optional[Callable[[str], int]] = None
    # Owner for controlled in-memory history mutations.
    history_owner: Optional["SessionHistoryOwner"] = None
    # Execution-time owners for act-level tech_lead ops (#6764/#6778), plus the
    # orchestrator-owned gated-proposal op store. Wired post-construction by
    # the composition root (production runners close over live orchestrator
    # state); unwired means the actions fail loudly instead of no-oping.
    tech_lead_reset_retry: Optional["TechLeadResetRetryExecutor"] = None
    tech_lead_kill_session: Optional["TechLeadKillSessionExecutor"] = None
    tech_lead_ops: Optional["TechLeadAuthorityStore"] = None
    # Cross-repo filing seam for the finding-promotion lane (#6957). Unwired
    # means promotion actions fail loudly instead of silently no-oping — the
    # lane is only ever planned when tech_lead.findings is enabled.
    promotion_target: Optional["PromotionTargetHost"] = None
    # Expedite-lane owner seam (#6870), wired post-construction; unwired = no-op.
    expedite_lane: Optional["ExpediteLane"] = None
    _active_label_mutation_stats: _LabelMutationStats | None = field(
        default=None, init=False, repr=False
    )
    _active_label_mutation_by_issue: dict[int, _LabelMutationStats] = field(
        default_factory=dict, init=False, repr=False
    )

    def apply(self, action: Action) -> ActionResult:
        """Apply a single action.

        Args:
            action: The action to apply

        Returns:
            ActionResult indicating success/failure
        """
        self._emit_action_start(action)

        try:
            result = self._dispatch(action)
        except ReconciliationRequired:
            # Re-raise ReconciliationRequired - it must propagate to orchestrator
            raise
        except ClaimLostError:
            # Re-raise ClaimLostError - it must propagate to orchestrator
            raise
        except Exception as e:
            logger.exception(f"Action failed: {action}")
            result = ActionResult.fail(action, str(e))

        self._emit_action_end(action, result)
        return result

    def apply_all(self, actions: Sequence[Action]) -> list[ActionResult]:
        """Apply multiple actions in sequence.

        Args:
            actions: The actions to apply

        Returns:
            List of ActionResults
        """
        self._active_label_mutation_stats = _LabelMutationStats()
        self._active_label_mutation_by_issue = {}
        try:
            return [self.apply(action) for action in actions]
        finally:
            self._emit_label_mutation_summary()
            self._active_label_mutation_stats = None
            self._active_label_mutation_by_issue = {}

    def _dispatch(self, action: Action) -> ActionResult:
        """Dispatch an action to the appropriate handler."""
        handlers: dict[ActionType, Callable[[Action], ActionResult]] = {
            ActionType.ADD_LABEL: self._apply_add_label,
            ActionType.REMOVE_LABEL: self._apply_remove_label,
            ActionType.SYNC_LABELS: self._apply_sync_labels,
            # SHED_RECOVERED_WORKFLOW_LABELS is intentionally NOT dispatchable:
            # shedding transient workflow labels is a private sub-step of the
            # RECOVER_TERMINAL_ISSUE owner command, which enforces the
            # reconciliation pause gate before invoking it. Leaving it out of
            # the dispatch table makes it impossible to call the shed as an
            # independent mutating action that would bypass that gate (#6431 F1).
            ActionType.LAUNCH_SESSION: self._apply_launch_session,
            ActionType.LAUNCH_VALIDATION_RETRY: self._apply_launch_validation_retry,
            ActionType.STOP_SESSION: self._apply_stop_session,
            # Queue operations - IO is handled here, state update by orchestrator
            ActionType.QUEUE_REVIEW: self._apply_queue_review,
            ActionType.QUEUE_RETROSPECTIVE_REVIEW: self._apply_queue_operation,
            ActionType.QUEUE_REWORK: self._apply_queue_operation,
            ActionType.QUEUE_TECH_LEAD: self._apply_queue_operation,
            ActionType.ESCALATE_TO_HUMAN: self._apply_escalate,
            ActionType.ENQUEUE_TO_MERGE_QUEUE: self._apply_enqueue_to_merge_queue,
            # Every tech-lead action type -> its extracted apply-time owner.
            **tech_lead_action_handlers(
                create_tech_lead_issue=self._apply_create_tech_lead_issue,
                surface_proposal=self._apply_surface_tech_lead_proposal,
                reset_retry=self._apply_reset_retry_issue,
                kill_hung_session=self._apply_kill_hung_session,
                repository_host=self.repository_host,
                authority=self.tech_lead_ops,
                promotion_target=self.promotion_target,
            ),
            # Cleanup operations
            ActionType.CLEANUP_SESSION: self._apply_cleanup_session,
            ActionType.REMOVE_WORKTREE: self._apply_remove_worktree,
            # Comments
            ActionType.ADD_COMMENT: self._apply_add_comment,
            ActionType.SUPERSEDE_PR: self._apply_supersede_pr,
            ActionType.CLOSE_ISSUE: self._apply_close_issue,
            ActionType.SET_ISSUE_STATE: self._apply_set_issue_state,
            # History operations
            ActionType.RECONCILE_HISTORY_ENTRY: self._apply_reconcile_history_entry,
            # Terminal recovery: shed labels, then finalize history (ordered)
            ActionType.RECOVER_TERMINAL_ISSUE: self._apply_recover_terminal_issue,
        }

        handler = handlers.get(action.action_type)
        if handler is None:
            return ActionResult.skip(
                action, f"No handler for action type: {action.action_type}"
            )

        return handler(action)

    def _apply_add_label(self, action: Action) -> ActionResult:
        """Add a label to an issue."""
        assert isinstance(action, AddLabelAction)

        # Enforce expected state before mutation (raises ReconciliationRequired)
        self._require_expected(action, action.issue_number)
        # Verify claim ownership before write (raises ClaimLostError)
        self._verify_claim_before_write(action, action.issue_number)

        try:
            self._record_label_stat(action.issue_number, "label_add_attempted")
            has_label = self._has_label_safely(action.issue_number, action.label)
            if has_label is True:
                self._record_label_stat(action.issue_number, "label_add_noop")
                self._log_label_mutation(
                    level=logging.INFO,
                    issue_number=action.issue_number,
                    operation="add",
                    outcome="noop",
                    label=action.label,
                    reason=action.reason,
                    detail="already present",
                )
                return ActionResult.ok(
                    action,
                    issue_number=action.issue_number,
                    label=action.label,
                    no_op=True,
                )
            self.labels.add_label(action.issue_number, action.label)
            self._persist_label_add(action.issue_number, action.label)
            self._record_label_stat(action.issue_number, "label_add_applied")
            self._log_label_mutation(
                level=logging.INFO,
                issue_number=action.issue_number,
                operation="add",
                outcome="applied",
                label=action.label,
                reason=action.reason,
            )
            self._emit_issue_labels_changed(action.issue_number, [action.label], [], issue_key=action.issue_key)
            return ActionResult.ok(
                action,
                issue_number=action.issue_number,
                label=action.label,
            )
        except Exception as e:
            self._record_label_stat(action.issue_number, "label_mutation_failed")
            self._log_label_mutation(
                level=logging.ERROR,
                issue_number=action.issue_number,
                operation="add",
                outcome="failed",
                label=action.label,
                reason=action.reason,
                detail=str(e),
            )
            return ActionResult.fail(action, str(e))

    def _apply_remove_label(self, action: Action) -> ActionResult:
        """Remove a label from an issue."""
        assert isinstance(action, RemoveLabelAction)

        # Enforce expected state before mutation (raises ReconciliationRequired)
        self._require_expected(action, action.issue_number)
        # Verify claim ownership before write (raises ClaimLostError)
        self._verify_claim_before_write(action, action.issue_number)

        try:
            self._record_label_stat(action.issue_number, "label_remove_attempted")
            has_label = self._has_label_safely(action.issue_number, action.label)
            should_skip_remove_noop = False
            # Remove no-op is reconcile-scoped. In startup/session-launch paths,
            # cached has_label=False may be stale, so only skip when fresh labels
            # explicitly confirm the label is absent.
            if has_label is False and self.reconcile and self.fresh_issue_reader is not None:
                current_labels = self._fetch_current_labels(action.issue_number)
                should_skip_remove_noop = (
                    current_labels is not None and action.label not in current_labels
                )
            if should_skip_remove_noop:
                self._record_label_stat(action.issue_number, "label_remove_noop")
                self._log_label_mutation(
                    level=logging.INFO,
                    issue_number=action.issue_number,
                    operation="remove",
                    outcome="noop",
                    label=action.label,
                    reason=action.reason,
                    detail="already absent",
                )
                return ActionResult.ok(
                    action,
                    issue_number=action.issue_number,
                    label=action.label,
                    no_op=True,
                )
            self.labels.remove_label(action.issue_number, action.label)
            self._persist_label_remove(action.issue_number, action.label)
            self._record_label_stat(action.issue_number, "label_remove_applied")
            self._log_label_mutation(
                level=logging.INFO,
                issue_number=action.issue_number,
                operation="remove",
                outcome="applied",
                label=action.label,
                reason=action.reason,
            )
            self._emit_issue_labels_changed(action.issue_number, [], [action.label], issue_key=action.issue_key)
            return ActionResult.ok(
                action,
                issue_number=action.issue_number,
                label=action.label,
            )
        except Exception as e:
            self._record_label_stat(action.issue_number, "label_mutation_failed")
            self._log_label_mutation(
                level=logging.ERROR,
                issue_number=action.issue_number,
                operation="remove",
                outcome="failed",
                label=action.label,
                reason=action.reason,
                detail=str(e),
            )
            return ActionResult.fail(action, str(e))

    def _apply_add_comment(self, action: Action) -> ActionResult:
        """Add a comment to an issue or PR."""
        assert isinstance(action, AddCommentAction)
        assert self.repository_host is not None, "repository_host required for add_comment"

        # Enforce expected state before mutation (raises ReconciliationRequired)
        self._require_expected(action, action.number)
        # Verify claim ownership before write (raises ClaimLostError)
        self._verify_claim_before_write(action, action.number)

        try:
            comment_url = self.repository_host.add_comment(action.number, action.comment)
            logger.info(issue_log(action.number, "Comment added (%d chars)"), len(action.comment))
            # Emit review comment event for PR-targeted comments.
            if action.is_pr:
                excerpt = action.comment.strip().replace("\n", " ")
                self.events.publish(make_trace_event(
                    EventName.REVIEW_COMMENT_ADDED,
                    {
                        "issue_number": action.number,
                        "pr_number": action.number,
                        "comment_url": comment_url,
                        "comment_excerpt": excerpt if excerpt else "",
                        "summary": "Posted review comment",
                    },
                ))
            return ActionResult.ok(
                action,
                number=action.number,
                is_pr=action.is_pr,
            )
        except Exception as e:
            logger.error(issue_log(action.number, "Failed to add comment: %s"), e)
            return ActionResult.fail(action, str(e))

    def _apply_supersede_pr(self, action: Action) -> ActionResult:
        """Comment on and close a PR that has been superseded by a reset."""
        assert isinstance(action, SupersedePullRequestAction)
        assert self.repository_host is not None, "repository_host required for supersede_pr"

        self._require_expected(action, action.issue_number)
        self._verify_claim_before_write(action, action.issue_number)

        step = "comment"
        try:
            comment_url = self.repository_host.add_comment(action.pr_number, action.comment)
            step = "close"
            self.repository_host.close_pr(action.pr_number)
            logger.info(
                issue_log(action.issue_number, "Superseded PR #%d"),
                action.pr_number,
            )
            return ActionResult.ok(
                action,
                issue_number=action.issue_number,
                pr_number=action.pr_number,
                comment_url=comment_url,
            )
        except Exception as e:
            logger.error(
                issue_log(
                    action.issue_number,
                    "Failed to supersede PR #%d during %s step: %s",
                ),
                action.pr_number,
                step,
                e,
                exc_info=True,
            )
            return ActionResult.fail(
                action,
                f"PR #{action.pr_number} {step} failed: {e}",
                pr_number=action.pr_number,
            )

    def _apply_enqueue_to_merge_queue(self, action: Action) -> ActionResult:
        """Enqueue a reviewer-approved PR into the provider's merge queue."""
        assert isinstance(action, EnqueueToMergeQueueAction)
        assert self.repository_host is not None, (
            "repository_host required for enqueue_to_merge_queue"
        )

        # Enqueue is a GitHub write on a (possibly still-claimed) issue.
        self._verify_claim_before_write(action, action.issue_number)

        try:
            self.repository_host.enqueue_to_merge_queue(action.pr_number)
        except Exception as e:
            logger.error(
                issue_log(action.issue_number, "Failed to enqueue PR #%d to merge queue: %s"),
                action.pr_number,
                e,
                exc_info=True,
            )
            return ActionResult.fail(
                action,
                f"PR #{action.pr_number} merge-queue enqueue failed: {e}",
                pr_number=action.pr_number,
            )

        logger.info(
            issue_log(action.issue_number, "Enqueued PR #%d to merge queue"),
            action.pr_number,
        )
        self.events.publish(make_trace_event(
            EventName.MERGE_QUEUE_ENQUEUED,
            {
                "issue_number": action.issue_number,
                "issue_key": action.issue_key or str(action.issue_number),
                "pr_number": action.pr_number,
                "pr_url": action.pr_url,
            },
        ))
        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            pr_number=action.pr_number,
        )

    def _apply_close_issue(self, action: Action) -> ActionResult:
        """Close an issue through the repository host."""
        assert isinstance(action, CloseIssueAction)
        assert self.repository_host is not None, "repository_host required for close_issue"

        self._require_expected(action, action.issue_number)
        self._verify_claim_before_write(action, action.issue_number)

        try:
            self.repository_host.update_issue_state(action.issue_number, "closed")
            logger.info(issue_log(action.issue_number, "Issue closed"))
            if action.comment:
                # Only after a successful close — a comment claiming "the
                # orchestrator closed it" before the close would leave a false
                # audit trail on failure and repeat on every retry. Best-effort:
                # a failed comment must never fail an already-applied close.
                try:
                    self.repository_host.add_comment(
                        action.issue_number, action.comment,
                    )
                except Exception as e:
                    logger.warning(
                        issue_log(
                            action.issue_number,
                            "Failed to post close comment: %s",
                        ),
                        e,
                    )
            return ActionResult.ok(
                action,
                issue_number=action.issue_number,
                state="closed",
            )
        except Exception as e:
            logger.error(
                issue_log(action.issue_number, "Failed to close issue: %s"),
                e,
            )
            return ActionResult.fail(action, str(e), issue_number=action.issue_number)

    def _apply_set_issue_state(self, action: Action) -> ActionResult:
        """Set an issue's open/closed state through the repository host."""
        assert isinstance(action, SetIssueStateAction)
        assert self.repository_host is not None, "repository_host required for set_issue_state"

        self._require_expected(action, action.issue_number)
        self._verify_claim_before_write(action, action.issue_number)

        try:
            self.repository_host.update_issue_state(action.issue_number, action.state)
            logger.info(
                issue_log(action.issue_number, "Issue state set to %s"),
                action.state,
            )
            return ActionResult.ok(
                action,
                issue_number=action.issue_number,
                state=action.state,
            )
        except Exception as e:
            logger.error(
                issue_log(action.issue_number, "Failed to set issue state to %s: %s"),
                action.state,
                e,
            )
            return ActionResult.fail(action, str(e), issue_number=action.issue_number)

    def _fetch_current_labels(self, issue_number: int) -> set[str] | None:
        """Fetch current labels for an issue if fresh_issue_reader is available.

        Returns:
            Set of label names, or None if fresh_issue_reader not configured
        """
        if self.fresh_issue_reader is None:
            return None
        try:
            labels = self.fresh_issue_reader.read_issue_labels(issue_number)
            return set(labels)
        except Exception as e:
            logger.warning(
                issue_log(issue_number, "Failed to fetch labels for reconciliation: %s"),
                e,
            )
            return None

    def _require_expected(self, action: Action, issue_number: int) -> None:
        """Enforce reconciliation before a mutation if action has expected state.

        This is the hard gate for optimistic concurrency control. If the action
        has an ExpectedState attached, we fetch current state and verify it
        satisfies the constraints. If not, we raise ReconciliationRequired.

        Args:
            action: The action being applied (checks action.expected)
            issue_number: The issue/PR number to check

        Raises:
            ReconciliationRequired: If current state doesn't satisfy expected
        """
        if action.expected is None:
            # No expected state attached - allow (for backwards compatibility)
            return

        if not self.reconcile:
            # Reconciliation disabled - skip enforcement
            return

        # Fetch current state
        current_labels = self._fetch_current_labels(issue_number)
        if current_labels is None:
            # Can't verify - fail closed (require fresh_issue_reader for reconciliation)
            logger.warning(
                issue_log(issue_number, "Reconciliation required but cannot fetch labels - failing closed"),
            )
            raise ReconciliationRequired(
                entity_type="issue",
                entity_id=issue_number,
                expected=ExternalSnapshot.for_issue(issue_number, set(action.expected.required_labels)),
                actual=ExternalSnapshot.for_issue(issue_number, set()),
                reason="Cannot fetch current labels to verify expected state",
            )

        actual = ExternalSnapshot.for_issue(issue_number, current_labels)

        # This raises ReconciliationRequired if constraints not satisfied
        require_reconciliation(action.expected, actual, entity_type="issue")

    def _verify_claim_before_write(self, action: Action, issue_number: int) -> None:
        """Verify claim ownership before a write operation.

        For multi-orchestrator coordination, this verifies the current orchestrator
        still owns the claim for this issue before making any external mutation.

        Args:
            action: The action being applied (for logging the operation type)
            issue_number: The issue number to verify claim for

        Raises:
            ClaimLostError: If the claim has been lost to another orchestrator
        """
        if not self.claim_gate:
            # No claim gate configured - skip verification
            return

        if not self.lease_id_lookup:
            # No lease_id lookup configured - skip verification
            return

        lease_id = self.lease_id_lookup(issue_number)
        if not lease_id:
            # No active session with lease for this issue - skip verification
            return

        # Verify claim ownership - raises ClaimLostError if lost
        self.claim_gate.verify_or_raise(
            issue_number=issue_number,
            lease_id=lease_id,
            operation=action.action_type.value,
        )

    def _has_label_safely(self, issue_number: int, label: str) -> bool | None:
        """Best-effort label presence check for no-op mutation guards."""
        try:
            return bool(self.labels.has_label(issue_number, label))
        except Exception as e:
            logger.debug(
                issue_log(issue_number, "Unable to check label presence for %s: %s"),
                label,
                e,
            )
            return None

    def _persist_label_add(self, issue_number: int, label: str) -> None:
        """Write-through: record label addition in LabelStore."""
        if self.label_store is None:
            return
        try:
            self.label_store.add_label(issue_number, label)
        except Exception as e:
            logger.debug("LabelStore add_label failed for #%d %s: %s", issue_number, label, e)

    def _persist_label_remove(self, issue_number: int, label: str) -> None:
        """Write-through: record label removal in LabelStore."""
        if self.label_store is None:
            return
        try:
            self.label_store.remove_label(issue_number, label)
        except Exception as e:
            logger.debug("LabelStore remove_label failed for #%d %s: %s", issue_number, label, e)

    def _check_reconciliation_for_sync(
        self,
        issue_number: int,
        add_labels: tuple[str, ...],
        remove_labels: tuple[str, ...],
    ) -> tuple[bool, str, set[str]]:
        """Check reconciliation for a sync operation.

        Args:
            issue_number: Issue to check
            add_labels: Labels we plan to add
            remove_labels: Labels we plan to remove

        Returns:
            Tuple of (should_proceed, message, current_labels).
            If reconciliation is not enabled or can't run, returns (True, "", current_labels).
        """
        if not self.reconcile:
            return True, "", set()

        current = self._fetch_current_labels(issue_number)
        if current is None:
            # Can't verify - proceed with warning
            logger.warning(
                issue_log(issue_number, "Reconciliation enabled but cannot fetch labels"),
            )
            return True, "Cannot fetch current labels", set()

        # Check 1: Labels we plan to remove should exist
        missing_to_remove = set(remove_labels) - current
        if missing_to_remove:
            msg = f"Labels to remove not present: {missing_to_remove}"
            logger.warning(issue_log(issue_number, "Reconciliation: %s"), msg)
            # This is a warning, not a hard failure - label may have been
            # removed externally which is fine
            self.events.publish(make_trace_event(
                EventName.RECONCILIATION_WARNING,
                {
                    "issue_number": issue_number,
                    "message": msg,
                    "missing_labels": list(missing_to_remove),
                },
            ))

        # Check 2: Labels we expect to be there for this transition
        # For now, we just log what we found vs expected
        self.events.publish(make_trace_event(
            EventName.RECONCILIATION_CHECKED,
            {
                "issue_number": issue_number,
                "current_labels": list(current),
                "add_labels": list(add_labels),
                "remove_labels": list(remove_labels),
            },
        ))

        return True, "", current

    def _apply_sync_labels(self, action: Action) -> ActionResult:
        """Synchronize labels on an issue.

        If reconciliation is enabled:
        1. Enforces expected state constraints (hard gate)
        2. Fetches current labels before mutations
        3. Logs any unexpected state (e.g., labels to remove not present)
        4. Emits reconciliation events for traceability
        """
        assert isinstance(action, SyncLabelsAction)

        # Enforce expected state before mutation (raises ReconciliationRequired)
        self._require_expected(action, action.issue_number)
        # Verify claim ownership before write (raises ClaimLostError)
        self._verify_claim_before_write(action, action.issue_number)

        # Soft reconciliation check (backwards compatibility - logs warnings)
        should_proceed, msg, _current_labels = self._check_reconciliation_for_sync(
            action.issue_number,
            action.add_labels,
            action.remove_labels,
        )
        if not should_proceed:
            return ActionResult.fail(action, f"Reconciliation failed: {msg}")

        errors = []

        # Add labels
        for label in action.add_labels:
            self._record_label_stat(action.issue_number, "label_add_attempted")
            try:
                self.labels.add_label(action.issue_number, label)
                self._persist_label_add(action.issue_number, label)
                self._record_label_stat(action.issue_number, "label_add_applied")
            except Exception as e:
                self._record_label_stat(action.issue_number, "label_mutation_failed")
                errors.append(f"add {label}: {e}")

        # Remove labels
        for label in action.remove_labels:
            self._record_label_stat(action.issue_number, "label_remove_attempted")
            try:
                self.labels.remove_label(action.issue_number, label)
                self._persist_label_remove(action.issue_number, label)
                self._record_label_stat(action.issue_number, "label_remove_applied")
            except Exception as e:
                self._record_label_stat(action.issue_number, "label_mutation_failed")
                errors.append(f"remove {label}: {e}")

        if errors:
            return ActionResult.fail(action, "; ".join(errors))

        self._emit_issue_labels_changed(
            action.issue_number,
            list(action.add_labels),
            list(action.remove_labels),
            issue_key=action.issue_key,
        )
        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            added=list(action.add_labels),
            removed=list(action.remove_labels),
        )

    def _apply_shed_recovered_workflow_labels(self, action: Action) -> ActionResult:
        """Shed transient workflow labels after an issue's work has landed.

        Private sub-step of the RECOVER_TERMINAL_ISSUE owner command — it is not
        registered in the dispatch table, so it can only be reached through
        ``_apply_recover_terminal_issue`` after that command has enforced the
        reconciliation pause gate. This keeps the gate the single enforcement
        point and makes an independent, gate-bypassing shed impossible (#6431).

        Reads the issue's live labels, asks the LabelManager which are
        recovered-workflow labels (pr-pending, publish-failed,
        publish-fail-count-N, blocking labels), and removes each from both
        GitHub and the local label_store. GitHub is the source of truth for
        which labels exist; the label_store is folded in too so a row stranded
        there by past drift is also cleaned in the same pass.
        """
        assert isinstance(action, ShedRecoveredWorkflowLabelsAction)
        assert self.label_manager is not None, (
            "label_manager is required to shed recovered workflow labels"
        )

        # Verify claim ownership before write (raises ClaimLostError)
        self._verify_claim_before_write(action, action.issue_number)

        current = self._labels_for_recovery_shed(action.issue_number)
        to_remove = self.label_manager.recovered_workflow_labels(sorted(current))

        removed: list[str] = []
        errors: list[str] = []
        for label in to_remove:
            self._record_label_stat(action.issue_number, "label_remove_attempted")
            try:
                self.labels.remove_label(action.issue_number, label)
                self._persist_label_remove(action.issue_number, label)
                self._record_label_stat(action.issue_number, "label_remove_applied")
                self._log_label_mutation(
                    level=logging.INFO,
                    issue_number=action.issue_number,
                    operation="remove",
                    outcome="applied",
                    label=label,
                    reason=action.reason,
                    detail="recovered workflow cleanup",
                )
                removed.append(label)
            except Exception as e:
                self._record_label_stat(action.issue_number, "label_mutation_failed")
                errors.append(f"remove {label}: {e}")

        if removed:
            self._emit_issue_labels_changed(
                action.issue_number, [], removed, issue_key=action.issue_key
            )
        if errors:
            return ActionResult.fail(action, "; ".join(errors))
        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            removed=removed,
        )

    def _labels_for_recovery_shed(self, issue_number: int) -> set[str]:
        """Union of the issue's live GitHub labels and its label_store rows.

        Live GitHub labels are authoritative; the label_store contribution
        ensures a label the orchestrator believes it applied is still cleaned
        even if the fresh read is unavailable or has already diverged.
        """
        labels: set[str] = set()
        fresh = self._fetch_current_labels(issue_number)
        if fresh is not None:
            labels |= fresh
        if self.label_store is not None:
            try:
                labels |= self.label_store.load_labels(issue_number)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    issue_log(issue_number, "Failed to read label_store for shed: %s"),
                    e,
                )
        return labels

    def _apply_launch_session(self, action: Action) -> ActionResult:
        """Launch a terminal session.

        Uses the injected session_launcher callback to handle entity lookup
        and actual session launching. This keeps ActionApplier unaware of
        Issue/PendingReview/PendingRework entity types.
        """
        assert isinstance(action, LaunchSessionAction)

        # Use the callback if provided (preferred path - handles entity lookup)
        if self.session_launcher is not None:
            session = self.session_launcher(action.session_type, action.number)
            if session:
                # An expedited issue that is now an active session has jumped
                # the lane: free its cap slot (via the queue owner, never a
                # direct priority_queue mutation) so the next urgent tech-lead
                # finding can be expedited (#6870). No-op for non-expedited work.
                if self.expedite_lane is not None:
                    self.expedite_lane.release(session.issue.number)
                return ActionResult.ok(
                    action,
                    session_name=session.terminal_id,
                    issue_number=session.issue.number,
                )
            else:
                return ActionResult.fail(
                    action,
                    f"Failed to launch {action.session_type} session for #{action.number}"
                )

        # Fallback: use command/working_dir from action (for testing or direct calls)
        if not action.command or not action.working_dir:
            return ActionResult.fail(
                action,
                "No session_launcher callback and action missing command/working_dir"
            )

        ref = SessionRef(session_type=action.session_type, number=action.number)

        # Check if already running
        if self.sessions.exists(ref):
            return ActionResult.skip(action, f"Session {ref.name} already running")

        ctx = SessionContext(
            ref=ref,
            command=action.command,
            working_dir=Path(action.working_dir),
            title=action.title,
        )

        success = self.sessions.start(ctx)

        if success:
            return ActionResult.ok(action, session_name=ref.name)
        else:
            return ActionResult.fail(action, "Failed to start session")

    def _apply_launch_validation_retry(self, action: Action) -> ActionResult:
        """Launch a validation retry session through the orchestrator callback."""
        assert isinstance(action, LaunchValidationRetryAction)

        if self.validation_retry_launcher is None:
            return ActionResult.fail(
                action,
                "No validation_retry_launcher callback configured",
            )

        session = self.validation_retry_launcher(action.issue_number)
        if session:
            return ActionResult.ok(
                action,
                session_name=session.terminal_id,
                issue_number=session.issue.number,
            )
        return ActionResult.fail(
            action,
            f"Failed to launch validation retry for issue #{action.issue_number}",
        )

    def _apply_stop_session(self, action: Action) -> ActionResult:
        """Stop a terminal session."""
        assert isinstance(action, StopSessionAction)

        ref = SessionRef(session_type=action.session_type, number=action.number)
        cancellation = self._cancel_review_exchange_for_session_ref(ref, reason="session-stopped")

        # Check if running
        if not self.sessions.exists(ref):
            return ActionResult.skip(
                action,
                f"Session {ref.name} not running",
                review_exchange_lifecycle_checked=cancellation is not None,
                cancelled_review_exchange_jobs=list(cancellation.cancelled_job_ids)
                if cancellation is not None
                else [],
            )

        self.sessions.stop(ref)
        return ActionResult.ok(
            action,
            session_name=ref.name,
            review_exchange_lifecycle_checked=cancellation is not None,
            cancelled_review_exchange_jobs=list(cancellation.cancelled_job_ids)
            if cancellation is not None
            else [],
        )

    def _cancel_review_exchange_for_session_ref(
        self,
        ref: SessionRef,
        *,
        reason: str,
    ) -> "ReviewExchangeCancellation | None":
        if ref.session_type not in {SessionType.ISSUE, SessionType.REWORK}:
            return None
        return self._cancel_review_exchange_for_issue(ref.number, reason=reason)

    def _cancel_review_exchange_for_issue(
        self,
        issue_number: int,
        *,
        reason: str,
    ) -> "ReviewExchangeCancellation | None":
        return cancel_issue_review_exchange(
            issue_number=issue_number,
            reason=reason,
            pair_registry=self.pair_registry,
            job_supervisor=self.background_job_supervisor,
        )

    def _terminate_issue_runtime_for_issue(
        self,
        issue_number: int,
        *,
        reason: str,
    ) -> "IssueRuntimeTermination":
        return terminate_issue_runtime(
            issue_number=issue_number,
            reason=reason,
            pair_registry=self.pair_registry,
            job_supervisor=self.background_job_supervisor,
            session_manager=self.sessions,
            publish_recovery=self.publish_recovery,
        )

    def _apply_queue_operation(self, action: Action) -> ActionResult:
        """Queue operations are handled by orchestrator state.

        The applier just signals success - actual queuing is done by the caller.
        """
        return ActionResult.ok(action, note="Queue operation delegated to orchestrator")

    def _get_latest_review_section(
        self, pr_number: int, provided_body: str | None
    ) -> str:
        """Build the latest review section for escalation comments.

        Returns formatted markdown section or empty string.
        """
        review_body = provided_body
        if not review_body and self.repository_host:
            try:
                reviews = self.repository_host.get_pr_reviews(pr_number)
                for review in reversed(reviews):
                    if review.get("state") == "CHANGES_REQUESTED" and review.get("body"):
                        review_body = review.get("body", "")
                        break
            except Exception as e:
                logger.debug("Failed to fetch PR reviews: %s", e)

        if not review_body:
            return ""

        if len(review_body) > 1000:
            review_body = review_body[:1000] + "..."
        return f"""
### Latest Review Feedback

<details>
<summary>Reviewer's comments (click to expand)</summary>

{review_body}

</details>
"""

    def _apply_escalate(self, action: Action) -> ActionResult:
        """Escalate to human intervention.

        The full escalation flow:
        1. Enforce expected state (reconciliation)
        2. Add needs-human label to the PR
        3. Remove needs-rework label from the PR
        4. Post an explanatory comment
        5. Emit trace event
        6. Release the persistent coder/reviewer pair — escalation
           ends the automated retry loop so the pair is no longer
           useful. ADR 0026 / B2 lifecycle release boundary.
        """
        assert isinstance(action, EscalateToHumanAction)

        # Enforce expected state before mutation (raises ReconciliationRequired)
        self._require_expected(action, action.pr_number)
        # Verify claim ownership before write (raises ClaimLostError)
        # Claims are on issues, not PRs, so use issue_number
        self._verify_claim_before_write(action, action.issue_number)

        # Tear down runtime work before label mutations so a partial
        # escalation (e.g. label add succeeds, comment fails) still
        # ends with hidden review-exchange work and visible issue/rework
        # terminals stopped. The lifecycle contract is "escalation kills
        # issue automation, full stop".
        self._terminate_issue_runtime_for_issue(
            action.issue_number,
            reason="escalated-to-human",
        )

        errors = []
        comment_url = ""

        added_labels: list[str] = []
        removed_labels: list[str] = []

        # Add needs-human label
        self._record_label_stat(action.issue_number, "label_add_attempted")
        try:
            self.labels.add_label(action.pr_number, action.needs_human_label)
            self._persist_label_add(action.pr_number, action.needs_human_label)
            self._record_label_stat(action.issue_number, "label_add_applied")
            added_labels.append(action.needs_human_label)
        except Exception as e:
            self._record_label_stat(action.issue_number, "label_mutation_failed")
            errors.append(f"add label: {e}")

        # Remove needs-rework label
        self._record_label_stat(action.issue_number, "label_remove_attempted")
        try:
            self.labels.remove_label(action.pr_number, action.needs_rework_label)
            self._persist_label_remove(action.pr_number, action.needs_rework_label)
            self._record_label_stat(action.issue_number, "label_remove_applied")
            removed_labels.append(action.needs_rework_label)
        except Exception as e:
            self._record_label_stat(action.issue_number, "label_mutation_failed")
            # Not a hard failure - label may already be removed
            logger.debug("Failed to remove needs-rework label: %s", e)
        self._emit_pr_view_changed(
            pr_number=action.pr_number,
            issue_number=action.issue_number,
            added=added_labels,
            removed=removed_labels,
            issue_key=action.issue_key,
        )

        # Post explanatory comment. If the action carries an explicit
        # comment_override, use that verbatim (post-publish-stuck path
        # provides its own copy that doesn't mention rework cycles).
        if self.repository_host:
            if action.comment_override is not None:
                comment = action.comment_override
            else:
                latest_review_section = self._get_latest_review_section(
                    action.pr_number, action.latest_review_body
                )
                comment = f"""## ⚠️ Escalated to Human Review

This PR has gone through {action.rework_cycles - 1} rework cycles without passing review.
Maximum rework cycles ({action.max_rework_cycles}) exceeded.
{latest_review_section}
**A human needs to review and either:**
- Approve the PR manually
- Provide specific guidance for the agent
- Take over the implementation
"""
            try:
                comment_url = self.repository_host.add_comment(action.pr_number, comment)
            except Exception as e:
                errors.append(f"add comment: {e}")
                comment_url = ""

        logger.warning(
            issue_log(action.issue_number, "PR #%d escalated to %s after %d rework cycles"),
            action.pr_number, action.needs_human_label, action.rework_cycles,
        )

        # Emit trace event
        self.events.publish(
            make_trace_event(
                EventName.REVIEW_ESCALATED,
                {
                    "pr_number": action.pr_number,
                    "issue_number": action.issue_number,
                    "rework_count": action.rework_cycles - 1,
                    "rework_cycle": action.rework_cycles,
                    "max_rework_cycles": action.max_rework_cycles,
                },
            )
        )
        if comment_url:
            self.events.publish(
                make_trace_event(
                    EventName.REVIEW_COMMENT_ADDED,
                    {
                        "issue_number": action.issue_number,
                        "pr_number": action.pr_number,
                        "comment_url": comment_url,
                        "summary": "Posted escalation comment",
                    },
                )
            )

        if errors:
            return ActionResult.fail(action, "; ".join(errors))

        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            pr_number=action.pr_number,
            escalation_reason=action.escalation_reason,
        )

    def _emit_action_start(self, action: Action) -> None:
        """Emit a trace event when starting an action."""
        self.events.publish(
            make_trace_event(
                EventName.ACTION_START,
                {
                    "action_type": action.action_type.value,
                    "reason": action.reason,
                },
            )
        )

    def _emit_action_end(self, action: Action, result: ActionResult) -> None:
        """Emit a trace event when completing an action."""
        self.events.publish(
            make_trace_event(
                EventName.ACTION_END,
                {
                    "action_type": action.action_type.value,
                    "result": result.result_type.value,
                    "error": result.error,
                },
            )
        )

    def _apply_reconcile_history_entry(self, action: Action) -> ActionResult:
        """Reconcile a session history entry through the history owner."""
        assert isinstance(action, ReconcileHistoryEntryAction)
        return apply_history_reconciliation(
            action,
            history_owner=self.history_owner,
            events=self.events,
            tech_lead_authority=self.tech_lead_ops,
            terminate_issue_runtime=lambda issue_number, reason: (
                self._terminate_issue_runtime_for_issue(issue_number, reason=reason)
            ),
        )

    def _apply_recover_terminal_issue(self, action: Action) -> ActionResult:
        """Shed recovered-workflow labels, then finalize awaiting-merge history.

        Owns the terminal-recovery ordering invariant in one place: the history
        entry only transitions to its terminal status after the label cleanup
        has succeeded. The shed is a best-effort GitHub write; finalizing the
        history first and shedding second would take the entry out of the
        reconcilable awaiting-merge statuses, so a later shed failure would
        never be retried and would strand the pr-pending / publish-failed /
        publish-fail-count-* labels this recovery removes.

        On shed failure we return failure WITHOUT touching history, leaving the
        entry reconcilable for the next awaiting-merge discovery pass to retry.
        """
        assert isinstance(action, RecoverTerminalIssueAction)

        # Enforce the reconciliation pause gate at the owner-command boundary,
        # before ANY label write (raises ReconciliationRequired). The previous
        # terminal-cleanup path carried this guard on its RemoveLabelAction; the
        # owner command must keep it so an issue paused for reconciliation
        # (io:needs-reconcile) cannot have its transient labels shed or its
        # awaiting-merge history finalized behind the fail-closed drift handling
        # that ReconciliationRequired enforces (#6431 F1). This is the single
        # enforcement point: the shed sub-step is reached only after it passes,
        # and is not independently dispatchable.
        self._require_expected(action, action.issue_number)

        # Verify claim ownership at the owner-command boundary before any
        # GitHub write (raises ClaimLostError). The shed sub-step verifies
        # again; both checks key off the issue's lease, so this is a cheap,
        # explicit guard that this command writes only to a still-claimed issue.
        self._verify_claim_before_write(action, action.issue_number)

        close_applied = False
        if action.close_issue:
            # Close-on-merge fallback (porchpin #81): revalidation ordering
            # and rationale live in run_close_on_merge_fallback — the module
            # owns the destructive precondition; the planner's bit is advisory.
            close_applied, close_error = run_close_on_merge_fallback(
                repository_host=self.repository_host,
                action=action,
                close=self._apply_close_issue,
            )
            if close_error is not None:
                # Fail without any further mutation (no shed, no history);
                # the entry stays reconcilable for retry.
                return ActionResult.fail(
                    action,
                    close_error,
                    issue_number=action.issue_number,
                    pr_number=action.pr_number,
                )

        shed_result = self._apply_shed_recovered_workflow_labels(
            ShedRecoveredWorkflowLabelsAction(
                issue_number=action.issue_number,
                issue_key=action.issue_key,
                reason=action.reason,
            )
        )
        if not shed_result.success:
            # Do not finalize history; keep the entry reconcilable for retry.
            return ActionResult.fail(
                action,
                "recovered-label shed failed; awaiting-merge history left "
                f"reconcilable for retry: {shed_result.error}",
                issue_number=action.issue_number,
                pr_number=action.pr_number,
            )

        history_result = self._apply_reconcile_history_entry(
            ReconcileHistoryEntryAction(
                issue_number=action.issue_number,
                pr_number=action.pr_number,
                pr_url=action.pr_url,
                status=action.status,
                source=action.source,
                issue_key=action.issue_key,
                reason=action.status_reason,
            )
        )
        if not history_result.success:
            return ActionResult.fail(
                action,
                history_result.error or "history reconciliation failed",
                issue_number=action.issue_number,
                pr_number=action.pr_number,
            )
        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            pr_number=action.pr_number,
            status=action.status,
            shed_removed=list(shed_result.details.get("removed", [])),
            closed_issue=close_applied,
        )

    def _apply_queue_review(self, action: Action) -> ActionResult:
        """Queue a PR for code review.

        Handles the IO part (adding review label). State update is handled
        by the orchestrator after this returns.
        """
        assert isinstance(action, QueueReviewAction)

        # Enforce expected state before mutation (raises ReconciliationRequired)
        if action.pr_number:
            self._require_expected(action, action.pr_number)
        # Verify claim ownership before write (raises ClaimLostError)
        # Claims are on issues, not PRs, so use issue_number
        if action.issue_number:
            self._verify_claim_before_write(action, action.issue_number)

        # Add review label if available
        if self.labels and action.code_review_label and action.pr_number:
            self._record_label_stat(action.issue_number or action.pr_number, "label_add_attempted")
            try:
                self.labels.add_label(action.pr_number, action.code_review_label)
                self._persist_label_add(action.pr_number, action.code_review_label)
                self._record_label_stat(action.issue_number or action.pr_number, "label_add_applied")
                logger.info(issue_log(action.issue_number, "Review label '%s' added to PR #%d"), action.code_review_label, action.pr_number)
                self._emit_pr_view_changed(
                    pr_number=action.pr_number,
                    issue_number=action.issue_number,
                    added=[action.code_review_label],
                    removed=[],
                    issue_key=action.issue_key,
                )
            except Exception as e:
                self._record_label_stat(action.issue_number or action.pr_number, "label_mutation_failed")
                logger.warning(issue_log(action.issue_number, "Failed to add review label to PR #%d: %s"), action.pr_number, e)

        self.events.publish(make_trace_event(EventName.REVIEW_QUEUED, {
            "pr_number": action.pr_number,
            "issue_number": action.issue_number,
            "pr_url": action.pr_url,
            "code_review_label": action.code_review_label,
        }))

        return ActionResult.ok(
            action,
            pr_number=action.pr_number,
            issue_number=action.issue_number,
        )

    def _apply_create_tech_lead_issue(self, action: Action) -> ActionResult:
        """Delegate tech-lead-authored issue creation to its apply-time owner."""
        assert isinstance(action, CreateTechLeadIssueAction)
        if not self.repository_host:
            return ActionResult.fail(
                action, "No repository_host configured for issue creation"
            )
        return apply_create_tech_lead_issue(
            action,
            repository_host=self.repository_host,
            events=self.events,
            ops=self.tech_lead_ops,
            add_comment=self.repository_host.add_comment,
            emit_labels_changed=self._emit_issue_labels_changed,
            expedite_lane=self.expedite_lane,
        )

    def _apply_surface_tech_lead_proposal(self, action: Action) -> ActionResult:
        """Surface a tech_lead decision proposal as a trace event (ADR-0031).

        Event choice and payload are owned by ``tech_lead_reset_retry`` (shared
        with the stale-downgrade surface); NO GitHub calls are made.
        """
        assert isinstance(action, SurfaceTechLeadProposalAction)
        return apply_surface_tech_lead_proposal(action, self.events)

    def _apply_reset_retry_issue(self, action: Action) -> ActionResult:
        """Execute a tech_lead reset_retry proposal via the injected owner (#6764).

        Precondition re-validation, stale downgrade, and the reset itself are
        owned by TechLeadResetRetryExecutor; approved gated proposals (#6778)
        are finalized by the tech_lead_proposals owner.
        """
        assert isinstance(action, ResetRetryIssueAction)
        executor = self.tech_lead_reset_retry
        return self._apply_tech_lead_op(action, executor.apply if executor else None, "reset_retry")

    def _apply_kill_hung_session(self, action: Action) -> ActionResult:
        """Execute an APPROVED kill_hung_session op via the injected owner
        (#6778) — same pause gate / stale policy / finalization shape as
        reset_retry."""
        assert isinstance(action, KillHungSessionAction)
        executor = self.tech_lead_kill_session
        return self._apply_tech_lead_op(action, executor.apply if executor else None, "kill_hung_session")

    def _apply_tech_lead_op(
        self,
        action: "_TechLeadOpAction",
        apply_fn: "Callable[[_TechLeadOpAction], ActionResult] | None",
        op_type: str,
    ) -> ActionResult:
        # Reconciliation pause gate first (raises ReconciliationRequired) — a
        # paused issue must never be mutated from an agent proposal.
        self._require_expected(action, action.issue_number)
        if apply_fn is None:
            return ActionResult.fail(
                action,
                f"tech_lead {op_type} execution requested but no executor is"
                " wired into this applier",
            )
        return execute_approved_tech_lead_op(
            action,
            apply_fn,
            repository_host=self.repository_host,
            ops=self.tech_lead_ops,
        )

    def _apply_cleanup_session(self, action: Action) -> ActionResult:
        """Clean up a completed session."""
        assert isinstance(action, CleanupSessionAction)

        errors = []
        cancellation = self._cancel_review_exchange_for_cleanup(action)
        self._cleanup_terminal_session(action, errors)
        self._cleanup_worktree(action, errors)

        self.events.publish(make_trace_event(EventName.CLEANUP_COMPLETED, {"issue_number": action.issue_number, "pr_number": action.pr_number}))

        details = {
            "issue_number": action.issue_number,
            "pr_number": action.pr_number,
            "review_exchange_lifecycle_checked": cancellation is not None,
            "cancelled_review_exchange_jobs": list(cancellation.cancelled_job_ids)
            if cancellation is not None
            else [],
        }
        if errors:
            return ActionResult.fail(action, "; ".join(errors), **details)

        return ActionResult.ok(action, **details)

    def _cancel_review_exchange_for_cleanup(
        self,
        action: "CleanupSessionAction",
    ) -> "ReviewExchangeCancellation | None":
        ref = self._cleanup_review_exchange_session_ref(action)
        return self._cancel_review_exchange_for_session_ref(
            ref,
            reason="session-cleanup",
        )

    def _cleanup_review_exchange_session_ref(
        self,
        action: "CleanupSessionAction",
    ) -> SessionRef:
        if action.terminal_id:
            return SessionRef(
                session_type=self._determine_session_type(action.terminal_id),
                number=action.issue_number,
            )
        logger.warning(
            "[APPLIER] CleanupSessionAction missing terminal_id; assuming "
            "issue session for review-exchange cleanup issue=%s pr=%s worktree=%s",
            action.issue_number,
            action.pr_number,
            action.worktree_path or "(none)",
        )
        return SessionRef(session_type=SessionType.ISSUE, number=action.issue_number)

    def _cleanup_terminal_session(self, action: "CleanupSessionAction", errors: list[str]) -> None:
        """Close terminal session if configured."""
        if not (action.close_tabs and action.terminal_id):
            return

        try:
            session_type = self._determine_session_type(action.terminal_id)
            ref = SessionRef(session_type=session_type, number=action.issue_number)
            if self.sessions.exists(ref):
                self.sessions.stop(ref)
                logger.info(issue_log(action.issue_number, "Closed terminal session"))
        except Exception as e:
            errors.append(f"close session: {e}")
            logger.warning(issue_log(action.issue_number, "Failed to close session: %s"), e)

    def _determine_session_type(self, session_name: str) -> SessionType:
        """Determine session type from session name."""
        if session_name.startswith(RETROSPECTIVE_REVIEW_TERMINAL_PREFIX):
            return SessionType.RETROSPECTIVE_REVIEW
        if session_name.startswith("review-"):
            return SessionType.REVIEW
        if session_name.startswith("rework-"):
            return SessionType.REWORK
        if session_name.startswith("tech-lead-"):
            return SessionType.TECH_LEAD
        return SessionType.ISSUE

    def _cleanup_worktree(self, action: "CleanupSessionAction", errors: list[str]) -> None:
        """Remove worktree if configured."""
        if not (action.remove_worktrees and action.worktree_path):
            return

        if not self.worktree_manager:
            errors.append("no worktree_manager configured")
            return

        try:
            # Force removal ONLY for a disposable scratch worktree: it holds
            # throwaway agent artifacts, so a leftover untracked file must not
            # make ``git worktree remove`` fail (exit 128) and leak it. A normal
            # coding worktree stays non-forced so user work is never discarded
            # (#6824 F8).
            self.worktree_manager.remove(
                Path(action.worktree_path), force=action.disposable_worktree
            )
            logger.info(issue_log(action.issue_number, "Removed worktree: %s"), action.worktree_path)
        except Exception as e:
            errors.append(f"remove worktree: {e}")
            logger.warning(issue_log(action.issue_number, "Failed to remove worktree: %s"), e)
            return
        # Removal SUCCEEDED (or the path was already gone). The "worktree is gone"
        # notification is a distinct concern: a callback failure must NOT re-fail
        # an already-completed removal, or the disposable cleanup would be retained
        # and retried forever against a now-absent path (#6824 R3).
        if self.on_worktree_removed:
            try:
                self.on_worktree_removed(action.worktree_path)
            except Exception as e:
                logger.warning(
                    issue_log(action.issue_number, "worktree-removed callback failed (worktree already gone): %s"),
                    e,
                )

    def _apply_remove_worktree(self, action: Action) -> ActionResult:
        """Remove a git worktree."""
        assert isinstance(action, RemoveWorktreeAction)

        if not self.worktree_manager:
            return ActionResult.fail(
                action, "No worktree_manager configured"
            )

        try:
            self.worktree_manager.remove(Path(action.worktree_path))
            # Notify async completion processing that worktree is gone
            if self.on_worktree_removed:
                self.on_worktree_removed(action.worktree_path)
            return ActionResult.ok(action, worktree_path=action.worktree_path)
        except Exception as e:
            return ActionResult.fail(action, str(e))

    def _emit_issue_labels_changed(
        self,
        issue_number: int,
        added: list[str],
        removed: list[str],
        issue_key: str = "",
    ) -> None:
        if not added and not removed:
            return
        self.events.publish(make_trace_event(
            EventName.ISSUE_LABELS_CHANGED,
            {
                "issue_number": issue_number,
                "issue_key": issue_key or str(issue_number),
                "added": added,
                "removed": removed,
            },
        ))

    def _log_label_mutation(
        self,
        *,
        level: int,
        issue_number: int,
        operation: str,
        outcome: str,
        label: str,
        reason: str,
        detail: str | None = None,
    ) -> None:
        message = "Label mutation: op=%s outcome=%s label=%s reason=%s"
        args: list[object] = [operation, outcome, label, reason or "-"]
        if detail:
            message += " detail=%s"
            args.append(detail)
        logger.log(level, issue_log(issue_number, message), *args)

    def _emit_pr_view_changed(
        self,
        pr_number: int,
        issue_number: int | None,
        added: list[str],
        removed: list[str],
        issue_key: str = "",
    ) -> None:
        if not added and not removed:
            return
        payload: dict[str, int | list[str] | str] = {
            "pr_number": pr_number,
            "added": added,
            "removed": removed,
        }
        if issue_number is not None:
            payload["issue_number"] = issue_number
            payload["issue_key"] = issue_key or str(issue_number)
        logger.info("[PR_VIEW] Emitting pr.view_changed: pr=%s issue_key=%s added=%s removed=%s",
                     pr_number, payload.get("issue_key"), added, removed)
        self.events.publish(make_trace_event(EventName.PR_VIEW_CHANGED, payload))

    @staticmethod
    def _increment_label_stat(stats: _LabelMutationStats, field_name: LabelMutationStatField) -> None:
        if field_name == "label_add_attempted":
            stats.label_add_attempted += 1
        elif field_name == "label_add_applied":
            stats.label_add_applied += 1
        elif field_name == "label_add_noop":
            stats.label_add_noop += 1
        elif field_name == "label_remove_attempted":
            stats.label_remove_attempted += 1
        elif field_name == "label_remove_applied":
            stats.label_remove_applied += 1
        elif field_name == "label_remove_noop":
            stats.label_remove_noop += 1
        else:
            stats.label_mutation_failed += 1

    def _record_label_stat(self, issue_number: int, field_name: LabelMutationStatField) -> None:
        """Increment label mutation counters for current apply_all batch."""
        if self._active_label_mutation_stats is None:
            return

        self._increment_label_stat(self._active_label_mutation_stats, field_name)

        issue_stats = self._active_label_mutation_by_issue.setdefault(
            issue_number, _LabelMutationStats()
        )
        self._increment_label_stat(issue_stats, field_name)

    def _emit_label_mutation_summary(self) -> None:
        """Emit per-batch label mutation summary event and log line."""
        stats = self._active_label_mutation_stats
        if stats is None or stats.attempted == 0:
            return

        attempted = stats.attempted
        payload: dict[str, object] = dict(stats.to_payload())
        payload["noop_ratio"] = stats.noop / attempted
        payload["failure_ratio"] = stats.label_mutation_failed / attempted
        payload["per_issue"] = [
            {"issue_number": issue_number, **issue_stats.to_payload()}
            for issue_number, issue_stats in sorted(self._active_label_mutation_by_issue.items())
            if issue_stats.attempted > 0
        ]

        self.events.publish(make_trace_event(EventName.LABEL_MUTATION_SUMMARY, payload))
        logger.info(
            "[LABELS] label_mutations attempted=%d applied=%d noop=%d failed=%d add_attempted=%d remove_attempted=%d",
            stats.attempted,
            stats.applied,
            stats.noop,
            stats.label_mutation_failed,
            stats.label_add_attempted,
            stats.label_remove_attempted,
        )
