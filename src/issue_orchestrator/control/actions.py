"""Action dataclasses - the Plan/Apply boundary.

Actions are the output of planning logic and the input to the applier.
They describe WHAT should happen, not HOW.

This separation enables:
- Planning code to be tested without IO (pure logic)
- Applier to be tested with fake ports
- Clear audit trail of decisions

Usage:
    # In workflow/planner
    actions = [
        AddLabelAction(issue_number=123, label="in-progress"),
        LaunchSessionAction(session_type=SessionType.ISSUE, number=123, ...),
    ]

    # In applier
    for action in actions:
        result = applier.apply(action)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..domain.models import (
    AwaitingMergeReconciliationSource,
    AwaitingMergeTerminalStatus,
    DiscoveredFailure,
)
from .action_base import Action as Action, ActionType as ActionType
# Tech-lead action types live in their own module for cohesion and line budget;
# re-exported here (including the domain value objects their fields use) so
# every existing ``from .actions import CreateTechLeadIssueAction`` keeps
# working. The split is one-way: tech_lead_actions imports this module's Action
# base and ActionType enum, never the reverse.
from ..domain.tech_lead_milestone import (
    TechLeadMilestoneIntent as TechLeadMilestoneIntent,
)
from ..domain.tech_lead_session import (
    TechLeadSessionFlavor as TechLeadSessionFlavor,
)
from .tech_lead_actions import (
    TECH_LEAD_ISSUE_CREATION_ACTION_TYPES as TECH_LEAD_ISSUE_CREATION_ACTION_TYPES,
    AppendPatternObservationAction as AppendPatternObservationAction,
    CreateTechLeadCaseFileIssueAction as CreateTechLeadCaseFileIssueAction,
    CreateTechLeadIssueAction as CreateTechLeadIssueAction,
    CreateTechLeadProposalIssueAction as CreateTechLeadProposalIssueAction,
    DiscardTerminalTechLeadProposalOpsAction as DiscardTerminalTechLeadProposalOpsAction,
    KillHungSessionAction as KillHungSessionAction,
    PromoteTechLeadFindingAction as PromoteTechLeadFindingAction,
    ReportPromotedFindingEvidenceAction as ReportPromotedFindingEvidenceAction,
    ResetRetryIssueAction as ResetRetryIssueAction,
    SettleTechLeadPromotionAction as SettleTechLeadPromotionAction,
    SurfaceTechLeadProposalAction as SurfaceTechLeadProposalAction,
)
from .session_manager import SessionType



@dataclass(frozen=True)
class AddLabelAction(Action):
    """Add a label to an issue."""

    issue_number: int = 0
    label: str = ""
    issue_key: str = ""  # stable_id for SSE events; falls back to str(issue_number) when empty
    action_type: ActionType = field(default=ActionType.ADD_LABEL, init=False)


@dataclass(frozen=True)
class RemoveLabelAction(Action):
    """Remove a label from an issue."""

    issue_number: int = 0
    label: str = ""
    issue_key: str = ""  # stable_id for SSE events; falls back to str(issue_number) when empty
    action_type: ActionType = field(default=ActionType.REMOVE_LABEL, init=False)


@dataclass(frozen=True)
class SyncLabelsAction(Action):
    """Synchronize labels on an issue to match desired state."""

    issue_number: int = 0
    add_labels: tuple[str, ...] = field(default_factory=tuple)
    remove_labels: tuple[str, ...] = field(default_factory=tuple)
    issue_key: str = ""  # stable_id for SSE events; falls back to str(issue_number) when empty
    action_type: ActionType = field(default=ActionType.SYNC_LABELS, init=False)


@dataclass(frozen=True)
class ShedRecoveredWorkflowLabelsAction(Action):
    """Shed an issue's transient workflow labels after its work has landed.

    The set of labels to remove (``pr-pending``, ``publish-failed``,
    ``publish-fail-count-N``, blocking labels) is decided at apply time from the
    issue's live labels, so the planner does not need to know the issue's
    current labels (which it usually lacks for already-closed/merged issues).
    """

    issue_number: int = 0
    issue_key: str = ""  # stable_id for SSE events; falls back to str(issue_number) when empty
    action_type: ActionType = field(
        default=ActionType.SHED_RECOVERED_WORKFLOW_LABELS, init=False
    )


@dataclass(frozen=True)
class LaunchSessionAction(Action):
    """Launch a terminal session for an agent."""

    session_type: SessionType = SessionType.ISSUE
    number: int = 0  # Issue or PR number
    command: str = ""
    working_dir: str = ""
    title: Optional[str] = None
    action_type: ActionType = field(default=ActionType.LAUNCH_SESSION, init=False)


@dataclass(frozen=True)
class LaunchValidationRetryAction(Action):
    """Launch a retry session for a failed validation gate."""

    issue_number: int = 0
    retry_count: int = 0
    action_type: ActionType = field(default=ActionType.LAUNCH_VALIDATION_RETRY, init=False)

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("LaunchValidationRetryAction requires a positive issue_number")
        if self.retry_count < 0:
            raise ValueError("LaunchValidationRetryAction requires a non-negative retry_count")


@dataclass(frozen=True)
class StopSessionAction(Action):
    """Stop a terminal session."""

    session_type: SessionType = SessionType.ISSUE
    number: int = 0
    action_type: ActionType = field(default=ActionType.STOP_SESSION, init=False)


@dataclass(frozen=True)
class CreateWorktreeAction(Action):
    """Create a git worktree for an issue."""

    issue_number: int = 0
    branch_name: str = ""
    worktree_path: str = ""
    action_type: ActionType = field(default=ActionType.CREATE_WORKTREE, init=False)


@dataclass(frozen=True)
class RemoveWorktreeAction(Action):
    """Remove a git worktree."""

    worktree_path: str = ""
    action_type: ActionType = field(default=ActionType.REMOVE_WORKTREE, init=False)


@dataclass(frozen=True)
class QueueReviewAction(Action):
    """Queue a PR for code review."""

    issue_number: int = 0
    pr_number: int = 0
    pr_url: str = ""
    branch_name: str = ""
    code_review_label: str = ""  # Label to add (e.g., needs-code-review)
    agent_label: Optional[str] = None  # Agent that created the PR (for per-agent reviewer)
    issue_key: str = ""  # stable_id for SSE events; falls back to str(issue_number) when empty
    issue_labels: tuple[str, ...] = ()
    action_type: ActionType = field(default=ActionType.QUEUE_REVIEW, init=False)


@dataclass(frozen=True)
class QueueRetrospectiveReviewAction(Action):
    """Queue an issue for review of its existing implementation."""

    issue_number: int = 0
    issue_title: str = ""
    agent_label: str = ""
    trigger_label: str = ""
    issue_key: str = ""
    prior_pr_number: int | None = None
    prior_pr_url: str | None = None
    issue_labels: tuple[str, ...] = ()
    action_type: ActionType = field(default=ActionType.QUEUE_RETROSPECTIVE_REVIEW, init=False)


@dataclass(frozen=True)
class QueueReworkAction(Action):
    """Queue an issue for rework."""

    issue_number: int = 0
    pr_number: int = 0
    pr_url: str = ""
    branch_name: str = ""
    rework_cycle: int = 1
    source: str = "review_label"
    feedback: str | None = None
    action_type: ActionType = field(default=ActionType.QUEUE_REWORK, init=False)


@dataclass(frozen=True)
class QueueTechLeadAction(Action):
    """Queue an issue for tech_lead review (failure investigation).

    ``failure`` is a REQUIRED keyword field, not optional: this action serves
    only failure investigations (batch/health anchors ride
    :class:`CreateTechLeadIssueAction`), and every investigation exists because
    a failure was discovered. It carries the typed triggering-failure context
    across the plan/apply boundary: the planner reads it from the per-tick
    ``discovered_failures`` buffer (cleared after planning), and the applier
    stores it on the queue item so the launch-time board snapshot — built on
    a later tick — still contains the investigation's own triggering failure.
    """

    issue_number: int = 0
    title: str = ""
    failure: DiscoveredFailure = field(kw_only=True)
    action_type: ActionType = field(default=ActionType.QUEUE_TECH_LEAD, init=False)


@dataclass(frozen=True)
class EscalateToHumanAction(Action):
    """Escalate an issue to human intervention.

    When applied:
    1. Adds needs_human_label to the PR
    2. Removes needs_rework_label from the PR
    3. Posts an escalation comment explaining why human review is needed
    """

    issue_number: int = 0
    pr_number: int = 0
    escalation_reason: str = ""
    # The next three fields drive the rework-cycles-exceeded comment
    # template and are IGNORED when ``comment_override`` is set (the
    # post-publish path provides its own self-contained body that does
    # not mention rework cycles).
    rework_cycles: int = 0
    max_rework_cycles: int = 3
    latest_review_body: Optional[str] = None
    needs_human_label: str = "blocked-needs-human"
    needs_rework_label: str = "needs-rework"
    issue_key: str = ""  # stable_id for SSE events; falls back to str(issue_number) when empty
    # When set, the applier posts this exact markdown body instead of the
    # default rework-cycles-exceeded template. Used by the post-publish
    # path to explain why an *approved* PR is being escalated (stuck on
    # CI, blocked by branch protection, etc.). Mutually exclusive with
    # the rework-cycles message in practice — see field comments above.
    comment_override: Optional[str] = None
    action_type: ActionType = field(default=ActionType.ESCALATE_TO_HUMAN, init=False)


@dataclass(frozen=True)
class AddCommentAction(Action):
    """Add a comment to an issue or PR."""

    number: int = 0  # Issue or PR number
    comment: str = ""
    is_pr: bool = False
    action_type: ActionType = field(default=ActionType.ADD_COMMENT, init=False)


@dataclass(frozen=True)
class SupersedePullRequestAction(Action):
    """Comment on and close a PR that belongs to discarded work."""

    issue_number: int = 0
    pr_number: int = 0
    comment: str = ""
    action_type: ActionType = field(default=ActionType.SUPERSEDE_PR, init=False)


@dataclass(frozen=True)
class CloseIssueAction(Action):
    """Close an issue; a ``comment`` posts first (best-effort, never blocks)."""

    issue_number: int = 0
    comment: str = ""
    action_type: ActionType = field(default=ActionType.CLOSE_ISSUE, init=False)


@dataclass(frozen=True)
class SetIssueStateAction(Action):
    """Set an issue's open/closed state through the repository host."""

    issue_number: int = 0
    state: str = "open"
    action_type: ActionType = field(default=ActionType.SET_ISSUE_STATE, init=False)

    def __post_init__(self) -> None:
        if self.state not in {"open", "closed"}:
            raise ValueError("SetIssueStateAction state must be 'open' or 'closed'")


@dataclass(frozen=True)
class CleanupSessionAction(Action):
    """Clean up a completed session (close tab, remove worktree).

    Produced by the Planner when a pending cleanup's PR has been reviewed.
    The orchestrator applies it by closing the terminal tab and removing the worktree.
    """

    issue_number: int = 0
    pr_number: int = 0
    terminal_id: str = ""
    worktree_path: str = ""
    close_tabs: bool = True
    remove_worktrees: bool = True
    # A run-scoped tech_lead scratch worktree is DISPOSABLE (throwaway artifacts): the
    # applier force-removes ONLY this identity, never a reusable coding worktree (#6824 F8).
    disposable_worktree: bool = False
    action_type: ActionType = field(default=ActionType.CLEANUP_SESSION, init=False)


@dataclass(frozen=True)
class ReconcileHistoryEntryAction(Action):
    """Reconcile a completed history entry into a terminal PR/issue status.

    The inherited ``reason`` is the status reason persisted to history.
    """

    issue_number: int = 0
    pr_number: int = 0
    pr_url: str = ""
    status: AwaitingMergeTerminalStatus = "closed"
    source: AwaitingMergeReconciliationSource = "pull_request"
    issue_key: str = ""  # stable_id for SSE events; falls back to str(issue_number) when empty
    action_type: ActionType = field(default=ActionType.RECONCILE_HISTORY_ENTRY, init=False)


@dataclass(frozen=True)
class RecoverTerminalIssueAction(Action):
    """Shed an issue's transient workflow labels, then finalize its
    awaiting-merge history — one owner command for the terminal-recovery
    ordering invariant.

    Terminal recovery sheds the transient workflow labels (``pr-pending``,
    ``publish-failed``, ``publish-fail-count-N``, blocking labels) from GitHub
    + the local ``label_store`` *before* the history entry terminalizes: a
    failed shed (or close, below) leaves the entry reconcilable so the next
    discovery pass retries, instead of stranding the labels this P0 removes
    (#6431). The label set is decided at apply time from live labels. The
    inherited ``reason`` is the audit/shed reason; ``status_reason`` is
    persisted to history.
    """

    issue_number: int = 0
    pr_number: int = 0
    pr_url: str = ""
    status: AwaitingMergeTerminalStatus = "closed"
    source: AwaitingMergeReconciliationSource = "pull_request"
    status_reason: str = ""
    issue_key: str = ""  # stable_id for SSE events; falls back to str(issue_number) when empty
    # Close-on-merge fallback: PR merged but no closing reference fired.
    # Advisory — the applier revalidates the destructive precondition against
    # live state (close_on_merge module) using ``merged_at`` as the evidence.
    close_issue: bool = False
    merged_at: str = ""
    action_type: ActionType = field(
        default=ActionType.RECOVER_TERMINAL_ISSUE, init=False
    )


@dataclass(frozen=True)
class EnqueueToMergeQueueAction(Action):
    """Enqueue a reviewer-approved PR into the provider's native merge queue.

    Produced by the planner from a ``DiscoveredMergeQueueEnqueue`` fact and
    executed by the ActionApplier, which performs the protected enqueue via the
    repository host. GitHub remains the merge authority.
    """

    issue_number: int = 0
    pr_number: int = 0
    pr_url: str = ""
    issue_key: str = ""  # stable_id for SSE events; falls back to str(issue_number) when empty
    action_type: ActionType = field(default=ActionType.ENQUEUE_TO_MERGE_QUEUE, init=False)


# Action result types


class ActionResultType(Enum):
    """Result of applying an action."""

    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"  # Already applied or not applicable


@dataclass(frozen=True)
class ActionResult:
    """Result of applying an action.

    Attributes:
        action: The action that was applied
        result_type: Success, failure, or skipped
        error: Error message if failed
        details: Additional details about the result
    """

    action: Action
    result_type: ActionResultType
    error: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """Check if the action succeeded."""
        return self.result_type == ActionResultType.SUCCESS

    @property
    def issue_number(self) -> int | None:
        """Canonical issue number this result carries, or ``None`` if it carries
        none (e.g. the session_launcher-less fallback launch, which has no
        ``Session`` and thus no canonical issue identity).

        A typed accessor over the untyped ``details`` bag: it fails fast on a
        present-but-non-``int`` value (a producer/contract bug, ``bool`` included
        since ``bool`` is an ``int`` subclass) rather than letting a malformed
        identity flow silently downstream. Consumers that *require* the number
        (e.g. blocked->front launch cleanup for an issue launch) enforce presence
        themselves; see #6873 N4.
        """
        value = self.details.get("issue_number")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"ActionResult.issue_number must be int, got {value!r}")
        return value

    @classmethod
    def ok(cls, action: Action, **details: str | int | bool | list[str] | None) -> "ActionResult":
        """Create a successful result."""
        return cls(
            action=action,
            result_type=ActionResultType.SUCCESS,
            details=details,
        )

    @classmethod
    def fail(cls, action: Action, error: str, **details: str | int | bool | list[str] | None) -> "ActionResult":
        """Create a failed result."""
        return cls(
            action=action,
            result_type=ActionResultType.FAILURE,
            error=error,
            details=details,
        )

    @classmethod
    def skip(
        cls,
        action: Action,
        reason: str,
        **details: str | int | bool | list[str] | None,
    ) -> "ActionResult":
        """Create a skipped result."""
        return cls(
            action=action,
            result_type=ActionResultType.SKIPPED,
            details={"skip_reason": reason, **details},
        )
