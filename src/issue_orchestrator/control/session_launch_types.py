"""Shared result types for session launch flows."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..domain.models import Session


class LaunchDisposition(Enum):
    """What a launch attempt means for the pending item that requested it.

    A failed launch is not one thing. "The terminal is already up", "I could
    not read the file I needed", "the provider refused" and "give up" call for
    four different queue reactions, and encoding them as ad-hoc booleans meant
    an unrecognised failure silently fell through to the most destructive one —
    dropping the work (#6999 F10/A1). Every launch path returns exactly one of
    these, and one owner maps it to a queue action.
    """

    #: A session started. The pending item is done.
    LAUNCHED = "launched"
    #: A terminal for this work is already running. The queue keeps the item
    #: and tries to restore that terminal; a successful restore consumes it.
    EXISTING_TERMINAL = "existing_terminal"
    #: The provider refused before anything was attempted — an expired login,
    #: or a CLI that is not installed. Nothing about the work failed and
    #: nothing was consumed, so the item stays queued exactly as it was, for a
    #: tick when the provider is ready. Deliberately distinct from a retry
    #: budget: there is no failure here to count against the work.
    PROVIDER_DEFERRED = "provider_deferred"
    #: Required input could not be prepared (a transient DB/log/filesystem
    #: read). The item is retained, but on a bounded retry budget owned by the
    #: queue — this one IS a failure of the request itself.
    INPUT_RETRY = "input_retry"
    #: The launcher gave up. The queue drops the item.
    PERMANENT_FAILURE = "permanent_failure"


@dataclass
class LaunchResult:
    """Result of a session launch attempt."""

    session: Session | None
    success: bool
    reason: str = ""
    #: How the owning queue should settle its pending item. Defaults to
    #: ``PERMANENT_FAILURE`` so a launch path that fails without saying why is
    #: treated as the launcher having given up — the historical behaviour — and
    #: is normalised to ``LAUNCHED`` whenever the launch actually succeeded.
    disposition: LaunchDisposition = LaunchDisposition.PERMANENT_FAILURE

    def __post_init__(self) -> None:
        if self.success:
            self.disposition = LaunchDisposition.LAUNCHED

    @property
    def defers_to_provider(self) -> bool:
        """Whether the provider refused and the work must stay untouched."""
        return self.disposition is LaunchDisposition.PROVIDER_DEFERRED


@dataclass
class ClaimAcquisitionResult:
    """Result of attempting to acquire a distributed claim for an issue.

    Used to track claim state through the launch process so cleanup
    can release claims on failure.
    """

    success: bool
    lease_id: str | None = None
    lease_acquired_at: datetime | None = None
    lease_expires_at: datetime | None = None
    error: str | None = None

    def as_launch_failure(self) -> LaunchResult:
        """Convert a failed claim to a launch result."""
        return LaunchResult(None, False, self.error or "Claim acquisition failed")
