"""Typed failure vocabulary for one persistent-PTY round.

The execution-side counterpart to ``domain/review_exchange_failures``: those are
the reasons, these are the exceptions that carry one. Every persistent round
failure arrives at the exchange runner as one of the two types below, tagged
with a :class:`RoundFailureReason` (which drives respawn policy) and — when the
poll loop had a detector running — the :class:`RoundIdleTrace` the failure was
declared from, so kill-evidence capture never has to re-derive it from logs.

Kept separate from ``persistent_round_runner`` so the round loop stays the
round loop; nothing here touches a PTY.
"""

from __future__ import annotations

from ..domain.exchange_kill_evidence import RoundIdleTrace
from ..domain.review_exchange_failures import (
    RoundFailureReason,
    round_failure_reason_value,
)


def _bind_round_failure(
    exc: "PersistentRoundError | PersistentRoundTimeoutError",
    failure_reason: RoundFailureReason,
    idle_trace: RoundIdleTrace | None,
) -> None:
    """Attach the machine reason and idle-detector trace to a round failure."""
    if not isinstance(failure_reason, RoundFailureReason):
        raise TypeError("failure_reason must be a RoundFailureReason")
    exc.failure_reason = round_failure_reason_value(failure_reason)
    exc.idle_trace = idle_trace


class PersistentRoundError(RuntimeError):
    """Raised when a persistent round fails before a valid response exists."""

    failure_reason: str
    idle_trace: RoundIdleTrace | None

    def __init__(
        self,
        message: str,
        *,
        failure_reason: RoundFailureReason = RoundFailureReason.ROUND_ERROR,
        idle_trace: RoundIdleTrace | None = None,
    ) -> None:
        super().__init__(message)
        _bind_round_failure(self, failure_reason, idle_trace)


class PersistentRoundTimeoutError(TimeoutError):
    """Raised when a round's response file does not appear within the timeout."""

    failure_reason: str
    idle_trace: RoundIdleTrace | None

    def __init__(
        self,
        message: str,
        *,
        failure_reason: RoundFailureReason = RoundFailureReason.TIMEOUT,
        idle_trace: RoundIdleTrace | None = None,
    ) -> None:
        super().__init__(message)
        _bind_round_failure(self, failure_reason, idle_trace)


# Decision table for an exception that reached us untagged — a legacy or
# hand-constructed instance. Most specific type first, since the timeout type
# is not a subclass of the generic one but future additions might be.
_UNTAGGED_FALLBACKS: tuple[tuple[type[BaseException], RoundFailureReason], ...] = (
    (PersistentRoundTimeoutError, RoundFailureReason.TIMEOUT),
    (PersistentRoundError, RoundFailureReason.ROUND_ERROR),
)


def persistent_round_failure_reason(exc: BaseException) -> str:
    """Return the machine reason for a round failure exception."""
    tagged = getattr(exc, "failure_reason", None)
    if isinstance(tagged, str) and tagged:
        return tagged
    for failure_type, fallback in _UNTAGGED_FALLBACKS:
        if isinstance(exc, failure_type):
            return fallback.value
    return RoundFailureReason.UNKNOWN.value


def persistent_round_idle_trace(exc: BaseException) -> RoundIdleTrace | None:
    """Return the idle-detector trace a round failure was declared with."""
    trace = getattr(exc, "idle_trace", None)
    return trace if isinstance(trace, RoundIdleTrace) else None
