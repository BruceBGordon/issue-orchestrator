"""Retention-aware Timeline routes for the standalone Control Center."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import (
    IssueDetailPayload,
    TerminalRecordingPayload,
    TimelineEvidencePinRequestPayload,
    TimelineEvidenceStatePayload,
)
from ..domain.timeline_evidence import (
    SetTimelineEvidencePinCommand,
    TimelineEvidenceIdentity,
)
from ..timeline import TimelineStream
from ..view_models.issue_detail import build_issue_detail_view_model
from ..view_models.timeline_evidence_presentation import (
    TimelineEventBatch,
    attach_timeline_evidence,
    scope_timeline_actions_to_repository,
    timeline_evidence_public_state,
)
from ..view_models.timeline_view import normalize_timeline_view
from .control_api_timeline_support import ControlApiTimelineDependency
from .timeline_presentation import (
    _build_phase_toc,
    _build_timeline_cycles,
    _decorate_timeline_events,
    _filter_timeline_events,
)
from .timeline_projection_boundary import timeline_projection_endpoint

control_timeline_router = APIRouter()


@control_timeline_router.get(
    "/api/session/terminal-recording/{issue_number}",
    response_model=TerminalRecordingPayload,
)
async def control_terminal_recording(
    issue_number: int,
    deps: ControlApiTimelineDependency,
    repo_root: str = Query(...),
    run_dir: str = Query(...),
    offset: int = 0,
    limit: int = 200,
    round_index: int | None = None,
    session_role: str | None = None,
) -> JSONResponse:
    """Serve one exact recording only while repository retention allows it."""
    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)
    state = deps.timeline_evidence.describe(
        validated_root,
        TimelineEvidenceIdentity(issue_number, Path(run_dir)),
    )
    if not state.available:
        return JSONResponse(
            {
                "error": state.label,
                "detail": state.help_text,
                "run_dir": str(state.identity.run_dir),
            },
            status_code=410,
        )

    from .web_session_routes import serve_terminal_recording

    return serve_terminal_recording(
        issue_number,
        run_dir,
        offset,
        limit,
        round_index,
        session_role,
    )


@control_timeline_router.put(
    "/api/issues/{issue_number}/timeline-evidence/pin",
    response_model=TimelineEvidenceStatePayload,
)
async def control_set_timeline_evidence_pin(
    issue_number: int,
    payload: TimelineEvidencePinRequestPayload,
    deps: ControlApiTimelineDependency,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Pin or unpin an exact repository-scoped Timeline run."""
    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)
    command = SetTimelineEvidencePinCommand(
        identity=TimelineEvidenceIdentity(issue_number, Path(payload.run_dir)),
        pinned=payload.pinned,
    )
    try:
        state = deps.timeline_evidence.set_pinned(validated_root, command)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(timeline_evidence_public_state(state))


@control_timeline_router.get(
    "/api/issue-detail/{issue_number}",
    response_model=IssueDetailPayload,
)
@timeline_projection_endpoint("control_issue_detail")
async def control_issue_detail(
    issue_number: int,
    deps: ControlApiTimelineDependency,
    repo_root: str = Query(...),
    view: str = Query("user"),
) -> JSONResponse:
    """Render a repository-scoped issue Timeline with retention state."""
    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    with deps.timeline_evidence.open_issue(validated_root, issue_number) as issue:
        if issue is None:
            return JSONResponse(
                {
                    "error": "not_found",
                    "detail": f"No timeline events for issue {issue_number}",
                },
                status_code=404,
            )

        stream = TimelineStream.from_records(issue_number, list(issue.records))
        raw_events = [event.to_dict() for event in stream.events]
        filtered_events = _filter_timeline_events(raw_events)
        events = _decorate_timeline_events(filtered_events, issue_number)
        events = attach_timeline_evidence(
            TimelineEventBatch(events),
            issue_number,
            issue.evidence,
        )
        events = scope_timeline_actions_to_repository(
            events,
            validated_root,
        ).events
        payload = build_issue_detail_view_model(
            issue_number=issue_number,
            title=f"Issue #{issue_number}",
            issue_url="",
            events=events,
            phase_toc=_build_phase_toc(events),
            cycles=_build_timeline_cycles(events),
            context=None,
            view=normalize_timeline_view(view),
            raw_events=raw_events,
        )
    return JSONResponse(payload)


__all__ = ["control_timeline_router"]
