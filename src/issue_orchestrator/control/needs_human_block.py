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
    #: Every other orchestrator escalation: a session that ended without a
    #: completion record, publish failures past their bound, an invalid
    #: completion record, a stuck sweep. One token rather than one per call
    #: site, because a remover needs to know THAT another lifecycle requires the
    #: block, not which of a dozen planner branches asserted it - and one token
    #: cannot be forgotten by the next branch someone adds.
    SESSION_LIFECYCLE = "session_lifecycle"


class BlockMutation(Enum):
    """What an applier is doing to a label, from this owner's point of view.

    Three moments, because each one means something different to provenance:
    an acquisition records a cause, a release must first ask whether it is the
    last one, and a committed removal ends every cause at once.
    """

    #: The add committed - whether it applied the label or found it present.
    ACQUIRED = "acquired"
    #: A removal is about to be attempted. The only mutation ever withheld.
    RELEASING = "releasing"
    #: The removal committed, so the record every cause stood on is gone.
    RELEASED = "released"


class SharedNeedsHumanBlock(Protocol):
    """The one owner of the shared ``needs-human`` label's provenance.

    Every orchestrator acquisition and release of that label goes through here,
    so a remover can always tell whether it is the last cause standing.
    """

    def owns(self, label: str) -> bool:
        """Whether ``label`` is the shared block this owner governs."""
        ...

    def blocks_mutation(
        self,
        issue_number: int,
        label: str,
        mutation: BlockMutation,
        *,
        cause: NeedsHumanCause,
        reason: str = "",
    ) -> bool:
        """Take note of one label mutation; say whether it must NOT proceed.

        Only a ``RELEASING`` mutation is ever withheld, and only when another
        cause still requires the block.
        """
        ...

    def held_by_another_cause(
        self, issue_number: int, *, excluding: NeedsHumanCause
    ) -> bool:
        """Whether a cause OTHER than ``excluding`` still requires the block."""
        ...


@dataclass(frozen=True, slots=True)
class NeedsHumanBlock:
    """The bounded owner of every orchestrator-owned cause of the shared block.

    Provenance is read from wherever each cause already keeps it rather than
    being copied into one table: the tech-lead marker is a label, a quarantine
    is a ledger row, and only the causes that had no home at all get one here.
    Copying the other two would create a second truth to keep in sync, and the
    whole defect this closes is two records of one fact disagreeing.
    """

    #: The shared label itself. Held so the applier can ask "is this the one?"
    #: instead of every caller re-deriving it from configuration.
    needs_human_label: str
    #: The tech-lead lifecycle's durable provenance label.
    tech_lead_marker: str
    #: Live label read for one issue. Must bypass caches: a stale observation
    #: here can retract a block a human is waiting on.
    read_labels: Callable[[int], Sequence[str]]
    #: Issues held open by a quarantine whose cause has not been released.
    quarantined_issue_numbers: Callable[[], frozenset[int]]
    #: Durable rows for the causes that keep their provenance nowhere else.
    causes: NeedsHumanCauseStore

    def owns(self, label: str) -> bool:
        return label == self.needs_human_label

    def blocks_mutation(
        self,
        issue_number: int,
        label: str,
        mutation: BlockMutation,
        *,
        cause: NeedsHumanCause,
        reason: str = "",
    ) -> bool:
        """Every mutation of the shared label, through one guarded entry.

        The applier reports EVERY label it touches and this owner ignores all
        but its own, so no call site can create or clear a block with no
        discoverable owner - neither the ten that exist today nor the eleventh
        someone adds later (#6999 F2 round 2). One entry rather than three
        because "is this my label?" is one rule: asked in three places it is a
        rule that can be answered differently in three places, which is the
        exact shape of the scatter this owner replaced.
        """
        if not self.owns(label):
            return False
        return _BLOCK_MUTATIONS[mutation](self, issue_number, cause, reason)

    def acquired(
        self, issue_number: int, cause: NeedsHumanCause, reason: str
    ) -> bool:
        """Record which lifecycle is asserting the block. Never withholds.

        Recording an ALREADY-PRESENT label matters just as much as a fresh one:
        a cause arriving second is exactly the case that used to leave no trace
        and lose its block.

        The tech-lead marker and the quarantine row ARE their causes' records,
        so re-recording them here would duplicate a fact that already exists and
        make its two copies capable of disagreeing. Their lifecycles clear their
        own provenance on their own terms, which a row here would not follow.
        """
        if cause is NeedsHumanCause.SESSION_LIFECYCLE:
            self.causes.record_needs_human_cause(
                issue_number, cause.value, reason=reason
            )
        return False

    def releasing(
        self, issue_number: int, cause: NeedsHumanCause, reason: str
    ) -> bool:
        """Withdraw ``cause``, and say whether the label must nonetheless stay.

        The backstop that makes the ownership rule unconditional. The quarantine
        and tech-lead lifecycles already ask before emitting their removal, but
        they are not the only removers and a future one should not have to
        remember: withdrawing here can only take the label off when it was the
        last cause, whoever asked.
        """
        del reason
        if cause is NeedsHumanCause.SESSION_LIFECYCLE:
            self.causes.withdraw_needs_human_cause(issue_number, cause.value)
        return self.held_by_another_cause(issue_number, excluding=cause)

    def released(
        self, issue_number: int, cause: NeedsHumanCause, reason: str
    ) -> bool:
        """The label WAS the record, so with it gone every cause is stale."""
        del cause, reason
        self.forget(issue_number)
        return False

    def forget(self, issue_number: int) -> None:
        self.causes.clear_needs_human_causes(issue_number)

    def held_by_another_cause(
        self, issue_number: int, *, excluding: NeedsHumanCause
    ) -> bool:
        return any(
            self._holds(cause, issue_number)
            for cause in NeedsHumanCause
            if cause is not excluding
        )

    def _holds(self, cause: NeedsHumanCause, issue_number: int) -> bool:
        if cause is NeedsHumanCause.CLAIM_QUARANTINE:
            return self._quarantine_holds(issue_number)
        if cause is NeedsHumanCause.SESSION_LIFECYCLE:
            return self._session_lifecycle_holds(issue_number)
        return self._tech_lead_holds(issue_number)

    def _quarantine_holds(self, issue_number: int) -> bool:
        try:
            return issue_number in self.quarantined_issue_numbers()
        except Exception:
            logger.exception(
                "[BLOCK] Could not read quarantine state for issue #%d; treating "
                "the shared needs-human block as still required",
                issue_number,
            )
            return True

    def _session_lifecycle_holds(self, issue_number: int) -> bool:
        """A recorded session/planner cause, reconciled against the live label.

        The label is authoritative. A human who clears ``needs-human`` ends
        every cause at once and tells nobody, so a row found standing over an
        absent label is stale by definition and is dropped here rather than
        being allowed to hold the block open for a cause that no longer exists.
        """
        try:
            recorded = self.causes.needs_human_causes(issue_number)
        except Exception:
            logger.exception(
                "[BLOCK] Could not read needs-human causes for issue #%d; "
                "treating the shared block as still required",
                issue_number,
            )
            return True
        if NeedsHumanCause.SESSION_LIFECYCLE.value not in recorded:
            return False
        if not self._label_present(issue_number):
            self.forget(issue_number)
            return False
        return True

    def _tech_lead_holds(self, issue_number: int) -> bool:
        return self.tech_lead_marker in self._live_labels(issue_number)

    def _label_present(self, issue_number: int) -> bool:
        return self.needs_human_label in self._live_labels(issue_number)

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
                "[BLOCK] Could not read labels for issue #%d; treating the shared "
                "needs-human block as still required",
                issue_number,
            )
            return frozenset({self.tech_lead_marker, self.needs_human_label})


@dataclass(frozen=True, slots=True)
class _NoOtherCauses:
    """No shared block is governed here at all.

    An explicit null object for composition paths that genuinely have no owner
    wired (and for unit tests of a single lifecycle), rather than an optional
    every caller would have to re-check. ``owns`` answers False for every label,
    so an applier holding this one behaves exactly as it did before the owner
    existed.
    """

    def owns(self, label: str) -> bool:
        del label
        return False

    def blocks_mutation(
        self,
        issue_number: int,
        label: str,
        mutation: BlockMutation,
        *,
        cause: NeedsHumanCause,
        reason: str = "",
    ) -> bool:
        del issue_number, label, mutation, cause, reason
        return False

    def forget(self, issue_number: int) -> None:
        del issue_number

    def held_by_another_cause(
        self, issue_number: int, *, excluding: NeedsHumanCause
    ) -> bool:
        del issue_number, excluding  # no second owner to ask
        return False


# One entry per mutation. A table rather than branches inside the entry point,
# so "what does this owner do about X" has a single enumerable answer and a
# mutation added later cannot inherit another one's behaviour by accident.
_BLOCK_MUTATIONS: dict[
    BlockMutation, Callable[[NeedsHumanBlock, int, NeedsHumanCause, str], bool]
] = {
    BlockMutation.ACQUIRED: NeedsHumanBlock.acquired,
    BlockMutation.RELEASING: NeedsHumanBlock.releasing,
    BlockMutation.RELEASED: NeedsHumanBlock.released,
}


NO_OTHER_NEEDS_HUMAN_CAUSES: SharedNeedsHumanBlock = _NoOtherCauses()


__all__ = [
    "NO_OTHER_NEEDS_HUMAN_CAUSES",
    "BlockMutation",
    "NeedsHumanBlock",
    "NeedsHumanCause",
    "SharedNeedsHumanBlock",
]
