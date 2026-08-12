"""Tests for instance detection module."""

import json
from unittest.mock import patch, MagicMock

from issue_orchestrator.observation.instance_detector import (
    DashboardStatus,
    RepoStatus,
    SystemState,
    discover_repos,
    detect_system_state,
    get_best_entry_point,
    write_dashboard_pid,
    clear_dashboard_pid,
    _is_orchestrator_codebase,
    _get_config_status,
)
from issue_orchestrator.infra.repo_registry import RegisteredRepo
from issue_orchestrator.infra.supervisor import MultiInstanceStatus, SupervisorStatus


class TestDashboardStatus:
    """Tests for DashboardStatus dataclass."""

    def test_to_dict_running(self):
        status = DashboardStatus(
            running=True, pid=1234, port=19080, started_at="2024-01-01T00:00:00Z"
        )
        result = status.to_dict()
        assert result == {
            "running": True,
            "pid": 1234,
            "port": 19080,
            "started_at": "2024-01-01T00:00:00Z",
        }

    def test_to_dict_not_running(self):
        status = DashboardStatus(running=False)
        result = status.to_dict()
        assert result == {
            "running": False,
            "pid": None,
            "port": None,
            "started_at": None,
        }


class TestRepoStatus:
    """Tests for RepoStatus dataclass."""

    def test_to_dict_running_orchestrator(self):
        status = RepoStatus(
            path="/home/user/repo",
            name="repo",
            config_status="ready",
            orchestrator_state="running",
            orchestrator_pid=5678,
            orchestrator_port=8080,
            configs=["default.yaml"],
            selected_config="default.yaml",
            is_current_dir=True,
        )
        result = status.to_dict()
        assert result["path"] == "/home/user/repo"
        assert result["orchestrator_state"] == "running"
        assert result["orchestrator_port"] == 8080
        assert result["is_current_dir"] is True

    def test_to_dict_stopped_orchestrator(self):
        status = RepoStatus(
            path="/home/user/repo",
            name="repo",
            config_status="needs_setup",
            orchestrator_state="stopped",
        )
        result = status.to_dict()
        assert result["orchestrator_state"] == "stopped"
        assert result["orchestrator_pid"] is None


class TestSystemState:
    """Tests for SystemState dataclass."""

    def test_to_dict(self):
        dashboard = DashboardStatus(running=False)
        repo = RepoStatus(
            path="/home/user/repo",
            name="repo",
            config_status="ready",
            orchestrator_state="stopped",
        )
        state = SystemState(
            dashboard=dashboard,
            repos=[repo],
            current_directory="/home/user",
            is_orchestrator_codebase=False,
            cwd_is_git_repo=True,
        )
        result = state.to_dict()
        assert result["dashboard"]["running"] is False
        assert len(result["repos"]) == 1
        assert result["current_directory"] == "/home/user"
        assert result["is_orchestrator_codebase"] is False
        assert result["cwd_is_git_repo"] is True


class TestIsOrchestratorCodebase:
    """Tests for _is_orchestrator_codebase function."""

    def test_returns_true_for_orchestrator_codebase(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "issue-orchestrator"\n')
        assert _is_orchestrator_codebase(tmp_path) is True

    def test_returns_false_for_other_project(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "some-other-project"\n')
        assert _is_orchestrator_codebase(tmp_path) is False

    def test_returns_false_for_no_pyproject(self, tmp_path):
        assert _is_orchestrator_codebase(tmp_path) is False


class TestGetConfigStatus:
    """Tests for _get_config_status function."""

    def test_ready_with_configs(self, tmp_path):
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("repo: test")

        with patch(
            "issue_orchestrator.observation.instance_detector.list_configs",
            return_value=["default.yaml"],
        ):
            status, modes, mode_configs = _get_config_status(tmp_path)
        assert status == "ready"
        assert modes == ["default"]
        assert mode_configs == {"default": ["default.yaml"]}

    def test_legacy_config(self, tmp_path):
        (tmp_path / ".issue-orchestrator.yaml").write_text("repo: test")

        with patch(
            "issue_orchestrator.observation.instance_detector.list_configs",
            return_value=[],
        ):
            status, modes, mode_configs = _get_config_status(tmp_path)
        assert status == "legacy"
        assert modes == []
        assert mode_configs == {}

    def test_needs_setup(self, tmp_path):
        with patch(
            "issue_orchestrator.observation.instance_detector.list_configs",
            return_value=[],
        ):
            status, modes, mode_configs = _get_config_status(tmp_path)
        assert status == "needs_setup"
        assert modes == []
        assert mode_configs == {}


class TestGetBestEntryPoint:
    """Tests for get_best_entry_point function."""

    def test_returns_open_dashboard_when_running(self):
        state = SystemState(
            dashboard=DashboardStatus(running=True, port=19080),
            repos=[],
            current_directory="/tmp",
        )
        entry = get_best_entry_point(state)
        assert entry["action"] == "open_dashboard"
        assert entry["url"] == "http://127.0.0.1:19080/"
        assert entry["port"] == 19080

    def test_returns_start_dashboard_with_deep_link_for_running_repo(self):
        """When orchestrator running but no dashboard, start dashboard with deep-link."""
        state = SystemState(
            dashboard=DashboardStatus(running=False),
            repos=[
                RepoStatus(
                    path="/home/user/repo",
                    name="repo",
                    config_status="ready",
                    orchestrator_state="running",
                    orchestrator_port=8080,
                    is_current_dir=True,
                ),
            ],
            current_directory="/home/user/repo",
        )
        with patch(
            "issue_orchestrator.observation.instance_detector._find_available_dashboard_port",
            return_value=19080,
        ):
            entry = get_best_entry_point(state)
        # Always goes through unified dashboard, with deep-link to active repo
        assert entry["action"] == "start_dashboard"
        assert entry["port"] == 19080
        assert entry["repo_path"] == "/home/user/repo"

    def test_open_dashboard_with_deep_link_when_orchestrator_running(self):
        """When dashboard running and orchestrator running, deep-link to repo."""
        state = SystemState(
            dashboard=DashboardStatus(running=True, port=19080),
            repos=[
                RepoStatus(
                    path="/home/user/repo",
                    name="repo",
                    config_status="ready",
                    orchestrator_state="running",
                    orchestrator_port=8080,
                    is_current_dir=True,
                ),
            ],
            current_directory="/home/user/repo",
        )
        entry = get_best_entry_point(state)
        assert entry["action"] == "open_dashboard"
        assert entry["url"] == "http://127.0.0.1:19080/?repo=%2Fhome%2Fuser%2Frepo"
        assert entry["repo_path"] == "/home/user/repo"

    def test_open_dashboard_uses_codespaces_forwarded_url(self, monkeypatch):
        """Browser URLs should use forwarded Codespaces links when available."""
        monkeypatch.setenv("CODESPACE_NAME", "octo-space")
        monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
        state = SystemState(
            dashboard=DashboardStatus(running=True, port=19080),
            repos=[],
            current_directory="/tmp",
        )

        entry = get_best_entry_point(state)

        assert entry["action"] == "open_dashboard"
        assert entry["url"] == "https://octo-space-19080.app.github.dev/"

    def test_returns_start_dashboard_when_nothing_running(self):
        state = SystemState(
            dashboard=DashboardStatus(running=False),
            repos=[],
            current_directory="/tmp",
        )
        with patch(
            "issue_orchestrator.observation.instance_detector._find_available_dashboard_port",
            return_value=19080,
        ):
            entry = get_best_entry_point(state)
        assert entry["action"] == "start_dashboard"
        assert entry["port"] == 19080

    def test_returns_start_dashboard_with_fallback_port_when_default_unavailable(self):
        state = SystemState(
            dashboard=DashboardStatus(running=False),
            repos=[],
            current_directory="/tmp",
        )
        with patch(
            "issue_orchestrator.observation.instance_detector._find_available_dashboard_port",
            return_value=19081,
        ):
            entry = get_best_entry_point(state)
        assert entry["action"] == "start_dashboard"
        assert entry["port"] == 19081


class TestDashboardPidFile:
    """Tests for dashboard PID file functions."""

    def test_write_and_clear_dashboard_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "dashboard.pid"
        monkeypatch.setattr(
            "issue_orchestrator.observation.instance_detector.DASHBOARD_PID_FILE",
            pid_file,
        )

        # Write PID file
        write_dashboard_pid(19080)
        assert pid_file.exists()

        data = json.loads(pid_file.read_text())
        assert data["port"] == 19080
        assert "pid" in data
        assert "started_at" in data

        # Clear PID file
        clear_dashboard_pid()
        assert not pid_file.exists()

    def test_clear_nonexistent_pid_file(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "dashboard.pid"
        monkeypatch.setattr(
            "issue_orchestrator.observation.instance_detector.DASHBOARD_PID_FILE",
            pid_file,
        )
        # Should not raise
        clear_dashboard_pid()


class TestDetectSystemState:
    """Tests for detect_system_state function."""

    def test_detects_current_directory(self, tmp_path):
        (tmp_path / ".git").mkdir()

        with patch(
            "issue_orchestrator.observation.instance_detector.load_registry"
        ) as mock_registry:
            mock_registry.return_value.repos = []
            with patch(
                "issue_orchestrator.observation.instance_detector._read_dashboard_pid"
            ) as mock_dash:
                mock_dash.return_value = DashboardStatus(running=False)
                with patch(
                    "issue_orchestrator.observation.instance_detector.list_configs",
                    return_value=[],
                ):
                    with patch(
                        "issue_orchestrator.observation.instance_detector.supervisor"
                    ) as mock_sup:
                        mock_sup.status.return_value = MagicMock(state="stopped")
                        state = detect_system_state(tmp_path)

        assert state.current_directory == str(tmp_path)
        assert state.cwd_is_git_repo is True

    def test_detects_orchestrator_codebase(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "issue-orchestrator"\n'
        )

        with patch(
            "issue_orchestrator.observation.instance_detector.load_registry"
        ) as mock_registry:
            mock_registry.return_value.repos = []
            with patch(
                "issue_orchestrator.observation.instance_detector._read_dashboard_pid"
            ) as mock_dash:
                mock_dash.return_value = DashboardStatus(running=False)
                state = detect_system_state(tmp_path)

        assert state.is_orchestrator_codebase is True
        # Orchestrator codebase should not be added as a repo
        assert len(state.repos) == 0

    def test_reports_active_identity_separately_from_registry_selection(
        self,
        tmp_path,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        registered = RegisteredRepo(
            path=str(repo),
            selected_mode="default",
            selected_config="main.yaml",
        )
        active = SupervisorStatus(
            state="running",
            pid=42,
            port=19090,
            instance_id="orchestrator-1",
            configuration_mode="codex",
            config_name="other.yaml",
            config_fingerprint="codex-fingerprint",
        )

        with (
            patch(
                "issue_orchestrator.observation.instance_detector.load_registry"
            ) as registry,
            patch(
                "issue_orchestrator.observation.instance_detector._read_dashboard_pid",
                return_value=DashboardStatus(running=False),
            ),
            patch(
                "issue_orchestrator.observation.instance_detector.supervisor.status_all_instances",
                return_value=MultiInstanceStatus(
                    repo_root=str(repo),
                    instances=[active],
                ),
            ),
            patch(
                "issue_orchestrator.observation.instance_detector._check_if_paused",
                return_value=False,
            ),
        ):
            registry.return_value.repos = [registered]
            payload = detect_system_state(tmp_path).repos[0].to_dict()

        assert payload["selected_mode"] == "default"
        assert payload["selected_config"] == "main.yaml"
        assert payload["active_mode"] == "codex"
        assert payload["active_config"] == "other.yaml"
        assert payload["active_config_fingerprint"] == "codex-fingerprint"
        assert payload["active_instances"] == [active.to_dict()]


class TestDiscoverRepos:
    def test_discovery_reports_directory_backed_modes(
        self,
        tmp_path,
    ) -> None:
        repo = tmp_path / "project"
        (repo / ".git").mkdir(parents=True)
        config = repo / ".issue-orchestrator/config/modes/codex/main.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("agents: {}\n", encoding="utf-8")

        with patch(
            "issue_orchestrator.observation.instance_detector.load_registry"
        ) as registry:
            registry.return_value.repos = []
            discovered = discover_repos([tmp_path], max_depth=1)

        assert discovered == [
            {
                "path": str(repo.resolve()),
                "name": "project",
                "configs": [],
                "modes": ["codex"],
                "mode_configs": {"codex": ["main.yaml"]},
                "status": "ready",
            }
        ]
