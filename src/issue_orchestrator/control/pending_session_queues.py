"""Ownership of the orchestrator's in-memory pending queues.

Separated from launch routing (#6999 A2 round 1) because they are different
jobs: routing decides how one launch outcome settles, while this module owns
what may enter and leave each queue at all. Producers say WHICH variant they
are queueing and this owner constructs the item, applies the one deduplication
rule, and returns a typed outcome - they never touch the state lists.

Admission is the other half of the same ownership: a request that comes back
from a dead session re-enters through the very table that says how each queue
removes an item, so a queue's duplicate rule lives in exactly one place whether
the item arrives from discovery or from a provider-deferred relaunch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

from ..domain.models import (
    DiscoveredFailure,
    PendingRetrospectiveReview,
    PendingReview,
    PendingRework,
    PendingTechLeadReview,
    PendingValidationRetry,
)
from ..domain.pending_work import (
    PendingWorkClaim,
    PendingWorkKind,
    PendingWorkRequest,
)
from ..domain.tech_lead_session import TechLeadSessionFlavor

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState

logger = logging.getLogger(__name__)


class TechLeadQueueOutcome(Enum):
    """Explicit result of asking the queue owner to enqueue tech_lead work."""

    QUEUED = "queued"
    DUPLICATE = "duplicate"


# Bound on retryable launch failures per queued tech_lead item. Three attempts
# ride out a transient SQLite/log/filesystem blip without relaunch-looping a
# genuinely broken input forever; after the third failure the item is dropped
# and the drop is surfaced loudly (fail-fast-but-not-silent).
TECH_LEAD_LAUNCH_RETRY_LIMIT = 3


class TechLeadRetentionOutcome(Enum):
    """Explicit result of retaining a queued tech_lead item after a retryable failure."""

    RETAINED = "retained"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class PendingSessionQueues:
    """Owner for pending session queues: launch-routing removals + tech_lead intake.

    Tech Lead intake is behavior-level (#6768 round 3): producers say WHICH
    variant they are queueing (batch review, failure investigation, or health
    review) and this owner constructs the ``PendingTechLeadReview``, applies the
    single deduplication rule (by issue number against the pending queue), and
    returns an explicit :class:`TechLeadQueueOutcome`. Producers never touch the
    dataclass or the state list.
    """

    state: "OrchestratorState"

    def remove_review(self, pr_number: int) -> None:
        self.state.pending_reviews[:] = [
            r for r in self.state.pending_reviews if r.pr_number != pr_number
        ]

    def remove_retrospective_review(self, issue_number: int) -> None:
        self.state.pending_retrospective_reviews[:] = [
            r
            for r in self.state.pending_retrospective_reviews
            if r.issue_number != issue_number
        ]

    def remove_rework(self, rework: PendingRework) -> None:
        self.state.pending_reworks[:] = [
            r for r in self.state.pending_reworks if r.issue_key != rework.issue_key
        ]

    def remove_validation_retry(self, issue_number: int) -> None:
        self.state.pending_validation_retries[:] = [
            queued
            for queued in self.state.pending_validation_retries
            if queued.issue_number != issue_number
        ]

    def remove_tech_lead(self, issue_number: int) -> None:
        self.state.pending_tech_lead_reviews[:] = [
            t
            for t in self.state.pending_tech_lead_reviews
            if t.issue_number != issue_number
        ]

    def queue_batch_review(self, issue_number: int, title: str) -> TechLeadQueueOutcome:
        """Queue a threshold-created batch tracking issue (audits the PR manifest)."""
        return self._queue_tech_lead(
            PendingTechLeadReview(
                issue_number, title, flavor=TechLeadSessionFlavor.BATCH_REVIEW
            )
        )

    def queue_health_review(
        self,
        issue_number: int,
        title: str,
        *,
        problem_cohort: tuple[DiscoveredFailure, ...] = (),
    ) -> TechLeadQueueOutcome:
        """Queue an interval-created health-review anchor (ADR-0031 §4); like a
        batch review it carries no singular failure context. An unscheduled
        problem-storm review instead carries its typed cohort so the later
        launch snapshot cannot lose the trigger facts at end-of-tick."""
        return self._queue_tech_lead(
            PendingTechLeadReview(
                issue_number,
                title,
                flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
                problem_cohort=problem_cohort,
            )
        )

    def remove_failure_investigations(
        self, issue_numbers: frozenset[int]
    ) -> None:
        """Remove only storm-superseded individual investigation entries.

        Batch and health anchors may share an issue number with other tech_lead
        bookkeeping and must never be removed by a problem-cohort transition.
        """
        self.state.pending_tech_lead_reviews[:] = [
            item
            for item in self.state.pending_tech_lead_reviews
            if not (
                item.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
                and item.issue_number in issue_numbers
            )
        ]

    def queue_failure_investigation(
        self, issue_number: int, title: str, *, failure: DiscoveredFailure
    ) -> TechLeadQueueOutcome:
        """Queue a focused investigation of one failed issue.

        ``failure`` is required (non-optional): the queue item is the only
        carrier of the typed triggering-failure context once the per-tick
        ``discovered_failures`` buffer is cleared after planning (the
        launch-time board snapshot reads it from here).
        ``PendingTechLeadReview.__post_init__`` stays as defense-in-depth
        against untyped callers passing ``None`` anyway.
        """
        return self._queue_tech_lead(
            PendingTechLeadReview(
                issue_number,
                title,
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                failure=failure,
            )
        )

    def retain_tech_lead_for_retry(self, issue_number: int) -> TechLeadRetentionOutcome:
        """Bounded retention of a queued tech_lead item after a retryable launch failure.

        Before escalation starts, failure investigations have no labels-as-
        truth recovery: the queued item is the only record (the per-tick
        ``discovered_failures`` buffer is cleared after planning), so a
        transient required-input prep failure must retain it for retry, not
        delete it. Retention is bounded by ``TECH_LEAD_LAUNCH_RETRY_LIMIT``:
        once exhausted ``EXHAUSTED`` is returned, but the item is NOT removed
        here (#6771 round 4). Destructive queue removal must not precede the
        lifecycle's committed needs-human transition, so the launch caller
        commits the drop via
        ``remove_tech_lead`` only after ``escalate_issue_needs_human`` succeeds;
        on escalation failure the item is retained and re-attempted.

        Asking to retain an item that is not queued is an invariant violation
        upstream (the launch path holds the item it just failed to launch);
        fail fast rather than silently absorbing it.
        """
        item = next(
            (
                t
                for t in self.state.pending_tech_lead_reviews
                if t.issue_number == issue_number
            ),
            None,
        )
        if item is None:
            raise ValueError(
                f"Cannot retain tech_lead item for issue #{issue_number} after a "
                "retryable launch failure: no such item is queued"
            )
        item.retryable_launch_failures += 1
        if item.retryable_launch_failures >= TECH_LEAD_LAUNCH_RETRY_LIMIT:
            return TechLeadRetentionOutcome.EXHAUSTED
        logger.warning(
            "[TECH_LEAD] Retaining %s for issue #%d after retryable launch failure "
            "%d/%d",
            item.flavor.value,
            issue_number,
            item.retryable_launch_failures,
            TECH_LEAD_LAUNCH_RETRY_LIMIT,
        )
        return TechLeadRetentionOutcome.RETAINED

    def restore_deferred(self, claim: PendingWorkClaim) -> bool:
        """Return a launched-but-provider-deferred request to its own queue.

        The admission half of this owner (#6999 A1): every queue already
        declares here how an item is removed, so it declares here how one comes
        back. Admission is a decision TABLE rather than a branch chain, and each
        entry delegates to the collection's own owner method, so the duplicate
        rule for a queue lives in exactly one place whether the item is arriving
        from discovery or coming back from a dead session.

        The re-admitted object is the ORIGINAL request, preserving the context
        that exists nowhere else - a failure investigation's typed
        ``DiscoveredFailure``, a validation retry's prompt/error/count, a
        rework's cycle number.

        Returns True when the item was admitted, False when the queue already
        held an equivalent request. Restoring costs no retry budget: nothing
        about the request failed.
        """
        admit = _QUEUE_ADMISSION.get(claim.kind)
        if admit is None:
            # Named explicitly, like the launch-disposition switch: a queue kind
            # added without an entry here must fail loudly rather than silently
            # drop the only record of its work.
            raise ValueError(f"unhandled pending work kind: {claim.kind}")
        return admit(self, claim.request)

    def queue_existing_tech_lead(
        self, item: PendingTechLeadReview
    ) -> TechLeadQueueOutcome:
        """Re-admit an already-built tech_lead item under the same dedup rule.

        Distinct from the ``queue_*`` producers above, which BUILD the item from
        its parts. Restoring must not rebuild: the original carries its typed
        failure context and its spent retry count (#6999 A1).
        """
        return self._queue_tech_lead(item)

    def _queue_tech_lead(self, item: PendingTechLeadReview) -> TechLeadQueueOutcome:
        """Apply the one dedup rule (issue number vs pending queue) and enqueue."""
        queue = self.state.pending_tech_lead_reviews
        if any(t.issue_number == item.issue_number for t in queue):
            logger.info(
                "[TECH_LEAD] Issue #%d already queued for tech_lead; skipping %s request",
                item.issue_number,
                item.flavor.value,
            )
            return TechLeadQueueOutcome.DUPLICATE
        queue.append(item)
        logger.info(
            "[TECH_LEAD] Queued %s for issue #%d: %s",
            item.flavor.value,
            item.issue_number,
            item.title,
        )
        return TechLeadQueueOutcome.QUEUED



def _admit_review(queues: PendingSessionQueues, request: PendingWorkRequest) -> bool:
    assert isinstance(request, PendingReview)
    return queues.state.queue_pending_review(request)


def _admit_retrospective_review(
    queues: PendingSessionQueues, request: PendingWorkRequest
) -> bool:
    assert isinstance(request, PendingRetrospectiveReview)
    return queues.state.queue_pending_retrospective_review(request)


def _admit_rework(queues: PendingSessionQueues, request: PendingWorkRequest) -> bool:
    assert isinstance(request, PendingRework)
    return queues.state.queue_pending_rework(request)


def _admit_validation_retry(
    queues: PendingSessionQueues, request: PendingWorkRequest
) -> bool:
    assert isinstance(request, PendingValidationRetry)
    return queues.state.queue_pending_validation_retry(request)


def _admit_tech_lead(queues: PendingSessionQueues, request: PendingWorkRequest) -> bool:
    assert isinstance(request, PendingTechLeadReview)
    # Through the tech-lead intake owner, not the state list: this queue's
    # duplicate rule carries logging and a typed outcome that the others do not.
    return queues.queue_existing_tech_lead(request) is TechLeadQueueOutcome.QUEUED


# One entry per pending queue. A decision table rather than a branch chain so
# "which queue admits this" has a single, enumerable answer (#6999 A1).
_QUEUE_ADMISSION: dict[
    PendingWorkKind, Callable[[PendingSessionQueues, PendingWorkRequest], bool]
] = {
    PendingWorkKind.REVIEW: _admit_review,
    PendingWorkKind.RETROSPECTIVE_REVIEW: _admit_retrospective_review,
    PendingWorkKind.REWORK: _admit_rework,
    PendingWorkKind.VALIDATION_RETRY: _admit_validation_retry,
    PendingWorkKind.TECH_LEAD: _admit_tech_lead,
}


__all__ = [
    "TECH_LEAD_LAUNCH_RETRY_LIMIT",
    "PendingSessionQueues",
    "TechLeadQueueOutcome",
    "TechLeadRetentionOutcome",
]
