"""Domain types for the claim/lease system.

This module defines the core data structures for distributed issue coordination
between multiple orchestrator instances.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class ClaimFetchError(Exception):
    """Raised when claims cannot be fetched due to API errors.

    This distinguishes "no claims exist" (empty list) from "couldn't read
    claims" (API failure). Callers use this to avoid falsely interpreting
    transient GitHub outages as claim loss.
    """


class ClaimState(Enum):
    """State of a claim on an issue."""

    UNCLAIMED = "unclaimed"  # No active claim
    CLAIMING = "claiming"  # Convergence in progress
    CLAIMED = "claimed"  # Stable winner determined
    CLAIM_LOST = "claim_lost"  # Lost claim during session
    CLAIM_EXPIRED = "claim_expired"  # Claim expired without renewal


@dataclass(frozen=True)
class Claim:
    """Represents a claim on an issue by an orchestrator instance.

    Claims are stored by ClaimManager implementations and used to coordinate
    which orchestrator is allowed to work on an issue.

    Attributes:
        lease_id: Unique identifier for this claim (UUID + timestamp)
        claimant: Identifier of the orchestrator instance (e.g., hostname or UUID)
        issue_number: The GitHub issue number being claimed
        started_at: When the claim was created
        expires_at: When the claim expires if not renewed
        priority: Epoch milliseconds for tie-breaking (higher wins)
    """

    lease_id: str
    claimant: str
    issue_number: int
    started_at: datetime
    expires_at: datetime
    priority: int

    def is_expired(self, now: datetime | None = None) -> bool:
        """Check if this claim has expired.

        Args:
            now: Current time. Defaults to datetime.now() if not provided.

        Returns:
            True if the claim has expired.
        """
        if now is None:
            now = datetime.now()
        return now >= self.expires_at

    def time_until_expiry_seconds(self, now: datetime | None = None) -> float:
        """Get seconds until this claim expires.

        Args:
            now: Current time. Defaults to datetime.now() if not provided.

        Returns:
            Seconds until expiry (negative if already expired).
        """
        if now is None:
            now = datetime.now()
        return (self.expires_at - now).total_seconds()


@dataclass(frozen=True)
class ClaimResult:
    """Result of attempting to claim an issue.

    Attributes:
        success: Whether the claim was successful
        lease_id: The lease ID if successful, None otherwise
        state: The resulting claim state
        competing_claims: List of other claims seen during attempt
        error: Error message if claim failed
    """

    success: bool
    lease_id: str | None
    state: ClaimState
    competing_claims: list[Claim] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def claimed(cls, lease_id: str) -> "ClaimResult":
        """Create a successful claim result."""
        return cls(
            success=True,
            lease_id=lease_id,
            state=ClaimState.CLAIMED,
        )

    @classmethod
    def contested(
        cls, lease_id: str, competing_claims: list[Claim]
    ) -> "ClaimResult":
        """Create a result indicating claim is being contested."""
        return cls(
            success=False,
            lease_id=lease_id,
            state=ClaimState.CLAIMING,
            competing_claims=competing_claims,
        )

    @classmethod
    def lost(cls, lease_id: str, winner: Claim) -> "ClaimResult":
        """Create a result indicating we lost the claim."""
        return cls(
            success=False,
            lease_id=lease_id,
            state=ClaimState.CLAIM_LOST,
            competing_claims=[winner],
        )

    @classmethod
    def failed(cls, error: str) -> "ClaimResult":
        """Create a failed claim result."""
        return cls(
            success=False,
            lease_id=None,
            state=ClaimState.UNCLAIMED,
            error=error,
        )
