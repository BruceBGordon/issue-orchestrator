"""Lightweight control API for the orchestrator.

This API is always available regardless of UI mode, providing programmatic
control over the orchestrator. The web dashboard (in web mode) adds additional
routes on top of this.

Control API endpoints (in-process):
- POST /api/refresh - Trigger immediate issue refresh
- POST /api/pause - Pause orchestrator
- POST /api/resume - Resume orchestrator
- GET /api/status - Get orchestrator status
- GET /api/events - Stream structured events (SSE)
- GET /api/events_since - Fetch buffered events since an event id
- POST /api/gh_audit_report - Emit GH audit report to disk
- GET /api/snapshot - Fetch snapshot for test resync
- POST /api/issues/{issue_number}/resume - Resume processing for a debug session
- POST /api/issues/{issue_number}/debug-session - Launch interactive debug session

Supervisor Control API endpoints (process management):
- POST /control/orchestrator/start - Start orchestrator for a repo
- POST /control/orchestrator/stop - Stop orchestrator for a repo
- POST /control/orchestrator/pause - Pause orchestrator (passthrough)
- POST /control/orchestrator/resume - Resume orchestrator (passthrough)
- POST /control/orchestrator/refresh - Trigger refresh (passthrough)
- GET /control/orchestrator/status - Get orchestrator process status
- GET /control/orchestrator/last_failure - Get last startup failure
- GET /control/orchestrator/log_tail - Get recent log lines

Multi-repo Registry API endpoints:
- GET /control/repos - List all registered repos with status
- POST /control/repos - Add a repo to the registry
- DELETE /control/repos - Remove a repo from the registry

E2E Test Runner API endpoints:
- POST /control/e2e/start - Start E2E test run
- POST /control/e2e/stop - Stop running E2E test
- GET /control/e2e/status - Get E2E runner status
- GET /control/e2e/runs - List recent E2E runs
- GET /control/e2e/run/{run_id} - Get run details with test results
- GET /control/e2e/run/{run_id}/timeline - Get timeline events for shared rendering
- GET /control/e2e/logs/{run_id} - Get run logs
- GET /control/e2e/failed/{run_id} - Get failed tests from a run
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from ..infra import gh_audit
from ..execution.repository_engine_supervisor import build_default_supervisor_ops
from ..ports.repository_engine_supervisor import SupervisorOps
from ..ports import RepositoryHost
from ..control.goal_pilot import GoalPilot
from ..execution.control_center_actions import ControlCenterActions
from ..execution.repository_setup_validation import (
    RepositorySetupValidationDetectorAdapter,
)
from ._auth_middleware import (
    AuthSurfaceConfig,
    evaluate_request,
    handle_login_post,
    is_agent_callback_route,
    issue_sse_token_response,
    resolve_browser_page_auth,
)
from .brand_assets import read_logo_svg
from .bootstrap_repository_setup import build_repository_setup_owner
from .control_api_goal_pilot_routes import control_goal_pilot_router
from .control_api_goal_pilot_support import (
    ControlApiGoalPilotDependencies,
    install_control_api_goal_pilot_dependencies,
)
from .control_api_e2e_runs import control_e2e_runs_router
from .timeline_projection_boundary import timeline_projection_endpoint
from .control_api_orchestrator_routes import control_orchestrator_router
from .control_api_orchestrator_support import (
    ControlApiOrchestratorDependencies,
    install_control_api_orchestrator_dependencies,
)
from .control_api_e2e_support import (
    ControlApiE2EDependencies,
    install_control_api_e2e_dependencies,
)
from .control_api_issue_routes import control_issue_router
from .control_api_issue_support import (
    ControlApiIssueDependencies,
    install_control_api_issue_dependencies,
)
from .control_api_repo_routes import control_repo_router
from .control_api_repo_support import (
    ControlApiRepoDependencies,
    install_control_api_repo_dependencies,
)
from .control_api_setup_routes import control_setup_router
from .control_api_setup_support import (
    ControlApiSetupDependencies,
    install_control_api_setup_dependencies,
)
from .control_api_shutdown_routes import control_shutdown_router
from .control_api_shutdown_state import (
    begin_engine_shutdown_operation,
    coerce_graceful_timeout_seconds,
    finish_engine_shutdown_operation,
    global_shutdown_in_progress,
)
from .shutdown_reason_support import parse_shutdown_reason
from .control_api_shutdown_support import (
    ControlApiShutdownDependencies,
    install_control_api_shutdown_dependencies,
)
from .control_api_tools_routes import control_tools_router
from .control_api_tools_support import (
    ControlApiToolsDependencies,
    install_control_api_tools_dependencies,
)
from .control_api_e2e_triage import control_e2e_triage_router
from .timeline_presentation import (
    _build_phase_toc,
    _build_timeline_cycles,
    _decorate_timeline_events,
    _filter_timeline_events,
)

# Path to templates
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

if TYPE_CHECKING:
    from ..domain.repository_launch_selection import RepositoryLaunchSelection
    from ..domain.repository_setup_auth import RepositorySetupGitHubAuthorization
    from ..infra.orchestrator import Orchestrator
    from ..infra.config import Config
    from ..ports.repository_setup import RepositorySetupGitHubVerification

logger = logging.getLogger(__name__)
_PREFERRED_REPO_ROOT_ENV = "ISSUE_ORCHESTRATOR_CC_REPO_ROOT"


def _load_config_selection(
    repo_root: Path,
    selection: "RepositoryLaunchSelection",
) -> "Config":
    """Load exactly one typed E2E mode/config selection.

    Raises FileNotFoundError if the config file does not exist.
    """
    from ..execution.control_center_runtime import load_config_for_selection

    return load_config_for_selection(repo_root, selection)


def _create_repository_setup_host(
    repo_name: str,
    authorization: "RepositorySetupGitHubAuthorization",
) -> RepositoryHost:
    """Composition-root adapter for setup label mutations."""
    from ..execution.providers import create_repository_setup_host

    return create_repository_setup_host(repo_name, authorization)


def _verify_repository_setup_github_authorization(
    repo_name: str,
    authorization: "RepositorySetupGitHubAuthorization",
) -> "RepositorySetupGitHubVerification":
    """Composition-root adapter for setup GitHub verification."""
    from ..execution.providers import verify_repository_setup_github_authorization

    return verify_repository_setup_github_authorization(repo_name, authorization)


def _store_repository_setup_github_token(
    authorization: "RepositorySetupGitHubAuthorization",
    *,
    repo: str,
) -> "RepositorySetupGitHubAuthorization":
    """Composition-root adapter for repo-scoped keychain storage."""
    from ..execution.providers import store_repository_setup_github_token

    return store_repository_setup_github_token(authorization, repo=repo)


# Create minimal control API app
control_app = FastAPI(title="Issue Orchestrator Control API")
STATIC_DIR = Path(__file__).parent.parent / "static"
if STATIC_DIR.exists():
    control_app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Bearer-token enforcement (security issue #5987, F3 + #6017 review).
#
# Two tokens gate the Control API:
#
# - ``_admin_token`` authorizes every route. Held by the orchestrator,
#   the operator CLI, the Control Center, and MCP clients driven by the
#   operator.
# - ``_agent_callback_token`` authorizes an allowlist of routes only
#   (see ``_auth_middleware.is_agent_callback_route``). Issued to agent
#   subprocesses so they can call preflight-push / exchange-respond /
#   issue-resume without holding the admin credential (#6017 P2 review).
#
# Both are ``None`` by default so unit tests using ``TestClient`` keep
# working. Every production entrypoint that serves these routes must
# call ``configure_api_token`` to turn enforcement on:
# ``ControlAPIServer.start``, ``control_center.main``, and
# ``EngineStartup.configure_auth`` — the last of these serves
# ``control_app`` mounted under the dashboard app, and omitting it
# left the engine with no callback token at all (#6924).
_admin_token: str | None = None
_agent_callback_token: str | None = None

# Paths that must remain accessible without any authentication —
# browser chrome, static assets, the login form, and favicon. The
# landing HTML (``/``) is NOT in this set: without a valid session
# cookie it renders the login page, not the dashboard, and neither
# path issues a usable credential until ``POST /login`` verifies the
# admin bearer token. See security #6017 re-review-2 P1 — earlier
# versions minted a session cookie for any anonymous GET of ``/``,
# which defeated bearer-token auth entirely.
_UNAUTHENTICATED_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/login",
        "/favicon.ico",
    }
)
_UNAUTHENTICATED_PREFIXES: tuple[str, ...] = ("/static/",)

# The agent-callback route allowlist lives in ``_auth_middleware`` so
# every surface serving these routes answers identically — see that
# module's docstring for why a per-surface copy was a defect (#6913).
_CONTROL_API_SURFACE = AuthSurfaceConfig(
    sse_path="/api/events",
    public_paths=_UNAUTHENTICATED_PATHS,
    name="control_api",
    public_prefixes=_UNAUTHENTICATED_PREFIXES,
    agent_callback_matcher=is_agent_callback_route,
)


@control_app.middleware("http")
async def _require_api_token_middleware(  # pyright: ignore[reportUnusedFunction]
    request: Request, call_next: Any
) -> Response:
    """Enforce Control API auth via the shared three-path gate.

    See ``_auth_middleware.evaluate_request`` for the bearer /
    session-cookie / SSE-token logic; this wrapper just binds the
    module-level admin + agent-callback tokens.
    """
    gate_response = evaluate_request(
        request, _admin_token, _agent_callback_token, _CONTROL_API_SURFACE
    )
    if gate_response is not None:
        return gate_response
    return await call_next(request)


def configure_api_token(
    admin: str | None,
    *,
    agent_callback: str | None = None,
) -> None:
    """Enable (or disable) bearer-token enforcement on the Control API.

    ``admin`` — required for anything other than the agent-callback
    allowlist. Pass ``None`` to disable enforcement entirely (test
    default).

    ``agent_callback`` — optional scoped token; when set, carrying
    ``Authorization: Bearer <agent_callback>`` is accepted on the
    allowlisted routes.
    """
    global _admin_token, _agent_callback_token
    _admin_token = admin
    _agent_callback_token = agent_callback


def get_configured_api_token() -> str | None:
    """Return the currently configured admin token."""
    return _admin_token


def get_configured_agent_callback_token() -> str | None:
    """Return the currently configured agent-callback token."""
    return _agent_callback_token


# Global reference to orchestrator (set at startup)
_orchestrator: "Orchestrator | None" = None

# Supervisor operations (injectable for testing)
_supervisor: SupervisorOps = build_default_supervisor_ops()
_control_actions = ControlCenterActions(supervisor=_supervisor)


def set_orchestrator(orchestrator: "Orchestrator") -> None:
    """Set the orchestrator instance for the control API."""
    global _orchestrator
    _orchestrator = orchestrator


def get_orchestrator() -> "Orchestrator | None":
    """Get the orchestrator instance."""
    return _orchestrator


def _with_state_lock(fn):
    if _orchestrator is None:
        return fn()
    lock = getattr(_orchestrator, "state_lock", None)
    if lock is None:
        return fn()
    with lock:
        return fn()


def _get_goal_pilot() -> GoalPilot:
    """Create a GoalPilot instance from the running orchestrator."""
    if _orchestrator is None:
        raise RuntimeError("Orchestrator not initialized")
    return GoalPilot(
        store=_orchestrator.deps.goal_pilot_store,
        events=_orchestrator.deps.events,
        action_applier=_orchestrator.deps.action_applier,
        repo_root=str(_orchestrator.config.repo_root),
        ctx=_orchestrator.event_context,
    )


def set_supervisor(supervisor: SupervisorOps) -> None:
    """Set the supervisor operations instance (for testing)."""
    global _supervisor, _control_actions
    _supervisor = supervisor
    _control_actions = ControlCenterActions(supervisor=_supervisor)


def get_supervisor() -> SupervisorOps:
    """Get the supervisor operations instance."""
    return _supervisor


def set_control_actions(actions: ControlCenterActions) -> None:
    """Inject control-center action service (for testing)."""
    global _control_actions
    _control_actions = actions


def get_control_actions() -> ControlCenterActions:
    """Get the control-center action service."""
    return _control_actions


def _preferred_repo_root() -> Path | None:
    """Resolve preferred repo root for this Control Center process."""
    raw = os.environ.get(_PREFERRED_REPO_ROOT_ENV, "").strip()
    if not raw:
        return None
    try:
        root = Path(raw).resolve()
    except (OSError, ValueError):
        return None
    if not root.exists() or not root.is_dir():
        return None
    return root


# Track orchestrator child PIDs for zombie reaping (used by control_center).
# This avoids racing with subprocess.run() for unrelated children.
#
# Only control_start (and the restart path at line ~1139) spawn orchestrators
# as children of the control center process. Other entry points (CLI, MCP server)
# are separate processes that manage their own children independently.
import threading as _threading

_tracked_pids: set[int] = set()
_tracked_pids_lock = _threading.Lock()


def _schedule_control_center_exit(delay_seconds: float = 0.5) -> None:
    """Terminate Control Center process after a short delay."""
    import signal
    import threading

    def delayed_shutdown() -> None:
        time.sleep(delay_seconds)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(2)
        os.kill(os.getpid(), signal.SIGKILL)

    threading.Thread(target=delayed_shutdown, daemon=False).start()


def _schedule_control_center_exit_dependency() -> None:
    """FastAPI dependency hook that keeps tests patchable without inline lambdas."""
    _schedule_control_center_exit()


def track_child_pid(pid: int) -> None:
    """Register an orchestrator child PID for zombie reaping."""
    with _tracked_pids_lock:
        _tracked_pids.add(pid)
        logger.debug("Tracking orchestrator PID %d for reaping", pid)


def untrack_child_pid(pid: int) -> None:
    """Unregister an orchestrator child PID."""
    with _tracked_pids_lock:
        _tracked_pids.discard(pid)


def get_tracked_pids() -> list[int]:
    """Get copy of tracked PIDs for reaping."""
    with _tracked_pids_lock:
        return list(_tracked_pids)


def _track_launched_pids(supervisor_data: Mapping[str, object]) -> None:
    """Register launched orchestrator PIDs for zombie reaping.

    Called by control_start after successfully launching orchestrators.
    """
    # Handle multi-instance launches
    instances = supervisor_data.get("instances")
    if isinstance(instances, list):
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            pid = instance.get("pid")
            if isinstance(pid, int):
                track_child_pid(pid)
    # Handle single-instance launches
    else:
        pid = supervisor_data.get("pid")
        if isinstance(pid, int):
            track_child_pid(pid)


# ======================================================================# Unified Dashboard API Endpoints
# ======================================================================# These endpoints support the unified dashboard entry point.


@control_app.get("/api/state")
async def get_system_state() -> JSONResponse:
    """Get complete system state for the unified dashboard.

    Returns dashboard status, all repos with orchestrator status, and context info.
    This is the primary endpoint for the unified dashboard to understand current state.
    """
    from ..observation.instance_detector import detect_system_state

    state = detect_system_state()
    return JSONResponse(state.to_dict())


@control_app.get("/api/repos")
async def get_repos() -> JSONResponse:
    """List all known repos with status.

    Returns registered repos plus current directory (if it's a repo).
    Each repo includes config status and orchestrator state.
    """
    from ..observation.instance_detector import detect_system_state

    state = detect_system_state()
    return JSONResponse({"repos": [r.to_dict() for r in state.repos]})


@control_app.post("/api/repos/{repo_id:path}/start")
async def start_repo_orchestrator(repo_id: str, request: Request) -> JSONResponse:
    """Start orchestrator for a specific repo.

    The repo_id is the URL-encoded absolute path to the repo.

    JSON body (optional):
        config_name: str - Config file to use (default: default.yaml)
    """
    from urllib.parse import unquote

    repo_path = unquote(repo_id)
    path = Path(repo_path)

    if not path.exists():
        return JSONResponse(
            {"error": f"Repository not found: {repo_path}"}, status_code=404
        )

    # Parse optional launch selection from body.
    config_name = "default.yaml"
    mode = "default"
    try:
        body = await request.json()
        if isinstance(body, dict) and "config_name" in body:
            config_name = body["config_name"]
        if isinstance(body, dict) and "mode" in body:
            mode = body["mode"]
    except Exception:
        pass

    try:
        from ..domain.repository_launch_selection import RepositoryLaunchSelection
        from ..execution.repository_engine_start import RepositoryEngineStartRequest

        selection = RepositoryLaunchSelection.parse(
            mode=mode,
            config_name=config_name,
        )
        result = get_control_actions().start_repo_engine_cmd.execute(
            RepositoryEngineStartRequest(
                repo_root=path,
                selection=selection,
                actor="legacy-control-api",
            )
        )
        if result.succeeded:
            _track_launched_pids(result.payload)
        return JSONResponse(dict(result.payload), status_code=result.status_code)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("Failed to start orchestrator for %s", repo_path)
        return JSONResponse({"error": str(e)}, status_code=500)


@control_app.post("/api/repos/{repo_id:path}/stop")
async def stop_repo_orchestrator(repo_id: str, request: Request) -> JSONResponse:
    """Stop orchestrator for a specific repo.

    The repo_id is the URL-encoded absolute path to the repo.

    JSON body:
        reason: str (REQUIRED) - The "why" behind this stop, threaded
            into the target's ``/api/shutdown`` so the target log
            records the calling intent. Empty/missing → 400.
        actor: str (optional) - Source identifier for log grouping.
        force: bool (optional, default false) - Force kill if
            graceful shutdown fails.
    """
    from urllib.parse import unquote

    repo_path = unquote(repo_id)
    path = Path(repo_path)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty / malformed body
        body = {}
    parsed = parse_shutdown_reason(
        body,
        endpoint="/api/repos/{repo_id}/stop",
        default_actor="control_api.stop_repo",
    )
    if isinstance(parsed, JSONResponse):
        return parsed

    force = bool(body.get("force", False)) if isinstance(body, dict) else False

    stopped = _supervisor.stop(
        path, force=force, reason=parsed.reason, actor=parsed.actor
    )
    return JSONResponse({"status": "stopped" if stopped else "failed"})


@control_app.get("/api/repos/{repo_id:path}/status")
async def get_repo_status(repo_id: str) -> JSONResponse:
    """Get detailed status for a specific repo.

    The repo_id is the URL-encoded absolute path to the repo.
    """
    from urllib.parse import unquote
    from ..observation.instance_detector import (
        _get_config_status,
        get_orchestrator_details,
    )
    from ..execution.control_center_runtime import get_selected_launch_selection

    repo_path = unquote(repo_id)
    path = Path(repo_path)

    if not path.exists():
        return JSONResponse(
            {"error": f"Repository not found: {repo_path}"}, status_code=404
        )

    config_status, modes, mode_configs = _get_config_status(path)
    selection = get_selected_launch_selection(path)
    runtime = get_orchestrator_details(
        path,
        selected_mode=selection.mode.value,
        selected_config=selection.config.value,
    )

    return JSONResponse(
        {
            "path": repo_path,
            "name": path.name,
            "config_status": config_status,
            "configs": mode_configs.get("default", []),
            "modes": modes,
            "mode_configs": mode_configs,
            "orchestrator_state": runtime["state"],
            "orchestrator_pid": runtime["pid"],
            "orchestrator_port": runtime["port"],
            "selected_mode": selection.mode.value,
            "selected_config": selection.config.value,
            "active_mode": runtime["active_mode"],
            "active_config": runtime["active_config"],
            "active_config_fingerprint": runtime["active_config_fingerprint"],
            "active_instances": runtime["active_instances"],
        }
    )


@control_app.get("/api/discover")
async def discover_repos_api(
    search_paths: str = Query(
        default="",
        description="Comma-separated paths to search",
    ),
    max_depth: int = Query(default=2, description="Max directory depth"),
) -> JSONResponse:
    """Discover git repositories that could be configured.

    Scans common development directories for git repos.
    """
    from ..observation.instance_detector import discover_repos

    paths = None
    if search_paths:
        paths = [Path(p.strip()).expanduser() for p in search_paths.split(",")]

    discovered = discover_repos(search_paths=paths, max_depth=max_depth)
    return JSONResponse({"discovered": discovered})


@control_app.post("/api/refresh")
async def refresh(request: Request) -> JSONResponse:
    """Request an immediate refresh of issues from GitHub.

    This triggers the orchestrator to fetch issues on the next loop iteration,
    bypassing the fetch-layer network sync interval.

    Optional JSON body:
        inflight_stable_ids: list[str] - Issue IDs that tests expect to discover.
            If provided and these issues are not found after a cached refresh,
            the orchestrator will retry without cache to handle GitHub's
            eventual consistency.
    """
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

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

    _orchestrator.request_refresh(inflight_stable_ids=inflight_stable_ids)
    return JSONResponse({"status": "refresh_requested"})


@control_app.post("/api/pause")
async def pause() -> JSONResponse:
    """Pause the orchestrator - stop launching new sessions."""
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    _orchestrator.pause()
    return JSONResponse({"status": "paused"})


@control_app.post("/api/resume")
async def resume() -> JSONResponse:
    """Resume the orchestrator - allow launching new sessions."""
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    _orchestrator.resume()
    return JSONResponse({"status": "resumed"})


def _active_session_status_payload(session: Any) -> dict[str, Any]:
    runtime_minutes = session.runtime_minutes
    timeout_minutes = session.agent_config.timeout_minutes
    return {
        "session_name": session.terminal_id,
        "issue_number": session.issue.number,
        "title": session.issue.title,
        "runtime_minutes": runtime_minutes,
        "agent_type": session.issue.agent_type,
        "status": "running" if runtime_minutes < timeout_minutes else "slow",
        "branch": session.branch_name,
    }


@control_app.get("/api/status")
async def status() -> JSONResponse:
    """Get current orchestrator status."""
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    state = _orchestrator.state
    sessions = [
        _active_session_status_payload(session) for session in state.active_sessions
    ]
    return JSONResponse(
        {
            "paused": state.paused,
            "active_sessions": len(state.active_sessions),
            "sessions": sessions,
            "pending_reviews": len(state.pending_reviews),
            "pending_reworks": len(state.pending_reworks),
            "completed_today": len(state.completed_today),
            "issues_in_queue": len(state.cached_queue_issues),
            "instance_id": _orchestrator.deps.services.instance_id,
        }
    )


@control_app.get("/api/events")
async def events(request: Request):
    """Server-Sent Events endpoint for test automation."""
    if _orchestrator is None or _orchestrator.event_hub is None:
        return JSONResponse({"error": "Event hub not initialized"}, status_code=503)

    event_hub = _orchestrator.event_hub
    logger.info(
        "[SSE] Client connected (subscribers=%d, last_event_id=%s)",
        event_hub.stats().get("subscribers"),
        event_hub.last_event_id,
    )

    async def event_generator():
        subscription = event_hub.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        subscription.queue.get(), timeout=30.0
                    )
                    yield {
                        "event": event.type,
                        "data": json.dumps(
                            {
                                "event_id": event.event_id,
                                "type": event.type,
                                "issue_key": event.issue_key,
                                "payload": event.payload,
                            }
                        ),
                    }
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}
        finally:
            event_hub.unsubscribe(subscription)
            logger.info(
                "[SSE] Client disconnected (subscribers=%d)",
                event_hub.stats().get("subscribers"),
            )

    return EventSourceResponse(event_generator())


@control_app.get("/api/events_since")
async def events_since(after: int = Query(0, alias="after")) -> JSONResponse:
    """Return buffered events since the provided event id."""
    if _orchestrator is None or _orchestrator.event_hub is None:
        return JSONResponse({"error": "Event hub not initialized"}, status_code=503)

    event_hub = _orchestrator.event_hub
    events = event_hub.get_since(after)
    stats = event_hub.stats()
    logger.info(
        "[SSE] Replay request after=%d events=%d oldest=%s newest=%s",
        after,
        len(events),
        stats.get("oldest_event_id"),
        stats.get("newest_event_id"),
    )
    payload = [
        {
            "event_id": event.event_id,
            "type": event.type,
            "issue_key": event.issue_key,
            "payload": event.payload,
        }
        for event in events
    ]
    return JSONResponse(
        {
            "events": payload,
            "last_event_id": event_hub.last_event_id,
            "stats": stats,
        }
    )


@control_app.get("/api/events_stats")
async def events_stats() -> JSONResponse:
    """Return event buffer and replay statistics."""
    if _orchestrator is None or _orchestrator.event_hub is None:
        return JSONResponse({"error": "Event hub not initialized"}, status_code=503)

    return JSONResponse({"stats": _orchestrator.event_hub.stats()})


@control_app.post("/api/gh_audit_report")
async def gh_audit_report() -> JSONResponse:
    """Emit the GH audit report to disk and return the path."""
    if not gh_audit.enabled():
        return JSONResponse({"error": "GH audit not enabled"}, status_code=400)
    path = gh_audit.emit_report()
    return JSONResponse({"status": "ok", "path": path})


@control_app.get("/api/snapshot")
async def snapshot() -> JSONResponse:
    """Fetch a snapshot of orchestrator state for test resync."""
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    if _orchestrator.event_hub is None:
        return JSONResponse({"error": "Event hub not initialized"}, status_code=503)

    from ..control.snapshot_builder import SnapshotBuilder

    builder = SnapshotBuilder(
        config=_orchestrator.config, repository_host=_orchestrator.deps.repository_host
    )
    snapshot_id = _orchestrator.event_hub.last_event_id
    last_tick_id = _orchestrator.event_context.tick_id

    try:
        data = await asyncio.to_thread(
            builder.build_snapshot,
            _orchestrator.state,
            snapshot_id,
            last_tick_id,
        )
        return JSONResponse(data)
    except Exception as exc:
        logger.exception("Control API snapshot failed: %s", exc)
        return JSONResponse(
            {"error": "snapshot_failed", "detail": str(exc)}, status_code=500
        )


@control_app.get("/api/health")
async def health() -> JSONResponse:
    """Get health status of orchestrator components.

    Returns status of:
    - orchestrator: running/not initialized
    - terminal: tmux server and session health
    """
    health_data: dict = {
        "orchestrator": {"status": "not_initialized"},
        "terminal": {"status": "unknown"},
    }

    if _orchestrator is None:
        return JSONResponse(health_data, status_code=503)

    health_data["orchestrator"] = {
        "status": "running",
        "paused": _orchestrator.state.paused,
        "active_sessions": len(_orchestrator.state.active_sessions),
    }

    # Get terminal health via hook
    try:
        terminal_health = _orchestrator.deps.runner.terminal_health_check()
        if terminal_health:
            health_data["terminal"] = terminal_health
        else:
            health_data["terminal"] = {"status": "no_plugin"}
    except Exception as e:
        health_data["terminal"] = {"status": "error", "error": str(e)}

    # Overall health
    terminal_ok = health_data["terminal"].get("healthy", False)
    health_data["overall"] = "healthy" if terminal_ok else "degraded"

    status_code = 200 if terminal_ok else 503
    return JSONResponse(health_data, status_code=status_code)


# NOTE: ``POST /api/shutdown`` is intentionally NOT defined here. It used
# to live on ``control_app`` and accepted unreasoned shutdowns, which
# made the orchestrator log unable to attribute who triggered a stop.
# The route now lives only on ``web_operator_router`` (see
# ``web_operator_routes.shutdown``) and requires a non-empty ``reason``
# in the JSON body. Re-adding a duplicate here would silently bypass
# that contract because ``app.include_router(web_operator_router)`` is
# called before ``app.mount("", control_app)`` in ``web.py`` — the
# operator route wins on the engine surface, but a duplicate would
# still be reachable on the standalone cc surface and on
# ``TestClient(control_app)`` paths, so we keep it deleted.


@control_app.get("/favicon.ico")
async def favicon():
    """Serve the logo as favicon."""
    return Response(
        content=read_logo_svg(),
        media_type="image/svg+xml",
    )


_DEV_NO_AUTH_BANNER_HTML = (
    '<div style="background:#b91c1c;color:#fff;padding:8px 16px;'
    "text-align:center;font-family:sans-serif;font-weight:600;"
    'letter-spacing:0.4px;z-index:9999;position:sticky;top:0;">'
    "⚠  Authentication DISABLED (--dev-no-auth). "
    "Any local process can mutate state. Dev use only."
    "</div>"
)


def _control_center_html_response(
    content: str, *, status_code: int = 200
) -> HTMLResponse:
    """Return Control Center shell HTML that browsers must revalidate on reopen."""
    response = HTMLResponse(content=content, status_code=status_code)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@control_app.get("/", response_class=HTMLResponse)
async def control_center_ui(request: Request) -> HTMLResponse:
    """Serve the Control Center dashboard to an authenticated browser.

    When auth is disabled entirely (test default) or the visitor
    already holds a valid session cookie, we render the dashboard
    with the session's CSRF token embedded in a ``<meta>`` tag.
    Otherwise we serve the login page; the session cookie is minted
    only after ``POST /login`` verifies the admin bearer token.

    Regression for security #6017 re-review-2 P1: this route used to
    mint a valid session for anyone who hit ``/``, letting any local
    process turn an anonymous visit into admin-equivalent API access.
    """
    auth_enabled = not (_admin_token is None and _agent_callback_token is None)
    page_auth = resolve_browser_page_auth(request, auth_enabled=auth_enabled)
    if isinstance(page_auth, HTMLResponse):
        # No valid session and auth is active — show login form.
        return page_auth

    template_path = _TEMPLATES_DIR / "control_center.html"
    if not template_path.exists():
        return _control_center_html_response(
            "<html><body><h1>Control Center</h1><p>Template not found</p></body></html>",
            status_code=500,
        )

    from ..infra.runtime_identity import resolve_runtime_identity
    from ..infra.static_version import STATIC_VERSION_TOKEN

    # Sidebar SHA: resolve from the package install location, not
    # ``Path.cwd()``. Operators frequently launch the cc from outside
    # any git checkout (e.g., from /), which made the sidebar show
    # "unknown" and left them unable to tell whether the running cc
    # picked up a recent merge. The package-relative resolver finds
    # the source repo when running from a development checkout and
    # cleanly returns ``None`` for non-source installs.
    runtime_identity = resolve_runtime_identity()
    commit_short = runtime_identity.source_commit_short or "unknown"
    content = template_path.read_text()
    content = content.replace("{{ version }}", runtime_identity.package_version)
    content = content.replace("{{ commit_sha }}", commit_short)
    # Cache-buster for ``/static/*`` URLs: every cc restart produces a
    # new token (commit SHA when running from source; process-start
    # epoch for wheel installs), so the browser refetches stale JS/CSS
    # automatically instead of the operator hard-reloading. See
    # PR #6263 for the incident this prevents.
    content = content.replace("{{ static_version }}", STATIC_VERSION_TOKEN)
    content = content.replace(
        "{{ browser_auth_required }}", page_auth.browser_auth_required
    )
    content = content.replace("{{ csrf_token }}", page_auth.csrf_token)
    # Render the dev-mode banner only when the operator has
    # explicitly disabled auth (``--dev-no-auth`` /
    # ``ISSUE_ORCHESTRATOR_DEV_NO_AUTH=1``). In the normal
    # auth-enabled path we can't reach this branch without a valid
    # session cookie, so no banner is needed.
    content = content.replace(
        "{{ dev_no_auth_banner }}",
        _DEV_NO_AUTH_BANNER_HTML if not auth_enabled else "",
    )
    # Server-render the flash diagnostic probe as a parser-blocking
    # <script> tag only when ?debug=flash is in the URL. The
    # localStorage-based toggle is handled by an inline gate in the
    # template (loads the script asynchronously, fine for ad-hoc
    # debugging where a slightly-late install is acceptable).
    flash_debug_enabled = request.query_params.get("debug") == "flash"
    flash_debug_script = (
        '<script src="/static/js/flash_debug.js"></script>'
        if flash_debug_enabled
        else ""
    )
    content = content.replace("{{ flash_debug_script }}", flash_debug_script)
    return _control_center_html_response(content)


@control_app.post("/login")
async def control_center_login(request: Request) -> Response:
    """Exchange the admin bearer token for a browser session cookie.

    Delegates to the shared ``handle_login_post`` helper; see that
    function for the accept-both-content-types / constant-time verify
    / cookie-mint semantics.
    """
    return await handle_login_post(request, _admin_token)


@control_app.get("/api/sse-token")
async def issue_browser_sse_token(request: Request) -> JSONResponse:
    """Return a short-lived single-use SSE token for the caller's session."""
    return issue_sse_token_response(request)


# ======================================================================# Supervisor Control API - Process Management Endpoints
# ======================================================================# These endpoints manage orchestrator processes via the Supervisor.
# They work with repo_root paths rather than in-process state.


def _validate_repo_root(repo_root: object | None) -> Path | None:
    """Validate and normalize a ``repo_root`` parameter from a request.

    The Control API is bearer-token authenticated (security #5987 F3),
    so any caller reaching this point already holds a valid token.
    Even so, we defend against malformed input here so a bug in a
    client (or a future unauthenticated surface) cannot feed this
    helper something weird — including the wrong JSON type.

    Accepts ``object | None`` because the request layer passes
    ``body.get("repo_root")`` straight through; a client that sends
    a number, a bool, bytes, or a dict used to trigger a 500 from
    the downstream ``.strip()`` / ``Path()`` call. This helper owns
    all of that validation and returns ``None`` on any non-string
    input (#6017 re-review-3 P2 on #6018):

    - Reject anything that is not ``str``.
    - Reject empty / whitespace-only strings.
    - Reject strings containing null bytes — ``Path`` accepts them
      silently on some platforms and they confuse downstream tooling.
    - ``Path.resolve`` normalizes ``..`` segments and follows
      symlinks, so the returned path is always the canonical target.
    - Require the resolved target to exist as a directory.

    Returns the resolved ``Path`` on success, ``None`` on rejection.
    Rejections log at DEBUG so misconfiguration leaves a trace.
    """
    if not isinstance(repo_root, str):
        if repo_root is not None:
            logger.debug(
                "validate_repo_root rejected non-string value of type %s",
                type(repo_root).__name__,
            )
        return None
    if not repo_root or not repo_root.strip():
        return None
    if "\x00" in repo_root:
        logger.debug("validate_repo_root rejected value with null byte")
        return None

    try:
        path = Path(repo_root).resolve()
    except (ValueError, OSError) as exc:
        logger.debug("validate_repo_root could not resolve %r: %s", repo_root, exc)
        return None

    try:
        if not path.exists() or not path.is_dir():
            logger.debug(
                "validate_repo_root rejected %s: does not exist or not a directory",
                path,
            )
            return None
    except OSError as exc:
        logger.debug("validate_repo_root stat failed for %s: %s", path, exc)
        return None

    return path


install_control_api_e2e_dependencies(
    control_app,
    ControlApiE2EDependencies(
        get_orchestrator=get_orchestrator,
        load_config_selection=_load_config_selection,
        validate_repo_root=_validate_repo_root,
    ),
)
install_control_api_orchestrator_dependencies(
    control_app,
    ControlApiOrchestratorDependencies(
        get_supervisor=get_supervisor,
        get_control_actions=get_control_actions,
        validate_repo_root=_validate_repo_root,
        track_launched_pids=_track_launched_pids,
        coerce_graceful_timeout_seconds=coerce_graceful_timeout_seconds,
        global_shutdown_in_progress=global_shutdown_in_progress,
        begin_engine_shutdown_operation=begin_engine_shutdown_operation,
        finish_engine_shutdown_operation=finish_engine_shutdown_operation,
    ),
)
install_control_api_shutdown_dependencies(
    control_app,
    ControlApiShutdownDependencies(
        get_supervisor=get_supervisor,
        schedule_control_center_exit=_schedule_control_center_exit_dependency,
    ),
)
install_control_api_goal_pilot_dependencies(
    control_app,
    ControlApiGoalPilotDependencies(
        get_orchestrator=get_orchestrator,
        get_goal_pilot=_get_goal_pilot,
    ),
)
install_control_api_issue_dependencies(
    control_app,
    ControlApiIssueDependencies(
        get_orchestrator=get_orchestrator,
        with_state_lock=_with_state_lock,
    ),
)
install_control_api_tools_dependencies(
    control_app,
    ControlApiToolsDependencies(
        get_control_actions=get_control_actions,
        validate_repo_root=_validate_repo_root,
    ),
)
install_control_api_repo_dependencies(
    control_app,
    ControlApiRepoDependencies(
        get_supervisor=get_supervisor,
        get_control_actions=get_control_actions,
        validate_repo_root=_validate_repo_root,
        get_preferred_repo_root=_preferred_repo_root,
        get_expected_engine_identity_raw=lambda: (
            os.environ.get(
                "ISSUE_ORCHESTRATOR_EXPECTED_IDENTITY",
                "",
            ).strip()
            or None
        ),
    ),
)
install_control_api_setup_dependencies(
    control_app,
    ControlApiSetupDependencies(
        validate_repo_root=_validate_repo_root,
        setup_owner=build_repository_setup_owner(
            _create_repository_setup_host,
            _verify_repository_setup_github_authorization,
        ),
        github_token_store=_store_repository_setup_github_token,
        validation_detector=RepositorySetupValidationDetectorAdapter(),
    ),
)
control_app.include_router(control_orchestrator_router)
control_app.include_router(control_shutdown_router)
control_app.include_router(control_goal_pilot_router)
control_app.include_router(control_issue_router)
control_app.include_router(control_tools_router)
control_app.include_router(control_repo_router)
control_app.include_router(control_setup_router)
control_app.include_router(control_e2e_runs_router)
control_app.include_router(control_e2e_triage_router)


@control_app.get("/api/session/terminal-recording/{issue_number}")
async def control_terminal_recording(
    issue_number: int,
    offset: int = 0,
    limit: int = 200,
    run_dir: str | None = None,
    round_index: int | None = None,
    session_role: str | None = None,
) -> JSONResponse:
    """Terminal recording endpoint on control center — delegates to shared implementation."""
    from ..entrypoints.web import serve_terminal_recording

    return serve_terminal_recording(
        issue_number,
        run_dir,
        offset,
        limit,
        round_index,
        session_role,
    )


@control_app.get("/api/issue-detail/{issue_number}")
@timeline_projection_endpoint("control_issue_detail")
async def control_issue_detail(
    issue_number: int,
    repo_root: str = Query(...),
    view: str = Query("user"),
) -> JSONResponse:
    """Issue detail endpoint on control center.

    Reads timeline events from the E2E worktree's timeline.sqlite for E2E
    test issues, then runs them through the same view model pipeline as
    the dashboard's issue-detail endpoint. Returns the same payload shape
    so the existing renderJourneyTimeline JS works without changes.
    """
    from ..execution.timeline_store import read_timeline_records
    from ..infra.e2e_worktree import get_e2e_worktree_path
    from ..timeline import TimelineStream
    from ..view_models.issue_detail import build_issue_detail_view_model
    from ..view_models.timeline_view import normalize_timeline_view

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    view = normalize_timeline_view(view)

    # Try base repo timeline first, then E2E worktree timeline
    candidates = [
        validated_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
        get_e2e_worktree_path(validated_root)
        / ".issue-orchestrator"
        / "state"
        / "timeline.sqlite",
    ]
    records: list = []
    for db_path in candidates:
        if not db_path.exists():
            continue
        try:
            found = read_timeline_records(db_path, issue_number, limit=5000)
            if found:
                records = found
                break
        except Exception:
            logger.debug("Could not read timeline from %s", db_path, exc_info=True)

    if not records:
        return JSONResponse(
            {
                "error": "not_found",
                "detail": f"No timeline events for issue {issue_number}",
            },
            status_code=404,
        )

    stream = TimelineStream.from_records(issue_number, records)
    raw_events = [evt.to_dict() for evt in stream.events]
    filtered_events = _filter_timeline_events(raw_events)
    decorated = _decorate_timeline_events(filtered_events, issue_number)
    phase_toc = _build_phase_toc(decorated)
    cycles = _build_timeline_cycles(decorated)

    payload = build_issue_detail_view_model(
        issue_number=issue_number,
        title=f"Issue #{issue_number}",
        issue_url="",
        events=decorated,
        phase_toc=phase_toc,
        cycles=cycles,
        context=None,
        view=view,
        raw_events=raw_events,
    )
    return JSONResponse(payload)
