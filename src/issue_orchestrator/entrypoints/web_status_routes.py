"""Dashboard status and auxiliary read routes."""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..infra.audit import SkipReason, audit_queue
from ..infra.e2e_slot_policy import get_e2e_role
from ..view_models.dashboard import flow_steps_for, issue_url_for
from ..view_models.issue_card_labels import blocked_summary
from .web_session_context import WebOrchestratorDependency

web_status_router = APIRouter()


@web_status_router.get("/api/status")
async def get_status(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get current orchestrator status as JSON."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = orchestrator.state
    config = orchestrator.config

    sessions = []
    for session in state.active_sessions:
        sessions.append({
            "issue_number": session.issue.number,
            "title": session.issue.title,
            "runtime_minutes": session.runtime_minutes,
            "agent_type": session.issue.agent_type,
            "status": "running" if session.runtime_minutes < session.agent_config.timeout_minutes else "slow",
            "branch": session.branch_name,
            # Control Center worktree audits consume this server-to-server as
            # authoritative activity evidence. Older engines omit it, which
            # deliberately makes the audit fail closed and retain disposables.
            "worktree_path": str(session.worktree_path),
        })

    # Serialize pending reviews
    pending_reviews = []
    for review in state.pending_reviews:
        pending_reviews.append({
            "issue_number": review.issue_number,
            "pr_number": review.pr_number,
            "pr_url": review.pr_url,
            "branch_name": review.branch_name,
        })

    tick_id = orchestrator.event_context.tick_id
    if not isinstance(tick_id, (int, float)):
        tick_id = None
    last_tick_time = orchestrator.last_tick_time
    if not isinstance(last_tick_time, (int, float)):
        last_tick_time = None

    # Determine E2E role for this instance
    instance_id = os.environ.get("INSTANCE_ID")
    e2e_role = get_e2e_role(config.e2e, instance_id=instance_id)

    return JSONResponse({
        "paused": state.paused,
        "shutdown_requested": orchestrator.shutdown_requested,
        # Exposed so the Control Center's server-to-server probe (in
        # control_center_repo_status._apply_internal_runtime_state) can
        # see whether the engine has finished its initial reconcile,
        # and the CC frontend can keep the per-repo "Open dashboard"
        # button disabled until the dashboard would render a settled
        # view rather than a procession of SSE-driven updates.
        "startup_status": state.startup_status,
        "active_sessions": sessions,
        "max_sessions": config.max_concurrent_sessions,
        "completed_today": state.completed_today,
        "queue": state.priority_queue,
        "pending_reviews": pending_reviews,
        "tick_id": tick_id,
        "last_tick_time": last_tick_time,
        "e2e_role": e2e_role if config.e2e.enabled else None,
    })


@web_status_router.get("/api/excluded-issues")
async def get_excluded_issues(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get issues known to the system but excluded from scheduling."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = orchestrator.state
    config = orchestrator.config

    displayed_numbers = {
        s.issue.number for s in state.active_sessions
    } | {
        i.number for i in state.cached_queue_issues
    } | {
        e.issue_number for e in state.session_history
    }

    entries = audit_queue(config, state=state, issue_tracker=orchestrator.repository_host)
    excluded: list[dict[str, object]] = []

    for entry in entries:
        if entry.status == SkipReason.QUEUED:
            continue
        if entry.issue.number in displayed_numbers:
            continue

        dep_problem = state.dependency_problems.get(entry.issue.number)
        if dep_problem:
            reason = f"dependency: {dep_problem.summary}"
        else:
            reason = entry.status.value
            if entry.detail:
                reason = f"{reason}: {entry.detail}"

        flow_stage = "not_eligible"
        excluded.append({
            "issue_number": entry.issue.number,
            "title": entry.issue.title,
            "agent_type": (entry.issue.agent_type or "unknown").replace("agent:", ""),
            "issue_url": issue_url_for(config, entry.issue.number),
            "excluded_reason": reason,
            "flow_stage": flow_stage,
            "flow_steps": flow_steps_for(flow_stage),
            "blocked_summary": blocked_summary(
                list(entry.issue.labels),
                orchestrator.deps.label_manager,
                dep_problem.summary if dep_problem else None,
            ),
        })

    return JSONResponse({"excluded": excluded})
