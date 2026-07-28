"""``ControlAPIServer.start()`` must not publish an endpoint it isn't serving.

After its readiness loop the server published ``self.port``
unconditionally. A server whose ``serve()`` returned without ever
setting ``started`` therefore reported ready on a fixed port nothing was
listening on, and orchestration happily launched agents against a dead
address (#6924 F9). Failing to bind must fail loudly, before publication.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.entrypoints.control_api import (
    ControlAPIServer,
    get_orchestrator,
    set_orchestrator,
)
from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)


class _FakeServer:
    """Stands in for uvicorn.Server with a controllable outcome."""

    def __init__(self, *, started: bool, raises: Exception | None = None) -> None:
        self.started = started
        self._raises = raises
        self.should_exit = False
        self.servers: list = []

    async def serve(self) -> None:
        if self._raises is not None:
            raise self._raises
        return None


@pytest.fixture
def orchestrator_with_endpoint(monkeypatch: pytest.MonkeyPatch):
    # ``start()`` resolves the admin/callback tokens and ``setdefault``s
    # them into os.environ, which outlives the autouse token-reset
    # fixture and changes what later auth tests resolve. Pin both so
    # nothing is created on disk and nothing leaks.
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_TOKEN", "lifecycle-admin-token-long")
    monkeypatch.setenv(
        "ISSUE_ORCHESTRATOR_AGENT_CALLBACK_TOKEN", "lifecycle-agent-token-long"
    )
    orchestrator = MagicMock()
    endpoint = RuntimeAgentCallbackEndpoint()
    orchestrator.deps.agent_callback_endpoint = endpoint
    orchestrator.config = MagicMock(
        browser_session_ttl_seconds=None,
        sse_token_ttl_seconds=None,
        browser_session_max=None,
    )
    # ``start()`` installs this orchestrator as the control_app module
    # global via set_orchestrator(). Left in place, later tests hitting
    # /api/status try to JSON-serialize a MagicMock and fail with an
    # error that points nowhere near this file.
    previous = get_orchestrator()
    try:
        yield orchestrator, endpoint
    finally:
        set_orchestrator(previous)


@pytest.mark.asyncio
async def test_never_started_server_does_not_publish(
    orchestrator_with_endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, endpoint = orchestrator_with_endpoint
    monkeypatch.setattr(
        "uvicorn.Server", lambda config: _FakeServer(started=False)
    )
    server = ControlAPIServer(orchestrator, port=19999)

    with pytest.raises(RuntimeError, match="did not start"):
        await server.start()

    assert endpoint.is_ready() is False, "reported ready without a live server"
    assert endpoint.bound_port() is None
    assert endpoint.resolve_port(0) is None


@pytest.mark.asyncio
async def test_serve_failure_is_surfaced_and_not_published(
    orchestrator_with_endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind error must reach the operator, not become a silent dead port."""
    orchestrator, endpoint = orchestrator_with_endpoint
    boom = OSError("address already in use")
    monkeypatch.setattr(
        "uvicorn.Server", lambda config: _FakeServer(started=False, raises=boom)
    )
    server = ControlAPIServer(orchestrator, port=19999)

    with pytest.raises(RuntimeError, match="failed to start"):
        await server.start()

    assert endpoint.is_ready() is False
    assert endpoint.bound_port() is None
