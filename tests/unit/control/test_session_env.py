"""The agent session environment contract (``control/session_env``).

Focus: the callback port an agent is handed. ``control_api_port: 0``
means "bind any free port", and the supervised engine never writes the
bound port back into ``Config`` — so every consumer reading
``config.control_api_port`` handed agents a ``0`` they could not dial,
and every agent callback failed (#6913, #6924).

Tested through the public ``build_session_env_exports`` seam with a real
``Config`` and real ``Path`` values, so these assertions hold for every
launch path that goes through it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from issue_orchestrator.control.session_env import (
    api_port_export,
    build_session_env_exports,
)
from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)
from issue_orchestrator.infra.config import Config

_PORT_EXPORT = re.compile(r"ISSUE_ORCHESTRATOR_API_PORT='(\d+)'")


@pytest.fixture
def endpoint() -> RuntimeAgentCallbackEndpoint:
    """A fresh endpoint per test — nothing bound, like a new process."""
    return RuntimeAgentCallbackEndpoint()


def _exports(
    config: Config, tmp_path: Path, endpoint: RuntimeAgentCallbackEndpoint
) -> str:
    return build_session_env_exports(
        config=config,
        completion_path=".issue-orchestrator/completion.json",
        session_id="coding-1",
        agent_label="agent:backend",
        issue_number=6410,
        run_dir=tmp_path / "run",
        worktree_path=tmp_path,
        callback_endpoint=endpoint,
    )


def _exported_port(exports: str) -> str | None:
    match = _PORT_EXPORT.search(exports)
    return match.group(1) if match else None


class TestCallbackPortInSessionEnv:
    def test_bound_port_is_exported_when_config_requests_auto_assign(
        self, tmp_path: Path, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        """The production shape: config says 0, the server bound a real port.

        This is the case the whole bug lived in — the agent must get the
        port that is actually listening, not the auto-assign request.
        """
        endpoint.publish_bound_port(59957)
        assert _exported_port(_exports(Config(control_api_port=0), tmp_path, endpoint)) == "59957"

    def test_auto_assign_with_nothing_bound_exports_no_port(
        self, tmp_path: Path, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        """Before the server binds there is genuinely no endpoint.

        Omitting beats exporting ``0``: a consumer sees "unset" and fails
        honestly rather than dialling ``localhost:0``.
        """
        exports = _exports(Config(control_api_port=0), tmp_path, endpoint)
        assert "ISSUE_ORCHESTRATOR_API_PORT" not in exports

    def test_bound_port_wins_over_configured_port(
        self, tmp_path: Path, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        """What is listening beats what was requested."""
        endpoint.publish_bound_port(59957)
        assert _exported_port(_exports(Config(control_api_port=8080), tmp_path, endpoint)) == "59957"

    def test_explicit_configured_port_used_before_anything_binds(
        self, tmp_path: Path, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        assert _exported_port(_exports(Config(control_api_port=8080), tmp_path, endpoint)) == "8080"


class TestApiPortExportRendering:
    """The rendered fragment, so callers can rely on its exact shape."""

    def test_renders_quoted_assignment_with_leading_space(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        endpoint.publish_bound_port(59957)
        assert api_port_export(0, endpoint) == " ISSUE_ORCHESTRATOR_API_PORT='59957'"

    def test_renders_empty_when_there_is_no_endpoint(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        assert api_port_export(0, endpoint) == ""


def test_session_env_still_carries_the_other_launch_contract(
    tmp_path: Path, endpoint: RuntimeAgentCallbackEndpoint
) -> None:
    """Guard the extraction: the rest of the contract must be intact."""
    exports = _exports(Config(control_api_port=0), tmp_path, endpoint)
    for expected in (
        "ISSUE_ORCHESTRATOR_COMPLETION_PATH=",
        "ISSUE_ORCHESTRATOR_SESSION_ID='coding-1'",
        "ISSUE_ORCHESTRATOR_AGENT_LABEL='agent:backend'",
        "ISSUE_ORCHESTRATOR_ISSUE_NUMBER='6410'",
        "ISSUE_ORCHESTRATOR_WORKTREE=",
        "PYTHONPATH=",
        "PATH=",
    ):
        assert expected in exports, f"missing {expected}"


class TestLaunchGateOnEndpointReadiness:
    """The launcher must not spawn an agent before the endpoint is known.

    Reproduces the production gap F7 found: the CLI start modes bound a
    Control API but never published it, so an agent could be launched
    into an environment with no callback. The gate makes publication a
    precondition rather than a hope.
    """

    def test_endpoint_starts_unready(
        self, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        assert endpoint.is_ready() is False

    def test_unready_endpoint_exports_no_port(
        self, tmp_path: Path, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        """The state the gate exists to prevent an agent from ever seeing."""
        exports = _exports(Config(control_api_port=0), tmp_path, endpoint)
        assert "ISSUE_ORCHESTRATOR_API_PORT" not in exports

    def test_declaring_unavailable_is_ready_with_no_port(
        self, tmp_path: Path, endpoint: RuntimeAgentCallbackEndpoint
    ) -> None:
        """A no-Control-API deployment must launch, just without a port."""
        endpoint.declare_unavailable()
        assert endpoint.is_ready() is True
        exports = _exports(Config(control_api_port=0), tmp_path, endpoint)
        assert "ISSUE_ORCHESTRATOR_API_PORT" not in exports
