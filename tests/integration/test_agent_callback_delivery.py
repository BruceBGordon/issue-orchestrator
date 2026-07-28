"""End-to-end: can an agent actually deliver a callback? (#6913, #6924)

Every prior test of this path asserted a piece — the allowlist, the
token, the port rule — and the pieces all passed while the whole was
broken. This test starts from the production shape:

    control_api_port = 0        (auto-assign; the real deployment)
    nothing bound yet
    no agent-callback token in the environment

then boots a real server on an auto-assigned port, builds the agent
environment through the same public seam session launch uses, and makes
a real HTTP request with exactly the port and token that environment
carries. If any link regresses — the surface rejects agent tokens, the
engine never publishes one, or the port is the 0 sentinel — this fails.
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

import pytest
import urllib.error
import urllib.request

from issue_orchestrator.control.session_env import build_session_env_exports
from issue_orchestrator.entrypoints.control_api import (
    configure_api_token,
    get_configured_agent_callback_token,
    get_configured_api_token,
)
from issue_orchestrator.entrypoints.run_orchestrator import _configure_dashboard_auth
from issue_orchestrator.entrypoints.web import (
    app,
    configure_dashboard_admin_token,
    get_configured_dashboard_admin_token,
)
from issue_orchestrator.infra.agent_callback_endpoint import (
    record_bound_callback_port,
    reset_bound_callback_port,
)
from issue_orchestrator.infra.api_token import AGENT_CALLBACK_TOKEN_ENV_VAR
from issue_orchestrator.infra.config import Config

_PORT_EXPORT = re.compile(r"ISSUE_ORCHESTRATOR_API_PORT='(\d+)'")

# Routes an agent must be able to reach with its scoped token. Each is
# POSTed with an empty body: we assert on the auth verdict only, so any
# non-401/403 status (400 bad payload, 503 no orchestrator) is a pass.
AGENT_CALLBACK_PATHS = (
    "/api/review-exchange/respond",
    "/api/preflight-push",
    "/api/issues/6410/resume",
)


class _LiveServer:
    """A real uvicorn server on an auto-assigned port."""

    def __init__(self) -> None:
        self.port: int | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> int:
        import uvicorn

        ready = threading.Event()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            # port=0 mirrors the deployment: the OS picks the port and
            # only the running server knows which one.
            config = uvicorn.Config(
                app, host="127.0.0.1", port=0, log_level="error", access_log=False
            )
            self._server = uvicorn.Server(config)

            async def _serve() -> None:
                task = self._loop.create_task(self._server.serve())
                while not self._server.started:
                    await asyncio.sleep(0.01)
                self.port = self._server.servers[0].sockets[0].getsockname()[1]
                ready.set()
                await task

            self._loop.run_until_complete(_serve())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        if not ready.wait(timeout=30):
            raise RuntimeError("server did not start within 30s")
        assert self.port is not None
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15)


@pytest.fixture
def live_server():
    server = _LiveServer()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def clean_auth_state(monkeypatch: pytest.MonkeyPatch):
    """Start from a process that has never seen a callback token."""
    prev_dashboard = get_configured_dashboard_admin_token()
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    reset_bound_callback_port()
    # Absent, not pre-seeded — pre-seeding is what let the earlier test
    # pass while the engine published nothing (#6924 F2).
    monkeypatch.delenv(AGENT_CALLBACK_TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv(
        "ISSUE_ORCHESTRATOR_API_TOKEN", "integration-admin-token-long-enough"
    )
    try:
        yield
    finally:
        configure_dashboard_admin_token(prev_dashboard)
        configure_api_token(prev_admin, agent_callback=prev_agent)
        reset_bound_callback_port()


def _post(url: str, token: str) -> int:
    request = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.mark.integration
def test_agent_callback_is_deliverable_from_the_generated_environment(
    live_server: _LiveServer,
    clean_auth_state: None,
    tmp_path: Path,
) -> None:
    # 1. Engine startup: resolves and publishes the callback token.
    class _Cfg:
        browser_session_ttl_seconds = 900
        sse_token_ttl_seconds = 10
        browser_session_max = 7

    _configure_dashboard_auth(dev_no_auth=False, config=_Cfg())

    import os

    token = os.environ.get(AGENT_CALLBACK_TOKEN_ENV_VAR)
    assert token, (
        "engine startup must publish the agent-callback token to the "
        "environment agents inherit"
    )

    # 2. The server binds an auto-assigned port and records it.
    bound_port = live_server.start()
    record_bound_callback_port(bound_port)

    # 3. Session launch builds the agent environment — config still 0.
    exports = build_session_env_exports(
        config=Config(control_api_port=0),
        completion_path=".issue-orchestrator/completion.json",
        session_id="coding-1",
        agent_label="agent:backend",
        issue_number=6410,
        run_dir=tmp_path / "run",
        worktree_path=tmp_path,
    )
    match = _PORT_EXPORT.search(exports)
    assert match, f"no callback port in the agent environment: {exports}"
    agent_port = int(match.group(1))
    assert agent_port == bound_port, (
        f"agent was handed port {agent_port}, server is on {bound_port}"
    )

    # 4. The agent's own port + token must reach every callback route.
    for path in AGENT_CALLBACK_PATHS:
        status = _post(f"http://127.0.0.1:{agent_port}{path}", token)
        assert status not in (401, 403), (
            f"agent callback rejected on {path}: HTTP {status} — "
            "a verdict sent here would be undeliverable"
        )


@pytest.mark.integration
def test_agent_token_still_cannot_reach_admin_routes(
    live_server: _LiveServer,
    clean_auth_state: None,
) -> None:
    """The fix must not turn the scoped token into an admin credential."""

    class _Cfg:
        browser_session_ttl_seconds = 900
        sse_token_ttl_seconds = 10
        browser_session_max = 7

    _configure_dashboard_auth(dev_no_auth=False, config=_Cfg())

    import os

    token = os.environ[AGENT_CALLBACK_TOKEN_ENV_VAR]
    port = live_server.start()

    for path in ("/api/shutdown", "/api/resume", "/api/issues/not-an-int/resume"):
        status = _post(f"http://127.0.0.1:{port}{path}", token)
        assert status in (401, 403), (
            f"{path} accepted the agent-callback token (HTTP {status})"
        )
