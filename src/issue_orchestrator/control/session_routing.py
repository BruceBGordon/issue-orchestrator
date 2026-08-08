"""Orchestrator-facing session routing helpers.

These helpers bridge orchestrator state and session infrastructure. Core launch
policy stays in SessionLauncher; this module handles wrapper concerns such as
active-session registration, orphan restoration, tech_lead dispatch, and
SessionManager adapter calls.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ..domain.models import (
    Issue,
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingTechLeadReview,
    PendingValidationRetry,
    Session,
)
from ..domain.pending_work import (
    PendingWorkClaim,
    PendingWorkKind,
)
from ..domain.tech_lead_session import TechLeadLaunchScope
from ..events import EventName
from ..infra.config import Config
from ..ports import EventSink, Issue as IssueProtocol, make_trace_event
from ..ports.pending_work_claim_store import PendingWorkClaimStore
from ..ports.session_runner import DiscoveredSession
from .active_sessions import append_unique_active_sessions
from .existing_terminal_restoration import (
    _ExistingTerminalRestorationRequest,
    _restore_existing_terminal,
)
from .pending_session_queues import (
    TECH_LEAD_LAUNCH_RETRY_LIMIT,
    PendingSessionQueues,
)
from .in_flight_work import InFlightWorkLedger
from .launch_transaction import (
    LaunchSettlement,
    PendingWorkLaunchClaim,
    RetryPlan,
)
from .session_launch_types import LaunchDisposition
from .session_launcher import SessionLauncher
from .session_manager import SessionManager, SessionRef

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from .claim_quarantine import ClaimQuarantineOwner
    from ..domain.state_machines.session_machine import SessionStateMachine
    from .session_manager import SessionType
    from .session_restorer import SessionRestorer
    from .state_machine_manager import StateMachineManager

logger = logging.getLogger(__name__)


def orchestrator_launch_review_session(
    review: PendingReview,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
    claims: PendingWorkClaimStore,
) -> Optional[Session]:
    """Launch a review session and update orchestrator queues."""
    pending_queues = PendingSessionQueues(state)
    # One object for the whole launch: the launcher holds the claim durably
    # before it spawns anything, and the settlement below settles that same
    # claim afterwards (#6999 A2).
    work = PendingWorkLaunchClaim(
        claim=PendingWorkClaim(PendingWorkKind.REVIEW, review), claims=claims
    )
    result = session_launcher.launch_review_session(
        review, state.active_sessions, work_claim=work
    )
    return LaunchSettlement(
        work=work,
        remove=lambda: pending_queues.remove_review(review.pr_number),
        restore_existing=lambda: _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=review.issue_number,
                session_name=f"review-{review.pr_number}",
                is_review=True,
                tab_name=f"Review PR #{review.pr_number}",
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        ),
    ).settle(result, state)


def orchestrator_launch_retrospective_review_session(
    review: PendingRetrospectiveReview,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
    claims: PendingWorkClaimStore,
) -> Optional[Session]:
    """Launch a retrospective review session and update orchestrator queues."""
    pending_queues = PendingSessionQueues(state)
    work = PendingWorkLaunchClaim(
        claim=PendingWorkClaim(PendingWorkKind.RETROSPECTIVE_REVIEW, review),
        claims=claims,
    )
    result = session_launcher.launch_retrospective_review_session(
        review,
        state.active_sessions,
        work_claim=work,
    )
    return LaunchSettlement(
        work=work,
        remove=lambda: pending_queues.remove_retrospective_review(review.issue_number),
        restore_existing=lambda: _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=review.issue_number,
                session_name=SessionRef.for_retrospective_review(
                    review.issue_number
                ).name,
                is_review=True,
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        ),
    ).settle(result, state)


def orchestrator_launch_rework_session(
    rework: PendingRework,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
    claims: PendingWorkClaimStore,
) -> Optional[Session]:
    """Launch a rework session and update orchestrator queues."""
    pending_queues = PendingSessionQueues(state)
    work = PendingWorkLaunchClaim(
        claim=PendingWorkClaim(PendingWorkKind.REWORK, rework), claims=claims
    )
    result = session_launcher.launch_rework_session(
        rework, state.active_sessions, work_claim=work
    )
    def _restore_rework() -> Optional[Session]:
        issue_number = rework.resolve_issue_number()
        if issue_number is None:
            logger.warning("[ORPHAN] Rework missing issue number: %s", rework.issue_key)
            return None
        return _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=issue_number,
                session_name=f"rework-{issue_number}",
                is_review=False,
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        )

    return LaunchSettlement(
        work=work,
        remove=lambda: pending_queues.remove_rework(rework),
        restore_existing=_restore_rework,
    ).settle(result, state)


def orchestrator_launch_validation_retry_session(
    retry: PendingValidationRetry,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
    claims: PendingWorkClaimStore,
) -> Optional[Session]:
    """Launch a validation retry session and update retry queue tracking."""
    pending_queues = PendingSessionQueues(state)
    work = PendingWorkLaunchClaim(
        claim=PendingWorkClaim(PendingWorkKind.VALIDATION_RETRY, retry), claims=claims
    )
    result = session_launcher.launch_validation_retry_session(
        retry, state.active_sessions, work_claim=work
    )
    return LaunchSettlement(
        work=work,
        remove=lambda: pending_queues.remove_validation_retry(retry.issue_number),
        restore_existing=lambda: _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=retry.issue_number,
                session_name=f"issue-{retry.issue_number}",
                is_review=False,
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        ),
        drop_on_permanent_failure=False,
    ).settle(result, state)


def orchestrator_launch_tech_lead_session(
    tech_lead: PendingTechLeadReview,
    state: "OrchestratorState",
    config: Config,
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
    claims: PendingWorkClaimStore,
) -> Optional[Session]:
    """Launch a queued tech_lead session and update orchestrator queues.

    The pending-tech-lead queue carries every tech_lead variant — threshold-created
    batch tracking issues, interval-created health-review anchors (ADR-0031
    §4), and failure investigations — and the planner launches them
    through this path before ordinary issue pickup. The producer boundary that
    queued the item declared its flavor; forward it verbatim (#6768 B5:
    hard-coding one flavor here made batch reviews skip manifest prep).

    Queue lifecycle mirrors :func:`orchestrator_launch_review_session`
    (#6768 round 4 — a launched item previously stayed queued and was
    relaunched every tick): the item is removed through the owning
    :class:`PendingSessionQueues` on success, on restore of an existing
    terminal, and on permanent launch failure (labels-as-truth recovers a
    dropped batch at startup; a dropped investigation is a best-effort audit).
    It is retained in exactly three cases:

    - ``EXISTING_TERMINAL`` — a terminal that could not be restored yet;
    - ``PROVIDER_DEFERRED`` — the provider refused before anything was
      attempted. Nothing about the investigation failed, so it keeps its full
      retry budget and simply waits for a tick when the provider is ready
      (#6999 F10);
    - ``RETRYABLE_FAILURE`` — the launch attempt failed transiently BEFORE the
      session started: required-input prep, or a terminal that never came up.
      For failure investigations the queued item is the only record of the
      investigation (no labels-as-truth recovery), so one transient
      SQLite/log/filesystem/terminal error must not delete it. Retention is
      bounded by the queue owner (``plan_tech_lead_retry``); on exhaustion
      the item is dropped as a DURABLE needs-human transition — the
      needs-human label plus an explanatory comment applied through the
      launcher's owning action boundary, then the ``ISSUE_NEEDS_HUMAN``
      event (#6771 round 3: a log line and an event alone do not survive an
      orchestrator restart; labels are the source of truth).

    Every one of those outcomes settles the DURABLE claim too, in the same
    transaction (#6999 F4), and the ledger hears first (#6999 F2). A retained
    item keeps its deferred row, rewritten with the retry budget the retention
    just spent BEFORE that spend reaches the queue; a dropped one - permanent
    failure, or a committed escalation after exhaustion - has its row retired
    before the queue item goes, so the startup sweep cannot re-admit an
    investigation the bound already ended.
    """
    agent = config.tech_lead_review_agent
    if not agent or agent not in config.agents:
        raise ValueError(f"Invalid tech lead agent: {agent}")
    pending_queues = PendingSessionQueues(state)
    work = PendingWorkLaunchClaim(
        claim=PendingWorkClaim(PendingWorkKind.TECH_LEAD, tech_lead), claims=claims
    )
    result = session_launcher.launch_issue_session(
        Issue(tech_lead.issue_number, tech_lead.title, [agent]),
        state.active_sessions,
        tech_lead_scope=tech_lead.launch_scope(),
        work_claim=work,
    )

    def _plan_retry(_claim: PendingWorkClaim) -> RetryPlan:
        """Plan one unit of the budget; act on it only once it is durable.

        The plan is what keeps the durable ledger honest (#6999 F4/F2). The
        advanced request is handed back as a COPY so the launch transaction can
        write it to the ledger before this queue is touched, and the exhausted
        branch's needs-human transition - the one irreversible act in the whole
        settlement - runs only after that write.
        """
        plan = pending_queues.plan_tech_lead_retry(tech_lead.issue_number)
        return RetryPlan(
            spent=PendingWorkClaim(PendingWorkKind.TECH_LEAD, plan.item),
            exhausted=plan.exhausted,
            apply=lambda: pending_queues.apply_tech_lead_retry(plan),
            commit_exhaustion=lambda: _commit_dropped_tech_lead(
                tech_lead, result.reason, session_launcher
            ),
        )

    return LaunchSettlement(
        work=work,
        remove=lambda: pending_queues.remove_tech_lead(tech_lead.issue_number),
        restore_existing=lambda: _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=tech_lead.issue_number,
                session_name=f"issue-{tech_lead.issue_number}",
                is_review=False,
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        ),
        plan_retry=_plan_retry,
    ).settle(result, state)


def _commit_dropped_tech_lead(
    tech_lead: PendingTechLeadReview,
    last_error: str,
    session_launcher: SessionLauncher,
) -> bool:
    """Commit protocol for a tech_lead item that exhausted its launch retries.

    The queued item is the only record before escalation starts, so it is
    dropped only after ``escalate_issue_needs_human`` confirms the label and
    comment transition (#6771 round 4). A partial marker commit is independently
    crash-recoverable, while this process retains the richer queued context for
    retry. The failure is surfaced and no ISSUE_NEEDS_HUMAN event is emitted for
    a non-transition.

    Reports ONLY whether the transition committed; neither queue nor ledger is
    touched here (#6999 F2). The caller owns that order - ledger first, queue
    second - and this is the irreversible step that has to run between the
    durable spend and either of them.
    """
    logger.error(
        "[TECH_LEAD] Escalating dropped %s for issue #%d after %d retryable "
        "launch failures: %s",
        tech_lead.flavor.value,
        tech_lead.issue_number,
        TECH_LEAD_LAUNCH_RETRY_LIMIT,
        last_error,
    )
    comment = (
        f"**Queued {tech_lead.flavor.value} dropped after "
        f"{TECH_LEAD_LAUNCH_RETRY_LIMIT} launch failures**\n\n"
        "The orchestrator could not prepare the required inputs for this "
        f"tech_lead session {TECH_LEAD_LAUNCH_RETRY_LIMIT} times in a row, so the "
        "queued item was dropped and will not retry on its own.\n\n"
        f"Last error: {last_error}\n\n"
        "A human needs to fix the launch failure and re-queue (or close) "
        "this investigation."
    )
    committed = session_launcher.escalate_issue_needs_human(
        issue_number=tech_lead.issue_number,
        reason="tech_lead launch retries exhausted",
        comment=comment,
        context="tech_lead_launch_retry_exhausted",
        event_data={
            "issue_number": tech_lead.issue_number,
            "issue_title": tech_lead.title,
            "reason": (
                f"tech_lead launch failed {TECH_LEAD_LAUNCH_RETRY_LIMIT} "
                f"times on required-input preparation; dropping "
                f"queued {tech_lead.flavor.value}: {last_error}"
            ),
        },
    )
    if committed:
        return True
    logger.error(
        "[TECH_LEAD] Durable needs-human escalation did NOT commit for issue "
        "#%d; retaining queued %s context for retry (any committed marker "
        "also enables crash recovery)",
        tech_lead.issue_number,
        tech_lead.flavor.value,
    )
    return False


def session_launcher_callback(
    session_type: "SessionType",
    number: int,
    launch_issue_fn: Callable[[int], Optional[Session]],
    launch_review_fn: Callable[[int], Optional[Session]],
    launch_retrospective_review_fn: Callable[[int], Optional[Session]],
    launch_rework_fn: Callable[[int], Optional[Session]],
    launch_tech_lead_fn: Callable[[int], Optional[Session]],
) -> Optional[Session]:
    """Route SessionManager launch callbacks by session type."""
    from .session_manager import SessionType

    handlers = {
        SessionType.ISSUE: launch_issue_fn,
        SessionType.REVIEW: launch_review_fn,
        SessionType.RETROSPECTIVE_REVIEW: launch_retrospective_review_fn,
        SessionType.REWORK: launch_rework_fn,
        SessionType.TECH_LEAD: launch_tech_lead_fn,
    }
    return handlers[session_type](number)


def recover_unresolved_work(
    state: "OrchestratorState",
    claims: PendingWorkClaimStore,
    quarantine: "ClaimQuarantineOwner",
) -> int:
    """Sweep the durable ledger when there is nothing to restore (#6999 F8).

    The same sweep :func:`restore_running_sessions` ends with, exposed for the
    paths that short-circuit before restoring anything. It must run there too:
    a row whose terminal has already gone is invisible to discovery, so the
    branch that says "nothing to restore" is the branch where that row is the
    only record of the work left.
    """
    return InFlightWorkLedger(state, claims).recover_unresolved(quarantine)


def restore_running_sessions(
    running: list["DiscoveredSession"],
    state: "OrchestratorState",
    session_restorer: "SessionRestorer",
    claims: PendingWorkClaimStore,
    quarantine: "ClaimQuarantineOwner",
) -> list[Session]:
    """Restore running terminal sessions into active-session tracking.

    Restoring the terminal is only half of it. A session launched off a pending
    queue is still carrying that queue's request, and after a restart the
    request lives only in the orchestrator's claim store - so the claim is
    rehydrated here, before the session is admitted (#6999 F4). Without it a
    provider failure observed after a restart would find no claim and the work
    would be gone for good.

    Admission is deliberately gated on that rehydration (#6999 F6). A terminal
    whose claim RECORD EXISTS but cannot be read is not admitted at all: it is
    alive and doing queued work nobody can now name, so processing it normally
    would end with its completion settling as claimless and destroying that
    work. It is quarantined and escalated instead, where a human can see it.
    Healthy neighbours restore regardless.

    Finally the ledger is swept for work no live terminal is holding at all
    (#6999 F8) - a run killed mid-settlement leaves a row discovery will never
    surface, and for a failure investigation that row is the only record there
    is. That sweep is why this function must be called on EVERY startup and
    reconcile, including when discovery returns nothing: the case it exists for
    is precisely the one with no terminal left to discover.
    """
    from .claim_quarantine import QuarantineSubject

    ledger = InFlightWorkLedger(state, claims)
    restored = session_restorer.restore_sessions(running, state.active_sessions)
    restoration = ledger.rehydrate(restored)
    for quarantined in restoration.quarantined:
        quarantine.quarantine(
            QuarantineSubject.live_run_with_unreadable_claim(quarantined)
        )
    # Every DISCOVERED run gets a verdict, not only the ones that rebuilt into
    # a Session (#6999 F14). The ledger owns that accounting.
    accounting = ledger.account_for_discovered(running, restoration, quarantine)
    added = append_unique_active_sessions(
        state.active_sessions, list(restoration.admitted)
    )
    # Then the half discovery cannot reach: ledger rows whose run ended while
    # its settlement was interrupted, and deferred rows whose in-memory
    # re-queue did not survive the restart (#6999 F8). Every run observed alive
    # this pass is passed through, quarantined and stale ones included, so a
    # row belonging to a terminal that IS here cannot look orphaned (F11).
    ledger.recover_unresolved(
        quarantine,
        live_run_keys=accounting.live_run_keys,
        live_quarantine_keys=accounting.live_quarantine_keys,
    )
    if added:
        logger.info(
            "[ORPHAN] Restored %d running terminal session(s): %s",
            len(added),
            ", ".join(str(session.terminal_id) for session in added),
        )
    elif running and not restoration.quarantined:
        logger.warning(
            "[ORPHAN] Found %d running terminal session(s), but none could be restored",
            len(running),
        )
    return added


def parse_session_ref(
    session_name: str,
    operation: str,
    events: EventSink,
):
    """Parse a session ref and publish a trace event on invalid names."""
    from .session_manager import SessionRef

    try:
        return SessionRef.from_name(session_name)
    except ValueError as e:
        events.publish(
            make_trace_event(
                EventName.SESSION_NAME_PARSE_ERROR,
                {"session_name": session_name, "error": str(e)},
            )
        )
        raise


def create_session(
    name: str,
    cmd: str,
    wd: Path,
    title: str | None,
    session_manager: SessionManager,
    events: EventSink,
) -> bool:
    """Create a terminal session through SessionManager."""
    from .session_manager import SessionContext

    ref = parse_session_ref(name, "create", events)
    return session_manager.start(
        SessionContext(ref=ref, command=cmd, working_dir=wd, title=title)
    )


def session_exists(
    name: str, session_manager: SessionManager, events: EventSink
) -> bool:
    """Check whether a terminal session exists through SessionManager."""
    return session_manager.exists(parse_session_ref(name, "exists", events))


def kill_session(name: str, session_manager: SessionManager, events: EventSink) -> None:
    """Stop a terminal session through SessionManager."""
    session_manager.stop(parse_session_ref(name, "kill", events))


def get_session_machine(
    name: str,
    n: int,
    timeout: int,
    state_machines: "StateMachineManager",
) -> Optional["SessionStateMachine"]:
    """Get or create the state machine for a terminal session."""
    return state_machines.get_session_machine(name, n, timeout)


def orchestrator_launch_session(
    issue: IssueProtocol,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer | None" = None,
    *,
    tech_lead_scope: TechLeadLaunchScope | None = None,
) -> Optional[Session]:
    """Launch an issue session and update active-session tracking."""
    result = session_launcher.launch_issue_session(
        issue, state.active_sessions, tech_lead_scope=tech_lead_scope
    )
    if result.success and result.session:
        append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.disposition is LaunchDisposition.EXISTING_TERMINAL and session_restorer is not None:
        restored = _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=issue.number,
                session_name=f"issue-{issue.number}",
                is_review=False,
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        )
        if restored:
            return restored
    return result.session if result.success else None
