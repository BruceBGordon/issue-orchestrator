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
        """End EVERY cause and take the label off. Operator/terminal intent."""
        ...

    def held_by_another_cause(
        self, issue_number: int, *, excluding: NeedsHumanCause
    ) -> bool:
        """Whether a cause OTHER than ``excluding`` still requires the block."""
        ...


#: Causes whose provenance already lives somewhere durable and lifecycle-owned:
#: the tech-lead marker label and the quarantine ledger row. This owner reads
#: them but never keeps a second copy, because two records of one fact are two
#: records that can disagree - the defect this whole boundary closes.
_SELF_RECORDING_CAUSES = frozenset(
    {NeedsHumanCause.TECH_LEAD_ESCALATION, NeedsHumanCause.CLAIM_QUARANTINE}
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
        try:
            self.labels.add_label(request.target, self.needs_human_label)
        except Exception:
            logger.exception(
                "[BLOCK] Could not apply the shared needs-human block to #%d "
                "for %s; recording nothing, so the next tick retries cleanly",
                request.target,
                request.cause.value,
            )
            return BlockOutcome.FAILED
        self._record(request)
        return BlockOutcome.HELD

    def release(self, request: HumanBlockRequest) -> BlockOutcome:
        """Withdraw one cause; take the label off only if it was the last.

        The ONE way an orchestrator lifecycle gives its own block back.
        Withdrawing always happens - this cause IS discharged - but the label
        only follows when nothing else is standing on it. That is the defect
        this owner closes, and it now holds for every remover rather than for
        the two that remembered to ask.
        """
        self._withdraw(request)
        if self.held_by_another_cause(request.target, excluding=request.cause):
            logger.info(
                "[BLOCK] #%d keeps needs-human after %s withdrew: another "
                "lifecycle still requires it",
                request.target,
                request.cause.value,
            )
            return BlockOutcome.HELD_BY_ANOTHER_CAUSE
        return self._take_label_off(request.target, request.reason)

    def force_clear(self, target: int, reason: str) -> BlockOutcome:
        """End EVERY cause and take the label off (#6999 F2 round 3).

        Operator and terminal-recovery intent, which is categorically different
        from a lifecycle giving its own block back: it OVERRIDES the other
        causes rather than asking them. Naming that intent is what stops a
        force-clear reading as one cause's release - and, in the other
        direction, what stops a bypassing removal leaving rows behind for a
        later re-added label to inherit.
        """
        return self._take_label_off(target, reason)

    def held_by_another_cause(
        self, issue_number: int, *, excluding: NeedsHumanCause
    ) -> bool:
        return any(
            self._holds(cause, issue_number)
            for cause in NeedsHumanCause
            if cause is not excluding
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

    def _record(self, request: HumanBlockRequest) -> None:
        if request.cause not in _SELF_RECORDING_CAUSES:
            self.causes.record_needs_human_cause(
                request.target, request.cause.value, reason=request.reason
            )

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
