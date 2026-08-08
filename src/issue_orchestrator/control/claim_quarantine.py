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
    QuarantineCause,
    QuarantineLabelState,
    QuarantineRecord,
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
from .needs_human_block import (
    NO_OTHER_NEEDS_HUMAN_CAUSES,
    NeedsHumanCause,
    SharedNeedsHumanBlock,
)

logger = logging.getLogger(__name__)


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


class _BlockExit(Enum):
    """What a resolving quarantine owes the shared block on its way out."""

    #: Ours, and nothing else is standing on it - take it off, then delete.
    CLEAR = "clear"
    #: Not ours, or another lifecycle now needs it - delete only our record.
    LEAVE = "leave"
    #: A sibling quarantine on the same issue still holds it. Stay ``releasing``
    #: and retry next sweep, so the last one out is the one that clears it.
    WAIT = "wait"


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
    calls (#6999 F12). Four facts are persisted separately because they change
    separately: whether this quarantine ACQUIRED the shared blocking label (as
    opposed to finding it already there), whether the operator comment
    committed, whether a resolved cause still has cleanup outstanding, and WHICH
    CAUSE the committed comment was written for. The row survives every one of
    those until its own step succeeds, so anything that failed is retried by the
    next sweep rather than becoming final.

    The cause is durable for the same reason the announcement is (#6999 F6).
    Every word an operator reads - and the event name the dashboard reacts to -
    is chosen from the cause alone, so an announcement remembered without the
    cause that produced it can only be re-asserted, never corrected. A run
    announced as ended and then rediscovered alive kept telling a human it had
    finished and could be re-queued by hand, over a terminal still doing the
    work.
    """

    store: ClaimQuarantineStore
    labels: QuarantineLabelOps
    events: EventSink
    #: Every OTHER durable cause of the shared ``needs-human`` label (#6999 F4).
    #: A quarantine's own row proves it applied the block; it does not prove it
    #: is still the only reason for it, and removing a block another lifecycle
    #: now requires is how a live escalation silently disappears.
    block: SharedNeedsHumanBlock = NO_OTHER_NEEDS_HUMAN_CAUSES

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
        # Every branch below either deletes the row or leaves it ``releasing``,
        # which the sweep retries. There is no path that ends with the
        # obligation dropped and the label still on the issue.
        record = self.store.read_quarantine(quarantine_key)
        if record is None:
            return
        if not record.releasing:
            self.store.mark_quarantine_releasing(quarantine_key)
        owed = self._exit_owes(record)
        if owed is _BlockExit.WAIT:
            return
        if owed is _BlockExit.CLEAR and not self._clear_block(record.issue_number):
            return
        self.store.release_quarantine(quarantine_key)

    def _exit_owes(self, record: QuarantineRecord) -> "_BlockExit":
        """What this quarantine's exit owes the SHARED block, as one answer.

        Three separate questions used to be three inline guards on the way out,
        which is exactly the shape that let one of them be forgotten (#6999 F4).
        They are one decision: this quarantine may take the shared label off
        only if it demonstrably put it there, no sibling quarantine is still
        standing on it, and no other lifecycle has since come to need it.
        """
        if not record.block_is_ours:
            # Nothing of ours on the issue; the row is all there is to remove.
            return _BlockExit.LEAVE
        if record.issue_number in self.store.quarantined_issue_numbers():
            # Another quarantine on the same issue is still live, and it is
            # very likely standing on OUR label: the second one to escalate
            # found the label already present and recorded itself PREEXISTING,
            # so it will never take it off. Deleting our row here would strand
            # the block forever. The row stays in ``releasing`` - excluded from
            # the live set - and the next sweep retries it until we are last out.
            return _BlockExit.WAIT
        if self.block.held_by_another_cause(
            record.issue_number, excluding=NeedsHumanCause.CLAIM_QUARANTINE
        ):
            # OUR cause is gone, but the shared label is not ours alone to take
            # off (#6999 F4). A tech-lead escalation that became required while
            # this quarantine held the block recorded its own marker and is now
            # relying on the same label; removing it would leave that escalation
            # with neither the block nor anything to recover it from. Withdraw
            # our cause - the row - and leave the label to the owner that still
            # needs it, which will clear it on its own terms.
            logger.info(
                "[WORK] Quarantine on issue #%d resolved, but another lifecycle "
                "still requires needs-human; leaving the label and dropping only "
                "the quarantine record",
                record.issue_number,
            )
            return _BlockExit.LEAVE
        return _BlockExit.CLEAR

    def _clear_block(self, issue_number: int) -> bool:
        cleared = self.labels.release_block(issue_number)
        if not cleared:
            logger.error(
                "[WORK] Could not clear the quarantine block on issue #%d; "
                "keeping the record so the next sweep retries",
                issue_number,
            )
        return cleared

    def _escalate(self, subject: QuarantineSubject) -> None:
        quarantine_key = subject.quarantine_key
        # This pass's observation is written FIRST, unconditionally, and the
        # store decides what it means for the announcement (#6999 F6). Recording
        # only on the first sight of a run was how a changed cause kept the old
        # story: a run announced as an ended terminal, then rediscovered alive
        # and unrestorable, still told an operator it had finished and could be
        # re-queued by hand. It also revives a row whose release had not yet
        # committed - the cause is demonstrably back.
        self._record(subject)
        record = self.store.read_quarantine(quarantine_key)
        assert record is not None
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
        if record.announces(subject.cause):
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
            cause=subject.cause,
            work_kind=subject.work_kind,
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
        event=EventName.SESSION_RUN_UNRESTORABLE_CLAIM_UNREADABLE,
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
    needs_human_block: SharedNeedsHumanBlock = NO_OTHER_NEEDS_HUMAN_CAUSES,
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

    return ClaimQuarantineOwner(
        store=store,
        labels=_Labels(),
        events=events,
        block=needs_human_block,
    )


__all__ = [
    "ClaimQuarantineOwner",
    "QuarantineCause",
    "QuarantineLabelOps",
    "QuarantineSubject",
    "build_claim_quarantine_owner",
]
