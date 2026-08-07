"""Port for cross-orchestrator ownership of a LOGICAL RUN (#6994).

``ClaimManager`` coordinates who may write to an ISSUE. This port coordinates
who owns a *run* — a unit of work with its own identity (``run_key``) that may
not have an issue yet. The tech-lead run admission owner needs the second
question answered atomically, because the race it must win happens BEFORE any
subject exists:

* two engines both scan for an open health-review anchor, both find none, and
  both create one;
* two engines both see "#42 is not queued locally" and both enqueue it.

Both are check-then-act gaps that no amount of local locking can close. The
operation that closes them is a compare-and-swap acquisition of the run identity
itself, which is what :meth:`RunClaimStore.acquire` is.

Exception contract: implementations MUST NOT raise for an unreachable backing
store. They return :meth:`RunClaimAcquisition.unavailable` (or ``None`` from
:meth:`current`) so the caller can distinguish "a peer owns this" from "we could
not tell", and choose its own policy — admission fails CLOSED on an unreadable
store, because admitting on ignorance is what produces the duplicate run this
port exists to prevent.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.claim import RunClaim, RunClaimAcquisition


class RunClaimStore(Protocol):
    """Atomic, shared ownership of a logical run."""

    def acquire(self, run_key: str) -> RunClaimAcquisition:
        """Atomically take ownership of ``run_key``.

        Exactly one concurrent caller may win. A caller that loses receives the
        live holder, so it can report a truthful typed duplicate/conflict rather
        than a second "queued".

        An EXPIRED holder is not a holder: the run's previous owner died without
        releasing, and the run must become claimable again or it would be
        stranded forever. Taking over an expired lease is therefore a win.
        """
        ...

    def renew(self, run_key: str, lease_id: str) -> bool:
        """Extend our lease. False when we are no longer the holder."""
        ...

    def release(self, run_key: str, lease_id: str) -> None:
        """Give up ownership, if ``lease_id`` still holds it."""
        ...

    def current(self, run_key: str) -> RunClaim | None:
        """The live holder of ``run_key``, or None (no holder, or unreadable)."""
        ...


class NullRunClaimStore:
    """Single-orchestrator run ownership: this instance always wins.

    The "No Nulls" counterpart to :class:`NullClaimManager` — injected when
    claims are disabled, so admission code never branches on whether shared
    coordination exists. Correct precisely because with claims disabled there IS
    no peer that could contend.
    """

    def __init__(self) -> None:
        self._held: dict[str, str] = {}

    def acquire(self, run_key: str) -> RunClaimAcquisition:
        lease_id = f"null-run-claim-{run_key}"
        self._held[run_key] = lease_id
        return RunClaimAcquisition.acquired(lease_id)

    def renew(self, run_key: str, lease_id: str) -> bool:
        return self._held.get(run_key) == lease_id

    def release(self, run_key: str, lease_id: str) -> None:
        if self._held.get(run_key) == lease_id:
            del self._held[run_key]

    def current(self, run_key: str) -> RunClaim | None:
        _ = run_key
        return None


__all__ = ["NullRunClaimStore", "RunClaimStore"]
