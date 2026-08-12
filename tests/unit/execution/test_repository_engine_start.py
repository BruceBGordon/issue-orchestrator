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
from issue_orchestrator.ports.repository_engine_supervisor import (
    MultiInstanceStatus,
    SupervisorStatus,
)


def _selection(mode: str = "codex") -> RepositoryLaunchSelection:
    return RepositoryLaunchSelection.parse(mode=mode, config_name="main.yaml")


def _stopped_supervisor() -> MagicMock:
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(repo_root="")
    supervisor.status.return_value = SupervisorStatus(state="stopped")
    return supervisor


def _prepare_successful_start(
    monkeypatch: pytest.MonkeyPatch,
    selection: RepositoryLaunchSelection,
    launch: Mock,
) -> Mock:
    monkeypatch.setattr(
        "issue_orchestrator.execution.repository_engine_start."
        "inspect_orchestrator_at_port",
        lambda _repo, _port, *, expected_identity: None,
    )
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

    result = StartRepositoryEngineCommand(_stopped_supervisor()).execute(
        RepositoryEngineStartRequest(repo_root=tmp_path, selection=selection)
    )

    assert result.status_code == 200
    assert result.payload["mode"] == "codex"
    assert result.payload["config_name"] == "main.yaml"
    assert result.payload["config_fingerprint"] == "fingerprint"
    assert launch.call_args.kwargs["mode"] == "codex"
    assert launch.call_args.kwargs["config_name"] == "main.yaml"
    persisted.assert_called_once_with(tmp_path, selection)


def test_start_owner_reports_matching_dynamic_multi_instance_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    launch = Mock()
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        expected_count=2,
        instances=[
            SupervisorStatus(
                state="running",
                port=23101,
                instance_id="orchestrator-1",
                configuration_mode="codex",
                config_name="main.yaml",
                config_fingerprint="fingerprint",
            ),
            SupervisorStatus(
                state="running",
                port=23102,
                instance_id="orchestrator-2",
                configuration_mode="codex",
                config_name="main.yaml",
                config_fingerprint="fingerprint",
            ),
        ],
    )
    _prepare_successful_start(monkeypatch, selection, launch)

    result = StartRepositoryEngineCommand(supervisor).execute(
        RepositoryEngineStartRequest(repo_root=tmp_path, selection=selection)
    )

    assert result.status_code == 409
    assert result.payload["error"] == "already_running"
    assert result.payload["ports"] == [23101, 23102]
    assert len(result.payload["instances"]) == 2
    launch.assert_not_called()


def test_start_owner_rejects_conflicting_dynamic_lock_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    launch = Mock()
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[
            SupervisorStatus(
                state="running",
                port=24101,
                instance_id="orchestrator-1",
                configuration_mode="claude",
                config_name="main.yaml",
                config_fingerprint="other-fingerprint",
            )
        ],
    )
    _prepare_successful_start(monkeypatch, selection, launch)

    result = StartRepositoryEngineCommand(supervisor).execute(
        RepositoryEngineStartRequest(repo_root=tmp_path, selection=selection)
    )

    assert result.status_code == 409
    assert result.payload["error"] == "configuration_conflict"
    assert result.payload["ports"] == [24101]
    assert result.payload["active"] == [
        {
            "mode": "claude",
            "config_name": "main.yaml",
            "config_fingerprint": "other-fingerprint",
        }
    ]
    launch.assert_not_called()


def test_start_owner_restarts_tracked_engine_with_repo_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    doctor = SimpleNamespace(to_dict=lambda: {"ok": True})
    launch = Mock(
        return_value=SimpleNamespace(
            status="ok",
            launched=True,
            supervisor={"pid": 123, "port": 24601},
            doctor=doctor,
            error=None,
            conflict=None,
        )
    )
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[
            SupervisorStatus(
                state="running",
                port=24601,
                instance_id="orchestrator-1",
                configuration_mode="codex",
                config_name="main.yaml",
                config_fingerprint="fingerprint",
            )
        ],
    )
    supervisor.stop.return_value = True
    _prepare_successful_start(monkeypatch, selection, launch)
    inspect = Mock(return_value={"port": 24601, "identity_mismatch": {"branch": {}}})
    monkeypatch.setattr(
        "issue_orchestrator.execution.repository_engine_start."
        "inspect_orchestrator_at_port",
        inspect,
    )

    result = StartRepositoryEngineCommand(supervisor).execute(
        RepositoryEngineStartRequest(repo_root=tmp_path, selection=selection)
    )

    assert result.status_code == 200
    inspect.assert_called_once()
    supervisor.stop.assert_called_once_with(
        tmp_path,
        force=True,
        instance_id="orchestrator-1",
        reason="engine identity mismatch detected on repository start",
        actor="control-center",
    )
    launch.assert_called_once()


def test_start_owner_rejects_tracked_repo_identity_drift_when_stop_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    launch = Mock()
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[
            SupervisorStatus(
                state="running",
                port=24701,
                configuration_mode="codex",
                config_name="main.yaml",
                config_fingerprint="fingerprint",
            )
        ],
    )
    supervisor.stop.return_value = False
    _prepare_successful_start(monkeypatch, selection, launch)
    monkeypatch.setattr(
        "issue_orchestrator.execution.repository_engine_start."
        "inspect_orchestrator_at_port",
        Mock(return_value={"port": 24701, "identity_mismatch": {"branch": {}}}),
    )

    result = StartRepositoryEngineCommand(supervisor).execute(
        RepositoryEngineStartRequest(repo_root=tmp_path, selection=selection)
    )

    assert result.status_code == 409
    assert result.payload["error"] == "engine_identity_mismatch"
    assert result.payload["port"] == 24701
    launch.assert_not_called()


def test_start_owner_reuses_config_probe_for_a_tracked_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    launch = Mock()
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[
            SupervisorStatus(
                state="running",
                port=24801,
                configuration_mode="codex",
                config_name="main.yaml",
                config_fingerprint="fingerprint",
            )
        ],
    )
    _prepare_successful_start(monkeypatch, selection, launch)
    direct_inspect = Mock()
    monkeypatch.setattr(
        "issue_orchestrator.execution.repository_engine_start."
        "inspect_orchestrator_at_port",
        direct_inspect,
    )
    monkeypatch.setattr(
        "issue_orchestrator.execution.repository_engine_start."
        "inspect_repository_orchestrator_ownership",
        lambda *_: RepositoryOrchestratorOwnership(
            requested=selection,
            matching=(
                {
                    "port": 24801,
                    "info": {"config_fingerprint": "fingerprint"},
                    "active_selection": selection.to_dict(),
                },
            ),
            conflicting=(),
        ),
    )

    result = StartRepositoryEngineCommand(supervisor).execute(
        RepositoryEngineStartRequest(repo_root=tmp_path, selection=selection)
    )

    assert result.status_code == 409
    assert result.payload["error"] == "already_running"
    direct_inspect.assert_not_called()
    launch.assert_not_called()


def test_start_owner_replenishes_after_stopping_one_stale_tracked_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    doctor = SimpleNamespace(to_dict=lambda: {"ok": True})
    launch = Mock(
        return_value=SimpleNamespace(
            status="ok",
            launched=True,
            supervisor={"instances": []},
            doctor=doctor,
            error=None,
            conflict=None,
        )
    )
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[
            SupervisorStatus(
                state="running",
                port=24901,
                instance_id="orchestrator-1",
                configuration_mode="codex",
                config_name="main.yaml",
                config_fingerprint="fingerprint",
            ),
            SupervisorStatus(
                state="running",
                port=24902,
                instance_id="orchestrator-2",
                configuration_mode="codex",
                config_name="main.yaml",
                config_fingerprint="fingerprint",
            ),
        ],
    )
    supervisor.stop.return_value = True
    _prepare_successful_start(monkeypatch, selection, launch)
    monkeypatch.setattr(
        "issue_orchestrator.execution.repository_engine_start."
        "inspect_orchestrator_at_port",
        Mock(
            side_effect=[
                {"port": 24901},
                {"port": 24902, "identity_mismatch": {"branch": {}}},
            ]
        ),
    )

    result = StartRepositoryEngineCommand(supervisor).execute(
        RepositoryEngineStartRequest(repo_root=tmp_path, selection=selection)
    )

    assert result.status_code == 200
    supervisor.stop.assert_called_once_with(
        tmp_path,
        force=True,
        instance_id="orchestrator-2",
        reason="engine identity mismatch detected on repository start",
        actor="control-center",
    )
    launch.assert_called_once()


def test_force_restart_stops_tracked_dynamic_ports_through_supervisor_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    doctor = SimpleNamespace(to_dict=lambda: {"ok": True})
    launch = Mock(
        return_value=SimpleNamespace(
            status="ok",
            launched=True,
            supervisor={"pid": 123, "port": 25101},
            doctor=doctor,
            error=None,
            conflict=None,
        )
    )
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[
            SupervisorStatus(
                state="running",
                pid=201,
                port=25101,
                instance_id="orchestrator-1",
                configuration_mode="codex",
                config_name="main.yaml",
                config_fingerprint="fingerprint",
            ),
            SupervisorStatus(
                state="running",
                pid=202,
                port=25102,
                instance_id="orchestrator-2",
                configuration_mode="codex",
                config_name="main.yaml",
                config_fingerprint="fingerprint",
            ),
        ],
    )
    supervisor.stop_tracked_instance.return_value = True
    _prepare_successful_start(monkeypatch, selection, launch)

    result = StartRepositoryEngineCommand(supervisor).execute(
        RepositoryEngineStartRequest(
            repo_root=tmp_path,
            selection=selection,
            force_restart=True,
        )
    )

    assert result.status_code == 200
    assert supervisor.stop_tracked_instance.call_count == 2
    supervisor.stop_by_port.assert_not_called()
    launch.assert_called_once()


def test_force_restart_does_not_hide_a_named_instance_stop_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    launch = Mock()
    tracked = [
        SupervisorStatus(
            state="running",
            pid=301,
            port=25201,
            instance_id="orchestrator-1",
            configuration_mode="codex",
            config_name="main.yaml",
            config_fingerprint="fingerprint",
        ),
        SupervisorStatus(
            state="running",
            pid=302,
            port=25202,
            instance_id="orchestrator-2",
            configuration_mode="codex",
            config_name="main.yaml",
            config_fingerprint="fingerprint",
        ),
    ]
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=tracked,
    )
    supervisor.stop_tracked_instance.side_effect = [True, False]
    _prepare_successful_start(monkeypatch, selection, launch)

    result = StartRepositoryEngineCommand(supervisor).execute(
        RepositoryEngineStartRequest(
            repo_root=tmp_path,
            selection=selection,
            force_restart=True,
        )
    )

    assert result.status_code == 500
    assert result.payload["error"] == "stop_failed"
    assert [
        call.args[1] for call in supervisor.stop_tracked_instance.call_args_list
    ] == tracked
    supervisor.stop_all_instances.assert_not_called()
    launch.assert_not_called()


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

    result = StartRepositoryEngineCommand(_stopped_supervisor()).execute(
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

    result = StartRepositoryEngineCommand(_stopped_supervisor()).execute(
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
    other_config = other_repo / ".issue-orchestrator/config/modes/codex/main.yaml"
    other_config.parent.mkdir(parents=True)
    other_config.write_text("agents: {}\n", encoding="utf-8")

    result = StartRepositoryEngineCommand(_stopped_supervisor()).execute(
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
            StartRepositoryEngineCommand(_stopped_supervisor()).execute,
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
