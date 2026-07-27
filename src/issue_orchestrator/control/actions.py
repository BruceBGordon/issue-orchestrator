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
from typing import TYPE_CHECKING, Any, Optional

from ..domain.models import (
    AwaitingMergeReconciliationSource,
    AwaitingMergeTerminalStatus,
    DiscoveredFailure,
)
# TechLeadMilestoneIntent is a pure domain value object (moved to
# domain/tech_lead_milestone.py); imported here for the CreateTechLeadIssueAction
# field default and re-exported so existing `from ...control.actions import
# TechLeadMilestoneIntent` importers keep working.
from ..domain.tech_lead_milestone import TechLeadMilestoneIntent
from ..domain.tech_lead_session import TechLeadSessionFlavor
# Action result types are the Apply/report half of this boundary. They live in
# `action_results.py` (an ActionResult is about *what happened*, not about the
# action vocabulary) and are re-exported here so existing importers are
# unaffected.
from .action_results import ActionResult, ActionResultType
from .session_manager import SessionType

if TYPE_CHECKING:
    from ..domain.tech_lead_session import StoredTechLeadOp
    from .reconciliation import ExpectedState


class ActionType(Enum):
    """Types of actions the orchestrator can take."""

    # Label operations
    ADD_LABEL = "add_label"
    REMOVE_LABEL = "remove_label"
    SYNC_LABELS = "sync_labels"
    SHED_RECOVERED_WORKFLOW_LABELS = "shed_recovered_workflow_labels"

    # Provider outage impact: owns the blocked-label transition *and* the
    # durable issue-scoped record of it (see control/provider_impact.py).
    APPLY_PROVIDER_IMPACT = "apply_provider_impact"

    # Session operations
    LAUNCH_SESSION = "launch_session"
    LAUNCH_VALIDATION_RETRY = "launch_validation_retry"
    STOP_SESSION = "stop_session"

    # GitHub operations
    CREATE_PR = "create_pr"
    ADD_COMMENT = "add_comment"
    SUPERSEDE_PR = "supersede_pr"
    CLOSE_ISSUE = "close_issue"
    SET_ISSUE_STATE = "set_issue_state"

    # Worktree operations
    CREATE_WORKTREE = "create_worktree"
    REMOVE_WORKTREE = "remove_worktree"

    # Queue operations
    QUEUE_REVIEW = "queue_review"
    QUEUE_RETROSPECTIVE_REVIEW = "queue_retrospective_review"
    QUEUE_REWORK = "queue_rework"
    QUEUE_TECH_LEAD = "queue_tech_lead"

    # Issue creation
    CREATE_TECH_LEAD_ISSUE = "create_tech_lead_issue"

    # Gated act-level proposal issue: create + record the stored op (#6778)
    CREATE_TECH_LEAD_PROPOSAL_ISSUE = "create_tech_lead_proposal_issue"

    # Pattern case-file issue: create + record the pattern ledger row (#6781)
    CREATE_TECH_LEAD_CASE_FILE_ISSUE = "create_tech_lead_case_file_issue"

    # Tech Lead decision proposals (event-only surfacing, ADR-0031)
    SURFACE_TECH_LEAD_PROPOSAL = "surface_tech_lead_proposal"

    # Act-level tech_lead execution: scratch reset via the reset owner (#6764)
    RESET_RETRY_ISSUE = "reset_retry_issue"

    # Act-level tech_lead execution: terminate issue runtime (#6778, approved ops)
    KILL_HUNG_SESSION = "kill_hung_session"

    # Confirm-and-discard terminal gated-proposal ledger rows (#6779 R7/R10):
    # the single mutating boundary for proposal-op cleanup, applied off the
    # read-only fact path so fact gathering stays side-effect free.
    DISCARD_TERMINAL_TECH_LEAD_PROPOSAL_OPS = "discard_terminal_tech_lead_proposal_ops"

    # Escalation
    ESCALATE_TO_HUMAN = "escalate_to_human"

    # Merge queue (optional GitHub Merge Queue integration)
    ENQUEUE_TO_MERGE_QUEUE = "enqueue_to_merge_queue"

    # Cleanup operations
    CLEANUP_SESSION = "cleanup_session"

    # History operations
    RECONCILE_HISTORY_ENTRY = "reconcile_history_entry"

    # Terminal recovery (shed transient labels, then finalize history)
    RECOVER_TERMINAL_ISSUE = "recover_terminal_issue"


# These actions deliberately share one apply-time owner: all create a
# tech-lead-authored issue, while proposal and case-file variants additionally
# finalize their respective authority-ledger record.
TECH_LEAD_ISSUE_CREATION_ACTION_TYPES: frozenset[ActionType] = frozenset(
    {
        ActionType.CREATE_TECH_LEAD_ISSUE,
        ActionType.CREATE_TECH_LEAD_PROPOSAL_ISSUE,
        ActionType.CREATE_TECH_LEAD_CASE_FILE_ISSUE,
    }
)


@dataclass(frozen=True)
class Action:
    """Base action class.

    All actions are immutable data objects that describe an intended change.
    The actual execution is handled by the ActionApplier.

    Mutating actions (those that write to GitHub) should have `expected` set
    to enable optimistic concurrency control. Before applying the mutation,
    the applier verifies current state satisfies `expected`. If not, it raises
    ReconciliationRequired instead of applying the mutation.
    """

    action_type: ActionType
    reason: str = ""  # Why this action is being taken (for audit)
    # Expected state constraints for reconciliation (required for mutating actions)
    expected: Optional["ExpectedState"] = None

    def __post_init__(self):
        # Validate that subclasses set the correct action_type
        pass


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
class CreateTechLeadIssueAction(Action):
    """Create a tech_lead review issue when PR threshold is met.

    The Planner produces this when tech_lead_facts.pr_count >= threshold.
    The orchestrator applies it by creating the GitHub issue. Both creation
    paths — the planner's batch tracking issue and decision-driven follow-up
    issues — share this one action, so the applier is the single milestone
    resolution boundary.
    """

    title: str = ""
    body: str = ""
    labels: tuple[str, ...] = field(default_factory=tuple)
    pr_count: int = 0
    milestone: TechLeadMilestoneIntent = field(default_factory=TechLeadMilestoneIntent)
    # Non-empty only for an immediate problem-storm health review. Preserves
    # the exact discovery facts across create -> durable ledger -> pending
    # queue -> launch, so the cohort the anchor is authorized over is the one
    # that was actually discovered. The board snapshot's failure list is
    # deliberately broader board context and is never the authority (#6780).
    storm_problems: tuple[DiscoveredFailure, ...] = ()
    # The lifecycle variant this anchor is authored as. The owner that decides
    # to create the anchor (health-review trigger vs batch planning) states it
    # here, so the applier reports the decision instead of re-deriving it from
    # marker labels at the creation boundary (#6780).
    flavor: TechLeadSessionFlavor = TechLeadSessionFlavor.BATCH_REVIEW
    # The board fingerprint the health-review trigger fired on, carried to the
    # post-creation stamp so "reviewed" records what justified the review, not a
    # recompute against a board that by then holds this anchor. "" (batch, or no
    # facts) means never-reviewed: fails toward reviewing (ADR-0031 §4, #6793).
    health_review_fingerprint: str = ""
    # Expedite-lane intent (#6870): set for a decision-driven create_issue the
    # tech lead marked urgent. The applier's create boundary reads it (with the
    # gate presence) to front-queue the new issue via the expedite owner.
    expedite: bool = False
    action_type: ActionType = field(default=ActionType.CREATE_TECH_LEAD_ISSUE, init=False)


@dataclass(frozen=True)
class CreateTechLeadProposalIssueAction(CreateTechLeadIssueAction):
    """Create a GATED act-level tech_lead proposal issue (#6778, ADR-0031 §2).

    A ``CreateTechLeadIssueAction`` that additionally carries the typed
    :class:`StoredTechLeadOp`. The applier creates the issue AND records the op
    create-once in the orchestrator-owned authority store, keyed by the new
    issue number, then links the proposal from the session's anchor issue.
    The issue body is human documentation only — execution consumes the
    stored op, never the body (tamper boundary).
    """

    op: "StoredTechLeadOp" = field(kw_only=True)
    anchor_issue_number: int = 0
    action_type: ActionType = field(
        default=ActionType.CREATE_TECH_LEAD_PROPOSAL_ISSUE, init=False
    )

    def __post_init__(self) -> None:
        from ..domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL

        # Self-validating type: an ungated proposal issue would be
        # schedulable before any approval. (Baseline note: this branch is an
        # accepted control_policy_branch_sites entry — the invariant is
        # inherently about the gate label, not scattered policy.)
        if PROPOSED_TECH_LEAD_LABEL not in self.labels:
            raise ValueError(
                "CreateTechLeadProposalIssueAction must carry the"
                f" {PROPOSED_TECH_LEAD_LABEL!r} gate label"
            )
        if self.anchor_issue_number <= 0:
            raise ValueError(
                "CreateTechLeadProposalIssueAction requires a positive"
                " anchor_issue_number"
            )


@dataclass(frozen=True)
class CreateTechLeadCaseFileIssueAction(CreateTechLeadIssueAction):
    """Create a pattern CASE-FILE issue for a flag_pattern proposal (#6781).

    A ``CreateTechLeadIssueAction`` that additionally carries the pattern
    signature (the durable ledger key) and optional area. The applier
    creates the issue AND records the (signature -> issue) ledger row
    create-once in the orchestrator-owned authority store; later
    flag_pattern proposals with the same signature comment evidence onto
    the recorded issue instead of filing a second one. The issue body is
    human documentation only — dedup consults the ledger, never the body
    (tamper boundary).
    """

    pattern_signature: str = ""
    area: str | None = None
    dedup_comment: str = ""
    additional_observation_comments: tuple[str, ...] = ()
    action_type: ActionType = field(
        default=ActionType.CREATE_TECH_LEAD_CASE_FILE_ISSUE, init=False
    )

    def __post_init__(self) -> None:
        from ..domain.tech_lead_session import require_case_file_observation_label

        # Self-validating type: an empty signature could never accrue
        # evidence. The observation-label invariant is delegated to its
        # domain owner (an unlabeled case file would be schedulable work).
        if not self.pattern_signature.strip():
            raise ValueError(
                "CreateTechLeadCaseFileIssueAction requires a non-empty"
                " pattern_signature (the ledger key)"
            )
        if not self.dedup_comment.strip():
            raise ValueError(
                "CreateTechLeadCaseFileIssueAction requires a non-empty"
                " dedup_comment for apply-time ledger reconciliation"
            )
        require_case_file_observation_label(self.labels)


@dataclass(frozen=True)
class SurfaceTechLeadProposalAction(Action):
    """Surface a tech_lead decision proposal without executing it (ADR-0031).

    Emitted for propose-mode (shadow) authority, ``flag_pattern`` records,
    and rejected decision artifacts. The applier only publishes a trace
    event (``TECH_LEAD_ACTION_PROPOSED``, or ``TECH_LEAD_DECISION_REJECTED`` when
    ``mode == "rejected"``) — it makes NO GitHub calls.

    ``mode`` values:
    - ``"shadow"`` — propose-mode authority: recorded as would-have-done.
    - ``"pattern"`` — a ``flag_pattern`` proposal (its execution IS the record).
    - ``"rejected"`` — the decision artifact pair failed validation;
      ``proposal_type`` is ``"decision"`` and ``body_preview`` carries the
      failure detail.
    """

    issue_number: int = 0  # The tech_lead session's anchor issue
    action_id: str = ""
    proposal_type: str = ""
    target_number: int = 0  # 0 = no target
    target_is_pr: bool = False
    title: str = ""
    body_preview: str = ""  # Capped at 500 chars by the construction site
    finding_ids: tuple[str, ...] = ()
    mode: str = ""  # "shadow" | "pattern" | "rejected"
    action_type: ActionType = field(
        default=ActionType.SURFACE_TECH_LEAD_PROPOSAL, init=False
    )


@dataclass(frozen=True)
class ResetRetryIssueAction(Action):
    """Execute a tech_lead ``reset_retry`` proposal via the reset owner (#6764).

    Planned by ``plan_tech_lead_decision_actions`` ONLY when
    ``tech_lead.authority.reset_retry`` is ``execute``. Proposals are
    stale-checkable facts, not commands (ADR-0031 §2): the applier's owner
    re-validates the recorded preconditions against current state at
    execution time and downgrades to a surfaced proposal
    (``TECH_LEAD_ACTION_PROPOSED``, ``mode="stale_downgrade"``) when the board
    has moved — no mutations are posted on the downgrade path.

    ``anchor_issue_number`` is the tech_lead session's anchor issue — the event
    surface a downgrade is reported against, mirroring
    :class:`SurfaceTechLeadProposalAction`. For failure investigations and
    health reviews the immutable launch scope forces
    ``issue_number == anchor_issue_number``.
    """

    issue_number: int = 0  # The issue to scratch-reset (the proposal's target)
    rationale: str = ""  # The agent's recorded rationale (proposal body)
    proposal_id: str = ""  # The decision artifact action id (A<n>)
    finding_ids: tuple[str, ...] = ()
    anchor_issue_number: int = 0
    # Set (>0) when this execution consumes an APPROVED gated proposal's
    # stored op (#6778): the applier then finalizes the proposal issue
    # (outcome comment + close + discard_op). 0 = direct execute-authority.
    proposal_issue_number: int = 0
    action_type: ActionType = field(default=ActionType.RESET_RETRY_ISSUE, init=False)

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("ResetRetryIssueAction requires a positive issue_number")
        if not self.proposal_id:
            raise ValueError("ResetRetryIssueAction requires the proposal id")


@dataclass(frozen=True)
class KillHungSessionAction(Action):
    """Execute an APPROVED ``kill_hung_session`` proposal op (#6778).

    Planned ONLY from an approved gated proposal's :class:`StoredTechLeadOp`
    (there is no direct execute-authority tier yet — startup rejects
    ``tech_lead.authority.kill_hung_session: execute``). The applier's owner
    (``tech_lead_kill_session``) re-validates that the target issue still has an
    active session and applies the issue-runtime termination boundary — the
    same ``terminate_issue_runtime`` the reset owner uses, WITHOUT the reset.
    Stale proposals downgrade with no mutations, mirroring ``reset_retry``.
    """

    issue_number: int = 0  # The issue whose runtime is terminated (op target)
    rationale: str = ""  # The agent's recorded rationale (stored op)
    proposal_id: str = ""  # The decision artifact action id (A<n>)
    finding_ids: tuple[str, ...] = ()
    anchor_issue_number: int = 0  # Event surface: the proposal issue
    proposal_issue_number: int = 0  # The gated proposal issue to finalize
    # The active session run id the proposal bound its consent to (#6779 R1).
    # The applier's kill owner refuses to terminate unless the target issue's
    # LIVE session still matches this id, so a replacement session started
    # before approval is never killed.
    target_session_id: str = ""
    action_type: ActionType = field(default=ActionType.KILL_HUNG_SESSION, init=False)

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("KillHungSessionAction requires a positive issue_number")
        if not self.proposal_id:
            raise ValueError("KillHungSessionAction requires the proposal id")
        if self.proposal_issue_number <= 0:
            raise ValueError(
                "KillHungSessionAction requires the gated proposal issue number"
                " (there is no direct execute tier for kill_hung_session)"
            )


@dataclass(frozen=True)
class DiscardTerminalTechLeadProposalOpsAction(Action):
    """Confirm-and-discard terminal gated-proposal ledger rows (#6779 R7/R10).

    Emitted by the planner from a read-only fact (``candidate_issue_numbers``):
    ledger op rows whose proposal issue was ABSENT from the exhaustive open
    scan. Absence alone is not proof of terminality — an exhaustive-scan
    truncation (a later-page API failure, or a >2000-issue repo) can drop a
    still-open proposal from the scan. So the applier's owner CONFIRMS each
    candidate with a fresh targeted issue read before discarding: a deleted or
    closed issue is terminal and its op is discarded; a still-open issue was a
    pagination gap and its live op is preserved. This keeps fact gathering
    read-only while routing the (formerly scattered) discard mutation through
    one invariant-enforcing boundary.
    """

    candidate_issue_numbers: tuple[int, ...] = ()
    action_type: ActionType = field(
        default=ActionType.DISCARD_TERMINAL_TECH_LEAD_PROPOSAL_OPS, init=False
    )


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
    """Close an issue through the repository host."""

    issue_number: int = 0
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

    Terminal recovery must shed the transient workflow labels (``pr-pending``,
    ``publish-failed``, ``publish-fail-count-N``, blocking labels) from GitHub +
    the local ``label_store`` *before* the history entry transitions to its
    terminal status. The applier sheds first and finalizes history only on
    success: if the (best-effort, GitHub-write) shed fails, the history entry is
    left in its reconcilable awaiting-merge status so the next awaiting-merge
    discovery pass re-finds and retries the cleanup, instead of terminalizing
    the entry and stranding exactly the labels this P0 removes (#6431).

    The exact label set is decided at apply time from the issue's live labels,
    so the planner need not know the (usually closed/merged) issue's labels.
    The inherited ``reason`` is the audit/shed reason; ``status_reason`` is the
    status reason persisted to history.
    """

    issue_number: int = 0
    pr_number: int = 0
    pr_url: str = ""
    status: AwaitingMergeTerminalStatus = "closed"
    source: AwaitingMergeReconciliationSource = "pull_request"
    status_reason: str = ""
    issue_key: str = ""  # stable_id for SSE events; falls back to str(issue_number) when empty
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
