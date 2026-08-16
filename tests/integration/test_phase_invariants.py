"""Tests for phase invariants.

These tests verify that the phase implementation meets its defining invariants,
not just that some related files exist.

Phase invariants:
- Phase 1: Cannot start a second orchestrator for same repo; stale lock recovers.
- Phase 2: Control API endpoints work even when orchestrator is not running.
- Phase 3: UI served by control plane, not by a running orchestrator.
- Phase 4: Failures are structured and visible via control endpoints when orchestrator is down.
- Phase 5: AI diagnose creates analysis-only report without credentials.
- Phase 6: AST guardrails enforce subprocess boundaries.
- Phase 7: Supervisor manages multiple repos from one control center.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.control_api import control_app
from issue_orchestrator.infra.repo_lock import acquire_lock, release_lock, AlreadyRunning
from issue_orchestrator.ports.repository_setup import (
    RepositorySetupGitHubVerification,
)


def _complete_setup_payload(
    repo_root: Path,
    **overrides: object,
) -> dict[str, object]:
    payload = {
        "repo_root": str(repo_root),
        "repo_name": "test/repo",
        "worker_agent_label": "agent:dev",
        "model": "sonnet",
        "effort": "high",
        "configure_reviewer": True,
        "reviewer_model": "sonnet",
        "reviewer_effort": "high",
        "configure_internal_reviewer": False,
        "internal_review_max_rounds": 5,
        "internal_review_instructions": ".io/internal-review.md",
        "validation_quick_command": "make test-quick",
        "validation_publish_command": "make validate",
        "github_authorization": {
            "kind": "detected",
            "api_url": "https://api.github.com",
            "http_timeout_seconds": 20,
        },
        "configure_tech_lead": True,
        "tech_lead_model": "sonnet",
        "tech_lead_effort": "high",
        "tech_lead_review_threshold": 1,
    }
    payload.update(overrides)
    return payload


class TestPhase1LockInvariant:
    """Phase 1: Cannot start a second orchestrator for same repo; stale lock recovers."""

    def test_cannot_acquire_lock_twice(self, tmp_path: Path) -> None:
        """Cannot start a second orchestrator for same repo."""
        import os

        # First lock succeeds (uses current PID)
        info = acquire_lock(tmp_path, port=8080)
        assert info.pid == os.getpid()

        # Second lock fails because we're still "running"
        with pytest.raises(AlreadyRunning) as exc_info:
            acquire_lock(tmp_path, port=8081)

        assert exc_info.value.pid == os.getpid()
        assert exc_info.value.port == 8080

        # Cleanup
        release_lock(tmp_path)

    def test_stale_lock_recovers(self, tmp_path: Path) -> None:
        """Stale lock from dead process is recovered."""
        import os
        from issue_orchestrator.infra.repo_lock import _write_lock, LockInfo
        from issue_orchestrator.infra.repo_identity import lock_file

        # Manually write a lock with a non-existent PID
        fake_lock = LockInfo(
            repo_root=str(tmp_path),
            pid=999999,  # Non-existent PID
            started_at="2024-01-01T00:00:00+00:00",
            http_port=8080,
            state_dir=str(tmp_path / ".issue-orchestrator" / "state"),
        )
        _write_lock(lock_file(tmp_path), fake_lock)

        # acquire_lock should recover from stale lock
        info = acquire_lock(tmp_path, port=8081)
        assert info.pid == os.getpid()
        assert info.recovered is True

        release_lock(tmp_path)


class TestPhase2ControlAPIWithoutOrchestrator:
    """Phase 2: Control API status works when orchestrator is not running."""

    def test_status_works_without_orchestrator(self, tmp_path: Path) -> None:
        """GET /control/orchestrator/status works without a running orchestrator."""
        client = TestClient(control_app)

        response = client.get(
            "/control/orchestrator/status",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["state"] in ("stopped", "unknown", "failed")

    def test_repos_list_works_without_orchestrator(self) -> None:
        """GET /control/repos works without any orchestrator running."""
        client = TestClient(control_app)

        response = client.get("/control/repos")

        assert response.status_code == 200
        data = response.json()
        assert "repos" in data


class TestPhase3ControlCenterUI:
    """Phase 3: UI served by control plane, not by a running orchestrator."""

    def test_control_center_ui_served_by_control_api(self) -> None:
        """Unified dashboard UI is served at / by control_api."""
        client = TestClient(control_app)

        response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        # New unified dashboard has "Issue Orchestrator" branding
        assert "Issue Orchestrator" in response.text

    def test_control_center_ui_contains_repo_management(self) -> None:
        """Unified dashboard has repo management functionality."""
        client = TestClient(control_app)

        response = client.get("/")
        script_response = client.get("/static/js/control_center.js")

        assert response.status_code == 200
        assert script_response.status_code == 200
        # UI should have start/stop functionality
        assert "startRepo" in script_response.text
        assert "stopRepo" in script_response.text
        # UI should have repository loading
        assert "loadRepos" in script_response.text

    def test_sidebar_navigation_present(self) -> None:
        """Unified dashboard has sidebar navigation with key views."""
        client = TestClient(control_app)

        response = client.get("/")
        html = response.text

        # Sidebar should have navigation items
        assert 'class="sidebar"' in html, "Sidebar not found"
        assert 'data-view="repositories"' in html, "Repositories nav item not found"
        assert 'data-view="tools"' in html, "Tools nav item not found"
        assert 'data-view="settings"' in html, "Settings nav item not found"

    def test_theme_support_present(self) -> None:
        """Unified dashboard has theme support (dark/light)."""
        client = TestClient(control_app)

        response = client.get("/")
        html = response.text

        # Theme selector should be present
        assert 'class="theme-selector"' in html, "Theme selector not found"
        assert 'data-theme="light"' in html, "Light theme option not found"
        assert 'data-theme="dark"' in html, "Dark theme option not found"


class TestPhase4FailureVisibility:
    """Phase 4: Failures visible via control endpoints when orchestrator is down."""

    def test_last_failure_endpoint_exists(self, tmp_path: Path) -> None:
        """GET /control/orchestrator/last_failure endpoint exists."""
        client = TestClient(control_app)

        response = client.get(
            "/control/orchestrator/last_failure",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        # No failure should return empty
        assert data.get("last_failure") is None or "message" in data.get("last_failure", {})

    def test_doctor_endpoint_exists(self, tmp_path: Path) -> None:
        """GET /control/orchestrator/doctor endpoint exists."""
        client = TestClient(control_app)

        response = client.get(
            "/control/orchestrator/doctor",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert "overall" in data or "error" in data


class TestPhase5AIDiagnoseNoCredentials:
    """Phase 5: AI diagnose does not receive credentials."""

    def test_get_safe_env_strips_credentials(self) -> None:
        """_get_safe_env strips all credential environment variables."""
        from issue_orchestrator.infra.ai_diagnose import _get_safe_env

        with patch.dict(
            "os.environ",
            {
                "GITHUB_TOKEN": "secret1",
                "GH_TOKEN": "secret2",
                "OPENAI_API_KEY": "secret3",
                "ISSUE_ORCH_GITHUB_TOKEN": "secret4",
                "PATH": "/usr/bin",
                "HOME": "/home/user",
            },
        ):
            safe_env = _get_safe_env()

        assert "GITHUB_TOKEN" not in safe_env
        assert "GH_TOKEN" not in safe_env
        assert "OPENAI_API_KEY" not in safe_env
        assert "ISSUE_ORCH_GITHUB_TOKEN" not in safe_env
        assert safe_env.get("PATH") == "/usr/bin"
        assert safe_env.get("HOME") == "/home/user"


class TestPhase7MultiRepoFromControlCenter:
    """Phase 7: Supervisor manages multiple repos from one control center."""

    def test_discover_repos_endpoint_exists(self, tmp_path: Path) -> None:
        """GET /control/repos/discover endpoint exists and returns discovered repos."""
        client = TestClient(control_app)

        # Create a test repo with config and .git directory
        test_repo = tmp_path / "test-repo"
        test_repo.mkdir()
        (test_repo / ".git").mkdir()  # Discover endpoint only finds git repos
        config_dir = (
            test_repo
            / ".issue-orchestrator"
            / "config"
            / "modes"
            / "default"
        )
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("repo:\n  name: test/repo\n")

        response = client.get(
            "/control/repos/discover",
            params={"search_paths": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert "discovered" in data

        # Should find our test repo
        discovered_paths = [r["path"] for r in data["discovered"]]
        assert str(test_repo.resolve()) in discovered_paths

    def test_can_register_multiple_repos(self, tmp_path: Path) -> None:
        """Can register multiple repos via control API."""
        from issue_orchestrator.infra.repo_registry import (
            load_registry,
            save_registry,
            RepoRegistry,
        )

        # Create test repos
        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"
        repo1.mkdir()
        repo2.mkdir()

        # Mock registry file
        registry_file = tmp_path / "repos.json"

        with patch(
            "issue_orchestrator.infra.repo_registry._repos_file",
            return_value=registry_file,
        ):
            client = TestClient(control_app)

            # Add first repo
            response1 = client.post(
                "/control/repos",
                json={"repo_root": str(repo1)},
            )
            assert response1.status_code == 200
            assert response1.json()["status"] == "added"

            # Add second repo
            response2 = client.post(
                "/control/repos",
                json={"repo_root": str(repo2)},
            )
            assert response2.status_code == 200
            assert response2.json()["status"] == "added"

            # List should show both
            response3 = client.get("/control/repos")
            assert response3.status_code == 200
            repos = response3.json()["repos"]
            paths = [r["path"] for r in repos]
            assert str(repo1.resolve()) in paths
            assert str(repo2.resolve()) in paths


class TestSetupWizardEndpoints:
    """Setup wizard API endpoints for GUI configuration."""

    @pytest.fixture(autouse=True)
    def _verified_github_authorization(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exercise endpoint composition without reaching external GitHub."""
        owner = control_app.state.control_api_setup_dependencies.setup_owner

        def _verify(repo_name, authorization):
            return RepositorySetupGitHubVerification(
                identity="phase-invariant-user",
                repository=repo_name,
                auth_kind="personal",
                source="integration test credential",
                normalized_authorization=authorization,
            )

        monkeypatch.setattr(owner, "_github_verifier", _verify)

    def test_prereqs_endpoint_exists(self) -> None:
        """GET /control/setup/prereqs endpoint exists."""
        client = TestClient(control_app)

        response = client.get("/control/setup/prereqs")

        assert response.status_code == 200
        data = response.json()
        assert "all_ok" in data
        assert "checks" in data
        assert "git" in data["checks"]
        # GitHub authorization is a resumable wizard stage, not a prerequisite:
        # users may open Setup before creating or selecting a credential.
        assert "github_auth" not in data["checks"]
        assert "ai_provider_clis" in data["checks"]

    def test_github_verification_endpoint_exists(self) -> None:
        """The dedicated GitHub stage is registered separately from prereqs."""
        response = TestClient(control_app).post(
            "/control/setup/github-auth/verify",
            json={},
        )

        assert response.status_code == 422

    def test_validate_endpoint_exists(self, tmp_path: Path) -> None:
        """POST /control/repos/validate endpoint exists."""
        client = TestClient(control_app)

        # Create a repo with config
        config_dir = (
            tmp_path / ".issue-orchestrator" / "config" / "modes" / "default"
        )
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "repo:\n  name: test/repo\nagents:\n  agent:dev:\n    prompt: dev.md\n"
        )

        response = client.post(
            "/control/repos/validate",
            json={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert "valid" in data
        assert "has_config" in data

    def test_detect_endpoint_exists(self, tmp_path: Path) -> None:
        """GET /control/setup/detect endpoint exists."""
        client = TestClient(control_app)

        response = client.get(
            "/control/setup/detect",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert "repo_root" in data
        assert "existing_config" in data

    def test_preview_endpoint_executes_setup_command(self, tmp_path: Path) -> None:
        """POST /control/setup/preview executes the typed setup command."""
        client = TestClient(control_app)

        response = client.post(
            "/control/setup/preview",
            json=_complete_setup_payload(tmp_path),
        )

        assert response.status_code == 200
        data = response.json()
        assert "yaml" in data
        assert "files" in data
        assert "name: test/repo" in data["yaml"]

    def test_save_endpoint_creates_config(self, tmp_path: Path) -> None:
        """POST /control/setup/save endpoint creates config file."""
        client = TestClient(control_app)

        response = client.post(
            "/control/setup/save",
            json=_complete_setup_payload(
                tmp_path,
                create_prompts=True,
                create_labels=False,  # Skip GitHub API calls
            ),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "saved"
        assert "config_path" in data

        # Config file should exist at new location
        config_path = (
            tmp_path
            / ".issue-orchestrator"
            / "config"
            / "modes"
            / "default"
            / "default.yaml"
        )
        assert config_path.exists()
        content = config_path.read_text()
        assert "name: test/repo" in content

    def test_detect_returns_existing_config(self, tmp_path: Path) -> None:
        """GET /control/setup/detect returns existing_config when present."""
        client = TestClient(control_app)

        # Create a config file at new location
        config_content = """repo:
  name: existing/repo
agents:
  agent:backend:
    prompt: backend.md
    model: opus
"""
        config_dir = (
            tmp_path / ".issue-orchestrator" / "config" / "modes" / "default"
        )
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(config_content)

        response = client.get(
            "/control/setup/detect",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["existing_config"] is not None
        assert data["existing_config"]["repo"]["name"] == "existing/repo"
        assert "agent:backend" in data["existing_config"]["agents"]

    def test_save_endpoint_requires_confirmation_to_replace_existing_config(
        self,
        tmp_path: Path,
    ) -> None:
        """Existing configs remain unchanged until replacement is confirmed."""
        client = TestClient(control_app)

        # Create initial config at new location
        config_dir = (
            tmp_path / ".issue-orchestrator" / "config" / "modes" / "default"
        )
        config_dir.mkdir(parents=True)
        initial_config = "repo:\n  name: old/repo\nagents:\n  agent:old: {}\n"
        (config_dir / "default.yaml").write_text(initial_config)

        replacement = _complete_setup_payload(
            tmp_path,
            repo_name="new/repo",
            worker_agent_label="agent:new",
            configure_reviewer=False,
            configure_tech_lead=False,
            create_prompts=False,
            create_labels=False,
        )
        unconfirmed = client.post(
            "/control/setup/save",
            json=replacement,
        )

        assert unconfirmed.status_code == 409
        assert unconfirmed.json()["error"] == "replace_confirmation_required"
        assert (config_dir / "default.yaml").read_text() == initial_config

        response = client.post(
            "/control/setup/save",
            json={**replacement, "replace_existing": True},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "saved"

        # Config should be updated
        content = (config_dir / "default.yaml").read_text()
        assert "name: new/repo" in content
        assert "agent:new" in content
        assert "agent:old" not in content  # Old config replaced


class TestDashboardRendering:
    """Tests that the orchestrator dashboard renders without errors."""

    def test_dashboard_renders_without_orchestrator(self) -> None:
        """Dashboard should render even without a running orchestrator."""
        from issue_orchestrator.entrypoints.web import app

        client = TestClient(app)

        response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_dashboard_contains_all_tabs(self) -> None:
        """Dashboard should have all expected tabs."""
        from issue_orchestrator.entrypoints.web import app

        client = TestClient(app)

        response = client.get("/")

        assert response.status_code == 200
        # Check dashboard tabs are present
        assert "switchTab('kanban')" in response.text or "Kanban" in response.text
        assert "switchTab('e2e')" in response.text or "E2E" in response.text

    def test_dashboard_issue_detail_drawer_present(self) -> None:
        """Dashboard should include issue-detail drawer surface."""
        from issue_orchestrator.entrypoints.web import app

        client = TestClient(app)

        response = client.get("/")

        assert response.status_code == 200
        assert "issueDetailDrawer" in response.text
        assert "issueDetailTitle" in response.text
        assert "issueDetailJourney" in response.text
        assert "issueDetailFocusBtn" not in response.text
        assert "issueDetailGitHubBtn" not in response.text

    def test_dashboard_api_endpoints_exist(self) -> None:
        """Dashboard API endpoints should exist."""
        from issue_orchestrator.entrypoints.web import app

        client = TestClient(app)

        # These should return 503 (no orchestrator) not 404 (not found)
        response = client.get("/api/milestones")
        assert response.status_code == 503  # No orchestrator, but endpoint exists

        response = client.post("/api/issues", json={"title": "test"})
        assert response.status_code == 503  # No orchestrator, but endpoint exists


class TestToolEndpoints:
    """Tests for the dashboard tool endpoints."""

    def test_audit_endpoint_exists(self, tmp_path: Path) -> None:
        """GET /control/tools/audit endpoint exists."""
        client = TestClient(control_app)

        response = client.get(
            "/control/tools/audit",
            params={"repo_root": str(tmp_path)},
        )

        # Should return 404 (no config) not 500 (endpoint error) or 404 (route not found)
        assert response.status_code == 404
        data = response.json()
        assert "error" in data
        assert "Config" in data["error"]

    def test_audit_endpoint_with_config(self, tmp_path: Path) -> None:
        """GET /control/tools/audit returns audit entries when config exists."""
        # Create a minimal config
        config_dir = (
            tmp_path / ".issue-orchestrator" / "config" / "modes" / "default"
        )
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "repo:\n  name: test/repo\nagents:\n  agent:dev:\n    prompt: dev.md\n"
        )
        # Create a .git directory so it's a valid repo
        (tmp_path / ".git").mkdir()

        client = TestClient(control_app)

        response = client.get(
            "/control/tools/audit",
            params={"repo_root": str(tmp_path)},
        )

        # This will likely fail because we don't have a real GitHub repo,
        # but it should at least not 404 on the route. GitHub access failures
        # are surfaced as structured upstream errors rather than hidden behind
        # a generic 500.
        assert response.status_code in (200, 401, 403, 429, 500, 502, 503)
        if response.status_code != 200:
            data = response.json()
            assert "error" in data
            if response.status_code in (401, 403, 429, 502, 503):
                assert "error_code" in data

    def test_trace_endpoint_exists(self, tmp_path: Path) -> None:
        """GET /control/tools/trace endpoint exists and handles missing logs."""
        client = TestClient(control_app)

        response = client.get(
            "/control/tools/trace",
            params={"repo_root": str(tmp_path), "issue_number": 123},
        )

        assert response.status_code == 200
        data = response.json()
        # No log file should exist for a fresh tmp_path
        assert "entries" in data or "message" in data

    def test_labels_init_endpoint_exists(self, tmp_path: Path) -> None:
        """POST /control/tools/labels/init endpoint exists."""
        client = TestClient(control_app)

        response = client.post(
            "/control/tools/labels/init",
            json={"repo_root": str(tmp_path)},
        )

        # Should return 404 (no config) not 500
        assert response.status_code == 404
        data = response.json()
        assert "error" in data

    def test_worktrees_cleanup_endpoint_exists(self, tmp_path: Path) -> None:
        """POST /control/tools/worktrees/cleanup endpoint exists."""
        # Create minimal config
        config_dir = (
            tmp_path / ".issue-orchestrator" / "config" / "modes" / "default"
        )
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "repo:\n  name: test/repo\nagents:\n  agent:dev:\n    prompt: dev.md\n"
        )

        client = TestClient(control_app)

        response = client.post(
            "/control/tools/worktrees/cleanup",
            json={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        # Should return stale_worktrees list
        assert "stale_worktrees" in data or "message" in data
