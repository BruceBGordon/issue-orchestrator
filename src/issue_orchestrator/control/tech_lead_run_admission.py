"""Scoped admission for tech-lead runs — the one owner of run identity (#6994).

Every tech-lead run is requested by SOMEONE: the reactive failure model, the
periodic/storm health-review trigger, the stuck sweep, the one-shot CLI, and
(from #6994) the repository dashboard. Before this module each of those paths
carried its own slice of "may this run start?" — the reaction model deduped
against the pending queue, the workflow owned paused/capacity, the on-demand
trigger owned nothing at all — so the same logical run could be admitted twice
by two different paths and nothing owned the relationship BETWEEN runs.

This module is that missing owner. The run vocabulary it decides over — the two
scopes, the request, the trigger, and the typed outcome — lives in
:mod:`...domain.tech_lead_run`, so an entrypoint or a view model can speak about
runs without importing this policy. What lives HERE is the policy itself.

Two operations, one policy:

* :meth:`TechLeadRunCoordinator.admit` answers a REQUEST — identity/dedup,
  eligibility revalidation, cross-instance claim conflict, and enqueue — and
  returns a typed :class:`TechLeadRunAdmission` the caller reports verbatim.
* :meth:`TechLeadRunCoordinator.launch_gate` answers a TICK — which queued runs
  the scope matrix lets launch right now, and why the rest wait.

Deliberate boundaries:

* **Capacity is not re-decided here.** ``tech_lead_slot_availability`` remains
  the single owner of the numeric budget; this owner decides only SEMANTIC
  conflicts, so the two cannot drift.
* **Shared truth, not process-local truth.** A global run deduplicates against
  the marker-labeled anchor issue on GitHub (ADR-0013 labels-as-truth, the same
  discovery the automatic trigger uses), and both scopes consult the issue
  claim before admitting, so a peer orchestrator's in-flight run is visible.
  The in-process pending queue alone would let two engines both admit.
* **No launching.** Admission ENQUEUES; the planner still launches, so a manual
  request rides the identical evidence/authority path an automatic one does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional, Protocol, Sequence

from ..domain.models import DiscoveredFailure, PendingTechLeadReview, SessionStatus
from ..domain.claim import ClaimFetchError
from ..domain.tech_lead_run import (
    BARRIER_GLOBAL_AWAITING_DRAIN,
    BARRIER_GLOBAL_RUN_ACTIVE,
    BARRIER_GLOBAL_RUN_QUEUED,
    REASON_ADMITTED,
    REASON_ANCHOR_UNAVAILABLE,
    REASON_CLAIMED_BY_PEER,
    REASON_DUPLICATE_REQUEST,
    REASON_ISSUE_CLOSED,
    REASON_ISSUE_NOT_FOUND,
    REASON_NO_LONGER_BLOCKED,
    REASON_NO_TECH_LEAD_AGENT,
    REASON_ORCHESTRATOR_PAUSED,
    REASON_RUN_ACTIVE,
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    TechLeadRunAdmission,
    TechLeadRunOutcome,
    TechLeadRunRequest,
    TechLeadRunScope,
    TechLeadRunScopeKind,
)
from ..domain.tech_lead_session import TechLeadSessionFlavor
from ..events import EventName
from ..ports import make_trace_event
from .tech_lead_session_policy import is_tech_lead_session

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session
    from ..infra.config import Config
    from ..ports import EventSink, Issue, RepositoryHost
    from ..ports.claim_manager import ClaimManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TechLeadLaunchGate:
    """Which queued runs the scope matrix allows to launch this tick.

    ``held`` is never silently empty-handed: whenever anything is withheld,
    ``barrier_reason`` says which rule withheld it, so the launch log and the
    dashboard can explain a queued-but-idle run instead of showing a stall.
    """

    launchable: tuple[PendingTechLeadReview, ...]
    held: tuple[PendingTechLeadReview, ...]
    barrier_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if bool(self.held) != bool(self.barrier_reason):
            raise ValueError(
                "TechLeadLaunchGate: barrier_reason must be set iff runs are held"
                f" (held={len(self.held)}, reason={self.barrier_reason!r})"
            )


def scope_of_pending(item: PendingTechLeadReview) -> TechLeadRunScope:
    """The scope a queued item runs at, derived from its declared flavor.

    Health reviews AND batch reviews audit the whole repository (one walks the
    board, the other the accumulated PR manifest), so both are global; only a
    failure investigation is scoped to a single subject issue.
    """
    if item.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        return IssueInvestigationScope(item.issue_number)
    return GlobalHealthReviewScope()


def is_global_pending(item: PendingTechLeadReview) -> bool:
    """True when a queued item holds the exclusive whole-repository scope."""
    return scope_of_pending(item).kind is TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW


def active_tech_lead_sessions(
    config: "Config", active_sessions: "Sequence[Session]"
) -> tuple["Session", ...]:
    """The active sessions that ARE tech-lead runs (ADR-0031 identity rule)."""
    return tuple(
        session
        for session in active_sessions
        if is_tech_lead_session(config.tech_lead_review_agent, session.agent_label)
    )


def has_active_global_run(
    config: "Config", active_sessions: "Sequence[Session]"
) -> bool:
    """True when a whole-repository tech-lead run is executing right now.

    Read from the launch scope the producer stamped onto the session, so the
    answer does not depend on re-reading GitHub. A session restored across a
    restart carries no stamp; it is treated as issue-scoped, which fails toward
    letting targeted work proceed rather than toward a barrier nobody can
    explain — and a GLOBAL request still deduplicates correctly in that window,
    because :meth:`TechLeadRunCoordinator.admit` additionally consults the open
    anchor issue, which is shared truth that survives the restart.
    """
    return any(
        session.tech_lead_scope is not None
        and session.tech_lead_scope.flavor
        is not TechLeadSessionFlavor.FAILURE_INVESTIGATION
        for session in active_tech_lead_sessions(config, active_sessions)
    )


class SupportsHealthReviewAnchor(Protocol):
    """The anchor lifecycle seam a global admission drives.

    Named structurally so this control owner stays decoupled from the infra
    facade: the production implementation is
    ``Orchestrator.ensure_health_review_anchor`` (discover-or-create the
    marker-labeled anchor issue and queue it), which is the SAME lifecycle the
    periodic trigger uses.
    """

    def ensure_health_review_anchor(self) -> Optional[PendingTechLeadReview]: ...


def plan_tech_lead_launch_gate(
    config: "Config",
    pending: "Sequence[PendingTechLeadReview]",
    active_sessions: "Sequence[Session]",
) -> TechLeadLaunchGate:
    """The scope-exclusivity gate over a tick's queued tech-lead runs.

    The three rules, in the order they apply:

    1. A queued global run is a BARRIER. Nothing else launches while it is
       queued, and the global run itself waits until every active tech-lead
       session has drained — that is what makes it exclusive rather than merely
       first in line.
    2. An ACTIVE global run holds everything back until it completes.
    3. Otherwise every queued targeted run is launchable; the numeric budget
       (``worker_budget.tech_lead_slot_availability``) slices it downstream,
       which is exactly why no capacity arithmetic happens here.

    A free function so the planner can consult the rule without constructing an
    admission coordinator — there is still only ONE implementation of it, which
    :meth:`TechLeadRunCoordinator.launch_gate` also delegates to.
    """
    items = tuple(pending)
    if not items:
        return TechLeadLaunchGate((), ())

    global_queued = tuple(item for item in items if is_global_pending(item))
    if global_queued:
        if active_tech_lead_sessions(config, active_sessions):
            return TechLeadLaunchGate((), items, BARRIER_GLOBAL_AWAITING_DRAIN)
        first = global_queued[0]
        held = tuple(item for item in items if item is not first)
        return TechLeadLaunchGate(
            (first,), held, BARRIER_GLOBAL_RUN_QUEUED if held else None
        )
    if has_active_global_run(config, active_sessions):
        return TechLeadLaunchGate((), items, BARRIER_GLOBAL_RUN_ACTIVE)
    return TechLeadLaunchGate(items, ())




class TechLeadRunCoordinator:
    """Sole owner of tech-lead run identity, scope conflicts, and admission.

    Constructed per call from live orchestrator dependencies (it holds no
    mutable state of its own) so a caller can never hand it a stale queue.
    """

    def __init__(
        self,
        *,
        state: "OrchestratorState",
        config: "Config",
        repository_host: "RepositoryHost",
        anchor_host: SupportsHealthReviewAnchor,
        discover_open_anchor: "Callable[[RepositoryHost, Config], Optional[int]]",
        is_blocking_any: "Callable[[Sequence[str]], bool]",
        events: "EventSink",
        claim_manager: "Optional[ClaimManager]" = None,
        now: Optional[datetime] = None,
    ) -> None:
        self._state = state
        self._config = config
        self._repository_host = repository_host
        self._anchor_host = anchor_host
        self._discover_open_anchor = discover_open_anchor
        self._is_blocking_any = is_blocking_any
        self._events = events
        self._claim_manager = claim_manager
        self._now = now

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------

    def admit(self, request: TechLeadRunRequest) -> TechLeadRunAdmission:
        """Apply the full scope-conflict matrix to one request, and record it.

        Order matters and is the policy: configuration, then engine liveness,
        then per-scope identity/eligibility, then the shared claim, then enqueue.
        Nothing downstream re-decides any of it. Every decision — admitted or
        not — emits ``TECH_LEAD_RUN_REQUESTED``, so the deferral/deduplication
        reason is machine-readable rather than log-only.
        """
        admission = self._decide(request)
        self._events.publish(
            make_trace_event(
                EventName.TECH_LEAD_RUN_REQUESTED,
                {
                    "run_key": admission.run_key,
                    "scope_kind": admission.scope_kind.value,
                    "issue_number": admission.issue_number,
                    "trigger": admission.trigger.value,
                    "outcome": admission.outcome.value,
                    "reason": admission.reason,
                    "behind_global_barrier": admission.behind_global_barrier,
                },
            )
        )
        return admission

    def _decide(self, request: TechLeadRunRequest) -> TechLeadRunAdmission:
        """The admission matrix itself, free of emission concerns."""
        if not self._config.tech_lead_review_agent:
            return self._reject(
                request,
                TechLeadRunOutcome.NOT_CONFIGURED,
                REASON_NO_TECH_LEAD_AGENT,
                "No tech lead agent is configured for this repository.",
            )
        if self._state.paused:
            return self._reject(
                request,
                TechLeadRunOutcome.PAUSED,
                REASON_ORCHESTRATOR_PAUSED,
                "The Repository Engine is paused; resume it to run tech-lead work.",
            )
        if isinstance(request.scope, IssueInvestigationScope):
            return self._admit_issue(request, request.scope)
        return self._admit_global(request)

    def _admit_issue(
        self, request: TechLeadRunRequest, scope: IssueInvestigationScope
    ) -> TechLeadRunAdmission:
        """Admit (or refuse) a focused investigation of one issue."""
        number = scope.issue_number
        running = self._active_run_for_issue(number)
        if running is not None:
            return self._reject(
                request,
                TechLeadRunOutcome.ALREADY_RUNNING,
                REASON_RUN_ACTIVE,
                f"A tech-lead session is already investigating issue #{number}.",
            )
        if any(item.issue_number == number for item in self._pending()):
            return self._existing(
                request,
                TechLeadRunOutcome.ALREADY_QUEUED,
                f"Issue #{number} is already queued for tech-lead investigation.",
            )

        failure = request.failure
        title = request.title
        if failure is None:
            # No typed context yet: this is a hand-aimed request, so the subject
            # is revalidated against GitHub RIGHT NOW and the context is built
            # from what we read. A request that already carries a failure was
            # produced by a trigger that observed it this tick — re-reading the
            # issue would buy nothing and spend a GitHub call.
            issue = self._repository_host.get_issue(number)
            if issue is None:
                return self._reject(
                    request,
                    TechLeadRunOutcome.NOT_ELIGIBLE,
                    REASON_ISSUE_NOT_FOUND,
                    f"Issue #{number} could not be read from GitHub.",
                )
            blocking = self._blocking_label(issue)
            eligibility = self._issue_eligibility(issue, blocking)
            if eligibility is not None:
                return self._reject(
                    request, TechLeadRunOutcome.NOT_ELIGIBLE, *eligibility
                )
            failure = manual_focus_failure(issue, blocking)
            title = title or issue.title

        conflict = self._peer_claim_detail(number)
        if conflict is not None:
            return self._reject(
                request, TechLeadRunOutcome.CLAIM_CONFLICT, REASON_CLAIMED_BY_PEER, conflict
            )

        outcome = self._queues().queue_failure_investigation(
            number, title, failure=failure
        )
        from .session_routing import TechLeadQueueOutcome

        if outcome is TechLeadQueueOutcome.DUPLICATE:
            return self._existing(
                request,
                TechLeadRunOutcome.ALREADY_QUEUED,
                f"Issue #{number} is already queued for tech-lead investigation.",
            )
        return self._queued(
            request,
            f"Queued a tech-lead investigation of issue #{number}.",
        )

    def _admit_global(self, request: TechLeadRunRequest) -> TechLeadRunAdmission:
        """Admit (or coalesce) the exclusive whole-board health review."""
        pending_global = next(
            (item for item in self._pending() if is_global_pending(item)), None
        )
        if pending_global is not None:
            return self._existing(
                request,
                TechLeadRunOutcome.ALREADY_QUEUED,
                "A board health review is already queued.",
                issue_number=pending_global.issue_number,
            )

        anchor = self._open_anchor_number()
        if anchor is not None and self._active_run_for_issue(anchor) is not None:
            return self._reject(
                request,
                TechLeadRunOutcome.ALREADY_RUNNING,
                REASON_RUN_ACTIVE,
                "A board health review is already running.",
                issue_number=anchor,
            )
        if has_active_global_run(self._config, self._state.active_sessions):
            return self._reject(
                request,
                TechLeadRunOutcome.ALREADY_RUNNING,
                REASON_RUN_ACTIVE,
                "A board health review is already running.",
            )
        if anchor is not None:
            conflict = self._peer_claim_detail(anchor)
            if conflict is not None:
                return self._reject(
                    request,
                    TechLeadRunOutcome.CLAIM_CONFLICT,
                    REASON_CLAIMED_BY_PEER,
                    conflict,
                    issue_number=anchor,
                )

        queued = self._anchor_host.ensure_health_review_anchor()
        if queued is None:
            return self._reject(
                request,
                TechLeadRunOutcome.FAILED,
                REASON_ANCHOR_UNAVAILABLE,
                "The health-review anchor issue could not be prepared.",
            )
        return self._queued(
            request,
            f"Queued a board health review (anchor #{queued.issue_number}).",
            issue_number=queued.issue_number,
        )

    # ------------------------------------------------------------------
    # Launch gating (the global barrier)
    # ------------------------------------------------------------------

    def launch_gate(
        self,
        pending: "Sequence[PendingTechLeadReview]",
        active_sessions: "Sequence[Session]",
    ) -> TechLeadLaunchGate:
        """Which queued runs the scope matrix lets launch this tick."""
        return plan_tech_lead_launch_gate(self._config, pending, active_sessions)

    def barrier_reason_for_new_request(self) -> Optional[str]:
        """Why a newly admitted targeted run would not start immediately."""
        if any(is_global_pending(item) for item in self._pending()):
            return BARRIER_GLOBAL_RUN_QUEUED
        if has_active_global_run(self._config, self._state.active_sessions):
            return BARRIER_GLOBAL_RUN_ACTIVE
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pending(self) -> list[PendingTechLeadReview]:
        return list(self._state.pending_tech_lead_reviews)

    def _queues(self):
        from .session_routing import PendingSessionQueues

        return PendingSessionQueues(self._state)

    def _active_run_for_issue(self, issue_number: int) -> "Optional[Session]":
        for session in active_tech_lead_sessions(
            self._config, self._state.active_sessions
        ):
            if session.issue.number == issue_number:
                return session
        return None

    def _open_anchor_number(self) -> Optional[int]:
        """The open marker-labeled anchor issue, or None.

        Shared GitHub truth (ADR-0013): it is what makes a global run's identity
        visible to a PEER orchestrator and what survives a restart, neither of
        which the in-process queue can do.
        """
        try:
            return self._discover_open_anchor(self._repository_host, self._config)
        except Exception as exc:  # pragma: no cover - transport-specific
            logger.warning(
                "[TECH_LEAD_ADMISSION] open health-review anchor lookup failed: %s",
                exc,
            )
            return None

    def _issue_eligibility(
        self, issue: "Issue", blocking: str
    ) -> Optional[tuple[str, str]]:
        """Revalidate a hand-aimed subject. None when it is still worth a run.

        The rule: the issue must be OPEN and must still carry a blocking label.
        It is re-run by every fresh request, so a queued run whose subject
        recovered is refused rather than launched. ``blocking`` is the label the
        caller already resolved — classification happens ONCE, so the verdict and
        the evidence-map context can never disagree about which label blocked it.
        """
        lifecycle = (getattr(issue, "state", "") or "").casefold()
        if lifecycle and lifecycle != "open":
            return (
                REASON_ISSUE_CLOSED,
                f"Issue #{issue.number} is closed; nothing to investigate.",
            )
        if not blocking:
            return (
                REASON_NO_LONGER_BLOCKED,
                f"Issue #{issue.number} is no longer blocked; nothing to investigate.",
            )
        return None

    def _blocking_label(self, issue: "Issue") -> str:
        """The issue's first blocking label, or "" when it carries none."""
        return next(
            (name for name in issue.labels if self._is_blocking_any([name])), ""
        )

    def _peer_claim_detail(self, issue_number: int) -> Optional[str]:
        """Detail text when a DIFFERENT orchestrator holds a live claim.

        A claim whose lease belongs to one of our own active sessions is ours,
        not a conflict. A fetch failure is not treated as a conflict: refusing
        on an unreadable claim store would make an operator's request fail for a
        transient GitHub blip, and the launch path re-verifies ownership through
        ``ClaimGate`` (which does fail closed) before any mutation.
        """
        if self._claim_manager is None:
            return None
        try:
            claim = self._claim_manager.get_current_claim(issue_number)
        except ClaimFetchError:
            logger.warning(
                "[TECH_LEAD_ADMISSION] claim lookup failed for #%d; admitting and"
                " deferring to the launch-time claim gate",
                issue_number,
            )
            return None
        if claim is None or claim.is_expired(self._now):
            return None
        own_leases = {
            session.lease_id
            for session in self._state.active_sessions
            if session.lease_id
        }
        if claim.lease_id in own_leases:
            return None
        return (
            f"Another orchestrator instance ({claim.claimant}) holds the claim on"
            f" #{issue_number}."
        )

    def _reject(
        self,
        request: TechLeadRunRequest,
        outcome: TechLeadRunOutcome,
        reason: str,
        detail: str,
        *,
        issue_number: Optional[int] = None,
    ) -> TechLeadRunAdmission:
        return TechLeadRunAdmission(
            outcome=outcome,
            scope_kind=request.scope.kind,
            run_key=request.scope.run_key,
            reason=reason,
            detail=detail,
            trigger=request.trigger,
            issue_number=issue_number or request.scope.subject_issue_number,
        )

    def _existing(
        self,
        request: TechLeadRunRequest,
        outcome: TechLeadRunOutcome,
        detail: str,
        *,
        issue_number: Optional[int] = None,
    ) -> TechLeadRunAdmission:
        return replace(
            self._reject(
                request,
                outcome,
                REASON_DUPLICATE_REQUEST,
                detail,
                issue_number=issue_number,
            ),
            behind_global_barrier=self.barrier_reason_for_new_request() is not None,
        )

    def _queued(
        self,
        request: TechLeadRunRequest,
        detail: str,
        *,
        issue_number: Optional[int] = None,
    ) -> TechLeadRunAdmission:
        barrier = self.barrier_reason_for_new_request()
        return TechLeadRunAdmission(
            outcome=TechLeadRunOutcome.QUEUED,
            scope_kind=request.scope.kind,
            run_key=request.scope.run_key,
            reason=REASON_ADMITTED,
            detail=detail,
            trigger=request.trigger,
            issue_number=issue_number or request.scope.subject_issue_number,
            behind_global_barrier=(
                barrier is not None
                and request.scope.kind is TechLeadRunScopeKind.ISSUE
            ),
        )


# Blocking-label context for a manual investigation of an issue that carries no
# recognisable blocking label. It rides along as evidence-map context only; the
# failure reason is always ``timed_out`` so the reaction model investigates a
# leaf issue regardless of the label.
MANUAL_TECH_LEAD_LABEL = "manual-tech-lead"


def manual_focus_failure(
    issue: "Issue", blocking_label: str, observed_at: float = 0.0
) -> DiscoveredFailure:
    """The typed failure context a hand-aimed investigation carries.

    Single owner for the shape (the one-shot CLI dispatch delegates here):
    ``failure_reason`` is always ``timed_out`` — never ``blocked`` — so the
    reaction model INVESTIGATES a leaf issue instead of treating it as healthy
    waiting, and the issue's real terminal label rides along in
    ``blocking_label`` as evidence-map context. The queue item is the only
    carrier of this context once the per-tick discovery buffer is cleared, so
    every trigger path must hand the launch boundary the same shape.
    """
    return DiscoveredFailure(
        issue_number=issue.number,
        issue_title=issue.title,
        failure_reason=SessionStatus.TIMED_OUT.value,
        blocking_label=blocking_label or MANUAL_TECH_LEAD_LABEL,
        issue_body=issue.body or "",
        issue_milestone=issue.milestone,
        observed_at=observed_at,
        artifact_hints=(),
    )
