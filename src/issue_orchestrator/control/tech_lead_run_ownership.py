"""Which logical tech-lead runs THIS engine owns, across instances (#6994).

The admission matrix in :mod:`.tech_lead_run_admission` decides whether a run
*should* exist. This module decides whether this engine *may own* it, and — the
part a second Repository Engine makes non-trivial — whether it may EXECUTE it
right now.

Why it is a separate owner from the coordinator: the coordinator is constructed
per request from live state and holds nothing, whereas ownership is exactly the
thing that must OUTLIVE a request (a run is owned from admission, through the
ticks it waits behind a barrier, until its session ends). Folding the two
together would either give the coordinator a lifetime it must not have, or push
lease bookkeeping out to every call site.

Four operations, and the invariant each preserves:

* :meth:`TechLeadRunOwnership.claim` — atomic reservation of a run identity.
  Exactly one engine wins a given ``run_key``; the loser is told WHO won. This
  closes the two check-then-act gaps admission cannot close locally: "no anchor
  exists, so create one" and "no local queue entry exists, so enqueue one".
* :meth:`TechLeadRunOwnership.begin_run` — the atomic EXCLUSIVITY decision, made
  at the moment a session would start. Reserving a run never consults the
  barrier (queuing behind a global review is the designed behaviour); starting
  one always does, against every engine's live runs at once. Composing
  independent per-key claims could never decide this, which is why the shared
  store is a ledger (round 2 F1/A1).
* :meth:`TechLeadRunOwnership.reconcile` — one call per tick that renews every
  live run's lease, drops ownership of runs that ended, and re-establishes
  ownership of runs recovered from shared truth after a restart. It reports a
  TYPED outcome per run, because "a peer holds it", "we held it and lost it",
  and "we could not tell" demand three different behaviours from the caller
  (round 2 F4).
* :meth:`TechLeadRunOwnership.release` — explicit hand-back when a run is
  withdrawn or completes, so a peer need not wait out the lease.

Leases are tracked in memory as well as in the store so the renewal decision
costs no GitHub read: reservation already told us when the lease expires, and
re-reading it every tick to discover that would be exactly the polling the
project's GitHub API discipline forbids.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Callable, Collection, Optional

from ..domain.run_ledger import (
    RunLedgerOutcome,
    RunLedgerRequest,
    RunLedgerRequestKind,
    RunLedgerStatus,
)

if TYPE_CHECKING:
    from ..domain.tech_lead_run import TechLeadRunScope, TechLeadRunScopeKind
    from ..ports.run_ledger_store import TechLeadRunLedgerStore

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


class RunExecutionVerdict(str, Enum):
    """Whether this engine may START a session for a run, right now.

    ``BARRIER`` is not a failure: the run keeps its reservation and retries on a
    later tick. It is separate from ``LOST`` precisely so a caller cannot
    withdraw a perfectly healthy run that is merely waiting its turn.
    """

    STARTED = "started"
    BARRIER = "barrier"
    LOST = "lost"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RunExecutionAdmission:
    """The answer to "may this engine start ``run_key`` now?"."""

    verdict: RunExecutionVerdict
    run_key: str
    barrier_reason: str = ""
    holder: str = ""
    detail: str = ""

    @property
    def started(self) -> bool:
        return self.verdict is RunExecutionVerdict.STARTED


class RunReleaseStatus(str, Enum):
    """What happened when this engine tried to hand a run back.

    Separate from a bare ``None`` return because the durable store reports
    transport and codec failure as a typed ``UNAVAILABLE`` rather than by
    raising: a caller that inferred success from "no exception" would report a
    clean teardown while the shared ledger entry was still there, blocking every
    conflicting run until its lease expired (#6994 round 2 F12).
    """

    RELEASED = "released"
    # We held nothing, so there is nothing to give back. Success, not failure.
    NOT_HELD = "not_held"
    # The coordination store could not be reached. The local lease is KEPT so a
    # later reconciliation retries the release rather than losing it.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RunRelease:
    """The answer to "is this run definitively handed back?"."""

    status: RunReleaseStatus
    run_key: str
    detail: str = ""

    @property
    def released(self) -> bool:
        """True only when the durable hold is gone (or never existed)."""
        return self.status is not RunReleaseStatus.UNAVAILABLE


class RunReconcileStatus(str, Enum):
    """What one live run's lease looks like after a tick's reconciliation."""

    OWNED = "owned"
    # A live peer holds this run and we never did. RETAIN and retry: the holder
    # will release or its lease will lapse. Withdrawing here is how a restart
    # strands its own recovered anchor behind an unexpired pre-crash lease.
    CONTENDED = "contended"
    # We held it and no longer do. Queued work is withdrawn; an active session
    # cannot prove ownership and must be stopped.
    LOST = "lost"
    # We could not tell. Never a reason to stop a running session.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RunOwnershipOutcome:
    """One run's reconciliation result."""

    run_key: str
    status: RunReconcileStatus
    holder: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RunOwnershipReconciliation:
    """Every live run's reconciliation result, grouped by what it demands.

    Grouped HERE rather than by each caller so the "contention is not loss"
    rule has one implementation: a caller that iterated raw outcomes would be
    free to invent its own grouping, which is exactly the drift that made a
    transient store outage look like ownership loss.
    """

    outcomes: tuple[RunOwnershipOutcome, ...] = ()

    def _keys(self, status: RunReconcileStatus) -> tuple[str, ...]:
        return tuple(o.run_key for o in self.outcomes if o.status is status)

    @property
    def lost(self) -> tuple[str, ...]:
        """Runs this engine definitively no longer owns."""
        return self._keys(RunReconcileStatus.LOST)

    @property
    def contended(self) -> tuple[str, ...]:
        """Runs a peer holds. Retained and retried, never withdrawn."""
        return self._keys(RunReconcileStatus.CONTENDED)

    @property
    def unavailable(self) -> tuple[str, ...]:
        """Runs whose ownership could not be established or verified."""
        return self._keys(RunReconcileStatus.UNAVAILABLE)

    def outcome_for(self, run_key: str) -> Optional[RunOwnershipOutcome]:
        return next((o for o in self.outcomes if o.run_key == run_key), None)


@dataclass(frozen=True, slots=True)
class _Lease:
    lease_id: str
    expires_at: datetime


class _LeaseEffect(str, Enum):
    """What one ledger verdict implies for our in-memory lease bookkeeping."""

    REFRESH = "refresh"
    KEEP = "keep"
    DROP = "drop"


# ----------------------------------------------------------------------
# Translation tables
#
# Every ledger verdict maps to exactly one answer per question, declared once
# here instead of re-derived by a branch chain in each method. A table is also
# what makes "did we cover every verdict?" answerable by reading, and a new
# ledger verdict fails loudly at the lookup rather than falling into whichever
# branch happened to be last.
# ----------------------------------------------------------------------

_OWNERSHIP_VERDICT: dict[RunLedgerStatus, RunOwnershipVerdict] = {
    RunLedgerStatus.GRANTED: RunOwnershipVerdict.OWNED,
    RunLedgerStatus.ADOPTED: RunOwnershipVerdict.OWNED,
    RunLedgerStatus.HELD_BY_PEER: RunOwnershipVerdict.HELD_BY_PEER,
    RunLedgerStatus.LOST: RunOwnershipVerdict.HELD_BY_PEER,
    RunLedgerStatus.BARRIER: RunOwnershipVerdict.HELD_BY_PEER,
    RunLedgerStatus.UNAVAILABLE: RunOwnershipVerdict.UNAVAILABLE,
}

_EXECUTION_VERDICT: dict[RunLedgerStatus, RunExecutionVerdict] = {
    RunLedgerStatus.GRANTED: RunExecutionVerdict.STARTED,
    RunLedgerStatus.ADOPTED: RunExecutionVerdict.STARTED,
    RunLedgerStatus.BARRIER: RunExecutionVerdict.BARRIER,
    RunLedgerStatus.UNAVAILABLE: RunExecutionVerdict.UNAVAILABLE,
    RunLedgerStatus.HELD_BY_PEER: RunExecutionVerdict.LOST,
    RunLedgerStatus.LOST: RunExecutionVerdict.LOST,
}

# An engine that could not even RESERVE the run cannot execute it. Contention is
# reported as LOST here (not BARRIER) because a peer owning the identity is not
# something waiting will resolve for this launch attempt.
_EXECUTION_FROM_OWNERSHIP: dict[RunOwnershipVerdict, RunExecutionVerdict] = {
    RunOwnershipVerdict.HELD_BY_PEER: RunExecutionVerdict.LOST,
    RunOwnershipVerdict.UNAVAILABLE: RunExecutionVerdict.UNAVAILABLE,
}

_RECONCILE_FROM_OWNERSHIP: dict[RunOwnershipVerdict, RunReconcileStatus] = {
    RunOwnershipVerdict.OWNED: RunReconcileStatus.OWNED,
    # Never LOST: we never held this one, so there is nothing to have lost. It
    # is retained and retried until the holder's lease lapses (round 2 F4).
    RunOwnershipVerdict.HELD_BY_PEER: RunReconcileStatus.CONTENDED,
    RunOwnershipVerdict.UNAVAILABLE: RunReconcileStatus.UNAVAILABLE,
}

_RECONCILE_STATUS: dict[RunLedgerStatus, RunReconcileStatus] = {
    RunLedgerStatus.GRANTED: RunReconcileStatus.OWNED,
    RunLedgerStatus.ADOPTED: RunReconcileStatus.OWNED,
    RunLedgerStatus.BARRIER: RunReconcileStatus.OWNED,
    RunLedgerStatus.UNAVAILABLE: RunReconcileStatus.UNAVAILABLE,
    RunLedgerStatus.HELD_BY_PEER: RunReconcileStatus.LOST,
    RunLedgerStatus.LOST: RunReconcileStatus.LOST,
}

# What a RELEASE request's verdict means for us. Anything other than "we could
# not tell" leaves nothing of ours in the shared cell — a peer holding the key
# means our entry is already gone — so only UNAVAILABLE keeps the lease alive
# for a retry.
_RELEASE_STATUS: dict[RunLedgerStatus, RunReleaseStatus] = {
    RunLedgerStatus.GRANTED: RunReleaseStatus.RELEASED,
    RunLedgerStatus.ADOPTED: RunReleaseStatus.RELEASED,
    RunLedgerStatus.BARRIER: RunReleaseStatus.RELEASED,
    RunLedgerStatus.HELD_BY_PEER: RunReleaseStatus.RELEASED,
    RunLedgerStatus.LOST: RunReleaseStatus.RELEASED,
    RunLedgerStatus.UNAVAILABLE: RunReleaseStatus.UNAVAILABLE,
}

_LEASE_EFFECT: dict[RunLedgerStatus, _LeaseEffect] = {
    RunLedgerStatus.GRANTED: _LeaseEffect.REFRESH,
    RunLedgerStatus.ADOPTED: _LeaseEffect.REFRESH,
    # A barrier is a queue position and an outage is ignorance; neither is
    # evidence that our lease is gone, so both KEEP it.
    RunLedgerStatus.BARRIER: _LeaseEffect.KEEP,
    RunLedgerStatus.UNAVAILABLE: _LeaseEffect.KEEP,
    RunLedgerStatus.HELD_BY_PEER: _LeaseEffect.DROP,
    RunLedgerStatus.LOST: _LeaseEffect.DROP,
}

_RECONCILE_LOG: dict[RunReconcileStatus, str] = {
    RunReconcileStatus.CONTENDED: (
        "[TECH_LEAD_RUN] Run %s is held by %s; retaining it and retrying until"
        " the hold lapses"
    ),
    RunReconcileStatus.LOST: "[TECH_LEAD_RUN] Lost ownership of run %s to %s",
    RunReconcileStatus.UNAVAILABLE: (
        "[TECH_LEAD_RUN] Could not verify ownership of run %s: %s"
    ),
}

_UNAVAILABLE_OWNERSHIP_DETAIL = (
    "Tech-lead run ownership could not be established: {detail}."
)


def _ownership_detail(outcome: RunLedgerOutcome) -> str:
    """The operator-facing sentence for one reservation verdict."""
    template = _OWNERSHIP_DETAIL_TEMPLATE.get(outcome.status, "{detail}")
    return template.format(
        detail=outcome.detail or "the coordination store is unreachable"
    )


_OWNERSHIP_DETAIL_TEMPLATE: dict[RunLedgerStatus, str] = {
    RunLedgerStatus.UNAVAILABLE: _UNAVAILABLE_OWNERSHIP_DETAIL,
}


class TechLeadRunOwnership:
    """This engine's set of owned tech-lead runs, backed by the shared ledger."""

    def __init__(
        self,
        store: "TechLeadRunLedgerStore",
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
        # Reentrant: ``begin_run`` reserves through ``claim`` and ``reconcile``
        # both claims and releases, and the whole sequence must be one
        # transaction against the shared store AND this bookkeeping. Leaving
        # ``_held`` unsynchronized let the dashboard thread and the tick thread
        # disagree about which runs this engine owns (#6994 round 2 F8).
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def claim(self, scope: "TechLeadRunScope") -> RunOwnership:
        """Atomically reserve ``scope``'s run identity.

        Idempotent for the engine that already holds it: repeated requests for
        the same logical run coalesce onto one reservation instead of churning
        the coordination store on every click.
        """
        run_key = scope.run_key
        with self._lock:
            if run_key in self._held:
                return RunOwnership(RunOwnershipVerdict.OWNED, run_key)
            outcome = self._submit(RunLedgerRequestKind.RESERVE, scope)
            self._apply_lease_effect(run_key, outcome)
            return RunOwnership(
                _OWNERSHIP_VERDICT[outcome.status],
                run_key,
                holder=outcome.holder,
                detail=_ownership_detail(outcome),
            )

    def owns(self, run_key: str) -> bool:
        """True when this engine currently holds ``run_key``."""
        with self._lock:
            return run_key in self._held

    def release(self, run_key: str) -> RunRelease:
        """Hand ``run_key`` back so a peer need not wait out the lease.

        Returns a TYPED result rather than nothing, because the durable store
        can refuse without raising. On ``UNAVAILABLE`` the local lease is kept:
        the run is no longer live, so the next reconciliation sees a held key
        that is not live and retries the release — which is only possible if we
        still remember holding it.
        """
        with self._lock:
            lease = self._held.get(run_key)
            if lease is None:
                return RunRelease(RunReleaseStatus.NOT_HELD, run_key)
            outcome = self._store.submit(
                RunLedgerRequest(
                    kind=RunLedgerRequestKind.RELEASE,
                    run_key=run_key,
                    scope_kind=_kind_of(run_key),
                    lease_id=lease.lease_id,
                )
            )
            release = RunRelease(
                _RELEASE_STATUS[outcome.status], run_key, detail=outcome.detail
            )
            if release.released:
                del self._held[run_key]
            else:
                logger.warning(
                    "[TECH_LEAD_RUN] Could not hand run %s back (%s); keeping the"
                    " lease so the next reconciliation retries it",
                    run_key,
                    release.detail,
                )
            return release

    # ------------------------------------------------------------------
    # Execution — the atomic exclusivity decision
    # ------------------------------------------------------------------

    def begin_run(self, scope: "TechLeadRunScope") -> RunExecutionAdmission:
        """Take the right to EXECUTE ``scope`` now, against every engine's runs.

        Called immediately before a session starts, by the single launch
        authority. Fails CLOSED on an unreadable store: starting a global review
        alongside a peer's targeted work is precisely the outcome the ledger
        exists to prevent, and a launch deferred by one tick costs nothing.
        """
        run_key = scope.run_key
        with self._lock:
            lease = self._held.get(run_key)
            if lease is None:
                # Not reserved here — reserve first (exactly once), so the CLI's
                # direct launch path cannot start a run this engine never owned.
                ownership = self.claim(scope)
                lease = self._held.get(run_key)
                if lease is None:
                    return RunExecutionAdmission(
                        _EXECUTION_FROM_OWNERSHIP[ownership.verdict],
                        run_key,
                        holder=ownership.holder,
                        detail=ownership.detail,
                    )
            outcome = self._submit(
                RunLedgerRequestKind.PROMOTE, scope, lease_id=lease.lease_id
            )
            self._apply_lease_effect(run_key, outcome, fallback=lease.lease_id)
            return RunExecutionAdmission(
                _EXECUTION_VERDICT[outcome.status],
                run_key,
                barrier_reason=outcome.barrier_reason,
                holder=outcome.holder,
                detail=outcome.detail,
            )

    def end_run(self, run_key: str) -> RunRelease:
        """A session for ``run_key`` is over; the run is no longer exclusive.

        The typed result matters here more than anywhere else: the one-shot
        timeout path terminates and then runs no further tick, so a caller that
        assumed success would report a leak-free teardown that had not happened.
        """
        return self.release(run_key)

    # ------------------------------------------------------------------
    # Per-tick reconciliation
    # ------------------------------------------------------------------

    def reconcile(
        self, live_runs: "Collection[TechLeadRunScope]"
    ) -> RunOwnershipReconciliation:
        """Align held leases with the runs that actually exist right now.

        Called once per tick with the union of queued and active tech-lead runs
        — the caller owns WHAT is live, this owns what that implies for the
        leases. Every live run comes back with a typed status; the caller
        decides the consequence, because the consequence differs by whether the
        run is queued or executing.
        """
        live = {scope.run_key: scope for scope in live_runs}
        with self._lock:
            for run_key in [key for key in self._held if key not in live]:
                self.release(run_key)

            outcomes = [
                self._reconcile_one(live[run_key]) for run_key in sorted(live)
            ]
            return RunOwnershipReconciliation(tuple(outcomes))

    def _reconcile_one(self, scope: "TechLeadRunScope") -> RunOwnershipOutcome:
        run_key = scope.run_key
        lease = self._held.get(run_key)
        if lease is None:
            # A run recovered from shared truth after a restart, or one a peer
            # produced: ownership has to be (re-)established before we may
            # launch it. A reservation ADOPTS this engine's own live pre-crash
            # lease rather than reporting it as a peer's (round 2 F4).
            ownership = self.claim(scope)
            return self._report(
                run_key,
                _RECONCILE_FROM_OWNERSHIP[ownership.verdict],
                ownership.holder,
                ownership.detail,
            )
        if not self._needs_renewal(lease):
            return RunOwnershipOutcome(run_key, RunReconcileStatus.OWNED)
        outcome = self._submit(
            RunLedgerRequestKind.RENEW, scope, lease_id=lease.lease_id
        )
        self._apply_lease_effect(run_key, outcome, fallback=lease.lease_id)
        return self._report(
            run_key,
            _RECONCILE_STATUS[outcome.status],
            outcome.holder,
            outcome.detail,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _report(
        self, run_key: str, status: RunReconcileStatus, holder: str, detail: str
    ) -> RunOwnershipOutcome:
        """Log the non-owned reconciliation results, then hand back the typed one."""
        message = _RECONCILE_LOG.get(status)
        if message:
            logger.warning(message, run_key, holder or detail or "an unknown peer")
        return RunOwnershipOutcome(run_key, status, holder=holder, detail=detail)

    def _apply_lease_effect(
        self, run_key: str, outcome: RunLedgerOutcome, *, fallback: str = ""
    ) -> None:
        """Keep, refresh, or drop our in-memory lease, per the ledger's verdict."""
        effect = _LEASE_EFFECT[outcome.status]
        if effect is _LeaseEffect.REFRESH:
            self._remember(run_key, outcome.lease_id or fallback)
        elif effect is _LeaseEffect.DROP:
            self._held.pop(run_key, None)

    def _submit(
        self,
        kind: RunLedgerRequestKind,
        scope: "TechLeadRunScope",
        *,
        lease_id: str = "",
    ) -> RunLedgerOutcome:
        return self._store.submit(
            RunLedgerRequest(
                kind=kind,
                run_key=scope.run_key,
                scope_kind=scope.kind,
                lease_id=lease_id,
            )
        )

    def _remember(self, run_key: str, lease_id: str) -> None:
        self._held[run_key] = _Lease(
            lease_id=lease_id,
            expires_at=self._now() + timedelta(seconds=self._lease_seconds),
        )

    def _needs_renewal(self, lease: _Lease) -> bool:
        remaining = (lease.expires_at - self._now()).total_seconds()
        return remaining <= self._renew_before_expiry_seconds


def _kind_of(run_key: str) -> "TechLeadRunScopeKind":
    """The scope kind a held run key belongs to.

    Release is the one operation a caller can reach holding only a key (a
    withdrawal knows the run, not the scope value), so the kind is recovered
    from the key's own namespace by the DOMAIN owner of that mapping rather
    than by a second, looser copy of it here.
    """
    from ..domain.tech_lead_run import scope_kind_of_run_key

    return scope_kind_of_run_key(run_key)


def single_instance_run_ownership() -> TechLeadRunOwnership:
    """Run ownership for a deployment with no peer engines.

    Backed by :class:`SingleInstanceRunLedgerStore`, which evaluates the SAME
    conflict matrix in memory — so scope exclusivity is a real invariant for one
    engine too, not a rule that only switches on when claims are configured. It
    lives here, with the owner it constructs, so a caller that needs a default
    never has to know which store and lease numbers make one.
    """
    from ..domain.lease_config import LeaseConfig
    from ..ports.run_ledger_store import SingleInstanceRunLedgerStore

    lease = LeaseConfig()
    return TechLeadRunOwnership(
        SingleInstanceRunLedgerStore(lease_seconds=lease.lease_seconds),
        lease_seconds=lease.lease_seconds,
        renew_before_expiry_seconds=lease.renew_interval_seconds,
    )


__all__ = [
    "RunExecutionAdmission",
    "RunExecutionVerdict",
    "RunOwnership",
    "RunOwnershipOutcome",
    "RunOwnershipReconciliation",
    "RunOwnershipVerdict",
    "RunReconcileStatus",
    "RunRelease",
    "RunReleaseStatus",
    "TechLeadRunOwnership",
    "single_instance_run_ownership",
]
