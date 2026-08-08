"""Web dashboard for the orchestrator."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
import webbrowser
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from ..control.shutdown_manager import shutdown_manager
from ..events.sse_envelope import apply_sse_envelope
from ..execution.client_host import ClientHost, detect_client_host
from ..execution.review_artifact_reader import ManifestReviewArtifactReader
from ._auth_middleware import (
    AuthSurfaceConfig,
    evaluate_request,
    handle_login_post,
    is_agent_callback_route,
    issue_sse_token_response,
)
from .brand_assets import read_logo_svg
from .timeline_presentation import (
    _decorate_timeline_events,
    _is_agent_scoped_event,
    _timeline_event_default_actions,
    _timeline_event_actions,
    _timeline_event_recommended_actions,
    _timeline_event_requires_run_dir,
)
from .web_diagnostics_routes import install_web_diagnostics_dependencies, web_diagnostics_router
from .web_retrospective_review_routes import web_retrospective_review_router
from .web_tech_lead_routes import web_tech_lead_router
from .web_issue_detail_routes import web_issue_detail_router
from .web_log_routes import web_log_router
from .web_operator_routes import install_web_operator_dependencies, web_operator_router
from .web_read_model_routes import web_read_model_router
from .web_refresh_routes import web_refresh_router
from .web_retry_history_routes import web_retry_history_router
from .web_settings_routes import web_settings_router
from .web_session_context import (
    install_web_session_context_dependencies,
    resolve_issue_session_context,
)
from .web_session_routes import serve_terminal_recording, web_session_router
from .web_status_routes import web_status_router

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# How long orchestration waits for the server to publish its bound port
# before starting anyway (and reporting the problem).
_SERVER_PUBLISH_TIMEOUT_SECONDS = 30.0
_COMPAT_EXPORTS = (
    _decorate_timeline_events,
    _is_agent_scoped_event,
    _timeline_event_default_actions,
    _timeline_event_actions,
    _timeline_event_recommended_actions,
    _timeline_event_requires_run_dir,
)
__all__ = ("_resolve_issue_session_context", "serve_terminal_recording")


# Create FastAPI app
app = FastAPI(title="Issue Orchestrator")

# Static directory (sibling to templates directory)
STATIC_DIR = Path(__file__).parent.parent / "static"

# Mount static files with proper security (prevents path traversal, uses async streaming)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Dashboard auth (security #5987 F3 — PR 8)
#
# The Web Dashboard on port 8080 exposes a large surface including
# ``/api/shutdown``, ``/api/open-file``, ``/api/kill/{issue}``, and an
# SSE stream on ``/api/events``. Before this change every route was
# reachable with no credentials, so any same-host process could
# mutate orchestrator state or snoop the SSE feed.
#
# PR 8 applies the same three-path gate the Control Center shipped in
# #6011: bearer token for programmatic clients, session cookie + CSRF
# for browsers, short-lived single-use token on the SSE URL. The
# admin secret is shared with the CC on purpose — the dashboard
# surface is a strict superset in sensitivity, so one login covers
# both.
# ---------------------------------------------------------------------------

_dashboard_admin_token: str | None = None

# ``/`` and ``/settings`` (top-level HTML pages) are public so that
# anonymous visitors can be shown the login form — each page handler
# itself checks for a valid session and renders the login form when
# auth is enabled but the caller has no cookie. Their mutating
# ``/api/*`` endpoints stay gated, so no data is exposed.
_DASHBOARD_UNAUTHENTICATED_PATHS: frozenset[str] = frozenset({
    "/",
    "/settings",
    "/login",
    "/favicon.ico",
})
_DASHBOARD_UNAUTHENTICATED_PREFIXES: tuple[str, ...] = ("/static/",)

# This app mounts ``control_app`` at ``""`` (see the bottom of this
# module), so the agent-callback routes are served on the dashboard
# port and *this* gate is the first one they hit. It must therefore
# honour the same allowlist the Control API does — see #6913.
_DASHBOARD_SURFACE = AuthSurfaceConfig(
    sse_path="/api/events",
    public_paths=_DASHBOARD_UNAUTHENTICATED_PATHS,
    name="web_dashboard",
    public_prefixes=_DASHBOARD_UNAUTHENTICATED_PREFIXES,
    agent_callback_matcher=is_agent_callback_route,
)


def configure_dashboard_admin_token(admin: str | None) -> None:
    """Enable (or disable) dashboard auth.

    ``admin`` — the shared admin bearer token (same one used for the
    Control API). Pass ``None`` to disable enforcement entirely;
    ``TestClient`` defaults to this.
    """
    global _dashboard_admin_token
    _dashboard_admin_token = admin


def get_configured_dashboard_admin_token() -> str | None:
    """Return the admin token currently enforced on the dashboard."""
    return _dashboard_admin_token


@app.middleware("http")
async def _dashboard_auth_middleware(  # pyright: ignore[reportUnusedFunction]
    request: Request, call_next: Any
) -> Response:
    """Enforce dashboard auth via the shared three-path gate.

    ``control_app`` is mounted at ``""`` (bottom of this module), so its
    routes are served here and flow through both gates — intentional
    defense in depth. That also means agent-callback requests reach
    *this* gate first, so it must know the agent-callback token; passing
    ``None`` here rejected every valid agent callback before the inner
    gate ever ran (#6913). The token is read from the Control API rather
    than mirrored, so the two surfaces cannot drift apart.
    """
    from .control_api import get_configured_agent_callback_token

    gate_response = evaluate_request(
        request,
        _dashboard_admin_token,
        get_configured_agent_callback_token(),
        _DASHBOARD_SURFACE,
    )
    if gate_response is not None:
        return gate_response
    return await call_next(request)


if os.environ.get("IO_DEV"):

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        """Prevent browser from caching static assets (CSS/JS).

        Without this, the dashboard iframe in the control center serves stale
        JS/CSS even after a hard-refresh of the parent page.
        Only active when IO_DEV=1 is set.
        """
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response


@app.post("/login")
async def dashboard_login(request: Request) -> Response:
    """Exchange the admin bearer token for a dashboard session cookie.

    Delegates to the shared helper; accepts both form-urlencoded (HTML
    form) and JSON (programmatic) bodies.
    """
    return await handle_login_post(request, _dashboard_admin_token)


@app.get("/api/sse-token")
async def dashboard_sse_token(request: Request) -> JSONResponse:
    """Return a short-lived single-use SSE token for the caller's session."""
    return issue_sse_token_response(request)

# Global reference to orchestrator (set at startup)
_orchestrator: "Orchestrator | None" = None
# Global reference to uvicorn server (for shutdown)
_server: "Any" = None
_client_host: ClientHost = detect_client_host()

# SSE event subscribers - set of asyncio.Queue objects
_event_subscribers: set[asyncio.Queue] = set()

# Main event loop reference for thread-safe event broadcasting
# Set at startup so worker threads can schedule SSE broadcasts
_main_loop: asyncio.AbstractEventLoop | None = None


async def broadcast_event(event_type: str, data: dict | None = None) -> None:
    """Broadcast an event to all SSE subscribers.

    This is the single serialization boundary for the SSE stream, so it is where
    the public envelope is applied: ``apply_sse_envelope`` stamps the ``schema``
    version onto every event regardless of which producer emitted it. Callers
    must not add the field themselves — see
    :mod:`issue_orchestrator.events.sse_envelope`.

    Args:
        event_type: Type of event (e.g., "session_started", "session_completed", "state_changed")
        data: Optional data to include with the event
    """
    enveloped = apply_sse_envelope(event_type, data)
    # ``data`` is a read-only mapping; copy it into a plain dict so the
    # transport can JSON-serialize it.
    event = {"type": enveloped.type, "data": dict(enveloped.data)}
    dead_subscribers = []

    for queue in _event_subscribers:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Queue full, mark for removal
            dead_subscribers.append(queue)

    # Clean up dead subscribers
    for queue in dead_subscribers:
        _event_subscribers.discard(queue)


def add_event_subscriber(queue: asyncio.Queue) -> None:
    """Register an SSE subscriber queue."""
    _event_subscribers.add(queue)


def remove_event_subscriber(queue: asyncio.Queue) -> None:
    """Remove an SSE subscriber queue."""
    _event_subscribers.discard(queue)


def event_subscribers_snapshot() -> set[asyncio.Queue]:
    """Return a snapshot of current SSE subscribers."""
    return set(_event_subscribers)


def get_main_loop() -> asyncio.AbstractEventLoop | None:
    """Return the main event loop reference for SSE scheduling."""
    return _main_loop


@contextlib.contextmanager
def swapped_event_subscribers(subscribers: set[asyncio.Queue]) -> Iterator[None]:
    """Temporarily replace the SSE subscriber set (for tests)."""
    global _event_subscribers
    original = _event_subscribers
    _event_subscribers = subscribers
    try:
        yield
    finally:
        _event_subscribers = original


def get_orchestrator():
    """Get the orchestrator instance. Override in tests via app.dependency_overrides."""
    return _orchestrator


def get_client_host() -> ClientHost:
    """Return the current client-host adapter."""
    return _client_host


def _resolve_issue_session_context(issue_number: int):
    """Compatibility wrapper for callers still importing from ``web``."""
    return resolve_issue_session_context(get_orchestrator(), issue_number)


def trigger_server_shutdown():
    """Trigger uvicorn server shutdown."""
    global _server
    if _server:
        _server.should_exit = True
        _server.force_exit = True  # Don't wait for graceful shutdown


def _orchestrator_host_repo_root() -> Path | None:
    """Return the orchestrator's host repo root for archived-session
    path resolution in the open-file endpoint, or None when the
    orchestrator isn't initialized yet (e.g. early startup, tests
    that bypass bootstrap)."""
    orch = get_orchestrator()
    if orch is None:
        return None
    repo_root = getattr(getattr(orch, "config", None), "repo_root", None)
    return repo_root if isinstance(repo_root, Path) else None


def _orchestrator_worktree_base() -> Path | None:
    """Return the configured worktree base for safe host path opens."""
    orch = get_orchestrator()
    if orch is None:
        return None
    worktree_base = getattr(getattr(orch, "config", None), "worktree_base", None)
    return worktree_base if isinstance(worktree_base, Path) else None


install_web_session_context_dependencies(
    app,
    get_orchestrator=get_orchestrator,
    review_artifact_reader=ManifestReviewArtifactReader(),
)
install_web_operator_dependencies(
    app,
    get_client_host=get_client_host,
    broadcast_event=broadcast_event,
    trigger_server_shutdown=trigger_server_shutdown,
    get_host_repo_root=_orchestrator_host_repo_root,
    get_worktree_base=_orchestrator_worktree_base,
)
install_web_diagnostics_dependencies(app, get_client_host=get_client_host)
app.include_router(web_read_model_router)
app.include_router(web_status_router)
app.include_router(web_refresh_router)
app.include_router(web_settings_router)
app.include_router(web_log_router)
app.include_router(web_operator_router)
app.include_router(web_diagnostics_router)
app.include_router(web_retry_history_router)
app.include_router(web_retrospective_review_router)
app.include_router(web_tech_lead_router)
app.include_router(web_session_router)
app.include_router(web_issue_detail_router)


def set_orchestrator(orchestrator) -> None:
    """Set the orchestrator instance. Used by tests and application startup."""
    global _orchestrator
    _orchestrator = orchestrator


def set_client_host(client_host: ClientHost) -> None:
    """Set the client-host adapter. Used by tests."""
    global _client_host
    _client_host = client_host


def set_server(server) -> None:
    """Set the server instance. Used by tests and application startup."""
    global _server
    _server = server


@app.get("/favicon.ico")
async def favicon():
    """Serve the logo as favicon."""
    return Response(
        content=read_logo_svg(),
        media_type="image/svg+xml",
    )


@app.get("/api/events")
async def events(request: Request):
    """Server-Sent Events endpoint for real-time updates.

    The dashboard connects to this endpoint to receive instant notifications
    when sessions start, complete, or state changes.
    """
    async def event_generator():
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        _event_subscribers.add(queue)
        logger.info("[SSE] Client connected, %d total subscribers", len(_event_subscribers))

        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                try:
                    # Wait for event with timeout (sends keepalive)
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": event["type"],
                        "data": json.dumps(event["data"]),
                    }
                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent connection timeout
                    yield {"comment": "keepalive"}
        finally:
            _event_subscribers.discard(queue)
            logger.info("[SSE] Client disconnected, %d remaining subscribers", len(_event_subscribers))

    return EventSourceResponse(event_generator())


@app.post("/api/test/create")
async def create_test_issues() -> JSONResponse:
    """Create test issues for testing."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..testing.support.test_data import create_test_issues as _create_test_issues

    config = _orchestrator.config
    if not config.repo:
        return JSONResponse({"error": "No repo configured"}, status_code=400)
    try:
        urls = _create_test_issues(config.repo, list(config.agents.keys()))
        # Set filtering.label so orchestrator picks them up
        config.filtering.label = "test-data"
        return JSONResponse({"created": urls})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/test/cleanup")
async def cleanup_test_issues() -> JSONResponse:
    """Close all test issues and clear their session history."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..testing.support.test_data import cleanup_test_issues as _cleanup_test_issues

    config = _orchestrator.config
    if not config.repo:
        return JSONResponse({"error": "No repo configured"}, status_code=400)
    try:
        count = _cleanup_test_issues(config.repo)
        # Also clear session history for test issues (titles starting with [TEST])
        state = _orchestrator.state
        state.session_history = [
            entry for entry in state.session_history
            if not entry.title.startswith("[TEST]")
        ]
        return JSONResponse({"closed": count})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a port is already in use by another process.

    Uses SO_REUSEADDR to ignore TIME_WAIT state - a port in TIME_WAIT
    can still be bound by uvicorn (which also uses SO_REUSEADDR).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


def _kill_process_on_port(port: int, use_sigkill: bool = False) -> bool:
    """Kill any process using the specified port.

    Args:
        port: The port to free
        use_sigkill: If True, use SIGKILL (force); otherwise SIGTERM (graceful)

    Returns True if a process was killed, False otherwise.
    """
    try:
        # Use lsof to find process using the port (macOS/Linux)
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            sig = signal.SIGKILL if use_sigkill else signal.SIGTERM
            for pid in pids:
                try:
                    os.kill(int(pid), sig)
                    logger.info("[web] Sent %s to process %s on port %d",
                               "SIGKILL" if use_sigkill else "SIGTERM", pid, port)
                except (ProcessLookupError, ValueError):
                    pass
            return True
    except FileNotFoundError:
        # lsof not available
        pass
    return False


def ensure_port_available(port: int, host: str = "127.0.0.1", max_retries: int = 5) -> None:
    """Ensure the specified port is available, killing any existing process if needed.

    This function is designed to handle orchestrator restarts gracefully by
    automatically freeing the port from any stale processes.
    """
    import time

    if not _is_port_in_use(port, host):
        return

    logger.warning("[web] Port %d is already in use, attempting to free it...", port)

    # First try graceful shutdown (SIGTERM)
    _kill_process_on_port(port, use_sigkill=False)
    time.sleep(0.3)

    # Retry loop with escalating force
    for attempt in range(max_retries):
        if not _is_port_in_use(port, host):
            logger.info("[web] Port %d is now available", port)
            return

        # After first attempt, use SIGKILL
        if attempt > 0:
            logger.warning("[web] Port %d still in use, force killing (attempt %d/%d)...",
                          port, attempt + 1, max_retries)
            _kill_process_on_port(port, use_sigkill=True)

        # Exponential backoff: 0.5s, 1s, 2s, 4s, 8s
        wait_time = 0.5 * (2 ** attempt)
        time.sleep(wait_time)

    # Final check
    if not _is_port_in_use(port, host):
        logger.info("[web] Port %d is now available", port)
        return

    # Still in use after all retries - provide helpful error
    raise RuntimeError(
        f"Port {port} is already in use and could not be freed after {max_retries} attempts. "
        f"Try manually: lsof -ti:{port} | xargs kill -9"
    )


def _get_bound_port(server: object) -> int:
    """Read the actual bound port from a started uvicorn server."""
    for s in getattr(server, "servers", []):
        for sock in getattr(s, "sockets", []):
            addr = sock.getsockname()
            if isinstance(addr, tuple) and len(addr) >= 2:
                return addr[1]
    raise RuntimeError("Could not determine bound port from uvicorn server")


async def run_web_dashboard(
    orchestrator: "Orchestrator",
    port: int = 8080,
    open_browser: bool = True,
    on_server_started: Callable[[int], Awaitable[None] | None] | None = None,
    server_published: asyncio.Event | None = None,
) -> None:
    """Run the web dashboard server.

    Args:
        orchestrator: The orchestrator instance
        port: Port to run on (default 8080, 0 = auto-assign free port)
        open_browser: If True, auto-open browser (default True)
    """
    global _orchestrator, _server
    _orchestrator = orchestrator

    # Also set orchestrator for mounted control_app
    from .control_api import set_orchestrator as set_control_orchestrator
    set_control_orchestrator(orchestrator)

    if port != 0:
        # Ensure fixed port is available before starting
        ensure_port_available(port)

    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",  # Reduce noise, we have our own logging
        timeout_graceful_shutdown=0,  # Exit immediately when shutdown requested
    )
    server = uvicorn.Server(config)
    _server = server  # Store for shutdown access

    async def after_server_started() -> None:
        while not server.started:
            await asyncio.sleep(0.05)
        actual_port = _get_bound_port(server) if port == 0 else port
        if on_server_started is not None:
            result = on_server_started(actual_port)
            if asyncio.iscoroutine(result):
                await result
        # Set only after the hook has run, so anything awaiting this
        # sees a published callback endpoint, not merely a bound socket.
        if server_published is not None:
            server_published.set()
        if open_browser:
            url = f"http://127.0.0.1:{actual_port}"
            logger.info("[web] Starting uvicorn server on %s", url)
            webbrowser.open(url)
            return
        logger.info("[web] Starting uvicorn server on 127.0.0.1:%d", actual_port)

    asyncio.create_task(after_server_started())

    await server.serve()
    logger.info("[web] Server stopped")


async def run_with_web_dashboard(
    orchestrator: "Orchestrator",
    port: int = 8080,
    open_browser: bool = True,
    on_server_started: Callable[[int], Awaitable[None] | None] | None = None,
) -> None:
    """Run orchestrator with web dashboard.

    The web server starts immediately while startup runs in background.
    The orchestrator loop waits for startup to complete before processing.

    Args:
        orchestrator: The orchestrator instance
        port: Port to run web server on
        open_browser: If True, auto-open browser (default True)
    """
    import time

    # Initialize shutdown manager with repo root for lock cleanup
    if orchestrator.config.repo_root:
        shutdown_manager.initialize(orchestrator.config.repo_root)
    # shutdown_manager.exit() uses os._exit(), so register the engine-owned
    # cleanup boundary before any HTTP or signal exit can bypass it.
    shutdown_manager.add_cleanup_callback(orchestrator.close)

    def run_startup_sync():
        """Run startup synchronously in a thread.

        Note: _emit_event calls during startup won't reach SSE subscribers
        because asyncio.Queue is not thread-safe. The startup_complete event
        is emitted after returning to the main event loop.
        """
        asyncio.run(orchestrator.startup())

    server_published = asyncio.Event()

    async def run_startup_and_loop():
        """Run startup then the orchestrator loop."""
        global _main_loop

        # Wait for the server to publish its bound port before starting
        # orchestration. A fixed sleep raced session launch against
        # endpoint publication, so an agent could be spawned before it
        # could be told where to call back (#6924 F7).
        #
        # Bounded: if the server never comes up, the loop must still
        # start so the dashboard reports the problem instead of hanging
        # silently. Agent launch stays blocked by the readiness gate in
        # SessionLauncher, so proceeding here cannot spawn an agent that
        # has nowhere to call back.
        try:
            await asyncio.wait_for(
                server_published.wait(), timeout=_SERVER_PUBLISH_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.error(
                "[web] Server did not publish a bound port within %.0fs; "
                "starting orchestration anyway. Agent sessions stay "
                "blocked until the callback endpoint is available.",
                _SERVER_PUBLISH_TIMEOUT_SECONDS,
            )
        try:
            # Run startup in a thread pool to avoid blocking the event loop
            # startup() makes synchronous GitHub API calls that would block serving requests
            startup_start = time.time()
            logger.info("[web] Starting orchestrator startup in thread pool...")

            loop = asyncio.get_running_loop()
            # Store main loop reference for thread-safe SSE broadcasting
            _main_loop = loop
            await loop.run_in_executor(None, run_startup_sync)

            startup_elapsed = time.time() - startup_start
            logger.info("[web] Startup completed in %.1fs, emitting startup_complete event", startup_elapsed)

            # Emit startup_complete HERE in the main event loop (not from thread)
            # This ensures SSE subscribers receive it properly
            await broadcast_event("startup_complete", {"elapsed_seconds": startup_elapsed})

            await orchestrator.run_loop()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[web] Orchestrator task crashed — triggering shutdown")
            orchestrator.shutdown_requested = True
            shutdown_manager.request_shutdown(reason="orchestrator task crashed")
            shutdown_manager.exit()

    import os

    if os.environ.get("ORCHESTRATOR_NO_BROWSER") in {"1", "true", "True"}:
        open_browser = False

    # Start orchestrator (startup + loop) in background
    orchestrator_task = asyncio.create_task(run_startup_and_loop())

    try:
        # Run web server in foreground (available immediately)
        await run_web_dashboard(
            orchestrator,
            port,
            open_browser=open_browser,
            on_server_started=on_server_started,
            server_published=server_published,
        )
    finally:
        # When web server stops, stop orchestrator
        logger.info("[web] Shutting down orchestrator...")
        orchestrator.shutdown_requested = True
        orchestrator_task.cancel()
        try:
            # Give the task 2 seconds to clean up, then exit anyway
            # (task may be stuck in synchronous GitHub API calls in thread pool)
            await asyncio.wait_for(orchestrator_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            logger.info("[web] Orchestrator task did not exit cleanly, forcing exit")

        # Force exit - thread pool threads (e.g., startup) can't be cancelled
        # and would keep the process alive indefinitely
        logger.info("[web] Shutdown complete, exiting via shutdown_manager")
        shutdown_manager.request_shutdown(reason="web server stopped")
        shutdown_manager.exit()


# Mount control API AFTER all routes are defined so app's routes take precedence
# This allows dashboard JS to fetch /control/... routes from the same port
from .control_api import control_app
app.mount("", control_app)
