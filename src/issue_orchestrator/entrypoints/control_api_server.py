"""The in-process Control API server and its startup lifecycle.

Split from ``control_api``, which defines the routes. This module owns
when the server is *usable*: it binds, verifies uvicorn actually
started, and only then publishes the bound port to the agent-callback
endpoint. Publishing before that check reported ready on an address
nothing was serving, and orchestration launched agents against it
(#6924 F9).

Every ``issue-orchestrator start`` mode binds through this class, so
this is where that lifecycle converges.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Optional

from ..infra import browser_session
from ..infra.api_token import resolve_agent_callback_token, resolve_api_token
from ._auth_middleware import install_access_log_redaction
from .control_api import configure_api_token, control_app, set_orchestrator

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class ControlAPIServer:
    """Manages the control API server lifecycle."""

    def __init__(self, orchestrator: "Orchestrator", port: int = 19080):
        """Initialize the control API server.

        Args:
            orchestrator: The orchestrator instance to control
            port: Port to listen on (default: 19080 to avoid conflict with web dashboard)
        """
        self.orchestrator = orchestrator
        self.port = port
        self._server: Optional[Any] = None  # uvicorn.Server (imported inside start())
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the control API server.

        When self.port is 0, uvicorn binds to an OS-assigned free port.
        After startup, self.port is updated to the actual bound port.
        """
        import uvicorn

        set_orchestrator(self.orchestrator)

        # Resolve + activate both tokens before binding. Kept inside
        # ``start`` so test harnesses that import ``control_app``
        # without spinning up a server do not inadvertently create
        # the token files on a developer machine. The admin token
        # authorizes every route; the agent-callback token narrows
        # the default path for agent subprocesses to
        # ``_AGENT_CALLBACK_ROUTES`` — defense in depth, not an
        # isolation boundary against a same-user malicious agent
        # that can read the admin token file directly (issue #6024).
        # See security #5987 (F3) and #6017 review (P2).
        admin_token = resolve_api_token()
        agent_callback_token = resolve_agent_callback_token()
        configure_api_token(admin_token, agent_callback=agent_callback_token)
        # Initialize the browser-session HMAC secret + tunables so the
        # Control Center UI can establish an ``io_session`` cookie on
        # first visit (#6017 re-review P3). YAML config supplies the
        # defaults; env vars override at resolution time.
        cfg = getattr(self.orchestrator, "config", None)
        # Derive the HMAC secret from the admin token so the dashboard
        # process (which loads the same token) ends up with the same
        # secret without any IPC. A cookie minted on port 19080 then
        # validates on port 8080 — single-login UX across processes.
        browser_session.initialize(
            admin_token=admin_token,
            session_ttl_seconds=getattr(cfg, "browser_session_ttl_seconds", None),
            sse_token_ttl_seconds=getattr(cfg, "sse_token_ttl_seconds", None),
            max_sessions=getattr(cfg, "browser_session_max", None),
        )
        # Strip SSE tokens from uvicorn access-log lines so a query
        # param that's still valid for a few seconds doesn't persist
        # in log storage (#6017 re-review-3 P2).
        install_access_log_redaction()
        # Export into the process environment so in-process clients
        # (MCP server, CLI tools launched by this orchestrator) pick
        # up the admin token. The agent-callback token is surfaced
        # only into agent subprocesses — see agent_runner_env.py.
        os.environ.setdefault("ISSUE_ORCHESTRATOR_API_TOKEN", admin_token)
        os.environ["ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN"] = agent_callback_token
        config = uvicorn.Config(
            control_app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",  # Quiet logging
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        # Run server in background task
        self._task = asyncio.create_task(self._server.serve())

        # Wait for server to be ready (up to 5 seconds)
        for _ in range(50):
            if self._server.started:
                break
            if self._task.done():
                break
            await asyncio.sleep(0.1)

        # Never publish an endpoint the server is not actually serving.
        # Publishing unconditionally reported ready with a fixed port
        # that nothing was listening on, so orchestration launched agents
        # against a dead address (#6924 F9). Surface the serve() failure
        # if there was one; otherwise report the timeout.
        if not self._server.started:
            if self._task.done() and self._task.exception() is not None:
                raise RuntimeError(
                    "Control API server failed to start"
                ) from self._task.exception()
            raise RuntimeError(
                f"Control API server did not start within 5s on port {self.port}"
            )

        # Read back the actual bound port (important when port=0)
        if self.port == 0 and self._server.started:
            for s in self._server.servers:
                for sock in s.sockets:
                    addr = sock.getsockname()
                    if isinstance(addr, tuple) and len(addr) >= 2:
                        self.port = addr[1]
                        break

        # Publish where agents can reach us. All three CLI start modes
        # bind through this class, so routing publication here covers
        # every supported server lifecycle rather than just the
        # supervised web entrypoint (#6924 F7).
        self.orchestrator.deps.agent_callback_endpoint.publish_bound_port(
            self.port
        )
        logger.info(f"Control API started on http://127.0.0.1:{self.port}")

    async def stop(self) -> None:
        """Stop the control API server."""
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            logger.info("Control API stopped")
