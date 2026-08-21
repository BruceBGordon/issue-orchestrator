"""The one shape a pause/resume route replies with.

Both the control API and the web dashboard expose pause/resume, and both must
answer with what the OWNER committed rather than with what the caller asked
for — transitions are idempotent, so "the request succeeded" and "the request
changed something" are different facts. Keeping the builder here gives the two
routers one answer shape and one place where the lifecycle vocabulary is typed.
"""

from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import JSONResponse

from ..domain.pause_state import (
    PauseActor,
    PauseTransitionOutcome,
    PauseTransitionStatus,
)


def transition_response(
    status: PauseTransitionStatus, outcome: PauseTransitionOutcome
) -> JSONResponse:
    """Reply with the committed transition, not the requested one.

    ``committed`` distinguishes "I paused it" from "it was already paused";
    ``actor``/``reason`` report what is actually on record, which for a rejected
    duplicate is the ORIGINAL pauser rather than this caller.
    """
    return JSONResponse(
        {
            "status": str(status),
            "committed": outcome.committed,
            "requested_actor": str(outcome.requested_actor),
            "actor": (
                str(outcome.recorded_actor)
                if outcome.recorded_actor is not None
                else None
            ),
            "reason": (
                str(outcome.recorded_reason)
                if outcome.recorded_reason is not None
                else None
            ),
        }
    )


async def requested_actor(request: Request, default: PauseActor) -> PauseActor:
    """Read the caller's self-declared actor from an optional JSON body.

    EVERY router that serves ``/api/pause`` must honour this. The Control
    Center and MCP both post to the repository engine's port, and the engine
    app registers ``web_refresh_router`` long before it mounts ``control_app``
    — so the dashboard router wins the path, and an actor honoured only by the
    control router is silently discarded. That is how a "fix" for Control
    Center attribution can journal every pause as ``web_api`` and still pass
    a client-side test.

    An unknown or absent value falls back to ``default`` rather than failing
    the request: the transition matters more than its label.
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
