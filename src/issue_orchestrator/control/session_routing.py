"""Orchestrator-facing session routing helpers.

These helpers bridge orchestrator state and session infrastructure. Core launch
policy stays in SessionLauncher; this module handles wrapper concerns such as
active-session registration, orphan restoration, tech_lead dispatch, and
SessionManager adapter calls.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ..domain.models import (
    DiscoveredFailure,
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
    PendingWorkRequest,
)
from ..domain.tech_lead_session import TechLeadLaunchScope, TechLeadSessionFlavor
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
from .in_flight_work import InFlightWorkLedger, LaunchSettlement
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


class TechLeadQueueOutcome(Enum):
    """Explicit result of asking the queue owner to enqueue tech_lead work."""

    QUEUED = "queued"
    DUPLICATE = "duplicate"


# Bound on retryable launch failures per queued tech_lead item. Three attempts
# ride out a transient SQLite/log/filesystem blip without relaunch-looping a
# genuinely broken input forever; after the third failure the item is dropped
# and the drop is surfaced loudly (fail-fast-but-not-silent).
TECH_LEAD_LAUNCH_RETRY_LIMIT = 3


class TechLeadRetentionOutcome(Enum):
    """Explicit result of retaining a queued tech_lead item after a retryable failure."""

    RETAINED = "retained"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class PendingSessionQueues:
    """Owner for pending session queues: launch-routing removals + tech_lead intake.

    Tech Lead intake is behavior-level (#6768 round 3): producers say WHICH
    variant they are queueing (batch review, failure investigation, or health
    review) and this owner constructs the ``PendingTechLeadReview``, applies the
    single deduplication rule (by issue number against the pending queue), and
    returns an explicit :class:`TechLeadQueueOutcome`. Producers never touch the
    dataclass or the state list.
    """

    state: "OrchestratorState"

    def remove_review(self, pr_number: int) -> None:
        self.state.pending_reviews[:] = [
            r for r in self.state.pending_reviews if r.pr_number != pr_number
        ]

    def remove_retrospective_review(self, issue_number: int) -> None:
        self.state.pending_retrospective_reviews[:] = [
            r
            for r in self.state.pending_retrospective_reviews
            if r.issue_number != issue_number
        ]

    def remove_rework(self, rework: PendingRework) -> None:
        self.state.pending_reworks[:] = [
            r for r in self.state.pending_reworks if r.issue_key != rework.issue_key
        ]

    def remove_validation_retry(self, issue_number: int) -> None:
        self.state.pending_validation_retries[:] = [
            queued
            for queued in self.state.pending_validation_retries
            if queued.issue_number != issue_number
        ]

    def remove_tech_lead(self, issue_number: int) -> None:
        self.state.pending_tech_lead_reviews[:] = [
            t
            for t in self.state.pending_tech_lead_reviews
            if t.issue_number != issue_number
        ]

    def queue_batch_review(self, issue_number: int, title: str) -> TechLeadQueueOutcome:
        """Queue a threshold-created batch tracking issue (audits the PR manifest)."""
        return self._queue_tech_lead(
            PendingTechLeadReview(
                issue_number, title, flavor=TechLeadSessionFlavor.BATCH_REVIEW
            )
        )

    def queue_health_review(
        self,
        issue_number: int,
        title: str,
        *,
        problem_cohort: tuple[DiscoveredFailure, ...] = (),
    ) -> TechLeadQueueOutcome:
        """Queue an interval-created health-review anchor (ADR-0031 §4); like a
        batch review it carries no singular failure context. An unscheduled
        problem-storm review instead carries its typed cohort so the later
        launch snapshot cannot lose the trigger facts at end-of-tick."""
        return self._queue_tech_lead(
            PendingTechLeadReview(
                issue_number,
                title,
                flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
                problem_cohort=problem_cohort,
            )
        )

    def remove_failure_investigations(
        self, issue_numbers: frozenset[int]
    ) -> None:
        """Remove only storm-superseded individual investigation entries.

        Batch and health anchors may share an issue number with other tech_lead
        bookkeeping and must never be removed by a problem-cohort transition.
        """
        self.state.pending_tech_lead_reviews[:] = [
            item
            for item in self.state.pending_tech_lead_reviews
            if not (
                item.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
                and item.issue_number in issue_numbers
            )
        ]

    def queue_failure_investigation(
        self, issue_number: int, title: str, *, failure: DiscoveredFailure
    ) -> TechLeadQueueOutcome:
        """Queue a focused investigation of one failed issue.

        ``failure`` is required (non-optional): the queue item is the only
        carrier of the typed triggering-failure context once the per-tick
        ``discovered_failures`` buffer is cleared after planning (the
        launch-time board snapshot reads it from here).
        ``PendingTechLeadReview.__post_init__`` stays as defense-in-depth
        against untyped callers passing ``None`` anyway.
        """
        return self._queue_tech_lead(
            PendingTechLeadReview(
                issue_number,
                title,
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                failure=failure,
            )
        )

    def retain_tech_lead_for_retry(self, issue_number: int) -> TechLeadRetentionOutcome:
        """Bounded retention of a queued tech_lead item after a retryable launch failure.

        Before escalation starts, failure investigations have no labels-as-
        truth recovery: the queued item is the only record (the per-tick
        ``discovered_failures`` buffer is cleared after planning), so a
        transient required-input prep failure must retain it for retry, not
        delete it. Retention is bounded by ``TECH_LEAD_LAUNCH_RETRY_LIMIT``:
        once exhausted ``EXHAUSTED`` is returned, but the item is NOT removed
        here (#6771 round 4). Destructive queue removal must not precede the
        lifecycle's committed needs-human transition, so the launch caller
        commits the drop via
        ``remove_tech_lead`` only after ``escalate_issue_needs_human`` succeeds;
        on escalation failure the item is retained and re-attempted.

        Asking to retain an item that is not queued is an invariant violation
        upstream (the launch path holds the item it just failed to launch);
        fail fast rather than silently absorbing it.
        """
        item = next(
            (
                t
                for t in self.state.pending_tech_lead_reviews
                if t.issue_number == issue_number
            ),
            None,
        )
        if item is None:
            raise ValueError(
                f"Cannot retain tech_lead item for issue #{issue_number} after a "
                "retryable launch failure: no such item is queued"
            )
        item.retryable_launch_failures += 1
        if item.retryable_launch_failures >= TECH_LEAD_LAUNCH_RETRY_LIMIT:
            return TechLeadRetentionOutcome.EXHAUSTED
        logger.warning(
            "[TECH_LEAD] Retaining %s for issue #%d after retryable launch failure "
            "%d/%d",
            item.flavor.value,
            issue_number,
            item.retryable_launch_failures,
            TECH_LEAD_LAUNCH_RETRY_LIMIT,
        )
        return TechLeadRetentionOutcome.RETAINED

    def restore_deferred(self, claim: PendingWorkClaim) -> bool:
        """Return a launched-but-provider-deferred request to its own queue.

        The admission half of this owner (#6999 A1): every queue already
        declares here how an item is removed, so it declares here how one comes
        back. Admission is a decision TABLE rather than a branch chain, and each
        entry delegates to the collection's own owner method, so the duplicate
        rule for a queue lives in exactly one place whether the item is arriving
        from discovery or coming back from a dead session.

        The re-admitted object is the ORIGINAL request, preserving the context
        that exists nowhere else - a failure investigation's typed
        ``DiscoveredFailure``, a validation retry's prompt/error/count, a
        rework's cycle number.

        Returns True when the item was admitted, False when the queue already
        held an equivalent request. Restoring costs no retry budget: nothing
        about the request failed.
        """
        admit = _QUEUE_ADMISSION.get(claim.kind)
        if admit is None:
            # Named explicitly, like the launch-disposition switch: a queue kind
            # added without an entry here must fail loudly rather than silently
            # drop the only record of its work.
            raise ValueError(f"unhandled pending work kind: {claim.kind}")
        return admit(self, claim.request)

    def queue_existing_tech_lead(
        self, item: PendingTechLeadReview
    ) -> TechLeadQueueOutcome:
        """Re-admit an already-built tech_lead item under the same dedup rule.

        Distinct from the ``queue_*`` producers above, which BUILD the item from
        its parts. Restoring must not rebuild: the original carries its typed
        failure context and its spent retry count (#6999 A1).
        """
        return self._queue_tech_lead(item)

    def _queue_tech_lead(self, item: PendingTechLeadReview) -> TechLeadQueueOutcome:
        """Apply the one dedup rule (issue number vs pending queue) and enqueue."""
        queue = self.state.pending_tech_lead_reviews
        if any(t.issue_number == item.issue_number for t in queue):
            logger.info(
                "[TECH_LEAD] Issue #%d already queued for tech_lead; skipping %s request",
                item.issue_number,
                item.flavor.value,
            )
            return TechLeadQueueOutcome.DUPLICATE
        queue.append(item)
        logger.info(
            "[TECH_LEAD] Queued %s for issue #%d: %s",
            item.flavor.value,
            item.issue_number,
            item.title,
        )
        return TechLeadQueueOutcome.QUEUED



def _admit_review(queues: PendingSessionQueues, request: PendingWorkRequest) -> bool:
    assert isinstance(request, PendingReview)
    return queues.state.queue_pending_review(request)


def _admit_retrospective_review(
    queues: PendingSessionQueues, request: PendingWorkRequest
) -> bool:
    assert isinstance(request, PendingRetrospectiveReview)
    return queues.state.queue_pending_retrospective_review(request)


def _admit_rework(queues: PendingSessionQueues, request: PendingWorkRequest) -> bool:
    assert isinstance(request, PendingRework)
    return queues.state.queue_pending_rework(request)


def _admit_validation_retry(
    queues: PendingSessionQueues, request: PendingWorkRequest
) -> bool:
    assert isinstance(request, PendingValidationRetry)
    return queues.state.queue_pending_validation_retry(request)


def _admit_tech_lead(queues: PendingSessionQueues, request: PendingWorkRequest) -> bool:
    assert isinstance(request, PendingTechLeadReview)
    # Through the tech-lead intake owner, not the state list: this queue's
    # duplicate rule carries logging and a typed outcome that the others do not.
    return queues.queue_existing_tech_lead(request) is TechLeadQueueOutcome.QUEUED


# One entry per pending queue. A decision table rather than a branch chain so
# "which queue admits this" has a single, enumerable answer (#6999 A1).
_QUEUE_ADMISSION: dict[
    PendingWorkKind, Callable[[PendingSessionQueues, PendingWorkRequest], bool]
] = {
    PendingWorkKind.REVIEW: _admit_review,
    PendingWorkKind.RETROSPECTIVE_REVIEW: _admit_retrospective_review,
    PendingWorkKind.REWORK: _admit_rework,
    PendingWorkKind.VALIDATION_RETRY: _admit_validation_retry,
    PendingWorkKind.TECH_LEAD: _admit_tech_lead,
}


def orchestrator_launch_review_session(
    review: PendingReview,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
    claims: PendingWorkClaimStore,
) -> Optional[Session]:
    """Launch a review session and update orchestrator queues."""
    pending_queues = PendingSessionQueues(state)
    result = session_launcher.launch_review_session(review, state.active_sessions)
    return LaunchSettlement(
        claims=claims,
        claim=PendingWorkClaim(PendingWorkKind.REVIEW, review),
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
    result = session_launcher.launch_retrospective_review_session(
        review,
        state.active_sessions,
    )
    return LaunchSettlement(
        claims=claims,
        claim=PendingWorkClaim(PendingWorkKind.RETROSPECTIVE_REVIEW, review),
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
    result = session_launcher.launch_rework_session(rework, state.active_sessions)
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
        claims=claims,
        claim=PendingWorkClaim(PendingWorkKind.REWORK, rework),
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
    result = session_launcher.launch_validation_retry_session(
        retry, state.active_sessions
    )
    return LaunchSettlement(
        claims=claims,
        claim=PendingWorkClaim(PendingWorkKind.VALIDATION_RETRY, retry),
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
    - ``INPUT_RETRY`` — required-input prep failed transiently BEFORE the
      session started. For failure investigations the queued item is the only
      record of the investigation (no labels-as-truth recovery), so one
      transient SQLite/log/filesystem error must not delete it. Retention is
      bounded by the queue owner (``retain_tech_lead_for_retry``); on exhaustion
      the item is dropped as a DURABLE needs-human transition — the
      needs-human label plus an explanatory comment applied through the
      launcher's owning action boundary, then the ``ISSUE_NEEDS_HUMAN``
      event (#6771 round 3: a log line and an event alone do not survive an
      orchestrator restart; labels are the source of truth).
    """
    agent = config.tech_lead_review_agent
    if not agent or agent not in config.agents:
        raise ValueError(f"Invalid tech lead agent: {agent}")
    pending_queues = PendingSessionQueues(state)
    result = session_launcher.launch_issue_session(
        Issue(tech_lead.issue_number, tech_lead.title, [agent]),
        state.active_sessions,
        tech_lead_scope=tech_lead.launch_scope(),
    )
    def _retain_for_input_retry() -> None:
        outcome = pending_queues.retain_tech_lead_for_retry(tech_lead.issue_number)
        if outcome is TechLeadRetentionOutcome.EXHAUSTED:
            _commit_or_retain_dropped_tech_lead(
                tech_lead, result.reason, session_launcher, pending_queues
            )

    return LaunchSettlement(
        claims=claims,
        claim=PendingWorkClaim(PendingWorkKind.TECH_LEAD, tech_lead),
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
        retain_for_input_retry=_retain_for_input_retry,
    ).settle(result, state)


def _commit_or_retain_dropped_tech_lead(
    tech_lead: PendingTechLeadReview,
    last_error: str,
    session_launcher: SessionLauncher,
    pending_queues: PendingSessionQueues,
) -> None:
    """Commit protocol for a tech_lead item that exhausted its launch retries.

    The queued item is the only record before escalation starts, so it is
    dropped only after ``escalate_issue_needs_human`` confirms the label and
    comment transition (#6771 round 4). A partial marker commit is independently
    crash-recoverable, while this process retains the richer queued context for
    retry. The failure is surfaced and no ISSUE_NEEDS_HUMAN event is emitted for
    a non-transition.
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
        pending_queues.remove_tech_lead(tech_lead.issue_number)
        return
    logger.error(
        "[TECH_LEAD] Durable needs-human escalation did NOT commit for issue "
        "#%d; retaining queued %s context for retry (any committed marker "
        "also enables crash recovery)",
        tech_lead.issue_number,
        tech_lead.flavor.value,
    )


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
    ledger = InFlightWorkLedger(state, claims)
    restored = session_restorer.restore_sessions(running, state.active_sessions)
    restoration = ledger.rehydrate(restored)
    for quarantined in restoration.quarantined:
        quarantine.quarantine_session(quarantined)
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
