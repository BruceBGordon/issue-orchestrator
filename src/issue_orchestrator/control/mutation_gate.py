"""The optimistic-concurrency gate every mutation crosses (#6957 round-2 F4/A5).

``ActionApplier`` owns a lot; this is the one part of it that decides whether a
write may happen AT ALL, so it gets to be its own named owner rather than two
methods buried in a 1800-line dispatcher. Two rules, and they are the whole
module:

1. **Unknown is not empty.** The reader raises when it cannot read an issue
   (:class:`~..ports.fresh_issue_reader.FreshIssueReadError`), and this turns
   that into "unknown", never into an observed empty label set. The distinction
   is load-bearing: an empty set SATISFIES an expectation that merely forbids
   ``io:needs-reconcile``, so conflating them let a failed GitHub read walk the
   control plane straight through an operator pause.
2. **Unknown fails closed.** An action carrying expectations that cannot be
   verified raises ``ReconciliationRequired`` — the same outcome as a verified
   violation — so the orchestrator pauses the issue instead of guessing.

Actions with no ``ExpectedState``, and appliers with reconciliation disabled,
pass straight through; that is the pre-existing contract and this module does
not change it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..infra.logging_config import issue_log
from ..ports.fresh_issue_reader import FreshIssueReadError, FreshIssueReader
from .action_base import Action
from .reconciliation import (
    ExternalSnapshot,
    ReconciliationRequired,
    require_reconciliation,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationGate:
    """Reads current labels and refuses mutations the board no longer allows."""

    fresh_issue_reader: FreshIssueReader | None
    reconcile: bool

    def current_labels(self, issue_number: int) -> set[str] | None:
        """The issue's CURRENT labels, or None when they could not be OBSERVED.

        None means "unknown", never "empty" — see the module docstring.
        ``FreshIssueReadError`` is the port's explicit failure; the broad catch
        keeps any other adapter fault on the same fail-closed path rather than
        trusting a half-formed answer.
        """
        if self.fresh_issue_reader is None:
            return None
        try:
            return set(self.fresh_issue_reader.read_issue_labels(issue_number))
        except FreshIssueReadError as exc:
            logger.warning(
                issue_log(
                    issue_number,
                    "Fresh label read failed; reconciliation state is unknown: %s",
                ),
                exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                issue_log(issue_number, "Failed to fetch labels for reconciliation: %s"),
                exc,
            )
            return None

    def require_expected(self, action: Action, issue_number: int) -> None:
        """Enforce *action*'s expected state before it mutates *issue_number*.

        Raises:
            ReconciliationRequired: the current state violates the expectation,
                or could not be observed at all.
        """
        if action.expected is None:
            return
        if not self.reconcile:
            return

        current_labels = self.current_labels(issue_number)
        if current_labels is None:
            logger.warning(
                issue_log(
                    issue_number,
                    "Reconciliation required but cannot fetch labels - failing closed",
                ),
            )
            raise ReconciliationRequired(
                entity_type="issue",
                entity_id=issue_number,
                expected=ExternalSnapshot.for_issue(
                    issue_number, set(action.expected.required_labels)
                ),
                actual=ExternalSnapshot.for_issue(issue_number, set()),
                reason="Cannot fetch current labels to verify expected state",
            )

        # Raises ReconciliationRequired when the constraints are not satisfied.
        require_reconciliation(
            action.expected,
            ExternalSnapshot.for_issue(issue_number, current_labels),
            entity_type="issue",
        )
