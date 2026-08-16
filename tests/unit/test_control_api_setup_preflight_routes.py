"""Setup, discovery, log-tail, and preflight control route tests split from test_control_api."""

# ruff: noqa: F403,F405

from tests.unit import test_control_api as _support
from tests.unit.test_control_api import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestDiscoverReposEndpoint:
    def test_default_search_paths_do_not_scan_home_or_parent_when_cwd_is_home(
        self,
        tmp_path: Path,
    ) -> None:
        from issue_orchestrator.observation.instance_detector import default_repo_search_paths

        home = tmp_path / "home"
        home.mkdir()

        paths = default_repo_search_paths(home=home, cwd=home)
        resolved_paths = {str(path) for path in paths}

        assert str(home.resolve()) not in resolved_paths
        assert str(home.parent.resolve()) not in resolved_paths
        assert str((home / "dev").resolve()) in resolved_paths

    def test_default_search_paths_add_cwd_when_outside_common_roots(
        self,
        tmp_path: Path,
    ) -> None:
        from issue_orchestrator.observation.instance_detector import default_repo_search_paths

        home = tmp_path / "home"
        cwd = tmp_path / "elsewhere"
        home.mkdir()
        cwd.mkdir()

        paths = default_repo_search_paths(home=home, cwd=cwd)
        resolved_paths = {str(path) for path in paths}

        assert str(cwd.resolve()) in resolved_paths

    def test_default_search_paths_adds_parent_for_git_repo_outside_common_roots(
        self,
        tmp_path: Path,
    ) -> None:
        from issue_orchestrator.observation.instance_detector import default_repo_search_paths

        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        repo = workspace / "target"
        home.mkdir()
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()

        paths = default_repo_search_paths(home=home, cwd=repo)
        resolved_paths = {str(path) for path in paths}

        assert str(workspace.resolve()) in resolved_paths
        assert str(repo.resolve()) not in resolved_paths

    def test_default_search_paths_do_not_add_extra_context_inside_common_roots(
        self,
        tmp_path: Path,
    ) -> None:
        from issue_orchestrator.observation.instance_detector import default_repo_search_paths

        home = tmp_path / "home"
        repo = home / "dev" / "trustlist"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()

        paths = default_repo_search_paths(home=home, cwd=repo)
        resolved_paths = {str(path) for path in paths}

        assert str((home / "dev").resolve()) in resolved_paths
        assert str(repo.resolve()) not in resolved_paths
        assert len(paths) == 7

    def test_discovers_ready_repo_with_config(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "trustlist"
        repo.mkdir()
        (repo / ".git").mkdir()
        config_dir = (
            repo / ".issue-orchestrator" / "config" / "modes" / "default"
        )
        config_dir.mkdir(parents=True)
        (config_dir / "main.yaml").write_text("repo:\n  name: test/trustlist\n")

        monkeypatch.setattr(
            "issue_orchestrator.infra.repo_registry.load_registry",
            lambda: SimpleNamespace(repos=[]),
        )

        response = supervisor_client.get(
            "/control/repos/discover",
            params={"search_paths": str(tmp_path), "max_depth": 2},
        )

        assert response.status_code == 200
        discovered = response.json()["discovered"]
        assert any(item["name"] == "trustlist" and item["status"] == "ready" for item in discovered)

    def test_dedupes_repo_found_through_overlapping_search_roots(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "workspace"
        repo = workspace / "trustlist"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()
        config_dir = (
            repo / ".issue-orchestrator" / "config" / "modes" / "default"
        )
        config_dir.mkdir(parents=True)
        (config_dir / "main.yaml").write_text("repo:\n  name: test/trustlist\n")

        monkeypatch.setattr(
            "issue_orchestrator.infra.repo_registry.load_registry",
            lambda: SimpleNamespace(repos=[]),
        )

        response = supervisor_client.get(
            "/control/repos/discover",
            params={
                "search_paths": f"{tmp_path},{workspace}",
                "max_depth": 2,
            },
        )

        assert response.status_code == 200
        discovered = response.json()["discovered"]
        trustlist_paths = [
            item["path"] for item in discovered if item["name"] == "trustlist"
        ]
        assert trustlist_paths == [str(repo.resolve())]


class TestSetupPrereqsGitHubAuth:
    def test_build_github_auth_check_uses_repo_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from issue_orchestrator.adapters.github.tokens import TokenValidationResult
        from issue_orchestrator.entrypoints.setup_wizard_common import build_github_auth_check

        cfg = Config()
        cfg.repo = "BruceBGordon/tixmeup"
        cfg.github_token_env = "TIXMEUP_GITHUB_TOKEN"
        cfg.github_keyring_service = "tixmeup-github"
        cfg.github_keyring_username = "bruce"

        seen: dict[str, object] = {}

        def _validate(**kwargs: object) -> TokenValidationResult:
            seen.update(kwargs)
            return TokenValidationResult(valid=False, error="missing repo auth")

        monkeypatch.setattr("issue_orchestrator.execution.providers.validate_github_token", _validate)

        check = build_github_auth_check(cfg)

        assert check == {"ok": False, "detail": "missing repo auth"}
        assert seen["configured_env"] == "TIXMEUP_GITHUB_TOKEN"
        assert seen["configured_keyring_service"] == "tixmeup-github"
        assert seen["configured_keyring_username"] == "bruce"
        assert seen["repo"] == "BruceBGordon/tixmeup"


class TestSupervisorLastFailure:
    """Tests for GET /control/orchestrator/last_failure endpoint."""

    def test_last_failure_returns_none_when_no_file(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return null when no failure file exists."""
        response = supervisor_client.get(
            "/control/orchestrator/last_failure",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        assert response.json()["last_failure"] is None

    def test_last_failure_returns_data_when_file_exists(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return failure data when file exists."""
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        failure_path = state_dir / "last_failure.json"

        failure_data = {
            "phase": "bootstrap",
            "message": "Missing token",
            "suggested_fix": "Set GITHUB_TOKEN",
        }
        with open(failure_path, "w") as f:
            json.dump(failure_data, f)

        response = supervisor_client.get(
            "/control/orchestrator/last_failure",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()["last_failure"]
        assert data["phase"] == "bootstrap"
        assert data["message"] == "Missing token"


class TestSupervisorLogTail:
    """Tests for GET /control/orchestrator/log_tail endpoint."""

    def test_log_tail_returns_empty_when_no_log(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return empty list when no log file exists."""
        response = supervisor_client.get(
            "/control/orchestrator/log_tail",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["lines"] == []
        assert data["total_lines"] == 0

    def test_log_tail_returns_lines_when_log_exists(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return log lines when file exists."""
        log_dir = tmp_path / ".issue-orchestrator" / "state" / "logs"
        log_dir.mkdir(parents=True)
        log_path = log_dir / "orchestrator.log"

        # Write some log lines
        lines = [f"Log line {i}" for i in range(10)]
        with open(log_path, "w") as f:
            f.write("\n".join(lines))

        response = supervisor_client.get(
            "/control/orchestrator/log_tail",
            params={"repo_root": str(tmp_path), "n": 5},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["lines"]) <= 5
        assert data["total_lines"] == 10


class TestSupervisorRejectsNonlocalRepo:
    """Security tests: Supervisor Control API should reject non-local paths."""

    def test_rejects_relative_path(self, supervisor_client: TestClient) -> None:
        """Reject relative paths."""
        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": "../some/path"},
        )

        # Should resolve and check if exists - ../some/path likely doesn't exist
        assert response.status_code == 400

    def test_rejects_empty_path(self, supervisor_client: TestClient) -> None:
        """Reject empty path."""
        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": ""},
        )

        assert response.status_code == 400


# --- Test: Preflight Push Endpoint ---


class TestPreflightPushEndpoint:
    """Tests for POST /api/preflight-push endpoint.

    This endpoint uses GitWorkingCopy.push_preflight() internally, which
    follows the ports & adapters pattern. Tests mock the push_preflight
    method to verify endpoint behavior.
    """

    @pytest.fixture
    def client(self):
        """Create a test client (no orchestrator needed for this endpoint)."""
        return TestClient(control_app)

    def test_rejects_missing_worktree(self, client: TestClient) -> None:
        """Return 400 when worktree is not provided."""
        response = client.post(
            "/api/preflight-push",
            json={},
        )

        assert response.status_code == 400
        assert "worktree is required" in response.json()["error"]

    def test_rejects_nonexistent_worktree(self, client: TestClient) -> None:
        """Return 400 when worktree path does not exist."""
        response = client.post(
            "/api/preflight-push",
            json={"worktree": "/nonexistent/path"},
        )

        assert response.status_code == 400
        assert "does not exist" in response.json()["error"]

    def test_rejects_invalid_json(self, client: TestClient) -> None:
        """Return 400 when body is not valid JSON."""
        response = client.post(
            "/api/preflight-push",
            content="not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["error"]

    def test_returns_success_when_push_would_succeed(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Return would_succeed=True when dry-run push succeeds."""
        from issue_orchestrator.ports.working_copy import PreflightResult

        # Create a fake worktree directory
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.execution.git_working_copy.GitWorkingCopy.push_preflight"
        ) as mock_preflight:
            mock_preflight.return_value = PreflightResult(would_succeed=True)

            response = client.post(
                "/api/preflight-push",
                json={"worktree": str(worktree)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["would_succeed"] is True
        assert data["error"] is None

    def test_returns_failure_with_stale_info_error(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Return would_succeed=False with fix hint for stale info error."""
        from issue_orchestrator.ports.working_copy import PreflightResult

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.execution.git_working_copy.GitWorkingCopy.push_preflight"
        ) as mock_preflight:
            mock_preflight.return_value = PreflightResult(
                would_succeed=False,
                error="error: failed to push (stale info)",
                fix_hint="Branch has diverged. Run: git fetch origin && git rebase origin/main",
            )

            response = client.post(
                "/api/preflight-push",
                json={"worktree": str(worktree)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["would_succeed"] is False
        assert "stale info" in data["error"]
        assert "git fetch" in data["fix_hint"]

    def test_returns_failure_with_rejected_error(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Return would_succeed=False with fix hint for rejected error."""
        from issue_orchestrator.ports.working_copy import PreflightResult

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.execution.git_working_copy.GitWorkingCopy.push_preflight"
        ) as mock_preflight:
            mock_preflight.return_value = PreflightResult(
                would_succeed=False,
                error="! [rejected] branch -> branch (non-fast-forward)",
                fix_hint="Branch has diverged. Run: git fetch origin && git rebase origin/main",
            )

            response = client.post(
                "/api/preflight-push",
                json={"worktree": str(worktree)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["would_succeed"] is False
        assert "rejected" in data["error"]
        assert data["fix_hint"] is not None

    def test_handles_no_branch_detected(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Return error when current branch cannot be determined."""
        from issue_orchestrator.ports.working_copy import PreflightResult

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.execution.git_working_copy.GitWorkingCopy.push_preflight"
        ) as mock_preflight:
            mock_preflight.return_value = PreflightResult(
                would_succeed=False,
                error="Could not determine current branch",
                fix_hint="Ensure you are on a branch, not in detached HEAD state",
            )

            response = client.post(
                "/api/preflight-push",
                json={"worktree": str(worktree)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["would_succeed"] is False
        assert "branch" in data["error"].lower()

    def test_handles_timeout(self, client: TestClient, tmp_path: Path) -> None:
        """Return error when push check times out."""
        from issue_orchestrator.ports.working_copy import PreflightResult

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.execution.git_working_copy.GitWorkingCopy.push_preflight"
        ) as mock_preflight:
            mock_preflight.return_value = PreflightResult(
                would_succeed=False,
                error="Push check timed out",
                fix_hint="Network or remote issue - retry later",
            )

            response = client.post(
                "/api/preflight-push",
                json={"worktree": str(worktree)},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["would_succeed"] is False
        assert "timed out" in data["error"].lower()


# --- Test: Resume Issue Endpoint ---
