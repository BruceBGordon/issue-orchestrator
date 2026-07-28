"""``EngineStartup`` — what the engine publishes before sessions launch.

An agent can only call back if it holds an accepted credential and was
told a port something is listening on. Both are established here, and
when either silently did not happen every agent callback failed while
looking like an unresponsive agent (#6913, #6924).

This is the public seam production uses, so these tests drive the same
object rather than reassembling startup from private helpers —
reassembly is what hid the missing wiring in the first place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.control_api import (
    configure_api_token,
    get_configured_agent_callback_token,
    get_configured_api_token,
)
from issue_orchestrator.entrypoints.engine_startup import EngineStartup
from issue_orchestrator.entrypoints.web import (
    configure_dashboard_admin_token,
    get_configured_dashboard_admin_token,
)
from issue_orchestrator.infra import browser_session as bs_module
from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)
from issue_orchestrator.infra.api_token import AGENT_CALLBACK_TOKEN_ENV_VAR

ADMIN_TOKEN = "bootstrap-admin-token-that-is-long-enough"


@dataclass
class _FakeConfig:
    browser_session_ttl_seconds: int = 900
    sse_token_ttl_seconds: int = 10
    browser_session_max: int = 7


@pytest.fixture
def endpoint() -> RuntimeAgentCallbackEndpoint:
    return RuntimeAgentCallbackEndpoint()


@pytest.fixture
def startup(endpoint: RuntimeAgentCallbackEndpoint) -> EngineStartup:
    return EngineStartup(callback_endpoint=endpoint)


@pytest.fixture(autouse=True)
def _restore_auth_state():
    prev_dashboard = get_configured_dashboard_admin_token()
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    prev_ttl = bs_module.SESSION_TTL_SECONDS
    prev_sse = bs_module.SSE_TOKEN_TTL_SECONDS
    prev_max = bs_module.MAX_SESSIONS
    yield
    configure_dashboard_admin_token(prev_dashboard)
    configure_api_token(prev_admin, agent_callback=prev_agent)
    bs_module.initialize(
        session_ttl_seconds=prev_ttl,
        sse_token_ttl_seconds=prev_sse,
        max_sessions=prev_max,
    )


class TestConfigureAuth:
    def test_publishes_the_callback_token_agents_inherit(
        self, startup: EngineStartup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Configuring the server is not enough — agents must receive it.

        The variable starts ABSENT deliberately. Pre-seeding it makes
        this vacuous: it cannot tell "the engine published the token"
        from "the token happened to be inherited", which is how the
        missing publication survived a green test (#6924 F2).
        """
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_TOKEN", ADMIN_TOKEN)
        monkeypatch.delenv(AGENT_CALLBACK_TOKEN_ENV_VAR, raising=False)

        startup.configure_auth(dev_no_auth=False, config=_FakeConfig())

        configured = get_configured_agent_callback_token()
        assert configured, "engine must configure an agent-callback token"
        assert os.environ.get(AGENT_CALLBACK_TOKEN_ENV_VAR) == configured, (
            "the API demands a secret its agents never receive"
        )

    def test_configures_the_shared_admin_token(
        self, startup: EngineStartup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_TOKEN", ADMIN_TOKEN)
        startup.configure_auth(dev_no_auth=False, config=_FakeConfig())
        assert get_configured_api_token() == ADMIN_TOKEN
        assert get_configured_dashboard_admin_token() == ADMIN_TOKEN

    def test_honours_operator_browser_session_config(
        self, startup: EngineStartup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ui.browser_session.*`` must apply here, not just on the CC."""
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_TOKEN", ADMIN_TOKEN)
        startup.configure_auth(dev_no_auth=False, config=_FakeConfig())
        assert bs_module.SESSION_TTL_SECONDS == 900
        assert bs_module.SSE_TOKEN_TTL_SECONDS == 10
        assert bs_module.MAX_SESSIONS == 7

    def test_dev_no_auth_clears_the_callback_token(
        self, startup: EngineStartup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agents must not carry a secret the now-open API does not enforce."""
        monkeypatch.setenv(AGENT_CALLBACK_TOKEN_ENV_VAR, "stale-agent-token")
        startup.configure_auth(dev_no_auth=True, config=_FakeConfig())
        assert get_configured_agent_callback_token() is None
        assert AGENT_CALLBACK_TOKEN_ENV_VAR not in os.environ

    def test_dev_no_auth_still_honours_browser_session_config(
        self, startup: EngineStartup
    ) -> None:
        startup.configure_auth(dev_no_auth=True, config=_FakeConfig())
        assert bs_module.SESSION_TTL_SECONDS == 900
        assert bs_module.MAX_SESSIONS == 7


class TestServerStartedHook:
    """Closes the loop: the endpoint being correct is useless unpopulated."""

    @staticmethod
    def _hook(startup: EngineStartup, monkeypatch, requested_port: int, tmp_path: Path):
        from issue_orchestrator.entrypoints import engine_startup as module

        lock_writes: list[int] = []
        monkeypatch.setattr(
            module,
            "set_lock_http_port",
            lambda _root, port, instance_id=None: lock_writes.append(port),
        )
        hook = startup.server_started_hook(
            repo_root=tmp_path, requested_port=requested_port, instance_id="test"
        )
        return hook, lock_writes

    def test_auto_assign_publishes_the_bound_port(
        self,
        startup: EngineStartup,
        endpoint: RuntimeAgentCallbackEndpoint,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        hook, lock_writes = self._hook(startup, monkeypatch, 0, tmp_path)
        hook(59957)
        assert endpoint.bound_port() == 59957
        assert lock_writes == [59957]

    def test_publishes_even_when_the_requested_port_was_bound_exactly(
        self,
        startup: EngineStartup,
        endpoint: RuntimeAgentCallbackEndpoint,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The lock write is skipped here; the endpoint publish must not be.

        The lock already records the requested port, so it needs no
        update — but the agent still needs an endpoint. Inheriting that
        guard would leave explicit-port deployments with no callback.
        """
        hook, lock_writes = self._hook(startup, monkeypatch, 8080, tmp_path)
        hook(8080)
        assert endpoint.bound_port() == 8080
        assert lock_writes == [], "lock should not be rewritten with the same port"

    def test_agents_can_resolve_the_port_only_after_the_hook_runs(
        self,
        startup: EngineStartup,
        endpoint: RuntimeAgentCallbackEndpoint,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The consumer-visible effect the whole chain depends on."""
        hook, _ = self._hook(startup, monkeypatch, 0, tmp_path)
        assert endpoint.resolve_port(0) is None
        hook(59957)
        assert endpoint.resolve_port(0) == 59957
