"""Dashboard session log routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..execution.manifest_accessor import ArtifactNotFoundError
from ..infra.session_log_prettify import prettify_session_log
from .web_exact_recorded_run import exact_recorded_run_response
from .web_session_context import WebOrchestratorDependency, resolve_issue_session_context
from .web_session_routes import (
    build_ui_log_stream_observation as _build_ui_log_stream_observation,
    preview_lines_from_claude_jsonl as _preview_lines_from_claude_jsonl,
    preview_lines_from_terminal_recording as _preview_lines_from_terminal_recording,
)

web_log_router = APIRouter()


@web_log_router.get("/api/log/{issue_number}")
async def get_session_log(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:  # noqa: C901 - log retrieval with multiple fallback paths
    """Get Claude session log for an issue."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    context = resolve_issue_session_context(orchestrator, issue_number)
    worktree_path = context.worktree_path

    if not worktree_path:
        return JSONResponse({
            "error": f"No worktree path found for issue #{issue_number}",
            "hint": "Session may have been cleaned up or never started",
        }, status_code=404)

    path_str = str(worktree_path)
    escaped_path = path_str.replace("/", "-")
    if not escaped_path.startswith("-"):
        escaped_path = "-" + escaped_path

    claude_projects = Path.home() / ".claude" / "projects" / escaped_path
    if not claude_projects.exists():
        return JSONResponse({
            "error": "Claude project directory not found",
            "path": str(claude_projects),
            "hint": "Session may not have been started yet",
        }, status_code=404)

    jsonl_files = sorted(claude_projects.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not jsonl_files:
        return JSONResponse({
            "error": "No session logs found",
            "path": str(claude_projects),
        }, status_code=404)

    latest_log = jsonl_files[0]

    try:
        lines = latest_log.read_text().strip().split("\n")
        total_lines = len(lines)

        if total_lines > 100:
            lines = lines[-100:]
            truncated = True
        else:
            truncated = False

        return JSONResponse({
            "issue_number": issue_number,
            "log_path": str(latest_log),
            "total_lines": total_lines,
            "truncated": truncated,
            "lines": lines,
        })
    except Exception as e:
        return JSONResponse({"error": f"Failed to read log: {e}"}, status_code=500)


@web_log_router.get("/api/log/local/{issue_number}")
async def get_agent_ui_log(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    offset: int = 0,
    limit: int = 200,
    run_dir: str | None = None,
) -> JSONResponse:  # noqa: C901, PLR0912 - log parsing with format detection and streaming
    """Get the local agent UI log for an issue."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    if not run_dir:
        return JSONResponse({
            "error": "run_dir is required",
            "hint": "Open logs from a run-scoped timeline action.",
        }, status_code=400)

    exact_run = exact_recorded_run_response(run_dir, issue_number=issue_number)
    if isinstance(exact_run, JSONResponse):
        return exact_run
    run_identity = exact_run.artifacts.run_identity
    accessor = exact_run.artifacts
    stream_observation = _build_ui_log_stream_observation(run_identity.run_dir, resolved_log_path=None)
    try:
        artifact = accessor.get_agent_log(allow_empty=True)
    except ArtifactNotFoundError as e:
        return JSONResponse({
            "error": "No agent log found",
            "hint": "Session may not have started or its run-scoped log was not attached",
            "diagnostic": {
                "run_dir": str(run_identity.run_dir),
                "detail": str(e),
            },
            "stream_observation": stream_observation,
        }, status_code=404)
    log_path = artifact.path
    stream_observation = _build_ui_log_stream_observation(run_identity.run_dir, resolved_log_path=log_path)

    try:
        if artifact.descriptor.content_type == "application/x-ndjson":
            all_lines = _preview_lines_from_terminal_recording(log_path)
        else:
            all_lines = _preview_lines_from_claude_jsonl(log_path)

        # Dispatch to the per-provider prettifier so Codex / Claude / raw PTY
        # all reach the UI as clean readable text rather than envelope JSON.
        # The prettifier owns blank-line handling (it keeps section breaks
        # between codex items) — don't filter those out here.
        all_lines = prettify_session_log(all_lines)
        total_lines = len(all_lines)

        if offset > 0:
            lines = all_lines[offset:]
        else:
            lines = all_lines

        truncated = False
        if limit > 0 and len(lines) > limit:
            if offset == 0:
                lines = lines[-limit:]
                truncated = True
            else:
                lines = lines[:limit]

        return JSONResponse({
            "issue_number": issue_number,
            "log_path": str(log_path),
            "total_lines": total_lines,
            "offset": offset,
            "truncated": truncated,
            "lines": lines,
            "stream_observation": stream_observation,
        })
    except Exception as e:
        return JSONResponse({"error": f"Failed to read log: {e}"}, status_code=500)
