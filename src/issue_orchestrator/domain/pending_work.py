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


@dataclass(frozen=True, slots=True)
class InFlightWork:
    """A claim held by a running session, keyed by that session's terminal."""

    terminal_id: str
    claim: PendingWorkClaim


__all__ = [
    "InFlightWork",
    "PendingWorkClaim",
    "PendingWorkKind",
    "PendingWorkRequest",
]
