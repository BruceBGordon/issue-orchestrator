"""Who requires the shared ``needs-human`` block, and why (#6999 F4/F2/A2).

``needs-human`` is ONE label with SEVERAL independent causes, and until this
module existed each owner reasoned about it from inside its OWN provenance
alone. That is not enough to decide a removal. The concrete loss: a quarantine
acquires ``needs-human``; while it is present another lifecycle comes to need
the same label, finds it already there and records nothing; the quarantine then
resolves and takes "its" label off. The second cause is left with neither the
block nor anything to recover it from, so an issue a human was told to look at
silently returns to the board.

Three orchestrator-owned causes exist, and each records itself durably:

* ``TECH_LEAD_ESCALATION`` — its own marker label
  (:mod:`.tech_lead_needs_human_reconcile`);
* ``CLAIM_QUARANTINE`` — its row in the quarantine ledger
  (:mod:`.claim_quarantine`);
* ``SESSION_LIFECYCLE`` — everything the planners escalate (a session that ended
  without a completion record, publish failures past their bound, an invalid
  completion record, a stuck sweep). These recorded NOTHING before (#6999 F2
  round 2), which is why the loss above stayed open for every cause but the
  tech-lead one. They now record a row through this owner.

Anything else wearing the label is operator intent, and is recognised by the
ABSENCE of any recorded cause rather than by a fourth token: a human does not
announce themselves, and a label nobody can account for is exactly what "a human
put this here" looks like.

Reads FAIL CLOSED: a cause that cannot be evaluated is reported as present,
because wrongly keeping a block costs a tick of attention and wrongly removing
one loses a human's only signal.

The label stays authoritative over the rows, never the other way round. A row
means "while this label is present, THIS lifecycle is one of the reasons for
it", so the moment the label goes — removed by an owner here, or by a human out
of band — every row for that issue goes with it. That is what stops a stale row
stranding an issue in ``needs-human`` forever, which is the failure mode a
provenance ledger invites if it is allowed to outlive what it describes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..ports.pending_work_claim_store import NeedsHumanCauseStore

logger = logging.getLogger(__name__)

#: Why a generic label action that touches the governed label is refused
#: outright (#6999 F2 round 3). Fail-fast beats a silent default: a catch-all
#: made every uncaused call site look correct while collapsing independent
#: assertions onto one row, where a single release erased them all.
UNCAUSED_BLOCK_MUTATION = (
    "shared needs-human block mutated without a NeedsHumanCause; route it "
    "through the shared-block owner"
)


class NeedsHumanCause(Enum):
    """One durable, independently recorded cause of the shared block.

    Enumerated rather than passed as free text: every owner that may remove the
    label has to be able to name the ones it is NOT, and a cause added without
    an entry here would simply be invisible to the others.
    """

    #: A tech-lead investigation that exhausted its bounded launch budget.
    #: Recorded as the tech-lead marker label on the issue.
    TECH_LEAD_ESCALATION = "tech_lead_escalation"
    #: A run whose pending-work claim could not be read or rebuilt. Recorded as
    #: a non-releasing row in the quarantine ledger.
    CLAIM_QUARANTINE = "claim_quarantine"
    #: An AGENT asked for it, through ``coding-done needs_human`` /
    #: ``reviewer-done``. Its own cause because it is the one assertion that
    #: arrives from outside the orchestrator's own planning, on the completion
    #: path rather than through an action (#6999 F2 round 3).
    AGENT_COMPLETION = "agent_completion"
    #: A PR escalated out of the merge/awaiting-merge lifecycle. Its own cause
    #: because it is the one SESSION-side assertion that is targetedly
    #: released again - by the post-publish "now reworkable" clear - so sharing
    #: a token with anything else would let that clear erase another block.
    MERGE_ESCALATION = "merge_escalation"
    #: Every other orchestrator escalation the planners raise: a session that
    #: ended without a completion record, publish failures past their bound, an
    #: invalid completion record, a stuck sweep, a failed rework worktree, a
    #: retrospective review, an uncommitted mandated reset. They share ONE token
    #: because none of them is ever released on its own terms: each ends when a
    #: human clears the label, or when an operator/terminal force-clear ends
    #: every cause at once. A lifecycle that gains a targeted release must take
    #: its own cause rather than joining this one.
    SESSION_LIFECYCLE = "session_lifecycle"


@dataclass(frozen=True, slots=True)
class HumanBlockRequest:
    """One lifecycle asserting or withdrawing the shared block (#6999 F2 r3).

    ``target`` is the number ACTUALLY mutated, which is not always an issue: a
    merge escalation blocks the PR. Carrying it explicitly is what stops a
    caller recording provenance against an issue while labelling a pull
    request, after which no remover can find the cause it is standing on.
    """

    target: int
    cause: NeedsHumanCause
    reason: str


class BlockOutcome(Enum):
    """What a command did to the shared label."""

    #: The label is on the target and this cause is recorded against it.
    HELD = "held"
    #: This cause is withdrawn and the label came off with it.
    CLEARED = "cleared"
    #: This cause is withdrawn, but the label stays: another cause needs it.
    HELD_BY_ANOTHER_CAUSE = "held_by_another_cause"
    #: The label write did not commit. The caller retries; nothing is assumed.
    FAILED = "failed"
    #: No owner governs this label in this composition, so NOTHING happened and
    #: the caller must do its own write. Distinct from ``FAILED`` on purpose: a
    #: null owner that reported success turned real mutations into silent
    #: no-ops, which is a worse bug than the bypass it was closing.
    UNGOVERNED = "ungoverned"

    @property
    def committed(self) -> bool:
        return self in {BlockOutcome.HELD, BlockOutcome.CLEARED}


class BlockLabelWriter(Protocol):
    """The narrow label port the block owner needs to be the sole writer."""

    def add_label(self, issue_number: int, label: str) -> None: ...

    def remove_label(self, issue_number: int, label: str) -> None: ...


class SharedNeedsHumanBlock(Protocol):
    """The one owner of every mutation of the shared ``needs-human`` label.

    Not an observer attached to a couple of handlers (#6999 F2 round 3): the
    label goes on and comes off HERE, so a path that does not consume this
    boundary cannot mutate it at all. Three commands, because the three intents
    are genuinely different - a lifecycle asserting a block, a lifecycle giving
    its own block back, and an operator or terminal recovery overriding every
    cause at once.
    """

    def owns(self, label: str) -> bool:
        """Whether ``label`` is the shared block this owner governs."""
        ...

    def acquire(self, request: HumanBlockRequest) -> BlockOutcome:
        """Put the shared block on ``request.target`` for ``request.cause``."""
        ...

    def release(self, request: HumanBlockRequest) -> BlockOutcome:
        """Withdraw ``request.cause``; take the label off only if it was last."""
        ...

    def force_clear(self, target: int, reason: str) -> BlockOutcome:
        """Override the causes this owner records and take the label off.

        REFUSES with ``HELD_BY_ANOTHER_CAUSE`` when a cause it cannot settle -
        a quarantine, a tech-lead escalation - still requires the block. Those
        two re-assert it within a tick, so clearing around them would report a
        success the next reconciliation pass contradicts (#6999 F3).
        """
        ...

    def held_by_another_cause(
        self, issue_number: int, *, excluding: NeedsHumanCause
    ) -> bool:
        """Whether a cause OTHER than ``excluding`` still requires the block."""
        ...

    def unsettleable_holders(self, issue_number: int) -> tuple[NeedsHumanCause, ...]:
        """The causes a force-clear cannot settle, for an operator to act on."""
        ...


#: Causes whose provenance already lives somewhere durable and lifecycle-owned:
#: the tech-lead marker label and the quarantine ledger row. This owner reads
#: them but never keeps a second copy, because two records of one fact are two
#: records that can disagree - the defect this whole boundary closes.
_SELF_RECORDING_CAUSES = frozenset(
    {NeedsHumanCause.TECH_LEAD_ESCALATION, NeedsHumanCause.CLAIM_QUARANTINE}
)

#: ...and because their records live in their own lifecycles, they are also the
#: causes a force-clear CANNOT settle (#6999 F3 round 4). Both re-assert the
#: block on their next pass, so clearing the label around them would be undone
#: within a tick - after an operator had been told the issue was unblocked.
_UNSETTLEABLE_BY_FORCE = (
    NeedsHumanCause.CLAIM_QUARANTINE,
    NeedsHumanCause.TECH_LEAD_ESCALATION,
)


@dataclass(frozen=True, slots=True)
class NeedsHumanBlock:
    """The bounded owner of the shared block: its label AND its provenance.

    Owning both is the point. While provenance was recorded beside a mutation
    performed elsewhere, correctness depended on every caller choosing to
    report - and four production paths did not: agent-authored ``needs_human``
    completions, merge escalation onto a PR, recovered-terminal label shedding,
    and operator retry/dismiss. Each could put the label on with no cause
    recorded, or take it off leaving causes behind.
    """

    #: The shared label itself. Held so callers ask "is this the one?" instead
    #: of re-deriving it from configuration.
    needs_human_label: str
    #: The tech-lead lifecycle's durable provenance label.
    tech_lead_marker: str
    #: The one label port through which this label is written.
    labels: BlockLabelWriter
    #: Live label read for one target. Must bypass caches: a stale observation
    #: here can retract a block a human is waiting on.
    read_labels: Callable[[int], Sequence[str]]
    #: Issues held open by a quarantine whose cause has not been released.
    quarantined_issue_numbers: Callable[[], frozenset[int]]
    #: Durable rows for the causes that keep their provenance nowhere else.
    causes: NeedsHumanCauseStore

    def owns(self, label: str) -> bool:
        return label == self.needs_human_label

    def acquire(self, request: HumanBlockRequest) -> BlockOutcome:
        """Assert the shared block for one cause, and record that it did.

        The ONE way the governed label goes on. Applying the label and
        recording who needs it in the same call is what makes the boundary
        real: a caller cannot label a target without saying who needs it, and
        cannot say who needs it without the label following. Recording an
        ALREADY PRESENT label matters just as much as applying a fresh one - a
        cause arriving second is exactly the case that used to leave no trace
        and lose its block.
        """
        # Provenance FIRST, then the label (#6999 F4 round 4). Labelling first
        # left a window where the external write had committed and the cause
        # store had not: a LIVE block nobody owns, which the next remover takes
        # away because it can find no reason for it. This order can only leave
        # the opposite - a recorded cause with no label - which the read path
        # prunes on sight.
        #
        # Which write records it depends on whether this is a NEW GENERATION of
        # the label (#6999 F4 round 5). Rows only ever mean "while this label is
        # present, X requires it", so an absent label makes every existing row
        # stale - and a clear that failed after a committed removal leaves
        # exactly that. Inheriting one would give the incoming cause a companion
        # nothing is asserting, and releasing the new cause would then find the
        # ghost and keep the block forever. Relying on some later read to prune
        # it first is not a fix; it is a race this owner happened to win.
        if not self._recorded(request):
            return BlockOutcome.FAILED
        try:
            self.labels.add_label(request.target, self.needs_human_label)
        except Exception:
            logger.exception(
                "[BLOCK] Could not apply the shared needs-human block to #%d "
                "for %s; withdrawing the cause just recorded so nothing claims "
                "a block that does not exist",
                request.target,
                request.cause.value,
            )
            self._withdraw(request)
            return BlockOutcome.FAILED
        return BlockOutcome.HELD

    def release(self, request: HumanBlockRequest) -> BlockOutcome:
        """Withdraw one cause; take the label off only if it was the last.

        The ONE way an orchestrator lifecycle gives its own block back.
        Withdrawing always happens - this cause IS discharged - but the label
        only follows when nothing else is standing on it. That is the defect
        this owner closes, and it now holds for every remover rather than for
        the two that remembered to ask.
        """
        if self.held_by_another_cause(request.target, excluding=request.cause):
            # Asked BEFORE withdrawing, because the question is about the other
            # causes and this one does not count itself. Only then is this
            # cause discharged - the label stays for whoever else needs it.
            self._withdraw(request)
            logger.info(
                "[BLOCK] #%d keeps needs-human after %s withdrew: another "
                "lifecycle still requires it",
                request.target,
                request.cause.value,
            )
            return BlockOutcome.HELD_BY_ANOTHER_CAUSE
        # Last cause standing. The LABEL goes first and the cause is discharged
        # only once it is actually gone (#6999 F4 round 4): withdrawing first
        # meant a failed removal returned FAILED with the label still on the
        # issue and its last cause already erased - the unowned live block this
        # owner exists to make impossible.
        return self._take_label_off(request.target, request.reason)

    def force_clear(self, target: int, reason: str) -> BlockOutcome:
        """End every cause this owner can settle, or REFUSE (#6999 F3 round 4).

        Operator and terminal-recovery intent: it overrides the causes recorded
        here rather than asking them, because those exist only while the label
        does. But two causes are not this owner's to settle, and pretending
        otherwise was a lie with teeth:

        * a QUARANTINE row means a terminal nobody can account for may still be
          running the issue's work. It re-applies the block on every scan, so
          clearing the label here would be undone within a tick - after the
          operator had been told the issue was requeued, and after the queue had
          acted on that. Relaunching work whose quarantined terminal is still
          alive is the duplicate execution the quarantine exists to prevent.
        * a TECH-LEAD escalation keeps its own marker label, which this owner
          does not remove; its lifecycle recovers the block from that marker on
          the next reconcile, so the clear would not stick either.

        So a force-clear that meets either one is REFUSED, with the label left
        exactly where it is. ``HELD_BY_ANOTHER_CAUSE`` is the honest answer: the
        operator learns the issue is still blocked and why, instead of being
        told it was cleared by a command that could not clear it.
        """
        held = self.unsettleable_holders(target)
        if held:
            logger.warning(
                "[BLOCK] Refusing to force-clear needs-human on #%d (%s): %s "
                "still requires it, and this command cannot settle that owner",
                target,
                reason,
                ", ".join(cause.value for cause in held),
            )
            return BlockOutcome.HELD_BY_ANOTHER_CAUSE
        return self._take_label_off(target, reason)

    def held_by_another_cause(
        self, issue_number: int, *, excluding: NeedsHumanCause
    ) -> bool:
        return any(
            self._holds(cause, issue_number)
            for cause in NeedsHumanCause
            if cause is not excluding
        )

    def unsettleable_holders(self, issue_number: int) -> tuple[NeedsHumanCause, ...]:
        """Which of the causes this owner cannot settle are holding the block.

        Named for the operator: a refusal that cannot say WHICH lifecycle is
        holding an issue leaves them with nothing to act on.
        """
        return tuple(
            cause
            for cause in _UNSETTLEABLE_BY_FORCE
            if self._holds(cause, issue_number)
        )

    def forget(self, target: int) -> None:
        self.causes.clear_needs_human_causes(target)

    # -- internals ---------------------------------------------------------

    def _take_label_off(self, target: int, reason: str) -> BlockOutcome:
        try:
            self.labels.remove_label(target, self.needs_human_label)
        except Exception:
            logger.exception(
                "[BLOCK] Could not clear the shared needs-human block on #%d "
                "(%s); the causes stay recorded so the next attempt retries",
                target,
                reason,
            )
            return BlockOutcome.FAILED
        # The label WAS the record every cause stood on, so with it gone they
        # are all stale. Leaving one behind is how a bypassing removal let a
        # later re-added label inherit a cause nothing was asserting.
        self.forget(target)
        return BlockOutcome.CLEARED

    def _recorded(self, request: HumanBlockRequest) -> bool:
        """Record this cause against the CURRENT generation of the label.

        When the label is absent the incoming cause opens a new generation, so
        every earlier row is replaced in ONE transaction rather than cleared and
        then written: a clear-then-record can die in between and leave the new
        cause recorded beside the stale one, which is precisely the state being
        prevented.

        A failure here aborts the acquisition rather than proceeding, because
        the alternative is applying a live block whose provenance is wrong.
        """
        if request.cause in _SELF_RECORDING_CAUSES:
            return True
        try:
            if self.needs_human_label in self._live_labels(request.target):
                self.causes.record_needs_human_cause(
                    request.target, request.cause.value, reason=request.reason
                )
            else:
                self.causes.restart_needs_human_causes(
                    request.target, request.cause.value, reason=request.reason
                )
        except Exception:
            logger.exception(
                "[BLOCK] Could not record %s as a cause of the shared block on "
                "#%d; refusing to apply a block whose provenance is unknown",
                request.cause.value,
                request.target,
            )
            return False
        return True

    def _withdraw(self, request: HumanBlockRequest) -> None:
        if request.cause not in _SELF_RECORDING_CAUSES:
            self.causes.withdraw_needs_human_cause(
                request.target, request.cause.value
            )

    def _holds(self, cause: NeedsHumanCause, issue_number: int) -> bool:
        if cause is NeedsHumanCause.CLAIM_QUARANTINE:
            return self._quarantine_holds(issue_number)
        if cause is NeedsHumanCause.TECH_LEAD_ESCALATION:
            return self._tech_lead_holds(issue_number)
        return self._recorded_cause_holds(cause, issue_number)

    def _quarantine_holds(self, issue_number: int) -> bool:
        try:
            return issue_number in self.quarantined_issue_numbers()
        except Exception:
            logger.exception(
                "[BLOCK] Could not read quarantine state for #%d; treating the "
                "shared needs-human block as still required",
                issue_number,
            )
            return True

    def _recorded_cause_holds(
        self, cause: NeedsHumanCause, issue_number: int
    ) -> bool:
        """A recorded cause, reconciled against the live label.

        The label is authoritative. A human who clears ``needs-human`` ends
        every cause at once and tells nobody, so a row found standing over an
        absent label is stale by definition and is dropped here rather than
        holding the block open for a cause that no longer exists.
        """
        try:
            recorded = self.causes.needs_human_causes(issue_number)
        except Exception:
            logger.exception(
                "[BLOCK] Could not read needs-human causes for #%d; treating "
                "the shared block as still required",
                issue_number,
            )
            return True
        if cause.value not in recorded:
            return False
        if self.needs_human_label not in self._live_labels(issue_number):
            self.forget(issue_number)
            return False
        return True

    def _tech_lead_holds(self, issue_number: int) -> bool:
        return self.tech_lead_marker in self._live_labels(issue_number)

    def _live_labels(self, issue_number: int) -> frozenset[str]:
        """Fresh labels, or a fail-closed answer that keeps every block.

        A read failure must not read as "no cause holds this": returning both
        markers means every caller concludes the block is still required, which
        is the safe direction for a signal only a human can replace.
        """
        try:
            return frozenset(self.read_labels(issue_number))
        except Exception:
            logger.exception(
                "[BLOCK] Could not read labels for #%d; treating the shared "
                "needs-human block as still required",
                issue_number,
            )
            return frozenset({self.tech_lead_marker, self.needs_human_label})


@dataclass(frozen=True, slots=True)
class _NoOtherCauses:
    """No shared block is governed here at all.

    An explicit null object for composition paths that genuinely have no owner
    wired (and for unit tests of a single lifecycle), rather than an optional
    every caller would have to re-check. ``owns`` answers False for every
    label, so nothing routes a mutation into it by accident.
    """

    def owns(self, label: str) -> bool:
        del label
        return False

    def acquire(self, request: HumanBlockRequest) -> BlockOutcome:
        del request
        return BlockOutcome.UNGOVERNED

    def release(self, request: HumanBlockRequest) -> BlockOutcome:
        del request
        return BlockOutcome.UNGOVERNED

    def force_clear(self, target: int, reason: str) -> BlockOutcome:
        del target, reason
        return BlockOutcome.UNGOVERNED

    def held_by_another_cause(
        self, issue_number: int, *, excluding: NeedsHumanCause
    ) -> bool:
        del issue_number, excluding  # no second owner to ask
        return False

    def unsettleable_holders(self, issue_number: int) -> tuple[NeedsHumanCause, ...]:
        del issue_number
        return ()


NO_OTHER_NEEDS_HUMAN_CAUSES: SharedNeedsHumanBlock = _NoOtherCauses()


__all__ = [
    "NO_OTHER_NEEDS_HUMAN_CAUSES",
    "UNCAUSED_BLOCK_MUTATION",
    "BlockLabelWriter",
    "BlockOutcome",
    "HumanBlockRequest",
    "NeedsHumanBlock",
    "NeedsHumanCause",
    "SharedNeedsHumanBlock",
]
