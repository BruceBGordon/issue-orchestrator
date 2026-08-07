"""Durable storage for the queued request a running session is carrying.

The narrowest contract :class:`~issue_orchestrator.control.in_flight_work.InFlightWorkLedger`
needs (#6999 F4). ``SessionOutput`` satisfies it structurally, because the claim
belongs with the run assets of the session that took it: same directory, same
lifetime, discovered by the same restoration seam that rebuilds the session.

Why durable at all: the claim is removed from its pending queue at launch, so
between then and terminal settlement it is the ONLY record of that work. An
in-memory ledger loses it on restart, and the affected work types cannot be
reconstructed from a restored terminal — a tech-lead failure investigation has
no label anchor at all, a rework has had its ``needs-rework`` trigger stripped,
and a validation retry's prompt, error and attempt count exist nowhere else.

Values are typed both ways: the port speaks :class:`PendingWorkClaim`, and the
adapter owns the encoding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.pending_work import PendingWorkClaim


class PendingWorkClaimStore(Protocol):
    """Read/write the claim held by one session run."""

    def write_pending_work_claim(
        self, run_dir: Path, claim: PendingWorkClaim
    ) -> None:
        """Record that this run holds ``claim``. Overwrites any prior value."""
        ...

    def read_pending_work_claim(self, run_dir: Path) -> PendingWorkClaim | None:
        """The claim this run holds, or None when it holds none."""
        ...

    def clear_pending_work_claim(self, run_dir: Path) -> None:
        """Drop this run's claim. A no-op when there is none."""
        ...


__all__ = ["PendingWorkClaimStore"]
