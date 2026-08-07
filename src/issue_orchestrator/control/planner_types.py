"""Planner input/output contract types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Sequence

from ..domain.models import (
    CleanupFacts,
    DiscoveredAwaitingMergeDrift,
    DiscoveredAwaitingMergeEscalation,
    DiscoveredAwaitingMergeReconciliation,
    DiscoveredEscalation,
    DiscoveredFailure,
    DiscoveredMergeQueueEnqueue,
    DiscoveredReview,
    DiscoveredRetrospectiveReview,
    DiscoveredRework,
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingTechLeadReview,
    PendingValidationRetry,
    Session,
    TechLeadFacts,
)
from ..ports.issue import Issue
from .actions import Action, ActionType


@dataclass(frozen=True)
class E2ESlotSignals:
    """Observation of how a first-class E2E workload relates to worker slots.

    ``occupies_slot`` — an E2E run is active right now, so it holds one worker
    slot (the planner drops worker capacity by 1). ``due`` — the suite is due
    and no run is active, so the planner reserves a slot for it. At most one is
    ever True; both are False when ``e2e.occupies_session_slot`` is off, which
    the reader factory guards so the default path does zero extra work.

    Lives in the control layer (the planner consumes it as a fact); the infra
    reader that PRODUCES it (``infra.e2e_slot_policy.make_e2e_slot_reader``)
    imports it from here, so the control layer never reaches into subprocess/git
    to learn E2E state.
    """

    occupies_slot: bool = False
    due: bool = False


if TYPE_CHECKING:
    from ..domain.models import OrchestratorState


@dataclass(frozen=True)
class OrchestratorSnapshot:
    """Immutable snapshot of orchestrator state for planning.

    This is the input to the planner - a point-in-time view of all
    relevant state needed to make planning decisions.
    """

    issues: tuple[Issue, ...]
    active_sessions: tuple[Session, ...]
    pending_reviews: tuple[PendingReview, ...]
    pending_reworks: tuple[PendingRework, ...]
    pending_tech_lead: tuple[PendingTechLeadReview, ...]
    paused: bool
    pending_retrospective_reviews: tuple[PendingRetrospectiveReview, ...] = field(default_factory=tuple)
    pending_validation_retries: tuple[PendingValidationRetry, ...] = field(default_factory=tuple)
    priority_queue: tuple[int, ...] = field(default_factory=tuple)
    issues_started_count: int = 0
    max_issues_to_start: Optional[int] = None
    # Discovered facts for Planner-centric queue management
    discovered_reviews: tuple[DiscoveredReview, ...] = field(default_factory=tuple)
    discovered_retrospective_reviews: tuple[DiscoveredRetrospectiveReview, ...] = field(default_factory=tuple)
    discovered_awaiting_merge_reconciliations: tuple[
        DiscoveredAwaitingMergeReconciliation, ...
    ] = field(default_factory=tuple)
    discovered_awaiting_merge_drifts: tuple[
        DiscoveredAwaitingMergeDrift, ...
    ] = field(default_factory=tuple)
    discovered_reworks: tuple[DiscoveredRework, ...] = field(default_factory=tuple)
    discovered_escalations: tuple[DiscoveredEscalation, ...] = field(default_factory=tuple)
    discovered_awaiting_merge_escalations: tuple[
        DiscoveredAwaitingMergeEscalation, ...
    ] = field(default_factory=tuple)
    discovered_merge_queue_enqueues: tuple[
        DiscoveredMergeQueueEnqueue, ...
    ] = field(default_factory=tuple)
    discovered_failures: tuple[DiscoveredFailure, ...] = field(default_factory=tuple)
    # Unacknowledged stuck-sweep escalations to (re-)label needs-human; the
    # planner emits the idempotent label via the Applier (#6824 R1, label-only).
    stuck_sweep_escalations: tuple[int, ...] = field(default_factory=tuple)
    tech_lead_facts: Optional[TechLeadFacts] = None
    # Authoritative lifecycle reads of queued investigation subjects the
    # filtered board did not carry (#6994 F4). Launch-time revalidation
    # consults these so a subject CLOSED while queued is observed as closed
    # rather than merely absent.
    tech_lead_subjects: tuple["Issue", ...] = ()
    cleanup_facts: Optional[CleanupFacts] = None
    # Issues with stale in-progress labels (label present but no active session)
    stale_in_progress_issues: tuple[Issue, ...] = field(default_factory=tuple)
    # Issues with stale claims (io:claimed label but claim is expired)
    stale_claim_issues: tuple[Issue, ...] = field(default_factory=tuple)
    # Issues that failed this cycle - skip until cache refresh (prevents immediate retry)
    failed_this_cycle: frozenset[int] = field(default_factory=frozenset)
    # Issues that completed this session (have session_history entries)
    session_history_issue_numbers: frozenset[int] = field(default_factory=frozenset)
    # E2E-as-first-class-workload facts (e2e.occupies_session_slot). Both are
    # False in the default (flag off), so every capacity path is byte-for-byte
    # unchanged. When the flag is on, EXACTLY one can be true on a given tick:
    #   * e2e_occupies_slot: an E2E run is active RIGHT NOW, so it holds one
    #     worker slot — ``_launch_budgets`` drops worker capacity by 1.
    #   * e2e_due: the suite is due to run and no run is active, so the planner
    #     reserves one worker slot AFTER completion work but BEFORE new issues,
    #     letting a due suite beat new issues without preempting reviews/reworks.
    e2e_occupies_slot: bool = False
    e2e_due: bool = False

    @property
    def active_count(self) -> int:
        """Number of currently active sessions."""
        return len(self.active_sessions)

    @property
    def active_issue_numbers(self) -> frozenset[int]:
        """Issue numbers with active sessions."""
        return frozenset(s.issue.number for s in self.active_sessions)

    @classmethod
    def from_state(
        cls,
        issues: Sequence[Issue],
        state: "OrchestratorState",
        max_issues_to_start: Optional[int] = None,
        discovered_reviews: Sequence[DiscoveredReview] = (),
        discovered_retrospective_reviews: Sequence[DiscoveredRetrospectiveReview] = (),
        discovered_awaiting_merge_reconciliations: Sequence[
            DiscoveredAwaitingMergeReconciliation
        ] = (),
        discovered_awaiting_merge_drifts: Sequence[
            DiscoveredAwaitingMergeDrift
        ] = (),
        discovered_reworks: Sequence[DiscoveredRework] = (),
        discovered_escalations: Sequence[DiscoveredEscalation] = (),
        discovered_awaiting_merge_escalations: Sequence[
            DiscoveredAwaitingMergeEscalation
        ] = (),
        discovered_merge_queue_enqueues: Sequence[
            DiscoveredMergeQueueEnqueue
        ] = (),
        discovered_failures: Sequence[DiscoveredFailure] = (),
        tech_lead_facts: Optional[TechLeadFacts] = None,
        tech_lead_subjects: tuple["Issue", ...] = (),
        cleanup_facts: Optional[CleanupFacts] = None,
        stale_in_progress_issues: Sequence[Issue] = (),
        stale_claim_issues: Sequence[Issue] = (),
    ) -> "OrchestratorSnapshot":
        """Create snapshot from mutable state.

        Args:
            issues: Current list of issues from GitHub
            state: Mutable orchestrator state object
            max_issues_to_start: Optional limit on issues to start this session
            discovered_reviews: Reviews discovered from session completions/scans
            discovered_awaiting_merge_reconciliations: Awaiting-merge history
                transitions discovered from scans
            discovered_awaiting_merge_drifts: Open issues whose pr-pending label
                no longer matches PR state
            discovered_reworks: Reworks discovered from scans
            discovered_escalations: Escalations discovered from scans
            discovered_failures: Failures discovered from session completions (for tech_lead)
            tech_lead_facts: Facts about tech_lead trigger conditions
            cleanup_facts: Facts about pending cleanups and their review status
            stale_in_progress_issues: Issues with stale in-progress labels
            stale_claim_issues: Issues with stale/expired claims
        """
        return cls(
            issues=tuple(issues),
            active_sessions=tuple(state.active_sessions),
            pending_reviews=tuple(state.pending_reviews),
            pending_retrospective_reviews=tuple(state.pending_retrospective_reviews),
            pending_reworks=tuple(state.pending_reworks),
            pending_tech_lead=tuple(state.pending_tech_lead_reviews),
            pending_validation_retries=tuple(state.pending_validation_retries),
            paused=state.paused,
            priority_queue=tuple(state.priority_queue),
            issues_started_count=state.issues_started_count,
            max_issues_to_start=max_issues_to_start,
            discovered_reviews=tuple(discovered_reviews),
            discovered_retrospective_reviews=tuple(discovered_retrospective_reviews),
            discovered_awaiting_merge_reconciliations=tuple(
                discovered_awaiting_merge_reconciliations
            ),
            discovered_awaiting_merge_drifts=tuple(
                discovered_awaiting_merge_drifts
            ),
            discovered_reworks=tuple(discovered_reworks),
            discovered_escalations=tuple(discovered_escalations),
            discovered_awaiting_merge_escalations=tuple(
                discovered_awaiting_merge_escalations
            ),
            discovered_merge_queue_enqueues=tuple(
                discovered_merge_queue_enqueues
            ),
            discovered_failures=tuple(discovered_failures),
            tech_lead_facts=tech_lead_facts,
            tech_lead_subjects=tech_lead_subjects,
            cleanup_facts=cleanup_facts,
            stale_in_progress_issues=tuple(stale_in_progress_issues),
            stale_claim_issues=tuple(stale_claim_issues),
            failed_this_cycle=frozenset(state.failed_this_cycle),
        )


@dataclass(frozen=True)
class SkippedItem:
    """An item that was considered but not acted upon."""

    item_type: str  # "issue", "review", "rework", "tech_lead"
    number: int
    reason: str


@dataclass(frozen=True)
class Plan:
    """Complete plan for one planning cycle.

    A Plan is the output of the planner - it describes what actions
    should be taken, plus explanations for items that were skipped.
    """

    actions: tuple[Action, ...]
    skipped: tuple[SkippedItem, ...]

    @classmethod
    def empty(cls) -> "Plan":
        """Create an empty plan (no actions)."""
        return cls(actions=(), skipped=())

    @property
    def action_count(self) -> int:
        """Number of actions in the plan."""
        return len(self.actions)

    def actions_of_type(self, action_type: ActionType) -> list[Action]:
        """Get all actions of a specific type."""
        return [a for a in self.actions if a.action_type == action_type]

    def has_action_for(self, number: int) -> bool:
        """Check if plan has any action for a given issue/PR number."""
        for action in self.actions:
            if getattr(action, "number", None) == number:
                return True
            if getattr(action, "issue_number", None) == number:
                return True
            if getattr(action, "pr_number", None) == number:
                return True
        return False


@dataclass
class PlanContext:
    issue_labels_by_number: dict[int, tuple[str, ...]]
    planned_adds_by_issue: dict[int, set[str]] = field(default_factory=dict)
    planned_removes_by_issue: dict[int, set[str]] = field(default_factory=dict)

    def issue_labels(self, issue_number: int) -> tuple[str, ...]:
        return self.issue_labels_by_number.get(issue_number, ())

    def planned_adds(self, issue_number: int) -> set[str]:
        return self.planned_adds_by_issue.setdefault(issue_number, set())

    def planned_removes(self, issue_number: int) -> set[str]:
        return self.planned_removes_by_issue.setdefault(issue_number, set())

    def should_add_label(self, issue_number: int, label: str) -> bool:
        return (
            label not in self.issue_labels(issue_number)
            and label not in self.planned_adds(issue_number)
        )

    def should_remove_label(self, issue_number: int, label: str) -> bool:
        return (
            label in self.issue_labels(issue_number)
            and label not in self.planned_removes(issue_number)
            and label not in self.planned_adds(issue_number)
        )

    def record_add(self, issue_number: int, label: str) -> None:
        self.planned_adds(issue_number).add(label)

    def record_remove(self, issue_number: int, label: str) -> None:
        self.planned_removes(issue_number).add(label)
