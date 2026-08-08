"""The typed queued request a launched session is carrying (#6999 A1).

A session that launched from a pending queue *consumed* a request to do so. Until
that session reaches a true terminal work outcome the request is not spent, it is
merely in flight — and if the session dies for a reason that has nothing to do
with the work (an expired provider credential), the request has to go back.

These are the domain value objects for that span. The policy that decides when a
claim is consumed versus returned lives in
:mod:`issue_orchestrator.control.in_flight_work`; nothing here knows about
providers, terminals, or GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Union

from .models import (
    PendingRetrospectiveReview,
    PendingReview,
    PendingRework,
    PendingTechLeadReview,
    PendingValidationRetry,
)

# Every queue whose item is removed at launch. Issue sessions are absent on
# purpose: they are claimed with the in-progress label rather than dequeued, so
# labels-as-truth already restores them and there is nothing to hold.
PendingWorkRequest = Union[
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingValidationRetry,
    PendingTechLeadReview,
]


class PendingWorkKind(Enum):
    """Which pending queue a claim came from, and must be returned to."""

    REVIEW = "review"
    RETROSPECTIVE_REVIEW = "retrospective_review"
    REWORK = "rework"
    VALIDATION_RETRY = "validation_retry"
    TECH_LEAD = "tech_lead"


@dataclass(frozen=True, slots=True)
class PendingWorkClaim:
    """One launch's claim on one queued request.

    Carries the ORIGINAL request object, not a reconstruction of it. A failure
    investigation's typed :class:`~.models.DiscoveredFailure`, a validation
    retry's prompt/error/retry-count, and a rework's cycle number exist nowhere
    else once the item leaves its queue, so returning a rebuilt stand-in would
    silently downgrade the work.
    """

    kind: PendingWorkKind
    request: PendingWorkRequest

    def work_key(self) -> str:
        """Stable identity of the WORK, independent of which run is doing it.

        A run key answers "which session holds this"; this answers "which piece
        of work is this". The durable ledger needs both: a request deferred by
        one run and later relaunched by another must supersede its own earlier
        record rather than accumulate one row per attempt (#6999 F8).
        """
        return f"{self.kind.value}:{_request_id(self.request)}"


@dataclass(frozen=True, slots=True)
class InFlightWork:
    """A claim held by a running session, keyed by that session's terminal."""

    terminal_id: str
    claim: PendingWorkClaim


def _request_id(request: PendingWorkRequest) -> str:
    """The number a queue dedupes on, which is what identifies the work.

    Each pending queue admits at most one item per this value (see
    ``PendingSessionQueues.restore_deferred``), so it is exactly the right
    grain: two claims sharing it are the same work by every queue's own rule.
    """
    if isinstance(request, PendingReview):
        return str(request.pr_number)
    if isinstance(request, PendingRetrospectiveReview):
        return str(request.issue_number)
    if isinstance(request, PendingRework):
        return str(request.resolve_issue_number())
    if isinstance(request, PendingValidationRetry):
        return str(request.issue_number)
    # The union's last member: exhaustive by construction, so no unreachable
    # fall-through to guard. A member added without an entry above lands here
    # and is caught by the type checker rather than silently mis-keyed.
    return str(request.issue_number)


__all__ = [
    "InFlightWork",
    "PendingWorkClaim",
    "PendingWorkKind",
    "PendingWorkRequest",
]
