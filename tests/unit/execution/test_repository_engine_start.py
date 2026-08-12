"""Behavior tests for the shared Repository Engine start owner."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.execution.control_center_actions import (
    SelectLaunchConfigurationCommand,
    SelectLaunchConfigurationRequest,
)
from issue_orchestrator.execution.control_center_runtime import (
    RepositoryOrchestratorOwnership,
)
from issue_orchestrator.execution.repository_engine_start import (
    RepositoryEngineStartRequest,
    StartRepositoryEngineCommand,
)


def _selection(mode: str = "codex") -> RepositoryLaunchSelection:
    return RepositoryLaunchSelection.parse(mode=mode, config_name="main.yaml")


def _prepare_successful_start(
    monkeypatch: pytest.MonkeyPatch,
    selection: RepositoryLaunchSelection,
    launch: Mock,
) -> Mock:
    monkeypatch.setattr(
        "issue_orchestrator.execution.repository_engine_start."
        "inspect_repository_orchestrator_ownership",
        lambda _repo, _selection: RepositoryOrchestratorOwnership(
            requested=selection,
            matching=(),
            conflicting=(),
        ),
    )
    monkeypatch.setattr(
        "issue_orchestrator.execution.repository_engine_start.build_repo_identity",
        lambda _repo: SimpleNamespace(to_dict=lambda: {"root": "/repo"}),
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.config.Config.load",
        lambda _path: SimpleNamespace(config_fingerprint="fingerprint"),
    )
    monkeypatch.setattr("issue_orchestrator.infra.launcher.launch_subprocess", launch)
    persisted = Mock(return_value=True)
    monkeypatch.setattr(
        "issue_orchestrator.infra.repo_registry.record_launched_selection",
        persisted,
    )
    return persisted


def test_start_owner_persists_the_exact_launched_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    doctor = SimpleNamespace(to_dict=lambda: {"ok": True})
    launch = Mock(
        return_value=SimpleNamespace(
            status="ok",
            launched=True,
            supervisor={"pid": 123, "port": 19090},
            doctor=doctor,
            error=None,
            conflict=None,
        )
    )
    persisted = _prepare_successful_start(monkeypatch, selection, launch)

    result = StartRepositoryEngineCommand(MagicMock()).execute(
        RepositoryEngineStartRequest(repo_root=tmp_path, selection=selection)
    )

    assert result.status_code == 200
    assert result.payload["mode"] == "codex"
    assert result.payload["config_name"] == "main.yaml"
    assert result.payload["config_fingerprint"] == "fingerprint"
    assert launch.call_args.kwargs["mode"] == "codex"
    assert launch.call_args.kwargs["config_name"] == "main.yaml"
    persisted.assert_called_once_with(tmp_path, selection)


def test_start_owner_rejects_maintenance_config_as_engine_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection("default")
    maintenance = (
        tmp_path / ".issue-orchestrator/config/maintenance/hooks-validate.yaml"
    )
    maintenance.parent.mkdir(parents=True)
    maintenance.write_text("agents: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "issue_orchestrator.execution.repository_engine_start."
        "inspect_repository_orchestrator_ownership",
        lambda _repo, _selection: RepositoryOrchestratorOwnership(
            requested=selection,
            matching=(),
            conflicting=(),
        ),
    )

    result = StartRepositoryEngineCommand(MagicMock()).execute(
        RepositoryEngineStartRequest(
            repo_root=tmp_path,
            selection=selection,
            config_path=maintenance,
        )
    )

    assert result.status_code == 400
    assert result.payload["error"] == "invalid_config_path"


def test_start_owner_rejects_maintenance_symlink_as_external_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection("default")
    external = tmp_path / "external.yaml"
    external.write_text("agents: {}\n", encoding="utf-8")
    maintenance = (
        tmp_path / ".issue-orchestrator/config/maintenance/hooks-validate.yaml"
    )
    maintenance.parent.mkdir(parents=True)
    maintenance.symlink_to(external)
    monkeypatch.setattr(
        "issue_orchestrator.execution.repository_engine_start."
        "inspect_repository_orchestrator_ownership",
        lambda _repo, _selection: RepositoryOrchestratorOwnership(
            requested=selection,
            matching=(),
            conflicting=(),
        ),
    )

    result = StartRepositoryEngineCommand(MagicMock()).execute(
        RepositoryEngineStartRequest(
            repo_root=tmp_path,
            selection=selection,
            config_path=maintenance,
        )
    )

    assert result.status_code == 400
    assert result.payload["error"] == "invalid_config_path"


def test_start_owner_rejects_config_owned_by_another_repository(
    tmp_path: Path,
) -> None:
    requested_repo = tmp_path / "requested"
    other_repo = tmp_path / "other"
    selection = _selection()
    other_config = (
        other_repo / ".issue-orchestrator/config/modes/codex/main.yaml"
    )
    other_config.parent.mkdir(parents=True)
    other_config.write_text("agents: {}\n", encoding="utf-8")

    result = StartRepositoryEngineCommand(MagicMock()).execute(
        RepositoryEngineStartRequest(
            repo_root=requested_repo,
            selection=selection,
            config_path=other_config,
        )
    )

    assert result.status_code == 400
    assert result.payload["error"] == "configuration_repository_mismatch"


@pytest.mark.asyncio
async def test_start_and_selection_change_share_one_mutation_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    selection = _selection("default")
    doctor = SimpleNamespace(to_dict=lambda: {"ok": True})

    def blocking_launch(**_kwargs: object) -> SimpleNamespace:
        started.set()
        assert release.wait(timeout=2)
        return SimpleNamespace(
            status="ok",
            launched=True,
            supervisor={"pid": 123, "port": 19090},
            doctor=doctor,
            error=None,
            conflict=None,
        )

    persisted = _prepare_successful_start(
        monkeypatch,
        selection,
        Mock(side_effect=blocking_launch),
    )
    start_task = asyncio.create_task(
        asyncio.to_thread(
            StartRepositoryEngineCommand(MagicMock()).execute,
            RepositoryEngineStartRequest(repo_root=tmp_path, selection=selection),
        )
    )
    assert await asyncio.to_thread(started.wait, 2)

    select_result = await SelectLaunchConfigurationCommand(MagicMock()).execute(
        SelectLaunchConfigurationRequest(
            repo_root=tmp_path,
            selection=_selection("codex"),
        )
    )
    release.set()
    start_result = await start_task

    assert select_result.status_code == 409
    assert select_result.payload["error"] == "engine_running"
    assert start_result.status_code == 200
    persisted.assert_called_once_with(tmp_path, selection)
