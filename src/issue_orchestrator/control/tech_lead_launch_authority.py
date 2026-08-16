"""The ONE place a tech-lead session may start (#6994 round 2 F2 / A2).

Admission decides that a run *should* exist. The planner decides which queued
runs *look* launchable this tick. Neither is authority to START one, and before
this owner existed the difference did not matter because there was no single
gate: the reactive tick reached the launch through the planner (which consulted
the scope gate against a snapshot taken BEFORE that same tick's actions ran),
while the one-shot CLI called ``launch_tech_lead_session`` directly and consulted
nothing. Two paths, two authorities, one bypassable invariant.

So every path now funnels through :meth:`TechLeadLaunchAuthority.launch`, and it
re-decides everything that could have changed since planning, in the order that
makes each answer cheap before the expensive one:

1. **Subject eligibility**, re-asked against GitHub right now. A run can wait
   many ticks behind the global barrier and its subject can be closed, unblocked
   or (for a whole-repository review) already completed by a peer in that
   window. A focused investigation withdraws only on POSITIVE evidence; a global
   run fails CLOSED and must prove its anchor is still open (round 2 F9).
2. **Scope exclusivity against LIVE state** — this engine's pending queue and
   active sessions as they are at this instant, not as a plan-time snapshot
   remembered them. That closes the within-tick bypass where a tick creates a
   global anchor and then executes an already-planned targeted launch.
3. **The shared run ledger** — the atomic, cross-engine half of the same rule
   (:meth:`TechLeadRunOwnership.begin_run`). Local state cannot see a peer's
   runs, so the local gate alone is an optimisation, never the boundary.

Only then does the real launch run. A refusal leaves the run QUEUED (it retries
next tick) except when revalidation withdrew it, and a launch that fails to
produce a session hands the ledger entry straight back so a peer is not made to
wait out a lease for work that never started.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from ..domain.models import PendingTechLeadReview
from ..domain.tech_lead_run import (
    REASON_ANCHOR_CLOSED,
    REASON_ANCHOR_UNREADABLE,
    REASON_CLAIMED_BY_PEER,
    REASON_NO_TECH_LEAD_AGENT,
    REASON_RUN_CLAIM_UNAVAILABLE,
    REASON_TECH_LEAD_DISABLED,
    TechLeadRunScope,
)
from ..events import EventName
from ..ports import make_trace_event
from .tech_lead_launch_planning import (
    issue_run_eligibility,
    plan_tech_lead_launch_gate,
)
from .tech_lead_run_admission import scope_of_pending
from .tech_lead_run_ownership import RunExecutionVerdict

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session
    from ..infra.config import Config
    from ..ports import EventSink, Issue, RepositoryHost
    from .tech_lead_run_activity import TechLeadRunActivity
    from .tech_lead_run_ownership import TechLeadRunOwnership

logger = logging.getLogger(__name__)

# Why the launch authority refused, beyond the barrier reasons the launch gate
# already names. Machine-readable, because the dashboard and the launch log both
# branch on them.
REASON_LAUNCH_SCOPE_BARRIER = "scope_barrier"
REASON_LAUNCH_NOT_OWNED = "run_not_owned"


@dataclass(frozen=True, slots=True)
class TechLeadLaunchRefusal:
    """Why a queued run did not start, and whether it survives the refusal."""

    run_key: str
    issue_number: int
    reason: str
    detail: str
    # False when the run was REMOVED rather than held: its subject stopped being
    # worth investigating, so retrying it next tick would be wrong.
    retained: bool = True


class TechLeadLaunchAuthority:
    """Final authority over starting a tech-lead session.

    Constructed per launch from live orchestrator state (it holds nothing of its
    own), so a caller can never hand it a stale queue — which is the exact
    failure this owner exists to remove.
    """

    def __init__(
        self,
        *,
        state: "OrchestratorState",
        config: "Config",
        ownership: "TechLeadRunOwnership",
        repository_host: "Optional[RepositoryHost]",
        is_blocking_any: "Callable[[Sequence[str]], bool]",
        events: "EventSink",
        launch: "Callable[[PendingTechLeadReview], Optional[Session]]",
        activity: "TechLeadRunActivity",
    ) -> None:
        self._state = state
        self._config = config
        self._ownership = ownership
        self._repository_host = repository_host
        self._is_blocking_any = is_blocking_any
        self._events = events
        self._launch = launch
        self._activity = activity

    def launch(self, tech_lead: PendingTechLeadReview) -> "Optional[Session]":
        """Start ``tech_lead`` if — and only if — it may still start."""
        scope = scope_of_pending(tech_lead)
        refusal = self._refusal_for(tech_lead, scope)
        if refusal is not None:
            self._report(refusal)
            if not refusal.retained:
                self._withdraw(tech_lead, refusal)
            return None
        session = self._launch(tech_lead)
        if session is None:
            # Nothing is executing this run, so the exclusive hold must go back
            # immediately; leaving it would make every other tech-lead run wait
            # out a lease for a session that never started.
            self._ownership.end_run(scope.run_key)
            return None
        # ADR-0033: the run's LOCAL record opens here, at the same single gate
        # that guarantees one session per run — so the history can never show a
        # run this authority refused, nor miss one it allowed (#6858).
        self._activity.note_started(session)
        return session

    # ------------------------------------------------------------------
    # The three gates, cheapest local evidence first
    # ------------------------------------------------------------------

    def _refusal_for(
        self, tech_lead: PendingTechLeadReview, scope: TechLeadRunScope
    ) -> Optional[TechLeadLaunchRefusal]:
        if not self._config.tech_lead_enabled:
            explicitly_disabled = self._config.tech_lead_explicitly_disabled
            return TechLeadLaunchRefusal(
                scope.run_key,
                tech_lead.issue_number,
                REASON_TECH_LEAD_DISABLED
                if explicitly_disabled
                else REASON_NO_TECH_LEAD_AGENT,
                "Tech lead is disabled for this repository."
                if explicitly_disabled
                else "No tech lead agent is configured for this repository.",
            )
        withdrawal = self._revalidate_subject(tech_lead, scope)
        if withdrawal is not None:
            return withdrawal
        barrier = self._local_scope_barrier(tech_lead)
        if barrier is not None:
            # The gate's own barrier vocabulary is the reason, so a local
            # refusal and a peer-induced one read identically to an operator.
            return TechLeadLaunchRefusal(
                scope.run_key,
                tech_lead.issue_number,
                barrier,
                f"Held by tech-lead scope exclusivity ({barrier}).",
            )
        return self._shared_execution_refusal(tech_lead, scope)

    def _revalidate_subject(
        self, tech_lead: PendingTechLeadReview, scope: TechLeadRunScope
    ) -> Optional[TechLeadLaunchRefusal]:
        """Re-ask whether this run's subject is still worth a session.

        The two scopes get OPPOSITE benefit of the doubt, because their failure
        costs are opposite:

        * a focused investigation withdraws only on POSITIVE evidence — an
          unreadable subject proves nothing, and turning a transient GitHub
          failure into a cancelled investigation is worse than one extra run;
        * a whole-repository review fails CLOSED — it must prove its anchor is
          still open before starting, or a recovered copy relaunches an audit a
          peer engine already finished (round 2 F9).
        """
        if self._repository_host is None:
            return None
        if scope.kind.is_global:
            return self._revalidate_anchor(tech_lead, scope)
        issue = self._read_issue(tech_lead.issue_number)
        if issue is None:
            return None
        blocking = next(
            (name for name in issue.labels if self._is_blocking_any([name])), ""
        )
        verdict = issue_run_eligibility(issue, blocking)
        if verdict is None:
            return None
        return TechLeadLaunchRefusal(
            scope.run_key,
            tech_lead.issue_number,
            verdict[0],
            verdict[1],
            retained=False,
        )

    def _revalidate_anchor(
        self, tech_lead: PendingTechLeadReview, scope: TechLeadRunScope
    ) -> Optional[TechLeadLaunchRefusal]:
        """Prove the whole-repository run's anchor is still open and runnable.

        Every engine requeues the same open anchor at startup, and a contended
        copy is deliberately RETAINED rather than withdrawn (round 2 F4). That
        retention is only safe if the loser re-checks the durable anchor before
        launching: when the winner finishes it CLOSES the anchor and releases
        the ledger hold, at which point the loser would otherwise acquire the
        now-free run and start a second session from its stale in-memory copy.

        The lease alone cannot express this — it says who may run, not whether
        the logical run is already done — so the durable anchor is the authority
        (round 2 A6).
        """
        issue = self._read_issue(tech_lead.issue_number)
        if issue is None:
            return TechLeadLaunchRefusal(
                scope.run_key,
                tech_lead.issue_number,
                REASON_ANCHOR_UNREADABLE,
                (
                    f"The {scope.kind.value} anchor #{tech_lead.issue_number}"
                    " could not be read; holding the run rather than starting a"
                    " whole-repository review we cannot vouch for."
                ),
            )
        lifecycle = (getattr(issue, "state", "") or "").casefold()
        if lifecycle and lifecycle != "open":
            return TechLeadLaunchRefusal(
                scope.run_key,
                tech_lead.issue_number,
                REASON_ANCHOR_CLOSED,
                (
                    f"Anchor #{tech_lead.issue_number} is closed; this"
                    " whole-repository review has already been completed."
                ),
                retained=False,
            )
        return None

    def _local_scope_barrier(self, tech_lead: PendingTechLeadReview) -> Optional[str]:
        """The scope gate, re-applied to state as it is at THIS instant.

        The planner asks the same question of a plan-time snapshot. Asking again
        here is what stops a tick that queued a global anchor from then running
        an already-planned targeted launch out of the stale snapshot.
        """
        gate = plan_tech_lead_launch_gate(
            self._config,
            list(self._state.pending_tech_lead_reviews),
            list(self._state.active_sessions),
        )
        if any(item is tech_lead for item in gate.launchable):
            return None
        if any(
            item.issue_number == tech_lead.issue_number
            and item.flavor is tech_lead.flavor
            for item in gate.launchable
        ):
            return None
        return gate.barrier_reason or REASON_LAUNCH_SCOPE_BARRIER

    def _shared_execution_refusal(
        self, tech_lead: PendingTechLeadReview, scope: TechLeadRunScope
    ) -> Optional[TechLeadLaunchRefusal]:
        """The atomic cross-engine half of the same exclusivity rule."""
        admission = self._ownership.begin_run(scope)
        if admission.started:
            return None
        if admission.verdict is RunExecutionVerdict.BARRIER:
            return TechLeadLaunchRefusal(
                scope.run_key,
                tech_lead.issue_number,
                admission.barrier_reason or REASON_LAUNCH_SCOPE_BARRIER,
                admission.detail,
            )
        if admission.verdict is RunExecutionVerdict.UNAVAILABLE:
            return TechLeadLaunchRefusal(
                scope.run_key,
                tech_lead.issue_number,
                REASON_RUN_CLAIM_UNAVAILABLE,
                admission.detail,
            )
        return TechLeadLaunchRefusal(
            scope.run_key,
            tech_lead.issue_number,
            REASON_CLAIMED_BY_PEER if admission.holder else REASON_LAUNCH_NOT_OWNED,
            admission.detail,
        )

    # ------------------------------------------------------------------
    # Consequences
    # ------------------------------------------------------------------

    def _withdraw(
        self, tech_lead: PendingTechLeadReview, refusal: TechLeadLaunchRefusal
    ) -> None:
        """Remove a run whose subject stopped being worth investigating."""
        from .pending_session_queues import PendingSessionQueues

        PendingSessionQueues(self._state).remove_tech_lead(tech_lead.issue_number)
        self._ownership.release(refusal.run_key)
        self._events.publish(
            make_trace_event(
                EventName.TECH_LEAD_RUN_WITHDRAWN,
                {
                    "run_key": refusal.run_key,
                    "issue_number": refusal.issue_number,
                    "reason": refusal.reason,
                    "detail": refusal.detail,
                },
            )
        )

    def _report(self, refusal: TechLeadLaunchRefusal) -> None:
        logger.info(
            "[TECH_LEAD_RUN] Not launching %s (#%d): %s (%s)",
            refusal.run_key,
            refusal.issue_number,
            refusal.reason,
            refusal.detail,
        )
        self._events.publish(
            make_trace_event(
                EventName.TECH_LEAD_RUN_HELD,
                {
                    "run_key": refusal.run_key,
                    "issue_number": refusal.issue_number,
                    "reason": refusal.reason,
                    "detail": refusal.detail,
                    "retained": refusal.retained,
                },
            )
        )

    def _read_issue(self, number: int) -> "Optional[Issue]":
        """The subject, or None when it cannot be read.

        One targeted read per launch, not a scan: tech-lead launches are rare,
        and the alternative — trusting a plan-time snapshot — is exactly the
        staleness this owner exists to remove.
        """
        assert self._repository_host is not None
        try:
            return self._repository_host.get_issue(number)
        except Exception as exc:  # pragma: no cover - transport specific
            logger.warning(
                "[TECH_LEAD_RUN] Could not revalidate subject #%d before launch:"
                " %s; launching on the evidence we have",
                number,
                exc,
            )
            return None


__all__ = [
    "REASON_LAUNCH_NOT_OWNED",
    "REASON_LAUNCH_SCOPE_BARRIER",
    "TechLeadLaunchAuthority",
    "TechLeadLaunchRefusal",
]
