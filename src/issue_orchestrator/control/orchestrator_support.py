"""OrchestratorSupport - extracted methods from Orchestrator per IMPLEMENT_THIS.md Phase 3.

This class holds methods moved from orchestrator.py to reduce its size.
The Orchestrator delegates to this class for support operations.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Callable, cast

if TYPE_CHECKING:
    from types import FrameType
    from ..domain.models import OrchestratorState
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from ..infra.config import Config
    from ..infra.orchestrator import Orchestrator
    from .planner import Planner
    from .planner_types import OrchestratorSnapshot, Plan
    from .action_applier import ActionApplier, ActionResult
    from .actions import Action
    from .cleanup_manager import CleanupManager
    from .session_manager import SessionManager
    from .fact_gatherer import FactGatherer
    from .state_machine_manager import StateMachineManager
    from .health_gate import HealthGate, HealthDecision
    from .open_issue_corpus import OpenIssueCorpusManager
    from .provider_launch_readiness import ProviderLaunchReadinessSampler
    from ..ports.worktree_manager import WorktreeManager
    from ..ports.issue import Issue

from ..events import EventName, EventContext
from ..ports import EventSink, make_trace_event, RepositoryHost
from .actions import AddLabelAction
from .stale_detection import _detect_stale_claims, _detect_stale_in_progress
from .queue_cache import (
    QueueCache,
    queue_shrink_confirmation_due,
    queue_shrink_confirmation_pending,
    record_issue_refreshes,
)
from .blocked_front_queue import front_queue_newly_unblocked, release_blocked_front_on_launch
from .dependency_gate_snapshot import build_refresh_snapshot
from .tech_lead_artifact_retention import clear_discovered_facts
from .tech_lead_run_ownership import TechLeadRunOwnership, single_instance_run_ownership
from .tech_lead_run_wiring import tech_lead_state_handlers
from .issue_fetch_resilience import IssueFetchResilience, TransientIssueFetchError
from .reconciliation import ReconciliationRequired, get_pause_label
from .tick_telemetry import report_slow_tick
from .session_history import (
    CLOSED_ISSUE_HISTORY_STATUS_REASON,
    ClosedIssueHistoryMutation,
    SessionHistoryOwner,
)
from .transition_log import log_transition
from ..domain.models import (
    BLOCKED_HISTORY_STATUSES,
    PendingRetrospectiveReview,
    PendingReview, PendingRework,
)

logger = logging.getLogger(__name__)

_BLOCKED_HISTORY_HOT_REFRESH_LOOKBACK = 200


def init_orchestrator_components(orch: "Orchestrator") -> None:
    """Initialize orchestrator components - moved per method table.

    This is the core logic from Orchestrator.__post_init__.
    All dependencies are now provided via OrchestratorDeps (no fallbacks needed).
    Helpers are exposed via @cached_property on Orchestrator - no fields set here.
    """
    from ..observation.observer import SessionObserver
    from .session_history import SessionHistoryOwner

    # Wire up the scheduler from the planner
    orch.scheduler = orch.deps.planner.scheduler

    # Wire up action_applier's session_launcher callback
    orch.deps.action_applier.session_launcher = orch.session_launcher_callback
    orch.deps.action_applier.validation_retry_launcher = orch.launch_validation_retry_by_number
    orch.deps.action_applier.claim_gate = orch.deps.claim_gate
    orch.deps.action_applier.lease_id_lookup = (
        lambda issue_number: next(
            (
                session.lease_id
                for session in orch.state.active_sessions
                if session.issue.number == issue_number
            ),
            None,
        )
    )
    # Bind to live state (not the current list object): recovery/retry paths
    # replace ``state.session_history`` wholesale, and a captured list would go
    # stale, stranding awaiting-merge reconciliation (#6692).
    orch.deps.action_applier.history_owner = SessionHistoryOwner(
        lambda: orch.state.session_history
    )

    # Create observer (still created here as it depends on runtime orchestrator state)
    orch.observer = SessionObserver(
        config=orch.config,
        events=orch.deps.events,
        session_runner=orch.deps.runner,
        repository_host=orch.deps.repository_host,
        fresh_issue_reader=orch.deps.fresh_issue_reader,
        session_output=orch.deps.session_output,
        provider_readiness_probe=orch.deps.provider_readiness_probe,
    )
    orch.observer.session_machines = orch.deps.state_machine_manager.session_machines

    # Attach TraceEvent emitter to completion processor (for review exchange events)
    if getattr(orch.deps, "completion_processor", None) is not None:
        orch.deps.completion_processor.set_event_emitter(
            orch.deps.events,
            orch.event_context,
        )



@dataclass
class OrchestratorSupport:
    """Support class holding methods extracted from Orchestrator."""

    config: "Config"
    events: EventSink
    repository_host: RepositoryHost
    state: "OrchestratorState"
    event_context: EventContext
    session_manager: "SessionManager"
    action_applier: "ActionApplier"
    fact_gatherer: "FactGatherer"
    planner: "Planner"
    worktree_manager: "WorktreeManager"
    state_machine_manager: "StateMachineManager"
    cleanup_manager: "CleanupManager"
    get_review_machine: Callable[[int, int], object]
    kill_session: Callable[[str], None]
    queue_cache_store: "QueueCacheStore | None" = None
    # Durable tech_lead ledgers (#6780). Anchor intake records a storm cohort
    # here, and the end-of-tick fact clear reads it to hold the cohort's run
    # artifacts. Optional so unrelated tests need not wire it; without it a
    # storm anchor cannot prove its cohort, so intake declines to collapse the
    # individual investigations rather than losing the problems.
    tech_lead_authority: "TechLeadAuthorityStore | None" = None
    # Cross-instance ownership of logical tech-lead runs; never None, so the
    # apply seam has nothing to branch on (#6994).
    run_ownership: "TechLeadRunOwnership" = field(
        default_factory=single_instance_run_ownership
    )

    _last_ui_update: float = field(default=0.0, init=False)
    _ui_update_interval: int = field(default=30, init=False)

    # Accessor properties per method table
    @property
    def _sm(self) -> "SessionManager":
        return self.session_manager

    @property
    def _aa(self) -> "ActionApplier":
        return self.action_applier

    @property
    def _fg(self) -> "FactGatherer":
        return self.fact_gatherer

    @property
    def _planner(self) -> "Planner":
        return self.planner

    @property
    def _wm(self) -> "WorktreeManager":
        return self.worktree_manager

    @property
    def _state_machines(self) -> "StateMachineManager":
        return self.state_machine_manager

    @property
    def _cleanup_manager(self) -> "CleanupManager":
        return self.cleanup_manager

    def _get_milestone_filter(self) -> str | None:
        return self.config.filtering.milestone

    def _check_health(self, health_gate: "HealthGate") -> object:
        """Check system health using HealthGate service."""
        return health_gate.check(paused=self.state.paused)

    def clear_discovered_facts(self, tick: "OrchestratorSnapshot") -> None:
        clear_discovered_facts(
            self.state, self.config, self.tech_lead_authority, tick_paused=tick.paused
        )

    def emit_heartbeat_if_needed(self) -> None:
        if time.time() - self._last_ui_update >= self._ui_update_interval and self.state.active_sessions:
            self.events.publish(make_trace_event(
                EventName.ORCHESTRATOR_HEARTBEAT,
                self.event_context.enrich({
                    "active_count": len(self.state.active_sessions),
                    "sessions": [s.issue.number for s in self.state.active_sessions],
                }),
            ))
            self._last_ui_update = time.time()

    def request_refresh(self, inflight_stable_ids: set[str] | None, inflight_dict: dict[str, float], ttl: float) -> None:
        if inflight_stable_ids:
            now = time.monotonic()
            expires_at = now + ttl
            for stable_id in inflight_stable_ids:
                inflight_dict[stable_id] = expires_at
            logger.info("[REFRESH] Manual refresh requested with %d inflight IDs: %s",
                       len(inflight_stable_ids), sorted(inflight_stable_ids))
        else:
            logger.info("[REFRESH] Manual refresh requested")

    def apply_plan(self, plan: "Plan", pause_issue_callback: Callable[[int, str], None]) -> None:
        if plan.action_count == 0:
            return

        self.events.publish(make_trace_event(EventName.APPLY_STARTED, self.event_context.enrich({"steps": plan.action_count})))

        applied_count = 0
        failed_count = 0

        for action in plan.actions:
            if self.state.paused:
                break

            result_info = self._apply_single_action(action, pause_issue_callback)
            if result_info.success:
                applied_count += 1
            else:
                failed_count += 1

            if result_info.halt:
                break

        self.events.publish(make_trace_event(EventName.APPLY_COMPLETED, self.event_context.enrich({"applied_steps": applied_count, "failed_steps": failed_count})))

    @dataclass
    class _ActionApplyResult:
        """Result of applying a single action."""
        success: bool = False
        halt: bool = False  # Stop processing remaining actions

    def _apply_single_action(self, action: "Action", pause_issue_callback: Callable[[int, str], None]) -> "_ActionApplyResult":
        """Apply a single action and return the result."""
        from .actions import ActionType

        # Check tech_lead cooldown
        if action.action_type == ActionType.CREATE_TECH_LEAD_ISSUE and self._cleanup_manager:
            if not self._cleanup_manager.should_retry_tech_lead_issue():
                logger.warning("[PLAN] Skipping tech_lead issue creation due to cooldown")
                self._emit_apply_failed(action, "tech_lead_issue_creation_cooldown")
                return self._ActionApplyResult(success=False)

        try:
            result = self._aa.apply(action)
            if result.success:
                return self._handle_action_success(action, result)
            return self._handle_action_failure(action, result)
        except ReconciliationRequired as rr:
            return self._handle_reconciliation_error(rr, pause_issue_callback)
        except Exception as e:
            logger.exception("Failed to apply action %s: %s", action, e)
            self.events.publish(make_trace_event(EventName.APPLY_FAILED, self.event_context.enrich({"step_type": action.action_type.value, "error": str(e)})))
            return self._ActionApplyResult(success=False)

    def _handle_action_success(self, action: "Action", result: "ActionResult") -> "_ActionApplyResult":
        """Handle successful action application."""
        self._update_state_after_action(action, result)
        self.events.publish(make_trace_event(
            EventName.APPLY_STEP_APPLIED,
            self.event_context.enrich({"step_type": action.action_type.value, "issue_number": self._get_action_issue_number(action), "result": "success"}),
        ))
        return self._ActionApplyResult(success=True)

    def _handle_action_failure(self, action: "Action", result: "ActionResult") -> "_ActionApplyResult":
        """Handle failed action application."""
        from .actions import ActionType

        logger.warning("[PLAN] Action %s failed: %s", action.action_type.value, result.error)

        # Mark issue as failed if applicable
        failed_actions_mark_issue = {
            ActionType.ADD_LABEL,
            ActionType.REMOVE_LABEL,
            ActionType.SYNC_LABELS,
            ActionType.ADD_COMMENT,
            ActionType.LAUNCH_SESSION,
        }
        if action.action_type in failed_actions_mark_issue:
            issue_number = self._resolve_action_issue_number(action)
            if issue_number is not None:
                self.state.failed_this_cycle.add(issue_number)
                logger.info("[PLAN] Marked issue #%d failed_this_cycle due to %s failure", issue_number, action.action_type.value)

        # Handle tech_lead issue failure cooldown
        if action.action_type.value == "create_tech_lead_issue" and self._cleanup_manager:
            try:
                self._cleanup_manager.mark_tech_lead_issue_failure()
            except Exception:
                pass

        self._emit_apply_failed(action, result.error or "unknown")
        return self._ActionApplyResult(success=False)

    def _handle_reconciliation_error(self, rr: ReconciliationRequired, pause_issue_callback: Callable[[int, str], None]) -> "_ActionApplyResult":
        """Handle reconciliation required error."""
        issue_number = rr.entity_id
        logger.warning("[RECONCILIATION] Drift detected for %s #%d: %s", rr.entity_type, issue_number, rr.reason)
        self.events.publish(make_trace_event(
            EventName.RECONCILIATION_REQUIRED,
            self.event_context.enrich({
                "issue_number": issue_number, "entity_type": rr.entity_type, "reason": rr.reason,
                "expected_labels": list(rr.expected.labels), "actual_labels": list(rr.actual.labels),
            }),
        ))
        pause_issue_callback(issue_number, rr.reason)
        return self._ActionApplyResult(success=False, halt=True)

    def _emit_apply_failed(self, action: "Action", error: str) -> None:
        """Emit APPLY_FAILED event."""
        self.events.publish(make_trace_event(
            EventName.APPLY_FAILED,
            self.event_context.enrich({"step_type": action.action_type.value, "issue_number": self._get_action_issue_number(action), "error": error}),
        ))

    def _get_action_issue_number(self, action: "Action") -> int | None:
        """Get issue number from action for events."""
        return getattr(action, "number", getattr(action, "issue_number", None))

    def _resolve_action_issue_number(self, action: "Action") -> int | None:
        """Resolve issue number from action, excluding PRs."""
        issue_number = getattr(action, "issue_number", None)
        if issue_number is not None:
            return issue_number
        number = getattr(action, "number", None)
        if number is None or getattr(action, "is_pr", False):
            return None
        return number

    def _update_state_after_action(self, action: "Action", result: "ActionResult") -> None:
        from .actions import ActionType

        handlers = {
            ActionType.LAUNCH_SESSION: self._handle_launch_session,
            ActionType.LAUNCH_VALIDATION_RETRY: self._handle_launch_validation_retry,
            ActionType.ESCALATE_TO_HUMAN: self._handle_escalate_to_human,
            ActionType.CREATE_TECH_LEAD_ISSUE: self._handle_create_tech_lead_issue,
            ActionType.CLEANUP_SESSION: self._handle_cleanup_session,
            ActionType.QUEUE_REVIEW: self._handle_queue_review,
            ActionType.QUEUE_RETROSPECTIVE_REVIEW: self._handle_queue_retrospective_review,
            ActionType.QUEUE_REWORK: self._handle_queue_rework,
            **tech_lead_state_handlers(self),  # every tech-lead queue transition
        }

        handler = handlers.get(action.action_type)
        if handler:
            handler(action, result)

    def _handle_launch_session(self, action: "Action", result: "ActionResult") -> None:
        from .actions import LaunchSessionAction
        a = cast(LaunchSessionAction, action)
        logger.info("[PLAN] Launched %s session for #%d", a.session_type, a.number)
        release_blocked_front_on_launch(self.state, a.session_type, result.issue_number)

    def _handle_launch_validation_retry(self, action: "Action", result: "ActionResult") -> None:
        from .actions import LaunchValidationRetryAction
        a = cast(LaunchValidationRetryAction, action)
        logger.info(
            "[PLAN] Launched validation retry for issue #%d (retry_count=%d)",
            a.issue_number,
            a.retry_count,
        )

    def _handle_escalate_to_human(self, action: "Action", result: "ActionResult") -> None:
        from .actions import EscalateToHumanAction
        a = cast(EscalateToHumanAction, action)
        logger.info("[PLAN] Escalated PR #%d (cycle %d)", a.pr_number, a.rework_cycles)

    def _handle_create_tech_lead_issue(self, action: "Action", result: "ActionResult") -> None:
        from .actions import CreateTechLeadIssueAction
        from .tech_lead_run_wiring import intake_owned_tech_lead_anchor
        num = result.details.get("issue_number")
        if num:
            # Via the run-ownership owner: the periodic/storm anchor is the
            # same logical global run the dashboard and CLI request (#6994).
            intake_owned_tech_lead_anchor(cast(CreateTechLeadIssueAction, action), num, self)
            logger.info("Created tech_lead #%d", num)

    def _handle_cleanup_session(self, action: "Action", result: "ActionResult") -> None:
        from .actions import CleanupSessionAction
        a = cast(CleanupSessionAction, action)
        self.state.pending_cleanups = [c for c in self.state.pending_cleanups if c.pr_number != a.pr_number]
        # F8 second guard: a disposable cleanup fact is RETAINED across the clear,
        # so drop it here only on SUCCESS — a failed removal is re-planned next tick.
        if a.disposable_worktree and result.success:
            self.state.immediate_cleanups = [
                c
                for c in self.state.immediate_cleanups
                if not (c.issue_number == a.issue_number and c.scratch_worktree)
            ]

    def _handle_queue_review(self, action: "Action", result: "ActionResult") -> None:
        from .actions import QueueReviewAction
        a = cast(QueueReviewAction, action)
        if any(r.pr_number == a.pr_number for r in self.state.pending_reviews):
            return
        self.state.pending_reviews.append(
            PendingReview(
                issue_key=self.repository_host.create_issue_key(a.issue_number),
                pr_number=a.pr_number, pr_url=a.pr_url, branch_name=a.branch_name,
                _issue_number=a.issue_number, agent_label=a.agent_label, issue_labels=a.issue_labels,
            )
        )
        log_transition("review", a.pr_number, "CREATED", "QUEUED", f"from #{a.issue_number}")
        self.get_review_machine(a.pr_number, a.issue_number)

    def _handle_queue_retrospective_review(self, action: "Action", result: "ActionResult") -> None:
        from .actions import QueueRetrospectiveReviewAction
        a = cast(QueueRetrospectiveReviewAction, action)
        if self.state.has_pending_or_active_retrospective_review(a.issue_number):
            return
        self.state.pending_retrospective_reviews.append(
            PendingRetrospectiveReview(
                issue_key=self.repository_host.create_issue_key(a.issue_number),
                issue_number=a.issue_number,
                issue_title=a.issue_title,
                agent_label=a.agent_label,
                trigger_label=a.trigger_label,
                prior_pr_number=a.prior_pr_number,
                prior_pr_url=a.prior_pr_url, issue_labels=a.issue_labels,
            )
        )
        log_transition(
            "retrospective-review",
            a.issue_number,
            "CREATED",
            "QUEUED",
            "from trigger label",
        )

    def _handle_queue_rework(self, action: "Action", result: "ActionResult") -> None:
        from .actions import QueueReworkAction
        a = cast(QueueReworkAction, action)
        if any(r.resolve_issue_number() == a.issue_number for r in self.state.pending_reworks):
            return
        agent = next((r.agent_type for r in self.state.discovered_reworks if r.issue_number == a.issue_number), "agent:developer")
        self.state.pending_reworks.append(
            PendingRework(
                self.repository_host.create_issue_key(a.issue_number),
                agent,
                a.rework_cycle,
                issue_number=a.issue_number,
                pr_number=a.pr_number or None,
                source=a.source,
                feedback=a.feedback,
            )
        )
        log_transition("rework", a.issue_number, "CREATED", "QUEUED", f"cycle {a.rework_cycle}")

    def update_queue_cache(self) -> None:
        from .queue_projection import QueueProjection
        projection = QueueProjection(
            self.config,
            self.repository_host,
            self.events,
            self.queue_cache_store,
        )
        projection.update_and_emit(self.state)

    def recover_orphaned_cleanups(self) -> None:
        self._cleanup_manager.recover_orphaned_cleanups(
            lambda msg: setattr(self.state, 'startup_message', msg)
        )


def pause_issue_for_reconciliation(
    events: EventSink,
    action_applier: "ActionApplier",
    event_context: EventContext,
    issue_number: int,
    reason: str,
) -> None:
    """Pause an issue due to reconciliation failure (state drift)."""
    pause_label = get_pause_label()
    try:
        action_applier.apply(AddLabelAction(
            issue_number=issue_number,
            label=pause_label,
            reason="reconciliation drift detected",
        ))
        logger.warning(
            "[RECONCILIATION] Paused issue #%d with label '%s': %s",
            issue_number, pause_label, reason
        )
        events.publish(make_trace_event(
            EventName.ISSUE_PAUSED_RECONCILE,
            event_context.enrich({
                "issue_number": issue_number,
                "pause_label": pause_label,
                "reason": reason,
            }),
        ))
    except Exception as e:
        logger.error(
            "[RECONCILIATION] Failed to add pause label to #%d: %s",
            issue_number, e
        )


def emit_heartbeat_if_needed(
    events: EventSink,
    event_context: EventContext,
    state: "OrchestratorState",
    last_ui_update: float,
    ui_update_interval: int = 30,
) -> float:
    """Emit heartbeat if needed - moved per method table.

    Returns:
        Updated last_ui_update timestamp
    """
    now = time.time()
    if now - last_ui_update >= ui_update_interval:
        events.publish(make_trace_event(
            EventName.ORCHESTRATOR_IDLE,
            event_context.enrich({
                "active_sessions": len(state.active_sessions),
                "paused": state.paused,
            }),
        ))
        return now
    return last_ui_update


def check_health(
    health_gate: "HealthGate",
    paused: bool,
) -> "HealthDecision":
    """Check system health - moved per method table."""
    return health_gate.check(paused=paused)


def handle_signal(
    orchestrator: "Orchestrator",
    signum: int,
    frame: Optional["FrameType"],
) -> None:
    """Handle OS signal (SIGINT, SIGTERM) - moved per method table.

    Args:
        orchestrator: The Orchestrator instance.
        signum: Signal number.
        frame: Stack frame (unused).
    """
    # Force shutdown if already requested, otherwise graceful
    orchestrator.request_shutdown(force=orchestrator.shutdown_requested)


# Keep PlanApplier for backward compatibility during transition
PlanApplier = OrchestratorSupport


def run_planning_cycle(
    config: "Config",
    events: EventSink,
    event_context: EventContext,
    state: "OrchestratorState",
    fact_gatherer: "FactGatherer",
    planner: "Planner",
    repository_host: RepositoryHost,
    scheduler: object,
    github_workflow: object,
    apply_plan_fn: Callable[["Plan"], None],
    clear_discovered_facts_fn: Callable[["OrchestratorSnapshot"], None],
    last_network_sync: float,
    refresh_requested: bool,
    inflight_stable_ids: dict[str, float],
    issue_fetch_resilience: IssueFetchResilience,
    observer: object | None = None,
    claim_manager: object | None = None,
    queue_cache_store: "QueueCacheStore | None" = None,
    io_claimed_label: str = "io:claimed",
    open_issue_corpus: "OpenIssueCorpusManager | None" = None,
    provider_launch_sampler: "ProviderLaunchReadinessSampler | None" = None,
) -> tuple[float, bool]:
    """Run the planning cycle - extracted from Orchestrator per move map Step 2."""
    now = time.time()
    should_fetch = (
        now - last_network_sync >= config.fetch_layer_network_sync_seconds
    ) or refresh_requested or queue_shrink_confirmation_due(state, now)

    if should_fetch:
        try:
            last_network_sync, refresh_requested = _fetch_and_update_queue(
                config, events, state, repository_host, scheduler, github_workflow,
                refresh_requested, inflight_stable_ids,
                issue_fetch_resilience=issue_fetch_resilience,
                queue_cache_store=queue_cache_store,
                open_issue_corpus=open_issue_corpus,
            )
        except TransientIssueFetchError as exc:
            # Recoverable issue-list failure (transient 404/5xx/network): keep
            # the cached queue and resume the normal refresh cadence instead of
            # crashing or burning the loop error budget. The orchestrator is
            # label-recoverable, so skipping a refresh is cheap.
            #
            # Only the issue-list fetch itself is guarded inside
            # _fetch_and_update_queue; a RepositoryHostError from a downstream
            # PR/dependency scan is *not* classified as an issue-fetch failure
            # and propagates to the loop-error path instead.
            logger.warning(
                "[FETCH] %s — keeping cached queue (%d issue(s)); will retry next cycle. %s",
                exc.summary, len(state.cached_queue_issues), exc.suggested_fix,
            )
            last_network_sync = now
        # PermanentIssueFetchError is intentionally NOT caught here: it
        # propagates to the run loop, which logs the actionable message and
        # shuts down cleanly rather than crashing with a raw traceback.

    # Detect stale issues and claims
    stale_issues = _detect_stale_in_progress(observer, state, events, event_context)
    stale_claim_issues = _detect_stale_claims(state.cached_queue_issues, state.active_sessions, claim_manager, events, event_context, io_claimed_label=io_claimed_label)

    # Sample provider launch eligibility BEFORE planning: it probes a CLI and
    # writes circuit state, so it is the tick's job, not the pure planner's
    # (#6999 A3). Planning then reads the result as a snapshot fact.
    provider_launch = provider_launch_sampler.sample() if provider_launch_sampler else None

    # Create snapshot and plan
    snapshot = fact_gatherer.create_snapshot(state, state.cached_queue_issues, stale_in_progress_issues=stale_issues, stale_claim_issues=stale_claim_issues, provider_launch=provider_launch)
    _emit_facts_gathered(events, event_context, state, stale_issues)

    plan = planner.plan(snapshot)
    _emit_plan_computed(events, event_context, plan)

    if plan.action_count > 0:
        logger.info("[PLAN] %d action(s): %s", plan.action_count, ", ".join(f"{a.action_type.value}:{getattr(a, 'number', '?')}" for a in plan.actions))

    apply_plan_fn(plan)
    _track_stale_ticks(config, events, event_context, state, stale_issues)
    # The tick's OWN snapshot decides retention: this call is separated from
    # the snapshot by a fetch, planning and apply, and `state.paused` can have
    # been flipped from the web thread in that window.
    clear_discovered_facts_fn(snapshot)

    return last_network_sync, refresh_requested


def _fetch_issue_list(
    config: "Config",
    state: "OrchestratorState",
    github_workflow: object,
    required_stable_ids: set[str] | None,
    sync_plan: "_SelectiveSyncPlan",
    full_scan: bool,
) -> tuple[list["Issue"], set[int], str | None]:
    """Obtain the issue payload + next watermark from the repository host.

    This is the single repository-host *issue-list fetch* the resilience policy
    guards. It performs no queue mutation, PR/dependency scan, or persistence —
    those are downstream of the guarded boundary in ``_fetch_and_update_queue``.
    """
    if full_scan:
        all_issues = github_workflow.fetch_all_issues(config.filtering.milestone, required_stable_ids)
        refreshed_numbers = {issue.number for issue in all_issues}
        next_watermark: str | None = _iso_now_utc()
    else:
        all_issues, refreshed_numbers, next_watermark = _fetch_incremental_issues(
            config,
            state,
            github_workflow,
            required_stable_ids,
            sync_plan,
        )
    return all_issues, refreshed_numbers, next_watermark


def _fetch_and_update_queue(
    config: "Config",
    events: EventSink,
    state: "OrchestratorState",
    repository_host: RepositoryHost,
    scheduler: object,
    github_workflow: object,
    refresh_requested: bool,
    inflight_stable_ids: dict[str, float],
    *,
    issue_fetch_resilience: IssueFetchResilience,
    queue_cache_store: "QueueCacheStore | None" = None,
    open_issue_corpus: "OpenIssueCorpusManager | None" = None,
) -> tuple[float, bool]:
    """Fetch issues and update queue cache.
    The GitHub issue-read phase is run under ``issue_fetch_resilience``; the
    downstream work (label cache, PR scan, dependency scan, closed-history
    reconciliation, queue mutation, persistence) is *not* guarded. A
    ``RepositoryHostError`` from those downstream operations is a genuine
    failure of that operation, not an issue-list fetch failure, so it must not
    be reclassified as a (potentially shutdown-promoting) issue-fetch error.
    """
    from ..infra import gh_audit

    refresh_started_at = time.time()
    manual_refresh = refresh_requested
    gh_usage_before = gh_audit.get_live_usage_snapshot()
    required_stable_ids = set(inflight_stable_ids.keys()) if inflight_stable_ids else None
    state.queue_refresh_in_progress = True
    # The request flag is captured and cleared by Orchestrator._run_planning_cycle
    # so requests made during this fetch survive for the next tick.

    try:
        if required_stable_ids:
            logger.info("[FETCH] %s refresh with %d required inflight IDs: %s", "Manual" if manual_refresh else "Scheduled", len(required_stable_ids), sorted(required_stable_ids))
        else:
            logger.info("[FETCH] %s refresh", "Manual" if manual_refresh else "Scheduled")

        reason = gh_audit.AuditReason.QUEUE_REFRESH_MANUAL if manual_refresh else gh_audit.AuditReason.QUEUE_REFRESH_SCHEDULED
        scope = gh_audit.AuditScope.MANUAL if manual_refresh else gh_audit.AuditScope.PERIODIC
        full_scan = _should_run_full_scan(config, state, manual_refresh, required_stable_ids, refresh_started_at)
        sync_plan = _build_selective_sync_plan(config, state, manual_refresh, full_scan)
        # Guard the GitHub issue reads; downstream queue mutation stays outside.
        with gh_audit.context(reason=reason, scope=scope):
            all_issues, refreshed_numbers, next_watermark = issue_fetch_resilience.guard(
                lambda: _fetch_issue_list(
                    config, state, github_workflow, required_stable_ids, sync_plan, full_scan,
                )
            )
            if open_issue_corpus is not None:
                issue_fetch_resilience.guard(open_issue_corpus.sync)

        refreshed_at = time.time()
        _process_inflight_ids(required_stable_ids, all_issues, inflight_stable_ids)
        _update_label_cache(repository_host, all_issues)
        _record_issue_refreshes(state, refreshed_numbers, refreshed_at)
        _reconcile_closed_issue_history(state, all_issues)

        github_workflow.scan_pending_pr_work(state, include_general_scans=sync_plan.run_pr_scan)

        if sync_plan.run_dependency_scan:
            # dep_blocked is the scheduler's *availability* verdict; the dashboard
            # snapshot is the four-gate state for every lane, evaluated through the
            # dependency-gate owner (not availability) with active-worktree ancestry.
            decisions = scheduler.evaluate_issues(all_issues, active_sessions=state.active_sessions)
            dep_blocked = [
                (d.issue, d.detail or "dependency blocked")
                for d in decisions
                if d.is_dependency_blocked
            ]
            github_workflow.update_dependency_problems(state, dep_blocked)
            front_queue_newly_unblocked(state, decisions)
            state.dependency_gate_snapshot = build_refresh_snapshot(
                scheduler.dependency_evaluator, all_issues, state.active_sessions
            )

        # Capture old queue state BEFORE mutation so the diff is correct.
        # (replace_from_refresh mutates state.cached_queue_issues in-place.)
        old_numbers = {i.number for i in state.cached_queue_issues}
        old_key_by_number = {i.number: i.key.stable_id() for i in state.cached_queue_issues}

        queue_cache = QueueCache(config, state, queue_cache_store)
        new_queue = queue_cache.replace_from_refresh(all_issues)
        shrink_confirmation_pending = queue_shrink_confirmation_pending(state)

        new_numbers = {i.number for i in new_queue}
        added_numbers = new_numbers - old_numbers
        removed_numbers = old_numbers - new_numbers
        if added_numbers or removed_numbers:
            added = [
                {"number": i.number, "title": i.title, "issue_key": i.key.stable_id()}
                for i in new_queue if i.number in added_numbers
            ]
            removed = [
                {"number": num, "issue_key": old_key_by_number.get(num, str(num))}
                for num in removed_numbers
            ]
            events.publish(make_trace_event(EventName.QUEUE_CHANGED, {
                "added": added, "removed": removed, "total": len(new_queue),
            }))
            logger.info("Queue changed: %d added, %d removed, %d total",
                        len(added), len(removed), len(new_queue))
        _update_queue_refresh_metadata(
            state=state,
            refresh_started_at=refresh_started_at,
            full_scan=full_scan,
            next_watermark=next_watermark,
            shrink_confirmation_pending=shrink_confirmation_pending,
        )

        if queue_cache_store is not None:
            queue_cache.save_snapshot()

        if state.failed_this_cycle:
            logger.info("[REFRESH] Clearing failed_this_cycle: %s (labels now synced from GitHub)", state.failed_this_cycle)
            state.failed_this_cycle.clear()

        gh_usage_after = gh_audit.get_live_usage_snapshot()
        gh_calls = int(gh_usage_after.get("total_calls", 0)) - int(gh_usage_before.get("total_calls", 0))
        gh_errors = int(gh_usage_after.get("errors", 0)) - int(gh_usage_before.get("errors", 0))
        duration_ms = int((time.time() - refresh_started_at) * 1000)
        logger.info(
            "[FETCH-COST] mode=%s trigger=%s gh_calls=%d gh_errors=%d duration_ms=%d refreshed_issues=%d",
            state.queue_last_refresh_mode,
            "manual" if manual_refresh else "scheduled",
            max(0, gh_calls),
            max(0, gh_errors),
            max(0, duration_ms),
            len(refreshed_numbers),
        )

        return refreshed_at, False
    finally:
        state.queue_refresh_in_progress = False


def _update_queue_refresh_metadata(
    *,
    state: "OrchestratorState",
    refresh_started_at: float,
    full_scan: bool,
    next_watermark: str | None,
    shrink_confirmation_pending: bool,
) -> None:
    state.queue_last_refresh_at = refresh_started_at
    state.queue_last_network_sync_at = refresh_started_at
    state.queue_refresh_count += 1
    if full_scan:
        state.queue_last_full_scan_at = refresh_started_at
    state.queue_last_refresh_mode = "full" if full_scan else "incremental"
    if shrink_confirmation_pending:
        logger.warning(
            "[QUEUE_CACHE] not advancing queue watermark while shrink confirmation is pending"
        )
        return
    state.queue_delta_watermark = next_watermark


def _should_run_full_scan(
    config: "Config",
    state: "OrchestratorState",
    manual_refresh: bool,
    required_stable_ids: set[str] | None,
    now: float,
) -> bool:
    if not config.fetch_layer_enabled:
        return True
    if manual_refresh:
        return True
    if required_stable_ids:
        return True
    if queue_shrink_confirmation_due(state, now):
        return False
    if not state.cached_queue_issues:
        return True
    if state.queue_last_full_scan_at <= 0:
        return True
    return (now - state.queue_last_full_scan_at) >= config.fetch_layer_full_scan_interval_seconds


def _should_run_pr_scan(config: "Config", state: "OrchestratorState", manual_refresh: bool) -> bool:
    if manual_refresh:
        return True
    if config.fetch_layer_pr_scan_every_n_refreshes <= 1:
        return True
    next_refresh_count = state.queue_refresh_count + 1
    return next_refresh_count % config.fetch_layer_pr_scan_every_n_refreshes == 0


def _should_run_dependency_scan(config: "Config", state: "OrchestratorState", manual_refresh: bool) -> bool:
    if manual_refresh:
        return True
    if config.fetch_layer_dependency_scan_every_n_refreshes <= 1:
        return True
    next_refresh_count = state.queue_refresh_count + 1
    return next_refresh_count % config.fetch_layer_dependency_scan_every_n_refreshes == 0


@dataclass(frozen=True)
class _SelectiveSyncPlan:
    run_discovery: bool
    run_pr_scan: bool
    run_dependency_scan: bool


def _build_selective_sync_plan(
    config: "Config",
    state: "OrchestratorState",
    manual_refresh: bool,
    full_scan: bool,
) -> _SelectiveSyncPlan:
    default_plan = _SelectiveSyncPlan(
        run_discovery=config.fetch_layer_discovery_limit > 0,
        run_pr_scan=_should_run_pr_scan(config, state, manual_refresh),
        run_dependency_scan=_should_run_dependency_scan(config, state, manual_refresh),
    )
    if not config.fetch_layer_selective_sync_planner_enabled:
        return default_plan
    if manual_refresh or full_scan:
        return _SelectiveSyncPlan(run_discovery=True, run_pr_scan=True, run_dependency_scan=True)

    # Selective planner throttles non-critical scans off-cycle to reduce GH load.
    run_discovery = config.fetch_layer_discovery_limit > 0
    run_pr_scan = _should_run_pr_scan(config, state, manual_refresh)
    run_dependency_scan = _should_run_dependency_scan(config, state, manual_refresh)
    has_visible_hints = bool(_get_visible_issue_numbers(state))
    if not has_visible_hints:
        run_pr_scan = run_pr_scan or (state.queue_refresh_count % 3 == 0)
        run_dependency_scan = run_dependency_scan or (state.queue_refresh_count % 2 == 0)
    return _SelectiveSyncPlan(
        run_discovery=run_discovery,
        run_pr_scan=run_pr_scan,
        run_dependency_scan=run_dependency_scan,
    )


def _fetch_incremental_issues(
    config: "Config",
    state: "OrchestratorState",
    github_workflow: object,
    required_stable_ids: set[str] | None,
    sync_plan: _SelectiveSyncPlan,
) -> tuple[list["Issue"], set[int], str | None]:
    issue_map = {issue.number: issue for issue in state.cached_queue_issues}
    pending_shrink_due = queue_shrink_confirmation_due(state, time.time())
    hot_issue_numbers = _select_hot_issue_numbers(
        state,
        config.fetch_layer_max_hot_issues_per_cycle,
        config.fetch_layer_visibility_aware_enabled,
        include_pending_shrink=pending_shrink_due,
    )
    _log_pending_shrink_confirmation(state, hot_issue_numbers, pending_shrink_due)
    refreshed = github_workflow.refresh_issues(hot_issue_numbers)
    refreshed_numbers: set[int] = {issue.number for issue in refreshed}
    for issue in refreshed:
        issue_map[issue.number] = issue

    next_watermark = state.queue_delta_watermark
    if sync_plan.run_discovery and config.fetch_layer_discovery_limit > 0:
        if state.queue_delta_watermark:
            discovered, delta_watermark = github_workflow.fetch_delta_issues(
                since=state.queue_delta_watermark,
                fetch_limit=config.fetch_layer_discovery_limit,
            )
            next_watermark = delta_watermark or next_watermark
            for issue in discovered:
                in_scope = github_workflow.issue_in_scope(issue)
                in_open_state = issue.state.lower() == "open"
                currently_tracked = issue.number in issue_map
                if in_scope and in_open_state:
                    issue_map[issue.number] = issue
                elif currently_tracked:
                    issue_map.pop(issue.number, None)
                refreshed_numbers.add(issue.number)
        else:
            discovered = github_workflow.fetch_discovery_issues(
                config.filtering.milestone,
                config.fetch_layer_discovery_limit,
            )
            next_watermark = _iso_now_utc()
            for issue in discovered:
                issue_map[issue.number] = issue
                refreshed_numbers.add(issue.number)

    # Ensure required IDs can still be discovered even if they were not in hot/discovery subsets.
    if required_stable_ids:
        fallback = github_workflow.fetch_all_issues(config.filtering.milestone, required_stable_ids)
        for issue in fallback:
            issue_map[issue.number] = issue
            refreshed_numbers.add(issue.number)

    return list(issue_map.values()), refreshed_numbers, next_watermark


def _log_pending_shrink_confirmation(
    state: "OrchestratorState",
    hot_issue_numbers: list[int],
    pending_shrink_due: bool,
) -> None:
    if not pending_shrink_due:
        return
    pending_missing = set(state.queue_pending_shrink_missing_issue_numbers)
    selected_missing = pending_missing.intersection(hot_issue_numbers)
    remaining = max(0, len(pending_missing) - len(selected_missing))
    logger.info(
        "[QUEUE_CACHE] confirming pending queue shrink with targeted issue refresh: "
        "pending=%d selected=%d remaining_after_limit=%d",
        len(pending_missing),
        len(selected_missing),
        remaining,
    )


def _iso_now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _select_hot_issue_numbers(
    state: "OrchestratorState",
    limit: int,
    visibility_aware_enabled: bool,
    *,
    include_pending_shrink: bool = False,
) -> list[int]:
    if limit <= 0 and not include_pending_shrink:
        return []
    effective_limit = (
        limit if limit > 0 else len(state.queue_pending_shrink_missing_issue_numbers)
    )

    hot_issue_numbers: list[int] = []
    seen: set[int] = set()
    for issue_number in _iter_hot_issue_numbers(
        state,
        visibility_aware_enabled,
        include_pending_shrink=include_pending_shrink,
    ):
        if issue_number in seen:
            continue
        seen.add(issue_number)
        hot_issue_numbers.append(issue_number)
        if len(hot_issue_numbers) >= effective_limit:
            break

    return hot_issue_numbers


def _iter_hot_issue_numbers(
    state: "OrchestratorState",
    visibility_aware_enabled: bool,
    *,
    include_pending_shrink: bool = False,
):
    yield from _pending_shrink_hot_numbers(state, include_pending_shrink)
    for session in state.active_sessions:
        yield session.issue.number
    for review in state.pending_reviews:
        yield review.issue_number
    yield from _pending_rework_issue_numbers(state)
    for issue_number in state.priority_queue:
        yield issue_number
    yield from _visible_hot_issue_numbers(state, visibility_aware_enabled)
    for issue in state.cached_queue_issues:
        yield issue.number
    yield from _iter_blocked_history_issue_numbers(state)


def _pending_shrink_hot_numbers(
    state: "OrchestratorState",
    include_pending_shrink: bool,
) -> tuple[int, ...]:
    if not include_pending_shrink:
        return ()
    return tuple(state.queue_pending_shrink_missing_issue_numbers)


def _pending_rework_issue_numbers(state: "OrchestratorState"):
    for rework in state.pending_reworks:
        if rework.issue_number is not None:
            yield rework.issue_number


def _visible_hot_issue_numbers(
    state: "OrchestratorState",
    visibility_aware_enabled: bool,
) -> list[int]:
    if not visibility_aware_enabled:
        return []
    return _get_visible_issue_numbers(state)


def _iter_blocked_history_issue_numbers(state: "OrchestratorState"):
    seen: set[int] = set()
    for entry in reversed(state.session_history[-_BLOCKED_HISTORY_HOT_REFRESH_LOOKBACK:]):
        if entry.status in BLOCKED_HISTORY_STATUSES:
            if entry.issue_number in seen:
                continue
            seen.add(entry.issue_number)
            yield entry.issue_number


def _reconcile_closed_issue_history(
    state: "OrchestratorState",
    issues: list["Issue"],
) -> list[ClosedIssueHistoryMutation]:
    """Reconcile closed issue history and return mutations for tests/logging."""
    closed_numbers = {
        issue.number
        for issue in issues
        if issue.state.lower() == "closed"
    }
    if not closed_numbers:
        return []

    owner = SessionHistoryOwner(state.session_history)
    mutations: list[ClosedIssueHistoryMutation] = []
    for issue_number in sorted(closed_numbers):
        result = owner.reconcile_closed_issue(
            issue_number=issue_number,
            status_reason=CLOSED_ISSUE_HISTORY_STATUS_REASON,
        )
        if isinstance(result, ClosedIssueHistoryMutation):
            mutations.append(result)
            logger.info(
                "[history] Reconciled closed issue history: issue=%d previous_status=%s",
                issue_number,
                result.previous_status,
            )
    return mutations


def _record_issue_refreshes(
    state: "OrchestratorState",
    refreshed_numbers: set[int],
    refreshed_at: float,
) -> None:
    record_issue_refreshes(state, refreshed_numbers, refreshed_at)


def _get_visible_issue_numbers(state: "OrchestratorState") -> list[int]:
    if state.ui_visible_updated_at <= 0:
        return []
    if (time.time() - state.ui_visible_updated_at) > 120:
        return []
    return state.ui_visible_issue_numbers


def _process_inflight_ids(required_stable_ids: set[str] | None, all_issues: list["Issue"], inflight_stable_ids: dict[str, float]) -> None:
    """Process and remove discovered inflight IDs."""
    if not required_stable_ids:
        return
    discovered_ids = {i.key.stable_id() for i in all_issues}
    found = required_stable_ids & discovered_ids
    if found:
        for sid in found:
            inflight_stable_ids.pop(sid, None)
        logger.info("[INFLIGHT] Discovered %d/%d required IDs: %s", len(found), len(required_stable_ids), sorted(found))
    still_missing = required_stable_ids - discovered_ids
    if still_missing:
        logger.warning("[INFLIGHT] Still missing %d required IDs after refresh: %s", len(still_missing), sorted(still_missing))


def _update_label_cache(repository_host: RepositoryHost, all_issues: list["Issue"]) -> None:
    """Update label cache for all issues."""
    for issue in all_issues:
        try:
            repository_host.update_label_cache(issue.number, list(issue.labels))
        except Exception:
            logger.debug("Failed to update label cache for issue #%s", issue.number, exc_info=True)


def emit_queue_changes(events: EventSink, state: "OrchestratorState", new_queue: list["Issue"]) -> None:
    """Detect and emit queue changes."""
    _emit_queue_changes(events, state, new_queue)


def _emit_queue_changes(events: EventSink, state: "OrchestratorState", new_queue: list["Issue"]) -> None:
    """Detect and emit queue changes.

    NOTE: ``state.cached_queue_issues`` must still reflect the *old* queue
    when this function is called.  If a caller mutates the cache first
    (e.g. via ``replace_from_refresh``), the diff will be empty and no
    event will be published.
    """
    old_numbers = {i.number for i in state.cached_queue_issues}
    old_key_by_number = {i.number: i.key.stable_id() for i in state.cached_queue_issues}
    new_numbers = {i.number for i in new_queue}
    added_numbers = new_numbers - old_numbers
    removed_numbers = old_numbers - new_numbers
    if added_numbers or removed_numbers:
        added = [
            {"number": i.number, "title": i.title, "issue_key": i.key.stable_id()}
            for i in new_queue if i.number in added_numbers
        ]
        removed = [
            {"number": num, "issue_key": old_key_by_number.get(num, str(num))}
            for num in removed_numbers
        ]
        events.publish(make_trace_event(EventName.QUEUE_CHANGED, {"added": added, "removed": removed, "total": len(new_queue)}))
        logger.info("Queue changed: %d added, %d removed, %d total", len(added), len(removed), len(new_queue))


def _emit_facts_gathered(events: EventSink, event_context: EventContext, state: "OrchestratorState", stale_issues: list["Issue"]) -> None:
    """Emit facts gathered event."""
    events.publish(make_trace_event(
        EventName.FACTS_GATHERED,
        event_context.enrich({
            "issues_count": len(state.cached_queue_issues), "active_sessions": len(state.active_sessions),
            "pending_reviews": len(state.pending_reviews), "pending_reworks": len(state.pending_reworks),
            "stale_in_progress_count": len(stale_issues),
        }),
    ))


def _emit_plan_computed(events: EventSink, event_context: EventContext, plan: "Plan") -> None:
    """Emit plan computed event."""
    events.publish(make_trace_event(EventName.PLAN_COMPUTED, event_context.enrich({"steps": plan.action_count, "actions": [a.action_type.value for a in plan.actions]})))


def track_stale_ticks(
    config: "Config",
    events: EventSink,
    event_context: EventContext,
    state: "OrchestratorState",
    stale_issues: list["Issue"],
) -> None:
    """Track stale ticks and emit escalation events."""
    _track_stale_ticks(config, events, event_context, state, stale_issues)


def _track_stale_ticks(
    config: "Config",
    events: EventSink,
    event_context: EventContext,
    state: "OrchestratorState",
    stale_issues: list["Issue"],
) -> None:
    """Track consecutive ticks with stale in-progress labels for escalation.

    Updates state.stale_issue_ticks and emits PERSISTENT_STALE_DETECTED
    if an issue exceeds the configured threshold.
    """
    current_stale = {i.number for i in stale_issues}

    # Increment for current stale issues
    for issue_num in current_stale:
        state.stale_issue_ticks[issue_num] = state.stale_issue_ticks.get(issue_num, 0) + 1

    # Clear for no-longer-stale issues and emit cleared event
    for issue_num in list(state.stale_issue_ticks.keys()):
        if issue_num not in current_stale:
            del state.stale_issue_ticks[issue_num]
            events.publish(make_trace_event(
                EventName.STALE_IN_PROGRESS_CLEARED,
                event_context.enrich({"issue_number": issue_num}),
            ))
            logger.info("[STALE] Issue #%d is no longer stale", issue_num)

    # Emit event if threshold exceeded (and threshold > 0)
    threshold = config.stale_escalation_ticks
    if threshold > 0:
        for issue_num, ticks in state.stale_issue_ticks.items():
            if ticks >= threshold:
                events.publish(make_trace_event(
                    EventName.PERSISTENT_STALE_DETECTED,
                    event_context.enrich({
                        "issue_number": issue_num,
                        "consecutive_ticks": ticks,
                        "threshold": threshold,
                    }),
                ))
                logger.warning(
                    "[STALE] Issue #%d has been stale for %d consecutive ticks (threshold: %d)",
                    issue_num, ticks, threshold
                )


# Backwards compatibility - keep PlanningCycleRunner as an alias
PlanningCycleRunner = run_planning_cycle


def run_tick(
    loop_iteration: int,
    event_context: EventContext,
    inflight_stable_ids: dict[str, float],
    state: "OrchestratorState",
    events: EventSink,
    shutdown_requested: bool,
    process_active_sessions_fn: Callable[[], None],
    check_health_fn: Callable[[], "HealthDecision"],
    run_planning_cycle_fn: Callable[[], None],
    emit_heartbeat_fn: Callable[[], None],
) -> tuple[int, bool]:
    """Execute one orchestration tick - extracted from Orchestrator per move map.

    Returns:
        Tuple of (updated_loop_iteration, should_continue)
    """
    tick_start = time.monotonic()
    loop_iteration += 1
    event_context.tick_id = loop_iteration

    state.last_tick_started_at = time.time()
    state.current_tick_phase = "starting"

    # Prune expired inflight IDs
    now = time.monotonic()
    expired = [sid for sid, exp in inflight_stable_ids.items() if exp < now]
    for sid in expired:
        del inflight_stable_ids[sid]
    if expired:
        logger.debug("[INFLIGHT] Pruned %d expired IDs: %s", len(expired), expired)

    logger.info(
        "[LOOP] Iteration %d - active=%d paused=%s pending_reviews=%d pending_reworks=%d pending_tech_lead=%d inflight=%d",
        loop_iteration,
        len(state.active_sessions),
        state.paused,
        len(state.pending_reviews),
        len(state.pending_reworks),
        len(state.pending_tech_lead_reviews),
        len(inflight_stable_ids),
    )

    # Emit tick.started
    events.publish(make_trace_event(
        EventName.TICK_STARTED,
        event_context.enrich({}),
    ))

    if shutdown_requested:
        events.publish(make_trace_event(
            EventName.TICK_COMPLETED,
            event_context.enrich({"idle": True, "reason": "shutdown_requested"}),
        ))
        state.current_tick_phase = ""
        state.last_tick_completed_at = time.time()
        return loop_iteration, False

    state.current_tick_phase = "active_sessions"
    active_start = time.monotonic()
    process_active_sessions_fn()
    active_elapsed = time.monotonic() - active_start
    if active_elapsed > 5:
        logger.warning(
            "[LOOP] Active session processing took %.1fs (active=%d)",
            active_elapsed,
            len(state.active_sessions),
        )

    # Use HealthGate for system-wide blockers only. The planner's worker_budget
    # owner enforces worker, reserved tech-lead, and E2E capacity. Planning must
    # still run at worker saturation so it can apply non-launch actions or use
    # an independent reserved budget.
    #
    # A paused orchestrator may still need to refresh its read-only queue
    # projection after labels change in GitHub; planning remains safe because
    # the paused snapshot produces no launch actions.
    health_decision = check_health_fn()
    refresh_while_paused = (
        not health_decision.can_proceed
        and health_decision.reason == "paused"
        and bool(state.queue_refresh_requested)
    )
    if health_decision.can_proceed or refresh_while_paused:
        state.current_tick_phase = "planning"
        plan_start = time.monotonic()
        run_planning_cycle_fn()
        plan_elapsed = time.monotonic() - plan_start
        if plan_elapsed > 5:
            logger.warning("[LOOP] Planning cycle took %.1fs", plan_elapsed)
    else:
        # Emit plan.noop when we skip planning
        events.publish(make_trace_event(
            EventName.PLAN_NOOP,
            event_context.enrich({"reason": health_decision.reason}),
        ))

    state.current_tick_phase = "heartbeat"
    emit_heartbeat_fn()

    # Emit tick.completed
    events.publish(make_trace_event(
        EventName.TICK_COMPLETED,
        event_context.enrich({
            "idle": len(state.active_sessions) == 0,
            "active_sessions": len(state.active_sessions),
        }),
    ))
    tick_elapsed = time.monotonic() - tick_start
    report_slow_tick(events, event_context, state, tick_elapsed, active_elapsed)
    state.current_tick_phase = ""
    state.last_tick_completed_at = time.time()
    return loop_iteration, True
