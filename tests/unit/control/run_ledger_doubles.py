"""Two-engine test doubles for the shared tech-lead run ledger (#6994).

Deliberately NOT a hand-written stub of the conflict matrix. The whole point of
the ledger is that ONE pure function decides what conflicts with what, so a
double that re-implemented the matrix would prove the tests pass rather than
prove the rule holds. :class:`SharedRunLedger` therefore evaluates the real
:func:`issue_orchestrator.domain.run_ledger.resolve` over one shared cell, and
each engine gets a store view with its own claimant identity — which is exactly
the shape two Repository Engines coordinating through one GitHub ref have.

The clock is injected and never real, so every cross-engine ordering, lease
expiry, and adoption case is deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from issue_orchestrator.control.tech_lead_run_ownership import TechLeadRunOwnership
from issue_orchestrator.domain.run_ledger import (
    RunLedger,
    RunLedgerOutcome,
    RunLedgerRequest,
    RunLedgerRequestKind,
    resolve,
)

LEASE_SECONDS = 900
RENEW_BEFORE_EXPIRY_SECONDS = 300


class FrozenClock:
    """A hand-advanced clock. No sleeps, no real time, anywhere."""

    def __init__(self, start: Optional[datetime] = None) -> None:
        self.now = start or datetime(2026, 8, 7, 12, 0, 0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class SharedRunLedger:
    """One repository-wide ledger cell, shared by every engine view."""

    def __init__(self, clock: Optional[FrozenClock] = None) -> None:
        self.clock = clock or FrozenClock()
        self.ledger = RunLedger()
        self.submissions: list[RunLedgerRequest] = []
        self.unavailable = False

    def engine(self, claimant: str) -> "_EngineLedgerStore":
        """A store view for one orchestrator instance."""
        return _EngineLedgerStore(self, claimant)

    def ownership(
        self, claimant: str, *, lease_seconds: int = LEASE_SECONDS
    ) -> TechLeadRunOwnership:
        """The run-ownership owner one engine would be built with."""
        return TechLeadRunOwnership(
            self.engine(claimant),
            lease_seconds=lease_seconds,
            renew_before_expiry_seconds=RENEW_BEFORE_EXPIRY_SECONDS,
            now=self.clock,
        )

    def entry(self, run_key: str):
        return self.ledger.find(run_key, self.clock())

    def live_keys(self) -> tuple[str, ...]:
        return tuple(
            sorted(entry.run_key for entry in self.ledger.live(self.clock()))
        )


class _EngineLedgerStore:
    """One engine's view of the shared cell — its claimant is its identity."""

    def __init__(self, shared: SharedRunLedger, claimant: str) -> None:
        self._shared = shared
        self._claimant = claimant

    @property
    def claimant(self) -> str:
        return self._claimant

    def submit(self, request: RunLedgerRequest) -> RunLedgerOutcome:
        self._shared.submissions.append(request)
        if self._shared.unavailable:
            return RunLedgerOutcome.unavailable(
                request.run_key, "the coordination store is unreachable"
            )
        request = self._with_lease(request)
        resolution = resolve(
            self._shared.ledger,
            request,
            claimant=self._claimant,
            now=self._shared.clock(),
            lease_seconds=LEASE_SECONDS,
        )
        if resolution.ledger is not None:
            self._shared.ledger = resolution.ledger
        return resolution.outcome

    def read(self) -> Optional[RunLedger]:
        if self._shared.unavailable:
            return None
        return self._shared.ledger

    def _with_lease(self, request: RunLedgerRequest) -> RunLedgerRequest:
        """Mint a claimant-scoped lease id, as the real adapter does."""
        if request.kind is not RunLedgerRequestKind.RESERVE or request.lease_id:
            return request
        stamp = int(self._shared.clock().timestamp() * 1000)
        return RunLedgerRequest(
            kind=request.kind,
            run_key=request.run_key,
            scope_kind=request.scope_kind,
            lease_id=f"{self._claimant}-{request.run_key}-{stamp}",
        )


def single_engine_ownership(
    clock: Optional[FrozenClock] = None,
) -> tuple[SharedRunLedger, TechLeadRunOwnership]:
    """The common one-engine setup: shared cell plus this engine's owner."""
    shared = SharedRunLedger(clock)
    return shared, shared.ownership("engine-a")


__all__ = [
    "FrozenClock",
    "LEASE_SECONDS",
    "RENEW_BEFORE_EXPIRY_SECONDS",
    "SharedRunLedger",
    "single_engine_ownership",
]
