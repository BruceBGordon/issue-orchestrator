"""The single owner of the orchestrator's paused state.

Every pause and resume in the system funnels through :class:`PauseController`.
That is a deliberate response to how the state used to be managed: four modules
assigned ``OrchestratorState.paused`` directly, so the event, the log line, and
the durable record each depended on whichever call site happened to remember
them. Two call sites remembered none, five recorded no reason, and nothing was
persisted at all.

Concentrating the transition here means the provenance is structural rather than
conventional — a caller *cannot* pause without naming a reason and an actor,
because those are required arguments, and the event/log/journal fan-out happens
once, in one place, for every path.

The controller owns the transition; it does not own the policy of *when* to
pause. Callers decide that and say why.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol
from datetime import datetime, timedelta, timezone

from ..domain.pause_state import (
    PauseActor,
    PauseReason,
    PauseState,
    PauseTransition,
    PauseTransitionOutcome,
)
from ..events.catalog import EventName
from ..events.context import EventContext
from ..ports.event_sink import EventSink, make_trace_event
from ..ports.pause_journal import NullPauseJournal, PauseJournal

logger = logging.getLogger(__name__)


class PauseStateStore(Protocol):
    """Where the pause state actually lives.

    ``OrchestratorState`` satisfies this via its ``pause_state`` field. Depending
    on the one-attribute shape rather than the whole state object keeps the
    controller testable and stops it reaching into unrelated orchestrator state.

    The controller holds this store by reference for its lifetime, which also
    carries the incident backoff ladder. ``Orchestrator.state`` is therefore
    bound once and never reassigned; rebinding it would strand the owner on a
    detached object and silently disable auto-resume.
    """

    pause_state: PauseState


# Half-open retry schedule for INCIDENT pauses (the loop-error breaker).
#
# A pause caused by a transient fault — a DNS drop while the laptop sleeps, a
# database file briefly unreachable — used to be permanent: three consecutive
# tick errors halted the engine and nothing ever resumed it. Real incidents of
# exactly that shape left the engine paused for days.
#
# So an incident pause is now half-open: it expires, the engine retries, and if
# the fault has cleared it simply carries on. Escalating delays mean a genuinely
# broken engine backs off instead of hot-looping, while a blip costs a minute.
# The streak only resets after a healthy tick, so repeated failures really do
# climb the ladder rather than oscillating at the first rung forever.
_AUTO_RESUME_BACKOFF_SECONDS: tuple[float, ...] = (60.0, 300.0, 900.0, 3600.0)

# Consecutive failing ticks before the breaker trips. One blip is noise; three
# in a row means the engine cannot make progress.
_TICK_ERROR_LIMIT = 3


class PauseController:
    """Owns pause/resume transitions and their observability fan-out.

    Transitions are idempotent: pausing an already-paused engine keeps the
    ORIGINAL reason and timestamp rather than overwriting them. That matters
    because the first cause is the interesting one — an operator pausing an
    engine the error breaker already stopped must not erase the incident that
    actually halted it.
    """

    def __init__(
        self,
        *,
        events: EventSink,
        event_context: EventContext,
        store: "PauseStateStore",
        journal: PauseJournal | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._events = events
        self._event_context = event_context
        # The pause state is stored in ONE place — the orchestrator state object
        # every view model and planner already reads. Keeping a second copy here
        # would let the owner and its readers disagree, which is the same class
        # of bug this controller exists to remove.
        self._store = store
        self._journal = journal if journal is not None else NullPauseJournal()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._lock = threading.Lock()
        self._auto_resume_at: datetime | None = None
        self._incident_streak = 0
        self._consecutive_tick_errors = 0

    @property
    def state(self) -> PauseState:
        """The current pause state (immutable snapshot)."""
        return self._store.pause_state

    @property
    def paused(self) -> bool:
        return self.state.paused

    def pause(
        self,
        *,
        reason: PauseReason,
        actor: PauseActor,
        detail: str = "",
        emit_event: bool = True,
    ) -> PauseTransitionOutcome:
        """Pause the engine, recording why and who.

        Returns an outcome whose ``committed`` flag says whether THIS call
        performed the transition. When already paused this is a no-op that
        preserves the existing provenance — see the class docstring — and the
        outcome reports the stored actor, not the requested one.
        """
        now = self._clock()
        with self._lock:
            current = self._store.pause_state
            if current.paused:
                logger.debug(
                    "[PAUSE] Already paused (%s); ignoring %s pause from %s",
                    current.describe(now),
                    reason,
                    actor,
                )
                return PauseTransitionOutcome(
                    committed=False, state=current, requested_actor=actor
                )
            new_state = PauseState.paused_now(
                reason=reason, actor=actor, detail=detail, now=now
            )
            self._store.pause_state = new_state
            if reason.is_incident:
                index = min(self._incident_streak, len(_AUTO_RESUME_BACKOFF_SECONDS) - 1)
                backoff = _AUTO_RESUME_BACKOFF_SECONDS[index]
                self._incident_streak += 1
                self._auto_resume_at = now + timedelta(seconds=backoff)
                retry_at = self._auto_resume_at
            else:
                # A deliberate pause is held until something deliberately lifts it.
                self._auto_resume_at = None
                retry_at = None

        log = logger.warning if reason.is_incident else logger.info
        log("[PAUSE] Orchestrator paused — %s", new_state.describe(now))
        if retry_at is not None:
            logger.warning(
                "[PAUSE] Incident pause #%d — will auto-retry at %s. "
                "Resume sooner with POST /api/resume.",
                self._incident_streak,
                retry_at.isoformat(),
            )
        self._journal.record(
            PauseTransition(
                at=now, paused=True, reason=reason, actor=actor, detail=detail
            )
        )
        if emit_event:
            self._events.publish(
                make_trace_event(
                    EventName.ORCHESTRATOR_PAUSED,
                    self._event_context.enrich(new_state.to_payload(now)),
                )
            )
        return PauseTransitionOutcome(
            committed=True, state=new_state, requested_actor=actor
        )

    def resume(
        self, *, actor: PauseActor, detail: str = ""
    ) -> PauseTransitionOutcome:
        """Resume the engine, recording who resumed it and what it was paused for."""
        now = self._clock()
        with self._lock:
            previous = self._claim_resume_locked()
            if previous is None:
                logger.debug("[PAUSE] Already running; ignoring resume from %s", actor)
                return PauseTransitionOutcome(
                    committed=False,
                    state=self._store.pause_state,
                    requested_actor=actor,
                )
        return self._announce_resume(previous, actor=actor, detail=detail, now=now)

    def resume_if_due(
        self, *, actor: PauseActor, detail: str = ""
    ) -> PauseTransitionOutcome:
        """Atomically resume ONLY IF a half-open incident pause is still due.

        The check and the transition must be one operation. Split apart, the
        tick thread can observe an incident as due, an operator can resume and
        then deliberately re-pause on another thread, and the tick's stale
        decision then lifts that deliberate pause — silently restarting an
        engine a human just stopped. Re-reading the deadline under the same lock
        that performs the swap makes that interleaving impossible.
        """
        now = self._clock()
        with self._lock:
            if not self._is_auto_resume_due_locked(now):
                return PauseTransitionOutcome(
                    committed=False,
                    state=self._store.pause_state,
                    requested_actor=actor,
                )
            previous = self._claim_resume_locked()
            if previous is None:  # pragma: no cover - implied by the due check
                return PauseTransitionOutcome(
                    committed=False,
                    state=self._store.pause_state,
                    requested_actor=actor,
                )
        logger.info("[PAUSE] Incident pause backoff elapsed (%s) — retrying",
                    previous.describe(now))
        return self._announce_resume(previous, actor=actor, detail=detail, now=now)

    def _claim_resume_locked(self) -> PauseState | None:
        """Swap paused -> running under the caller's lock.

        Returns the previous paused state, or None if it was already running.
        """
        previous = self._store.pause_state
        if not previous.paused:
            return None
        self._store.pause_state = PauseState.running()
        self._auto_resume_at = None
        # A resume restarts the error budget: the next trip must earn its own
        # three failures rather than inheriting the ones that caused this pause.
        self._consecutive_tick_errors = 0
        return previous

    def _announce_resume(
        self,
        previous: PauseState,
        *,
        actor: PauseActor,
        detail: str,
        now: datetime,
    ) -> PauseTransitionOutcome:
        """Log, journal, and publish a resume that has already been committed."""

        held = previous.held_seconds(now)
        logger.info(
            "[PAUSE] Orchestrator resumed by %s after %.0fs paused (was %s)",
            actor,
            held,
            previous.reason,
        )
        self._journal.record(
            PauseTransition(
                at=now,
                paused=False,
                reason=None,
                actor=actor,
                detail=detail,
                previous_reason=previous.reason,
                held_seconds=held,
            )
        )
        self._events.publish(
            make_trace_event(
                EventName.ORCHESTRATOR_RESUMED,
                self._event_context.enrich(
                    {
                        "resumed_by": str(actor),
                        # Non-null by construction: only a committed resume gets
                        # here, and a paused state always carries a reason.
                        "previous_pause_reason": str(previous.reason),
                        "paused_held_seconds": held,
                        "detail": detail,
                    }
                ),
            )
        )
        return PauseTransitionOutcome(
            committed=True, state=self._store.pause_state, requested_actor=actor
        )

    def due_for_auto_resume(self, now: datetime | None = None) -> bool:
        """Whether a half-open incident pause has waited out its backoff.

        Only ever true for incident pauses — a deliberate pause (operator,
        startup, tech-lead) is never lifted behind the caller's back.
        """
        moment = now if now is not None else self._clock()
        with self._lock:
            return self._is_auto_resume_due_locked(moment)

    def _is_auto_resume_due_locked(self, now: datetime) -> bool:
        """Due-ness predicate; assumes the caller holds the lock."""
        if not self._store.pause_state.paused or self._auto_resume_at is None:
            return False
        return now >= self._auto_resume_at

    def note_tick_failure(self, error: Exception) -> PauseTransitionOutcome | None:
        """Count a failing tick and trip the breaker once they run consecutive.

        The count lives here rather than on the engine because it IS pause
        policy: it decides when a pause happens, exactly as the backoff ladder
        beside it decides when one lifts. Returns the pause outcome when this
        failure tripped the breaker, else None.
        """
        with self._lock:
            self._consecutive_tick_errors += 1
            count = self._consecutive_tick_errors
            already_paused = self._store.pause_state.paused
        if count < _TICK_ERROR_LIMIT or already_paused:
            return None
        return self.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD,
            actor=PauseActor.SYSTEM,
            detail=(
                f"{count} consecutive tick errors; "
                f"last: {type(error).__name__}: {error}"
            ),
        )

    @property
    def consecutive_tick_errors(self) -> int:
        """How many ticks have failed back-to-back (for event payloads)."""
        with self._lock:
            return self._consecutive_tick_errors

    def note_healthy_tick(self) -> None:
        """Record that the engine completed a tick cleanly.

        Clears the consecutive-error count and the incident backoff ladder, so
        an engine that recovers is not punished by escalation earned during an
        earlier bad patch.
        """
        with self._lock:
            self._consecutive_tick_errors = 0
            if self._store.pause_state.paused or self._incident_streak == 0:
                return
            self._incident_streak = 0

    def describe(self) -> str:
        """One-line summary of the current state, stamped with elapsed time."""
        return self.state.describe(self._clock())

    def recent_transitions(self, limit: int = 20) -> list[PauseTransition]:
        """The durable pause history — what "why is it paused?" actually needs."""
        return self._journal.recent(limit)
