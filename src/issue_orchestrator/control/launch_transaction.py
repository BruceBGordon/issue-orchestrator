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

    def settle_unspawned(
        self, disposal: WorkDisposal, claim: PendingWorkClaim | None = None
    ) -> None:
        """Bring the durable row into line with what the queue decided."""
        ...

    def spend_budget(self, claim: PendingWorkClaim) -> None:
        """Persist a request whose bounded retry budget has just been spent."""
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

    def settle_unspawned(
        self, disposal: WorkDisposal, claim: PendingWorkClaim | None = None
    ) -> None:
        """Finish the transaction :meth:`abandon_unspawned` left half-done.

        Deferring is the right FIRST move for every unspawned exit - it is
        crash-safe and says nothing the settlement might contradict. But it is
        not the last word (#6999 F4): the queue owner then decides whether the
        request still exists, and until that decision reaches the ledger the two
        can disagree. They disagreed in exactly the way that loses correctness:
        a dropped item left a recoverable row, so the startup sweep resurrected
        work a permanent failure or an exhausted, escalated retry budget had
        deliberately ended.

        ``claim`` is the request as the DECISION leaves it, which is not always
        the object the launch held: a spent retry budget produces an advanced
        copy so the durable payload can be written before the in-memory queue is
        touched (#6999 F2).
        """
        settled = claim or self.claim
        work_key = settled.work_key()
        if disposal is WorkDisposal.DROPPED:
            self.claims.retire_deferred_claim(work_key)
            logger.info(
                "[WORK] %s work was dropped by its queue; retiring its claim "
                "so recovery cannot re-admit it",
                settled.kind.value,
            )
            return
        # Still owned by a queue. The payload is rewritten from the request as
        # the decision leaves it, because the settlement may have spent part of
        # its bounded retry budget - a restart that read the pre-launch payload
        # would refund it.
        self.claims.refresh_deferred_claim(work_key, settled)

    def spend_budget(self, claim: PendingWorkClaim) -> None:
        """Make one spent unit of a bounded retry budget durable, first (#6999 F2).

        The budget lives in the queued request, and the in-memory queue does not
        survive a crash - so until the advanced request is in the ledger, the
        unit is not spent at all. Everything the settlement does afterwards can
        fail (an escalation that does not commit, a process that dies): none of
        it may be able to refund the attempt, so this write goes first, before
        the in-memory queue is touched and before anything irreversible runs.
        """
        self.claims.refresh_deferred_claim(claim.work_key(), claim)


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

    def settle_unspawned(
        self, disposal: WorkDisposal, claim: PendingWorkClaim | None = None
    ) -> None:
        return None

    def spend_budget(self, claim: PendingWorkClaim) -> None:
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


@dataclass(frozen=True, slots=True)
class SettlementDecision:
    """A settled launch outcome, and the queue projection still owed (#6999 F2).

    The decision and its projection are separated because they are not equally
    durable. The ledger row survives a restart; the in-memory queue does not. So
    the decision is committed to the ledger FIRST and the queue is then brought
    into line with it — never the other way round, which is how a permanently
    dropped item kept a recoverable row for the startup sweep to re-admit.
    """

    disposal: WorkDisposal
    #: The request as the decision leaves it, which is what the ledger stores.
    claim: PendingWorkClaim
    #: The in-memory queue mutation this decision implies. Run only after the
    #: durable side has committed.
    project: Callable[[], None]


def _no_projection() -> None:
    """The queue has nothing to do: the item is already where it belongs."""


@dataclass(frozen=True, slots=True)
class RetryPlan:
    """What spending one unit of a bounded launch budget WOULD do (#6999 F2).

    Planned rather than done, because the spend has to reach the ledger before
    it reaches anything else. ``spent`` is an advanced COPY of the request, so
    the durable payload can be written while the queue still holds the original;
    ``apply`` then projects the same spend onto that queue.

    ``commit_exhaustion`` is the only irreversible act in the whole settlement:
    the external needs-human transition that ends a bounded budget. It runs with
    the spend already durable, so a failure or a crash inside it can never
    refund the attempt, and it reports whether it committed — an escalation that
    did not commit leaves the work queued to try again.
    """

    spent: PendingWorkClaim
    exhausted: bool
    apply: Callable[[], None]
    commit_exhaustion: Callable[[], bool]


def unbounded_retry(claim: PendingWorkClaim) -> RetryPlan:
    """The plan for a queue with no launch budget: retain, spend nothing.

    An explicit default rather than an optional every settlement would have to
    re-check. Validation retries, reviews and reworks are re-derived from
    durable state, so a retryable launch failure simply leaves them queued.
    """
    return RetryPlan(
        spent=claim,
        exhausted=False,
        apply=_no_projection,
        commit_exhaustion=lambda: False,
    )


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

    Each of those reaches the LEDGER before it reaches the queue (#6999 F2).
    The two are not equally durable, so they are not peers: the ledger row is
    the decision and the queue is its projection. See :meth:`_commit`.

    ``work`` is the SAME object the launch held its claim with, so the durable
    record spans the whole launch rather than starting after it.
    """

    work: PendingWorkLaunchClaim
    remove: Callable[[], None]
    # Adopting an already-running terminal, and PLANNING the spend of one unit
    # of the bounded retryable-failure budget. Both default to doing nothing,
    # for the queues that have no such behaviour — an explicit no-op rather than
    # an optional every caller of `settle` would have to re-check. The retry
    # callback PLANS rather than acts, because the spend has to reach the ledger
    # before it reaches the queue (#6999 F2).
    restore_existing: Callable[[], Optional[Session]] = field(default=lambda: None)
    plan_retry: Callable[[PendingWorkClaim], RetryPlan] = field(
        default=unbounded_retry
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
            self._commit(
                SettlementDecision(
                    WorkDisposal.RETAINED, self.work.claim, _no_projection
                )
            )
            return None
        self._commit(self._decide(result))
        return None

    def _commit(self, decision: SettlementDecision) -> None:
        """Durable side first, in-memory queue second (#6999 F2).

        The one ordering rule of the whole transaction. A ledger row outlives
        the process; a pending queue does not. Mutating the queue first left a
        window in which a crash - or a store fault - kept a deferred row for
        work the launcher had already dropped, and the startup sweep exists
        precisely to re-admit deferred rows. Committing the decision first makes
        the queue a PROJECTION of durable truth rather than a second, rival copy
        of it: a crash before the projection loses only state that a restart
        rebuilds from the ledger anyway.
        """
        self.work.settle_unspawned(decision.disposal, decision.claim)
        decision.project()

    def _decide(self, result: LaunchResult) -> SettlementDecision:
        """What this launch outcome means for the request, as one typed answer.

        Deliberately returns the decision - disposal, durable payload, and the
        queue projection it implies - instead of acting on either side itself:
        the queue decision and the durable one are the same decision, and
        splitting them is how a dropped item kept a recoverable row.
        """
        claim = self.work.claim
        if result.disposition is LaunchDisposition.PROVIDER_DEFERRED:
            # The provider refused before the work was touched. Keep the item
            # exactly as it is: no restoration attempt (there is no terminal to
            # restore) and no budget spent (nothing about this request failed).
            # For a failure investigation the queue is the only record that
            # exists, so dropping it here would lose it permanently.
            logger.info("[PROVIDER] Launch deferred, work retained: %s", result.reason)
            return SettlementDecision(WorkDisposal.RETAINED, claim, _no_projection)
        if result.disposition is LaunchDisposition.RETRYABLE_FAILURE:
            return self._spend_retry_budget(claim)
        if result.disposition is LaunchDisposition.PERMANENT_FAILURE:
            if self.drop_on_permanent_failure:
                return SettlementDecision(WorkDisposal.DROPPED, claim, self.remove)
            return SettlementDecision(WorkDisposal.RETAINED, claim, _no_projection)
        # Named explicitly rather than left as a fall-through: dropping the
        # work is the destructive branch, and a disposition added later without
        # a decision here must not silently land in it (#6999 A1).
        raise ValueError(f"unhandled launch disposition: {result.disposition}")

    def _spend_retry_budget(self, claim: PendingWorkClaim) -> SettlementDecision:
        """Spend one unit of a bounded launch budget, durably, then act on it.

        Three steps in a fixed order, and the order is the correctness (#6999
        F2):

        1. the spend reaches the LEDGER, before the queue and before anything
           irreversible - so nothing that follows can refund the attempt;
        2. the queue is brought into line with it;
        3. only then does an exhausted budget run its external needs-human
           transition, and only a committed one drops the work.

        An escalation that does not commit leaves the request queued with its
        budget spent, so the next tick re-attempts the escalation rather than
        starting the budget again.

        The escalation in step 3 and the ledger retire that follows it straddle
        two stores - GitHub and SQLite - so they cannot be made one atomic act,
        and their ordering is a deliberate choice
        of which way to fail. Retiring the row first would mean a death in that
        window loses the work with nobody told about it. Escalating first means
        a death in that window leaves a recoverable row whose budget is already
        at its bound: the work comes back, the human has already been told, and
        the next settlement re-asserts the same idempotent escalation and drops
        it again. One redundant attempt is recoverable; a silently discarded
        investigation is not.
        """
        plan = self.plan_retry(claim)
        self.work.spend_budget(plan.spent)
        plan.apply()
        if not plan.exhausted or not plan.commit_exhaustion():
            return SettlementDecision(
                WorkDisposal.RETAINED, plan.spent, _no_projection
            )
        return SettlementDecision(WorkDisposal.DROPPED, plan.spent, self.remove)

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
    "RetryPlan",
    "SettlementDecision",
    "SpawnGuard",
    "WorkDisposal",
    "abandon_claim_unless_spawned",
    "unbounded_retry",
]
