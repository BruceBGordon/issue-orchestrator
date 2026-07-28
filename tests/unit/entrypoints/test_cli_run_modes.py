"""Production wiring of the three ``issue-orchestrator start`` modes.

Each mode must resolve the agent-callback endpoint question before the
run loop can launch anything: bind a Control API and publish the bound
port, or declare that it serves none. A mode that does neither leaves
the endpoint unresolved, and every session launch defers forever
(#6924 F7). These modes were previously untested as production wiring,
so that obligation was invisible.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from issue_orchestrator.entrypoints import cli_run_modes
from issue_orchestrator.entrypoints.cli_run_modes import declare_no_control_api
from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)


@pytest.fixture
def orchestrator():
    orch = MagicMock()
    orch.deps.agent_callback_endpoint = RuntimeAgentCallbackEndpoint()
    orch.startup = AsyncMock()
    orch.run_loop = AsyncMock()
    return orch


class TestDeclareNoControlApi:
    def test_declares_when_no_api_port(self, orchestrator) -> None:
        endpoint = orchestrator.deps.agent_callback_endpoint
        assert endpoint.is_ready() is False

        declare_no_control_api(orchestrator, None)

        assert endpoint.is_ready() is True, "launches would defer forever"
        assert endpoint.resolve_port(0) is None

    def test_stays_unresolved_when_an_api_port_is_requested(
        self, orchestrator
    ) -> None:
        """A port was asked for, so the server owes us a publication."""
        declare_no_control_api(orchestrator, 0)
        assert orchestrator.deps.agent_callback_endpoint.is_ready() is False


@pytest.mark.parametrize(
    "mode",
    ["run_no_dashboard", "run_tui_dashboard", "run_web_dashboard_mode"],
)
def test_every_run_mode_resolves_the_endpoint_without_an_api_port(
    orchestrator, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The obligation every mode shares, asserted per mode.

    Driven with ``api_port=None`` so no server is involved: what is
    under test is that the mode answers the endpoint question at all.
    """
    endpoint = orchestrator.deps.agent_callback_endpoint

    # Stop each mode right after its startup wiring. The web mode
    # imports from .web at call time and ends by calling
    # shutdown_manager.exit(), which is os._exit() — left real, it kills
    # the pytest process mid-run and the suite reports success with tests
    # silently unrun. Patch the module it resolves, not this one.
    from issue_orchestrator.entrypoints import web as web_module

    monkeypatch.setattr(
        web_module, "run_with_web_dashboard", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        cli_run_modes, "run_with_dashboard", AsyncMock(return_value=True), raising=False
    )
    monkeypatch.setattr(cli_run_modes, "console", MagicMock(), raising=False)
    config = MagicMock(ui_mode="tui", web_port=8080)
    args = MagicMock(port=8080)

    call = {
        "run_no_dashboard": lambda: cli_run_modes.run_no_dashboard(orchestrator, None),
        "run_tui_dashboard": lambda: cli_run_modes.run_tui_dashboard(
            orchestrator, config, None
        ),
        "run_web_dashboard_mode": lambda: cli_run_modes.run_web_dashboard_mode(
            orchestrator, config, args, None
        ),
    }[mode]

    async def _drive() -> None:
        try:
            await asyncio.wait_for(call(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            # Modes run a loop or need a real dashboard; we only assert
            # on the wiring that happens before that.
            pass

    asyncio.run(_drive())

    assert endpoint.is_ready() is True, (
        f"{mode} left the callback endpoint unresolved; every session "
        "launch would defer"
    )
