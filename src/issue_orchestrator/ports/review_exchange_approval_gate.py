"""Acceptance policy evaluated before a reviewer approval becomes terminal."""

from __future__ import annotations

from typing import Protocol


class ReviewExchangeApprovalGate(Protocol):
    """Return the rejection reason when current artifacts cannot be approved.

    The review-exchange runner calls this only after the reviewer returns
    ``ok``.  A non-empty result converts that approval into another bounded
    coder rework round; ``None`` accepts the approval.
    """

    def rejection_reason(self) -> str | None:
        ...
