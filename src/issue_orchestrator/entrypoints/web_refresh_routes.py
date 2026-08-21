"""Dashboard refresh and pause/resume action routes."""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..domain.pause_state import PauseActor, PauseReason, PauseTransitionStatus
from .pause_response import requested_actor, transition_response
from ..control.queue_cache import QueueCache, QueueMutationStatus, clear_issue_refresh, record_issue_refreshes
from ..control.session_history import (
    CLOSED_ISSUE_HISTORY_STATUS_REASON,
    ClosedIssueHistoryMutation,
    SessionHistoryOwner,
)
from ..ports.repository_host import (
    RepositoryHostError,
    repository_host_failure_payload,
    repository_host_failure_status,
)
from .web_session_context import WebOrchestratorDependency

logger = logging.getLogger(__name__)

web_refresh_router = APIRouter()


@web_refresh_router.post("/api/pause")
async def pause(
    request: Request, orchestrator: WebOrchestratorDependency
) -> JSONResponse:
    """Pause the orchestrator."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    # This router serves /api/pause on the engine port, ahead of control_app,
    # so it is the one that must honour a declared actor (see requested_actor).
    actor = await requested_actor(request, PauseActor.WEB_API)
    outcome = orchestrator.pause(reason=PauseReason.OPERATOR, actor=actor)
    return transition_response(PauseTransitionStatus.PAUSED, outcome)


@web_refresh_router.post("/api/resume")
async def resume(
    request: Request, orchestrator: WebOrchestratorDependency
) -> JSONResponse:
    """Resume the orchestrator."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    actor = await requested_actor(request, PauseActor.WEB_API)
    outcome = orchestrator.resume(actor=actor)
    return transition_response(PauseTransitionStatus.RESUMED, outcome)


@web_refresh_router.post("/api/refresh")
async def refresh(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Request an immediate refresh of issues from GitHub.

    This triggers the orchestrator to fetch issues on the next loop iteration,
    bypassing the fetch-layer network sync interval. Also resets the timer for
    the next scheduled refresh.

    Optional JSON body:
        inflight_stable_ids: list[str] - Issue IDs that tests expect to discover.
            If provided and these issues are not found after a cached refresh,
            the orchestrator will retry without cache to handle GitHub's
            eventual consistency.
    """
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    # Parse optional inflight_stable_ids from request body
    inflight_stable_ids: set[str] = set()
    try:
        body = await request.body()
        if body:
            data = json.loads(body)
            if isinstance(data, dict) and "inflight_stable_ids" in data:
                ids = data["inflight_stable_ids"]
                if isinstance(ids, list):
                    inflight_stable_ids = set(str(i) for i in ids)
    except (json.JSONDecodeError, ValueError):
        pass  # Ignore malformed body, proceed with empty set

    orchestrator.request_refresh(inflight_stable_ids=inflight_stable_ids)
    return JSONResponse({
        "status": "refresh_requested",
        "refresh": {
            "requested": True,
            "in_progress": bool(orchestrator.state.queue_refresh_in_progress),
        },
    })


@web_refresh_router.post("/api/refresh/visibility")
async def update_refresh_visibility(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Store issue visibility hints from Flow UI for visibility-aware refresh.

    JSON body:
        issues: list[int] - Issue numbers currently visible to the user.
    """
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    raw_issues = body.get("issues", [])
    if not isinstance(raw_issues, list):
        return JSONResponse({"error": "issues must be a list"}, status_code=400)

    visible_numbers: list[int] = []
    for value in raw_issues:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            visible_numbers.append(number)

    state = orchestrator.state
    state.ui_visible_issue_numbers = sorted(set(visible_numbers))
    state.ui_visible_updated_at = time.time()
    return JSONResponse({"status": "ok", "count": len(state.ui_visible_issue_numbers)})


@web_refresh_router.post("/api/issues/{issue_number}/refresh")
async def refresh_issue(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Refresh a single issue from GitHub and update cached queue state."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        issue = orchestrator.repository_host.get_issue(issue_number)
    except RepositoryHostError as exc:
        return JSONResponse(
            repository_host_failure_payload(
                exc,
                message=f"Failed to refresh issue #{issue_number} from GitHub",
            ),
            status_code=repository_host_failure_status(exc),
        )
    if issue is None:
        return JSONResponse({"error": f"Issue #{issue_number} not found"}, status_code=404)

    state = orchestrator.state
    config = orchestrator.config
    queue_cache = QueueCache(config, state, orchestrator.deps.queue_cache_store)
    outcome = queue_cache.upsert_refreshed_issue(issue)
    refreshed_at = time.time()
    if outcome.status == QueueMutationStatus.ACCEPTED:
        record_issue_refreshes(state, {issue_number}, refreshed_at)
    else:
        clear_issue_refresh(state, issue_number)
    history_reconciled = False
    if issue.state.lower() == "closed":
        logger.info(
            "Web refresh: issue=#%d is closed on GitHub; attempting history reconciliation",
            issue_number,
        )
        result = SessionHistoryOwner(state.session_history).reconcile_closed_issue(
            issue_number=issue_number,
            status_reason=CLOSED_ISSUE_HISTORY_STATUS_REASON,
        )
        history_reconciled = isinstance(result, ClosedIssueHistoryMutation)
        logger.info(
            "Web refresh: issue=#%d reconcile result=%s history_reconciled=%s",
            issue_number,
            type(result).__name__,
            history_reconciled,
        )
    else:
        logger.debug(
            "Web refresh: issue=#%d state=%r — no closed-issue reconciliation",
            issue_number,
            issue.state,
        )
    queue_cache.prune_refresh_timestamps()
    queue_cache.save_snapshot()

    return JSONResponse({
        "status": "refreshed" if outcome.status == QueueMutationStatus.ACCEPTED else outcome.status.value,
        "issue_number": issue_number,
        "updated": outcome.updated,
        "in_scope": outcome.in_queue,
        "last_refreshed_label": "just now",
        "last_refreshed_age_seconds": 0,
        "is_stale": False,
        "stale_reason": "",
        "history_reconciled": history_reconciled,
    })
