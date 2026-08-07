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
from typing import TYPE_CHECKING, Callable, Optional

from ..domain.models import Session
from ..domain.pending_work import InFlightWork, PendingWorkClaim
from ..ports.provider_resilience import ProviderErrorType
from .active_sessions import append_unique_active_sessions
from .session_launch_types import LaunchDisposition, LaunchResult

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState

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
class InFlightWorkLedger:
    """The one owner of claims held by running sessions.

    Deliberately thin: it holds claims and hands them back. Deciding which queue
    admits a returned request is the pending-queue owner's job, and deciding
    what a termination *means* is the caller's typed
    :class:`SettlementOutcome`.
    """

    state: "OrchestratorState"

    def take(self, terminal_id: str, claim: PendingWorkClaim) -> None:
        """Record that ``terminal_id`` launched holding ``claim``.

        Re-taking the same terminal replaces the previous claim rather than
        stacking a second one: a terminal id identifies one live session, so two
        simultaneous claims against it would be a bug, and keeping the newer one
        matches the item that was actually dequeued.
        """
        self._forget(terminal_id)
        self.state.in_flight_work.append(InFlightWork(terminal_id, claim))
        logger.debug("[WORK] %s holds %s", terminal_id, claim.kind.value)

    def settle(
        self, terminal_id: str, outcome: SettlementOutcome
    ) -> PendingWorkClaim | None:
        """Release ``terminal_id``'s claim, returning the work if deferred.

        Returns the claim that was settled, or ``None`` when the terminal held
        none — the ordinary case for an issue session, which claims its work
        with a label rather than by dequeuing it.
        """
        held = next(
            (w for w in self.state.in_flight_work if w.terminal_id == terminal_id),
            None,
        )
        if held is None:
            return None
        self._forget(terminal_id)
        if outcome is SettlementOutcome.CONSUMED:
            return held.claim
        self._restore(terminal_id, held.claim)
        return held.claim

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

    def _forget(self, terminal_id: str) -> None:
        self.state.in_flight_work[:] = [
            w for w in self.state.in_flight_work if w.terminal_id != terminal_id
        ]


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
        self.remove()
        InFlightWorkLedger(state).take(session.terminal_id, self.claim)


__all__ = [
    "InFlightWorkLedger",
    "LaunchSettlement",
    "SettlementOutcome",
]
