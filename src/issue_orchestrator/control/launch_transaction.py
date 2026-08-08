"""One launch, settled as one transaction (#6999 A2/F4).

A launch touches two records of the same request: the in-memory pending queue
it came off, and the durable claim ledger that has to survive a restart. Split
between a launcher that only knew how to defer and a settlement that only knew
how to mutate the queue, they could - and did - end a launch disagreeing:

* a permanently dropped item left a deferred row, which the startup sweep is
  built to re-admit, so "permanent" lasted until the next restart;
* an exhausted tech-lead retry budget was serialised before it was spent, so a
  restart refunded it and relaunched an investigation whose escalation to a
  human was already standing on the issue.

This module owns the whole span instead. :class:`PendingWorkLaunchClaim` takes
the durable claim before anything irreversible happens and
:func:`abandon_claim_unless_spawned` hands it back on every exit that started
no terminal; :class:`LaunchSettlement` then settles the queue AND the ledger
together, driven by one typed :class:`WorkDisposal`. Separated from
:mod:`.in_flight_work`, which owns the other span: what a LIVE terminal is
carrying, from its first byte until it dies.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable, Generator, Optional, Protocol

from ..domain.models import Session
from ..domain.pending_work import PendingWorkClaim
from ..domain.session_run import SessionRunAssets
from ..ports.pending_work_claim_store import PendingWorkClaimStore
from .active_sessions import append_unique_active_sessions
from .in_flight_work import InFlightWorkLedger
from .session_launch_types import LaunchDisposition, LaunchResult

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState

logger = logging.getLogger(__name__)


class WorkDisposal(Enum):
    """What the QUEUE side of a launch did with the request (#6999 F4/A2).

    The durable side has to match it, and it cannot work that out on its own:
    "the item was dropped" and "the item is still queued" look identical from
    the ledger. Returning it as a typed value is what lets one transaction
    settle both halves, instead of a blind compensation deferring every
    unspawned claim while the settlement independently retained or dropped it.
    """

    #: A queue still owns this request, so the deferred row is its durable
    #: backing and must survive - refreshed, because the queue owner may have
    #: mutated the request while settling (a spent retry budget).
    RETAINED = "retained"
    #: The request is gone by decision: dropped from its queue, or escalated to
    #: a human after exhausting its budget. The deferred row must go with it,
    #: or startup recovery re-admits work that was deliberately abandoned.
    DROPPED = "dropped"


class LaunchWorkClaim(Protocol):
    """The durable claim a launch takes BEFORE it spawns anything (#6999 A2)."""

    def hold_before_spawn(
        self, run: SessionRunAssets, *, issue_number: int
    ) -> LaunchResult | None:
        """Record the claim. A non-``None`` result aborts the launch."""
        ...

    def abandon_unspawned(self, run: SessionRunAssets) -> None:
        """Hand the work back because no terminal ever started."""
        ...

    def settle_unspawned(self, disposal: WorkDisposal) -> None:
        """Bring the durable row into line with what the queue decided."""
        ...


@dataclass(frozen=True, slots=True)
class PendingWorkLaunchClaim:
    """One queued request's durable ownership across a whole launch (#6999 A2).

    The claim used to be recorded only once a live ``Session`` came back, which
    put the durable record AFTER the terminal it describes. Two things followed
    from that ordering, and both lose work:

    * a crash between the spawn and the write - or between the spawn and the
      launch's own destructive label transitions - left a running agent carrying
      a request with no durable record anywhere. For a tech-lead failure
      investigation the in-memory queue is the only other copy, so a restart
      could not recover what that terminal owned;
    * the store write had nowhere left to fail. By the time it ran the terminal
      was irreversible, so a claim-store fault could only be reported, never
      undone.

    So the claim is taken as soon as the run identity exists and before anything
    irreversible happens. A launch that never reaches a live terminal hands the
    work back through the same owner, by DEFERRING the row rather than deleting
    it: "deferred" already means *untouched, waiting to be relaunched*, which is
    exactly true here, and it keeps a durable record for the startup sweep to
    re-admit instead of trusting the in-memory queue to survive.

    Deferring is only the first half of that hand-back. Whether the request
    still exists is the QUEUE owner's decision, and :meth:`settle_unspawned` is
    how it reaches the ledger (#6999 F4) - without it the two halves settle
    independently and a dropped item keeps a recoverable row.
    """

    claim: PendingWorkClaim
    claims: PendingWorkClaimStore

    def hold_before_spawn(
        self, run: SessionRunAssets, *, issue_number: int
    ) -> LaunchResult | None:
        """Record the claim durably, before this launch can spawn anything.

        ``issue_number`` comes from the launch path itself, which is the only
        place that also knows the issue the resulting ``Session`` will carry -
        the two must agree, because the ledger row is what a quarantine
        escalates against when the payload can no longer be read (#6999 F12).

        A store failure returns a RETRYABLE_FAILURE launch result rather than raising:
        nothing about the request failed, the queue item is untouched, and the
        alternative - spawning anyway - is the crash window this exists to close.
        """
        try:
            self.claims.hold_pending_work_claim(
                run, self.claim, issue_number=issue_number
            )
        except Exception as exc:  # store-defined write/conflict failure
            logger.error(
                "[WORK] Refusing to spawn a terminal for %s work on issue #%d: "
                "its pending-work claim could not be recorded, and a terminal "
                "holding a claim nothing durable knows about cannot be settled "
                "or recovered: %s",
                self.claim.kind.value,
                issue_number,
                exc,
            )
            return LaunchResult(
                None,
                False,
                f"Could not record the pending-work claim: {exc}",
                disposition=LaunchDisposition.RETRYABLE_FAILURE,
            )
        return None

    def abandon_unspawned(self, run: SessionRunAssets) -> None:
        self.claims.defer_pending_work_claim(run)
        logger.info(
            "[WORK] No terminal started for %s; its claim is deferred and the "
            "work is waiting to be relaunched",
            self.claim.kind.value,
        )

    def settle_unspawned(self, disposal: WorkDisposal) -> None:
        """Finish the transaction :meth:`abandon_unspawned` left half-done.

        Deferring is the right FIRST move for every unspawned exit - it is
        crash-safe and says nothing the settlement might contradict. But it is
        not the last word (#6999 F4): the queue owner then decides whether the
        request still exists, and until that decision reaches the ledger the two
        can disagree. They disagreed in exactly the way that loses correctness:
        a dropped item left a recoverable row, so the startup sweep resurrected
        work a permanent failure or an exhausted, escalated retry budget had
        deliberately ended.
        """
        work_key = self.claim.work_key()
        if disposal is WorkDisposal.DROPPED:
            self.claims.retire_deferred_claim(work_key)
            logger.info(
                "[WORK] %s work was dropped by its queue; retiring its claim "
                "so recovery cannot re-admit it",
                self.claim.kind.value,
            )
            return
        # Still owned by a queue. The payload is rewritten from the request as
        # it stands NOW, because the settlement may have spent part of its
        # bounded retry budget - a restart that read the pre-launch payload
        # would refund it.
        self.claims.refresh_deferred_claim(work_key, self.claim)


@dataclass(frozen=True, slots=True)
class _ClaimlessLaunch:
    """An ordinary issue session takes nothing off a pending queue.

    An explicit null object rather than an optional every launch path would have
    to re-check before touching (#6999 A2).
    """

    def hold_before_spawn(
        self, run: SessionRunAssets, *, issue_number: int
    ) -> LaunchResult | None:
        return None

    def abandon_unspawned(self, run: SessionRunAssets) -> None:
        return None

    def settle_unspawned(self, disposal: WorkDisposal) -> None:
        return None


NO_LAUNCH_WORK_CLAIM: LaunchWorkClaim = _ClaimlessLaunch()


@dataclass(slots=True)
class SpawnGuard:
    """Whether a launch reached the irreversible point of a live terminal."""

    terminal_spawned: bool = False

    def mark_spawned(self) -> None:
        self.terminal_spawned = True


@contextmanager
def abandon_claim_unless_spawned(
    work: LaunchWorkClaim, run: SessionRunAssets
) -> Generator[SpawnGuard, None, None]:
    """Give the queued work back on every launch exit that started no terminal.

    The compensating half of :meth:`PendingWorkLaunchClaim.hold_before_spawn`
    (#6999 A2). A launch has many pre-spawn failure exits - setup commands, a
    label that would not apply, the spawn itself - and an exception can leave by
    none of them; one guard covers the lot, so a new early return cannot forget
    to hand the work back.
    """
    guard = SpawnGuard()
    try:
        yield guard
    finally:
        if not guard.terminal_spawned:
            work.abandon_unspawned(run)


@dataclass(frozen=True)
class LaunchSettlement:
    """One launch's whole transaction: terminal outcome, queue, and ledger.

    The single place "does this launch outcome consume the work?" is answered.
    Each queue supplies its own removal and, where it has one, its restoration
    and bounded-retry behaviour; the mapping from disposition to action is
    shared, so a new disposition cannot mean different things per queue and an
    unhandled one cannot silently fall through to dropping the item (#6999 A1).

    It settles BOTH copies of the request, which is what makes it a transaction
    rather than a queue mutator (#6999 F4/A2). Every branch below ends in
    exactly one of three durable states, and none of them is implicit:

    * the work is now held by a live terminal — handed to
      :class:`InFlightWorkLedger` against that terminal, so the claim survives
      for as long as the session does (#6999 F2);
    * a queue still owns it — the deferred row stays as its crash-safe backing,
      rewritten from the request as the settlement leaves it;
    * it was dropped — the deferred row is retired with the queue item, because
      a permanent failure or an escalated, exhausted budget that leaves a
      recoverable row is not permanent at all.

    ``work`` is the SAME object the launch held its claim with, so the durable
    record spans the whole launch rather than starting after it.
    """

    work: PendingWorkLaunchClaim
    remove: Callable[[], None]
    # Adopting an already-running terminal, and spending one unit of the
    # bounded retryable-failure budget. Both default to doing nothing, for the
    # queues that have no such behaviour — an explicit no-op rather than an
    # optional every caller of `settle` would have to re-check. The retry
    # callback reports what it decided, because whether the item survived its
    # own budget is exactly what the durable half has to match.
    restore_existing: Callable[[], Optional[Session]] = field(default=lambda: None)
    retain_for_retry: Callable[[], WorkDisposal] = field(
        default=lambda: WorkDisposal.RETAINED
    )
    # Validation retries own their own durable queue and are re-derived from
    # it, so a plain failure leaves the item alone. Every other queue drops.
    drop_on_permanent_failure: bool = True

    def settle(
        self, result: LaunchResult, state: "OrchestratorState"
    ) -> Optional[Session]:
        if result.success and result.session:
            self._consume_into_flight(result.session, state)
            append_unique_active_sessions(state.active_sessions, [result.session])
            return result.session
        if result.disposition is LaunchDisposition.EXISTING_TERMINAL:
            restored = self.restore_existing()
            if restored:
                # An adopted terminal is running this work exactly as a freshly
                # spawned one is, so it holds the claim on the same terms.
                self._consume_into_flight(restored, state)
                return restored
            # Nothing was adopted, so the item is still queued and waiting.
            self.work.settle_unspawned(WorkDisposal.RETAINED)
            return None
        self.work.settle_unspawned(self._dispose(result))
        return None

    def _dispose(self, result: LaunchResult) -> WorkDisposal:
        """What this launch outcome means for the request, as one typed answer.

        Deliberately returns the disposal instead of acting on the ledger
        itself: the queue decision and the durable one are the same decision,
        and splitting them is how a dropped item kept a recoverable row.
        """
        if result.disposition is LaunchDisposition.PROVIDER_DEFERRED:
            # The provider refused before the work was touched. Keep the item
            # exactly as it is: no restoration attempt (there is no terminal to
            # restore) and no budget spent (nothing about this request failed).
            # For a failure investigation the queue is the only record that
            # exists, so dropping it here would lose it permanently.
            logger.info("[PROVIDER] Launch deferred, work retained: %s", result.reason)
            return WorkDisposal.RETAINED
        if result.disposition is LaunchDisposition.RETRYABLE_FAILURE:
            # The queue owner spends the budget and says whether the item
            # survived it. An exhausted budget is a committed drop, not a
            # retention, and the ledger has to hear about it.
            return self.retain_for_retry()
        if result.disposition is LaunchDisposition.PERMANENT_FAILURE:
            if self.drop_on_permanent_failure:
                self.remove()
                return WorkDisposal.DROPPED
            return WorkDisposal.RETAINED
        # Named explicitly rather than left as a fall-through: dropping the
        # work is the destructive branch, and a disposition added later without
        # a decision here must not silently land in it (#6999 A1).
        raise ValueError(f"unhandled launch disposition: {result.disposition}")

    def _consume_into_flight(
        self, session: Session, state: "OrchestratorState"
    ) -> None:
        # The ledger first: it is the thing that can refuse (a terminal already
        # holding a different claim is a bug, not a launch). Removing the queue
        # item only after it accepts means a refusal leaves the work queued.
        # For a freshly spawned session this re-holds the claim the launch
        # already took, which the store treats as idempotent; for an ADOPTED
        # terminal it is the first hold, against that terminal's own run assets.
        InFlightWorkLedger(state, self.work.claims).take(session, self.work.claim)
        self.remove()


__all__ = [
    "NO_LAUNCH_WORK_CLAIM",
    "LaunchSettlement",
    "LaunchWorkClaim",
    "PendingWorkLaunchClaim",
    "SpawnGuard",
    "WorkDisposal",
    "abandon_claim_unless_spawned",
]
