"""The owner of a tech-lead run's LOCAL record (ADR-0033 / #6858).

Three owners already touch a tech-lead run's life and none of them remembers
it: :class:`..control.tech_lead_run_admission.TechLeadRunCoordinator` decides a
run should exist, :class:`..control.tech_lead_launch_authority.TechLeadLaunchAuthority`
is the single gate a session may start behind, and
:class:`..control.tech_lead_run_ownership.TechLeadRunOwnership` coordinates who
owns it across engines. This owner is the fourth question — *what happened* —
and it is deliberately separate from all three, because a receipt written by a
decision-maker is a decision-maker with a second job.

Two seams, one on each side of a session's life:

* :meth:`TechLeadRunActivity.note_started` — called by the launch authority the
  instant a session exists, so the record is opened by the same gate that
  guarantees at most one session per run. Recording it anywhere earlier (at
  admission, say) would mint a record for every queued run that never starts.
* :meth:`TechLeadRunActivity.note_concluded` — called from completion
  finalization, the terminal seam every status funnels through, beside the
  retention owner that drops the run's launch authority there.

Everything this owner writes is best-effort by contract (see
:mod:`...ports.tech_lead_run_record_store`): a tech-lead run's product is its
proposals, and losing the receipt must never lose the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from ..domain.models import SessionStatus
from ..domain.tech_lead_run_record import TechLeadRunPhase, TechLeadRunRecord
from .publish_recovery import is_publish_failure
from .tech_lead_run_admission import scope_of_session
from .tech_lead_session_policy import is_tech_lead_session

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..infra.config import Config
    from ..ports.tech_lead_run_record_store import TechLeadRunRecordStore

logger = logging.getLogger(__name__)

# What each terminal session status means for the RUN. One table, because the
# question "did the tech lead conclude, escalate, fail, or get stopped?" is
# asked once and answered here — a branch chain at the call site is how the
# same status ends up rendered two different ways on two surfaces.
#
# A non-terminal status maps to nothing: the run is still going, so its record
# stays RUNNING rather than being concluded and then contradicted.
_PHASE_BY_STATUS: dict[SessionStatus, Optional[TechLeadRunPhase]] = {
    SessionStatus.COMPLETED: TechLeadRunPhase.COMPLETED,
    SessionStatus.BLOCKED: TechLeadRunPhase.NEEDS_HUMAN,
    SessionStatus.NEEDS_HUMAN: TechLeadRunPhase.NEEDS_HUMAN,
    SessionStatus.FAILED: TechLeadRunPhase.FAILED,
    SessionStatus.TIMED_OUT: TechLeadRunPhase.FAILED,
    SessionStatus.VALIDATION_FAILED: TechLeadRunPhase.FAILED,
    SessionStatus.PENDING: None,
    SessionStatus.RUNNING: None,
    SessionStatus.NEEDS_VALIDATION_RETRY: None,
}

_PHASE_DETAIL: dict[TechLeadRunPhase, str] = {
    TechLeadRunPhase.COMPLETED: "Decision recorded.",
    TechLeadRunPhase.NEEDS_HUMAN: "Escalated for a human decision.",
    TechLeadRunPhase.FAILED: "The session ended without a usable decision.",
    TechLeadRunPhase.WITHDRAWN: "Stopped before it reached a decision.",
}


class TechLeadRunActivity:
    """This engine's memory of the tech-lead runs it has executed."""

    def __init__(
        self,
        store: "TechLeadRunRecordStore",
        *,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._store = store
        self._now = now or datetime.now

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def note_started(self, session: "Session") -> None:
        """Open the record for a tech-lead session that has just started.

        A session with no launch stamp is not recorded rather than recorded as
        an unknown run: the stamp is what names the logical run, and a history
        row that cannot say which run it belongs to is noise, not evidence.
        """
        scope = scope_of_session(session)
        if scope is None:
            logger.debug(
                "[TECH_LEAD_RUN] Session %s carries no tech-lead launch scope;"
                " not recording it as a run",
                session.terminal_id,
            )
            return
        self._store.open_run(
            TechLeadRunRecord(
                run_key=scope.run_key,
                scope_kind=scope.kind,
                flavor=scope.flavor,
                phase=TechLeadRunPhase.RUNNING,
                started_at=session.started_at,
                run_id=session.run_assets.run_id,
                session_name=session.run_assets.session_name,
                # A REFERENCE to the GitHub object the run was coordinated
                # through, whether that is the focus issue or the anchor.
                subject_issue_number=session.issue.number,
                subject_title=session.issue.title,
            )
        )

    def note_concluded(
        self,
        config: "Config",
        session: "Session",
        status: SessionStatus,
        *,
        processing_errors: Optional[list[str]] = None,
    ) -> None:
        """Close the record for a finishing tech-lead session.

        A COMPLETED session that failed contract processing is recorded as
        FAILED, not completed: the orchestrator rejected its decision, so the
        run produced nothing, and saying otherwise on the activity surface
        would contradict the actions the same completion planned.

        A publish-stage failure is NOT a conclusion, exactly as it is not one
        for the launch-authority retention owner alongside this call: the retry
        re-enters completion for the same session run, so concluding now would
        publish a verdict the retry is about to overturn.
        """
        if not is_tech_lead_session(
            config.tech_lead_review_agent, session.issue.agent_type
        ):
            return
        if is_publish_failure(processing_errors):
            return
        phase = _PHASE_BY_STATUS[status]
        if phase is None:
            return
        if phase is TechLeadRunPhase.COMPLETED and processing_errors:
            phase = TechLeadRunPhase.FAILED
        outcome = self._read_decision_outcome(session)
        self._store.conclude_run(
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
            phase=phase,
            ended_at=self._now(),
            detail=outcome.detail or _PHASE_DETAIL[phase],
            findings=outcome.findings,
            proposals=outcome.proposals,
        )

    def note_withdrawn(self, session: "Session") -> None:
        """Close the record for a run stopped before it reached a decision.

        The one-shot timeout path terminates a session and then runs no further
        tick, so without this the record would sit at RUNNING forever — the
        activity surface's version of the leaked lease the termination owner
        already guards against.
        """
        self._store.conclude_run(
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
            phase=TechLeadRunPhase.WITHDRAWN,
            ended_at=self._now(),
            detail=_PHASE_DETAIL[TechLeadRunPhase.WITHDRAWN],
        )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def recent(self, *, limit: int) -> tuple[TechLeadRunRecord, ...]:
        """The newest runs this engine executed, most recently started first.

        Satisfies :class:`...ports.tech_lead_run_record_store.TechLeadRunHistoryReader`,
        which is what the dashboard projection depends on — so a view reaches
        history through a read-only port, never through this write-capable owner.
        ``limit`` has no default here: how many runs to SHOW is the display
        surface's decision, and a second default would be a second answer.
        """
        return self._store.recent(limit=limit)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_decision_outcome(self, session: "Session") -> "_DecisionOutcome":
        """What the run's own decision artifact says it produced.

        Read at the terminal seam and used ONLY for the record, never to decide
        anything: the authoritative validation of this same pair already ran in
        the completion processor's pre-action phase. An unreadable or absent
        pair yields empty counts, which is the truthful answer — a run whose
        decision cannot be read produced nothing an operator can act on.
        """
        from .tech_lead_decision_loader import load_tech_lead_artifact_pair_for_run

        try:
            result = load_tech_lead_artifact_pair_for_run(session.run_dir)
        except OSError:
            logger.warning(
                "[TECH_LEAD_RUN] Could not read the decision artifact for %s",
                session.terminal_id,
                exc_info=True,
            )
            return _DecisionOutcome()
        decision = result.decision
        if decision is None:
            return _DecisionOutcome()
        return _DecisionOutcome(
            detail=decision.summary,
            findings=len(decision.findings),
            proposals=len(decision.proposed_actions),
        )


@dataclass(frozen=True, slots=True)
class _DecisionOutcome:
    """The record-facing shape of a run's decision artifact."""

    detail: str = ""
    findings: int = 0
    proposals: int = 0


def in_memory_run_activity() -> TechLeadRunActivity:
    """Run history for a composition with no durable home for it.

    The counterpart to :func:`..control.tech_lead_run_ownership.single_instance_run_ownership`:
    a caller that needs a default never has to know which store makes one, and
    — more importantly — the launch and completion seams never grow an "if
    history is wired" branch, which is the place recording silently stops.
    """
    from ..ports.tech_lead_run_record_store import InMemoryTechLeadRunRecordStore

    return TechLeadRunActivity(InMemoryTechLeadRunRecordStore())


def optional_run_activity(
    activity: Optional[TechLeadRunActivity],
) -> TechLeadRunActivity:
    """``activity``, or an in-memory one when a composition wired none."""
    return activity if activity is not None else in_memory_run_activity()


__all__ = [
    "TechLeadRunActivity",
    "in_memory_run_activity",
    "optional_run_activity",
]
