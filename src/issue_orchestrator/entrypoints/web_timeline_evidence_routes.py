"""Typed HTTP commands for exact-run Timeline evidence retention."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import (
    TimelineEvidencePinRequestPayload,
    TimelineEvidenceStatePayload,
)
from ..domain.timeline_evidence import (
    SetTimelineEvidencePinCommand,
    TimelineEvidenceIdentity,
)
from ..view_models.timeline_evidence_presentation import (
    timeline_evidence_public_state,
)
from .web_session_context import WebOrchestratorDependency

web_timeline_evidence_router = APIRouter()


@web_timeline_evidence_router.put(
    "/api/issues/{issue_number}/timeline-evidence/pin",
    response_model=TimelineEvidenceStatePayload,
)
async def set_timeline_evidence_pin(
    issue_number: int,
    payload: TimelineEvidencePinRequestPayload,
    orchestrator: WebOrchestratorDependency,
) -> TimelineEvidenceStatePayload | JSONResponse:
    """Set retention pin state for one exact Timeline run."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    command = SetTimelineEvidencePinCommand(
        identity=TimelineEvidenceIdentity(issue_number, Path(payload.run_dir)),
        pinned=payload.pinned,
    )
    try:
        state = orchestrator.deps.timeline_evidence.set_pinned(command)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return TimelineEvidenceStatePayload.model_validate(
        timeline_evidence_public_state(state)
    )
