"""The orchestrator facade's pause surface, kept out of the facade module.

`infra/orchestrator.py` is far over its line budget, and it already delegates
its tick and planning bodies to `control/` helpers that take the orchestrator as
their first argument. The pause methods follow the same shape: they hold no
logic of their own, they take the state lock and hand the decision to
`PauseController`. Housing them here keeps that pattern consistent and keeps the
pause vocabulary in one neighbourhood with its owner.

Every function takes the state lock, because the controller serializes its own
transitions but the facade's callers also read `state` around them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.pause_state import PauseActor, PauseReason, PauseTransitionOutcome
from .pause_controller import PauseController

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator


def build_pause_controller(orch: "Orchestrator") -> PauseController:
    """The single owner of pause/resume transitions.

    The journal arrives through ``deps`` — this selects no concrete adapter;
    see ``InfraServices.pause_journal``.
    """
    return PauseController(
        events=orch.deps.events,
        event_context=orch._event_context,
        store=orch.state,
        journal=orch.deps.services.pause_journal,
    )


def pause(
    orch: "Orchestrator",
    *,
    reason: PauseReason,
    actor: PauseActor,
    detail: str = "",
) -> PauseTransitionOutcome:
    """Pause the engine. Every caller must say why and on whose behalf.

    ``reason``/``actor`` are required with no defaults: a default would let a
    call site silently invent provenance. Returns what was committed.
    """
    with orch.state_lock:
        return orch.pause_controller.pause(reason=reason, actor=actor, detail=detail)


def resume(
    orch: "Orchestrator", *, actor: PauseActor, detail: str = ""
) -> PauseTransitionOutcome:
    """Resume the engine, recording who lifted it and what it was paused for."""
    with orch.state_lock:
        return orch.pause_controller.resume(actor=actor, detail=detail)


def set_start_paused(orch: "Orchestrator", *, actor: PauseActor) -> None:
    """Set initial paused state and request dashboard read-model hydration.

    Runtime ``pause()`` only stops future execution. Startup-pause also needs
    one read-only refresh, because warm cache state may be stale before the
    dashboard first renders.
    """
    with orch.state_lock:
        # Event suppressed here: run_loop publishes the startup pause once the
        # event context is live, so emitting now would double-count it.
        orch.pause_controller.pause(
            reason=PauseReason.STARTUP,
            actor=actor,
            detail="started in paused mode",
            emit_event=False,
        )
        orch.state.queue_refresh_requested = True


def auto_resume_if_due(orch: "Orchestrator") -> None:
    """Lift a half-open incident pause whose backoff expired.

    Only incident pauses expire; see ``_AUTO_RESUME_BACKOFF_SECONDS``.
    """
    with orch.state_lock:
        orch.pause_controller.resume_if_due(
            actor=PauseActor.SYSTEM,
            detail="auto-resume: incident backoff elapsed, retrying",
        )
