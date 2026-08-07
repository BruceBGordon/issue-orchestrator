"""Which logical tech-lead runs THIS engine owns, across instances (#6994).

The admission matrix in :mod:`.tech_lead_run_admission` decides whether a run
*should* exist. This module decides whether this engine *may own* it — the
question that a second Repository Engine makes non-trivial.

Why it is a separate owner from the coordinator: the coordinator is constructed
per request from live state and holds nothing, whereas ownership is exactly the
thing that must OUTLIVE a request (a run is owned from admission, through the
ticks it waits behind a barrier, until its session ends). Folding the two
together would either give the coordinator a lifetime it must not have, or push
lease bookkeeping out to every call site.

The three operations, and the invariant each preserves:

* :meth:`TechLeadRunOwnership.claim` — atomic acquisition. Exactly one engine
  wins a given ``run_key``; the loser is told WHO won. This is what closes the
  two check-then-act gaps admission cannot close locally: "no anchor exists, so
  create one" and "no local queue entry exists, so enqueue one".
* :meth:`TechLeadRunOwnership.reconcile` — one call per tick that renews every
  live run's lease, drops ownership of runs that ended, and re-acquires runs
  recovered from shared truth after a restart. Runs it can no longer own come
  back as ``lost`` so the caller can withdraw them rather than launch work a
  peer already owns.
* :meth:`TechLeadRunOwnership.release` — explicit hand-back when a run is
  withdrawn or completes, so a peer need not wait out the lease.

Leases are tracked in memory as well as in the store so the renewal decision
costs no GitHub read: acquisition already told us when the lease expires, and
re-reading it every tick to discover that would be exactly the polling the
project's GitHub API discipline forbids.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Callable, Collection, Optional

if TYPE_CHECKING:
    from ..ports.run_claim_store import RunClaimStore

logger = logging.getLogger(__name__)


class RunOwnershipVerdict(str, Enum):
    """Whether this engine may act on a logical run."""

    OWNED = "owned"
    HELD_BY_PEER = "held_by_peer"
    # The coordination store could not be read or written. Deliberately NOT
    # folded into HELD_BY_PEER: "someone else is doing this" and "we cannot
    # tell" need different words to an operator and different policy here.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RunOwnership:
    """The answer to "may this engine own ``run_key``?"."""

    verdict: RunOwnershipVerdict
    run_key: str
    holder: str = ""
    detail: str = ""

    @property
    def owned(self) -> bool:
        return self.verdict is RunOwnershipVerdict.OWNED


@dataclass(frozen=True, slots=True)
class _Lease:
    lease_id: str
    expires_at: datetime


class TechLeadRunOwnership:
    """This engine's set of owned tech-lead runs, backed by shared claims."""

    def __init__(
        self,
        store: "RunClaimStore",
        *,
        lease_seconds: int,
        renew_before_expiry_seconds: int,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._store = store
        self._lease_seconds = lease_seconds
        self._renew_before_expiry_seconds = renew_before_expiry_seconds
        self._now = now or datetime.now
        self._held: dict[str, _Lease] = {}

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def claim(self, run_key: str) -> RunOwnership:
        """Atomically take ownership of ``run_key``.

        Idempotent for the engine that already holds it: repeated requests for
        the same logical run coalesce onto one claim instead of churning the
        coordination store on every click.
        """
        if run_key in self._held:
            return RunOwnership(RunOwnershipVerdict.OWNED, run_key)

        acquisition = self._store.acquire(run_key)
        if acquisition.won and acquisition.lease_id:
            self._held[run_key] = _Lease(
                lease_id=acquisition.lease_id,
                expires_at=self._now() + timedelta(seconds=self._lease_seconds),
            )
            return RunOwnership(RunOwnershipVerdict.OWNED, run_key)
        if acquisition.holder is not None:
            return RunOwnership(
                RunOwnershipVerdict.HELD_BY_PEER,
                run_key,
                holder=acquisition.holder.claimant,
                detail=(
                    f"Another orchestrator instance ({acquisition.holder.claimant})"
                    f" already owns this tech-lead run."
                ),
            )
        return RunOwnership(
            RunOwnershipVerdict.UNAVAILABLE,
            run_key,
            detail=(
                "Tech-lead run ownership could not be established: "
                f"{acquisition.error or 'the coordination store is unreachable'}."
            ),
        )

    def owns(self, run_key: str) -> bool:
        """True when this engine currently holds ``run_key``."""
        return run_key in self._held

    def release(self, run_key: str) -> None:
        """Hand ``run_key`` back so a peer need not wait out the lease."""
        lease = self._held.pop(run_key, None)
        if lease is not None:
            self._store.release(run_key, lease.lease_id)

    # ------------------------------------------------------------------
    # Per-tick reconciliation
    # ------------------------------------------------------------------

    def reconcile(self, live_run_keys: Collection[str]) -> tuple[str, ...]:
        """Align held claims with the runs that actually exist right now.

        Returns the run keys this engine may no longer act on, so the caller
        can withdraw them. Called once per tick with the union of queued and
        active tech-lead runs — the caller owns WHAT is live, this owns what
        that implies for the claims.
        """
        live = set(live_run_keys)
        lost: list[str] = []

        for run_key in [key for key in self._held if key not in live]:
            self.release(run_key)

        for run_key in sorted(live):
            lease = self._held.get(run_key)
            if lease is None:
                # A run recovered from shared truth after a restart, or one a
                # peer produced: ownership has to be (re-)established before we
                # may launch it.
                if not self.claim(run_key).owned:
                    lost.append(run_key)
                continue
            if not self._needs_renewal(lease):
                continue
            if self._store.renew(run_key, lease.lease_id):
                self._held[run_key] = _Lease(
                    lease_id=lease.lease_id,
                    expires_at=self._now() + timedelta(seconds=self._lease_seconds),
                )
                continue
            logger.warning(
                "[TECH_LEAD_RUN] Lost ownership of run %s; withdrawing it", run_key
            )
            self._held.pop(run_key, None)
            lost.append(run_key)

        return tuple(lost)

    def _needs_renewal(self, lease: _Lease) -> bool:
        remaining = (lease.expires_at - self._now()).total_seconds()
        return remaining <= self._renew_before_expiry_seconds


__all__ = [
    "RunOwnership",
    "RunOwnershipVerdict",
    "TechLeadRunOwnership",
]
