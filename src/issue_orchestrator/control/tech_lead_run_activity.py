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
* :meth:`TechLeadRunActivity.note_concluded` — called from
  ``finalize_terminal_outcome``, the POST-APPLY terminal seam that also commits
  the terminal trace event, the cached state machine, and the session history.
  Deliberately not from ``process_completion``: the authoritative terminal status
  does not exist yet there, so concluding early recorded COMPLETED for runs whose
  mandated action then failed and whose real outcome was FAILED — and the
  once-only guard made that first wrong answer permanent (#6858 round 1 F3).

This owner also decides WHEN a run's artifacts are preserved, because that is the
same event as concluding it: the run's evidence lives in a worktree that cleanup
is about to remove, so it is copied to the engine-owned archive at the terminal
seam and the locator is written with the verdict (#6858 round 1 F4).

Everything this owner writes is best-effort by contract (see
:mod:`...ports.tech_lead_run_record_store` and
:mod:`...ports.tech_lead_run_artifact_archive`): a tech-lead run's product is its
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

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..domain.tech_lead_run_artifacts import TechLeadRunArtifacts
    from ..ports.tech_lead_run_artifact_archive import TechLeadRunArtifactArchive
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
        archive: "TechLeadRunArtifactArchive",
        *,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._store = store
        self._archive = archive
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
        # The SCOPE decides the subject, not the session: a global run's subject
        # is the board or the PR manifest, and recording the anchor it was
        # coordinated through as its subject is what made health reviews read as
        # investigations of their own bookkeeping issue (#6858 round 1 F5).
        subject_issue_number = scope.subject_issue_number or 0
        self._store.open_run(
            TechLeadRunRecord(
                run_key=scope.run_key,
                scope_kind=scope.kind,
                flavor=scope.flavor,
                phase=TechLeadRunPhase.RUNNING,
                started_at=session.started_at,
                run_id=session.run_assets.run_id,
                session_name=session.run_assets.session_name,
                subject_issue_number=subject_issue_number,
                subject_title=session.issue.title if subject_issue_number else "",
                # The GitHub object the run was coordinated THROUGH, named as
                # what it is so an operator can still find the anchor.
                anchor_issue_number=session.issue.number,
            )
        )

    def note_concluded(
        self,
        session: "Session",
        effective_status: SessionStatus,
        *,
        processing_errors: Optional[list[str]] = None,
    ) -> None:
        """Close the record for a finishing tech-lead session.

        ``effective_status`` must be the POST-APPLY terminal status — the same
        value the terminal trace event, the cached state machine and the session
        history are finalized from. A mandated tech-lead action that fails (or an
        apply that raises) makes the effective outcome FAILED regardless of the
        agent's intent, and the record has to agree with the surfaces beside it
        (#6858 round 1 F3).

        A COMPLETED session that failed contract processing is recorded as
        FAILED, not completed: the orchestrator rejected its decision, so the
        run produced nothing, and saying otherwise on the activity surface
        would contradict the actions the same completion planned.

        A publish-stage failure is NOT a conclusion, exactly as it is not one
        for the launch-authority retention owner alongside this call: the retry
        re-enters completion for the same session run, so concluding now would
        publish a verdict the retry is about to overturn.
        """
        # The session's own immutable launch stamp, not the CURRENT agent
        # configuration: a repository that renames or removes its tech lead agent
        # mid-run would otherwise strand every open record at RUNNING forever
        # (#6858 round 1 F3).
        if scope_of_session(session) is None:
            return
        if is_publish_failure(processing_errors):
            return
        phase = _PHASE_BY_STATUS[effective_status]
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
            artifacts=self._preserve_artifacts(session),
        )

    def note_withdrawn(self, session: "Session") -> None:
        """Close the record for a run stopped before it reached a decision.

        The one-shot timeout path terminates a session and then runs no further
        tick, so without this the record would sit at RUNNING forever — the
        activity surface's version of the leaked lease the termination owner
        already guards against.

        A withdrawn run's artifacts are preserved too: a run stopped mid-audit is
        exactly the one an operator wants to read, and its worktree is removed on
        the same path that terminated it. Gated on the launch stamp for the same
        reason the conclusion is — and here it also keeps a session that is not a
        tech-lead run from copying its files into the tech-lead archive.
        """
        if scope_of_session(session) is None:
            return
        self._store.conclude_run(
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
            phase=TechLeadRunPhase.WITHDRAWN,
            ended_at=self._now(),
            detail=_PHASE_DETAIL[TechLeadRunPhase.WITHDRAWN],
            artifacts=self._preserve_artifacts(session),
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

    def _preserve_artifacts(
        self, session: "Session"
    ) -> "Optional[TechLeadRunArtifacts]":
        """Copy the run's evidence somewhere the cleanup path does not reach.

        Called at the terminal seam, which is the last moment the run's own
        directory is guaranteed to exist: the cleanup of a disposable
        investigation worktree is planned from this same completion.

        Retention runs here too, because this owner is the only one holding BOTH
        halves: the archive says which locations it retired, and the store
        immediately stops the matching rows advertising them (#6858 round 2 F6).
        A pruned run keeps its verdict and loses only its drill-down.
        """
        artifacts = self._archive.preserve(
            run_id=session.run_assets.run_id,
            session_name=session.run_assets.session_name,
            run_dir=session.run_dir,
        )
        self._store.forget_artifacts(self._archive.prune())
        return artifacts

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
    """Run history for a composition that keeps nothing durable.

    An EXPLICIT choice a composition makes, never a fallback one gets by
    omission: a bounded test or a one-shot CLI selects this so "this process
    remembers nothing after it exits" is visible where the wiring is, rather
    than discovered later as missing history (#6858 round 1 A2).
    """
    from ..ports.tech_lead_run_artifact_archive import DiscardedTechLeadRunArtifacts
    from ..ports.tech_lead_run_record_store import InMemoryTechLeadRunRecordStore

    return TechLeadRunActivity(
        InMemoryTechLeadRunRecordStore(), DiscardedTechLeadRunArtifacts()
    )


__all__ = [
    "TechLeadRunActivity",
    "in_memory_run_activity",
]
