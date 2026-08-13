"""Control Center tool routes.

These endpoints provide utilities accessible from the unified dashboard.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import (
    WorktreeAuditRequestPayload,
    WorktreeAuditResponsePayload,
)
from ..execution.control_center_actions import (
    AuditActionRequest,
    ConfiguredRepoActionRequest,
    TraceActionRequest,
)
from .control_api_tools_support import ControlApiToolsDependency

control_tools_router = APIRouter()


@control_tools_router.get("/control/tools/audit")
async def tools_audit(
    deps: ControlApiToolsDependency,
    repo_root: str = Query(...),
    issue_number: int | None = Query(default=None),
) -> JSONResponse:
    """Audit why issues are queued or blocked.

    Query params:
        repo_root: str - Repository root path
        issue_number: int (optional) - Specific issue to audit

    Returns:
        List of audit entries with issue status and reasons.
    """
    repo_path = deps.validate_repo_root(repo_root)
    if repo_path is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)
    actions = deps.get_control_actions()
    selection = actions.effective_launch_selection(repo_path)
    result = await actions.audit_cmd.execute(
        AuditActionRequest(
            repo_root=repo_path,
            selection=selection,
            issue_number=issue_number,
        ),
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@control_tools_router.get("/control/tools/trace")
async def tools_trace(
    deps: ControlApiToolsDependency,
    repo_root: str = Query(...),
    issue_number: int = Query(...),
    limit: int = Query(default=100),
) -> JSONResponse:
    """Get trace log entries for a specific issue.

    Query params:
        repo_root: str - Repository root path
        issue_number: int - Issue number to trace
        limit: int - Max lines to return (default: 100)

    Returns:
        List of log entries related to the issue.
    """
    repo_path = deps.validate_repo_root(repo_root)
    if repo_path is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)
    actions = deps.get_control_actions()
    result = await actions.trace_cmd.execute(
        TraceActionRequest(repo_root=repo_path, issue_number=issue_number, limit=limit),
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@control_tools_router.post("/control/tools/labels/init")
async def tools_labels_init(
    request: Request,
    deps: ControlApiToolsDependency,
) -> JSONResponse:
    """Initialize or refresh GitHub labels for a repository.

    JSON body:
        repo_root: str - Repository root path

    Returns:
        Summary of created/updated labels.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_path = deps.validate_repo_root(body.get("repo_root"))
    if repo_path is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    actions = deps.get_control_actions()
    result = await actions.labels_cmd.execute(
        ConfiguredRepoActionRequest(
            repo_root=repo_path,
            selection=actions.effective_launch_selection(repo_path),
        )
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@control_tools_router.post(
    "/control/tools/worktrees/cleanup",
    response_model=WorktreeAuditResponsePayload,
)
async def tools_worktrees_cleanup(
    body: WorktreeAuditRequestPayload,
    deps: ControlApiToolsDependency,
) -> WorktreeAuditResponsePayload | JSONResponse:
    """Audit registered worktrees (read-only, no deletion).

    The response uses the same ownership and safety policy as startup cleanup.
    It never deletes worktrees and does not recommend ``git worktree prune``,
    which only removes stale registrations rather than live worktree paths.

    JSON body:
        repo_root: str - Repository root path

    Returns:
        Classified registered worktrees and safe cleanup candidates.
    """
    repo_path = deps.validate_repo_root(body.repo_root)
    if repo_path is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    actions = deps.get_control_actions()
    result = await actions.stale_worktrees_cmd.execute(
        ConfiguredRepoActionRequest(
            repo_root=repo_path,
            selection=actions.effective_launch_selection(repo_path),
        ),
    )
    if result.status_code != 200:
        return JSONResponse(result.payload, status_code=result.status_code)
    return WorktreeAuditResponsePayload.model_validate(result.payload)
