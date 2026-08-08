"""Who requires the shared ``needs-human`` block, and why (#6999 F4/A2).

``needs-human`` is ONE label with SEVERAL independent causes. Two of them are
orchestrator-owned and durable:

* a tech-lead failure investigation that exhausted its launch budget, whose
  provenance is its own marker label
  (:mod:`.tech_lead_needs_human_reconcile`);
* a run quarantined for an unreadable or unrestorable pending-work claim, whose
  provenance is its row in the quarantine ledger (:mod:`.claim_quarantine`).

Anything else wearing the label is human intent.

Each owner used to reason about the shared label from inside its OWN provenance
alone, and that is not enough to decide a removal. The concrete loss: a
quarantine acquires ``needs-human``; while it is present a tech-lead escalation
becomes required, sees a bare label it did not apply, and deliberately declines
to claim it by recording no marker; the quarantine then resolves and takes
"its" label off. The tech-lead escalation is now left with neither the blocking
label nor the marker its own recovery reads, so an issue a human was told to
look at silently returns to the board.

This module is the single place that answers "does any OTHER live cause still
require this block?", so no owner has to know how another one records itself.
Reads FAIL CLOSED: a cause that cannot be evaluated is reported as present,
because wrongly keeping a block costs a tick of attention and wrongly removing
one loses a human's only signal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

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


class SharedNeedsHumanBlock(Protocol):
    """The question every owner of the shared block has to ask before removing it."""

    def held_by_another_cause(
        self, issue_number: int, *, excluding: NeedsHumanCause
    ) -> bool:
        """Whether a cause OTHER than ``excluding`` still requires the block."""
        ...


@dataclass(frozen=True, slots=True)
class NeedsHumanBlock:
    """The one reader of every orchestrator-owned cause of the shared block.

    Deliberately a reader, not an applier: each cause already commits its own
    provenance through its own owner (a marker label, a ledger row), and moving
    those writes here would centralise nothing that is currently duplicated
    while making two very different durability stories share one code path.
    What WAS missing is the cross-owner read, and that is all this provides.
    """

    #: The tech-lead lifecycle's durable provenance label.
    tech_lead_marker: str
    #: Live label read for one issue. Must bypass caches: a stale observation
    #: here can retract a block a human is waiting on.
    read_labels: Callable[[int], Sequence[str]]
    #: Issues held open by a quarantine whose cause has not been released.
    quarantined_issue_numbers: Callable[[], frozenset[int]]

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

    def _tech_lead_holds(self, issue_number: int) -> bool:
        try:
            return self.tech_lead_marker in set(self.read_labels(issue_number))
        except Exception:
            logger.exception(
                "[BLOCK] Could not read labels for issue #%d; treating the shared "
                "needs-human block as still required",
                issue_number,
            )
            return True


@dataclass(frozen=True, slots=True)
class _NoOtherCauses:
    """No orchestrator-owned cause but the caller's own.

    An explicit null object for composition paths that genuinely have no second
    owner wired (and for unit tests of a single lifecycle), rather than an
    optional every caller would have to re-check.
    """

    def held_by_another_cause(
        self, issue_number: int, *, excluding: NeedsHumanCause
    ) -> bool:
        del issue_number, excluding  # no second owner to ask
        return False


NO_OTHER_NEEDS_HUMAN_CAUSES: SharedNeedsHumanBlock = _NoOtherCauses()


__all__ = [
    "NO_OTHER_NEEDS_HUMAN_CAUSES",
    "NeedsHumanBlock",
    "NeedsHumanCause",
    "SharedNeedsHumanBlock",
]
