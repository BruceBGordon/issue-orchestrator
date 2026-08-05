"""Planner - pure policy decisions.

The planner answers "should we?" questions without side effects.
It takes an immutable snapshot of state and returns a Plan describing
what actions should be taken.

This separation from the orchestrator enables:
- Pure, fast tests (no mocks for tmux/GitHub)
- Explainability ("why didn't issue X run?")
- Reuse across execution strategies (tmux, cloud)

Rule of thumb:
- "Should we?" → Planner (this module)
- "Can we?" → Orchestrator
- "How?" → Adapters

Usage:
    snapshot = orchestrator.create_snapshot()
    plan = planner.plan(snapshot)
    orchestrator.apply(plan)
"""

import logging
import re
import time
from typing import TYPE_CHECKING, Callable, Optional

from ..infra.config import Config
from ..infra.logging_config import issue_log
from ..ports.issue import Issue
from ..domain.models import (
    PendingTechLeadReview,
    TechLeadFacts,
    active_retrospective_review_issue_numbers,
)
from ..domain.post_publish_escalation import build_post_publish_escalation_comment
from ..domain.tech_lead_naming import TECH_LEAD_DISPLAY_NAME
from ..domain.tech_lead_session import TechLeadCreationOrigin, TechLeadSessionFlavor

if TYPE_CHECKING:
    from .provider_resilience import ProviderResilienceManager
    from .label_manager import LabelManager
from .scheduler import Scheduler
from .dependency_evaluator import DependencyEvaluator
from .provider_availability import ProviderAvailabilityPolicy
from .workflows import (
    ReviewWorkflow,
    ReviewDecision,
    RetrospectiveReviewWorkflow,
    RetrospectiveReviewDecision,
    ReworkWorkflow,
    ReworkDecision,
    TechLeadWorkflow,
    TechLeadDecision,
)
from .actions import (
    Action,
    ActionType,
    AddCommentAction,
    AddLabelAction,
    RemoveLabelAction,
    LaunchSessionAction,
    LaunchValidationRetryAction,
    QueueReviewAction,
    QueueRetrospectiveReviewAction,
    QueueReworkAction,
    CreateTechLeadIssueAction,
    EnqueueToMergeQueueAction,
    EscalateToHumanAction,
    CleanupSessionAction,
    ReconcileHistoryEntryAction,
    RecoverTerminalIssueAction,
    SessionType,
    SyncLabelsAction,
)
from .awaiting_merge_post_publish_policy import (
    build_post_publish_validation_comment,
    POST_PUBLISH_VALIDATION_SOURCE,
)
from .queue_decision_log import QueueDecisionLog
from .reactive_tech_lead_planning import plan_reactive_tech_lead
from .tech_lead_launch_log import TechLeadLaunchLog
from .tech_lead_ledger_planning import plan_tech_lead_ledger_actions
from .tech_lead_reaction import TechLeadReactionPolicy
from .worker_budget import (
    TechLeadSlotAvailability,
    active_tech_lead_session_count,
    tech_lead_slot_availability,
    worker_slot_availability,
)
from .reconciliation import build_expected_for_mutation
from .stuck_sweep import build_stuck_sweep_escalation_actions
from .planner_types import OrchestratorSnapshot, Plan, PlanContext, SkippedItem
from .tech_lead_issue_policy import (
    apply_tech_lead_priority_prefix,
    batch_review_issue_labels,
    tech_lead_issue_milestone_intent,
)

logger = logging.getLogger(__name__)

# Launch action kinds that occupy a worker slot when planned — the single source
# of truth the capacity counter uses so a new launch kind can't silently escape
# the worker budget (#6892 review F1/F2). A provider-skip label action is
# deliberately absent (it launches nothing).
_CAPACITY_CONSUMING_LAUNCH_TYPES: frozenset[ActionType] = frozenset(
    {ActionType.LAUNCH_SESSION, ActionType.LAUNCH_VALIDATION_RETRY}
)


class Planner:
    """Pure policy decisions - no side effects.

    The planner takes a snapshot of current state and returns a Plan
    describing what actions should be taken. It delegates to:
    - Scheduler for issue prioritization and availability
    - DependencyEvaluator for dependency checking
    - Workflow classes for review/rework/tech_lead decisions

    The planner does NOT:
    - Make API calls
    - Mutate state
    - Start sessions
    - Emit events (beyond planning trace)
    """

    def __init__(
        self,
        config: Config,
        scheduler: Scheduler,
        dependency_evaluator: Optional[DependencyEvaluator] = None,
        review_workflow: Optional[ReviewWorkflow] = None,
        retrospective_review_workflow: Optional[RetrospectiveReviewWorkflow] = None,
        rework_workflow: Optional[ReworkWorkflow] = None,
        tech_lead_workflow: Optional[TechLeadWorkflow] = None,
        provider_resilience: Optional["ProviderResilienceManager"] = None,
        label_manager: Optional["LabelManager"] = None,
        clock: Callable[[], float] = time.time,
    ):
        """Initialize planner with its dependencies.

        Args:
            config: Application configuration
            scheduler: Issue prioritization and availability logic
            dependency_evaluator: Optional dependency checking
            review_workflow: Optional review decision logic
            rework_workflow: Optional rework decision logic
            tech_lead_workflow: Optional tech_lead decision logic
            label_manager: Label registry for prefix-aware queries.
        """
        self.config = config
        self.scheduler = scheduler
        self.dependency_evaluator = self._align_dependency_evaluator(dependency_evaluator)
        self.review_workflow = review_workflow
        self.retrospective_review_workflow = retrospective_review_workflow
        self.rework_workflow = rework_workflow
        self.tech_lead_workflow = tech_lead_workflow
        self.provider_resilience = provider_resilience
        self.provider_policy = ProviderAvailabilityPolicy(config, provider_resilience) if provider_resilience else None
        if label_manager is None:
            from .label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager
        self._tech_lead_reactions = TechLeadReactionPolicy(
            config=config,
            labels=label_manager,
            dependency_evaluator=self.dependency_evaluator,
            clock=clock,
        )
        # On-change human logs for the two coarse control decisions the planner
        # makes each tick — the per-issue queue decision, and the tech_lead
        # launch decision. Both write INFO lines keyed ``issue=<n>`` so a
        # deferred issue/session explains WHY in its per-issue trace instead of
        # going silent, and log only on change to avoid per-tick spam.
        self._queue_decision_log = QueueDecisionLog(logger)
        self._tech_lead_launch_log = TechLeadLaunchLog(logger)

    def _align_dependency_evaluator(
        self,
        dependency_evaluator: Optional[DependencyEvaluator],
    ) -> Optional[DependencyEvaluator]:
        """Read scheduler dependency gating configuration and fail on drift."""
        scheduler_evaluator = self.scheduler.dependency_evaluator
        if dependency_evaluator is None:
            return scheduler_evaluator
        if scheduler_evaluator is None:
            raise ValueError("Scheduler dependency evaluator is required when Planner dependency evaluator is provided")
        if scheduler_evaluator is not dependency_evaluator:
            raise ValueError("Planner and Scheduler dependency evaluators must be the same instance")
        return dependency_evaluator

    def plan(self, snapshot: OrchestratorSnapshot) -> Plan:
        """Create a plan for the current state.

        This is the main entry point. Given a snapshot of current state,
        determine what actions should be taken.

        Args:
            snapshot: Immutable view of current orchestrator state

        Returns:
            Plan with actions to execute and explanations for skipped items
        """
        actions: list[Action] = []
        skipped: list[SkippedItem] = []
        reaction = self._tech_lead_reactions.assess(snapshot)
        # Reactive tech_lead is one ATOMIC decision (#6780) owned by
        # reactive_tech_lead_planning: storm escalation vs individual
        # investigations, computed once so suppression is bound to actual
        # cohort persistence. Computing it here (before the paused
        # early-return) also runs the health-review gate exactly once, so its
        # TECH_LEAD_SKIPPED still fires while paused.
        reactive = plan_reactive_tech_lead(
            snapshot, reaction, self.config, workflow=self.tech_lead_workflow
        )

        # Check if paused
        if snapshot.paused:
            logger.debug("Planner: orchestrator is paused, returning empty plan")
            # Returning actions here would be dead code: apply_plan refuses to
            # apply anything while paused. The discovered facts a paused tick
            # cannot act on are instead RETAINED by clear_discovered_facts, so
            # a storm cohort survives the pause (#6780). The health-review
            # gate already ran above for its TECH_LEAD_SKIPPED emission (#6763).
            # A queued tech_lead session (e.g. a health review) is deferred by the
            # pause and never reaches the launch path, so log WHY here on-change —
            # else it sits queued with no explanation in its per-issue trace.
            self._tech_lead_launch_log.defer_all(
                snapshot.pending_tech_lead, "orchestrator_paused"
            )
            self._tech_lead_launch_log.retain(snapshot.pending_tech_lead)
            return Plan.empty()

        plan_context = PlanContext(issue_labels_by_number={
            issue.number: tuple(issue.labels) for issue in snapshot.issues
        })

        # === PHASE 1: Queue population actions (don't consume capacity) ===

        # 1a. Clean up stale in-progress labels (no session running)
        stale_cleanup_actions = self._plan_stale_cleanup(snapshot)
        actions.extend(stale_cleanup_actions)

        # 1a-2. Clean up stale claims (io:claimed but claim expired)
        stale_claim_actions = self._plan_stale_claim_cleanup(snapshot)
        actions.extend(stale_claim_actions)

        # 1a-2b. Apply provider resilience labels (provider unavailable/available)
        provider_label_actions = self._plan_provider_resilience_labels(snapshot, plan_context)
        actions.extend(provider_label_actions)

        # 1b. Queue discovered reviews from session completions/scans
        queue_actions = self._plan_discovered_reviews(snapshot)
        actions.extend(queue_actions)

        # 1b2. Queue retrospective reviews from trigger labels/UI requests
        retrospective_queue_actions = self._plan_discovered_retrospective_reviews(snapshot)
        actions.extend(retrospective_queue_actions)

        # 1c. Queue discovered reworks from scans
        rework_queue_actions = self._plan_discovered_reworks(snapshot)
        actions.extend(rework_queue_actions)

        # 1d. Handle escalations (PRs exceeding max rework cycles)
        escalation_actions = self._plan_discovered_escalations(snapshot)
        actions.extend(escalation_actions)

        # 1d1. Escalate stuck-sweep-exhausted issues to needs-human (#6824 R1):
        # the stuck-sweep owner defines the authoritative label-only escalation
        # (re-emitted, idempotent, until acknowledged); applied here through the
        # Applier, not a direct GitHub call from observation.
        actions.extend(build_stuck_sweep_escalation_actions(
            snapshot.stuck_sweep_escalations, self._lm.needs_human))

        # 1d2. Handle post-publish escalations (CI checks stuck > timeout,
        # or branch protection blocking merge despite checks passing).
        # Distinct from rework-cycle exhaustion: an *approved* PR being
        # handed to a human because no further automated retries help.
        post_publish_escalation_actions = self._plan_awaiting_merge_escalations(snapshot)
        actions.extend(post_publish_escalation_actions)

        # 1d3. Enqueue approved PRs into the merge queue (when enabled). The
        # merge queue coordinator already decided eligibility during discovery;
        # the planner just turns each fact into the protected enqueue action.
        merge_queue_actions = self._plan_merge_queue_enqueues(snapshot)
        actions.extend(merge_queue_actions)

        # 1e+1f2. Reactive tech_lead (tech-lead reaction, ADR-0031): the storm
        # cohort escalates to one unscheduled health-review anchor OR the
        # individual failure investigations queue — decided atomically above so
        # a suppressed cohort is never lost — plus any due periodic anchor.
        actions.extend(reactive.actions)

        # 1f. Create tech_lead issue if threshold met
        tech_lead_create_action = self._plan_tech_lead_issue_creation(snapshot)
        if tech_lead_create_action:
            actions.append(tech_lead_create_action)

        # 1f3. Everything the tech-lead DURABLE LEDGERS drive this tick:
        # approved gated proposals, terminal-op cleanup candidates, and finding
        # promotion/settlement. All three read facts the gatherer classified
        # read-only; the single owner turns them into actions (see
        # tech_lead_ledger_planning).
        actions.extend(
            plan_tech_lead_ledger_actions(self.config, snapshot.tech_lead_facts)
        )

        # 1g. Process cleanups for reviewed PRs
        cleanup_actions = self._plan_cleanups(snapshot)
        actions.extend(cleanup_actions)

        # 1h. Reconcile recovered awaiting-merge history entries
        history_reconciliation_actions = self._plan_awaiting_merge_reconciliations(snapshot)
        actions.extend(history_reconciliation_actions)

        launch_actions, launch_skipped = self._plan_session_launches(
            snapshot,
            plan_context,
            # Suppress individual investigation launches only when the cohort
            # was actually escalated this tick; on a deferred storm the fallback
            # investigations must be allowed to launch (#6780).
            suppressed_tech_lead_issue_numbers=reactive.suppressed_issue_numbers,
        )
        actions.extend(launch_actions)
        skipped.extend(launch_skipped)

        return Plan(actions=tuple(actions), skipped=tuple(skipped))

    def _active_tech_lead_count(self, snapshot: OrchestratorSnapshot) -> int:
        """Number of currently-active tech_lead (tech-lead) sessions.

        Delegates to the worker-budget owner so the "what is a tech_lead session"
        rule (ADR-0031: agent label == configured ``tech_lead_review_agent``) has
        a single definition shared with the E2E worker-slot gate.
        """
        return active_tech_lead_session_count(self.config, snapshot.active_sessions)

    def _worker_capacity(self, snapshot: OrchestratorSnapshot) -> int:
        """Remaining worker-budget capacity this tick — charged for reviews,
        reworks, validation retries and new issues (and for tech_lead only in
        the shared-budget default). A running first-class E2E workload
        (``snapshot.e2e_occupies_slot``) occupies one worker slot. Tech-lead
        reserved-slot accounting lives in ``tech_lead_slot_availability``
        (worker_budget), the single slot-accounting owner (#6892 review A2)."""
        worker_capacity = worker_slot_availability(
            self.config, snapshot.active_sessions
        ).remaining
        if snapshot.e2e_occupies_slot:
            worker_capacity -= 1
        return worker_capacity

    @staticmethod
    def _launch_count(actions: list[Action]) -> int:
        """Count capacity-consuming launches only — a provider-skip label action
        is not a launch. Every launch KIND is registered in
        ``_CAPACITY_CONSUMING_LAUNCH_TYPES`` (not a per-call concrete-class
        check), so a new launch kind can't silently escape the budget the way
        validation retries did (#6892 review F1/F2)."""
        return sum(
            1 for a in actions if a.action_type in _CAPACITY_CONSUMING_LAUNCH_TYPES
        )

    def _defer_pending_tech_lead(
        self, snapshot: OrchestratorSnapshot, slot: TechLeadSlotAvailability
    ) -> None:
        """Log (on-change) the owner-computed reason every queued tech_lead
        session was deferred this tick. ``slot.reason`` is set because the owner
        returns it iff ``available == 0``. Pruning departed items is the
        caller's job (``retain``)."""
        assert slot.reason is not None  # invariant of TechLeadSlotAvailability
        self._tech_lead_launch_log.defer_all(snapshot.pending_tech_lead, slot.reason)

    def _plan_session_launches(
        self,
        snapshot: OrchestratorSnapshot,
        plan_context: PlanContext,
        *,
        suppressed_tech_lead_issue_numbers: frozenset[int] = frozenset(),
    ) -> tuple[list[Action], list[SkippedItem]]:
        """Plan capacity-consuming session launches in priority order."""
        actions: list[Action] = []
        skipped: list[SkippedItem] = []
        capacity = self._worker_capacity(snapshot)
        # Worker-only active count for the review/rework worker gate (includes the
        # E2E charge, exactly as before). Tech-lead slot accounting does NOT reuse
        # this — its owner derives active-worker independently so E2E is never
        # misattributed as worker saturation (#6892 review F1/A2).
        worker_active_count = self.config.max_concurrent_sessions - capacity
        workflow_configured = bool(
            self.tech_lead_workflow and self.tech_lead_workflow.is_configured()
        )
        # The tech-lead slot budget + (when 0) its true reason, from the single
        # slot-accounting owner. Recomputed after higher-priority launches below.
        tech_lead_slot = tech_lead_slot_availability(
            self.config,
            snapshot.active_sessions,
            e2e_occupies_slot=snapshot.e2e_occupies_slot,
            launched_this_tick=0,
            workflow_configured=workflow_configured,
        )
        # Short-circuit only when NEITHER budget can launch anything.
        if capacity <= 0 and tech_lead_slot.available <= 0:
            logger.debug(
                "Planner: no capacity available (active=%d, max=%d)",
                snapshot.active_count,
                self.config.max_concurrent_sessions,
            )
            # A queued tech_lead session must not go silent: log the owner's
            # reason (nothing has launched yet this tick).
            self._defer_pending_tech_lead(snapshot, tech_lead_slot)
            self._tech_lead_launch_log.retain(snapshot.pending_tech_lead)
            return actions, skipped

        # PRIORITY ORDER: Reviews > Retrospective Reviews > Reworks >
        # Validation Retries > Tech Lead > New Issues.
        # This ensures completed work gets reviewed before starting new work.
        review_launch_count = 0
        retrospective_review_launch_count = 0
        rework_launch_count = 0
        validation_retry_launch_count = 0
        tech_lead_launch_count = 0

        # 2. Plan review launches (highest priority). The worker workflows gate
        # on the WORKER-only count (``worker_active_count``, owner: worker_budget),
        # NOT raw ``snapshot.active_count`` — else a reserved-tech-lead session steals
        # worker review/rework capacity (#6824 F5). Only actual launches consume
        # capacity — a provider-skip label action does not (#6892 review F2).
        if capacity > 0 and self.review_workflow:
            review_actions, review_skipped = self._plan_reviews(
                snapshot, capacity, worker_active_count, plan_context
            )
            actions.extend(review_actions)
            skipped.extend(review_skipped)
            review_launch_count = self._launch_count(review_actions)
            capacity -= review_launch_count

        # 2b. Plan retrospective review launches
        if capacity > 0 and self.retrospective_review_workflow:
            retrospective_actions, retrospective_skipped = self._plan_retrospective_reviews(
                snapshot,
                capacity,
                worker_active_count,
                plan_context,
            )
            actions.extend(retrospective_actions)
            skipped.extend(retrospective_skipped)
            retrospective_review_launch_count = self._launch_count(retrospective_actions)
            capacity -= retrospective_review_launch_count

        # 3. Plan rework launches
        if capacity > 0 and self.rework_workflow:
            rework_actions, rework_skipped = self._plan_reworks(
                snapshot, capacity, worker_active_count, plan_context
            )
            actions.extend(rework_actions)
            skipped.extend(rework_skipped)
            rework_launch_count = self._launch_count(rework_actions)
            capacity -= rework_launch_count

        # 4. Plan validation retry launches. These are continuations of
        # existing coding work and are not subject to max_issues_to_start.
        if capacity > 0:
            validation_retry_actions, validation_retry_skipped = self._plan_validation_retries(
                snapshot,
                capacity,
                plan_context,
            )
            actions.extend(validation_retry_actions)
            skipped.extend(validation_retry_skipped)
            validation_retry_launch_count = self._launch_count(validation_retry_actions)
            capacity -= validation_retry_launch_count

        # 5. Plan tech_lead launches. Recompute the slot now that higher-priority
        # launches this tick are known — the owner returns available>0 to launch,
        # or available==0 with the true deferral reason. By default tech_lead
        # draws from the shared worker budget; with tech_lead.max_concurrent set
        # it uses its own reserved additive slot and does NOT decrement worker
        # capacity.
        launched_this_tick = (
            review_launch_count
            + retrospective_review_launch_count
            + rework_launch_count
            + validation_retry_launch_count
        )
        tech_lead_slot = tech_lead_slot_availability(
            self.config,
            snapshot.active_sessions,
            e2e_occupies_slot=snapshot.e2e_occupies_slot,
            launched_this_tick=launched_this_tick,
            workflow_configured=workflow_configured,
        )
        if tech_lead_slot.available > 0:
            tech_lead_actions, tech_lead_skipped = self._plan_tech_lead(
                snapshot,
                tech_lead_slot.available,
                plan_context,
                reserved=self.config.tech_lead.max_concurrent is not None,
                suppressed_issue_numbers=suppressed_tech_lead_issue_numbers,
            )
            actions.extend(tech_lead_actions)
            skipped.extend(tech_lead_skipped)
            tech_lead_launch_count = self._launch_count(tech_lead_actions)
            if self.config.tech_lead.max_concurrent is None:
                capacity -= tech_lead_launch_count
        else:
            self._defer_pending_tech_lead(snapshot, tech_lead_slot)
        self._tech_lead_launch_log.retain(snapshot.pending_tech_lead)

        # 5b. Reserve one worker slot for a due first-class E2E run — after all
        # completion work above, before new issues below (see method docstring).
        capacity = self._reserve_e2e_worker_slot(snapshot, capacity)

        # 6. Plan issue launches with remaining capacity.
        #
        # Reviews/reworks/tech_lead get priority (they consumed capacity above),
        # but any leftover capacity goes to new issues. We never starve issue
        # launches just because review/rework/tech_lead actions were planned.
        if capacity > 0:
            pending_work_planned = (
                review_launch_count
                + retrospective_review_launch_count
                + rework_launch_count
                + validation_retry_launch_count
                + tech_lead_launch_count
            )
            if pending_work_planned:
                logger.info(
                    "Planner: pending work consumed %d slot(s) "
                    "(reviews=%d, retrospective_reviews=%d, reworks=%d, validation_retries=%d, tech_lead=%d), "
                    "%d slot(s) remain for issues",
                    pending_work_planned, review_launch_count,
                    retrospective_review_launch_count,
                    rework_launch_count, validation_retry_launch_count,
                    tech_lead_launch_count, capacity,
                )
            issue_actions, issue_skipped, _ = self._plan_issues(
                snapshot, capacity, worker_active_count
            )
            actions.extend(issue_actions)
            skipped.extend(issue_skipped)

        return actions, skipped

    def _reserve_e2e_worker_slot(
        self, snapshot: OrchestratorSnapshot, capacity: int
    ) -> int:
        """Hold back one worker slot for a due first-class E2E run.

        ``snapshot.e2e_due`` is only ever set when ``e2e.occupies_session_slot``
        is on (byte-for-byte off by default), and never at the same time as
        ``e2e_occupies_slot`` (a run is not "due" while it is already active).
        The caller invokes this AFTER all in-flight completion work (reviews/
        retrospectives/reworks/validation-retries/tech_lead) has consumed its
        share, but BEFORE new issues — so a due suite claims a slot ahead of new
        issues yet never preempts completion work: on a saturated board the
        completion work simply leaves nothing to reserve and E2E waits. The
        reservation only holds the slot back from new-issue launches; the run
        itself starts post-tick in ``maybe_trigger_e2e``, gated on the freed
        worker slot.
        """
        if snapshot.e2e_due and capacity > 0:
            return capacity - 1
        return capacity

    def _plan_discovered_reviews(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan queue actions for discovered reviews from session completions.

        Returns:
            List of QueueReviewAction for reviews not already queued
        """
        actions: list[Action] = []

        if not snapshot.discovered_reviews:
            return actions

        # Get already-queued PR numbers
        queued_pr_numbers = {r.pr_number for r in snapshot.pending_reviews}
        issue_labels_by_number = {issue.number: tuple(issue.labels) for issue in snapshot.issues}

        for review in snapshot.discovered_reviews:
            if review.pr_number not in queued_pr_numbers:
                ik = review.issue_key or str(review.issue_number)
                # Add pr-pending label to prevent issue re-pickup while awaiting merge
                actions.append(AddLabelAction(
                    issue_number=review.issue_number,
                    label=self._lm.pr_pending,
                    reason=f"session completed with PR #{review.pr_number} - awaiting merge",
                    expected=build_expected_for_mutation(),
                    issue_key=ik,
                ))
                # Skip review for dry-run PRs (fake PR numbers 90000-99999)
                is_dry_run_pr = 90000 <= review.pr_number <= 99999
                # Only queue review if code review agent is configured AND not dry-run
                if self.config.code_review_agent and not is_dry_run_pr:
                    actions.append(QueueReviewAction(
                        issue_number=review.issue_number,
                        pr_number=review.pr_number,
                        pr_url=review.pr_url,
                        branch_name=review.branch_name,
                        code_review_label=self.config.code_review_label or "",
                        agent_label=review.agent_label,
                        reason=f"session completed with PR #{review.pr_number}",
                        expected=build_expected_for_mutation(),
                        issue_key=ik,
                        issue_labels=issue_labels_by_number.get(review.issue_number, ()),
                    ))
                    logger.debug("Planner: queuing review for PR #%d", review.pr_number)
                else:
                    logger.debug("Planner: no code_review_agent - skipping review queue for PR #%d", review.pr_number)
            else:
                logger.debug("Planner: PR #%d already queued, skipping", review.pr_number)

        return actions

    def _plan_discovered_retrospective_reviews(
        self,
        snapshot: OrchestratorSnapshot,
    ) -> list[Action]:
        """Plan queue actions for trigger-labeled retrospective reviews."""
        actions: list[Action] = []
        if not snapshot.discovered_retrospective_reviews:
            return actions

        queued_issue_numbers = {
            review.issue_number
            for review in snapshot.pending_retrospective_reviews
        }
        active_retrospective_issue_numbers = active_retrospective_review_issue_numbers(
            snapshot.active_sessions
        )

        for review in snapshot.discovered_retrospective_reviews:
            if review.issue_number in queued_issue_numbers:
                continue
            if review.issue_number in active_retrospective_issue_numbers:
                continue
            actions.append(QueueRetrospectiveReviewAction(
                issue_number=review.issue_number,
                issue_title=review.issue_title,
                agent_label=review.agent_label,
                trigger_label=review.trigger_label,
                issue_key=review.issue_key or str(review.issue_number),
                prior_pr_number=review.prior_pr_number,
                prior_pr_url=review.prior_pr_url,
                issue_labels=review.issue_labels,
                reason="retrospective review trigger label discovered",
            ))
            logger.debug(
                "Planner: queuing retrospective review for issue #%d",
                review.issue_number,
            )

        return actions

    def _plan_awaiting_merge_reconciliations(
        self,
        snapshot: OrchestratorSnapshot,
    ) -> list[Action]:
        """Plan history status transitions discovered by awaiting-merge scans."""
        actions: list[Action] = []

        # Drifts already strip pr-pending via SyncLabelsAction below; track those
        # issues so the per-reconciliation removal doesn't double up.
        drift_issue_numbers = {
            drift.issue_number
            for drift in snapshot.discovered_awaiting_merge_drifts
        }

        for reconciliation in snapshot.discovered_awaiting_merge_reconciliations:
            issue_key = reconciliation.issue_key or str(reconciliation.issue_number)
            # Drift reconciliations ADD blocked:pr-closed (handled by the
            # SyncLabelsAction below) rather than shedding, so there is no label
            # cleanup to order ahead of the history transition — finalize the
            # history entry on its own.
            if reconciliation.issue_number in drift_issue_numbers:
                actions.append(ReconcileHistoryEntryAction(
                    issue_number=reconciliation.issue_number,
                    pr_number=reconciliation.pr_number,
                    pr_url=reconciliation.pr_url,
                    status=reconciliation.status,
                    source=reconciliation.source,
                    issue_key=issue_key,
                    reason=reconciliation.status_reason,
                ))
                continue
            # Terminal recovery: the issue's work has landed (PR merged/closed
            # or parent issue closed). One owner command sheds every stale
            # transient workflow label (pr-pending, publish-failed,
            # publish-fail-count-N, blocking) then finalizes awaiting-merge
            # history; the applier picks the set from live labels and gates the
            # history transition on the shed (and close) succeeding, so a
            # transient failure leaves the entry reconcilable, not stranded.
            actions.append(RecoverTerminalIssueAction(
                issue_number=reconciliation.issue_number,
                pr_number=reconciliation.pr_number,
                pr_url=reconciliation.pr_url,
                status=reconciliation.status,
                source=reconciliation.source,
                status_reason=reconciliation.status_reason,
                issue_key=issue_key,
                reason=f"awaiting-merge terminal: {reconciliation.status}",
                # Close-on-merge fallback (close_on_merge module, porchpin
                # #81): merged PR + still-open issue; advisory — the applier
                # revalidates live evidence. Never on closed status (drift's job).
                close_issue=reconciliation.status == "merged" and reconciliation.issue_open,
                merged_at=reconciliation.merged_at or "",
                # Carry the reconciliation pause guard the old terminal-cleanup
                # RemoveLabelAction used to carry: an issue paused for
                # reconciliation (io:needs-reconcile) must not have its labels
                # shed or its awaiting-merge history finalized behind the
                # fail-closed drift handling. The applier enforces this at the
                # owner-command boundary before any write (#6431 F1).
                expected=build_expected_for_mutation(),
            ))

        for drift in snapshot.discovered_awaiting_merge_drifts:
            actions.append(SyncLabelsAction(
                issue_number=drift.issue_number,
                add_labels=(self._lm.blocked_pr_closed,),
                remove_labels=(self._lm.pr_pending,),
                issue_key=drift.issue_key or str(drift.issue_number),
                reason=drift.status_reason,
                expected=build_expected_for_mutation(
                    required={self._lm.pr_pending},
                ),
            ))

        return actions

    def _record_provider_skip(
        self,
        issue_number: int,
        item_type: str,
        item_number: int,
        provider: str,
        actions: list[Action],
        skipped: list[SkippedItem],
        plan_context: PlanContext,
    ) -> None:
        skipped.append(SkippedItem(
            item_type=item_type,
            number=item_number,
            reason=f"provider unavailable: {provider}",
        ))
        logger.info(issue_log(issue_number, "Skipped: reason=provider_unavailable provider=%s"), provider)
        if not self.provider_policy:
            return
        issue_labels = plan_context.issue_labels(issue_number)
        planned_labels = plan_context.planned_adds(issue_number)
        if self.provider_policy.should_add_blocked_label(issue_labels, planned_labels):
            actions.append(AddLabelAction(
                issue_number=issue_number,
                label=self.provider_policy.blocked_label(),
                reason=f"provider unavailable: {provider}",
                expected=build_expected_for_mutation(),
            ))
            plan_context.record_add(issue_number, self.provider_policy.blocked_label())

    def _plan_provider_resilience_labels(
        self,
        snapshot: OrchestratorSnapshot,
        plan_context: PlanContext,
    ) -> list[Action]:
        if not self.provider_policy:
            return []
        actions: list[Action] = []
        label = self.provider_policy.blocked_label()
        providers_by_issue = self.provider_policy.providers_for_snapshot(snapshot)
        for issue in snapshot.issues:
            providers = providers_by_issue.get(issue.number, set())
            if not providers:
                continue
            any_open = self.provider_policy.any_open(providers)
            issue_labels = plan_context.issue_labels(issue.number)
            planned_labels = plan_context.planned_adds(issue.number)
            if any_open and self.provider_policy.should_add_blocked_label(issue_labels, planned_labels):
                actions.append(AddLabelAction(
                    issue_number=issue.number,
                    label=label,
                    reason=f"provider unavailable: {', '.join(sorted(providers))}",
                    expected=build_expected_for_mutation(),
                    issue_key=issue.key.stable_id(),
                ))
                plan_context.record_add(issue.number, label)
            if (
                not any_open
                and self.provider_policy.should_remove_blocked_label(issue_labels, planned_labels)
                and plan_context.should_remove_label(issue.number, label)
            ):
                actions.append(RemoveLabelAction(
                    issue_number=issue.number,
                    label=label,
                    reason=f"provider available: {', '.join(sorted(providers))}",
                    expected=build_expected_for_mutation(),
                    issue_key=issue.key.stable_id(),
                ))
                plan_context.record_remove(issue.number, label)
        return actions

    def _plan_tech_lead_issue_creation(self, snapshot: OrchestratorSnapshot) -> Optional[CreateTechLeadIssueAction]:
        """Plan tech_lead issue creation if threshold is met."""
        if not snapshot.tech_lead_facts:
            return None

        facts = snapshot.tech_lead_facts
        if not self._should_create_tech_lead_issue(facts):
            return None

        title, body = self._build_tech_lead_issue_content(facts)
        labels = batch_review_issue_labels(self.config, source_labels=facts.source_labels)
        # Milestone travels as INTENT; the applier resolves an explicit name
        # at the create-issue execution boundary (#6769 finding 4).
        milestone = tech_lead_issue_milestone_intent(self.config, facts.source_milestones)

        logger.info("Planner: creating tech_lead issue for %d PRs (labels=%s, milestone=%s)", facts.pr_count, labels, milestone)
        return CreateTechLeadIssueAction(
            title=title, body=body, labels=labels, pr_count=facts.pr_count,
            milestone=milestone, reason=f"threshold met: {facts.pr_count} >= {facts.threshold}",
            # This IS the anchor: no prior issue to reconcile against (#6957 F6).
            origin=TechLeadCreationOrigin.authors_anchor(),
        )

    def _should_create_tech_lead_issue(self, facts: "TechLeadFacts") -> bool:
        """Check if tech_lead issue should be created."""
        if facts.threshold <= 0:
            # Batch trigger disabled; facts may exist for the health review
            # alone (ADR-0031 §4), which plans its own anchor creation.
            return False
        if facts.pr_count < facts.threshold:
            logger.debug("Planner: tech_lead threshold not met (%d/%d)", facts.pr_count, facts.threshold)
            return False
        if facts.existing_tech_lead_issue:
            logger.debug("Planner: tech_lead issue #%d already exists", facts.existing_tech_lead_issue)
            return False
        return True

    def _build_tech_lead_issue_content(self, facts: "TechLeadFacts") -> tuple[str, str]:
        """Build title and body for tech_lead issue."""
        pr_list = "\n".join(f"- PR #{pr[0]}: {pr[1]}" for pr in facts.prs)
        body = f"""## Tech Lead Batch Review Triggered

{facts.pr_count} PRs have passed code review and are ready for tech_lead review:

{pr_list}

Review these PRs for patterns, architectural concerns, and process improvements.
Flip labels from `{facts.watch_label}` to `{self.config.tech_lead_reviewed_label}` after review.
"""
        title = apply_tech_lead_priority_prefix(
            self.config, f"{TECH_LEAD_DISPLAY_NAME} Batch Review: {facts.pr_count} PRs pending"
        )
        return title, body

    def _plan_discovered_reworks(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan queue actions for discovered reworks from scans.

        Returns:
            List of QueueReworkAction for reworks not already queued
        """
        actions: list[Action] = []

        if not snapshot.discovered_reworks:
            return actions

        queued_issue_ids = {
            r.resolve_issue_number()
            for r in snapshot.pending_reworks
            if r.resolve_issue_number() is not None
        }

        for rework in snapshot.discovered_reworks:
            if rework.issue_number not in queued_issue_ids:
                if (
                    rework.source == POST_PUBLISH_VALIDATION_SOURCE
                    and rework.pr_number > 0
                ):
                    actions.append(RemoveLabelAction(
                        issue_number=rework.pr_number, label=self._lm.code_reviewed,
                        reason="post-publish validation failed; clearing code-reviewed",
                    ))
                    if rework.clear_needs_human:
                        actions.append(RemoveLabelAction(
                            issue_number=rework.pr_number, label=self._lm.needs_human,
                            reason="post-publish state now reworkable; clearing needs-human",
                        ))
                    actions.append(AddLabelAction(
                        issue_number=rework.pr_number, label=self._lm.needs_rework,
                        reason="post-publish validation failed; marking PR for rework",
                    ))
                    if rework.feedback and not rework.feedback_comment_already_posted:
                        actions.append(AddCommentAction(
                            number=rework.pr_number,
                            is_pr=True,
                            comment=build_post_publish_validation_comment(rework.feedback),
                            reason="post-publish validation failed after review approval",
                        ))
                # Remove pr-pending so scheduler considers issue available again
                actions.append(RemoveLabelAction(
                    issue_number=rework.issue_number,
                    label=self._lm.pr_pending,
                    reason=f"rework needed for PR #{rework.pr_number} (cycle {rework.rework_cycle})",
                ))
                actions.append(QueueReworkAction(
                    issue_number=rework.issue_number,
                    pr_number=rework.pr_number,
                    pr_url="",  # Not tracked in DiscoveredRework
                    branch_name=rework.branch_name,
                    rework_cycle=rework.rework_cycle,
                    source=rework.source,
                    feedback=rework.feedback,
                    reason=f"scan found PR needing rework (cycle {rework.rework_cycle})",
                ))
                logger.debug("Planner: queuing rework for issue #%d (cycle %d)",
                            rework.issue_number, rework.rework_cycle)
            else:
                logger.debug("Planner: issue #%d already queued for rework, skipping",
                            rework.issue_number)

        return actions

    def _plan_discovered_escalations(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan escalation actions for PRs exceeding max rework cycles.

        Returns:
            List of EscalateToHumanAction for escalations
        """
        actions: list[Action] = []

        if not snapshot.discovered_escalations:
            return actions

        # Build issue-number → Issue lookup for stable key resolution
        issues_by_number = {i.number: i for i in snapshot.issues}

        for escalation in snapshot.discovered_escalations:
            # Resolve stable issue_key; fall back to str(issue_number)
            issue = issues_by_number.get(escalation.issue_number)
            issue_key = issue.key.stable_id() if issue else str(escalation.issue_number)

            # rework_cycle is the "next cycle" from the scanner (e.g., 3 means
            # label rework-cycle-2 was found).  Pass it directly — ActionApplier
            # subtracts 1 to derive the completed-cycle count for display.
            # This matches the normal-flow escalation in _plan_rework_launches.
            actions.append(EscalateToHumanAction(
                issue_number=escalation.issue_number,
                pr_number=escalation.pr_number,
                escalation_reason="max rework cycles exceeded",
                rework_cycles=escalation.rework_cycle,
                needs_human_label=self._lm.needs_human,
                needs_rework_label=self._lm.needs_rework,
                max_rework_cycles=self.config.max_rework_cycles,
                issue_key=issue_key,
                reason=f"PR #{escalation.pr_number} exceeded max rework cycles ({escalation.rework_cycle - 1})",
                expected=build_expected_for_mutation(),
            ))
            logger.info("Planner: escalating PR #%d after %d rework cycles",
                       escalation.pr_number, escalation.rework_cycle - 1)

        return actions

    def _plan_awaiting_merge_escalations(
        self, snapshot: OrchestratorSnapshot
    ) -> list[Action]:
        """Plan escalations for approved PRs stuck post-publish.

        Distinct from `_plan_discovered_escalations` (rework-cycle
        exhaustion): an approved PR is being handed to a human because
        either CI checks stalled past the configured timeout, or branch
        protection blocks merge in a way code rework can't unstick.

        Returns:
            List of EscalateToHumanAction with comment_override set to
            a cause-specific markdown body.
        """
        actions: list[Action] = []
        if not snapshot.discovered_awaiting_merge_escalations:
            return actions

        issues_by_number = {i.number: i for i in snapshot.issues}

        for escalation in snapshot.discovered_awaiting_merge_escalations:
            issue = issues_by_number.get(escalation.issue_number)
            issue_key = issue.key.stable_id() if issue else escalation.issue_key
            comment = build_post_publish_escalation_comment(
                kind=escalation.kind, reason=escalation.reason,
                needs_human_label=self._lm.needs_human,
            )
            actions.append(EscalateToHumanAction(
                issue_number=escalation.issue_number,
                pr_number=escalation.pr_number,
                escalation_reason=f"post-publish: {escalation.kind}",
                rework_cycles=escalation.rework_cycle,
                needs_human_label=self._lm.needs_human,
                needs_rework_label=self._lm.needs_rework,
                max_rework_cycles=self.config.max_rework_cycles,
                issue_key=issue_key,
                reason=(
                    f"PR #{escalation.pr_number} escalated post-publish "
                    f"({escalation.kind})"
                ),
                comment_override=comment,
                expected=build_expected_for_mutation(),
            ))
            logger.info(
                "Planner: escalating PR #%d post-publish: kind=%s",
                escalation.pr_number, escalation.kind,
            )

        return actions

    def _plan_merge_queue_enqueues(
        self, snapshot: OrchestratorSnapshot
    ) -> list[Action]:
        """Plan enqueue actions for PRs the merge queue coordinator approved.

        The coordinator owns eligibility (gate passed, not already queued,
        mergeable-or-behind); the planner only maps each discovered fact to an
        ``EnqueueToMergeQueueAction`` for the applier to execute.
        """
        actions: list[Action] = []
        for enqueue in snapshot.discovered_merge_queue_enqueues:
            actions.append(EnqueueToMergeQueueAction(
                issue_number=enqueue.issue_number,
                pr_number=enqueue.pr_number,
                pr_url=enqueue.pr_url,
                issue_key=enqueue.issue_key or str(enqueue.issue_number),
                reason=f"PR #{enqueue.pr_number} eligible for merge queue",
            ))
            logger.info(
                "Planner: enqueuing PR #%d to merge queue",
                enqueue.pr_number,
            )
        return actions

    def _plan_cleanups(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan cleanup actions for completed sessions.

        Handles two types of cleanups:
        1. Deferred cleanups - wait for review label before cleaning
        2. Immediate cleanups - clean up right away (no review workflow)

        Returns:
            List of CleanupSessionAction for cleanups ready to process
        """
        actions: list[Action] = []

        if not snapshot.cleanup_facts:
            return actions

        facts = snapshot.cleanup_facts

        # 1. Deferred cleanups - check if PR has been reviewed
        for cleanup in facts.pending_cleanups:
            # cleanup is a tuple of (issue_number, pr_number, terminal_id, worktree_path)
            issue_number, pr_number, terminal_id, worktree_path = cleanup
            if pr_number in facts.reviewed_pr_numbers:
                actions.append(CleanupSessionAction(
                    issue_number=issue_number,
                    pr_number=pr_number,
                    terminal_id=terminal_id,
                    worktree_path=worktree_path,
                    close_tabs=facts.close_tabs,
                    remove_worktrees=facts.remove_worktrees,
                    reason=f"PR #{pr_number} has been reviewed",
                ))
                logger.info("Planner: deferred cleanup for issue #%d (PR #%d reviewed)",
                           issue_number, pr_number)

        # 2. Immediate cleanups - ready to execute now, EXCEPT run assets that
        # pending/active tech_lead work still references (#6771, #6780):
        # cleaning those up before the investigation or health review
        # launches deletes the artifact hints it was queued to read. The hold
        # set comes from the tech-lead-problem-artifact owner in the fact
        # gatherer, which is also what retains these entries across the
        # end-of-tick fact clear; they are re-planned once the hold releases.
        for cleanup in facts.immediate_cleanups:
            if cleanup.issue_number in facts.held_issue_numbers:
                logger.info(
                    "Planner: holding cleanup for issue #%d — pending or active "
                    "tech_lead work still references its run assets",
                    cleanup.issue_number,
                )
                continue
            actions.append(CleanupSessionAction(
                issue_number=cleanup.issue_number,
                pr_number=0,  # No PR for immediate cleanups
                terminal_id=cleanup.terminal_id,
                worktree_path=cleanup.worktree_path,
                close_tabs=facts.close_tabs,
                # A disposable tech-lead-investigation scratch worktree is always
                # removed, even when the config keeps worktrees (#6823).
                remove_worktrees=facts.remove_worktrees or cleanup.scratch_worktree,
                # Carry disposable identity so the applier force-removes ONLY the
                # scratch worktree (leftover artifacts must not leak it) (#6824 F8).
                disposable_worktree=cleanup.scratch_worktree,
                reason=f"session {cleanup.reason}",
            ))
            logger.info("Planner: immediate cleanup for issue #%d (%s)",
                       cleanup.issue_number, cleanup.reason)

        return actions

    def _plan_stale_cleanup(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan cleanup actions for issues with stale in-progress labels.

        When an issue has the in-progress label but no active session exists,
        the label is stale and should be removed. This allows the issue to be
        retried or processed normally.

        Returns:
            List of RemoveLabelAction for stale in-progress labels
        """
        actions: list[Action] = []

        if not snapshot.stale_in_progress_issues:
            return actions

        for issue in snapshot.stale_in_progress_issues:
            actions.append(RemoveLabelAction(
                issue_number=issue.number,
                label=self._lm.in_progress,
                reason="stale - no running session",
                expected=build_expected_for_mutation(),
                issue_key=issue.key.stable_id(),
            ))
            logger.info("Planner: removing stale in-progress label from issue #%d",
                       issue.number)

        return actions

    def _plan_stale_claim_cleanup(self, snapshot: OrchestratorSnapshot) -> list[Action]:
        """Plan cleanup actions for issues with stale/expired claims.

        When an issue has the io:claimed label but the claim has expired
        (e.g., the orchestrator that held it crashed without releasing),
        we need to clean up:
        1. Remove the io:claimed label
        2. Add blocked:stale-claim label to flag for investigation

        The stale_claim_issues list is populated by the Orchestrator/Observer
        phase, which checks claims via ClaimManager.get_current_claim().

        Returns:
            List of label actions for stale claim cleanup
        """
        actions: list[Action] = []

        if not snapshot.stale_claim_issues:
            return actions

        for issue in snapshot.stale_claim_issues:
            # Remove the io:claimed label
            actions.append(RemoveLabelAction(
                issue_number=issue.number,
                label=self._lm.io_claimed,
                reason="stale claim expired",
                expected=build_expected_for_mutation(),
                issue_key=issue.key.stable_id(),
            ))
            # Add blocked:stale-claim label for visibility
            actions.append(AddLabelAction(
                issue_number=issue.number,
                label=self._lm.blocked_stale_claim,
                reason="stale claim detected - orchestrator may have crashed",
                expected=build_expected_for_mutation(),
                issue_key=issue.key.stable_id(),
            ))
            logger.info("Planner: cleaning up stale claim on issue #%d",
                       issue.number)

        return actions

    def _plan_issues(  # noqa: C901, PLR0912 — multi-phase issue scheduling
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
        worker_active_count: int,
    ) -> tuple[list[Action], list[SkippedItem], int]:
        """Plan which issues to launch.

        ``worker_active_count`` is the active-session count charged against the
        worker budget for the scheduler's own slot gate. It equals
        ``snapshot.active_count`` in the shared-budget default; with a reserved
        tech_lead budget it excludes active tech_lead sessions so they do not steal
        worker issue slots (the additive-budget invariant).

        Returns:
            Tuple of (actions, skipped_items, capacity_used)
        """
        actions: list[Action] = []
        skipped: list[SkippedItem] = []

        # Check max_issues_to_start limit
        if snapshot.max_issues_to_start is not None:
            remaining = snapshot.max_issues_to_start - snapshot.issues_started_count
            if remaining <= 0:
                logger.debug("Planner: max_issues_to_start reached (%d)",
                            snapshot.max_issues_to_start)
                return actions, skipped, 0
            capacity = min(capacity, remaining)

        # Get scheduler decisions with explicit availability reasons.
        scheduler_decisions = self.scheduler.evaluate_issues(
            list(snapshot.issues),
            check_dependencies=self.dependency_evaluator is not None,
            active_sessions=list(snapshot.active_sessions),
        )
        available = [d.issue for d in scheduler_decisions if d.available]
        dependency_blocked = [
            (d.issue, d.detail or "dependency blocked")
            for d in scheduler_decisions
            if d.is_dependency_blocked
        ]
        decision_reason_by_issue = {d.issue.number: d.reason for d in scheduler_decisions}
        decision_detail_by_issue = {
            d.issue.number: d.detail for d in scheduler_decisions if d.detail
        }
        scheduler_filtered = sum(
            1 for decision in scheduler_decisions
            if not decision.available and not decision.is_dependency_blocked
        )

        # Record dependency-blocked items and add cross-milestone labels
        for issue, reason in dependency_blocked:
            logger.info(issue_log(issue.number, "Skipped: reason=blocked_by_dependency detail=%s"), reason)
            skipped.append(SkippedItem(
                item_type="issue",
                number=issue.number,
                reason=f"dependency: {reason}",
            ))
            # Add cross-milestone label if this is a milestone scope violation
            if "cross-milestone" in reason.lower():
                actions.append(AddLabelAction(
                    issue_number=issue.number,
                    label=self._lm.blocked_cross_milestone,
                    reason=f"dependency violates milestone scope: {reason}",
                    expected=build_expected_for_mutation(),
                    issue_key=issue.key.stable_id(),
                ))

        # Filter out providers with open circuit
        if self.provider_policy:
            filtered: list[Issue] = []
            for issue in available:
                provider = self.provider_policy.provider_for_issue(issue)
                if provider and self.provider_policy.is_open(provider):
                    skipped.append(SkippedItem(
                        item_type="issue",
                        number=issue.number,
                        reason=f"provider unavailable: {provider}",
                    ))
                    logger.info(issue_log(issue.number, "Skipped: reason=provider_unavailable provider=%s"), provider)
                    continue
                filtered.append(issue)
            available = filtered

        # Filter out issues already being worked on, just completed, or failed this cycle.
        # Include both discovered (new this tick) and pending (queued for launch) reviews/reworks
        # to prevent launching a code session for an issue that already has review/rework work.
        issues_with_reviews = {r.issue_number for r in snapshot.discovered_reviews}
        issues_with_reviews.update(r.issue_number for r in snapshot.pending_reviews)
        issues_with_retrospective_reviews = {
            r.issue_number for r in snapshot.discovered_retrospective_reviews
        }
        issues_with_retrospective_reviews.update(
            r.issue_number for r in snapshot.pending_retrospective_reviews
        )
        issues_with_reworks = {r.issue_number for r in snapshot.discovered_reworks}
        issues_with_reworks.update(
            n for r in snapshot.pending_reworks if (n := r.resolve_issue_number()) is not None
        )
        excluded_issues = (
            snapshot.active_issue_numbers |
            issues_with_reviews |
            issues_with_retrospective_reviews |
            issues_with_reworks |
            snapshot.failed_this_cycle |  # Skip issues that failed until cache refresh
            snapshot.session_history_issue_numbers  # Skip issues with completed sessions
        )
        not_active = [
            issue for issue in available
            if issue.number not in excluded_issues
        ]

        # Log per-issue exclusion reasons for diagnostics.
        skip_reason_by_issue: dict[int, str] = {}
        for issue in available:
            if issue.number in snapshot.active_issue_numbers:
                skipped.append(SkippedItem(item_type="issue", number=issue.number, reason="active session running"))
                logger.info(issue_log(issue.number, "Skipped: reason=active_session"))
                skip_reason_by_issue[issue.number] = "active_session"
            elif issue.number in issues_with_reviews:
                skipped.append(SkippedItem(item_type="issue", number=issue.number, reason="pending review"))
                logger.info(issue_log(issue.number, "Skipped: reason=pending_review"))
                skip_reason_by_issue[issue.number] = "pending_review"
            elif issue.number in issues_with_retrospective_reviews:
                skipped.append(SkippedItem(item_type="issue", number=issue.number, reason="pending retrospective review"))
                logger.info(issue_log(issue.number, "Skipped: reason=pending_retrospective_review"))
                skip_reason_by_issue[issue.number] = "pending_retrospective_review"
            elif issue.number in issues_with_reworks:
                skipped.append(SkippedItem(item_type="issue", number=issue.number, reason="pending rework"))
                logger.info(issue_log(issue.number, "Skipped: reason=pending_rework"))
                skip_reason_by_issue[issue.number] = "pending_rework"
            elif issue.number in snapshot.failed_this_cycle:
                skipped.append(SkippedItem(item_type="issue", number=issue.number, reason="failed this cycle - waiting for cache refresh"))
                logger.info(issue_log(issue.number, "Skipped: reason=failed_this_cycle"))
                skip_reason_by_issue[issue.number] = "failed_this_cycle"
            elif issue.number in snapshot.session_history_issue_numbers:
                skipped.append(SkippedItem(item_type="issue", number=issue.number, reason="session completed this run"))
                logger.info(issue_log(issue.number, "Skipped: reason=session_history"))
                skip_reason_by_issue[issue.number] = "session_history"

        # Pick next batch based on priority
        to_launch = self.scheduler.pick_next_batch(
            available=not_active,
            current_count=worker_active_count,
            priority_overrides=list(snapshot.priority_queue),
        )

        # Create launch actions
        for issue in to_launch[:capacity]:
            priority_reason = self._get_priority_reason(issue)
            logger.info(
                issue_log(issue.number, "Selected for session: type=code priority=%s slots_available=%d"),
                priority_reason, capacity
            )
            actions.append(LaunchSessionAction(
                session_type=SessionType.ISSUE,
                number=issue.number,
                command="",  # Orchestrator will fill in
                working_dir="",  # Orchestrator will fill in
                reason=f"scheduled: priority={priority_reason}",
            ))

        # Pipeline funnel summary for diagnostics
        snapshot_numbers = sorted(i.number for i in snapshot.issues)
        available_numbers = sorted(i.number for i in available)
        eligible_numbers = sorted(i.number for i in not_active)
        launching_numbers = sorted(i.number for i in to_launch[:capacity])
        logger.info(
            "[PLAN] Issue pipeline: snapshot=%s → scheduler=%s (filtered=%d, dep_blocked=%d) "
            "→ eligible=%s → launching=%s (capacity=%d)",
            snapshot_numbers, available_numbers, scheduler_filtered,
            len(dependency_blocked), eligible_numbers, launching_numbers, capacity,
        )
        launching_set = set(launching_numbers)
        dep_blocked_map = {issue.number: reason for issue, reason in dependency_blocked}
        decision_by_issue: dict[int, str] = {}
        detail_by_issue = dict(decision_detail_by_issue)
        for issue in snapshot.issues:
            if issue.number in launching_set:
                decision_by_issue[issue.number] = "launch:scheduled"
                continue
            if issue.number in skip_reason_by_issue:
                decision_by_issue[issue.number] = f"skip:{skip_reason_by_issue[issue.number]}"
                continue
            if issue.number in dep_blocked_map:
                decision_by_issue[issue.number] = "skip:dependency_blocked"
                detail_by_issue[issue.number] = dep_blocked_map[issue.number]
                continue
            scheduler_reason = decision_reason_by_issue.get(issue.number, "unknown")
            decision_by_issue[issue.number] = f"skip:{scheduler_reason}"
        self._queue_decision_log.record(decision_by_issue, detail_by_issue)

        return actions, skipped, len(actions)

    def _plan_reviews(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
        worker_active_count: int,
        plan_context: PlanContext,
    ) -> tuple[list[Action], list[SkippedItem]]:
        """Plan which reviews to launch."""
        actions: list[Action] = []
        skipped: list[SkippedItem] = []
        if not self.review_workflow or not self.review_workflow.is_configured():
            return actions, skipped

        decision: ReviewDecision = self.review_workflow.should_launch_reviews(
            pending_reviews=list(snapshot.pending_reviews),
            active_session_count=worker_active_count,  # worker-only, not raw (#6824 F5)
            paused=snapshot.paused,
        )

        if decision.skip_reason:
            for review in snapshot.pending_reviews:
                logger.info(
                    issue_log(review.issue_number, "Skipped review: pr=#%d reason=%s"),
                    review.pr_number, decision.skip_reason
                )
                skipped.append(SkippedItem(
                    item_type="review",
                    number=review.pr_number,
                    reason=decision.skip_reason,
                ))
            return actions, skipped

        if decision.should_launch:
            for review in decision.reviews_to_launch[:capacity]:
                reviewer_label = self.config.get_reviewer_for_agent(review.agent_label) if review.agent_label else self.config.code_review_agent
                provider = self.provider_policy.provider_for_agent_label(reviewer_label) if self.provider_policy else None
                if provider and self.provider_policy and self.provider_policy.is_open(provider):
                    self._record_provider_skip(
                        issue_number=review.issue_number,
                        item_type="review",
                        item_number=review.pr_number,
                        provider=provider,
                        actions=actions,
                        skipped=skipped,
                        plan_context=plan_context,
                    )
                    continue
                logger.info(
                    issue_log(review.issue_number, "Selected for session: type=review pr=#%d slots_available=%d"),
                    review.pr_number, capacity
                )
                actions.append(LaunchSessionAction(
                    session_type=SessionType.REVIEW,
                    number=review.pr_number,
                    command="",  # Orchestrator will fill in
                    working_dir="",  # Orchestrator will fill in
                    reason=f"review queued for PR #{review.pr_number}",
                ))

        return actions, skipped

    def _plan_retrospective_reviews(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
        worker_active_count: int,
        plan_context: PlanContext,
    ) -> tuple[list[Action], list[SkippedItem]]:
        """Plan which retrospective reviews to launch."""
        actions: list[Action] = []
        skipped: list[SkippedItem] = []
        workflow = self.retrospective_review_workflow
        if not workflow or not workflow.is_configured():
            return actions, skipped

        decision: RetrospectiveReviewDecision = workflow.should_launch_reviews(
            pending_reviews=list(snapshot.pending_retrospective_reviews),
            active_session_count=worker_active_count,  # worker-only, not raw (#6824 F5)
            paused=snapshot.paused,
        )
        if decision.skip_reason:
            for review in snapshot.pending_retrospective_reviews:
                skipped.append(SkippedItem(
                    item_type="retrospective_review",
                    number=review.issue_number,
                    reason=decision.skip_reason,
                ))
            return actions, skipped

        if decision.should_launch:
            for review in decision.reviews_to_launch[:capacity]:
                reviewer_label = self.config.get_reviewer_for_agent(review.agent_label)
                provider = (
                    self.provider_policy.provider_for_agent_label(reviewer_label)
                    if self.provider_policy
                    else None
                )
                if provider and self.provider_policy and self.provider_policy.is_open(provider):
                    self._record_provider_skip(
                        issue_number=review.issue_number,
                        item_type="retrospective_review",
                        item_number=review.issue_number,
                        provider=provider,
                        actions=actions,
                        skipped=skipped,
                        plan_context=plan_context,
                    )
                    continue
                logger.info(
                    issue_log(
                        review.issue_number,
                        "Selected for session: type=retrospective-review slots_available=%d",
                    ),
                    capacity,
                )
                actions.append(LaunchSessionAction(
                    session_type=SessionType.RETROSPECTIVE_REVIEW,
                    number=review.issue_number,
                    command="",
                    working_dir="",
                    reason=f"retrospective review queued for issue #{review.issue_number}",
                ))

        return actions, skipped

    def _plan_reworks(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
        worker_active_count: int,
        plan_context: PlanContext,
    ) -> tuple[list[Action], list[SkippedItem]]:
        """Plan which reworks to launch."""
        actions: list[Action] = []
        skipped: list[SkippedItem] = []
        if not self.rework_workflow:
            return actions, skipped

        decision: ReworkDecision = self.rework_workflow.should_launch_reworks(
            pending_reworks=list(snapshot.pending_reworks),
            active_session_count=worker_active_count,  # worker-only, not raw (#6824 F5)
            paused=snapshot.paused,
        )

        if decision.skip_reason:
            for rework in snapshot.pending_reworks:
                issue_num = rework.resolve_issue_number()
                if issue_num is None:
                    logger.warning("Planner: skipping rework with unresolved issue number: %s", rework.issue_key)
                    continue
                logger.info(
                    issue_log(issue_num, "Skipped rework: cycle=%d reason=%s"),
                    rework.rework_cycle, decision.skip_reason
                )
                skipped.append(SkippedItem(
                    item_type="rework",
                    number=issue_num,
                    reason=decision.skip_reason,
                ))
            return actions, skipped

        if decision.should_launch:
            for rework in decision.reworks_to_launch[:capacity]:
                issue_num = rework.resolve_issue_number()
                if issue_num is None:
                    logger.warning("Planner: skipping rework with unresolved issue number: %s", rework.issue_key)
                    continue
                provider = self.provider_policy.provider_for_agent_label(rework.agent_type) if self.provider_policy else None
                if provider and self.provider_policy and self.provider_policy.is_open(provider):
                    self._record_provider_skip(
                        issue_number=issue_num,
                        item_type="rework",
                        item_number=issue_num,
                        provider=provider,
                        actions=actions,
                        skipped=skipped,
                        plan_context=plan_context,
                    )
                    continue
                # Check for escalation
                escalation = self.rework_workflow.should_escalate(rework.rework_cycle)
                if escalation.should_escalate:
                    logger.info(
                        issue_log(issue_num, "Escalating to human: cycle=%d max=%d"),
                        rework.rework_cycle, escalation.max_cycles
                    )
                    actions.append(EscalateToHumanAction(
                        issue_number=issue_num,
                        pr_number=rework.pr_number or issue_num,
                        escalation_reason=escalation.reason or "max rework cycles reached",
                        rework_cycles=rework.rework_cycle,
                        needs_human_label=self._lm.needs_human,
                        needs_rework_label=self._lm.needs_rework,
                        max_rework_cycles=self.config.max_rework_cycles,
                        issue_key=rework.issue_key.stable_id(),
                        reason=f"escalating: cycle {rework.rework_cycle} > max {escalation.max_cycles}",
                        expected=build_expected_for_mutation(),
                    ))
                else:
                    logger.info(
                        issue_log(issue_num, "Selected for session: type=rework cycle=%d slots_available=%d"),
                        rework.rework_cycle, capacity
                    )
                    actions.append(LaunchSessionAction(
                        session_type=SessionType.REWORK,
                        number=issue_num,
                        command="",  # Orchestrator will fill in
                        working_dir="",  # Orchestrator will fill in
                        reason=f"rework cycle {rework.rework_cycle} for issue #{issue_num}",
                    ))

        return actions, skipped

    def _plan_validation_retries(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
        plan_context: PlanContext,
    ) -> tuple[list[Action], list[SkippedItem]]:
        """Plan launch actions for coding sessions that need validation retry."""
        actions: list[Action] = []
        skipped: list[SkippedItem] = []
        seen_issue_numbers: set[int] = set()

        for retry in snapshot.pending_validation_retries:
            if len(actions) >= capacity:
                break
            issue_number = retry.issue_number
            if issue_number in seen_issue_numbers:
                skipped.append(SkippedItem(
                    item_type="validation_retry",
                    number=issue_number,
                    reason="duplicate pending validation retry",
                ))
                continue
            seen_issue_numbers.add(issue_number)

            if issue_number in snapshot.active_issue_numbers:
                skipped.append(SkippedItem(
                    item_type="validation_retry",
                    number=issue_number,
                    reason="active session running",
                ))
                logger.info(issue_log(issue_number, "Skipped validation retry: reason=active_session"))
                continue

            provider = (
                self.provider_policy.provider_for_agent_label(retry.agent_label)
                if self.provider_policy and retry.agent_label
                else None
            )
            if provider and self.provider_policy and self.provider_policy.is_open(provider):
                self._record_provider_skip(
                    issue_number=issue_number,
                    item_type="validation_retry",
                    item_number=issue_number,
                    provider=provider,
                    actions=actions,
                    skipped=skipped,
                    plan_context=plan_context,
                )
                continue

            logger.info(
                issue_log(issue_number, "Selected for session: type=validation_retry retry_count=%d slots_available=%d"),
                retry.retry_count,
                capacity,
            )
            actions.append(LaunchValidationRetryAction(
                issue_number=issue_number,
                retry_count=retry.retry_count,
                reason=f"validation retry {retry.retry_count} for issue #{issue_number}",
            ))

        return actions, skipped

    def _plan_tech_lead(
        self,
        snapshot: OrchestratorSnapshot,
        capacity: int,
        plan_context: PlanContext,
        *,
        reserved: bool = False,
        suppressed_issue_numbers: frozenset[int] = frozenset(),
    ) -> tuple[list[Action], list[SkippedItem]]:
        """Plan which tech_lead reviews to launch.

        ``reserved`` selects the budget the launch gate uses: when False
        (default) tech_lead draws from the shared worker budget and the workflow
        gates on ``max_concurrent_sessions`` exactly as before; when True
        ``capacity`` is the reserved additive tech_lead budget and the workflow
        gates on it directly, so tech_lead launches even at worker saturation.
        """
        actions: list[Action] = []
        skipped: list[SkippedItem] = []
        if not self.tech_lead_workflow or not self.tech_lead_workflow.is_configured():
            return actions, skipped

        # A failure-investigation whose storm cohort was escalated this tick is
        # dropped from the launch set (#6780). Log the drop instead of removing
        # it silently, so a suppressed item explains WHY in its per-issue trace.
        pending_tech_lead = []
        for item in snapshot.pending_tech_lead:
            if (
                item.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
                and item.issue_number in suppressed_issue_numbers
            ):
                self._tech_lead_launch_log.note_suppressed(item, len(snapshot.pending_tech_lead))
            else:
                pending_tech_lead.append(item)
        # Provider eligibility precedes the workflow decision so the launching
        # event can never claim a launch the provider gate then suppresses
        # (#6892). One shared agent/provider => one gate for the whole queue.
        provider = self.provider_policy.provider_for_agent_label(self.config.tech_lead_review_agent) if self.provider_policy else None
        if provider and self.provider_policy and self.provider_policy.is_open(provider):
            for tech_lead in pending_tech_lead:
                self._record_provider_skip(
                    issue_number=tech_lead.issue_number,
                    item_type="tech_lead",
                    item_number=tech_lead.issue_number,
                    provider=provider,
                    actions=actions,
                    skipped=skipped,
                    plan_context=plan_context,
                )
            # Nothing is asked of the workflow => no launching event; log all as provider-skipped.
            self._tech_lead_launch_log.launch_outcomes(
                pending_tech_lead,
                [],
                list(pending_tech_lead),
                reserved=reserved,
                provider=provider,
            )
            return actions, skipped

        decision: TechLeadDecision = self.tech_lead_workflow.should_launch_tech_lead(
            pending_tech_lead=pending_tech_lead,
            paused=snapshot.paused,
            available_slots=capacity,  # owner-computed availability (worker_budget)
        )

        if decision.skip_reason:
            for tech_lead in pending_tech_lead:
                skipped.append(SkippedItem(
                    item_type="tech_lead",
                    number=tech_lead.issue_number,
                    reason=decision.skip_reason,
                ))
            self._tech_lead_launch_log.gate_skip(pending_tech_lead, decision.skip_reason)
            return actions, skipped

        launched: list[PendingTechLeadReview] = []
        if decision.should_launch:
            # Provider cleared above => every asked item launches; TECH_LEAD_LAUNCHING count == these actions.
            for tech_lead in decision.tech_lead_to_launch[:capacity]:
                actions.append(LaunchSessionAction(
                    session_type=SessionType.TECH_LEAD,
                    number=tech_lead.issue_number,
                    command="",  # Orchestrator will fill in
                    working_dir="",  # Orchestrator will fill in
                    title=tech_lead.title,
                    reason=f"tech_lead review for #{tech_lead.issue_number}",
                ))
                launched.append(tech_lead)
            # Per-item outcome (launch / capacity-deferred), on-change so a queued session is never silent.
            self._tech_lead_launch_log.launch_outcomes(
                pending_tech_lead,
                launched,
                [],
                reserved=reserved,
                provider=provider,
            )

        return actions, skipped

    def _get_priority_reason(self, issue: Issue) -> str:
        """Get a human-readable priority explanation for an issue."""
        parts = []
        if issue.milestone:
            parts.append(f"milestone={issue.milestone}")
        # Could extract priority from title [Px-nnn] pattern
        match = re.search(r"\[P(\d)-\d+\]", issue.title)
        if match:
            parts.append(f"P{match.group(1)}")
        if not parts:
            parts.append(f"issue #{issue.number}")
        return ", ".join(parts)

    def explain_skip(self, issue_number: int, snapshot: OrchestratorSnapshot) -> str:
        """Explain why an issue would be skipped.

        Useful for debugging and UI display.
        """
        # Check if already active
        if issue_number in snapshot.active_issue_numbers:
            return f"Issue #{issue_number} already has an active session"

        # Check if paused
        if snapshot.paused:
            return "Orchestrator is paused"

        # Check worker capacity through the canonical owner. Raw active_count
        # includes additive reserved tech-lead sessions and can falsely report
        # a full worker lane.
        worker_slot = worker_slot_availability(self.config, snapshot.active_sessions)
        if not worker_slot.is_free:
            return f"At capacity ({worker_slot.active}/{worker_slot.maximum})"

        # Check max_issues_to_start
        if snapshot.max_issues_to_start is not None:
            if snapshot.issues_started_count >= snapshot.max_issues_to_start:
                return f"max_issues_to_start limit reached ({snapshot.max_issues_to_start})"

        # Check dependencies
        issue = next((i for i in snapshot.issues if i.number == issue_number), None)
        if issue and self.dependency_evaluator and issue.body:
            report = self.dependency_evaluator.evaluate_work_gate(
                issue.number, issue.body, issue.milestone, emit_event=False
            )
            if not report.can_start_work:
                return f"Blocked by dependencies: {report.work_summary()}"

        return "Unknown reason"
