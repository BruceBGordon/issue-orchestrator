"""Main orchestrator - ties everything together."""

import asyncio, logging, os, signal, threading, time
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Optional, cast

if TYPE_CHECKING:
    from ..ports.provider_resilience import ProviderErrorType
    from ..control.planner_types import OrchestratorSnapshot, Plan
    from ..control.session_manager import SessionRef, SessionType
    from ..control.tech_lead_trigger import TechLeadTerminationOutcome
    from ..domain.tech_lead_session import TechLeadLaunchScope
    from ..ports.operator_issue_commands import OperatorIssueCommands
    from ..ports.session_runner import DiscoveredSession

from ..events import EventName, EventContext, EventHub
from ..control.orchestrator_support import (
    OrchestratorSupport,
    run_planning_cycle as _run_planning_cycle_impl,
    run_tick as _run_tick_impl,
    pause_issue_for_reconciliation,
    check_health as _check_health,
    init_orchestrator_components,
    handle_signal as _handle_signal,
)
from ..control.github_workflow import GitHubWorkflow, launch_issue_by_number as _gw_launch_issue_by_number, get_issue_machine as _gw_get_issue_machine
from ..control.worktree_manager import get_worktree_path, get_session_name, extract_issue_branches

logger = logging.getLogger(__name__)


from .config import Config
from ..ports.issue import Issue
from ..domain.models import Session, SessionStatus, OrchestratorState, PendingRetrospectiveReview, PendingReview, PendingRework, PendingTechLeadReview, AgentConfig, ORCHESTRATOR_PR_MARKER
from ..observation.observer import SessionObserver
from ..control.scheduler import Scheduler
from ..domain.state_machines.issue_machine import IssueStateMachine
from ..domain.state_machines.session_machine import SessionStateMachine
from ..domain.state_machines.review_machine import ReviewStateMachine
from ..control.session_completion import (
    handle_session_completion as _handle_session_completion,
    process_active_sessions as _process_active_sessions,
)
from ..control.session_launcher import SessionLauncher
from ..control.board_snapshot_builder import StateBoardSnapshotProvider
from ..control.tech_lead_run_wiring import TechLeadRunAdmission, TechLeadRunRequest, orchestrator_health_review_anchor as _ensure_health_review_anchor, orchestrator_launch_tech_lead_run as _launch_tech_lead_run, orchestrator_tech_lead_run as _admit_tech_lead_run, reconcile_orchestrator_tech_lead_ownership as _reconcile_tech_lead_ownership
from ..control.session_routing import (
    orchestrator_launch_review_session as _launch_review_session,
    orchestrator_launch_retrospective_review_session as _launch_retrospective_review_session,
    orchestrator_launch_rework_session as _launch_rework_session,
    orchestrator_launch_validation_retry_session as _launch_validation_retry_session,
    orchestrator_launch_tech_lead_session as _launch_tech_lead_session,
    session_launcher_callback as _session_launcher_callback,
    recover_unresolved_work as _recover_unresolved_work,
    restore_running_sessions as _restore_running_sessions,
    parse_session_ref as _parse_session_ref,
    create_session as _create_session,
    session_exists as _session_exists,
    kill_session as _kill_session,
    orchestrator_launch_session as _launch_session,
    get_session_machine as _sl_get_session_machine,
)
from ..control.cleanup_manager import CleanupManager
from ..control.worker_budget import worker_slot_free
from ..control.review_exchange_lifecycle import (
    IssueRuntimeTermination,
    ReviewExchangeCancellation,
    cancel_issue_review_exchange,
    terminate_issue_runtime,
)
from ..control.completion_handler import (
    CompletionHandler,
    launch_review_by_number as _ch_launch_review_by_number,
    launch_rework_by_number as _ch_launch_rework_by_number,
    launch_tech_lead_by_number as _ch_launch_tech_lead_by_number,
    get_review_machine as _ch_get_review_machine,
)
from ..control.startup_manager import StartupManager
from ..control.worktree_reconciliation import (
    StartupWorktreeReconciler,
    WorktreeAuditOwner,
)
from ..control.issue_fetch_resilience import (
    IssueFetchResilience,
    FetchFailureVerdict,
    PermanentIssueFetchError,
)
from ..ports import TraceEvent, RepositoryHost, SessionRunner
from ..ports.provider_resilience import ProviderCircuitStatusReader
from ..ports.repository_host import RepositoryHostError
from .startup_errors import StartupError, write_startup_failure
from ..control.health_gate import HealthDecision
from ..control.orchestrator_deps import OrchestratorDeps
from .e2e_runner import maybe_trigger_e2e, get_e2e_runner_manager
from .e2e_timeline import snapshot_agent_events
from .sqlite_maintenance import run_backups_if_due


@dataclass
class Orchestrator:
    """Main orchestrator - mediates gather → plan → apply cycle.

    All dependencies are injected via OrchestratorDeps - no Optional fields, no Null defaults.
    Bootstrap is the single source of truth for choosing implementations.
    """

    config: Config
    deps: OrchestratorDeps  # All dependencies bundled - no nulls, no optionals
    state: OrchestratorState = field(default_factory=OrchestratorState)
    scheduler: Scheduler = field(init=False)
    observer: SessionObserver = field(init=False)
    _shutdown_requested: bool = field(default=False, init=False)
    _inflight_stable_ids: dict[str, float] = field(default_factory=dict, init=False)  # stable_id -> expires_at (monotonic)
    _INFLIGHT_TTL_SECONDS: float = field(default=90.0, init=False, repr=False)
    _last_network_sync: float = field(default=0.0, init=False)
    _last_ui_update: float = field(default=0.0, init=False)
    _loop_iteration: int = field(default=0, init=False)
    _ui_update_interval: int = field(default=30, init=False)
    _event_context: EventContext = field(default_factory=EventContext, init=False)
    _loop_error_count: int = field(default=0, init=False)
    _loop_error_limit: int = field(default=3, init=False)
    _last_tick_time: float = field(default=0.0, init=False)
    # The serialization boundary for every state-mutating transition: the tick,
    # the control API, and the dashboard's tech-lead command surface all take it,
    # so read-then-mutate sequences on `state` cannot interleave across threads
    # (#6994 round 2 F8). Reentrant, so a transition nested inside a tick is
    # free. A public, injectable collaborator rather than a hidden attribute:
    # a test that needs to observe the boundary substitutes an instrumented
    # lock instead of reaching into internals.
    state_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _external_close_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _external_resources_closed: bool = field(default=False, init=False, repr=False)
    _last_backup_check: float = field(default=0.0, init=False)
    _last_orphan_reconcile_scan_at: float = field(default=0.0, init=False)
    _last_orphan_reconcile_active_count: int = field(default=0, init=False)
    _ORPHAN_RECONCILE_INTERVAL_SECONDS: ClassVar[float] = 30.0

    def __post_init__(self):
        # All validation is done by OrchestratorDeps being a frozen dataclass with no Optional fields.
        # If deps is constructed, all dependencies are present.
        init_orchestrator_components(self)

    @property
    def event_hub(self) -> EventHub:
        return self.deps.event_hub

    @property
    def repository_host(self) -> RepositoryHost:
        """Access the repository host (GitHub adapter) for issue/PR operations."""
        return self.deps.repository_host

    @property
    def session_runner(self) -> SessionRunner:
        """Access the session runner for terminal operations."""
        return self.deps.runner

    @property
    def provider_circuit(self) -> ProviderCircuitStatusReader:
        """Access the provider circuit-breaker status reader (issue #5980).

        A required facade property, so read-side consumers (the dashboard
        projection) depend on the behaviour-level port instead of traversing
        ``deps``, and a composition error cannot degrade into "no outage".
        """
        return self.deps.provider_resilience

    @property
    def shutdown_requested(self) -> bool:
        """Check if shutdown has been requested."""
        return self._shutdown_requested

    @shutdown_requested.setter
    def shutdown_requested(self, value: bool) -> None:
        """Set shutdown requested flag (use request_shutdown() for proper event emission)."""
        self._shutdown_requested = value

    @property
    def event_context(self) -> EventContext:
        """Access the event context for tick and event metadata."""
        return self._event_context

    @property
    def last_tick_time(self) -> float:
        """Get the timestamp of the last tick."""
        return self._last_tick_time

    @cached_property
    def operator_issue_commands(self) -> "OperatorIssueCommands":
        """Operator "retry"/"dismiss", bound to this facade's state and lock.

        The Control API asks for the COMMAND rather than assembling one: the
        transition it performs spans GitHub labels and the local retry/queue
        state and must settle them in that order, which is not a rule a
        transport can be trusted to re-derive twice (#6999 F5).
        """
        return self.deps.operator_issue_command_factory(
            state=lambda: self.state,
            run_locked=self._run_locked,
        )

    def _run_locked(self, fn):
        with self.state_lock:
            return fn()
    def kill_session(self, name: str) -> None:
        """Kill a session by terminal ID (public wrapper)."""
        self._kill_session(name)

    def terminate_tech_lead_session(self, session: "Session") -> "TechLeadTerminationOutcome":
        """Behaviour-complete termination of a tech_lead session (#6824 R7).

        The facade coordinates; the effects and their independence are owned by
        :mod:`..control.tech_lead_termination`.
        """
        from ..control.tech_lead_termination import terminate_tech_lead_session
        return terminate_tech_lead_session(self, session)

    def cancel_review_exchange_for_issue(
        self,
        issue_number: int,
        *,
        reason: str,
    ) -> ReviewExchangeCancellation:
        """Cancel issue-scoped review-exchange runtime work.

        Entrypoints use this behavior-level facade instead of reaching
        through ``deps`` to find lifecycle collaborators.
        """
        return cancel_issue_review_exchange(
            issue_number=issue_number,
            reason=reason,
            pair_registry=self.deps.services.pair_registry,
            job_supervisor=self.deps.services.background_job_supervisor,
        )

    def terminate_issue_runtime_for_issue(
        self,
        issue_number: int,
        *,
        reason: str,
    ) -> IssueRuntimeTermination:
        """Terminate all issue-scoped runtime owners at a lifecycle boundary."""
        return terminate_issue_runtime(
            issue_number=issue_number,
            reason=reason,
            pair_registry=self.deps.services.pair_registry,
            job_supervisor=self.deps.services.background_job_supervisor,
            session_manager=self.deps.session_manager,
            active_sessions=self.state.active_sessions,
            publish_recovery=self.deps.publish_recovery,
        )

    @cached_property
    def _cleanup_manager(self) -> CleanupManager:
        return CleanupManager(
            self.config, self.deps.repository_host, self.deps.worktree_manager,
            lambda name: _kill_session(name, self.deps.session_manager, self.deps.events),
            lambda name: _session_exists(name, self.deps.session_manager, self.deps.events),
            lambda issue_number, agent_config: get_worktree_path(self.config, issue_number, agent_config),
            lambda number, session_type="issue": get_session_name(number, session_type),
        )

    @cached_property
    def _completion_handler(self) -> CompletionHandler:
        return self.deps.completion_handler_factory(
            state_machines=self.deps.state_machine_manager,
            active_sessions=lambda: self.state.active_sessions,
        )

    @cached_property
    def _session_launcher(self) -> SessionLauncher:
        return self.deps.session_launcher_factory(
            board_snapshot_provider=StateBoardSnapshotProvider(self.deps.board_snapshot_builder, lambda: self.state),
            session_exists_fn=lambda name: _session_exists(name, self.deps.session_manager, self.deps.events),
            create_session_fn=self._create_session,
            get_issue_machine=self._get_issue_machine,
            get_session_machine=self._get_session_machine,
            get_review_machine=self._get_review_machine,
            refresh_issue_fn=self._refresh_issue,
            dependency_evaluator=self.scheduler.dependency_evaluator,
        )

    @cached_property
    def _plan_applier(self) -> OrchestratorSupport:
        return OrchestratorSupport(
            config=self.config, events=self.deps.events, repository_host=self.deps.repository_host,
            state=self.state, event_context=self._event_context, session_manager=self.deps.session_manager,
            action_applier=self.deps.action_applier, fact_gatherer=self.deps.fact_gatherer,
            planner=self.deps.planner, worktree_manager=self.deps.worktree_manager,
            state_machine_manager=self.deps.state_machine_manager, cleanup_manager=self._cleanup_manager,
            get_review_machine=self._get_review_machine,
            kill_session=lambda name: _kill_session(name, self.deps.session_manager, self.deps.events),
            queue_cache_store=self.deps.queue_cache_store,
            tech_lead_authority=self.deps.tech_lead_authority, run_ownership=self.deps.run_ownership,
        )

    def _get_session_name(self, number: int, session_type: str = "issue") -> str: return get_session_name(number, session_type)
    def _get_worktree_path(self, issue_number: int, agent_config: AgentConfig) -> Path: return get_worktree_path(self.config, issue_number, agent_config)
    def session_launcher_callback(self, session_type: "SessionType", number: int) -> Optional[Session]: return _session_launcher_callback(session_type, number, self._launch_issue_by_number, self._launch_review_by_number, self._launch_retrospective_review_by_number, self._launch_rework_by_number, self._launch_tech_lead_by_number)
    def _launch_issue_by_number(self, n: int) -> Optional[Session]: return _gw_launch_issue_by_number(n, self.state.cached_queue_issues, self.launch_session, lambda: setattr(self.state, 'issues_started_count', self.state.issues_started_count + 1))
    def _launch_review_by_number(self, n: int) -> Optional[Session]: return _ch_launch_review_by_number(n, self.state.pending_reviews, self.launch_review_session)
    def _launch_retrospective_review_by_number(self, n: int) -> Optional[Session]:
        review = next((r for r in self.state.pending_retrospective_reviews if r.issue_number == n), None)
        return self.launch_retrospective_review_session(review) if review else None
    def _launch_rework_by_number(self, n: int) -> Optional[Session]: return _ch_launch_rework_by_number(n, self.state.pending_reworks, self.launch_rework_session)
    def launch_validation_retry_by_number(self, n: int) -> Optional[Session]:
        retry = next((r for r in self.state.pending_validation_retries if r.issue_number == n), None)
        if retry is None:
            return None
        return _launch_validation_retry_session(retry, self.state, self._session_launcher, self.deps.session_restorer, self.deps.pending_work_claims)
    def _launch_tech_lead_by_number(self, n: int) -> Optional[Session]: return _ch_launch_tech_lead_by_number(n, self.state.pending_tech_lead_reviews, self.launch_tech_lead_session)

    def _get_issue_machine(self, issue: Issue) -> Optional[IssueStateMachine]: return _gw_get_issue_machine(issue, self.deps.state_machine_manager)
    def _get_session_machine(self, name: str, n: int, timeout: int) -> Optional[SessionStateMachine]: return _sl_get_session_machine(name, n, timeout, self.deps.state_machine_manager)
    def _get_review_machine(self, pr: int, issue: int) -> Optional[ReviewStateMachine]: return _ch_get_review_machine(pr, issue, self.deps.state_machine_manager)

    def _restore_running_sessions(self, running: list["DiscoveredSession"]) -> None: _restore_running_sessions(running, self.state, self.deps.session_restorer, self.deps.pending_work_claims, self.deps.claim_quarantine)
    def _parse_session_ref(self, session_name: str, operation: str) -> "SessionRef": return _parse_session_ref(session_name, operation, self.deps.events)
    def _create_session(self, name: str, cmd: str, wd: Path, title: str | None = None) -> bool: return _create_session(name, cmd, wd, title, self.deps.session_manager, self.deps.events)
    def _session_exists(self, name: str) -> bool: return _session_exists(name, self.deps.session_manager, self.deps.events)
    def _kill_session(self, name: str) -> None: _kill_session(name, self.deps.session_manager, self.deps.events)
    def _refresh_issue(self, n: int) -> Optional[Issue]: return self._github_workflow.refresh_issue(n)
    def _build_labels(self, *labels: str) -> list[str]: return self._github_workflow.build_labels(*labels)

    def _get_milestone_filter(self) -> str | None: return self.config.filtering.milestone

    @cached_property
    def _issue_fetch_resilience(self) -> IssueFetchResilience:
        """Single owner of the issue-list fetch resilience policy.

        Shared between startup and the steady-state tick so the consecutive
        repo-not-found count (which promotes a *persistent* 404 from transient
        to permanent) spans the whole process lifetime.
        """
        return IssueFetchResilience(self.config.repo)

    @cached_property
    def _startup_manager(self) -> StartupManager:
        return StartupManager(
            self.config, self.deps.events, self.deps.runner, self.deps.repository_host,
            self.deps.action_applier,
            lambda: extract_issue_branches(self.deps.working_copy, self.config.repo_root),
            lambda name: self._session_exists(name),
            lambda r: self._restore_running_sessions(r),
            self.launch_session, self.update_queue_cache,
            self._issue_fetch_resilience,
            self._startup_worktree_reconciler,
            queue_cache_store=self.deps.queue_cache_store,
            label_manager=self.deps.label_manager,
            label_store=self.deps.label_store,
            tech_lead_authority=self.deps.services.tech_lead_authority,
        )

    @cached_property
    def _startup_worktree_reconciler(self) -> StartupWorktreeReconciler:
        return StartupWorktreeReconciler(
            self.config,
            self._cleanup_manager,
            self.deps.worktree_manager,
            WorktreeAuditOwner(self.deps.worktree_manager),
        )

    async def startup(self) -> None:
        self._sweep_orphan_atomic_write_tempfiles()
        try:
            await self._startup_manager.run_startup(self.state)
        except PermanentIssueFetchError as error:
            # The resilience policy guards only the issue-list fetch inside
            # startup. A *transient* fetch failure is already handled there
            # (degrade-and-continue), so startup completes normally. A
            # confirmed-permanent failure (auth, or a repo-not-found that has
            # persisted) surfaces here: record an actionable message and refuse
            # to come up instead of crashing with a raw traceback. Any other
            # repository-host error from a non-fetch startup phase propagates
            # unchanged, exactly as it did before issue-fetch resilience existed.
            self._record_permanent_fetch_failure(error.verdict, context="STARTUP")
            self._shutdown_requested = True

    def _record_permanent_fetch_failure(
        self, verdict: FetchFailureVerdict, *, context: str
    ) -> None:
        """Log an actionable message and persist a clear failure record.

        Used for a confirmed-permanent issue-fetch failure at startup or in the
        run loop so operators get ``repo name + token-scope hint`` rather than a
        raw traceback.
        """
        logger.error("[%s] %s. %s", context, verdict.summary, verdict.suggested_fix)
        try:
            write_startup_failure(
                self.config.repo_root,
                StartupError(
                    phase="runtime",
                    message=verdict.summary,
                    suggested_fix=verdict.suggested_fix,
                ),
            )
        except OSError:
            logger.exception("[%s] Failed to persist permanent-failure record", context)
        self.state.startup_message = f"{verdict.summary}. {verdict.suggested_fix}"

    def _sweep_orphan_atomic_write_tempfiles(self) -> None:
        """Remove partial tempfiles left by a prior ``kill -9`` mid-rename."""
        # Local import keeps the control->infra boundary clean: this module
        # already imports orchestrator_support from control, so one more
        # control-side helper is acceptable.
        from ..control.review_exchange_loop import sweep_orphan_session_tempfiles

        sweep_orphan_session_tempfiles(self.config.repo_root)

    def launch_session(self, issue: Issue, *, tech_lead_scope: "TechLeadLaunchScope | None" = None) -> Optional[Session]:
        return _launch_session(issue, self.state, self._session_launcher, self.deps.session_restorer, tech_lead_scope=tech_lead_scope)
    def handle_session_completion(self, session: Session, status: SessionStatus, *, provider_error_type: "ProviderErrorType | None" = None) -> None: _handle_session_completion(session, status, self.state, self._completion_handler, self.deps.action_applier, self.observer, self.deps.worktree_manager, self._kill_session, self.config, self.deps.session_output, publish_recovery=self.deps.publish_recovery, pending_work_claims=self.deps.pending_work_claims, provider_error_type=provider_error_type)

    def tick(self) -> bool:
        with self.state_lock:
            self._last_tick_time = time.time()
            self.deps.provider_resilience.close_expired()
            self.deps.services.state_health_check()
            # Drain any background-job completions BEFORE the planning phase
            # decides next-step actions. That way a failed review-exchange
            # job is observable to the planner (via recorded failure) in the
            # same tick instead of causing another resubmit.
            supervisor = self.deps.services.background_job_supervisor
            if supervisor is not None:
                supervisor.tick()
            # Reconcile completed off-thread publish retries; success clears
            # failure state, while failures remain retryable.
            self.deps.publish_recovery.drain_completed_retries(self.state)
            self._session_launcher.reconcile_stale_tech_lead_needs_human(
                self.state.active_sessions, discover_markers=not self.state.paused
            )
            self._loop_iteration, cont = _run_tick_impl(
                self._loop_iteration,
                self._event_context,
                self._inflight_stable_ids,
                self.state,
                self.deps.events,
                self._shutdown_requested,
                self._process_active_sessions,
                self._check_health,
                self._run_planning_cycle,
                self._emit_heartbeat_if_needed,
            )
        # Check if we should auto-trigger E2E tests
        self._maybe_trigger_e2e()
        self._check_e2e_completion()
        self._maybe_run_sqlite_backups()
        return cont

    def _maybe_trigger_e2e(self) -> None:
        """Check and trigger E2E tests if conditions are met.

        Triggers when:
        1. E2E is enabled with auto_run_interval_minutes > 0
        2. This instance has executor role (not reader/disabled)
        3. Interval has passed since last run
        4. Main branch HEAD has changed since last tested commit
        """
        # Get instance_id from environment (set by CC for multi-instance)
        instance_id = os.environ.get("INSTANCE_ID")

        triggered = maybe_trigger_e2e(
            config=self.config,
            repo_root=self.config.repo_root,
            orchestrator_id=self.config.orchestrator_id,
            instance_id=instance_id,
            orchestrator_instance_id=self.deps.services.instance_id,
            # Worker-slot start-gate for a first-class E2E run: only when
            # e2e.occupies_session_slot is on does this decide whether the run
            # starts. Same worker-budget accounting the planner uses (tech_lead's
            # reserved slot excluded), so E2E competes for the WORKER budget,
            # not the tech-lead's slot. Computed after the tick's launches, so
            # it sees the slot the planner reserved for a due suite left free.
            worker_slot_free=worker_slot_free(
                self.config, self.state.active_sessions
            ),
        )
        if triggered:
            self.deps.events.publish(TraceEvent(
                EventName.E2E_AUTO_TRIGGERED,
                self._event_context.enrich({}),
            ))

    def _check_e2e_completion(self) -> None:
        """Detect E2E worker completion and broadcast via SSE.

        Called each tick. When the runner reports finished workers,
        publish E2E_COMPLETED or E2E_FAILED so the web dashboard
        updates immediately instead of waiting for the next poll.
        """
        runner = get_e2e_runner_manager()
        finished = runner.cleanup_finished()
        if not finished:
            return
        for orch_id in finished:
            # Determine outcome from last run in DB
            last_run = None
            try:
                from .e2e_db import E2EDB
                db_path = self.config.repo_root / ".issue-orchestrator" / "e2e.db"
                if db_path.exists():
                    db = E2EDB(db_path)
                    last_run = db.latest_run(orch_id)
                    status = last_run.status if last_run else "unknown"
                else:
                    status = "unknown"
            except Exception:
                status = "unknown"

            # Snapshot agent events from the E2E worktree timeline into the
            # base repo timeline so they persist across worktree refreshes.
            if last_run is not None:
                snapshot_agent_events(
                    last_run,
                    repo_root=self.config.repo_root,
                    timeline_store=self.deps.timeline_store,
                )

            event_name = EventName.E2E_COMPLETED if status == "passed" else EventName.E2E_FAILED
            self.deps.events.publish(TraceEvent(
                event_name,
                self._event_context.enrich({
                    "orchestrator_id": orch_id,
                    "status": status,
                }),
            ))

    def _maybe_run_sqlite_backups(self) -> None:
        if not self.config.sqlite_backup.enabled:
            return

        now = time.time()
        interval_seconds = max(60, self.config.sqlite_backup.check_interval_minutes * 60)
        if self._last_backup_check and now - self._last_backup_check < interval_seconds:
            return
        self._last_backup_check = now

        try:
            run_backups_if_due(self.config)
        except Exception as exc:
            logger.warning("[backup] Failed to run SQLite backups: %s", exc)

    def _cleanup_e2e_runner(self) -> None:
        """Clean up E2E runner on orchestrator shutdown.

        Behavior depends on survive_restart config:
        - True (default): Leave the worker and its 'running' row untouched so the
          detached worker can finish the run. Nothing is marked 'interrupted'
          here; that only happens later, in E2EDB.start_run(), if the worker
          turns out to have died (stale 'running' row with a dead PID).
        - False: Stop worker and mark run as canceled
        """
        if not self.config.e2e.enabled:
            return

        orchestrator_id = self.config.orchestrator_id

        manager = get_e2e_runner_manager()
        status = manager.status(orchestrator_id)

        if not status["running"]:
            return

        if self.config.e2e.survive_restart:
            # Let the worker continue and leave its run row alone. If the worker
            # later dies, the next start_run() sees the dead PID and marks that
            # orphaned row interrupted (resumable for the pytest runner).
            logger.info(
                "E2E worker pid=%s continuing (survive_restart=True)",
                status["pid"],
            )
        else:
            # Stop the worker
            logger.info("Stopping E2E worker pid=%s on shutdown", status["pid"])
            manager.stop(orchestrator_id, self.config.repo_root)

    def _check_health(self) -> HealthDecision:
        return cast(HealthDecision, _check_health(self.deps.health_gate, self.state.paused))

    def _process_active_sessions(self) -> None:
        try:
            self._reconcile_running_sessions()
            _process_active_sessions(
                self.state,
                self.observer,
                self.deps.session_controller,
                self._completion_handler,
                self.deps.action_applier,
                self.deps.worktree_manager,
                self._kill_session,
                self.config,
                completion_dispatcher=self.deps.completion_dispatcher,
                provider_resilience=self.deps.provider_resilience,
                publish_recovery=self.deps.publish_recovery,
                pending_work_claims=self.deps.pending_work_claims,
            )
            # Check lease renewals for active sessions
            self._check_lease_renewals()
            _reconcile_tech_lead_ownership(self)  # renew/withdraw run claims
        finally:
            self._last_orphan_reconcile_active_count = len(self.state.active_sessions)

    def _reconcile_running_sessions(self) -> None:
        """Restore live terminal sessions that fell out of active-session state."""
        active_count = len(self.state.active_sessions)
        scan_started = time.monotonic()
        should_scan, trigger = self._orphan_reconcile_trigger(scan_started, active_count)
        if not should_scan:
            self._last_orphan_reconcile_active_count = active_count
            return

        self._last_orphan_reconcile_scan_at = scan_started
        try:
            running = self.deps.runner.discover_running_sessions()
        except Exception:
            self._last_orphan_reconcile_active_count = len(self.state.active_sessions)
            logger.exception("[ORPHAN] Failed to discover running terminal sessions")
            return

        discovery_elapsed_ms = (time.monotonic() - scan_started) * 1000
        self._last_orphan_reconcile_active_count = len(self.state.active_sessions)

        logger.debug(
            "[ORPHAN] Runtime terminal discovery completed "
            "(trigger=%s, active=%d, discovered=%d, discovery_elapsed_ms=%.1f)",
            trigger,
            active_count,
            len(running),
            discovery_elapsed_ms,
        )

        tracked_names = {s.terminal_id for s in self.state.active_sessions}
        discovered = [
            (info, self.deps.session_restorer.canonical_terminal_id(info))
            for info in running
        ]
        untracked = [
            (info, session_name)
            for info, session_name in discovered
            if session_name not in tracked_names
        ]
        if not untracked:
            _recover_unresolved_work(self.state, self.deps.pending_work_claims, self.deps.claim_quarantine)
            return

        logger.warning(
            "[ORPHAN] Found %d running terminal session(s) missing from active tracking "
            "(tracked=%d, discovered=%d, trigger=%s, discovery_elapsed_ms=%.1f): %s",
            len(untracked),
            len(tracked_names),
            len(running),
            trigger,
            discovery_elapsed_ms,
            ", ".join(session_name for _, session_name in untracked),
        )
        restored = _restore_running_sessions(
            [info for info, _ in untracked],
            self.state,
            self.deps.session_restorer,
            self.deps.pending_work_claims,
            self.deps.claim_quarantine,
        )
        self._last_orphan_reconcile_active_count = len(self.state.active_sessions)
        restored_names = {s.terminal_id for s in restored}
        if restored:
            self.deps.events.publish(TraceEvent(
                EventName.SESSION_RESTORED,
                self._event_context.enrich({
                    "source": "runtime_reconcile",
                    "trigger": trigger,
                    "restored_count": len(restored),
                    "session_names": sorted(restored_names),
                    "tracked_before": len(tracked_names),
                    "discovered_count": len(running),
                    "discovery_elapsed_ms": discovery_elapsed_ms,
                }),
            ))
        missing = [
            session_name
            for _, session_name in untracked
            if session_name not in restored_names
        ]
        if missing:
            logger.warning(
                "[ORPHAN] Running terminal session(s) still untracked after restore attempt: %s",
                ", ".join(missing),
            )

    def _orphan_reconcile_trigger(self, now: float, active_count: int) -> tuple[bool, str]:
        """Return whether runtime registry discovery should run this tick."""
        if self._last_orphan_reconcile_scan_at == 0.0:
            return True, "initial"
        if active_count == 0 and self._last_orphan_reconcile_active_count > 0:
            return True, "active_sessions_dropped_to_zero"
        elapsed = now - self._last_orphan_reconcile_scan_at
        if elapsed >= self._ORPHAN_RECONCILE_INTERVAL_SECONDS:
            return True, "interval"
        return False, "throttled"


    def _check_lease_renewals(self) -> None:
        """Check and renew leases for active sessions.

        Handles sessions that have lost their claims by terminating them
        and adding the appropriate blocked label.
        """
        # Check renewals - returns sessions that lost their claim
        lost_sessions = self.deps.lease_renewer.check_renewals(
            list(self.state.active_sessions)
        )

        # Handle claim losses
        for session in lost_sessions:
            logger.warning(
                "[CLAIM] Session for issue #%d lost claim - terminating",
                session.issue.number,
            )

            # Kill the terminal session
            self._kill_session(session.terminal_id)

            # Remove from active sessions
            self.state.active_sessions = [
                s for s in self.state.active_sessions
                if s.terminal_id != session.terminal_id
            ]
            # Drop the session state machine to avoid relaunch conflicts.
            self.deps.state_machine_manager.remove_session_machine(session.terminal_id)

            # Add blocked label (best effort - session lost claim so this may also fail)
            try:
                self.deps.action_applier.labels.add_label(
                    session.issue.number,
                    self.deps.label_manager.blocked_claim_lost,
                )
            except Exception as e:
                logger.warning(
                    "[CLAIM] Failed to add blocked label to issue #%d: %s",
                    session.issue.number,
                    e,
                )

            # Post comment explaining what happened (best effort)
            try:
                comment = (
                    "## Work Cancelled\n\n"
                    "Another orchestrator claimed this issue while work was in progress. "
                    "The session has been terminated to avoid conflicts.\n\n"
                    f"- **Worktree preserved**: `{session.worktree_path}`\n"
                    f"- **Branch**: `{session.branch_name}`\n\n"
                    "To resume work, remove the `blocked:claim-lost` label and re-assign an agent label."
                )
                self.deps.repository_host.add_comment(session.issue.number, comment)
            except Exception as e:
                logger.warning(
                    "[CLAIM] Failed to post claim loss comment to issue #%d: %s",
                    session.issue.number,
                    e,
                )

            # Note: We preserve the worktree so work isn't lost

    def _run_planning_cycle(self) -> None:
        # Capture and clear the state-owned refresh flag before the cycle.
        # If request_refresh() is called during the cycle, it sets the state
        # flag again and the next tick will process that new request.
        with self.state_lock:
            refresh_to_process = self.state.queue_refresh_requested
            self.state.queue_refresh_requested = False
        # Promote any gated expedite follow-ups whose proposed-tech-lead gate an
        # operator has removed onto the worker lane before this tick plans (#6870).
        if (applier := self.deps.action_applier) and applier.expedite_lane:
            applier.expedite_lane.promote_ungated()
        self._last_network_sync, _ = _run_planning_cycle_impl(self.config, self.deps.events, self._event_context, self.state, self.deps.fact_gatherer, self.deps.planner, self.deps.repository_host, self.scheduler, self._github_workflow, self._apply_plan, self._clear_discovered_facts, self._last_network_sync, refresh_to_process, self._inflight_stable_ids, self._issue_fetch_resilience, self.observer, self.deps.claim_manager, queue_cache_store=self.deps.queue_cache_store, io_claimed_label=self.deps.label_manager.io_claimed, open_issue_corpus=self.deps.open_issue_corpus, provider_launch_sampler=self.deps.services.provider_launch_sampler)

    def _clear_discovered_facts(self, tick: "OrchestratorSnapshot") -> None: self._plan_applier.clear_discovered_facts(tick)
    def _emit_heartbeat_if_needed(self) -> None: self._plan_applier.emit_heartbeat_if_needed()

    def _reconcile_orphaned_labels_at_startup(self) -> None:
        """Best-effort orphan-label cleanup before the tick loop starts.

        A transient GitHub blip here (or a permanent auth/not-found after a
        degraded startup) must not crash the loop with a raw traceback — the
        tick reconciles labels later anyway.
        """
        try:
            self.reconcile_orphaned_pr_labels()
        except RepositoryHostError as error:
            logger.warning(
                "[LOOP] Skipping startup orphan-label reconcile — repository host "
                "unavailable: %s", error,
            )

    def _handle_loop_error(self, error: Exception) -> None:
        """Record a tick error and pause after too many consecutive failures."""
        self._loop_error_count += 1
        logger.exception("[LOOP] Error in iteration %d: %s", self._loop_iteration, error)
        self.deps.events.publish(TraceEvent(
            EventName.APPLY_FAILED,
            self._event_context.enrich({
                "step_type": "tick",
                "iteration": self._loop_iteration,
                "error": str(error),
                "error_count": self._loop_error_count,
            }),
        ))
        if self._loop_error_count >= self._loop_error_limit and not self.state.paused:
            self.state.paused = True
            logger.warning(
                "[LOOP] Pausing orchestrator after %d consecutive errors",
                self._loop_error_count,
            )
            self.deps.events.publish(TraceEvent(
                EventName.ORCHESTRATOR_PAUSED,
                self._event_context.enrich({
                    "reason": "loop_error_threshold",
                    "error_count": self._loop_error_count,
                }),
            ))

    async def run_loop(self) -> None:
        logger.info("Starting orchestration loop")

        # Initialize terminal backend (creates tmux session for this orchestrator)
        self.deps.runner.on_orchestrator_startup()

        # Emit orchestrator.started
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_STARTED,
            self._event_context.enrich({
                "mode": "web" if hasattr(self, "_web_mode") else "headless",
            }),
        ))
        with self.state_lock:
            if self.state.paused:
                self.deps.events.publish(TraceEvent(
                    EventName.ORCHESTRATOR_PAUSED,
                    self._event_context.enrich({"reason": "startup"}),
                ))

        self._reconcile_orphaned_labels_at_startup()
        self._last_network_sync, self._last_ui_update, self._loop_iteration = 0.0, time.time(), 0

        while not self._shutdown_requested:
            try:
                # Run tick in thread pool to avoid blocking the event loop
                # during long-running operations like git push with hooks
                should_continue = await asyncio.to_thread(self.tick)
                self._loop_error_count = 0
                if not should_continue:
                    break
            except PermanentIssueFetchError as e:
                # A confirmed-permanent issue fetch failure (auth, or a
                # repo-not-found that has persisted): fail fast with a clear,
                # actionable message and shut down cleanly rather than crashing
                # with a raw traceback or spinning the loop-error budget.
                self._record_permanent_fetch_failure(e.verdict, context="LOOP")
                self._shutdown_requested = True
                break
            except Exception as e:
                self._handle_loop_error(e)
            await asyncio.sleep(10)

        # Shutdown sequence
        active = self.state.active_sessions
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_SHUTDOWN_STARTED,
            self._event_context.enrich({
                "force": False,
                "active_sessions": len(active),
                "sessions": [s.issue.number for s in active],
            }),
        ))
        self._close_external_resources()
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_SHUTDOWN_COMPLETED,
            self._event_context.enrich({
                "force": False,
                "active_sessions_final": len(self.state.active_sessions),
                "iterations": self._loop_iteration,
            }),
        ))

    def _shutdown_runtime_owners(self) -> None:
        """Terminate agent processes before waiting for their worker threads."""
        logger.info("[SHUTDOWN] Terminating agent runtime owners")
        pair_registry = getattr(self.deps, "pair_registry", None)
        if pair_registry is not None:
            pair_registry.shutdown_all(reason="orchestrator-shutdown")
        self.deps.runner.on_orchestrator_shutdown()
        # Closing the agent processes above unblocks review-exchange workers.
        self._drain_background_jobs()
        logger.info("[SHUTDOWN] Agent runtime owners terminated")

    def _close_external_resources(self) -> None:
        """Close process-scoped resources exactly once across all exit paths."""
        with self._external_close_lock:
            if self._external_resources_closed:
                logger.debug("[SHUTDOWN] External resources already closed")
                return
            self._cleanup_e2e_runner()
            self._shutdown_runtime_owners()
            goal_pilot_store = getattr(self.deps, "goal_pilot_store", None)
            if goal_pilot_store is not None:
                close = getattr(goal_pilot_store, "close", None)
                if callable(close):
                    close()
            self._external_resources_closed = True

    def _drain_background_jobs(self, timeout: float = 60.0) -> None:
        """Block shutdown until review-exchange background threads finish.

        Without this, daemon-thread termination on process exit can strand
        ``summary.json`` / ``round-NNN.json`` mid-write. We duck-type on the
        optional ``wait_until_idle`` method so this module stays free of
        ``execution/`` imports; the thread-backed adapter supplies it, and
        a purely synchronous adapter simply doesn't.
        """
        supervisor = self.deps.services.background_job_supervisor
        if supervisor is None:
            return
        wait_until_idle = getattr(supervisor, "wait_until_idle", None)
        if not callable(wait_until_idle):
            return
        logger.info(
            "[SHUTDOWN] Waiting up to %.1fs for background job threads…",
            timeout,
        )
        idle = wait_until_idle(timeout=timeout)
        if not idle:
            logger.warning(
                "[SHUTDOWN] Background jobs still running after timeout; "
                "daemon threads will be terminated on exit"
            )
        # Drain any failures that landed during shutdown so they are
        # visible in logs rather than lost.
        supervisor.tick()

    def request_shutdown(self, force: bool = False) -> None:
        """Request graceful or forced shutdown."""
        self._shutdown_requested = True
        with self.state_lock:
            active = self.state.active_sessions
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_SHUTDOWN_REQUESTED,
            self._event_context.enrich({
                "force": force,
                "active_session_count": len(active),
                "sessions": [s.issue.number for s in active],
            }),
        ))
        if not active:
            logger.info("Shutdown requested - no active sessions, exiting")
            return
        if force:
            logger.info("Force shutdown - killing %d session(s)", len(active))
            for s in active:
                try:
                    self._kill_session(s.terminal_id)
                except Exception as e:
                    logger.warning("Failed to kill session %s: %s", s.terminal_id, e)
            with self.state_lock:
                self.state.active_sessions = []
        else:
            logger.info("Shutdown requested - waiting for %d session(s)", len(active))

    def close(self) -> None:
        """Release external resources for test harnesses and short-lived runs."""
        self._close_external_resources()

    def request_refresh(self, inflight_stable_ids: set[str] | None = None) -> None:
        with self.state_lock:
            self.state.queue_refresh_requested = True
            self._plan_applier.request_refresh(
                inflight_stable_ids,
                self._inflight_stable_ids,
                self._INFLIGHT_TTL_SECONDS,
            )

    def pause(self) -> None:
        with self.state_lock:
            self.state.paused = True
        logger.info("Orchestrator paused")
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_PAUSED,
            self._event_context.enrich({}),
        ))

    def set_start_paused(self) -> None:
        """Set initial paused state and request dashboard read-model hydration.

        Runtime ``pause()`` only stops future execution. Startup-pause also
        needs one read-only refresh because warm cache state may be stale before
        the dashboard first renders.
        """
        with self.state_lock:
            self.state.paused = True
            self.state.queue_refresh_requested = True
        logger.info("Orchestrator marked paused for startup")

    def resume(self) -> None:
        with self.state_lock:
            self.state.paused = False
        logger.info("Orchestrator resumed")
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_RESUMED,
            self._event_context.enrich({}),
        ))

    def get_failure_diagnosis(self, issue_number: int) -> dict:
        """Get failure diagnosis for a session.

        This provides diagnostic info for debugging failed sessions,
        used by the web UI's failure diagnosis endpoint.

        Returns a dict ready for JSON serialization.
        """
        from .session_failure_diagnosis import create_session_failure_diagnosis
        diagnosis = create_session_failure_diagnosis(
            issue_number=issue_number,
            session_history=self.state.session_history,
            active_sessions=self.state.active_sessions,
            config=self.config,
            agents=self.config.agents,
            session_output=self.deps.session_output,
        )
        return diagnosis.to_dict()

    def _pause_issue_for_reconciliation(self, issue_number: int, reason: str) -> None: pause_issue_for_reconciliation(self.deps.events, self.deps.action_applier, self._event_context, issue_number, reason)
    def _apply_plan(self, plan: "Plan") -> None: self._plan_applier.apply_plan(plan, self._pause_issue_for_reconciliation)
    def _fetch_all_issues(self, required_stable_ids: set[str] | None = None) -> list[Issue]: return self._github_workflow.fetch_all_issues(self._get_milestone_filter(), required_stable_ids)
    def update_queue_cache(self) -> None: self._plan_applier.update_queue_cache()
    def _update_dependency_problems(self, dep_blocked: list[tuple["Issue", str]]) -> None: self._github_workflow.update_dependency_problems(self.state, dep_blocked)
    @property
    def _github_workflow(self) -> GitHubWorkflow: return GitHubWorkflow(self.config, self.deps.events, self.deps.repository_host, self.deps.fact_gatherer, self.deps.pr_scanner, self.deps.label_sync, self._event_context, self.deps.label_manager, self.scheduler.dependency_evaluator)
    def launch_review_session(self, review: PendingReview) -> Optional[Session]: return _launch_review_session(review, self.state, self._session_launcher, self.deps.session_restorer, self.deps.pending_work_claims)
    def launch_retrospective_review_session(self, review: PendingRetrospectiveReview) -> Optional[Session]: return _launch_retrospective_review_session(review, self.state, self._session_launcher, self.deps.session_restorer, self.deps.pending_work_claims)
    # #6994 R2 F2/F8: `launch_tech_lead_session` is the ONE entry point — it re-decides
    # subject eligibility, scope exclusivity and cross-engine ownership immediately
    # before starting. `launch_queued_*` is the raw step that authority delegates to.
    # Both tech-lead transitions run under `state_lock` (reentrant, so the tick's
    # own launch nests safely) because admission and launch each read the pending
    # queue and then mutate it, and the dashboard command surface runs on a
    # different thread from the tick.
    def launch_queued_tech_lead_session(self, tech_lead: PendingTechLeadReview) -> Optional[Session]: return _launch_tech_lead_session(tech_lead, self.state, self.config, self._session_launcher, self.deps.session_restorer, self.deps.pending_work_claims)
    def launch_tech_lead_session(self, tech_lead: PendingTechLeadReview) -> Optional[Session]:
        with self.state_lock: return _launch_tech_lead_run(self, tech_lead)
    def ensure_health_review_anchor(self) -> Optional[PendingTechLeadReview]: return _ensure_health_review_anchor(self)
    def request_tech_lead_run(self, request: TechLeadRunRequest) -> TechLeadRunAdmission:
        with self.state_lock: return _admit_tech_lead_run(self, request)
    def process_deferred_cleanups(self) -> None: self.state.pending_cleanups = self._github_workflow.process_deferred_cleanups(self.state.pending_cleanups, self._cleanup_manager)
    def _recover_orphaned_cleanups(self) -> None: self._plan_applier.recover_orphaned_cleanups()
    def scan_needs_code_review_prs(self) -> None: self._github_workflow.scan_needs_code_review_prs(self.state)
    def scan_needs_rework_prs(self) -> None: self._github_workflow.scan_needs_rework_prs(self.state)
    def reconcile_orphaned_pr_labels(self) -> int: return self._github_workflow.reconcile_orphaned_pr_labels(ORCHESTRATOR_PR_MARKER)
    def launch_rework_session(self, rework: PendingRework) -> Optional[Session]: return _launch_rework_session(rework, self.state, self._session_launcher, self.deps.session_restorer, self.deps.pending_work_claims)

async def run_orchestrator(config_path: Optional[Path] = None) -> None:
    from ..entrypoints.bootstrap import build_orchestrator
    config = Config.load(config_path) if config_path else Config.find_and_load()
    orchestrator = build_orchestrator(config)
    signal.signal(signal.SIGINT, lambda s, f: _handle_signal(orchestrator, s, f))
    signal.signal(signal.SIGTERM, lambda s, f: _handle_signal(orchestrator, s, f))
    await orchestrator.startup()
    await orchestrator.run_loop()
