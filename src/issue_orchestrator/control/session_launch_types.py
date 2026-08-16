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
    #: The launch attempt itself failed and may work next time: required input
    #: could not be prepared (a transient DB/log/filesystem read), or the
    #: terminal never came up. The item is retained, but on a bounded retry
    #: budget owned by the queue — unlike a provider refusal, this attempt DID
    #: fail, and an input or a terminal that never recovers must not relaunch
    #: forever. Named for the retry rather than for one of its causes (#6999
    #: F5): a failed ``create_session`` is the same decision for the queue as a
    #: failed input read, and calling it an input failure would have made
    #: routing it here a lie.
    RETRYABLE_FAILURE = "retryable_failure"
    #: The durable pending-work claim could not be recorded, so the launch
    #: never happened AND nothing about this request exists in the ledger
    #: (#6999 F1 round 2). Deliberately distinct from ``RETRYABLE_FAILURE``,
    #: which it used to borrow: that disposition spends a unit of the queue's
    #: bounded budget, and the settlement makes that spend durable by rewriting
    #: the deferred row - a row that, in this case, was never created. The
    #: rewrite silently matched zero rows, so the budget was spent in memory
    #: against nothing, and a process death then lost the request outright.
    #: Nothing failed about the WORK here; the ledger did. The item is retained
    #: with its budget untouched, exactly as a provider refusal leaves it.
    CLAIM_UNRECORDED = "claim_unrecorded"
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

    @classmethod
    def terminal_spawn_failed(cls) -> "LaunchResult":
        """The terminal never came up, on any launch path (#6999 F5).

        A factory rather than five hand-built results, because every launch
        path has to agree on what a failed ``create_session`` means and two of
        them did not: review and rework returned SUCCESS, handing back a
        phantom session for a terminal that does not exist. One constructor is
        what makes "did the terminal start?" impossible to answer differently
        per queue.

        The disposition is deliberately RETRYABLE rather than permanent:
        nothing about the request failed, so the queue keeps it on its bounded
        budget instead of destroying a review, rework or investigation because
        the terminal manager hiccuped once.
        """
        return cls(
            None,
            False,
            "Failed to create terminal session",
            disposition=LaunchDisposition.RETRYABLE_FAILURE,
        )

    @classmethod
    def required_input_unavailable(cls, reason: str) -> "LaunchResult":
        """Retain queued work when required launch input cannot be prepared."""
        return cls(
            None,
            False,
            f"Required launch input unavailable: {reason}",
            disposition=LaunchDisposition.RETRYABLE_FAILURE,
        )

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
