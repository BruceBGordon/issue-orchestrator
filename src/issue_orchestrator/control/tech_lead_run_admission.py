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

One request, one verdict:

* :meth:`TechLeadRunCoordinator.admit` answers a REQUEST — identity/dedup,
  eligibility revalidation, cross-instance claim conflict, and enqueue — and
  returns a typed :class:`TechLeadRunAdmission` the caller reports verbatim.
* :mod:`.tech_lead_launch_planning` answers the TICK's question — which queued
  runs may launch right now, and which are no longer worth launching at all.

Deliberate boundaries:

* **Capacity is not re-decided here.** ``tech_lead_slot_availability`` remains
  the single owner of the numeric budget; this owner decides only SEMANTIC
  conflicts, so the two cannot drift.
* **Shared truth, not process-local truth.** Before a request is answered
  ``queued``, this owner ATOMICALLY takes the logical run through
  :class:`.tech_lead_run_ownership.TechLeadRunOwnership` (ADR-0033 compare-and-
  swap, keyed by ``run_key``). Reading shared state and then appending locally
  is a check-then-act gap two engines can both pass, which is how the same run
  used to be admitted twice and both callers told "queued" (#6994 round 1 F1).
  Ownership is taken BEFORE the health-review anchor is created, so the
  scan-then-create gap closes with it.
* **No launching.** Admission ENQUEUES; the planner still launches, so a manual
  request rides the identical evidence/authority path an automatic one does.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Callable, Optional, Protocol, Sequence

from ..domain.models import DiscoveredFailure, PendingTechLeadReview, SessionStatus
from ..domain.tech_lead_run import (
    BARRIER_GLOBAL_RUN_ACTIVE,
    BARRIER_GLOBAL_RUN_QUEUED,
    REASON_ADMITTED,
    REASON_ANCHOR_UNAVAILABLE,
    REASON_CLAIMED_BY_PEER,
    REASON_DUPLICATE_REQUEST,
    REASON_ISSUE_NOT_FOUND,
    REASON_NO_ON_DEMAND_ANCHOR,
    REASON_NO_TECH_LEAD_AGENT,
    REASON_ORCHESTRATOR_PAUSED,
    REASON_RUN_ACTIVE,
    REASON_RUN_CLAIM_UNAVAILABLE,
    REASON_TECH_LEAD_DISABLED,
    IssueInvestigationScope,
    TechLeadRunAdmission,
    TechLeadRunOutcome,
    TechLeadRunRequest,
    TechLeadRunScope,
    TechLeadRunScopeKind,
    global_scope_for_flavor,
)
from ..domain.tech_lead_session import TechLeadSessionFlavor
from ..events import EventName
from ..ports import make_trace_event
from .tech_lead_run_ownership import (
    RunOwnershipReconciliation,
    RunOwnershipVerdict,
    RunRelease,
    TechLeadRunOwnership,
)
from .tech_lead_session_policy import is_tech_lead_session

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session
    from ..infra.config import Config
    from ..ports import EventSink, Issue, RepositoryHost

logger = logging.getLogger(__name__)


def scope_of_pending(item: PendingTechLeadReview) -> TechLeadRunScope:
    """The scope a queued item runs at, derived from its declared flavor.

    Health reviews AND batch reviews audit the whole repository (one walks the
    board, the other the accumulated PR manifest), so both are global — but they
    are DIFFERENT global identities, not one bucket (#6994 round 1 F2). Both are
    exclusive of every other run; neither deduplicates against the other.
    """
    if item.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        return IssueInvestigationScope(item.issue_number)
    return global_scope_for_flavor(item.flavor)


def is_global_pending(item: PendingTechLeadReview) -> bool:
    """True when a queued item holds an exclusive whole-repository scope."""
    return scope_of_pending(item).kind.is_global


def run_key_of_pending(item: PendingTechLeadReview) -> str:
    """The logical run identity of a queued item."""
    return scope_of_pending(item).run_key


def scope_of_session(session: "Session") -> Optional[TechLeadRunScope]:
    """The scope an ACTIVE tech-lead session is running at, or None.

    ``None`` only when the session carries no launch stamp at all, which after
    #6994 round 1 F3 means a session whose flavor could not be recovered — not
    the routine restart case, which now restores the stamp from marker truth.
    """
    scope = session.tech_lead_scope
    if scope is None:
        return None
    if scope.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        return IssueInvestigationScope(session.issue.number)
    return global_scope_for_flavor(scope.flavor)


def live_run_scopes(
    config: "Config",
    pending: "Sequence[PendingTechLeadReview]",
    active_sessions: "Sequence[Session]",
) -> tuple[TechLeadRunScope, ...]:
    """Every logical run this engine currently has queued or running.

    The input to :meth:`TechLeadRunOwnership.reconcile`: ownership must cover a
    run for its WHOLE life, and a run's life spans the queue and the session, so
    neither collection alone is the answer. Scopes rather than bare keys,
    because the shared ledger judges the CONFLICT MATRIX and cannot do that
    without knowing which runs are whole-repository ones.
    """
    scopes: dict[str, TechLeadRunScope] = {
        run_key_of_pending(item): scope_of_pending(item) for item in pending
    }
    for session in active_tech_lead_sessions(config, active_sessions):
        scope = scope_of_session(session)
        if scope is not None:
            scopes.setdefault(scope.run_key, scope)
    return tuple(scopes[key] for key in sorted(scopes))


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

    Read from the launch scope stamped onto the session. That stamp is present
    on a RESTORED session too (``SessionRestorer`` rebuilds it from the anchor's
    marker label and the durable authority ledger — #6994 round 1 F3), so a
    global run survives a restart as a barrier instead of silently becoming
    issue-scoped and letting targeted work run alongside it.

    A tech-lead session with no stamp at all is a session whose flavor could not
    be established. It is treated as GLOBAL — the conservative direction: the
    cost of being wrong is a targeted run waiting a while, whereas failing the
    other way runs work concurrently with an exclusive review.
    """
    return any(
        session.tech_lead_scope is None
        or session.tech_lead_scope.flavor
        is not TechLeadSessionFlavor.FAILURE_INVESTIGATION
        for session in active_tech_lead_sessions(config, active_sessions)
    )


# Operator-facing name of each whole-repository run. One map so the admission
# detail text, the launch log, and the dashboard all call the same run the same
# thing rather than each inventing a phrase.
_GLOBAL_RUN_LABELS: dict[TechLeadRunScopeKind, str] = {
    TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW: "board health review",
    TechLeadRunScopeKind.GLOBAL_BATCH_REVIEW: "batch review",
}


def _scope_phrase(scope: TechLeadRunScope) -> str:
    """How a scope is named in operator-facing detail text."""
    if scope.kind.is_global:
        return f"A {_GLOBAL_RUN_LABELS[scope.kind]}"
    return f"A tech-lead investigation of issue #{scope.subject_issue_number}"


def _already_running_detail(scope: TechLeadRunScope) -> str:
    return f"{_scope_phrase(scope)} is already running."


def _already_queued_detail(scope: TechLeadRunScope) -> str:
    return f"{_scope_phrase(scope)} is already queued."


class SupportsHealthReviewAnchor(Protocol):
    """The anchor lifecycle seam a global admission drives.

    Named structurally so this control owner stays decoupled from the infra
    facade: the production implementation is
    ``Orchestrator.ensure_health_review_anchor`` (discover-or-create the
    marker-labeled anchor issue and queue it), which is the SAME lifecycle the
    periodic trigger uses.
    """

    def ensure_health_review_anchor(self) -> Optional[PendingTechLeadReview]: ...


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
        ownership: "TechLeadRunOwnership",
        is_blocking_any: "Callable[[Sequence[str]], bool]",
        events: "EventSink",
    ) -> None:
        self._state = state
        self._config = config
        self._repository_host = repository_host
        self._anchor_host = anchor_host
        self._ownership = ownership
        self._is_blocking_any = is_blocking_any
        self._events = events

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
        if not self._config.tech_lead_enabled:
            explicitly_disabled = self._config.tech_lead_explicitly_disabled
            return self._reject(
                request,
                TechLeadRunOutcome.NOT_CONFIGURED,
                REASON_TECH_LEAD_DISABLED
                if explicitly_disabled
                else REASON_NO_TECH_LEAD_AGENT,
                "Tech lead is disabled for this repository."
                if explicitly_disabled
                else "No tech lead agent is configured for this repository.",
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
        return self._admit_global(request, request.scope)

    def _admit_issue(
        self, request: TechLeadRunRequest, scope: IssueInvestigationScope
    ) -> TechLeadRunAdmission:
        """Admit (or refuse) a focused investigation of one issue."""
        number = scope.issue_number
        existing = self._existing_run_for_scope(request, scope)
        if existing is not None:
            return existing

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

        conflict = self._own_run_or_conflict(request, scope)
        if conflict is not None:
            return conflict

        outcome = self._queues().queue_failure_investigation(
            number, title, failure=failure
        )
        from .pending_session_queues import TechLeadQueueOutcome

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

    def _admit_global(
        self, request: TechLeadRunRequest, scope: TechLeadRunScope
    ) -> TechLeadRunAdmission:
        """Admit (or coalesce) one exclusive whole-repository review.

        Deduplication is per FLAVOR, never "any global run": a queued health
        review must not swallow a batch-review request, and vice versa. Two
        different global flavors are two runs that SERIALIZE — the launch gate
        makes the second wait — which is what the operator asked for, rather
        than one of the two silently disappearing (#6994 round 1 F2).
        """
        label = _GLOBAL_RUN_LABELS[scope.kind]
        existing = self._existing_run_for_scope(request, scope)
        if existing is not None:
            return existing
        if scope.kind is not TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW:
            # A batch review's anchor is authored by the PR-manifest threshold
            # path, which owns when a batch is worth auditing. Admission models
            # it as a distinct global identity (so it dedups and serializes
            # correctly) but does not manufacture one on request.
            return self._reject(
                request,
                TechLeadRunOutcome.NOT_ELIGIBLE,
                REASON_NO_ON_DEMAND_ANCHOR,
                f"A {label} is created from the PR-manifest threshold, not on"
                " demand; there is no batch anchor to run.",
            )

        # Ownership FIRST, then create. The old order — scan for an open anchor,
        # then create one if absent — is a check-then-act gap two engines can
        # both pass before either writes, which is precisely how a repository
        # ends up with two anchors and two "queued" answers (round 1 F1).
        conflict = self._own_run_or_conflict(request, scope)
        if conflict is not None:
            return conflict

        queued = self._anchor_host.ensure_health_review_anchor()
        if queued is None:
            self._ownership.release(scope.run_key)
            return self._reject(
                request,
                TechLeadRunOutcome.FAILED,
                REASON_ANCHOR_UNAVAILABLE,
                f"The {label} anchor issue could not be prepared.",
            )
        return self._queued(
            request,
            f"Queued a {label} (anchor #{queued.issue_number}).",
            issue_number=queued.issue_number,
        )

    # ------------------------------------------------------------------
    # Identity: does this logical run already exist, and may we own it?
    # ------------------------------------------------------------------

    def _existing_run_for_scope(
        self, request: TechLeadRunRequest, scope: TechLeadRunScope
    ) -> Optional[TechLeadRunAdmission]:
        """The coalescing answer when THIS engine already has the run, else None.

        Checked before any shared write so repeated clicks cost nothing: the
        run already exists here, so there is no race left to arbitrate.
        """
        running = self._active_session_for_run(scope.run_key)
        if running is not None:
            return self._reject(
                request,
                TechLeadRunOutcome.ALREADY_RUNNING,
                REASON_RUN_ACTIVE,
                _already_running_detail(scope),
                issue_number=running.issue.number,
            )
        queued = next(
            (
                item
                for item in self._pending()
                if run_key_of_pending(item) == scope.run_key
            ),
            None,
        )
        if queued is not None:
            return self._existing(
                request,
                TechLeadRunOutcome.ALREADY_QUEUED,
                _already_queued_detail(scope),
                issue_number=queued.issue_number,
            )
        return None

    def _own_run_or_conflict(
        self, request: TechLeadRunRequest, scope: TechLeadRunScope
    ) -> Optional[TechLeadRunAdmission]:
        """Atomically take the logical run, or return the typed refusal.

        Fails CLOSED when the coordination store cannot be reached. Admitting on
        ignorance is exactly what produces the duplicate run this step exists to
        prevent, and an operator who is told "could not establish ownership" can
        retry, whereas one told "queued" cannot discover it was not.
        """
        ownership = self._ownership.claim(scope)
        if ownership.owned:
            return None
        if ownership.verdict is RunOwnershipVerdict.HELD_BY_PEER:
            return self._reject(
                request,
                TechLeadRunOutcome.CLAIM_CONFLICT,
                REASON_CLAIMED_BY_PEER,
                ownership.detail,
            )
        return self._reject(
            request,
            TechLeadRunOutcome.FAILED,
            REASON_RUN_CLAIM_UNAVAILABLE,
            ownership.detail,
        )

    # ------------------------------------------------------------------
    # Ownership lifecycle
    # ------------------------------------------------------------------

    def reconcile_ownership(self) -> "RunOwnershipReconciliation":
        """Renew/settle leases for every run this engine has live (one per tick).

        Returns the TYPED per-run outcome rather than a flat "lost" list: the
        caller must treat contention (retain and retry) differently from
        definitive loss (withdraw / stop the session) and from an unreadable
        store (change nothing), and a flat list cannot express that (round 2 F4).
        """
        return self._ownership.reconcile(
            live_run_scopes(
                self._config,
                self._state.pending_tech_lead_reviews,
                self._state.active_sessions,
            )
        )

    def release_run(self, run_key: str) -> "RunRelease":
        """Hand a finished or withdrawn run back to the shared store.

        The typed result is passed through rather than swallowed: an
        unavailable coordination store means the hold is still live, and only
        the caller can decide whether that matters to what it is reporting.
        """
        return self._ownership.release(run_key)

    # ------------------------------------------------------------------
    # The global barrier, as it applies to a NEW request
    # ------------------------------------------------------------------

    def barrier_reason_for_new_request(
        self, scope: Optional[TechLeadRunScope] = None
    ) -> Optional[str]:
        """Why a newly admitted run would not start immediately.

        ``scope`` is excluded from its own barrier: a queued global run is not
        waiting behind itself, but a SECOND global flavor genuinely is.
        """
        run_key = scope.run_key if scope is not None else None
        if any(
            is_global_pending(item) and run_key_of_pending(item) != run_key
            for item in self._pending()
        ):
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
        from .pending_session_queues import PendingSessionQueues

        return PendingSessionQueues(self._state)

    def _active_session_for_run(self, run_key: str) -> "Optional[Session]":
        """The active tech-lead session executing this logical run, if any."""
        for session in active_tech_lead_sessions(
            self._config, self._state.active_sessions
        ):
            scope = scope_of_session(session)
            if scope is not None and scope.run_key == run_key:
                return session
        return None

    def _issue_eligibility(
        self, issue: "Issue", blocking: str
    ) -> Optional[tuple[str, str]]:
        """Revalidate a hand-aimed subject. None when it is still worth a run.

        The SAME rule launch-time revalidation applies, imported from its owner
        so a request and a launch can never disagree about eligibility.
        """
        from .tech_lead_launch_planning import issue_run_eligibility

        return issue_run_eligibility(issue, blocking)

    def _blocking_label(self, issue: "Issue") -> str:
        """The issue's first blocking label, or "" when it carries none."""
        return next(
            (name for name in issue.labels if self._is_blocking_any([name])), ""
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
            behind_global_barrier=(
                self.barrier_reason_for_new_request(request.scope) is not None
            ),
        )

    def _queued(
        self,
        request: TechLeadRunRequest,
        detail: str,
        *,
        issue_number: Optional[int] = None,
    ) -> TechLeadRunAdmission:
        return TechLeadRunAdmission(
            outcome=TechLeadRunOutcome.QUEUED,
            scope_kind=request.scope.kind,
            run_key=request.scope.run_key,
            reason=REASON_ADMITTED,
            detail=detail,
            trigger=request.trigger,
            issue_number=issue_number or request.scope.subject_issue_number,
            # A second GLOBAL flavor waits behind the first exactly as targeted
            # work does, so the barrier flag is asked about every scope rather
            # than assumed false for global runs.
            behind_global_barrier=(
                self.barrier_reason_for_new_request(request.scope) is not None
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
