"""Production wiring of the three ``issue-orchestrator start`` modes.

Each mode must resolve the agent-callback endpoint before the run loop
can launch anything: bind a Control API and publish the bound port, or
declare that it serves none. A mode doing neither leaves the endpoint
unresolved and every session launch defers forever (#6924 F7).

These modes had no production-wiring test, which is why F7 went
unnoticed. The tests drive each mode with explicit fakes at the real
import boundary, let unexpected exceptions fail, and assert the
collaborators were actually reached — an earlier version swallowed every
exception and asserted only state written before the crash, so a mode
could die immediately after and stay green (F12).
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from issue_orchestrator.entrypoints import cli_run_modes
from issue_orchestrator.entrypoints.cli_run_modes import declare_no_control_api
from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.execution.control_center_runtime import (
    RepositoryOrchestratorOwnership,
)
from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)

BOUND_PORT = 54321


def _close_coroutine(coro: object) -> None:
    if inspect.iscoroutine(coro):
        coro.close()


class _FakeControlAPIServer:
    """Stands in for the real server, publishing like it does."""

    def __init__(self, orchestrator, port: int) -> None:
        self._orchestrator = orchestrator
        self.port = BOUND_PORT if port == 0 else port
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True
        self._orchestrator.deps.agent_callback_endpoint.publish_bound_port(self.port)

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def orchestrator(tmp_path: Path):
    """A stub whose ``repo_root`` is a REAL path.

    A MagicMock here is not merely imprecise: the modes do real path
    arithmetic on ``config.repo_root``, so the mock's repr became a
    filename and four 77KB SQLite databases were committed at the
    repository root (F11).
    """
    orch = MagicMock()
    orch.config.repo_root = tmp_path
    orch.deps.agent_callback_endpoint = RuntimeAgentCallbackEndpoint()
    orch.startup = AsyncMock()
    orch.run_loop = AsyncMock()
    orch.close = MagicMock()
    return orch


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch):
    """Patch the class at the module each mode imports it from."""
    created: list[_FakeControlAPIServer] = []

    def _factory(orchestrator, port):
        server = _FakeControlAPIServer(orchestrator, port)
        created.append(server)
        return server

    monkeypatch.setattr(
        "issue_orchestrator.entrypoints.control_api_server.ControlAPIServer",
        _factory,
    )
    return created


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
        """A port was asked for, so the server still owes a publication."""
        declare_no_control_api(orchestrator, 0)
        assert orchestrator.deps.agent_callback_endpoint.is_ready() is False


def test_locked_cli_start_persists_exact_selection_after_lock_publication(
    tmp_path: Path,
    orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = RepositoryLaunchSelection.parse(
        mode="codex",
        config_name="main.yaml",
    )
    config = MagicMock(
        repo_root=tmp_path,
        launch_selection=selection,
        configuration_mode="codex",
        config_name="main.yaml",
        config_fingerprint="effective-fingerprint",
        control_api_port=None,
        web_port=8080,
        ui_mode="web",
    )
    args = MagicMock(no_dashboard=True, api_port=None, port=8080)
    acquire = MagicMock()
    release = MagicMock()
    record = MagicMock()
    monkeypatch.setattr(
        "issue_orchestrator.execution.control_center_runtime."
        "inspect_repository_orchestrator_ownership",
        lambda _repo, requested: RepositoryOrchestratorOwnership(
            requested=requested,
            matching=(),
            conflicting=(),
        ),
    )
    monkeypatch.setattr("issue_orchestrator.infra.repo_lock.is_locked", lambda _: False)
    monkeypatch.setattr("issue_orchestrator.infra.repo_lock.acquire_lock", acquire)
    monkeypatch.setattr("issue_orchestrator.infra.repo_lock.release_lock", release)
    monkeypatch.setattr(
        "issue_orchestrator.infra.repo_lock.repository_lifecycle_mutation",
        lambda _repo: nullcontext(),
    )
    monkeypatch.setattr(
        "issue_orchestrator.execution.repository_engine_start."
        "record_repository_engine_launch",
        record,
    )
    monkeypatch.setattr(cli_run_modes.asyncio, "run", _close_coroutine)

    result = cli_run_modes.run_locked_cli_engine(
        args,
        config,
        MagicMock(return_value=orchestrator),
    )

    assert result == 0
    acquire.assert_called_once_with(
        tmp_path,
        None,
        configuration_mode="codex",
        config_name="main.yaml",
        config_fingerprint="effective-fingerprint",
    )
    record.assert_called_once_with(tmp_path, selection)
    release.assert_called_once_with(tmp_path)


class TestNoDashboardMode:
    """``--no-dashboard``: the path F7's probe originally exercised."""

    def test_declares_and_runs_when_no_api_port(self, orchestrator) -> None:
        asyncio.run(cli_run_modes.run_no_dashboard(orchestrator, None))

        endpoint = orchestrator.deps.agent_callback_endpoint
        assert endpoint.is_ready() is True
        assert endpoint.resolve_port(0) is None
        orchestrator.startup.assert_awaited_once()
        orchestrator.run_loop.assert_awaited_once()

    def test_auto_assigned_port_is_published_and_server_stopped(
        self, orchestrator, fake_server
    ) -> None:
        """``api_port=0`` — the production path F7 missed.

        The agent must be handed the port the server actually bound, not
        the auto-assign request.
        """
        asyncio.run(cli_run_modes.run_no_dashboard(orchestrator, 0))

        assert len(fake_server) == 1
        assert fake_server[0].started is True
        assert fake_server[0].stopped is True, "server leaked"
        endpoint = orchestrator.deps.agent_callback_endpoint
        assert endpoint.is_ready() is True
        assert endpoint.resolve_port(0) == BOUND_PORT
        orchestrator.run_loop.assert_awaited_once()

    def test_startup_failure_propagates(self, orchestrator) -> None:
        """Failures must surface, not be swallowed by the test harness."""
        orchestrator.startup = AsyncMock(side_effect=RuntimeError("startup boom"))

        with pytest.raises(RuntimeError, match="startup boom"):
            asyncio.run(cli_run_modes.run_no_dashboard(orchestrator, None))


class TestTuiDashboardMode:
    def test_declares_and_runs_the_dashboard(
        self, orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dashboard = AsyncMock(return_value=True)
        # ``run_tui_dashboard`` imports this locally from .dashboard, so
        # patching cli_run_modes has no effect.
        monkeypatch.setattr(
            "issue_orchestrator.entrypoints.dashboard.run_with_dashboard", dashboard
        )
        config = MagicMock(ui_mode="tui")

        result = asyncio.run(cli_run_modes.run_tui_dashboard(orchestrator, config, None))

        assert result is True
        dashboard.assert_awaited_once()
        assert orchestrator.deps.agent_callback_endpoint.is_ready() is True
        orchestrator.startup.assert_awaited_once()

    def test_auto_assigned_port_is_published(
        self, orchestrator, fake_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "issue_orchestrator.entrypoints.dashboard.run_with_dashboard",
            AsyncMock(return_value=True),
        )
        config = MagicMock(ui_mode="tui")

        asyncio.run(cli_run_modes.run_tui_dashboard(orchestrator, config, 0))

        endpoint = orchestrator.deps.agent_callback_endpoint
        assert endpoint.resolve_port(0) == BOUND_PORT
        assert fake_server[0].stopped is True


class TestWebDashboardMode:
    def test_declares_and_runs_the_web_dashboard(
        self, orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        web_runner = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "issue_orchestrator.entrypoints.web.run_with_web_dashboard", web_runner
        )
        monkeypatch.setattr(cli_run_modes, "console", MagicMock(), raising=False)
        config = MagicMock(web_port=8080)
        args = MagicMock(port=8080)

        asyncio.run(
            cli_run_modes.run_web_dashboard_mode(orchestrator, config, args, None)
        )

        web_runner.assert_awaited_once()
        assert orchestrator.deps.agent_callback_endpoint.is_ready() is True

    def test_auto_assigned_port_is_published(
        self, orchestrator, fake_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "issue_orchestrator.entrypoints.web.run_with_web_dashboard",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(cli_run_modes, "console", MagicMock(), raising=False)
        config = MagicMock(web_port=8080)
        args = MagicMock(port=8080)

        asyncio.run(
            cli_run_modes.run_web_dashboard_mode(orchestrator, config, args, 0)
        )

        endpoint = orchestrator.deps.agent_callback_endpoint
        assert endpoint.resolve_port(0) == BOUND_PORT
        assert fake_server[0].stopped is True
