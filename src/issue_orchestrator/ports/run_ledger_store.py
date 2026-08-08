"""Port for the repository-wide tech-lead run ledger (#6994).

``ClaimManager`` coordinates who may write to an ISSUE. This port coordinates
which tech-lead RUNS may exist and which of them may EXECUTE — a question about
the relationship between runs, not about one key, which is why it is a ledger
port and not a per-key claim port (round 2 A1).

The whole conflict matrix lives in :mod:`...domain.run_ledger` as a pure
function; an implementation of this port only has to make one
read-decide-write cycle atomic. That split is deliberate: the matrix is the part
that must never differ between a single-instance deployment and a multi-engine
one, so it is not an implementation detail of either store.

Exception contract: implementations MUST NOT raise for an unreachable backing
store. They return :meth:`RunLedgerOutcome.unavailable` (or ``None`` from
:meth:`read`) so the caller can distinguish "a peer owns this", "we held it and
lost it", and "we could not tell" — three facts that demand three different
behaviours, and whose conflation is how a transient GitHub outage ends up
terminating a healthy session.
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager as ContextManager
from datetime import datetime
from typing import Callable, Optional, Protocol

from ..domain.run_ledger import (
    RunLedger,
    RunLedgerOutcome,
    RunLedgerRequest,
    resolve,
)


class TechLeadRunLedgerStore(Protocol):
    """Atomic read-decide-write over the shared tech-lead run ledger."""

    def submit(self, request: RunLedgerRequest) -> RunLedgerOutcome:
        """Apply ``request`` to the ledger atomically and report the verdict.

        Exactly one concurrent caller may win any contested decision. Losers get
        a typed refusal naming the holder, so they can report a truthful
        conflict rather than a second "queued".
        """
        ...

    def read(self) -> Optional[RunLedger]:
        """The live ledger, or None when it could not be read.

        Inspection only — never the basis for a decision, because anything
        decided from a read is a check-then-act gap by construction.
        """
        ...


class SingleInstanceRunLedgerStore:
    """The ledger for a deployment with no peer engines.

    The "No Nulls" counterpart to :class:`NullClaimManager`: admission code
    never branches on whether shared coordination exists. It is NOT a
    permissive stub — it evaluates the identical :func:`resolve` matrix in
    memory, so exclusivity between a global run and targeted work holds for a
    single engine exactly as it does across two. Only the contention it can
    observe differs, because with one engine there is no peer to contend with.

    One process is not one thread. The tick runs in a worker thread while the
    dashboard command surface answers on the event loop, so read-decide-write
    is serialized by a lock HERE (#6994 round 2 F8): the port promises
    atomicity, and an implementation that leaves it to its callers has not
    implemented the port. The GitHub adapter gets the same guarantee from ref
    compare-and-swap; this one has to provide it itself.
    """

    def __init__(
        self,
        *,
        claimant: str = "single-instance",
        lease_seconds: int = 900,
        now: Optional[Callable[[], datetime]] = None,
        lock: Optional[ContextManager[object]] = None,
    ) -> None:
        self._claimant = claimant
        self._lease_seconds = lease_seconds
        self._now = now or datetime.now
        self._ledger = RunLedger()
        # Injectable for the same reason ``now`` is: serialization is a
        # collaborator, and a test that needs to OBSERVE the boundary
        # substitutes an instrumented lock rather than reaching into internals.
        self._lock: ContextManager[object] = lock or threading.Lock()

    def submit(self, request: RunLedgerRequest) -> RunLedgerOutcome:
        with self._lock:
            resolution = resolve(
                self._ledger,
                request,
                claimant=self._claimant,
                now=self._now(),
                lease_seconds=self._lease_seconds,
            )
            if resolution.ledger is not None:
                self._ledger = resolution.ledger
            return resolution.outcome

    def read(self) -> Optional[RunLedger]:
        with self._lock:
            return self._ledger


__all__ = ["SingleInstanceRunLedgerStore", "TechLeadRunLedgerStore"]
