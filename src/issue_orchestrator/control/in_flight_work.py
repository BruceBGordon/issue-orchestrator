"""Ownership of queued work from launch to terminal settlement (#6999 A1/F2).

Before this module the pending queues had an owner only up to the moment a
session spawned. The launch settlement decided whether a *launch outcome*
consumed the work, removed the item, and stopped there — so from the terminal's
first byte until it died, nothing in the system still knew that the running
session was carrying a request.

That gap lost work. A session launched from a queue, ran, and then hit an
expired provider credential took the ordinary BLOCKED completion path, which
records the outage and releases the issue claim but has no request to give back:

* a tech-lead failure investigation carries its typed ``DiscoveredFailure`` in
  the queue item and nowhere else — dropping it loses the investigation;
* a validation retry carries its original prompt, validation error, retry count
  and source task in the queue item — a BLOCKED status does not take the
  ``NEEDS_VALIDATION_RETRY`` reconstruction branch, so the retry evaporates;
* a rework has its ``needs-rework`` trigger removed at launch, so neither the
  queue nor the labels are left holding it.

:class:`InFlightWorkLedger` closes that span. A launch hands it the typed claim;
terminal completion hands it back a typed :class:`SettlementOutcome`. Consuming
the work and returning it are the same decision made in one place, so no
completion path can reconstruct work from terminal-name prefixes or scattered
labels, and none can quietly forget to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from ..domain.models import PendingRework, Session
from ..domain.pending_work import InFlightWork, PendingWorkClaim, PendingWorkKind
from ..domain.session_key import SessionKey, TaskKind
from ..ports.pending_work_claim_store import ClaimState, PendingWorkClaimStore
from ..ports.provider_resilience import ProviderErrorType
from .active_sessions import append_unique_active_sessions
from .session_launch_types import LaunchDisposition, LaunchResult

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from .claim_quarantine import ClaimQuarantineOwner

logger = logging.getLogger(__name__)


class SettlementOutcome(Enum):
    """What a terminated session means for the request it launched with.

    Only two answers exist, and they are about the WORK, not about the session:
    either the work was really attempted and is spent, or the provider stopped
    the session before the work could be, and the request is untouched.
    """

    CONSUMED = "consumed"
    PROVIDER_DEFERRED = "provider_deferred"

    @classmethod
    def for_provider_error(
        cls, provider_error_type: ProviderErrorType | None
    ) -> "SettlementOutcome":
        """Classify a terminal completion by its typed provider verdict.

        A typed provider verdict — AUTH (a dead credential) or TRANSIENT (the
        provider itself was unreachable after its own retries) — means the
        session never got to attempt the work, so its claim is deferred rather
        than spent. Anything else, including an agent that reported BLOCKED on
        the substance of the work, consumes the claim.

        ``None`` is the overwhelmingly common case and maps to CONSUMED, so this
        classifier keeps today's behaviour for every non-provider outcome.
        """
        if provider_error_type in (
            ProviderErrorType.AUTH,
            ProviderErrorType.TRANSIENT,
        ):
            return cls.PROVIDER_DEFERRED
        return cls.CONSUMED


@dataclass(frozen=True, slots=True)
class QuarantinedSession:
    """A restored terminal whose claim record could not be read (#6999 F6).

    The dangerous state this type exists to make un-ignorable: the terminal is
    alive and doing queued work, and the orchestrator no longer knows which.
    Admitting it to ordinary processing would let its completion settle as
    claimless and destroy that work; the caller must instead keep it out and
    raise the problem where an operator will see it.
    """

    session: Session
    error: str
    # The store's own key for the run, so a rediscovered terminal maps back to
    # the same record rather than a new one.
    run_key: str
    # ...and the generation-aware key the quarantine marker itself uses, so a
    # replacement run reusing the directory gets its own (#6999 F12).
    quarantine_key: str


@dataclass(frozen=True, slots=True)
class StaleRun:
    """A live terminal whose claim has already been deferred (#6999 F8).

    Its work is back on a queue, so the terminal must not be admitted: settling
    it would let a run the orchestrator has already written off consume work
    the queue now owns. Reported rather than merely skipped so the anomaly is
    visible.
    """

    session: Session
    claim: PendingWorkClaim


@dataclass(frozen=True, slots=True)
class ClaimRestoration:
    """Per-session verdicts from rehydrating a restart's live terminals."""

    admitted: tuple[Session, ...]
    quarantined: tuple[QuarantinedSession, ...]
    stale: tuple[StaleRun, ...] = ()

    def live_quarantine_keys(self) -> frozenset[str]:
        """Quarantines this pass re-confirmed, for release reconciliation."""
        return frozenset(q.quarantine_key for q in self.quarantined)

    def observed_run_keys(self, claims: PendingWorkClaimStore) -> set[str]:
        """Every run this pass saw alive, whatever verdict it reached.

        Recovery must treat ALL of them as live, not just the admitted ones
        (#6999 F11): a quarantined run is deliberately kept out of
        ``active_sessions``, and without this its row would look orphaned, be
        re-queued and deleted - after which the same terminal reads as
        claimless and is admitted normally.
        """
        return {
            claims.run_key_for(session.run_assets)
            for session in (
                list(self.admitted)
                + [q.session for q in self.quarantined]
                + [stale.session for stale in self.stale]
            )
        }


class DuplicateClaimError(RuntimeError):
    """Two different claims were taken against one terminal (#6999 F5).

    A terminal id identifies one live session, so this means registry drift or
    a repeated adoption - a bug in the caller. Replacing the first claim would
    convert that bug into silent work loss, which is exactly what this whole
    boundary exists to prevent, so it is raised instead. Re-taking the SAME
    claim is idempotent and does not raise: a retried adoption of the terminal
    it already recorded has changed nothing.
    """


@dataclass(frozen=True, slots=True)
class InFlightWorkLedger:
    """The one owner of claims held by running sessions.

    Deliberately thin: it holds claims and hands them back. Deciding which queue
    admits a returned request is the pending-queue owner's job, and deciding
    what a termination *means* is the caller's typed
    :class:`SettlementOutcome`.

    Holds each claim in two places on purpose. ``state.in_flight_work`` is the
    fast in-process record; ``claims`` is the durable one, written beside the
    run assets of the session that took it, so a restart can rebuild what a
    live terminal is carrying (#6999 F4). The two are kept in step here and
    nowhere else.
    """

    state: "OrchestratorState"
    claims: PendingWorkClaimStore

    def take(self, session: Session, claim: PendingWorkClaim) -> None:
        """Record that ``session``'s terminal launched holding ``claim``.

        Raises :class:`DuplicateClaimError` if the terminal already holds a
        DIFFERENT claim. Taking the same claim twice is idempotent.
        """
        terminal_id = session.terminal_id
        existing = self.holds(terminal_id)
        if existing is not None and existing != claim:
            raise DuplicateClaimError(
                f"terminal {terminal_id} already holds a {existing.kind.value} "
                f"claim; refusing to replace it with a {claim.kind.value} one"
            )
        if existing is None:
            self.state.in_flight_work.append(InFlightWork(terminal_id, claim))
        # Written every time, including the idempotent path: the durable record
        # is what a restart reads, and an in-memory hit is no evidence that the
        # on-disk one was ever produced.
        self.claims.hold_pending_work_claim(
            session.run_assets, claim, issue_number=session.issue.number
        )
        logger.debug("[WORK] %s holds %s", terminal_id, claim.kind.value)

    def settle(
        self, session: Session, outcome: SettlementOutcome
    ) -> PendingWorkClaim | None:
        """Release the claim ``session``'s terminal holds, returning it if deferred.

        Returns the claim that was settled, or ``None`` when the terminal held
        none - the ordinary case for an issue session, which claims its work
        with a label rather than by dequeuing it.

        Settlement never destroys the only record of the work. A consumed
        claim is deleted because the work was really attempted. A deferred one
        is moved to a durable "deferred" row FIRST and re-admitted to its queue
        second, so a crash on either side of the in-memory re-queue leaves a row
        startup can enumerate (#6999 F8). If re-admission raises - an
        unregistered queue kind, a queue owner fault - the in-memory claim stays
        held too, so the next attempt can still find it (#6999 F5).
        """
        terminal_id = session.terminal_id
        held = self.holds(terminal_id)
        if held is None:
            return None
        if outcome is SettlementOutcome.CONSUMED:
            # The work really was attempted; only now may the durable record go.
            self.claims.consume_pending_work_claim(session.run_assets)
            self._forget_in_memory(session)
            return held
        # Durable first, in-memory second. The row moves to "deferred" and
        # SURVIVES, so a crash on either side of the in-memory re-queue leaves
        # something a fresh process can enumerate and re-admit (#6999 F8). It is
        # deleted only when a relaunch takes the same work again.
        self.claims.defer_pending_work_claim(session.run_assets)
        self._restore(terminal_id, held)
        self._forget_in_memory(session)
        return held

    def rehydrate(self, sessions: Sequence[Session]) -> "ClaimRestoration":
        """Re-take the claims of terminals that survived a restart (#6999 F4).

        The pending queues are in-memory, so after a restart a live terminal's
        request is in the orchestrator's claim store and nowhere else. Reading
        it back is what lets a provider failure observed AFTER the restart still
        return the work.

        The claim is also the authority on what the terminal is doing, which the
        restored session cannot always tell: terminal-name parsing gives a
        rework session generic CODE identity and no PR number, and without the
        PR number the provider-blocked planner cannot put ``needs-rework`` back.
        Reconciling identity from the claim fixes that at its source.

        Returns a typed per-session verdict rather than a bare list (#6999 F6).
        A session whose claim is RECORDED BUT UNREADABLE is quarantined, not
        admitted: letting it run on would end with ``settle`` finding no claim
        and treating it exactly like a claimless issue session, silently
        destroying the queued request the unreadable record described. Its
        neighbours are unaffected — one bad record must not stop an
        orchestrator restarting.
        """
        admitted: list[Session] = []
        quarantined: list[QuarantinedSession] = []
        stale: list[StaleRun] = []
        for session in sessions:
            try:
                lookup = self.claims.look_up_pending_work_claim(session.run_assets)
            except Exception as exc:  # adapter-defined decode/identity failure
                logger.error(
                    "[WORK] Quarantining %s: its claim record exists but could "
                    "not be read, so what work it holds is unknown: %s",
                    session.terminal_id,
                    exc,
                )
                quarantined.append(
                    QuarantinedSession(
                        session,
                        str(exc),
                        self.claims.run_key_for(session.run_assets),
                        self.claims.quarantine_key_for(session.run_assets),
                    )
                )
                continue
            if lookup.state is ClaimState.DEFERRED:
                assert lookup.claim is not None
                logger.error(
                    "[WORK] NOT restoring %s: its claim was already deferred, so "
                    "the work is back on its queue. Admitting it would let a "
                    "written-off run settle work the queue now owns.",
                    session.terminal_id,
                )
                stale.append(StaleRun(session, lookup.claim))
                continue
            claim = lookup.held
            if claim is not None:
                if self.holds(session.terminal_id) is None:
                    self.state.in_flight_work.append(
                        InFlightWork(session.terminal_id, claim)
                    )
                _reconcile_restored_identity(session, claim)
                logger.info(
                    "[WORK] Restored terminal %s is still holding %s",
                    session.terminal_id,
                    claim.kind.value,
                )
            admitted.append(session)
        return ClaimRestoration(tuple(admitted), tuple(quarantined), tuple(stale))

    def recover_unresolved(
        self,
        quarantine: "ClaimQuarantineOwner",
        *,
        live_run_keys: frozenset[str] = frozenset(),
        live_quarantine_keys: frozenset[str] = frozenset(),
    ) -> int:
        """Re-admit ledger work that no live terminal is holding (#6999 F8).

        The enumerable half of recovery. ``rehydrate`` can only reach terminals
        discovery still finds; a run that ended while its settlement was
        interrupted - a crash mid-defer, a kill between the durable transition
        and the in-memory re-queue - leaves a row no discovery will ever
        surface. Without this sweep that work sits in the ledger forever, and
        for a failure investigation the ledger is the only record there is.

        Deferred rows are re-admitted for the same reason: their in-memory
        re-queue did not survive the restart, only the row did.

        ``live_run_keys`` carries every run this pass observed alive whatever
        verdict it reached - including quarantined ones, which are deliberately
        missing from ``active_sessions`` and would otherwise look orphaned
        (#6999 F11). ``live_quarantine_keys`` names the quarantines that are
        still justified this pass; every other recorded one is released
        (#6999 F12).
        """
        live = live_run_keys | {
            self.claims.run_key_for(session.run_assets)
            for session in self.state.active_sessions
        }
        readmitted = 0
        for unresolved in self.claims.list_unresolved_claims():
            if unresolved.run_key in live:
                continue
            self._restore(unresolved.session_name, unresolved.claim)
            # The row STAYS, moved to deferred. An in-memory queue is not a
            # durable destination, so only a relaunch taking the same work may
            # retire it (#6999 F8). Re-running this sweep is therefore a no-op
            # against the queues, which dedupe by their own rules.
            self.claims.mark_deferred_by_run_key(unresolved.run_key)
            readmitted += 1
        still_quarantined: set[str] = set()
        for unreadable in self.claims.list_unreadable_claims():
            still_quarantined.add(f"{unreadable.run_key}@{unreadable.started_at}")
            if unreadable.run_key in live:
                continue
            # No work can be recovered from it, and it is not attached to any
            # live terminal, so the only honest move is to tell a human.
            quarantine.quarantine_unresolved(unreadable)
        # Anything still recorded but no longer in trouble has had its cause
        # repaired or removed; release it and the block it owns (#6999 F12).
        quarantine.reconcile_released(
            frozenset(still_quarantined | live_quarantine_keys)
        )
        return readmitted

    def holds(self, terminal_id: str) -> PendingWorkClaim | None:
        """The claim ``terminal_id`` is currently carrying, if any."""
        return next(
            (
                w.claim
                for w in self.state.in_flight_work
                if w.terminal_id == terminal_id
            ),
            None,
        )

    def _restore(self, terminal_id: str, claim: PendingWorkClaim) -> None:
        # Local import: session_routing owns the pending queues and builds
        # LaunchSettlement from this module, so importing it at module scope
        # would be a cycle. Same idiom as health_review_trigger.
        from .session_routing import PendingSessionQueues

        requeued = PendingSessionQueues(self.state).restore_deferred(claim)
        logger.warning(
            "[WORK] %s ended on its provider, not on its work: %s %s",
            terminal_id,
            claim.kind.value,
            "returned to its queue" if requeued else "already queued",
        )

    def _forget_in_memory(self, session: Session) -> None:
        self.state.in_flight_work[:] = [
            w
            for w in self.state.in_flight_work
            if w.terminal_id != session.terminal_id
        ]


def _reconcile_restored_identity(
    session: Session, claim: PendingWorkClaim
) -> None:
    """Give a restored session back the identity its claim proves it has.

    Restoration rebuilds a session from its terminal name and its run assets,
    which cannot express every task kind: a ``rework-*`` terminal comes back as
    generic CODE work with no PR number. The claim knows better, and downstream
    policy depends on it - notably restoring the ``needs-rework`` label, which
    is keyed on the PR (#6999 F4).
    """
    if claim.kind is not PendingWorkKind.REWORK:
        return
    request = claim.request
    assert isinstance(request, PendingRework)
    session.key = SessionKey(issue=session.key.issue, task=TaskKind.REWORK)
    if request.pr_number is not None:
        session.pr_number = request.pr_number
    session.rework_cycle = request.rework_cycle


@dataclass(frozen=True)
class LaunchSettlement:
    """How one pending queue settles its item for each launch disposition.

    The single place "does this launch outcome consume the work?" is answered.
    Each queue supplies its own removal and, where it has one, its restoration
    and bounded-retry behaviour; the mapping from disposition to action is
    shared, so a new disposition cannot mean different things per queue and an
    unhandled one cannot silently fall through to dropping the item (#6999 A1).

    Removal is not the end of the story. A consumed item is handed to
    :class:`InFlightWorkLedger` against the terminal that took it, so the claim
    survives for as long as the session does (#6999 F2).
    """

    claim: PendingWorkClaim
    claims: PendingWorkClaimStore
    remove: Callable[[], None]
    # Adopting an already-running terminal, and spending one unit of the
    # bounded required-input retry budget. Both default to doing nothing, for
    # the queues that have no such behaviour — an explicit no-op rather than an
    # optional every caller of `settle` would have to re-check.
    restore_existing: Callable[[], Optional[Session]] = field(default=lambda: None)
    retain_for_input_retry: Callable[[], None] = field(default=lambda: None)
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
        if result.disposition is LaunchDisposition.PROVIDER_DEFERRED:
            # The provider refused before the work was touched. Keep the item
            # exactly as it is: no restoration attempt (there is no terminal to
            # restore) and no budget spent (nothing about this request failed).
            # For a failure investigation the queue is the only record that
            # exists, so dropping it here would lose it permanently.
            logger.info("[PROVIDER] Launch deferred, work retained: %s", result.reason)
            return None
        if result.disposition is LaunchDisposition.INPUT_RETRY:
            self.retain_for_input_retry()
            return None
        if result.disposition is LaunchDisposition.PERMANENT_FAILURE:
            if self.drop_on_permanent_failure:
                self.remove()
            return None
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
        InFlightWorkLedger(state, self.claims).take(session, self.claim)
        self.remove()


__all__ = [
    "ClaimRestoration",
    "StaleRun",
    "DuplicateClaimError",
    "InFlightWorkLedger",
    "LaunchSettlement",
    "QuarantinedSession",
    "SettlementOutcome",
]
