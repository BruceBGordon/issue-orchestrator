"""Control-API pause and resume routes.

Split out of ``control_api.py`` the way the other ``control_api_*_routes``
modules are: that file is far over its line budget, and pause now carries real
behaviour (actor resolution, typed status, provenance in the status payload)
rather than a one-line delegation.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..domain.pause_state import PauseActor, PauseReason, PauseTransitionStatus
from .pause_response import transition_response

control_pause_router = APIRouter()


async def requested_actor(request: Request, default: PauseActor) -> PauseActor:
    """Read the caller's self-declared actor from an optional JSON body.

    Every remote surface (MCP, the Control Center) reaches the engine through
    the same HTTP route, so without a declared actor they would all be recorded
    identically and the pause journal could not tell them apart. An unknown or
    absent value falls back to ``default`` rather than failing the request — the
    transition matters more than its label.
    """
    try:
        body = await request.body()
        if not body:
            return default
        data = json.loads(body)
        if not isinstance(data, dict):
            return default
        return PauseActor(str(data.get("actor", "")))
    except (json.JSONDecodeError, ValueError):
        return default


@control_pause_router.post("/api/pause")
async def pause(request: Request) -> JSONResponse:
    """Pause the orchestrator - stop launching new sessions."""
    # Imported lazily: control_api includes this router, so a module-level
    # import would close the cycle.
    from .control_api import get_orchestrator

    orchestrator = get_orchestrator()
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    actor = await requested_actor(request, PauseActor.CONTROL_API)
    outcome = orchestrator.pause(reason=PauseReason.OPERATOR, actor=actor)
    return transition_response(PauseTransitionStatus.PAUSED, outcome)


@control_pause_router.post("/api/resume")
async def resume(request: Request) -> JSONResponse:
    """Resume the orchestrator - allow launching new sessions."""
    from .control_api import get_orchestrator

    orchestrator = get_orchestrator()
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    actor = await requested_actor(request, PauseActor.CONTROL_API)
    outcome = orchestrator.resume(actor=actor)
    return transition_response(PauseTransitionStatus.RESUMED, outcome)
