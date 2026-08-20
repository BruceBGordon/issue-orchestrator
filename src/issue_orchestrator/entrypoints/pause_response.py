"""The one shape a pause/resume route replies with.

Both the control API and the web dashboard expose pause/resume, and both must
answer with what the OWNER committed rather than with what the caller asked
for — transitions are idempotent, so "the request succeeded" and "the request
changed something" are different facts. Keeping the builder here gives the two
routers one answer shape and one place where the lifecycle vocabulary is typed.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from ..domain.pause_state import PauseTransitionOutcome, PauseTransitionStatus


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
