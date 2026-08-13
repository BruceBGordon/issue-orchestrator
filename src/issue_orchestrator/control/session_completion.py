"""Session completion flow coordination.

This module owns the post-launch session lifecycle: interpreting terminal
outcomes, applying completion policy actions, recording cleanup work, releasing
claims, and preserving failure diagnostics. Session launch setup stays in
``session_launcher``.
"""

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..domain.issue_key import GitHubIssueKey, IssueKey
from ..domain.models import (
    PendingRework,
    Session,
    SessionStatus,
    is_retrospective_review_session,
    resolve_retrospective_coder_agent,
)
from ..domain.session_key import TaskKind
from ..events import EventName
from ..infra.config import Config
from ..ports import EventSink
from ..ports.event_sink import make_trace_event
from ..ports.provider_resilience import ProviderErrorType
from ..ports.pending_work_claim_store import PendingWorkClaimStore
from ..ports.session_output import SessionOutput
from ..ports.worktree_manager import WorktreeManager
from .active_sessions import has_active_terminal
from .completion_cleanup_state import CompletionCleanupStateOwner
from .completion_dispatcher import (
    CompletedDecision,
    CompletionDispatcher,
    SynchronousCompletionDispatcher,
)
from .session_completion_diagnostics import run_session_analysis, surface_failure_context
from .session_run_resolution import resolve_session_run_dir
from .transition_log import log_transition
from .tech_lead_reaction import record_completed_session_problem

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..observation.observation import SessionObservationResult
    from ..observation.observer import SessionObserver
    from ..ports.claim_manager import ClaimManager
    from .action_applier import ActionApplier
    from .completion_handler import CompletionHandler
    from .provider_resilience import ProviderResilienceManager
    from .publish_recovery import PublishRecoveryService
    from .session_controller import SessionController, SessionDecision
    from .tech_lead_reset_retry import RequiredActLevelOutcome

logger = logging.getLogger(__name__)


def _validation_issue_key(session: Session, config: Config) -> IssueKey | None:
    repo = session.issue.repo or config.repo
    if repo:
        return GitHubIssueKey(repo=repo, external_id=str(session.issue.number))
    if config.is_validation_enabled():
        logger.info(
            "[COMPLETION] Validation attempt identity unavailable: repo is unset "
            "for issue %s",
            session.issue.number,
        )
    return None


_RUNTIME_TERMINAL_STATUSES = frozenset({
    SessionStatus.COMPLETED,
    SessionStatus.BLOCKED,
    SessionStatus.NEEDS_HUMAN,
    SessionStatus.FAILED,
    SessionStatus.TIMED_OUT,
    SessionStatus.VALIDATION_FAILED,
})

_SESSION_ALREADY_GONE_MARKERS = (
    "not found",
    "no such session",
    "does not exist",
    "already stopped",
)


def _is_session_already_gone_error(exc: Exception) -> bool:
    """Return true when stop failed because the runtime session already ended."""
    if isinstance(exc, (FileNotFoundError, LookupError)):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _SESSION_ALREADY_GONE_MARKERS)


def _terminate_finished_session(
    session: Session,
    status: SessionStatus,
    kill_session_fn: Callable[[str], None],
) -> None:
    """Best-effort runtime terminalization for sessions with final outcomes."""
    if status not in _RUNTIME_TERMINAL_STATUSES:
        return
    try:
        kill_session_fn(session.terminal_id)
    except Exception as exc:
        if _is_session_already_gone_error(exc):
            logger.debug(
                "[COMPLETION] Finished session %s was already stopped (status=%s): %s",
                session.terminal_id,
                status.value,
                exc,
            )
            return
        logger.warning(
            "[COMPLETION] Failed to stop finished session %s (status=%s): %s",
            session.terminal_id,
            status.value,
            exc,
        )


def _queue_rework_after_retrospective_changes(
    *,
    session: Session,
    status: SessionStatus,
    state: "OrchestratorState",
    completion_detail: dict[str, Any] | None,
) -> None:
    """Queue coder rework when a retrospective review requests changes.

    Label/state effects live in CompletionHandler; this owns only the rework queue.
    """

    if session.key.task != TaskKind.RETROSPECTIVE_REVIEW:
        return
    if status != SessionStatus.COMPLETED:
        return
    detail = completion_detail or {}
    if detail.get("outcome") != "review_changes_requested":
        return
    issue_number = session.issue.number

    coder_agent = resolve_retrospective_coder_agent(session.issue, session.agent_label)
    if not coder_agent:
        logger.warning(
            "[retrospective-review] Cannot queue rework for issue #%d: missing coder agent label",
            issue_number,
        )
        return
    queued = state.queue_pending_rework(
        PendingRework(
            issue_key=session.key.issue,
            agent_type=coder_agent,
            rework_cycle=1,
            issue_number=issue_number,
            pr_number=session.pr_number,
            source="retrospective_review",
            feedback=str(detail.get("review_issues") or detail.get("review_summary") or ""),
        )
    )
    if not queued:
        return
    logger.info(
        "[retrospective-review] Queued coder rework for issue #%d after changes requested",
        issue_number,
    )


def _failure_artifact_hints(
    worktree_path: Path,
    run_dir: Path,
    diagnostic_path: str | None,
    claude_log_path: Path | None,
) -> tuple[str, ...]:
    """On-disk artifacts a failure investigation should start from (#6762).

    The discovery seam knows both the failure diagnostic and the run directory,
    so hints are gathered here and carried on :class:`DiscoveredFailure` through
    plan -> queue -> board. Only EXISTING paths are included (a hint to a file
    never written is worse than none). Provenance is preserved (#6771 r3):
    ``write_failure_diagnostic`` reports a WORKTREE-RELATIVE diagnostic while
    ``SessionOutput.write_diagnostic`` reports an absolute one, so relative
    candidates are resolved against the worktree before the existence check;
    hints are stored ABSOLUTE (the investigation launches ticks later from a
    different cwd, where a relative hint would be unreadable).
    """
    from ..domain.run_manifest import MANIFEST_FILENAME
    from ..infra.terminal_recording import TERMINAL_RECORDING_FILENAME
    from .session_analyzer import ANALYSIS_FILENAME

    candidates: list[Path] = []
    if diagnostic_path:
        candidates.append(Path(diagnostic_path))
    if claude_log_path is not None:
        candidates.append(claude_log_path)
    candidates.extend(
        run_dir / name
        for name in (MANIFEST_FILENAME, ANALYSIS_FILENAME, TERMINAL_RECORDING_FILENAME)
    )
    resolved = (
        path if path.is_absolute() else worktree_path / path for path in candidates
    )
    return tuple(str(path) for path in resolved if path.exists())


def _surface_required_act_level_failure(
    action_applier: "ActionApplier",
    config: Config,
    session: Session,
    outcome: "RequiredActLevelOutcome",
) -> None:
    """Apply the durable operator surface for a failed mandated act-level action.

    Routes a failed decision-mandated reset to a needs-human label + comment via
    the existing action owners so the FAILED terminal is not merely in-memory
    (#6764 F2). The builder returns [] for a committed/genuine-failure outcome, so
    this applies nothing on those paths.
    """
    from .tech_lead_reset_retry import build_required_act_level_failure_actions

    actions = build_required_act_level_failure_actions(
        issue_number=session.issue.number,
        needs_human_label=config.get_label_needs_human(),
        outcome=outcome,
        session_id=session.terminal_id,
        runtime_minutes=session.runtime_minutes,
    )
    if actions:
        action_applier.apply_all(actions)


def handle_session_completion(  # noqa: C901, PLR0912 - handles validation, actions, observer cleanup, claims, and history
    session: Session,
    status: SessionStatus,
    state: "OrchestratorState",
    completion_handler: "CompletionHandler",
    action_applier: "ActionApplier",
    observer: "SessionObserver",
    worktree_manager: Optional[WorktreeManager],
    kill_session_fn: Callable[[str], None],
    config: Config,
    session_output: SessionOutput,
    # Required, not defaulted: settling the pending-work claim is part of what
    # completing a session MEANS, and a default let one completion path drift
    # from the other (#6999 F9/A4).
    pending_work_claims: PendingWorkClaimStore,
    pr_url_hint: Optional[str] = None,
    processing_errors: Optional[list[str]] = None,
    diagnostic_path: Optional[str] = None,
    validation_error: Optional[str] = None,
    validation_error_file: Optional[str] = None,
    review_exchange_completed: bool = False,
    review_exchange_halted: bool = False,
    blocked_label: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    completion_detail: Optional[dict[str, Any]] = None,
    claim_manager: Optional["ClaimManager"] = None,
    events: Optional[EventSink] = None,
    publish_recovery: Optional["PublishRecoveryService"] = None,
    # The typed provider verdict this session ended on (#6999). Carried rather
    # than re-derived so the reaction owner can decline to mint a substance
    # investigation for a credential outage.
    provider_error_type: ProviderErrorType | None = None,
) -> None:
    """Handle session completion - moved from Orchestrator per method table.

    Complexity is inherent - this processes validation retries, completion,
    actions, observer cleanup, claim release, history, and failure tracking as
    sequential steps sharing the session context. ``process_completion`` defers
    terminalization; the ONE effective outcome is finalized post-apply on every
    apply outcome (return or raise), then any apply error is re-raised (#6777).
    """
    from ..domain.models import DiscoveredReview, PendingValidationRetry

    name = session.terminal_id
    entity = (
        "retrospective-review"
        if is_retrospective_review_session(session)
        else "review"
        if name.startswith("review-")
        else "rework"
        if name.startswith("rework-")
        else "issue"
    )
    log_transition(entity, session.issue.number, "ACTIVE", status.value.upper(), f"runtime={session.runtime_minutes}min")

    # Remove by session name, NOT issue number - multiple sessions can share an issue number
    state.active_sessions = [s for s in state.active_sessions if s.terminal_id != session.terminal_id]

    # Settle the claim this session took off a pending queue at launch (#6999
    # F2/A1). One typed outcome for every terminal path: a session stopped by
    # its provider never got to attempt the work, so its request goes back to
    # its own queue with its full context and its full retry budget instead of
    # being spent. Every other status - including an agent-reported BLOCKED -
    # consumes it exactly as before. Issue sessions hold no claim (they are
    # claimed by label) and settle to a no-op.
    from .in_flight_work import InFlightWorkLedger, SettlementOutcome

    InFlightWorkLedger(state, pending_work_claims).settle(
        session,
        SettlementOutcome.for_provider_error(provider_error_type),
    )

    # Handle validation retry - queue for re-launch instead of normal completion
    if status == SessionStatus.NEEDS_VALIDATION_RETRY:
        next_retry_count = session.validation_retry_count + 1
        logger.info(
            "[COMPLETION] Issue #%d needs validation retry (attempt %d), queueing for re-launch",
            session.issue.number,
            next_retry_count,
        )
        completion_handler.mark_session_retry(session, reason="validation_retry")
        pending_retry = PendingValidationRetry(
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            agent_label=session.agent_label or "",
            worktree_path=str(session.worktree_path),
            branch_name=session.branch_name,
            original_prompt=session.original_prompt,
            validation_error=validation_error or "",
            validation_error_file=validation_error_file,
            retry_count=next_retry_count,
            source_task=session.key.task,
            validation_cmd=config.validation.quick.cmd,
        )
        state.pending_validation_retries = [
            retry for retry in state.pending_validation_retries
            if retry.issue_number != session.issue.number
        ]
        state.pending_validation_retries.append(pending_retry)
        # Kill the terminal session but don't cleanup worktree (agent will continue there)
        kill_session_fn(session.terminal_id)
        return  # Skip normal completion processing

    # Process completion (policy decisions). The terminal trace event, the cached
    # state-machine transition, and the completed_today gate ALL defer to the
    # EFFECTIVE post-apply outcome below (#6777): process_completion terminalizes
    # nothing here, so a mandated act-level action that fails — or an apply that
    # RAISES — cannot leave a false SESSION_COMPLETED or a completed cached machine.
    try:
        result = completion_handler.process_completion(
            session, status, pr_url_hint=pr_url_hint,
            processing_errors=processing_errors,
            diagnostic_path=diagnostic_path,
            review_exchange_completed=review_exchange_completed,
            review_exchange_halted=review_exchange_halted,
            blocked_label=blocked_label,
            blocked_reason=blocked_reason,
            completion_detail=completion_detail,
            finalize_terminal=False,
            # Routes a provider-caused block to the provider-impact owner
            # rather than generic blocked handling (#6999 F5).
            provider_error_type=provider_error_type,
        )
    finally:
        # Completion state is orchestrator-authoritative. Runtime session
        # registry cleanup is separate from the optional UI tab/worktree cleanup
        # action, otherwise a finished agent can be rediscovered as running.
        _terminate_finished_session(session, status, kill_session_fn)

    # Persist durable retry locators BEFORE applying the publish-failed labels
    # below, so a crash between the two can't leave GitHub marked publish-failed
    # with no locators (Retry Publish unavailable). No-op for non-publish failures.
    if publish_recovery is not None:
        publish_recovery.record_publish_failure(
            session,
            processing_errors,
            review_exchange_completed=review_exchange_completed,
            review_exchange_halted=review_exchange_halted,
        )

    run_dir = resolve_session_run_dir(session_output, session)
    claude_log_path = session_output.attach_claude_log(run_dir)
    run_session_analysis(run_dir)

    # Apply completion actions through the authority-with-effects owner (#6779 R13,
    # #6777): the mandated act-level action GATES its success-only siblings — they
    # do not commit unless it commits — and a raised apply past the runtime-kill
    # boundary (ReconciliationRequired / ClaimLostError / adapter fault) is captured
    # so terminal finalization still runs, then re-raised. Neither a failing
    # mandated reset nor an aborted apply may leave a success effect committed or
    # the cached machine RUNNING with the runtime dead.
    from .tech_lead_reset_retry import (
        apply_completion_actions_gated,
        effective_terminal_status,
        evaluate_required_act_level_outcome,
        finalize_required_act_level_history,
        required_act_level_outcome_after_apply,
    )
    applied_results, apply_error = apply_completion_actions_gated(
        action_applier, result.actions, issue_number=session.issue.number
    )

    # The required-act-level outcome is the single authoritative terminal-status
    # policy for the whole post-apply phase (ADR-0031 §2, #6764 F2, #6777): a
    # mandated reset that FAILED — or an apply that RAISED — makes the EFFECTIVE
    # status FAILED regardless of the agent's intent, so every consumer below
    # routes through `effective_status` and an aborted apply is never a success.
    required_act_outcome = required_act_level_outcome_after_apply(
        applied_results, apply_error
    )
    effective_status = effective_terminal_status(status, required_act_outcome)

    # Finalize BOTH terminal-outcome commits — the ONE trace event and the cached
    # SessionStateMachine transition — from the SAME effective outcome (#6777), on
    # the return AND the raised-apply path: a failed/aborted apply ends the machine
    # FAILED and emits a single SESSION_FAILED, never a false COMPLETED.
    completion_handler.finalize_terminal_outcome(
        session,
        effective_terminal_status(result.history_status, required_act_outcome),
        result.pr_url,
        result.pr_number,
        blocked_reason=blocked_reason,
        completion_detail=completion_detail,
    )
    # completed_today is a success gate: record only on a clean EFFECTIVE completion.
    if effective_status == SessionStatus.COMPLETED:
        state.completed_today.append(session.issue.number)

    _queue_rework_after_retrospective_changes(
        session=session,
        status=status,
        state=state,
        completion_detail=completion_detail,
    )

    # Observer handles session-level cleanup (kill sessions, close tabs)
    observer.handle_completion(session, effective_status)

    # Release claim if session had one
    if claim_manager and session.lease_id:
        try:
            claim_manager.release_claim(session.issue.number, session.lease_id)
            logger.info(
                "[COMPLETION] Released claim for issue #%d: lease_id=%s",
                session.issue.number,
                session.lease_id,
            )
            if events:
                events.publish(make_trace_event(
                    EventName.CLAIM_RELEASED,
                    {
                        "issue_number": session.issue.number,
                        "lease_id": session.lease_id,
                        "status": effective_status.value,
                    },
                ))
        except Exception as e:
            logger.warning(
                "[COMPLETION] Failed to release claim for issue #%d: %s",
                session.issue.number,
                e,
            )

    # Terminalize through the required-act-level outcome boundary (ADR-0031 §2,
    # #6764 F2, #6777): a mandated reset that failed — or an apply that raised —
    # routes the whole completion to a FAILED terminal record, never masked by the
    # agent's "completed" intent. A committed/downgraded outcome is untouched.
    state.session_history.append(
        finalize_required_act_level_history(
            result.history_entry,
            required_act_outcome,
        )
    )
    CompletionCleanupStateOwner(state).record(result.cleanup, session, effective_status)

    if result.should_queue_review and result.pr_url and result.pr_number:
        state.discovered_reviews.append(DiscoveredReview(
            session.issue.number, result.pr_number, result.pr_url, session.branch_name,
            agent_label=session.agent_label,
            issue_key=session.issue.key.stable_id(),
        ))
    record_completed_session_problem(
        status=effective_status,
        session=session,
        tech_lead_agent=config.tech_lead_review_agent,
        blocking_label=blocked_label or "",
        artifact_hints=lambda: _failure_artifact_hints(
            session.worktree_path, run_dir, diagnostic_path, claude_log_path
        ),
        record=state.record_discovered_failure,
        provider_error_type=provider_error_type,
    )
    if effective_status in (SessionStatus.FAILED, SessionStatus.TIMED_OUT):
        # Track failed issues to prevent immediate retry (cleared on cache refresh)
        state.failed_this_cycle.add(session.issue.number)
        logger.info(
            "[COMPLETION] Issue #%d added to failed_this_cycle (prevents retry until cache refresh)",
            session.issue.number,
        )

        # Surface AI session logs for debugging
        surface_failure_context(session, effective_status)

        # Crash-safe operator surface for a mandated reset that FAILED in-band —
        # driven by the REAL applied verdict, not the effective one. On the
        # raised-apply path the verdict is committed (no results), so this posts
        # nothing: a second GitHub write right after a reconciliation/claim raise
        # would re-fail and mask the re-raise (the terminal is already FAILED).
        _surface_required_act_level_failure(
            action_applier, config, session,
            evaluate_required_act_level_outcome(applied_results),
        )

    # A successful reset_retry made its target issue retryable mid-apply, but the
    # history append above re-keys it "already ran" and would re-block the very
    # relaunch the reset exists to trigger. Re-clear the planner/queue gates last,
    # via their owner (empty on the raised-apply path, so a no-op). (#6764)
    from .retry_history_state import RetryHistoryState
    from .tech_lead_reset_retry import preserve_reset_retry_eligibility

    cleared = preserve_reset_retry_eligibility(
        applied_results,
        make_retryable=RetryHistoryState(state).make_retryable,
    )
    if cleared:
        logger.info(
            "[COMPLETION] Preserved reset_retry eligibility after history append: %s",
            cleared,
        )

    # Re-raise the captured apply error ONLY now that terminal finalization has
    # committed (#6777): the reconciliation/claim machinery gets the exact signal
    # the pre-fix path raised, but never before the terminal is consistent — the
    # completion is finalized FAILED, not lost in limbo.
    if apply_error is not None:
        raise apply_error


def process_active_sessions(
    state: "OrchestratorState",
    observer: "SessionObserver",
    session_controller: "SessionController",
    completion_handler: "CompletionHandler",
    action_applier: "ActionApplier",
    worktree_manager: Optional[WorktreeManager],
    kill_session_fn: Callable[[str], None],
    config: Config,
    pending_work_claims: PendingWorkClaimStore,
    completion_dispatcher: "CompletionDispatcher | None" = None,
    provider_resilience: "ProviderResilienceManager | None" = None,
    publish_recovery: "PublishRecoveryService | None" = None,
) -> None:
    """Process active sessions - moved from Orchestrator per method table.

    Completion decisions (``decide_outcome``: publish gate + push + PR) run
    through ``completion_dispatcher``. The default synchronous dispatcher keeps
    the original one-tick behavior; the orchestrator injects a background
    dispatcher so a slow publish no longer blocks the heartbeat. Decisions that
    finished on a prior tick are drained and applied here, on the tick thread,
    so all ``OrchestratorState`` mutation stays single-threaded.

    Args:
        state: Orchestrator state (active_sessions)
        observer: Session observer for checking session status
        session_controller: For deciding outcome
        completion_handler: For processing completion
        action_applier: For applying actions
        worktree_manager: For worktree cleanup
        kill_session_fn: Function to kill terminal session
        config: Configuration
        provider_resilience: Applies provider-circuit effects returned by decisions
    """
    from ..observation.observation import SessionObservation

    dispatcher = completion_dispatcher or SynchronousCompletionDispatcher()

    def apply(completed: CompletedDecision) -> None:
        _apply_completed_decision(
            completed,
            state=state,
            completion_handler=completion_handler,
            action_applier=action_applier,
            observer=observer,
            worktree_manager=worktree_manager,
            kill_session_fn=kill_session_fn,
            config=config,
            session_output=session_controller.session_output,
            provider_resilience=provider_resilience,
            publish_recovery=publish_recovery,
            pending_work_claims=pending_work_claims,
        )

    # Apply decisions that finished on a prior tick (background dispatcher)
    # BEFORE dispatching new work: applying a completed decision removes its
    # session from active_sessions, so the dispatch loop below won't re-dispatch
    # a session whose decision already landed.
    _apply_completed_decisions(dispatcher.drain(), apply)

    seen_terminals: set[str] = set()
    for session in list(state.active_sessions):
        # Snapshot iteration is mutation-safe; the live check filters any
        # duplicate terminal already removed by an earlier snapshot entry.
        if not has_active_terminal(state.active_sessions, session.terminal_id):
            logger.debug(
                "[COMPLETION] Skipping stale active-session snapshot entry: %s",
                session.terminal_id,
            )
            continue
        if session.terminal_id in seen_terminals:
            # A duplicate snapshot entry for a terminal we already handled this
            # tick. The old inline path relied on handle_session_completion
            # removing the terminal before the duplicate was reached; completion
            # now applies in drain (possibly a later tick), so dedup explicitly.
            continue
        if dispatcher.in_flight(session.terminal_id):
            # Its completion decision is already running off the tick thread;
            # don't re-observe or re-dispatch until it lands in drain().
            continue
        seen_terminals.add(session.terminal_id)
        # Attribute the tick to this issue while we handle it. Completion
        # handling (validation gate + push + PR) is the slow work; if a
        # synchronous dispatcher runs it here and it overruns the heartbeat
        # budget, the dashboard reads current_tick_phase to explain the stall —
        # naming the issue turns "(phase: active_sessions)" into
        # "(phase: active_sessions:#392)".
        state.current_tick_phase = f"active_sessions:#{session.issue.number}"
        obs = observer.observe_session(session)
        if obs.observation == SessionObservation.RUNNING:
            continue
        dispatcher.dispatch(
            session,
            _completion_decider(session_controller, session, obs, config),
        )

    # Apply decisions a synchronous dispatcher just produced this tick (the
    # background dispatcher returns nothing here — its work is still running).
    _apply_completed_decisions(dispatcher.drain(), apply)


def _completion_decider(
    session_controller: "SessionController",
    session: Session,
    obs: "SessionObservationResult",
    config: Config,
) -> "Callable[[], SessionDecision]":
    """Bind a no-arg call to decide this session's outcome.

    Cheap per-session inputs (issue key, retry template) are computed now on the
    tick thread; the returned callable performs the slow git/GitHub/validation
    work and may run off-thread.
    """
    issue_key = _validation_issue_key(session, config)
    retry_prompt_template = (
        session.agent_config.retry_prompt_template or config.retry.retry_prompt_template
    )

    def decide() -> "SessionDecision":
        return session_controller.decide_outcome(
            obs, session.worktree_path, session.issue.number,
            session.issue.title, session.terminal_id, session.completion_path,
            validation_retry_count=session.validation_retry_count,
            original_prompt=session.original_prompt,
            retry_prompt_template=retry_prompt_template,
            repo_root=config.repo_root,
            issue_key=issue_key,
            session_run_assets=session.run_assets,
            task_kind=session.key.task,
        )

    return decide


def _apply_completed_decisions(
    completed_decisions: list[CompletedDecision],
    apply: Callable[[CompletedDecision], None],
) -> None:
    """Apply every drained decision, then raise any apply failure."""
    errors: list[BaseException] = []
    for completed in completed_decisions:
        try:
            apply(completed)
        except BaseException as exc:
            errors.append(exc)
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup("completion decision apply failures", errors)


def _apply_completed_decision(
    completed: CompletedDecision,
    *,
    state: "OrchestratorState",
    completion_handler: "CompletionHandler",
    action_applier: "ActionApplier",
    observer: "SessionObserver",
    worktree_manager: Optional[WorktreeManager],
    kill_session_fn: Callable[[str], None],
    config: Config,
    session_output: SessionOutput,
    pending_work_claims: PendingWorkClaimStore,
    provider_resilience: "ProviderResilienceManager | None" = None,
    publish_recovery: "PublishRecoveryService | None" = None,
) -> None:
    """Apply a finished completion decision on the tick thread."""
    if completed.error is not None:
        # Surface a decision-time failure on the tick thread, preserving the
        # fail-loud behavior of the old inline path.
        raise completed.error
    decision = completed.decision
    assert decision is not None  # error is None => decision is set
    _record_provider_resilience_effects(decision, provider_resilience)
    session = completed.session
    if decision.status == SessionStatus.RUNNING:
        logger.info(
            "[COMPLETION] Session remains active after completion decision: "
            "session=%s issue=%s reason=%s",
            session.terminal_id,
            session.issue.number,
            decision.reason,
        )
        return
    started = time.monotonic()
    # Extract pr_url, errors, and diagnostic_path from completion processor result
    pr_url_hint = None
    processing_errors = None
    diagnostic_path = None
    validation_error = decision.validation_error
    validation_error_file = decision.validation_error_file
    review_exchange_completed = False
    review_exchange_halted = False
    if decision.processing_result:
        if decision.processing_result.pr_url:
            pr_url_hint = decision.processing_result.pr_url
        if decision.processing_result.errors:
            processing_errors = decision.processing_result.errors
        if decision.processing_result.diagnostic_path:
            diagnostic_path = decision.processing_result.diagnostic_path
        review_exchange_completed = decision.processing_result.review_exchange_completed
        review_exchange_halted = decision.processing_result.review_exchange_halted
    diagnostic_path = decision.diagnostic_path or diagnostic_path
    handle_session_completion(
        session, decision.status, state, completion_handler, action_applier,
        observer, worktree_manager, kill_session_fn, config,
        session_output=session_output,
        pr_url_hint=pr_url_hint, processing_errors=processing_errors,
        diagnostic_path=diagnostic_path,
        validation_error=validation_error,
        validation_error_file=str(validation_error_file) if validation_error_file else None,
        review_exchange_completed=review_exchange_completed,
        review_exchange_halted=review_exchange_halted,
        blocked_label=decision.blocked_label,
        blocked_reason=decision.blocked_reason,
        completion_detail=decision.completion_detail,
        publish_recovery=publish_recovery,
        provider_error_type=decision.provider_error_type,
        pending_work_claims=pending_work_claims,
    )
    elapsed = time.monotonic() - started
    if elapsed > 5:
        logger.warning(
            "[LOOP] Session handling took %.1fs (session=%s issue=%s)",
            elapsed,
            session.terminal_id,
            session.issue.number,
        )


def _record_provider_resilience_effects(
    decision: "SessionDecision",
    provider_resilience: "ProviderResilienceManager | None",
) -> None:
    """Apply provider-circuit effects on the tick thread."""
    if provider_resilience is None:
        return
    if decision.provider_success:
        provider_resilience.record_success(decision.provider_success)
    if decision.provider_transient_failure:
        failure = decision.provider_transient_failure
        provider_resilience.record_transient_failure(
            failure.provider,
            error_summary=failure.error_summary,
            attempts=failure.attempts,
        )
    if decision.provider_auth_failure:
        auth_failure = decision.provider_auth_failure
        provider_resilience.record_auth_failure(
            auth_failure.provider,
            error_summary=auth_failure.error_summary,
            # The credential sample this verdict was confirmed against. Sharing
            # it with the launch-side check means one physical observation is
            # counted once, not once per consumer (#6999 F2).
            sample_id=auth_failure.sample_id,
        )
