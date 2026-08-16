"""Core control API route tests split from test_control_api."""

# ruff: noqa: F403,F405,SLF001

import json
from pathlib import Path
from types import SimpleNamespace

from tests.unit import test_control_api as _support
from tests.unit.route_helpers import route_path_counts
from tests.unit.test_control_api import *  # noqa: F403
from issue_orchestrator.infra.repo_registry import RegisteredRepo

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)


class TestRouteRegistration:
    """Route registration guardrails for the control API app."""

    def test_control_orchestrator_routes_are_registered_once(self) -> None:
        orchestrator_paths = {
            "/control/orchestrator/start",
            "/control/orchestrator/stop",
            "/control/orchestrator/reconcile",
            "/control/orchestrator/status",
            "/control/orchestrator/pause",
            "/control/orchestrator/resume",
            "/control/orchestrator/refresh",
            "/control/orchestrator/last_failure",
            "/control/orchestrator/doctor",
            "/control/orchestrator/ai_diagnose",
            "/control/orchestrator/log_tail",
        }
        counts = route_path_counts(control_app, orchestrator_paths)

        assert counts == Counter({path: 1 for path in orchestrator_paths})

    def test_control_shutdown_routes_are_registered_once(self) -> None:
        shutdown_paths = {
            "/control/shutdown",
            "/control/shutdown/state",
            "/control/shutdown/abort",
            "/control/shutdown/update",
            "/control/shutdown/force",
        }
        counts = route_path_counts(control_app, shutdown_paths)

        assert counts == Counter({path: 1 for path in shutdown_paths})

    def test_control_issue_routes_are_registered_once(self) -> None:
        issue_paths = {
            "/api/preflight-push",
            "/api/issues/{issue_number}/resume",
            "/api/issues/{issue_number}/debug-session",
            "/api/issues/{issue_number}/retry",
            "/api/issues/{issue_number}/dismiss",
            "/api/issues/{issue_number}/close",
        }
        counts = route_path_counts(control_app, issue_paths)

        assert counts == Counter({path: 1 for path in issue_paths})

    def test_extracted_control_route_families_are_registered_once(self) -> None:
        extracted_paths = {
            "/control/goal_pilot/runs",
            "/control/goal_pilot/config",
            "/control/goal_pilot/runs/{run_id}",
            "/control/goal_pilot/runs/{run_id}/phase",
            "/control/goal_pilot/runs/{run_id}/journeys",
            "/control/goal_pilot/journeys/{journey_id}",
            "/control/goal_pilot/runs/{run_id}/journeys/reorder",
            "/control/goal_pilot/runs/{run_id}/actions",
            "/control/goal_pilot/skills",
            "/control/goal_pilot/skills/export",
            "/control/tools/audit",
            "/control/tools/trace",
            "/control/tools/labels/init",
            "/control/tools/worktrees/cleanup",
        }
        counts = route_path_counts(control_app, extracted_paths)

        expected = Counter({path: 1 for path in extracted_paths})
        expected["/control/goal_pilot/runs"] = 2
        expected["/control/goal_pilot/runs/{run_id}"] = 2
        expected["/control/goal_pilot/runs/{run_id}/journeys"] = 2
        expected["/control/goal_pilot/skills"] = 2

        assert counts == expected


class TestGoalPilotRoutes:
    """Behavior guardrails for extracted Goal Pilot routes."""

    def test_goal_pilot_reorder_invokes_store_once(self) -> None:
        from issue_orchestrator.entrypoints.control_api_goal_pilot_support import (
            ControlApiGoalPilotDependencies,
            install_control_api_goal_pilot_dependencies,
        )

        pilot = MagicMock()
        pilot.reorder_journeys.return_value = {"status": "ok"}
        original_deps = getattr(
            control_app.state, "control_api_goal_pilot_dependencies"
        )
        install_control_api_goal_pilot_dependencies(
            control_app,
            ControlApiGoalPilotDependencies(
                get_orchestrator=lambda: MagicMock(),
                get_goal_pilot=lambda: pilot,
            ),
        )
        try:
            response = TestClient(control_app).post(
                "/control/goal_pilot/runs/run-1/journeys/reorder",
                json={"order": ["journey-1", "journey-2"]},
            )
        finally:
            install_control_api_goal_pilot_dependencies(control_app, original_deps)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        pilot.reorder_journeys.assert_called_once_with(
            "run-1", ["journey-1", "journey-2"]
        )


class TestOrchestratorNotInitialized:
    """Test that endpoints return 503 when orchestrator is not initialized."""

    def test_refresh_returns_503(self, client_without_orchestrator):
        """POST /api/refresh returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/refresh")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_pause_returns_503(self, client_without_orchestrator):
        """POST /api/pause returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/pause")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_resume_returns_503(self, client_without_orchestrator):
        """POST /api/resume returns 503 when orchestrator is None."""
        response = client_without_orchestrator.post("/api/resume")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_status_returns_503(self, client_without_orchestrator):
        """GET /api/status returns 503 when orchestrator is None."""
        response = client_without_orchestrator.get("/api/status")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"

    def test_events_returns_503(self, client_without_orchestrator):
        """GET /api/events returns 503 when orchestrator is None."""
        response = client_without_orchestrator.get("/api/events")

        assert response.status_code == 503
        assert response.json()["error"] == "Event hub not initialized"

    def test_events_since_returns_503(self, client_without_orchestrator):
        """GET /api/events_since returns 503 when orchestrator is None."""
        response = client_without_orchestrator.get("/api/events_since?after=0")

        assert response.status_code == 503
        assert response.json()["error"] == "Event hub not initialized"

    def test_events_stats_returns_503(self, client_without_orchestrator):
        """GET /api/events_stats returns 503 when orchestrator is None."""
        response = client_without_orchestrator.get("/api/events_stats")

        assert response.status_code == 503
        assert response.json()["error"] == "Event hub not initialized"

    def test_snapshot_returns_503(self, client_without_orchestrator):
        """GET /api/snapshot returns 503 when orchestrator is None."""
        response = client_without_orchestrator.get("/api/snapshot")

        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not initialized"


class TestEventHubNotInitialized:
    """Test SSE endpoints when event_hub is None."""

    def test_events_returns_503_when_event_hub_none(self, mock_orchestrator):
        """GET /api/events returns 503 when event_hub is None."""
        mock_orchestrator.event_hub = None
        set_orchestrator(mock_orchestrator)
        try:
            client = TestClient(control_app)
            response = client.get("/api/events")

            assert response.status_code == 503
            assert response.json()["error"] == "Event hub not initialized"
        finally:
            set_orchestrator(None)

    def test_events_since_returns_503_when_event_hub_none(self, mock_orchestrator):
        """GET /api/events_since returns 503 when event_hub is None."""
        mock_orchestrator.event_hub = None
        set_orchestrator(mock_orchestrator)
        try:
            client = TestClient(control_app)
            response = client.get("/api/events_since?after=0")

            assert response.status_code == 503
            assert response.json()["error"] == "Event hub not initialized"
        finally:
            set_orchestrator(None)

    def test_snapshot_returns_503_when_event_hub_none(self, mock_orchestrator):
        """GET /api/snapshot returns 503 when event_hub is None."""
        mock_orchestrator.event_hub = None
        set_orchestrator(mock_orchestrator)
        try:
            client = TestClient(control_app)
            response = client.get("/api/snapshot")

            assert response.status_code == 503
            assert response.json()["error"] == "Event hub not initialized"
        finally:
            set_orchestrator(None)


# --- Test: State Transition Endpoints ---


class TestPauseEndpoint:
    """Test the POST /api/pause endpoint."""

    def test_pause_calls_orchestrator_pause(self, client_with_orchestrator):
        """Pausing calls orchestrator.pause() and returns paused status."""
        client, mock_orch = client_with_orchestrator

        response = client.post("/api/pause")

        assert response.status_code == 200
        assert response.json() == {"status": "paused"}
        mock_orch.pause.assert_called_once()

    def test_pause_is_idempotent(self, client_with_orchestrator):
        """Pausing twice calls pause() twice (orchestrator handles idempotency)."""
        client, mock_orch = client_with_orchestrator

        client.post("/api/pause")
        client.post("/api/pause")

        assert mock_orch.pause.call_count == 2


class TestResumeEndpoint:
    """Test the POST /api/resume endpoint."""

    def test_resume_calls_orchestrator_resume(self, client_with_orchestrator):
        """Resuming calls orchestrator.resume() and returns resumed status."""
        client, mock_orch = client_with_orchestrator

        response = client.post("/api/resume")

        assert response.status_code == 200
        assert response.json() == {"status": "resumed"}
        mock_orch.resume.assert_called_once()

    def test_resume_is_idempotent(self, client_with_orchestrator):
        """Resuming twice calls resume() twice (orchestrator handles idempotency)."""
        client, mock_orch = client_with_orchestrator

        client.post("/api/resume")
        client.post("/api/resume")

        assert mock_orch.resume.call_count == 2


class TestControlCenterTemplate:
    """Test rendered control center UI copy and scope labels."""

    def test_control_center_ui_uses_engine_terminology(
        self, client_without_orchestrator
    ):
        response = client_without_orchestrator.get("/")

        assert response.status_code == 200
        body = response.text
        assert "Repository Engines" in body
        assert "Close Control Center" in body
        assert '<meta name="io-browser-auth-required" content="0">' in body
        assert 'id="sidebarCloseCC"' in body
        assert 'id="sidebarAppMenuBtn"' in body
        assert 'href="/static/brand/logo.svg"' in body
        assert 'src="/static/brand/logo.svg"' in body
        assert "/favicon.ico" not in body
        assert 'id="shutdownBtn"' not in body
        assert 'id="menuCloseCC"' not in body
        # Static asset URLs carry a ``?v=<token>`` cache-buster so a
        # cc restart automatically invalidates the browser cache. The
        # exact token varies per process, so just assert the prefix.
        assert 'href="/static/css/control_center.css?v=' in body
        assert 'href="/static/css/control_center_setup.css?v=' in body
        assert 'src="/static/js/browser_auth.js?v=' in body
        assert 'src="/static/js/control_center_setup_commands.js?v=' in body
        assert 'src="/static/js/control_center_setup.js?v=' in body
        assert 'src="/static/js/control_center.js?v=' in body
        assert "Closing this window does not stop repository engines" in body
        assert ">Stopped<" not in body
        assert 'id="sidebarRepoList"' not in body
        assert "nav-repo-list" not in body
        assert "nav-repo-item" not in body

    def test_control_center_ui_renders_runtime_version_in_sidebar_footer(
        self,
        client_without_orchestrator,
        monkeypatch,
    ):
        from issue_orchestrator.infra import runtime_identity

        sha = "abcdef1234567890abcdef1234567890abcdef12"
        monkeypatch.setattr(runtime_identity, "__version__", "1.2.3")
        monkeypatch.setattr(runtime_identity, "resolve_cc_commit_sha", lambda: sha)

        response = client_without_orchestrator.get("/")

        assert response.status_code == 200
        assert "v1.2.3 (abcdef1)" in response.text

    def test_control_center_shell_is_not_cached(self, client_without_orchestrator):
        response = client_without_orchestrator.get("/")

        assert response.status_code == 200
        assert (
            response.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
        )
        assert response.headers["Pragma"] == "no-cache"
        assert response.headers["Expires"] == "0"
        assert (
            '<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">'
            in response.text
        )
        assert '<meta http-equiv="Pragma" content="no-cache">' in response.text
        assert '<meta http-equiv="Expires" content="0">' in response.text

    def test_control_center_ui_renders_frozen_snapshot_launch_sha(
        self,
        client_without_orchestrator,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from issue_orchestrator.infra import static_version

        sha = "abcdef1234567890abcdef1234567890abcdef12"
        snapshot_dir = tmp_path / ".control-center-snapshot" / "launch-1"
        package_dir = snapshot_dir / "src" / "issue_orchestrator"
        package_dir.mkdir(parents=True)
        package_init = package_dir / "__init__.py"
        package_init.write_text("# frozen package\n", encoding="utf-8")
        (snapshot_dir / "source-metadata.json").write_text(
            json.dumps({"schema_version": 1, "commit_sha": sha}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("ISSUE_ORCHESTRATOR_CC_COMMIT_SHA", raising=False)
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_CC_SNAPSHOT", str(snapshot_dir / "src"))
        monkeypatch.setattr(static_version, "_RUNNING_PACKAGE_INIT", package_init)
        monkeypatch.setattr(static_version, "_PACKAGE_REPO_ROOT", None)

        response = client_without_orchestrator.get("/")

        assert response.status_code == 200
        assert f"({sha[:7]})" in response.text

    def test_control_center_static_assets_are_served(self, client_without_orchestrator):
        css_response = client_without_orchestrator.get("/static/css/control_center.css")
        browser_auth_response = client_without_orchestrator.get(
            "/static/js/browser_auth.js"
        )
        setup_commands_response = client_without_orchestrator.get(
            "/static/js/control_center_setup_commands.js"
        )
        setup_response = client_without_orchestrator.get(
            "/static/js/control_center_setup.js"
        )
        js_response = client_without_orchestrator.get("/static/js/control_center.js")
        logo_response = client_without_orchestrator.get("/static/brand/logo.svg")

        assert css_response.status_code == 200
        assert "--sidebar-width" in css_response.text
        assert browser_auth_response.status_code == 200
        assert "openAuthenticatedSseStream" in browser_auth_response.text
        assert setup_commands_response.status_code == 200
        assert "buildSetupPreviewRequest" in setup_commands_response.text
        assert setup_response.status_code == 200
        assert "createControlCenterSetupWizard" in setup_response.text
        assert js_response.status_code == 200
        assert "start_paused: startPaused" in js_response.text
        assert logo_response.status_code == 200
        assert "image/svg+xml" in logo_response.headers.get("content-type", "")
        assert "<svg" in logo_response.text

    def test_control_center_cache_buster_is_consistent_across_assets(
        self,
        client_without_orchestrator,
    ):
        """All ``?v=<token>`` query strings on a single render must share the same token.

        If two assets render with different tokens, the browser still
        eagerly fetches both — but the inconsistency would hide a real
        bug where the substitution dropped before some references.
        """
        import re

        body = client_without_orchestrator.get("/").text
        tokens = set(re.findall(r"/static/[^\"']+\?v=([^\"'&]+)", body))

        assert tokens, "Expected at least one ?v= cache-buster on a static URL"
        assert len(tokens) == 1, (
            f"All static assets should share one cache-buster per render, got: {tokens}"
        )
        # Defensive: token should never be the literal placeholder.
        assert "{{" not in next(iter(tokens))

    def test_control_center_cache_buster_is_stable_within_a_process(
        self,
        client_without_orchestrator,
    ):
        """Two requests in the same process get the same token.

        Token churn within a single cc lifetime would make every page
        load refetch every asset — defeats the cache entirely. The
        token is computed once at module import time.
        """
        import re

        body_a = client_without_orchestrator.get("/").text
        body_b = client_without_orchestrator.get("/").text
        token_a = re.search(r"/static/js/control_center\.js\?v=([^\"'&]+)", body_a)
        token_b = re.search(r"/static/js/control_center\.js\?v=([^\"'&]+)", body_b)

        assert token_a and token_b
        assert token_a.group(1) == token_b.group(1)

    def test_control_center_favicon_uses_packaged_logo(
        self, client_without_orchestrator
    ):
        response = client_without_orchestrator.get("/favicon.ico")

        assert response.status_code == 200
        assert "image/svg+xml" in response.headers.get("content-type", "")
        assert "<svg" in response.text

    def test_control_center_javascript_preserves_start_paused_wiring(self):
        script = CONTROL_CENTER_JS.read_text()

        assert "Start engine" in script
        assert "Not running" in script
        assert "await startRepo(path, null, true);" in script
        assert "start_paused: startPaused" in script


class TestControlCenterRepoContext:
    """Control Center should prefer the repo root it was launched from."""

    def test_control_repos_prioritizes_preferred_repo(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        from issue_orchestrator.infra import repo_registry

        preferred = tmp_path / "preferred"
        other = tmp_path / "other"
        preferred.mkdir()
        other.mkdir()

        repos = [
            RegisteredRepo(
                path=str(other),
                name="other",
                added_at="2026-01-01T00:00:00+00:00",
                selected_config="default.yaml",
                selected_mode="default",
                health=None,
            )
        ]

        def fake_list_repos():
            return list(repos)

        def fake_add_repo(path: str):
            repos.append(
                RegisteredRepo(
                    path=str(Path(path)),
                    name=Path(path).name,
                    added_at="2026-01-01T00:00:00+00:00",
                    selected_config="default.yaml",
                    selected_mode="default",
                    health=None,
                )
            )
            return repos[-1]

        monkeypatch.setenv("ISSUE_ORCHESTRATOR_CC_REPO_ROOT", str(preferred))
        monkeypatch.setattr(repo_registry, "list_repos", fake_list_repos)
        monkeypatch.setattr(repo_registry, "add_repo", fake_add_repo)
        mock_supervisor.status.return_value = SupervisorStatus(state="stopped")

        response = supervisor_client.get("/control/repos")

        assert response.status_code == 200
        data = response.json()
        assert data["repos"][0]["path"] == str(preferred)
        assert data["repos"][1]["path"] == str(other)

    def test_control_info_exposes_preferred_repo_root(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preferred = tmp_path / "preferred"
        preferred.mkdir()
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_CC_REPO_ROOT", str(preferred))

        response = supervisor_client.get("/control/info")

        assert response.status_code == 200
        data = response.json()
        assert data["preferred_repo_root"] == str(preferred)


class TestRepositoryLaunchSelectionEndpoint:
    def test_selection_change_is_rejected_while_engine_runs(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_supervisor: MagicMock,
    ) -> None:
        mock_supervisor.status_all_instances.return_value = MultiInstanceStatus(
            repo_root=str(tmp_path),
            instances=[
                SupervisorStatus(
                    state="running",
                    instance_id="orchestrator-1",
                )
            ],
        )

        response = supervisor_client.post(
            "/control/repos/select-config",
            json={
                "repo_root": str(tmp_path),
                "mode": "codex",
                "config_name": "main.yaml",
            },
        )

        assert response.status_code == 409
        assert response.json()["error"] == "engine_running"

    def test_selection_change_is_rejected_while_orphan_engine_runs(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        from issue_orchestrator.execution import control_center_runtime

        mock_supervisor.status_all_instances.return_value = MultiInstanceStatus(
            repo_root=str(tmp_path),
        )
        monkeypatch.setattr(
            control_center_runtime,
            "detect_repository_orchestrators",
            lambda *_args, **_kwargs: [{"port": 18080}],
        )

        response = supervisor_client.post(
            "/control/repos/select-config",
            json={
                "repo_root": str(tmp_path),
                "mode": "codex",
                "config_name": "main.yaml",
            },
        )

        assert response.status_code == 409
        assert response.json()["error"] == "engine_running"
        assert "untracked" in response.json()["detail"]

    def test_selection_endpoint_persists_complete_typed_selection(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        from issue_orchestrator.infra import repo_registry

        captured: list[object] = []
        config_path = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("agents: {}\n")
        mock_supervisor.status_all_instances.return_value = MultiInstanceStatus(
            repo_root=str(tmp_path),
        )
        mock_supervisor.status.return_value = SupervisorStatus(state="stopped")
        monkeypatch.setattr(
            repo_registry,
            "set_selected_launch_selection",
            lambda _path, selection: captured.append(selection) or True,
        )

        response = supervisor_client.post(
            "/control/repos/select-config",
            json={
                "repo_root": str(tmp_path),
                "mode": "codex",
                "config_name": "main",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "mode": "codex",
            "config_name": "main.yaml",
        }
        assert captured[0].to_dict() == {
            "mode": "codex",
            "config_name": "main.yaml",
        }

    def test_selection_endpoint_rejects_missing_mode_config(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_supervisor: MagicMock,
    ) -> None:
        mock_supervisor.status_all_instances.return_value = MultiInstanceStatus(
            repo_root=str(tmp_path),
        )

        response = supervisor_client.post(
            "/control/repos/select-config",
            json={
                "repo_root": str(tmp_path),
                "mode": "codex",
                "config_name": "missing.yaml",
            },
        )

        assert response.status_code == 404
        assert response.json()["error"] == "config_not_found"


# --- Test: Refresh Endpoint ---


class TestRefreshEndpoint:
    """Test the POST /api/refresh endpoint."""

    def test_refresh_without_body(self, client_with_orchestrator):
        """Refresh without body calls request_refresh with empty set."""
        client, mock_orch = client_with_orchestrator

        response = client.post("/api/refresh")

        assert response.status_code == 200
        assert response.json() == {"status": "refresh_requested"}
        mock_orch.request_refresh.assert_called_once_with(inflight_stable_ids=set())

    def test_refresh_with_inflight_stable_ids(self, client_with_orchestrator):
        """Refresh with inflight_stable_ids passes them to request_refresh."""
        client, mock_orch = client_with_orchestrator

        response = client.post(
            "/api/refresh",
            json={"inflight_stable_ids": ["issue-1", "issue-2", "issue-3"]},
        )

        assert response.status_code == 200
        assert response.json() == {"status": "refresh_requested"}
        mock_orch.request_refresh.assert_called_once()
        call_args = mock_orch.request_refresh.call_args
        assert call_args.kwargs["inflight_stable_ids"] == {
            "issue-1",
            "issue-2",
            "issue-3",
        }

    def test_refresh_with_integer_stable_ids(self, client_with_orchestrator):
        """Refresh converts integer stable_ids to strings."""
        client, mock_orch = client_with_orchestrator

        response = client.post("/api/refresh", json={"inflight_stable_ids": [1, 2, 3]})

        assert response.status_code == 200
        call_args = mock_orch.request_refresh.call_args
        assert call_args.kwargs["inflight_stable_ids"] == {"1", "2", "3"}

    def test_refresh_ignores_malformed_json(self, client_with_orchestrator):
        """Refresh ignores malformed JSON body and uses empty set."""
        client, mock_orch = client_with_orchestrator

        response = client.post(
            "/api/refresh",
            content="not valid json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 200
        mock_orch.request_refresh.assert_called_once_with(inflight_stable_ids=set())

    def test_refresh_ignores_empty_body(self, client_with_orchestrator):
        """Refresh with empty body uses empty set."""
        client, mock_orch = client_with_orchestrator

        response = client.post("/api/refresh", content="")

        assert response.status_code == 200
        mock_orch.request_refresh.assert_called_once_with(inflight_stable_ids=set())


class TestControlReposDashboardUrl:
    def test_control_repos_detects_nonselected_orphan_without_lock(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        from issue_orchestrator.entrypoints import control_api
        from issue_orchestrator.execution import control_center_repo_status
        from issue_orchestrator.infra import repo_registry

        repo = tmp_path / "repo"
        for mode in ("default", "codex"):
            config = repo / f".issue-orchestrator/config/modes/{mode}/main.yaml"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text("agents: {}\n", encoding="utf-8")
        monkeypatch.setattr(
            repo_registry,
            "list_repos",
            lambda: [
                RegisteredRepo(
                    path=str(repo),
                    selected_mode="default",
                    selected_config="main.yaml",
                )
            ],
        )
        monkeypatch.setattr(repo_registry, "add_repo", lambda path: None)
        monkeypatch.setattr(control_api, "_preferred_repo_root", lambda: None)
        monkeypatch.setattr(
            control_center_repo_status,
            "detect_repository_orchestrators",
            lambda _repo: [
                {
                    "port": 19090,
                    "info": {
                        "configuration_mode": "codex",
                        "config_name": "main.yaml",
                        "config_fingerprint": "codex-fingerprint",
                    },
                    "status": {"active_sessions": []},
                }
            ],
        )
        mock_supervisor.status.return_value = SupervisorStatus(state="stopped")
        mock_supervisor.status_all_instances.return_value = MultiInstanceStatus(
            repo_root=str(repo)
        )

        payload = supervisor_client.get("/control/repos").json()["repos"][0]

        assert payload["status"]["state"] == "running"
        assert payload["status"]["orphaned"] is True
        assert payload["selected_mode"] == "default"
        assert payload["active_mode"] == "codex"

    def test_control_repos_uses_active_lock_identity_for_multi_instance_topology(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        from issue_orchestrator.entrypoints import control_api
        from issue_orchestrator.execution import control_center_repo_status
        from issue_orchestrator.infra import repo_registry

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            repo_registry,
            "list_repos",
            lambda: [
                RegisteredRepo(
                    path=str(repo),
                    selected_mode="default",
                    selected_config="main.yaml",
                )
            ],
        )
        monkeypatch.setattr(repo_registry, "add_repo", lambda path: None)
        monkeypatch.setattr(control_api, "_preferred_repo_root", lambda: None)
        monkeypatch.setattr(
            control_center_repo_status,
            "enrich_runtime_health",
            lambda _path, payload, **_kwargs: payload,
        )
        monkeypatch.setattr(
            control_center_repo_status,
            "_apply_internal_runtime_state",
            lambda _payload, _port: None,
        )
        mock_supervisor.status_all_instances.return_value = MultiInstanceStatus(
            repo_root=str(repo),
            instances=[
                SupervisorStatus(
                    state="running",
                    pid=101,
                    port=19101,
                    instance_id="orchestrator-1",
                    configuration_mode="codex",
                    config_name="other.yaml",
                    config_fingerprint="active-fingerprint",
                ),
                SupervisorStatus(
                    state="running",
                    pid=102,
                    port=19102,
                    instance_id="orchestrator-2",
                    configuration_mode="codex",
                    config_name="other.yaml",
                    config_fingerprint="active-fingerprint",
                ),
            ],
        )

        response = supervisor_client.get("/control/repos")

        assert response.status_code == 200
        payload = response.json()["repos"][0]
        assert payload["selected_mode"] == "default"
        assert payload["active_mode"] == "codex"
        assert payload["active_config"] == "other.yaml"
        assert payload["active_config_fingerprint"] == "active-fingerprint"
        assert payload["expected_instances"] == 2
        assert payload["status"]["state"] == "running"
        assert payload["status"]["configuration_mode"] == "codex"
        assert len(payload["instances"]) == 2

    def test_control_repos_reports_conflicting_active_identities_deterministically(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        from issue_orchestrator.entrypoints import control_api
        from issue_orchestrator.execution import control_center_repo_status
        from issue_orchestrator.infra import repo_registry

        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            repo_registry,
            "list_repos",
            lambda: [
                RegisteredRepo(
                    path=str(repo),
                    selected_mode="default",
                    selected_config="main.yaml",
                )
            ],
        )
        monkeypatch.setattr(repo_registry, "add_repo", lambda path: None)
        monkeypatch.setattr(control_api, "_preferred_repo_root", lambda: None)
        monkeypatch.setattr(
            control_center_repo_status,
            "enrich_runtime_health",
            lambda _path, payload, **_kwargs: payload,
        )
        mock_supervisor.status_all_instances.return_value = MultiInstanceStatus(
            repo_root=str(repo),
            instances=[
                SupervisorStatus(
                    state="running",
                    pid=102,
                    instance_id="orchestrator-2",
                    configuration_mode="codex",
                    config_name="z.yaml",
                    config_fingerprint="z-fingerprint",
                ),
                SupervisorStatus(
                    state="running",
                    pid=101,
                    instance_id="orchestrator-1",
                    configuration_mode="claude",
                    config_name="a.yaml",
                    config_fingerprint="a-fingerprint",
                ),
            ],
        )

        payload = supervisor_client.get("/control/repos").json()["repos"][0]

        assert payload["selected_mode"] == "default"
        assert payload["selected_config"] == "main.yaml"
        assert payload["active_mode"] == "claude"
        assert payload["active_config"] == "a.yaml"
        assert payload["configuration_identity_conflict"] is True

    def test_legacy_repo_status_route_reports_mode_scoped_configs(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_supervisor: MagicMock,
    ) -> None:
        from urllib.parse import quote

        repo = tmp_path / "repo"
        config = repo / ".issue-orchestrator/config/modes/codex/main.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("agents: {}\n", encoding="utf-8")
        mock_supervisor.status.return_value = SupervisorStatus(state="stopped")

        response = supervisor_client.get(
            f"/api/repos/{quote(str(repo), safe='')}/status"
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["configs"] == []
        assert payload["modes"] == ["codex"]
        assert payload["mode_configs"] == {"codex": ["main.yaml"]}

    def test_control_repos_exposes_codespaces_dashboard_url(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        from issue_orchestrator.entrypoints import control_api
        from issue_orchestrator.infra import repo_registry

        repo = tmp_path / "repo"
        repo.mkdir()

        monkeypatch.setattr(
            repo_registry,
            "list_repos",
            lambda: [
                RegisteredRepo(
                    path=str(repo),
                    name=repo.name,
                    added_at="2026-01-01T00:00:00+00:00",
                    selected_config="main.yaml",
                    selected_mode="default",
                    health=None,
                )
            ],
        )
        monkeypatch.setattr(repo_registry, "add_repo", lambda path: None)
        monkeypatch.setattr(control_api, "_preferred_repo_root", lambda: None)
        monkeypatch.setenv("CODESPACE_NAME", "octo-space")
        monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
        monkeypatch.setattr(
            "httpx.get",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no probe")),
        )  # type: ignore[arg-type]

        mock_supervisor.status.return_value = SupervisorStatus(
            state="running",
            pid=123,
            port=55543,
        )

        response = supervisor_client.get("/control/repos")

        assert response.status_code == 200
        data = response.json()
        assert (
            data["repos"][0]["dashboard_url"]
            == "https://octo-space-55543.app.github.dev/"
        )

    def test_control_repos_omits_dashboard_url_when_port_is_unresolved(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        from issue_orchestrator.entrypoints import control_api
        from issue_orchestrator.infra import repo_registry

        repo = tmp_path / "repo"
        repo.mkdir()

        monkeypatch.setattr(
            repo_registry,
            "list_repos",
            lambda: [
                RegisteredRepo(
                    path=str(repo),
                    name=repo.name,
                    added_at="2026-01-01T00:00:00+00:00",
                    selected_config="main.yaml",
                    selected_mode="default",
                    health=None,
                )
            ],
        )
        monkeypatch.setattr(repo_registry, "add_repo", lambda path: None)
        monkeypatch.setattr(control_api, "_preferred_repo_root", lambda: None)

        mock_supervisor.status.return_value = SupervisorStatus(
            state="running",
            pid=123,
            port=0,
        )

        response = supervisor_client.get("/control/repos")

        assert response.status_code == 200
        data = response.json()
        assert data["repos"][0]["dashboard_url"] is None


# --- Test: Status Endpoint ---


class TestStatusEndpoint:
    """Test the GET /api/status endpoint."""

    def test_status_returns_state_summary(self, client_with_orchestrator):
        """Status endpoint returns orchestrator state summary."""
        client, mock_orch = client_with_orchestrator

        # Set up state with some data
        mock_orch.state.paused = True
        mock_orch.state.active_sessions = [
            SimpleNamespace(
                terminal_id="issue-42",
                issue=SimpleNamespace(
                    number=42,
                    title="Test issue",
                    agent_type="agent:test",
                ),
                runtime_minutes=3,
                agent_config=SimpleNamespace(timeout_minutes=10),
                branch_name="issue-42-test",
            ),
            SimpleNamespace(
                terminal_id="issue-43",
                issue=SimpleNamespace(
                    number=43,
                    title="Slow issue",
                    agent_type="agent:test",
                ),
                runtime_minutes=11,
                agent_config=SimpleNamespace(timeout_minutes=10),
                branch_name="issue-43-test",
            ),
        ]
        mock_orch.state.pending_reviews = [MagicMock()]
        mock_orch.state.pending_reworks = []
        mock_orch.state.completed_today = [1, 2, 3]
        mock_orch.state.cached_queue_issues = [MagicMock(), MagicMock(), MagicMock()]

        response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert data["paused"] is True
        assert data["active_sessions"] == 2
        assert data["sessions"] == [
            {
                "session_name": "issue-42",
                "issue_number": 42,
                "title": "Test issue",
                "runtime_minutes": 3,
                "agent_type": "agent:test",
                "status": "running",
                "branch": "issue-42-test",
            },
            {
                "session_name": "issue-43",
                "issue_number": 43,
                "title": "Slow issue",
                "runtime_minutes": 11,
                "agent_type": "agent:test",
                "status": "slow",
                "branch": "issue-43-test",
            },
        ]
        assert data["pending_reviews"] == 1
        assert data["pending_reworks"] == 0
        assert data["completed_today"] == 3
        assert data["issues_in_queue"] == 3

    def test_status_reflects_running_state(self, client_with_orchestrator):
        """Status shows paused=False when running."""
        client, mock_orch = client_with_orchestrator

        mock_orch.state.paused = False

        response = client.get("/api/status")

        assert response.json()["paused"] is False


# --- Test: Events Since Endpoint ---


class TestEventsSinceEndpoint:
    """Test the GET /api/events_since endpoint."""

    def test_events_since_returns_buffered_events(self, client_with_orchestrator):
        """events_since returns events after the specified event_id."""
        client, mock_orch = client_with_orchestrator

        # Mock get_since to return events
        mock_event = MagicMock()
        mock_event.event_id = 5
        mock_event.type = "session.started"
        mock_event.issue_key = "123"
        mock_event.payload = {"agent": "developer"}

        mock_orch.event_hub.get_since.return_value = [mock_event]
        mock_orch.event_hub.last_event_id = 5
        mock_orch.event_hub.stats.return_value = {
            "oldest_event_id": 1,
            "newest_event_id": 5,
            "buffer_size": 5,
        }

        response = client.get("/api/events_since?after=3")

        assert response.status_code == 200
        data = response.json()
        assert len(data["events"]) == 1
        assert data["events"][0]["event_id"] == 5
        assert data["events"][0]["type"] == "session.started"
        assert data["events"][0]["issue_key"] == "123"
        assert data["last_event_id"] == 5

    def test_events_since_with_no_events(self, client_with_orchestrator):
        """events_since returns empty list when no events after id."""
        client, mock_orch = client_with_orchestrator

        mock_orch.event_hub.get_since.return_value = []
        mock_orch.event_hub.last_event_id = 10

        response = client.get("/api/events_since?after=10")

        assert response.status_code == 200
        data = response.json()
        assert data["events"] == []
        assert data["last_event_id"] == 10

    def test_events_since_default_after_is_zero(self, client_with_orchestrator):
        """events_since defaults to after=0 if not specified."""
        client, mock_orch = client_with_orchestrator

        mock_orch.event_hub.get_since.return_value = []

        response = client.get("/api/events_since")

        mock_orch.event_hub.get_since.assert_called_once_with(0)


# --- Test: Events Stats Endpoint ---


class TestEventsStatsEndpoint:
    """Test the GET /api/events_stats endpoint."""

    def test_events_stats_returns_hub_stats(self, client_with_orchestrator):
        """events_stats returns the event hub statistics."""
        client, mock_orch = client_with_orchestrator

        mock_orch.event_hub.stats.return_value = {
            "buffer_size": 42,
            "buffer_max": 1000,
            "subscribers": 3,
            "oldest_event_id": 10,
            "newest_event_id": 52,
        }

        response = client.get("/api/events_stats")

        assert response.status_code == 200
        data = response.json()
        assert data["stats"]["buffer_size"] == 42
        assert data["stats"]["subscribers"] == 3


# --- Test: Health Endpoint ---


class TestHealthEndpoint:
    """Test the GET /api/health endpoint."""

    def test_health_returns_degraded_when_orchestrator_not_initialized(self):
        """Health endpoint returns 503 when orchestrator is not initialized."""
        set_orchestrator(None)
        client = TestClient(control_app)

        response = client.get("/api/health")

        assert response.status_code == 503
        data = response.json()
        assert data["orchestrator"]["status"] == "not_initialized"
        assert "terminal" in data

    def test_health_returns_degraded_when_terminal_unhealthy(
        self, client_with_orchestrator
    ):
        """Health endpoint returns 503 when terminal health check fails."""
        client, mock_orchestrator = client_with_orchestrator

        # Mock unhealthy terminal
        mock_orchestrator.deps.runner.terminal_health_check.return_value = {
            "healthy": False,
            "error": "test",
        }

        response = client.get("/api/health")

        assert response.status_code == 503
        data = response.json()
        assert data["orchestrator"]["status"] == "running"
        assert data["overall"] == "degraded"

    def test_health_returns_healthy_when_terminal_ok(self, client_with_orchestrator):
        """Health endpoint returns 200 when everything is healthy."""
        client, mock_orchestrator = client_with_orchestrator

        # Mock healthy terminal
        mock_orchestrator.deps.runner.terminal_health_check.return_value = {
            "healthy": True,
            "server_running": True,
            "session_exists": True,
            "backend": "tmux",
        }

        response = client.get("/api/health")

        assert response.status_code == 200
        data = response.json()
        assert data["orchestrator"]["status"] == "running"
        assert data["overall"] == "healthy"


# --- Test: GH Audit Report Endpoint ---


class TestGHAuditReportEndpoint:
    """Test the POST /api/gh_audit_report endpoint."""

    def test_audit_report_returns_error_when_disabled(self, client_with_orchestrator):
        """gh_audit_report returns 400 when audit is disabled."""
        client, _ = client_with_orchestrator

        with patch(
            "issue_orchestrator.entrypoints.control_api.gh_audit.enabled",
            return_value=False,
        ):
            response = client.post("/api/gh_audit_report")

        assert response.status_code == 400
        assert response.json()["error"] == "GH audit not enabled"

    def test_audit_report_returns_path_when_enabled(self, client_with_orchestrator):
        """gh_audit_report returns path when audit is enabled."""
        client, _ = client_with_orchestrator

        with patch(
            "issue_orchestrator.entrypoints.control_api.gh_audit.enabled",
            return_value=True,
        ):
            with patch(
                "issue_orchestrator.entrypoints.control_api.gh_audit.emit_report",
                return_value="/tmp/report.json",
            ):
                response = client.post("/api/gh_audit_report")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "path": "/tmp/report.json"}


# --- Test: Snapshot Endpoint ---


class TestSnapshotEndpoint:
    """Test the GET /api/snapshot endpoint."""

    def test_snapshot_builds_and_returns_data(self, client_with_orchestrator):
        """Snapshot endpoint builds snapshot and returns JSON data."""
        client, mock_orch = client_with_orchestrator

        mock_orch.event_hub.last_event_id = 42
        mock_orch.event_context.tick_id = 10

        # Mock SnapshotBuilder
        with patch(
            "issue_orchestrator.entrypoints.control_api.asyncio.to_thread"
        ) as mock_to_thread:
            mock_to_thread.return_value = {
                "snapshot_id": 42,
                "tick_id": 10,
                "sessions": [],
                "queue": [],
            }

            response = client.get("/api/snapshot")

        assert response.status_code == 200
        data = response.json()
        assert data["snapshot_id"] == 42
        assert data["tick_id"] == 10

    def test_snapshot_returns_500_on_error(self, client_with_orchestrator):
        """Snapshot endpoint returns 500 when snapshot building fails."""
        client, mock_orch = client_with_orchestrator

        with patch(
            "issue_orchestrator.entrypoints.control_api.asyncio.to_thread"
        ) as mock_to_thread:
            mock_to_thread.side_effect = Exception("Build failed")

            response = client.get("/api/snapshot")

        assert response.status_code == 500
        assert response.json()["error"] == "snapshot_failed"
        assert "Build failed" in response.json()["detail"]


# --- Test: set_orchestrator and get_orchestrator ---


class TestOrchestratorAccessors:
    """Test the module-level orchestrator accessors."""

    def test_set_and_get_orchestrator(self):
        """set_orchestrator and get_orchestrator work correctly."""
        mock = MagicMock()

        set_orchestrator(mock)
        try:
            assert get_orchestrator() is mock
        finally:
            set_orchestrator(None)

        assert get_orchestrator() is None

    def test_set_orchestrator_to_none(self):
        """set_orchestrator(None) clears the orchestrator."""
        mock = MagicMock()
        set_orchestrator(mock)
        set_orchestrator(None)

        assert get_orchestrator() is None


# --- Test: ControlAPIServer ---


class TestControlAPIServer:
    """Test the ControlAPIServer lifecycle management class."""

    def test_init_sets_attributes(self, mock_orchestrator):
        """Server initialization stores orchestrator and port."""
        from issue_orchestrator.entrypoints.control_api_server import ControlAPIServer

        server = ControlAPIServer(mock_orchestrator, port=8888)

        assert server.orchestrator is mock_orchestrator
        assert server.port == 8888

    def test_init_uses_default_port(self, mock_orchestrator):
        """Server uses default port 19080 when not specified."""
        from issue_orchestrator.entrypoints.control_api_server import ControlAPIServer

        server = ControlAPIServer(mock_orchestrator)

        assert server.port == 19080

    @pytest.mark.asyncio
    async def test_start_sets_global_orchestrator(self, mock_orchestrator):
        """Starting the server sets the global orchestrator reference."""
        from issue_orchestrator.entrypoints.control_api_server import ControlAPIServer
        import uvicorn

        server = ControlAPIServer(mock_orchestrator, port=19999)

        # Mock uvicorn.Config and Server to avoid actually starting a server
        mock_server_instance = MagicMock()
        mock_server_instance.started = True
        mock_server_instance.serve = AsyncMock()

        with patch.object(uvicorn, "Config"):
            with patch.object(uvicorn, "Server", return_value=mock_server_instance):
                await server.start()

                # Verify orchestrator was set globally
                assert get_orchestrator() is mock_orchestrator

                # Clean up
                set_orchestrator(None)

    @pytest.mark.asyncio
    async def test_start_with_port_zero_reads_back_bound_port(self, mock_orchestrator):
        """When port=0, start() reads back the OS-assigned port."""
        from issue_orchestrator.entrypoints.control_api_server import ControlAPIServer
        import uvicorn

        api_server = ControlAPIServer(mock_orchestrator, port=0)

        # Mock a uvicorn server whose socket reports port 54321
        mock_socket = MagicMock()
        mock_socket.getsockname.return_value = ("127.0.0.1", 54321)
        mock_inner_server = MagicMock()
        mock_inner_server.sockets = [mock_socket]

        mock_server_instance = MagicMock()
        mock_server_instance.started = True
        mock_server_instance.serve = AsyncMock()
        mock_server_instance.servers = [mock_inner_server]

        with patch.object(uvicorn, "Config"):
            with patch.object(uvicorn, "Server", return_value=mock_server_instance):
                await api_server.start()

        assert api_server.port == 54321
        set_orchestrator(None)

    @pytest.mark.asyncio
    async def test_stop_signals_server_exit(self, mock_orchestrator):
        """Stopping sets should_exit on the uvicorn server."""
        from issue_orchestrator.entrypoints.control_api_server import ControlAPIServer
        import asyncio

        server = ControlAPIServer(mock_orchestrator, port=19999)
        # Set up internal state for testing server stop lifecycle (noqa: SLF001)
        server._server = MagicMock()  # noqa: SLF001
        server._task = asyncio.create_task(asyncio.sleep(0))  # noqa: SLF001
        await server._task  # noqa: SLF001

        await server.stop()

        assert server._server.should_exit is True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_stop_handles_missing_server(self, mock_orchestrator):
        """Stopping when server is None does not raise."""
        from issue_orchestrator.entrypoints.control_api_server import ControlAPIServer

        server = ControlAPIServer(mock_orchestrator)
        # Set up internal state for testing stop() handles missing server (noqa: SLF001)
        server._server = None  # noqa: SLF001
        server._task = None  # noqa: SLF001

        # Should not raise
        await server.stop()
