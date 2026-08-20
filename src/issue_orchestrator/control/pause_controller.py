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
from datetime import datetime, timedelta, timezone

from ..domain.pause_state import PauseActor, PauseReason, PauseState, PauseTransition
from ..events.catalog import EventName
from ..events.context import EventContext
from ..ports.event_sink import EventSink, TraceEvent
from ..ports.pause_journal import NullPauseJournal, PauseJournal

logger = logging.getLogger(__name__)

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
        journal: PauseJournal | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._events = events
        self._event_context = event_context
        self._journal = journal if journal is not None else NullPauseJournal()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._lock = threading.Lock()
        self._state = PauseState.running()
        self._auto_resume_at: datetime | None = None
        self._incident_streak = 0

    @property
    def state(self) -> PauseState:
        """The current pause state (immutable snapshot)."""
        with self._lock:
            return self._state

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
    ) -> PauseState:
        """Pause the engine, recording why and who.

        Returns the resulting state. When already paused this is a no-op that
        preserves the existing provenance — see the class docstring.
        """
        now = self._clock()
        with self._lock:
            if self._state.paused:
                logger.debug(
                    "[PAUSE] Already paused (%s); ignoring %s pause from %s",
                    self._state.describe(now),
                    reason,
                    actor,
                )
                return self._state
            self._state = PauseState.paused_now(
                reason=reason, actor=actor, detail=detail, now=now
            )
            new_state = self._state
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
                TraceEvent(
                    EventName.ORCHESTRATOR_PAUSED,
                    self._event_context.enrich(new_state.to_payload(now)),
                )
            )
        return new_state

    def resume(self, *, actor: PauseActor, detail: str = "") -> PauseState:
        """Resume the engine, recording who resumed it and what it was paused for."""
        now = self._clock()
        with self._lock:
            if not self._state.paused:
                logger.debug("[PAUSE] Already running; ignoring resume from %s", actor)
                return self._state
            previous = self._state
            self._state = PauseState.running()
            self._auto_resume_at = None

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
            TraceEvent(
                EventName.ORCHESTRATOR_RESUMED,
                self._event_context.enrich(
                    {
                        "resumed_by": str(actor),
                        "previous_pause_reason": (
                            str(previous.reason) if previous.reason is not None else None
                        ),
                        "paused_held_seconds": held,
                        "detail": detail,
                    }
                ),
            )
        )
        return self._state

    def due_for_auto_resume(self, now: datetime | None = None) -> bool:
        """Whether a half-open incident pause has waited out its backoff.

        Only ever true for incident pauses — a deliberate pause (operator,
        startup, tech-lead) is never lifted behind the caller's back.
        """
        moment = now if now is not None else self._clock()
        with self._lock:
            if not self._state.paused or self._auto_resume_at is None:
                return False
            return moment >= self._auto_resume_at

    def note_healthy_tick(self) -> None:
        """Record that the engine completed a tick cleanly.

        Resets the incident backoff ladder, so an engine that recovers is not
        punished by the escalation earned during an earlier bad patch.
        """
        with self._lock:
            if self._state.paused or self._incident_streak == 0:
                return
            self._incident_streak = 0

    def describe(self) -> str:
        """One-line summary of the current state, stamped with elapsed time."""
        return self.state.describe(self._clock())

    def recent_transitions(self, limit: int = 20) -> list[PauseTransition]:
        """The durable pause history — what "why is it paused?" actually needs."""
        return self._journal.recent(limit)
