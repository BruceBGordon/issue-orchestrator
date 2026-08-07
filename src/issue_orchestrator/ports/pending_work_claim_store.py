"""Authoritative storage for the queued request a running session is carrying.

The narrowest contract :class:`~issue_orchestrator.control.in_flight_work.InFlightWorkLedger`
needs (#6999 F4). Why durable at all: the claim is removed from its pending
queue at launch, so between then and terminal settlement it is the ONLY record
of that work. An in-memory ledger loses it on restart, and the affected work
types cannot be reconstructed from a restored terminal — a tech-lead failure
investigation has no label anchor at all, a rework has had its ``needs-rework``
trigger stripped, and a validation retry's prompt, error and attempt count exist
nowhere else.

Why implementations must not store it in the run directory (#6999 F7): that
directory lives inside the session worktree, which is handed to the launched
agent and is writable by it. A claim stored there would let an agent rewrite
what work the orchestrator believes it is doing — which queue, on which PR, with
which evidence hints — and restoration would accept it as truth. That inverts
"Agent Intent, Orchestrator Authority". The authoritative record belongs in
orchestrator-owned storage outside every worktree.

The whole :class:`SessionRunAssets` is passed rather than a bare identity so an
implementation can key on the orchestrator-allocated run root AND validate the
identity recorded against it. Run ids are timestamps and are NOT unique on their
own — two sessions launched in the same second share one — so identity alone is
neither a safe key nor a sufficient check.

Writes are create-once: re-writing the SAME claim for a run is idempotent, and a
DIFFERENT claim for a run raises :class:`ConflictingPendingWorkClaimError`
rather than overwriting. One run does one piece of work; a second, different
claim is drift, and silently taking the newer one is how the only record of the
first gets lost.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.pending_work import PendingWorkClaim
from ..domain.session_run import SessionRunAssets


class ConflictingPendingWorkClaimError(RuntimeError):
    """A different claim already exists for this run identity."""


class PendingWorkClaimStore(Protocol):
    """Read/write the claim held by one session run."""

    def write_pending_work_claim(
        self, run: SessionRunAssets, claim: PendingWorkClaim
    ) -> None:
        """Record that ``run`` holds ``claim``.

        Idempotent for an identical claim; raises
        :class:`ConflictingPendingWorkClaimError` for a different one.
        """
        ...

    def read_pending_work_claim(
        self, run: SessionRunAssets
    ) -> PendingWorkClaim | None:
        """The claim ``run`` holds, or None when it holds none.

        Raises rather than returning None when a record exists but cannot be
        trusted or rebuilt: "no claim" and "a claim I cannot read" are different
        facts, and conflating them drops work while looking like a clean start.
        """
        ...

    def clear_pending_work_claim(self, run: SessionRunAssets) -> None:
        """Drop this run's claim. A no-op when there is none."""
        ...


class UnwiredPendingWorkClaimStore:
    """The explicit "no claim store was injected here" value.

    Deliberately NOT a silent no-op. Every method raises, because the only way
    to reach one is to hold a claim while nothing durable is recording it - and
    quietly dropping it is the failure this whole boundary exists to prevent.
    Paths that legitimately hold no claim never touch the store at all, so this
    can only fire on a genuine wiring bug.
    """

    def write_pending_work_claim(
        self, run: SessionRunAssets, claim: PendingWorkClaim
    ) -> None:
        raise self._unwired(run)

    def read_pending_work_claim(
        self, run: SessionRunAssets
    ) -> PendingWorkClaim | None:
        raise self._unwired(run)

    def clear_pending_work_claim(self, run: SessionRunAssets) -> None:
        raise self._unwired(run)

    @staticmethod
    def _unwired(run: SessionRunAssets) -> RuntimeError:
        return RuntimeError(
            f"no pending-work claim store is wired, but run {run.run_id} "
            f"(session {run.session_name}) holds a claim; the composition root "
            "must inject PendingWorkClaimStore"
        )


UNWIRED_PENDING_WORK_CLAIMS: UnwiredPendingWorkClaimStore = (
    UnwiredPendingWorkClaimStore()
)


__all__ = [
    "ConflictingPendingWorkClaimError",
    "PendingWorkClaimStore",
    "UNWIRED_PENDING_WORK_CLAIMS",
    "UnwiredPendingWorkClaimStore",
]
