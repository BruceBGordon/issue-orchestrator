"""What a terminal with an unreadable pending-work claim means (#6999 F6/F12).

A terminal whose stored claim cannot be read is the one restoration outcome that
must not be handled quietly. The session may still be alive and doing queued
work nobody can now name: admitting it would let its completion settle as
holding no claim and silently discard that request, and dropping it without a
word would leave a running agent nobody is watching.

This owner exists because that escalation is NOT the tech-lead launch-exhaustion
escalation it originally borrowed (#6999 F12). The two disagree on every rule
that matters:

* provenance — a quarantine can belong to review, rework, validation-retry or
  tech-lead work, so a comment written in the vocabulary of a failure
  investigation is simply wrong for most of them;
* clearing — the tech-lead lifecycle clears its marker as soon as ANY session
  for the issue is active, but a quarantined terminal is deliberately absent
  from ``active_sessions`` while still running, so a healthy sibling session on
  the same issue would silently retract the warning;
* identity — a quarantine belongs to one RUN, not to an issue. Two runs of the
  same issue can be quarantined independently, and re-discovering one every 30
  seconds must not re-comment.

So it keeps its own durable per-run marker, applies its own labels and comment,
and publishes the event only after that durable surface has committed. A failed
apply leaves the quarantine recorded-but-unescalated, which is what makes the
next orphan scan retry instead of treating the failure as final.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..domain.pending_work import PendingWorkKind
from ..events import EventName
from ..ports import EventSink, make_trace_event
from ..ports.pending_work_claim_store import (
    ClaimQuarantineStore,
    QuarantineLabelState,
    UnreadableClaim,
    UnresolvedClaim,
)
from .actions import (
    AddCommentAction,
    AddLabelAction,
    RemoveLabelAction,
    SupportsApplyAction,
)
from .in_flight_work import QuarantinedSession
from .label_manager import LabelManager

logger = logging.getLogger(__name__)


class QuarantineCause(Enum):
    """Why a run is quarantined — a typed part of this owner's boundary.

    Not one message with a boolean (#6999 A1). The two families say opposite
    things about the queued work:

    * an UNREADABLE CLAIM means nobody can name what the run is carrying, so an
      operator has to work it out before re-queuing anything;
    * an UNRESTORABLE RUN means the claim is perfectly intact and the work IS
      named — it is deliberately not being requeued because a terminal is still
      running it. Telling an operator that this work is "unknown" invites the
      manual requeue, and therefore the duplicate execution, that protecting the
      run was meant to prevent.

    The fourth state is both at once and gets its own variant rather than an
    implicit branch: a live terminal that can be neither rebuilt nor identified
    (#6999 F2). It used to be protected from requeueing and reported to nobody.
    """

    #: A live terminal was rebuilt, but the claim it holds cannot be read.
    CLAIM_UNREADABLE_LIVE_RUN = "claim_unreadable_live_run"
    #: A ledger row nothing is running, whose payload cannot be rebuilt.
    CLAIM_UNREADABLE_ENDED_RUN = "claim_unreadable_ended_run"
    #: A live terminal whose session assets could not be rebuilt. Its claim
    #: reads cleanly, so the work it holds is known exactly.
    RUN_UNRESTORABLE = "run_unrestorable"
    #: A live terminal that can be neither rebuilt nor identified.
    RUN_UNRESTORABLE_CLAIM_UNREADABLE = "run_unrestorable_claim_unreadable"

    @property
    def still_running(self) -> bool:
        """Whether a terminal for this run may still be alive."""
        return self is not QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN


@dataclass(frozen=True, slots=True)
class QuarantineSubject:
    """One run, one cause, and everything its escalation needs to be truthful.

    Built by the caller that observed the trouble, because that caller is the
    only one that knows which of the four states it is looking at. The named
    constructors each take the typed record their observation produced, so a
    caller cannot assemble a subject out of fields it guessed.
    """

    quarantine_key: str
    run_key: str
    session_name: str
    issue_number: int
    error: str
    cause: QuarantineCause
    #: Known only when the claim reads cleanly; it is what makes the
    #: unrestorable-run message able to name the work it is protecting.
    work_kind: PendingWorkKind | None = None

    @classmethod
    def live_run_with_unreadable_claim(
        cls, quarantined: "QuarantinedSession"
    ) -> "QuarantineSubject":
        """A restored terminal whose claim record could not be read."""
        session = quarantined.session
        return cls(
            quarantine_key=quarantined.quarantine_key,
            run_key=quarantined.run_key,
            session_name=session.terminal_id,
            # Trusted: the launching session's own issue, never parsed out of
            # the terminal name (a review terminal is named for its PR).
            issue_number=session.issue.number,
            error=quarantined.error,
            cause=QuarantineCause.CLAIM_UNREADABLE_LIVE_RUN,
        )

    @classmethod
    def ended_run_with_unreadable_claim(
        cls, unreadable: UnreadableClaim
    ) -> "QuarantineSubject":
        """A ledger row whose run is not live and cannot be rebuilt.

        The issue number comes from the ledger row, recorded at hold time from
        the launching session (#6999 F12). Deriving it from the terminal name
        would escalate the PR number for every ``review-*`` claim - and the
        payload, which is the other place it lives, is precisely what has
        become unreadable.
        """
        return cls(
            quarantine_key=unreadable.quarantine_key,
            run_key=unreadable.run_key,
            session_name=unreadable.session_name,
            issue_number=unreadable.issue_number,
            error=unreadable.error,
            cause=QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN,
        )

    @classmethod
    def unrestorable_live_run(
        cls, unresolved: UnresolvedClaim
    ) -> "QuarantineSubject":
        """A LIVE run whose session could not be rebuilt (#6999 F14).

        Its claim is perfectly readable; what failed is the run's own assets, so
        the orchestrator cannot track the terminal. Requeueing the work would
        launch a second session beside one that is still running.
        """
        return cls(
            quarantine_key=unresolved.quarantine_key,
            run_key=unresolved.run_key,
            session_name=unresolved.session_name,
            issue_number=unresolved.issue_number,
            error="the run's session assets could not be rebuilt",
            cause=QuarantineCause.RUN_UNRESTORABLE,
            work_kind=unresolved.claim.kind,
        )

    @classmethod
    def unrestorable_live_run_with_unreadable_claim(
        cls, unreadable: UnreadableClaim
    ) -> "QuarantineSubject":
        """A live run that can be neither rebuilt nor identified (#6999 F2)."""
        return cls(
            quarantine_key=unreadable.quarantine_key,
            run_key=unreadable.run_key,
            session_name=unreadable.session_name,
            issue_number=unreadable.issue_number,
            error=unreadable.error,
            cause=QuarantineCause.RUN_UNRESTORABLE_CLAIM_UNREADABLE,
        )


class QuarantineLabelOps(Protocol):
    """The blocking-label operations a quarantine needs, with typed outcomes.

    A boolean "did the apply work" cannot express what release depends on
    (#6999 F12/A5): adding a label that is already present SUCCEEDS, so success
    is not evidence this quarantine put it there.
    """

    def acquire_block(self, issue_number: int) -> QuarantineLabelState:
        """Add the blocking label; say whether it was already present."""
        ...

    def release_block(self, issue_number: int) -> bool:
        """Remove the blocking label. False means it did not commit."""
        ...

    def announce(self, issue_number: int, comment: str) -> bool:
        """Post the operator-visible comment. False means it did not commit."""
        ...


@dataclass(frozen=True, slots=True)
class ClaimQuarantineOwner:
    """The one place a run is quarantined for an unreadable claim.

    Escalation and release are a durable state machine, not a pair of one-shot
    calls (#6999 F12). Three facts are persisted separately because they fail
    separately: whether this quarantine ACQUIRED the shared blocking label (as
    opposed to finding it already there), whether the operator comment
    committed, and whether a resolved cause still has cleanup outstanding. The
    row survives every one of those until its own step succeeds, so anything
    that failed is retried by the next sweep rather than becoming final.
    """

    store: ClaimQuarantineStore
    labels: QuarantineLabelOps
    events: EventSink

    def quarantine(self, subject: QuarantineSubject) -> None:
        """Escalate one run under its own typed cause (#6999 A1).

        The single entry point. Which of the four causes it is decides what the
        operator is told and which event is published; everything else - the
        durable row, the shared block, the retry-until-committed protocol - is
        the same for all of them.
        """
        logger.error(
            "[WORK] Quarantined run %s (session %s, issue #%d): %s. %s",
            subject.run_key,
            subject.session_name,
            subject.issue_number,
            subject.error,
            _ESCALATIONS[subject.cause].log_consequence,
        )
        self._escalate(subject)

    def reconcile_released(self, live_quarantine_keys: frozenset[str]) -> None:
        """Advance every quarantine whose cause is gone, and retry what failed.

        Restoration and the ledger sweep report which quarantines are still
        justified; everything else has had its cause repaired or removed. Rows
        already mid-release are retried here too, which is the whole reason
        they were kept.
        """
        # Quarantines that do not own the label go first. When several share
        # one issue only the label's real owner can take it off, and it has to
        # wait until it is the last one out; releasing the others first lets
        # that happen in this sweep rather than the next one.
        ordered = sorted(
            self.store.list_quarantines(), key=lambda record: record.block_is_ours
        )
        for record in ordered:
            if record.quarantine_key in live_quarantine_keys and not record.releasing:
                continue
            self.release(record.quarantine_key)

    def release(self, quarantine_key: str) -> None:
        """End a quarantine whose cause is gone, and clear what it owns.

        Cleanup order is the point (#6999 F12). The row is first marked
        releasing and KEPT, so a failed label removal is retried by the next
        sweep instead of being lost. The blocking label comes off only when
        this quarantine acquired it and no other quarantine still holds the
        same issue; a ``needs-human`` applied by a human or another owner keeps
        its own provenance. Only after cleanup commits is the row deleted.
        """
        # Every early return below either deletes the row or leaves it
        # ``releasing``, which the sweep retries. There is no path that ends
        # with the obligation dropped and the label still on the issue.
        record = self.store.read_quarantine(quarantine_key)
        if record is None:
            return
        if not record.releasing:
            self.store.mark_quarantine_releasing(quarantine_key)
        if not record.block_is_ours:
            # Nothing of ours on the issue; the row is all there is to remove.
            self.store.release_quarantine(quarantine_key)
            return
        if record.issue_number in self.store.quarantined_issue_numbers():
            # Another quarantine on the same issue is still live, and it is
            # very likely standing on OUR label: the second one to escalate
            # found the label already present and recorded itself PREEXISTING,
            # so it will never take it off. Deleting our row here would strand
            # the block forever. The row stays in ``releasing`` - excluded from
            # the live set above, so it holds nothing open - and the next sweep
            # retries it until we are the last one out.
            return
        if not self.labels.release_block(record.issue_number):
            logger.error(
                "[WORK] Could not clear the quarantine block on issue #%d; "
                "keeping the record so the next sweep retries",
                record.issue_number,
            )
            return
        self.store.release_quarantine(quarantine_key)

    def _escalate(self, subject: QuarantineSubject) -> None:
        quarantine_key = subject.quarantine_key
        record = self.store.read_quarantine(quarantine_key)
        if record is None:
            self._record(subject)
            record = self.store.read_quarantine(quarantine_key)
            assert record is not None
        elif record.releasing:
            # The cause came back before cleanup finished; it is live again.
            self._record(subject)
        # Applied on EVERY pass, idempotently. The block is shared with owners
        # that lift it when a session for the issue looks active, and a
        # quarantined terminal is deliberately not one of those - so a sweep
        # that found it missing must put it back, whoever it belonged to.
        # Adding a label already present is a no-op, so this costs nothing.
        outcome = self.labels.acquire_block(subject.issue_number)
        if record.records_ownership(outcome):
            # Includes the reassertion case: a block this quarantine once found
            # already present, then had removed underneath it, and has now put
            # back itself. Recording that transition is what lets release take
            # it off again instead of stranding needs-human (#6999 F3).
            self.store.record_quarantine_label_state(quarantine_key, outcome)
        elif record.block_unrecorded:
            # Nothing recorded and this apply did not commit either: a
            # quarantine with no block at all is not escalated at all.
            logger.error(
                "[WORK] Could not apply the quarantine block on issue #%d; "
                "the next sweep retries",
                subject.issue_number,
            )
            return
        if record.announced:
            return
        escalation = _ESCALATIONS[subject.cause]
        if not self.labels.announce(subject.issue_number, escalation.comment(subject)):
            # Recorded but NOT announced, so the next sweep retries. The event
            # is deliberately withheld: announcing a quarantine whose durable
            # half never landed would show a warning that vanishes on restart.
            logger.error(
                "[WORK] Durable quarantine escalation did not commit for %s; "
                "leaving it unannounced so the next sweep retries",
                subject.session_name,
            )
            return
        self.store.mark_quarantine_announced(quarantine_key)
        self.events.publish(make_trace_event(
            escalation.event,
            {
                "issue_number": subject.issue_number,
                "session_name": subject.session_name,
                "run_key": subject.run_key,
                "cause": subject.cause.value,
                "error": subject.error,
            },
        ))

    def _record(self, subject: QuarantineSubject) -> None:
        self.store.record_quarantine(
            subject.quarantine_key,
            run_key=subject.run_key,
            session_name=subject.session_name,
            issue_number=subject.issue_number,
            error=subject.error,
        )


@dataclass(frozen=True, slots=True)
class _Escalation:
    """What one cause tells an operator, and under which event name."""

    event: EventName
    log_consequence: str
    headline: str
    #: What is (or is not) known about the work, in the operator's terms.
    finding: str
    #: What the operator has to do about it.
    instruction: str

    def comment(self, subject: QuarantineSubject) -> str:
        return (
            f"🔒 **{self.headline}**\n\n"
            f"`{subject.session_name}` took a queued request off one of the "
            "orchestrator's pending queues when it launched.\n\n"
            f"{self.finding.format(work=_work_phrase(subject.work_kind))}\n\n"
            f"Error: {subject.error}\n\n"
            f"{self.instruction}"
        )


def _work_phrase(work_kind: PendingWorkKind | None) -> str:
    """How to name the queued work in a comment, when it is known at all."""
    return "queued work" if work_kind is None else f"queued {work_kind.value} work"


_UNKNOWN_WORK_FINDING = (
    "That record can no longer be read, so which review, rework, validation "
    "retry or tech-lead investigation it is carrying is unknown. It is "
    "deliberately NOT being tracked: tracking it would let its completion be "
    "recorded as holding no work at all, silently discarding that request."
)

# One entry per cause. A table rather than branches inside the escalation, so
# "what does an operator read for this state" has a single enumerable answer and
# a new cause cannot inherit another cause's story by accident (#6999 A1).
_ESCALATIONS: dict[QuarantineCause, _Escalation] = {
    QuarantineCause.CLAIM_UNREADABLE_LIVE_RUN: _Escalation(
        event=EventName.SESSION_CLAIM_UNREADABLE,
        log_consequence=(
            "It is NOT being tracked, so its completion cannot settle as "
            "claimless and discard the queued work it holds"
        ),
        headline="Session quarantined: its pending-work claim is unreadable",
        finding=f"The terminal may still be running. {_UNKNOWN_WORK_FINDING}",
        instruction=(
            "A human needs to work out what this session was doing, re-queue it "
            "if necessary, and stop the terminal."
        ),
    ),
    QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN: _Escalation(
        event=EventName.SESSION_CLAIM_UNREADABLE,
        log_consequence="No live terminal is holding it, and it cannot be recovered",
        headline="Session quarantined: its pending-work claim is unreadable",
        finding=f"The terminal has already ended. {_UNKNOWN_WORK_FINDING}",
        instruction=(
            "A human needs to work out what this session was doing and re-queue "
            "it if necessary."
        ),
    ),
    QuarantineCause.RUN_UNRESTORABLE: _Escalation(
        event=EventName.SESSION_RUN_UNRESTORABLE,
        log_consequence=(
            "Its queued work is NOT being requeued - that would run it twice - "
            "and the terminal is not being tracked"
        ),
        headline="Session quarantined: its run could not be rebuilt",
        finding=(
            "The terminal is still running and its pending-work record is "
            "intact, so the orchestrator knows exactly what it is carrying: "
            "{work}. What failed is the run's own session assets, so the "
            "terminal cannot be tracked.\n\nThe work is deliberately NOT being "
            "re-queued: a re-queue would start a second session beside the one "
            "still running it."
        ),
        instruction=(
            "A human needs to stop the terminal, after which the next sweep "
            "re-queues the work automatically. Do not re-queue it by hand while "
            "the terminal is alive."
        ),
    ),
    QuarantineCause.RUN_UNRESTORABLE_CLAIM_UNREADABLE: _Escalation(
        event=EventName.SESSION_RUN_UNRESTORABLE,
        log_consequence=(
            "The terminal can be neither rebuilt nor identified, so it is "
            "untracked and its work cannot be recovered from the ledger"
        ),
        headline=(
            "Session quarantined: its run could not be rebuilt and its "
            "pending-work claim is unreadable"
        ),
        finding=(
            "The terminal is still running, and BOTH halves of its record "
            "failed: its session assets could not be rebuilt, so it cannot be "
            "tracked, and its pending-work claim cannot be read, so which "
            "review, rework, validation retry or tech-lead investigation it is "
            "carrying is unknown.\n\nNothing is being re-queued: there is "
            "nothing readable to re-queue, and a live terminal is still "
            "working."
        ),
        instruction=(
            "A human needs to inspect the terminal to work out what it is "
            "doing, stop it, and re-queue that work by hand."
        ),
    ),
}


def build_claim_quarantine_owner(
    *,
    store: ClaimQuarantineStore,
    action_applier: SupportsApplyAction,
    label_manager: LabelManager,
    events: EventSink,
) -> ClaimQuarantineOwner:
    """Assemble the owner from composition-root collaborators.

    The ActionApplier-to-typed-outcome adaptation lives here so both roots wire
    the same behaviour - in particular that adding an already-present label is
    reported as PREEXISTING rather than as a successful acquisition (#6999
    F12).
    """

    class _Labels:
        def acquire_block(self, issue_number: int) -> QuarantineLabelState:
            result = action_applier.apply(
                AddLabelAction(
                    issue_number=issue_number,
                    label=label_manager.needs_human,
                    reason="pending-work claim unreadable",
                )
            )
            if not result.success:
                return QuarantineLabelState.UNKNOWN
            if result.details.get("no_op") or result.details.get("presence_unknown"):
                # Already there, or the applier could not check whether it was.
                # Both mean the same thing to release: not provably ours.
                return QuarantineLabelState.PREEXISTING
            return QuarantineLabelState.ACQUIRED

        def release_block(self, issue_number: int) -> bool:
            return action_applier.apply(
                RemoveLabelAction(
                    issue_number=issue_number,
                    label=label_manager.needs_human,
                    reason="pending-work claim quarantine resolved",
                )
            ).success

        def announce(self, issue_number: int, comment: str) -> bool:
            return action_applier.apply(
                AddCommentAction(
                    number=issue_number,
                    comment=comment,
                    reason="pending-work claim unreadable",
                )
            ).success

    return ClaimQuarantineOwner(store=store, labels=_Labels(), events=events)


__all__ = [
    "ClaimQuarantineOwner",
    "QuarantineCause",
    "QuarantineLabelOps",
    "QuarantineSubject",
    "build_claim_quarantine_owner",
]
