"""The orchestrator facade's pause surface, kept out of the facade module.

`infra/orchestrator.py` is far over its line budget, and it already delegates
its tick and planning bodies to `control/` helpers. The pause methods belong
here for the same reason: they hold no logic of their own — each takes the
state lock and hands the decision to `PauseController` — so housing them beside
that owner keeps the pause vocabulary in one neighbourhood.

These take their collaborators explicitly rather than an orchestrator. Control
may not import `infra` (it reaches `execution` and `subprocess` transitively,
breaking two architecture contracts), and naming the two or three things each
function touches is a truer statement of the dependency than the whole facade.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from ..domain.pause_state import PauseActor, PauseReason, PauseTransitionOutcome
from .pause_controller import PauseController

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..events.context import EventContext
    from .orchestrator_deps import OrchestratorDeps


def build_pause_controller(
    *,
    deps: "OrchestratorDeps",
    event_context: "EventContext",
    state: "OrchestratorState",
) -> PauseController:
    """The single owner of pause/resume transitions.

    The journal arrives through ``deps`` — this selects no concrete adapter;
    see ``InfraServices.pause_journal``.
    """
    return PauseController(
        events=deps.events,
        event_context=event_context,
        store=state,
        journal=deps.services.pause_journal,
    )


def pause(
    controller: PauseController,
    state_lock: threading.RLock,
    *,
    reason: PauseReason,
    actor: PauseActor,
    detail: str = "",
) -> PauseTransitionOutcome:
    """Pause the engine. Every caller must say why and on whose behalf.

    ``reason``/``actor`` are required with no defaults: a default would let a
    call site silently invent provenance. Returns what was committed.
    """
    with state_lock:
        return controller.pause(reason=reason, actor=actor, detail=detail)


def resume(
    controller: PauseController,
    state_lock: threading.RLock,
    *,
    actor: PauseActor,
    detail: str = "",
) -> PauseTransitionOutcome:
    """Resume the engine, recording who lifted it and what it was paused for."""
    with state_lock:
        return controller.resume(actor=actor, detail=detail)


def set_start_paused(
    controller: PauseController,
    state_lock: threading.RLock,
    state: "OrchestratorState",
    *,
    actor: PauseActor,
) -> None:
    """Set initial paused state and request dashboard read-model hydration.

    Runtime ``pause()`` only stops future execution. Startup-pause also needs
    one read-only refresh, because warm cache state may be stale before the
    dashboard first renders.
    """
    with state_lock:
        # Event suppressed here: run_loop publishes the startup pause once the
        # event context is live, so emitting now would double-count it.
        controller.pause(
            reason=PauseReason.STARTUP,
            actor=actor,
            detail="started in paused mode",
            emit_event=False,
        )
        state.queue_refresh_requested = True


def auto_resume_if_due(
    controller: PauseController, state_lock: threading.RLock
) -> None:
    """Lift a half-open incident pause whose backoff expired.

    Only incident pauses expire; see ``_AUTO_RESUME_BACKOFF_SECONDS``.
    """
    with state_lock:
        controller.resume_if_due(
            actor=PauseActor.SYSTEM,
            detail="auto-resume: incident backoff elapsed, retrying",
        )
