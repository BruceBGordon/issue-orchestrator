"""Unit tests for command-backed Control Center actions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from issue_orchestrator.adapters.github.http_client import GitHubHttpError
from issue_orchestrator.execution.control_center_actions import (
    AuditActionRequest,
    AuditIssuesCommand,
    ConfiguredRepoActionRequest,
    ControlCenterActions,
    DoctorActionRequest,
    DoctorCommand,
    InitializeLabelsCommand,
    ListStaleWorktreesCommand,
    PauseOrchestratorCommand,
    RefreshActionRequest,
    RefreshOrchestratorCommand,
    RepoActionRequest,
    SelectLaunchConfigurationCommand,
    SelectLaunchConfigurationRequest,
    TraceActionRequest,
    TraceIssueCommand,
)
from issue_orchestrator.control.worktree_reconciliation import (
    WorktreeActivityEvidence,
    WorktreeAuditOwner,
)
from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.infra.repo_lock import acquire_lock, release_lock
from issue_orchestrator.infra.supervisor import MultiInstanceStatus, SupervisorStatus
from issue_orchestrator.ports.worktree_manager import (
    RegisteredWorktree,
    ReviewerHeadOwnership,
)
from issue_orchestrator.execution.control_center_worktree_audit import (
    ControlCenterWorktreeAuditOwner,
)


@pytest.mark.asyncio
async def test_pause_command_returns_not_running() -> None:
    supervisor = MagicMock()
    supervisor.status.return_value = SupervisorStatus(state="stopped")
    cmd = PauseOrchestratorCommand(supervisor)

    result = await cmd.execute(RepoActionRequest(repo_root=Path("/tmp/repo")))

    assert result.status_code == 400
    assert result.payload["error"] == "not_running"
    assert result.payload["state"] == "stopped"


@pytest.mark.asyncio
async def test_select_launch_configuration_rejects_live_owner(
    tmp_path: Path,
) -> None:
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[SupervisorStatus(state="unknown")],
    )

    result = await SelectLaunchConfigurationCommand(supervisor).execute(
        SelectLaunchConfigurationRequest(
            repo_root=tmp_path,
            selection=RepositoryLaunchSelection.parse(
                mode="codex",
                config_name="main.yaml",
            ),
        )
    )

    assert result.status_code == 409
    assert result.payload["error"] == "engine_running"
    assert result.payload["states"] == ["unknown"]


@pytest.mark.asyncio
async def test_select_launch_configuration_allows_failed_stale_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[SupervisorStatus(state="failed")],
    )
    monkeypatch.setattr(
        "issue_orchestrator.execution.control_center_runtime.detect_repository_orchestrators",
        lambda _repo: [],
    )
    monkeypatch.setattr(
        "issue_orchestrator.infra.config.list_configs",
        lambda _repo, _mode: ["main.yaml"],
    )
    persisted: list[RepositoryLaunchSelection] = []
    monkeypatch.setattr(
        "issue_orchestrator.infra.repo_registry.set_selected_launch_selection",
        lambda _repo, selection: persisted.append(selection) or True,
    )
    selection = RepositoryLaunchSelection.parse(
        mode="codex",
        config_name="main.yaml",
    )

    result = await SelectLaunchConfigurationCommand(supervisor).execute(
        SelectLaunchConfigurationRequest(repo_root=tmp_path, selection=selection)
    )

    assert result.status_code == 200
    assert persisted == [selection]


@pytest.mark.asyncio
async def test_select_launch_configuration_rejects_nonselected_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path)
    )
    monkeypatch.setattr(
        "issue_orchestrator.execution.control_center_runtime.detect_repository_orchestrators",
        lambda _repo: [
            {
                "port": 19090,
                "probed_selection": {
                    "mode": "claude",
                    "config_name": "other.yaml",
                },
            }
        ],
    )

    result = await SelectLaunchConfigurationCommand(supervisor).execute(
        SelectLaunchConfigurationRequest(
            repo_root=tmp_path,
            selection=RepositoryLaunchSelection.parse(
                mode="codex",
                config_name="main.yaml",
            ),
        )
    )

    assert result.status_code == 409
    assert result.payload["ports"] == [19090]


@pytest.mark.asyncio
async def test_select_launch_configuration_is_atomic_with_engine_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = MagicMock()
    persisted = Mock(return_value=True)
    monkeypatch.setattr(
        "issue_orchestrator.infra.repo_registry.set_selected_launch_selection",
        persisted,
    )
    info = acquire_lock(
        tmp_path,
        configuration_mode="default",
        config_name="main.yaml",
        config_fingerprint="fingerprint",
    )
    try:
        result = await SelectLaunchConfigurationCommand(supervisor).execute(
            SelectLaunchConfigurationRequest(
                repo_root=tmp_path,
                selection=RepositoryLaunchSelection.parse(
                    mode="codex",
                    config_name="main.yaml",
                ),
            )
        )
    finally:
        release_lock(tmp_path, pid=info.pid)

    assert result.status_code == 409
    assert result.payload["error"] == "engine_running"
    persisted.assert_not_called()
    supervisor.status_all_instances.assert_not_called()


def test_effective_selection_prefers_active_cli_lock(tmp_path: Path) -> None:
    supervisor = MagicMock()
    supervisor.status_all_instances.return_value = MultiInstanceStatus(
        repo_root=str(tmp_path),
        instances=[
            SupervisorStatus(
                state="running",
                configuration_mode="codex",
                config_name="main.yaml",
                config_fingerprint="fingerprint",
            )
        ],
    )

    selection = ControlCenterActions(supervisor).effective_launch_selection(tmp_path)

    assert selection.to_dict() == {
        "mode": "codex",
        "config_name": "main.yaml",
    }


@pytest.mark.asyncio
async def test_doctor_missing_target_config_never_falls_back_to_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd_repo = tmp_path / "control-center-cwd"
    cwd_config = cwd_repo / ".issue-orchestrator/config/modes/default/default.yaml"
    cwd_config.parent.mkdir(parents=True)
    cwd_config.write_text(
        "repo:\n  github:\n    app:\n      private_key_path: /secret/key.pem\n",
        encoding="utf-8",
    )
    target_repo = tmp_path / "target"
    target_repo.mkdir()
    expected_path = (
        target_repo / ".issue-orchestrator/config/modes/default/default.yaml"
    )
    captured: dict[str, object] = {}

    def fake_doctor(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(to_dict=lambda: {"overall": "warning"})

    monkeypatch.chdir(cwd_repo)
    monkeypatch.setattr("issue_orchestrator.infra.doctor.run_doctor", fake_doctor)

    result = await DoctorCommand().execute(
        DoctorActionRequest(
            repo_root=target_repo,
            selection=RepositoryLaunchSelection.default(),
        )
    )

    assert result.status_code == 200
    assert captured["config"] is None
    assert captured["config_path"] == expected_path
    assert captured["config_path"] != cwd_config


@pytest.mark.asyncio
async def test_trace_command_scopes_to_last_start(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    log_dir = repo_root / ".issue-orchestrator" / "state" / "logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "orchestrator.log"
    log_file.write_text(
        "\n".join([
            "old issue=4070 before startup",
            "Starting orchestrator",
            "tick issue=4070 in current run",
            "noise line",
        ]),
    )
    cmd = TraceIssueCommand()

    result = await cmd.execute(TraceActionRequest(repo_root=repo_root, issue_number=4070))

    assert result.status_code == 200
    assert result.payload["entries"] == ["tick issue=4070 in current run"]
    assert result.payload["total"] == 1


@pytest.mark.asyncio
async def test_pause_command_uses_async_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = MagicMock()
    supervisor.status.return_value = SupervisorStatus(state="running", port=18080)
    cmd = PauseOrchestratorCommand(supervisor)

    calls: dict[str, bool] = {"pause": False, "close": False}

    class FakeAsyncApi:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def pause(self) -> dict[str, str]:
            calls["pause"] = True
            return {"status": "paused"}

        async def close(self) -> None:
            calls["close"] = True

    monkeypatch.setattr(
        "issue_orchestrator.execution.control_center_actions.OrchestratorAsyncHttpApi",
        FakeAsyncApi,
    )

    result = await cmd.execute(RepoActionRequest(repo_root=Path("/tmp/repo")))

    assert result.status_code == 200
    assert result.payload == {"status": "paused"}
    assert calls == {"pause": True, "close": True}


@pytest.mark.asyncio
async def test_refresh_command_forwards_inflight_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = MagicMock()
    supervisor.status.return_value = SupervisorStatus(state="running", port=18080)
    cmd = RefreshOrchestratorCommand(supervisor)

    captured: dict[str, list[str] | None] = {"ids": None}

    class FakeAsyncApi:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def refresh(self, inflight_stable_ids: list[str]) -> dict[str, str]:
            captured["ids"] = inflight_stable_ids
            return {"status": "refresh_requested"}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "issue_orchestrator.execution.control_center_actions.OrchestratorAsyncHttpApi",
        FakeAsyncApi,
    )

    result = await cmd.execute(
        RefreshActionRequest(repo_root=Path("/tmp/repo"), inflight_stable_ids=["I_1", "I_2"]),
    )

    assert result.status_code == 200
    assert result.payload == {"status": "refresh_requested"}
    assert captured["ids"] == ["I_1", "I_2"]


@pytest.mark.asyncio
async def test_stale_worktrees_fallback_without_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "trustlist"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    managed = tmp_path / "trustlist-4070"
    reviewer = tmp_path / "trustlist-4070-review-20260812T010203123456Z"
    for path in (managed, reviewer):
        marker = path / ".issue-orchestrator" / "worktree-id"
        marker.parent.mkdir(parents=True)
        marker.write_text("wt-owned", encoding="utf-8")

    # Force fallback mode (no config available).
    monkeypatch.setattr(
        "issue_orchestrator.execution.control_center_runtime.load_config_for_selection",
        lambda repo_root, selection: (_ for _ in ()).throw(FileNotFoundError()),
    )

    fake_git = SimpleNamespace(
        list_registered=lambda _repo: (
            RegisteredWorktree(managed, "a" * 40, "4070-fix"),
            RegisteredWorktree(reviewer, "a" * 40, None),
        ),
        can_remove_without_user_changes=lambda _path: True,
        read_reviewer_head_ownership=lambda _path: ReviewerHeadOwnership(
            marker_present=False,
            expected_head=None,
        ),
    )
    activity_reader = SimpleNamespace(
        read=lambda _repo, _selection: WorktreeActivityEvidence.known(set()),
    )

    cmd = ListStaleWorktreesCommand(
        ControlCenterWorktreeAuditOwner(
            WorktreeAuditOwner(fake_git),
            activity_reader,
        )
    )
    result = await cmd.execute(
        ConfiguredRepoActionRequest(
            repo_root=repo_root,
            selection=RepositoryLaunchSelection.default(),
        )
    )

    assert result.status_code == 200
    assert result.payload["scope"] == "repo-parent-fallback"
    paths = [entry["path"] for entry in result.payload["stale_worktrees"]]
    assert paths == [str(reviewer)]
    assert {entry["path"] for entry in result.payload["worktrees"]} == {
        str(managed),
        str(reviewer),
    }
    assert "cleanup_command" not in result.payload


@pytest.mark.asyncio
async def test_worktree_audit_uses_selected_config_and_retains_active_disposables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "project"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    selected_base = tmp_path / "selected-worktrees"
    selected_base.mkdir()
    reviewer = selected_base / "project-41-review-20260812T010203123456Z"
    scratch = selected_base / "project-tech-lead-42-abcdef123456"
    for path in (reviewer, scratch):
        marker = path / ".issue-orchestrator" / "worktree-id"
        marker.parent.mkdir(parents=True)
        marker.write_text("wt-owned", encoding="utf-8")

    selection = RepositoryLaunchSelection.parse(
        mode="codex",
        config_name="second.yaml",
    )
    selected_config = SimpleNamespace(
        worktree_base=selected_base,
        tech_lead_enabled=False,
        cleanup=SimpleNamespace(
            without_tech_lead=SimpleNamespace(remove_worktrees=True),
        ),
    )
    loaded: list[tuple[Path, RepositoryLaunchSelection]] = []
    monkeypatch.setattr(
        "issue_orchestrator.execution.control_center_runtime.load_config_for_selection",
        lambda repo, requested: loaded.append((repo, requested)) or selected_config,
    )
    fake_git = SimpleNamespace(
        list_registered=lambda _repo: (
            RegisteredWorktree(reviewer, "a" * 40, None),
            RegisteredWorktree(
                scratch,
                "b" * 40,
                "tech-lead-investigation-42-abcdef123456",
            ),
        ),
        can_remove_without_user_changes=lambda _path: True,
    )
    activity_reader = SimpleNamespace(
        read=lambda _repo, requested: (
            WorktreeActivityEvidence.known({reviewer, scratch})
            if requested == selection
            else WorktreeActivityEvidence.unknown()
        ),
    )
    command = ListStaleWorktreesCommand(
        ControlCenterWorktreeAuditOwner(
            WorktreeAuditOwner(fake_git),
            activity_reader,
        )
    )

    result = await command.execute(
        ConfiguredRepoActionRequest(repo_root=repo_root, selection=selection)
    )

    assert loaded == [(repo_root, selection)]
    assert result.payload["issue_cleanup_enabled"] is True
    assert result.payload["activity_evidence"] == "known"
    assert result.payload["cleanup_candidates"] == []
    assert {entry["kind"] for entry in result.payload["worktrees"]} == {
        "reviewer",
        "tech_lead_scratch",
    }
    assert {entry["disposition"] for entry in result.payload["worktrees"]} == {
        "retained"
    }


@pytest.mark.asyncio
async def test_initialize_labels_uses_loaded_config_for_repository_host() -> None:
    config = Mock()
    config.repo = "owner/repo"
    config.agents = {"agent:backend": Mock()}

    client = Mock()
    client.list_labels.return_value = []

    label_manager = Mock(
        in_progress="in-progress",
        blocked="blocked",
        needs_human="needs-human",
        tech_lead_needs_human="tech-lead-needs-human",
    )
    label_manager.repository_initialization_labels.return_value = [
        "in-progress",
        "blocked",
        "needs-human",
        "tech-lead-needs-human",
        "priority:high",
        "priority:medium",
        "priority:low",
        "agent:backend",
    ]

    with patch(
        "issue_orchestrator.execution.control_center_runtime.load_config_for_selection",
        return_value=config,
    ):
        with patch("issue_orchestrator.control.label_manager.LabelManager", return_value=label_manager):
            with patch(
                "issue_orchestrator.execution.providers.create_repository_host",
                return_value=client,
            ) as mock_create_host:
                result = await InitializeLabelsCommand().execute(
                    ConfiguredRepoActionRequest(
                        repo_root=Path("/tmp/repo"),
                        selection=RepositoryLaunchSelection.default(),
                    ),
                )

    assert result.status_code == 200
    mock_create_host.assert_called_once_with("owner/repo", config=config)
    client.create_label.assert_any_call("tech-lead-needs-human", force=True)


@pytest.mark.asyncio
async def test_audit_command_reports_repository_host_error(tmp_path: Path) -> None:
    config = Mock(repo="owner/repo", repo_root=tmp_path)
    working_copy = Mock()
    working_copy.list_remote_branches.return_value = []
    upstream_error = GitHubHttpError(
        "GitHub unavailable",
        status_code=503,
        response_text='{"message":"GitHub search is degraded"}',
    )

    with patch(
        "issue_orchestrator.execution.control_center_runtime.load_config_for_selection",
        return_value=config,
    ):
        with patch(
            "issue_orchestrator.execution.providers.create_repository_host",
            return_value=Mock(),
        ):
            with patch(
                "issue_orchestrator.execution.git_working_copy.GitWorkingCopy",
                return_value=working_copy,
            ):
                with patch(
                    "issue_orchestrator.infra.analysis.extract_issue_branches",
                    return_value={},
                ):
                    with patch(
                        "issue_orchestrator.infra.audit.audit_queue",
                        side_effect=upstream_error,
                    ):
                        result = await AuditIssuesCommand().execute(
                            AuditActionRequest(
                                repo_root=tmp_path,
                                selection=RepositoryLaunchSelection.default(),
                            ),
                        )

    assert result.status_code == 502
    assert result.payload["error"] == "GitHub issue query failed"
    assert result.payload["error_code"] == "github_http_error"
    assert result.payload["upstream_status_code"] == 503
    assert "GitHub search is degraded" in result.payload["detail"]
