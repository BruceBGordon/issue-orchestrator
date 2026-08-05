"""What the engine must publish before any agent session can launch.

An agent subprocess can only call back if two things are true at the
moment it starts:

1. it holds a credential the serving surface accepts, and
2. it was told a port something is actually listening on.

Both are established during engine startup, by different code paths,
and neither is visible in a session's own wiring. When one silently did
not happen, every agent callback failed and the round runner read the
resulting silence as an unresponsive agent — SIGKILLing healthy coders
and stranding validated work (#6913, #6924).

This is the public seam that owns both, so production and tests drive
the same object. ``run_orchestrator`` holds one and calls it; a test
that wants to prove "the real engine publishes auth and an endpoint
before sessions can launch" drives this rather than reassembling
startup from private helpers — reassembly is precisely what hid the
missing wiring.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..infra.repo_lock import set_lock_http_port

if TYPE_CHECKING:
    from ..ports.agent_callback_endpoint import AgentCallbackEndpoint

logger = logging.getLogger(__name__)


class EngineStartup:
    """Publishes agent-callback auth and the bound endpoint."""

    def __init__(self, *, callback_endpoint: "AgentCallbackEndpoint") -> None:
        self._callback_endpoint = callback_endpoint

    def configure_auth(self, *, dev_no_auth: bool, config: Any) -> None:
        """Activate (or explicitly disable) engine auth before binding.

        Security #5987 F3 — PR 8. The same admin token is shared with the
        Control API so a single login covers both surfaces. ``config`` is
        the loaded ``Config`` instance; its ``browser_session_ttl_seconds``
        / ``sse_token_ttl_seconds`` / ``browser_session_max`` settings are
        threaded into ``browser_session.initialize`` so operator hardening
        under ``ui.browser_session.*`` applies to the dashboard too (#6041
        re-review P2). Without this, the dashboard silently fell back to
        the built-in defaults while the Control API honored the config —
        the same rule enforced differently by path.

        This process serves ``control_app`` mounted under the dashboard
        app, so it configures the Control API tokens too. The
        agent-callback token is resolved once and published to the
        process environment, which ``agent_runner_env`` passes through to
        agent subprocesses: configuring the server without exporting
        would make the API demand a secret its agents never receive.
        """
        from ..infra import browser_session
        from ..infra.api_token import (
            AGENT_CALLBACK_TOKEN_ENV_VAR,
            resolve_agent_callback_token,
            resolve_api_token,
        )
        from .control_api import configure_api_token
        from .web import configure_dashboard_admin_token

        session_ttl = getattr(config, "browser_session_ttl_seconds", None)
        sse_ttl = getattr(config, "sse_token_ttl_seconds", None)
        max_sessions = getattr(config, "browser_session_max", None)

        if dev_no_auth:
            logger.error(
                "⚠  Web Dashboard running with --dev-no-auth: authentication "
                "is DISABLED. Any local process can mutate state. DO NOT use "
                "on a shared host or in production."
            )
            print(
                "\n\033[1;31m"
                "⚠  AUTH DISABLED (--dev-no-auth). Any local process can "
                "mutate dashboard state. Dev only."
                "\033[0m\n",
                flush=True,
            )
            configure_dashboard_admin_token(None)
            configure_api_token(None, agent_callback=None)
            os.environ.pop("ISSUE_ORCHESTRATOR_API_TOKEN", None)
            # Clear the callback token too: agents must not carry a
            # secret the (now open) API no longer enforces.
            os.environ.pop(AGENT_CALLBACK_TOKEN_ENV_VAR, None)
            browser_session.initialize(
                session_ttl_seconds=session_ttl,
                sse_token_ttl_seconds=sse_ttl,
                max_sessions=max_sessions,
            )
            return

        admin_token = resolve_api_token()
        configure_dashboard_admin_token(admin_token)
        agent_callback_token = resolve_agent_callback_token()
        configure_api_token(admin_token, agent_callback=agent_callback_token)
        os.environ[AGENT_CALLBACK_TOKEN_ENV_VAR] = agent_callback_token
        # Derive the HMAC secret from the admin token so a session cookie
        # minted by the Control Center on port 19080 validates here too —
        # one login covers both processes.
        browser_session.initialize(
            admin_token=admin_token,
            session_ttl_seconds=session_ttl,
            sse_token_ttl_seconds=sse_ttl,
            max_sessions=max_sessions,
        )
        os.environ.setdefault("ISSUE_ORCHESTRATOR_API_TOKEN", admin_token)

    def server_started_hook(
        self,
        *,
        repo_root: Path,
        requested_port: int,
        instance_id: str | None,
    ) -> Callable[[int], None]:
        """Build the ``on_server_started`` callback for a bound port.

        Two consumers learn the real port here, and only here — the repo
        lock (so operators and sibling processes can find the dashboard)
        and the agent-callback endpoint (so spawned agents can call
        back).

        The lock write is skipped when the server bound exactly what was
        requested, because the lock already says so. The endpoint publish
        must NOT inherit that condition: with ``port: 0`` every bind is a
        "change", but with an explicit port the agent still needs an
        endpoint. Publishing unconditionally is what makes the two cases
        behave the same.
        """

        def _on_server_started(actual_port: int) -> None:
            self._callback_endpoint.publish_bound_port(actual_port)
            if actual_port != requested_port:
                set_lock_http_port(repo_root, actual_port, instance_id=instance_id)
                logger.info("Updated lock with actual bound port %d", actual_port)

        return _on_server_started
