"""Canonical event name catalog.

This module defines all stable event names that can be consumed by UI, tests,
and external systems. Event names follow the format: {domain}.{action_past_tense}

Domains:
- orchestrator: Lifecycle (started, ready, idle, paused, resumed, shutdown_*)
- tick: Per-cycle boundaries (started, completed)
- facts: Discovery phase (gathered)
- plan: Planning phase (computed, noop)
- apply: Action execution (started, step_applied, completed, failed)
- session: Agent session lifecycle (started, observed, completed, failed)
- agent: User-facing agent lifecycle projections (coding_completed)
- issue: Issue state changes (blocked, needs_human)
- dependency: Issue dependencies (blocked, unblocked)
- review: Code review workflow (started, queued, escalated)
- rework: Rework cycle (started)
- tech_lead: Tech Lead workflow (issue_created)
- cleanup: Cleanup operations (completed)
- validation: Local validation hooks (started, completed)
- config: Configuration events (merged)

All events emitted by the orchestrator MUST use EventName constants.
Raw strings are not accepted by TraceEvent.
"""

from enum import Enum


class EventName(str, Enum):
    """Canonical event names.

    Use these constants instead of raw strings to ensure consistency
    and enable IDE support/refactoring.
    """

    # =========================================================================
    # Orchestrator lifecycle
    # =========================================================================
    ORCHESTRATOR_STARTED = "orchestrator.started"
    ORCHESTRATOR_READY = "orchestrator.ready"
    ORCHESTRATOR_IDLE = "orchestrator.idle"
    ORCHESTRATOR_PAUSED = "orchestrator.paused"
    ORCHESTRATOR_RESUMED = "orchestrator.resumed"
    ORCHESTRATOR_SHUTDOWN_REQUESTED = "orchestrator.shutdown_requested"
    ORCHESTRATOR_SHUTDOWN_STARTED = "orchestrator.shutdown_started"
    ORCHESTRATOR_SHUTDOWN_COMPLETED = "orchestrator.shutdown_completed"
    ORCHESTRATOR_HEARTBEAT = "orchestrator.heartbeat"

    # =========================================================================
    # Tick boundaries (main orchestration cycle)
    # =========================================================================
    TICK_STARTED = "tick.started"
    TICK_COMPLETED = "tick.completed"
    # A tick whose wall-clock exceeded the heartbeat budget. Machine-consumable
    # counterpart to the "[LOOP] Tick took ..." log warning: carries the
    # sub-phase breakdown (active-session vs planning seconds) so the UI/timeline
    # can attribute a stall instead of inferring "stalled" from heartbeat age.
    TICK_SLOW = "tick.slow"

    # =========================================================================
    # Facts gathering
    # =========================================================================
    FACTS_GATHERED = "facts.gathered"
    ISSUES_FETCHED = "issues.fetched"

    # =========================================================================
    # Planning phase
    # =========================================================================
    PLAN_COMPUTED = "plan.computed"
    PLAN_NOOP = "plan.noop"

    # =========================================================================
    # Apply phase (action execution)
    # =========================================================================
    APPLY_STARTED = "apply.started"
    APPLY_STEP_APPLIED = "apply.step_applied"
    APPLY_COMPLETED = "apply.completed"
    APPLY_FAILED = "apply.failed"

    # =========================================================================
    # Action execution (per-action granularity)
    # =========================================================================
    ACTION_START = "action.start"
    ACTION_END = "action.end"

    # =========================================================================
    # Session lifecycle
    # =========================================================================
    SESSION_STARTED = "session.started"
    SESSION_OBSERVED = "session.observed"
    SESSION_COMPLETED = "session.completed"
    SESSION_FAILED = "session.failed"
    SESSION_NAME_PARSE_ERROR = "session.name_parse_error"
    SESSION_NO_COMPLETION_RECORD = "session.no_completion_record"
    SESSION_INVALID_COMPLETION_RECORD = "session.invalid_completion_record"
    SESSION_TIMEOUT_RECOVERED = "session.timeout_recovered"
    SESSION_PROCESSING_COMPLETED = "session.processing_completed"
    # Two distinct, deliberately non-timeout provider outcomes. Keeping both
    # out of SESSION_TIMEOUT is the point: a timeout mints a substance
    # failure-investigation, and a provider problem has none.
    #
    # A launch that never happened: the gate asked the provider before spawning
    # and it refused — an expired login, or a CLI that is not installed. Nothing
    # ran, so there is nothing to explain about the work itself.
    SESSION_LAUNCH_BLOCKED_PROVIDER = "session.launch_blocked_provider"
    # A session that *did* launch and is being terminated because its provider
    # is not authenticated. A separate name because the reader's question is
    # different — "what happened to my running session" versus "why did nothing
    # start" — and because exactly one owner announces each (#6999 F5).
    SESSION_PROVIDER_AUTH_TERMINATED = "session.provider_auth_terminated"
    # A restored terminal whose durable pending-work claim could not be read, so
    # the orchestrator does not know which queued request it is holding and
    # refuses to track it (#6999 F6). Paired with a durable needs-human label:
    # this event is what the dashboard reacts to, the label is what survives a
    # restart.
    SESSION_CLAIM_UNREADABLE = "session.claim_unreadable"
    # A live terminal the orchestrator discovered but could not rebuild into a
    # session. Distinct from the name above on purpose (#6999 A1/F2): here the
    # claim reads cleanly, so the work IS known and is deliberately not being
    # re-queued while the terminal runs. Reporting it as "unreadable claim"
    # would tell an operator to work out what the session was doing and re-queue
    # it - the manual duplicate launch that protecting the run prevents.
    SESSION_RUN_UNRESTORABLE = "session.run_unrestorable"
    SESSION_START_FAILED = "session.start_failed"
    SESSION_STOPPED = "session.stopped"
    SESSION_CLEANUP = "session.cleanup"
    SESSION_LAUNCHED = "session.launched"
    SESSION_RESTORED = "session.restored"
    SESSION_SLOW = "session.slow"
    SESSION_TIMEOUT = "session.timeout"
    SESSION_BLOCKED = "session.blocked"
    SESSION_RESUMED = "session.resumed"
    SESSION_NO_OUTPUT = "session.no_output"
    SESSION_VALIDATION_PASSED = "session.validation_passed"
    SESSION_VALIDATION_FAILED = "session.validation_failed"
    SESSION_VALIDATION_RETRY_NEEDED = "session.validation_retry_needed"
    SESSION_ARTIFACT_LOOKUP = "session.artifact_lookup"
    GH_RATE_LIMIT = "gh.rate_limit"
    GH_RATE_LIMIT_WARNING = "gh.rate_limit_warning"
    GH_SEARCH_ITEM_MALFORMED = "gh.search_item_malformed"

    # =========================================================================
    # Agent lifecycle projections
    # =========================================================================
    AGENT_CODING_COMPLETED = "agent.coding_completed"

    # =========================================================================
    # Worktree operations
    # =========================================================================
    WORKTREE_RESET = "worktree.reset"  # Worktree reset to main, discarding local work

    # =========================================================================
    # Completion lookup (coding-done/reviewer-done processing)
    # =========================================================================
    COMPLETION_LOOKUP = "completion.lookup"

    # =========================================================================
    # Issue state
    # =========================================================================
    ISSUE_BLOCKED = "issue.blocked"
    ISSUE_NEEDS_HUMAN = "issue.needs_human"
    ISSUE_DEPENDENCY_BLOCKED = "issue.dependency_blocked"
    ISSUE_CLAIMED = "issue.claimed"
    ISSUE_STARTED = "issue.started"
    ISSUE_UNBLOCKED = "issue.unblocked"
    ISSUE_PR_CREATED = "issue.pr_created"
    ISSUE_PR_REJECTED = "issue.pr_rejected"
    ISSUE_COMPLETED = "issue.completed"
    ISSUE_RELEASED = "issue.released"

    # =========================================================================
    # Dependencies
    # =========================================================================
    DEPENDENCY_BLOCKED = "dependency.blocked"
    DEPENDENCY_UNBLOCKED = "dependency.unblocked"

    # =========================================================================
    # Code review workflow
    # =========================================================================
    REVIEW_STARTED = "review.started"
    REVIEW_QUEUED = "review.queued"
    REVIEW_ESCALATED = "review.escalated"
    REVIEW_SKIPPED = "review.skipped"
    REVIEW_LAUNCHING = "review.launching"
    REVIEW_APPROVED = "review.approved"
    REVIEW_CHANGES_REQUESTED = "review.changes_requested"
    REVIEW_REWORK_STARTED = "review.rework_started"
    REVIEW_REWORK_COMPLETED = "review.rework_completed"
    REVIEW_TECH_LEAD_STARTED = "review.tech_lead_started"
    REVIEW_TECH_LEAD_APPROVED = "review.tech_lead_approved"
    REVIEW_MERGED = "review.merged"
    REVIEW_CLOSED = "review.closed"
    REVIEW_COMMENT_ADDED = "review.comment_added"
    # Merge queue (optional GitHub Merge Queue integration)
    MERGE_QUEUE_ENQUEUED = "merge_queue.enqueued"
    MERGE_QUEUE_FAILED = "merge_queue.failed"
    REVIEW_EXCHANGE_STARTED = "review_exchange.started"
    REVIEW_EXCHANGE_ROUND_STARTED = "review_exchange.round_started"
    REVIEW_EXCHANGE_ROUND_COMPLETED = "review_exchange.round_completed"
    REVIEW_EXCHANGE_COMPLETED = "review_exchange.completed"
    REVIEW_EXCHANGE_FAILED = "review_exchange.failed"
    REVIEW_EXCHANGE_ROLE_PROMPTED = "review_exchange.role_prompted"
    REVIEW_EXCHANGE_ROLE_FEEDBACK = "review_exchange.role_feedback"
    REVIEW_EXCHANGE_ROLE_TIMEOUT = "review_exchange.role_timeout"
    REVIEW_EXCHANGE_CHAPTER_RECORDED = "review_exchange.chapter_recorded"

    # =========================================================================
    # Rework cycle
    # =========================================================================
    REWORK_STARTED = "rework.started"
    REWORK_SKIPPED = "rework.skipped"
    REWORK_LAUNCHING = "rework.launching"
    REWORK_ESCALATING = "rework.escalating"

    # =========================================================================
    # Tech Lead workflow
    # =========================================================================
    TECH_LEAD_ISSUE_CREATED = "tech_lead.issue_created"
    TECH_LEAD_SKIPPED = "tech_lead.skipped"
    TECH_LEAD_LAUNCHING = "tech_lead.launching"
    # Tech Lead decision artifact wiring (ADR-0031); internal trace events only
    TECH_LEAD_ACTION_PROPOSED = "tech_lead.action_proposed"
    # Act-level proposal executed by the orchestrator (#6764), completing
    # the propose/execute pair (mirrors GOAL_PILOT_ACTION_*).
    TECH_LEAD_ACTION_EXECUTED = "tech_lead.action_executed"
    TECH_LEAD_DECISION_REJECTED = "tech_lead.decision_rejected"
    # Tech-lead attention sweep re-injected stuck issues / flagged exhausted
    # ones (#6823); internal trace event only (not a public UI event).
    TECH_LEAD_STUCK_SWEEP = "tech_lead.stuck_sweep"

    # =========================================================================
    # Cleanup operations
    # =========================================================================
    CLEANUP_COMPLETED = "cleanup.completed"

    # =========================================================================
    # Validation (subprocess hooks)
    # =========================================================================
    VALIDATION_STARTED = "validation.started"
    VALIDATION_COMPLETED = "validation.completed"

    # =========================================================================
    # Provider resilience
    # =========================================================================
    # Fleet-scoped: one circuit, no issue identity. Useful for fleet
    # observability, but they can never reach an issue timeline (the timeline
    # writer is keyed by ``issue_number``).
    PROVIDER_TRANSIENT_ERROR = "provider.transient_error"
    # A typed AUTH outcome reached the circuit owner: the provider's own
    # credential probe says it is not logged in. Fleet-scoped like its
    # neighbours — it names a circuit, not an issue. Which issue-scoped event
    # follows depends on where the work was when it hit the outage:
    #   * circuit still closed (sub-threshold): the launch gate refuses the
    #     launch => SESSION_LAUNCH_BLOCKED_PROVIDER
    #   * circuit open: planning parks the work up front => PROVIDER_ISSUE_BLOCKED
    #   * a session already running: it is terminated
    #     => SESSION_PROVIDER_AUTH_TERMINATED
    PROVIDER_AUTH_FAILED = "provider.auth_failed"
    PROVIDER_OUTAGE_ENTERED = "provider.outage_entered"
    PROVIDER_RETRY_SCHEDULED = "provider.retry_scheduled"
    PROVIDER_RETRY_ATTEMPTED = "provider.retry_attempted"
    PROVIDER_OUTAGE_EXITED = "provider.outage_exited"
    # Issue-scoped provider-impact transitions (issue #5980). Emitted by the
    # provider-impact owner command *after* the blocked-label transition has
    # been applied, so the outage stays legible in an issue's history long
    # after the circuit closed and the label was shed.
    PROVIDER_ISSUE_BLOCKED = "provider.issue_blocked"
    PROVIDER_ISSUE_UNBLOCKED = "provider.issue_unblocked"

    # =========================================================================
    # Configuration
    # =========================================================================
    CONFIG_MERGED = "config.merged"

    # =========================================================================
    # Goal Pilot
    # =========================================================================
    GOAL_PILOT_CREATED = "goal_pilot.created"
    GOAL_PILOT_UPDATED = "goal_pilot.updated"
    GOAL_PILOT_ACTION_PROPOSED = "goal_pilot.action_proposed"
    GOAL_PILOT_ACTION_EXECUTED = "goal_pilot.action_executed"
    GOAL_PILOT_ACTION_FAILED = "goal_pilot.action_failed"
    GOAL_PILOT_COMPLETED = "goal_pilot.completed"

    # =========================================================================
    # Reconciliation (label state verification)
    # =========================================================================
    RECONCILIATION_CHECKED = "reconciliation.checked"
    RECONCILIATION_WARNING = "reconciliation.warning"
    RECONCILIATION_REQUIRED = "reconciliation.required"  # Drift detected, action blocked
    ISSUE_PAUSED_RECONCILE = "issue.paused_reconcile"  # Issue paused due to drift
    HISTORY_RECONCILED = "history.reconciled"

    # =========================================================================
    # Queue projection (UI-specific, consider moving to projection layer)
    # =========================================================================
    QUEUE_CHANGED = "queue.changed"

    # =========================================================================
    # Labels
    # =========================================================================
    LABELS_SYNCED = "labels.synced"
    LABEL_MUTATION_SUMMARY = "labels.mutation_summary"
    ISSUE_LABELS_CHANGED = "issue.labels_changed"
    PR_VIEW_CHANGED = "pr.view_changed"

    # =========================================================================
    # Transitions (state machine)
    # =========================================================================
    TRANSITION_APPLIED = "transition.applied"
    TRANSITION_REJECTED = "transition.rejected"

    # =========================================================================
    # PR Scanner
    # =========================================================================
    SCANNER_REVIEWS_FOUND = "scanner.reviews_found"
    SCANNER_REWORKS_FOUND = "scanner.reworks_found"

    # =========================================================================
    # Dependencies evaluation
    # =========================================================================
    DEPENDENCIES_EVALUATED = "dependencies.evaluated"

    # =========================================================================
    # Stale in-progress detection and retry
    # =========================================================================
    STALE_IN_PROGRESS_DETECTED = "stale.in_progress_detected"
    STALE_IN_PROGRESS_CLEARED = "stale.in_progress_cleared"
    RETRY_TRIGGERED = "issue.retry_triggered"
    RETRY_NOOP_SESSION_RUNNING = "issue.retry_noop"
    STARTUP_STALE_CLEARED = "orchestrator.startup_stale_cleared"
    PERSISTENT_STALE_DETECTED = "stale.persistent_detected"

    # =========================================================================
    # Observation (session monitoring)
    # =========================================================================
    OBSERVATION_COMPLETION_DETECTED = "observation.completion_detected"
    OBSERVATION_RESULT = "observation.result"

    # =========================================================================
    # Publish Jobs (async completion processing)
    # =========================================================================
    PUBLISH_JOB_QUEUED = "publish_job.queued"  # Job added to queue
    PUBLISH_JOB_STARTED = "publish_job.started"  # Worker started executing
    PUBLISH_JOB_PUSH_STARTED = "publish_job.push_started"  # Git push started
    PUBLISH_JOB_PUSH_COMPLETED = "publish_job.push_completed"  # Git push finished
    PUBLISH_JOB_PR_CREATED = "publish_job.pr_created"  # PR created
    PUBLISH_JOB_VALIDATION_STARTED = "publish_job.validation_started"  # Validation started
    PUBLISH_JOB_VALIDATION_COMPLETED = "publish_job.validation_completed"  # Validation finished
    PUBLISH_JOB_SUCCEEDED = "publish_job.succeeded"  # Job completed successfully
    PUBLISH_JOB_FAILED = "publish_job.failed"  # Job failed

    # Synchronous completion processor publish outcome (push or PR create).
    # Emitted with ``stage`` = "push_branch" | "create_pr" and a human-readable
    # ``error`` string so the timeline can surface the actual cause of a
    # "Push or PR creation failed" card without the operator having to crack
    # open failure-diagnostic-*.json.
    PUBLISH_FAILED = "publish.failed"

    # =========================================================================
    # Resolver (identity resolution)
    # =========================================================================
    RESOLVER_DUPLICATE_EXTERNAL_ID = "resolver.duplicate_external_id"

    # =========================================================================
    # E2E Test Runner
    # Currently emitted: E2E_STARTED, E2E_STOPPED (from control_api.py)
    # Not yet emitted (reserved for future worker integration): E2E_PROGRESS, E2E_COMPLETED, E2E_FAILED
    # =========================================================================
    E2E_AUTO_TRIGGERED = "e2e.auto_triggered"
    E2E_STARTED = "e2e.started"
    E2E_PROGRESS = "e2e.progress"  # Periodic progress update during run (not yet emitted)
    E2E_COMPLETED = "e2e.completed"  # Run completed successfully (not yet emitted)
    E2E_FAILED = "e2e.failed"  # Run failed (not yet emitted)
    E2E_STOPPED = "e2e.stopped"

    # =========================================================================
    # Claim/Lease lifecycle (multi-orchestrator coordination)
    # =========================================================================
    CLAIM_ATTEMPTED = "claim.attempted"  # Orchestrator attempting to claim
    CLAIM_ACQUIRED = "claim.acquired"  # Successfully acquired claim
    CLAIM_CONTESTED = "claim.contested"  # Multiple claimants detected
    CLAIM_CONVERGED = "claim.converged"  # Convergence completed, winner determined
    CLAIM_LOST = "claim.lost"  # Lost claim during session
    CLAIM_LOST_BEFORE_WRITE = "claim.lost_before_write"  # Lost claim before mutation
    CLAIM_EXPIRED = "claim.expired"  # Claim expired without renewal
    CLAIM_RENEWED = "claim.renewed"  # Successfully renewed lease
    CLAIM_RELEASED = "claim.released"  # Voluntarily released claim
    CLAIM_STALE_DETECTED = "claim.stale_detected"  # Found orphaned claim

    def __str__(self) -> str:
        """Return the event name string for use in TraceEvent."""
        return self.value


class PublicEventName(str, Enum):
    """Events that surface in the user or ops timeline view.

    A type-level subset of `EventName`: each `PublicEventName` value
    matches the string value of the corresponding `EventName` and
    represents an event that fans out to at least one user-or-ops
    `ViewEvent` per `events/view_registry.py`.

    Use `PublicEventName` as the key type for the projection contract:

      - `EVENT_SPEC: dict[PublicEventName, EventSpec]` — only public
        events have a defined timeline projection.
      - Golden test fixtures — only assert on public events.
      - `project_timeline()` consumers that should not depend on
        private/debug-only events.

    Emit-side code continues to use `EventName.X`. The wire-protocol
    contract is unchanged; this enum exists for read-side type safety.

    Membership is enforced by `tests/unit/test_event_spec.py`, which
    asserts these values match the user/ops set computed from
    `VIEW_REGISTRY` — making the registry the single source of truth
    and this enum its codification.
    """

    SESSION_STARTED = "session.started"
    SESSION_COMPLETED = "session.completed"
    SESSION_FAILED = "session.failed"
    SESSION_TIMEOUT = "session.timeout"
    SESSION_BLOCKED = "session.blocked"
    SESSION_LAUNCH_BLOCKED_PROVIDER = "session.launch_blocked_provider"
    SESSION_PROVIDER_AUTH_TERMINATED = "session.provider_auth_terminated"
    SESSION_CLAIM_UNREADABLE = "session.claim_unreadable"
    SESSION_RUN_UNRESTORABLE = "session.run_unrestorable"
    SESSION_NO_COMPLETION_RECORD = "session.no_completion_record"
    SESSION_INVALID_COMPLETION_RECORD = "session.invalid_completion_record"
    SESSION_PROCESSING_COMPLETED = "session.processing_completed"
    SESSION_VALIDATION_PASSED = "session.validation_passed"
    SESSION_VALIDATION_FAILED = "session.validation_failed"
    SESSION_VALIDATION_RETRY_NEEDED = "session.validation_retry_needed"

    OBSERVATION_COMPLETION_DETECTED = "observation.completion_detected"
    COMPLETION_LOOKUP = "completion.lookup"
    WORKTREE_RESET = "worktree.reset"

    REVIEW_STARTED = "review.started"
    REVIEW_APPROVED = "review.approved"
    REVIEW_CHANGES_REQUESTED = "review.changes_requested"
    REVIEW_ESCALATED = "review.escalated"
    REVIEW_MERGED = "review.merged"
    REVIEW_COMMENT_ADDED = "review.comment_added"
    REVIEW_REWORK_STARTED = "review.rework_started"
    REVIEW_REWORK_COMPLETED = "review.rework_completed"

    REVIEW_EXCHANGE_STARTED = "review_exchange.started"
    REVIEW_EXCHANGE_ROUND_STARTED = "review_exchange.round_started"
    REVIEW_EXCHANGE_ROUND_COMPLETED = "review_exchange.round_completed"
    REVIEW_EXCHANGE_COMPLETED = "review_exchange.completed"
    REVIEW_EXCHANGE_FAILED = "review_exchange.failed"
    REVIEW_EXCHANGE_ROLE_PROMPTED = "review_exchange.role_prompted"
    REVIEW_EXCHANGE_ROLE_FEEDBACK = "review_exchange.role_feedback"
    REVIEW_EXCHANGE_ROLE_TIMEOUT = "review_exchange.role_timeout"
    REVIEW_EXCHANGE_CHAPTER_RECORDED = "review_exchange.chapter_recorded"

    REWORK_STARTED = "rework.started"
    REWORK_LAUNCHING = "rework.launching"

    TECH_LEAD_LAUNCHING = "tech_lead.launching"

    VALIDATION_STARTED = "validation.started"
    VALIDATION_COMPLETED = "validation.completed"

    ISSUE_BLOCKED = "issue.blocked"
    ISSUE_NEEDS_HUMAN = "issue.needs_human"
    ISSUE_COMPLETED = "issue.completed"
    ISSUE_UNBLOCKED = "issue.unblocked"
    ISSUE_PR_CREATED = "issue.pr_created"

    PROVIDER_ISSUE_BLOCKED = "provider.issue_blocked"
    PROVIDER_ISSUE_UNBLOCKED = "provider.issue_unblocked"

    PUBLISH_FAILED = "publish.failed"

    def __str__(self) -> str:
        return self.value


# Schema version for event payload evolution
EVENT_SCHEMA_VERSION = 1
