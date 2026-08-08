"""What a FINISHED republish job means (#6957 round-2 review F4 fallout).

``PublishRecoveryService.drain_completed_retries`` had five inline
``if ... log ... continue`` arms deciding whether a drained job may finalize the
issue's publish-failed state. Every one of them is a pure question about the
job and its result — no service state, no orchestrator state — and the loop
around them owns something else entirely: token correlation, tombstones, and
the pending-slot bookkeeping that must happen under the lock.

Splitting them makes the loop readable and the decision testable on its own,
and it keeps the rule that all five arms share in ONE place: anything short of a
successful terminal publish LEAVES THE ISSUE RETRYABLE. Never a permanent
lockout — the operator can always retry, and the next tick can always try again.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class DrainedRetryDisposition(str, Enum):
    """What the drain loop should do with one finished republish job."""

    #: The publish completed successfully: clear the publish-failed state.
    FINALIZE = "finalize"
    #: Anything else. The publish-failed label and locators stay put, so the
    #: issue remains retryable.
    LEAVE_RETRYABLE = "leave_retryable"


@dataclass(frozen=True)
class DrainedRetryOutcome:
    """A drained job's disposition plus the operator-facing reason for it."""

    disposition: DrainedRetryDisposition
    reason: str = ""
    #: True when the reason is a fault worth an ERROR log rather than INFO.
    faulted: bool = False

    @property
    def may_finalize(self) -> bool:
        return self.disposition is DrainedRetryDisposition.FINALIZE


_FINALIZE = DrainedRetryOutcome(disposition=DrainedRetryDisposition.FINALIZE)


def classify_drained_retry(*, job_error: Any, result: Any) -> DrainedRetryOutcome:
    """Decide what a finished republish job earns, without touching state.

    ``result`` is the republish outcome (None when the job produced none).
    A NON-TERMINAL result is the subtle one: the republish started or continued
    a background review exchange, so publish has not completed at all. The live
    path keeps such a completion RUNNING and resumes on a later tick, but
    retry-publish has no resume loop — so the issue stays retryable and the
    operator can retry once the exchange settles.
    """
    if job_error is not None:
        return DrainedRetryOutcome(
            disposition=DrainedRetryDisposition.LEAVE_RETRYABLE,
            reason=f"republish job raised: {job_error}",
            faulted=True,
        )
    if result is None:
        return DrainedRetryOutcome(
            disposition=DrainedRetryDisposition.LEAVE_RETRYABLE,
            reason="republish job finished without a result",
            faulted=True,
        )
    if result.is_non_terminal:
        return DrainedRetryOutcome(
            disposition=DrainedRetryDisposition.LEAVE_RETRYABLE,
            reason=(
                "republish is non-terminal (review_exchange_deferred="
                f"{result.review_exchange_deferred} validation_failed_rerouted="
                f"{result.validation_failed_rerouted}); leaving issue retryable"
                " without finalizing"
            ),
        )
    if not result.success:
        return DrainedRetryOutcome(
            disposition=DrainedRetryDisposition.LEAVE_RETRYABLE,
            reason=f"republish failed: {result.message}",
        )
    return _FINALIZE
