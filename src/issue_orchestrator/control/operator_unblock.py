"""What an operator's "retry" or "dismiss" actually does to an issue (#6999 A2).

Both commands mean the same thing to the board: take the labels that are
holding this issue back off, and only then let the orchestrator treat it as
free again. The endpoints used to spell that out themselves - a loop of raw
label writes, one typed owner call in the middle, and local-state pruning
afterwards - which is how the two drifted apart. Retry learned to respect a
refusal from the shared-block owner; dismiss caught the same refusal, threw it
away, pruned its state and told the operator the issue was dismissed.

So the sequence is one owner command with a typed outcome, and both endpoints
consume it. Two rules live here rather than in either endpoint:

* the SHARED BLOCK goes first, because it is the one label whose owner can
  legitimately refuse. A refusal means a quarantine or a tech-lead escalation
  still requires it, and both re-assert it within a tick - so an operator who
  is told "dismissed" would watch it come straight back;
* a refusal stops the rest. In particular it must not go on to strip the
  tech-lead marker, which is frequently the very provenance the refusal was
  protecting: removing it would leave the shared label standing with nothing
  left to explain or recover it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from .needs_human_block import BlockOutcome, SharedNeedsHumanBlock
from .retry_policy import labels_to_remove_for_retry

if TYPE_CHECKING:
    from ..ports.repository_host import RepositoryHost
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OperatorUnblockOutcome:
    """What an operator command managed to clear, and what stopped it."""

    removed: tuple[str, ...] = ()
    #: Ordinary labels that could not be removed. Historically tolerated on the
    #: dismiss path - a label that is already gone raises here too - so it is
    #: kept SEPARATE from the shared block, whose absence is the whole question.
    failed: tuple[str, ...] = ()
    #: The shared block, when it is still on the issue: its owner refused, or
    #: the write did not commit. Either way the issue is not unblocked, and
    #: this is the one failure neither command may look past.
    blocked: str | None = None
    #: Which causes are still holding it, for the operator to act on. Empty
    #: when the block simply failed to clear rather than being refused.
    held_by: tuple[str, ...] = field(default_factory=tuple)

    @property
    def committed(self) -> bool:
        """Whether the issue is genuinely unblocked.

        The gate on everything that follows: pruning queue and history state
        for an issue GitHub still blocks just makes the planner relaunch into
        it, and telling an operator it was cleared is a claim the next
        reconciliation pass contradicts.
        """
        return not self.failed and self.blocked is None

    def refusal(self, issue_number: int, performed: str) -> dict[str, Any]:
        """Describe a refusal for the operator who asked (#6999 F3 round 5).

        Shaped here rather than in either endpoint because a refusal must mean
        the same thing to both: the label that is still on the issue, the
        lifecycles holding it, and what was cleared before the command stopped.
        ``performed`` is the past participle of the command ("retried",
        "dismissed"), so the operator reads what did NOT happen.
        """
        if self.blocked is None:
            raise ValueError("not a refusal: nothing is blocking this issue")
        cause = (
            f"{', '.join(self.held_by)} still requires it"
            if self.held_by
            else "it could not be cleared"
        )
        return {
            "success": False,
            "error": (
                f"Issue #{issue_number} was not {performed}: {self.blocked} is "
                f"still on the issue because {cause}."
            ),
            "removed_labels": list(self.removed),
            "failed_labels": [self.blocked],
            "held_by": list(self.held_by),
        }


@dataclass(frozen=True, slots=True)
class OperatorUnblocker:
    """The one command behind retry and dismiss.

    Deliberately behaviour-level: callers say "unblock this issue", not "remove
    these five labels and then decide what that meant". The distinction matters
    because one of those labels answers back.
    """

    repository_host: "RepositoryHost"
    labels: "LabelManager"
    block: SharedNeedsHumanBlock

    def retry(
        self, issue_number: int, current_labels: Sequence[str]
    ) -> OperatorUnblockOutcome:
        """Clear what is gating a retry, so the planner may pick the issue up.

        WHICH labels those are is a retry-policy question, not an HTTP one, so
        it is answered here: an endpoint that assembles the set itself is one
        that can assemble a different set than dismiss does. The observation it
        needs - what the issue currently wears - is passed IN, because control
        reads labels through a fresh reader its caller owns, never through the
        cached repository read.
        """
        return self._unblock(
            issue_number,
            labels_to_remove_for_retry(current_labels, self.labels),
            "retry",
        )

    def dismiss(self, issue_number: int) -> OperatorUnblockOutcome:
        """Clear everything holding the issue on the board, retry or not."""
        lm = self.labels
        return self._unblock(
            issue_number,
            [lm.blocked, lm.needs_human, lm.tech_lead_needs_human,
             lm.blocked_failed, lm.in_progress],
            "dismiss",
        )

    def _unblock(
        self, issue_number: int, labels: Sequence[str], intent: str
    ) -> OperatorUnblockOutcome:
        """Clear ``labels``, shared block first, stopping if its owner refuses."""
        governed = [label for label in labels if self.block.owns(label)]
        rest = [label for label in labels if not self.block.owns(label)]

        removed: list[str] = []
        for label in governed:
            outcome = self.block.force_clear(issue_number, f"operator {intent}")
            if outcome is BlockOutcome.HELD_BY_ANOTHER_CAUSE:
                held = self._holders(issue_number)
                logger.warning(
                    "[%s] Issue #%d not unblocked: %s still requires %r, so "
                    "nothing else was cleared either - stripping its "
                    "provenance would leave the block with nothing to explain "
                    "it",
                    intent,
                    issue_number,
                    ", ".join(held) or "another lifecycle",
                    label,
                )
                return OperatorUnblockOutcome(
                    removed=tuple(removed), blocked=label, held_by=held
                )
            if not outcome.committed:
                logger.error(
                    "[%s] Issue #%d: the shared block did not clear; nothing "
                    "else was cleared either",
                    intent,
                    issue_number,
                )
                return OperatorUnblockOutcome(
                    removed=tuple(removed), blocked=label
                )
            removed.append(label)

        failed: list[str] = []
        for label in rest:
            try:
                self.repository_host.remove_label(issue_number, label)
            except Exception:
                logger.warning(
                    "[%s] Issue #%d: could not remove %r", intent, issue_number, label
                )
                failed.append(label)
                continue
            removed.append(label)
        return OperatorUnblockOutcome(removed=tuple(removed), failed=tuple(failed))

    def _holders(self, issue_number: int) -> tuple[str, ...]:
        """The causes a refusal is protecting, named for the operator."""
        return tuple(
            cause.value for cause in self.block.unsettleable_holders(issue_number)
        )


__all__ = ["OperatorUnblockOutcome", "OperatorUnblocker"]
