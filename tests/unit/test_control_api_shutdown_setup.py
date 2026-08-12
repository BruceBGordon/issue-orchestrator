"""Shutdown and setup control route tests split from test_control_api."""

# ruff: noqa: F403,F405

from tests.unit import test_control_api as _support
from tests.unit.test_control_api import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)


def _complete_setup_payload(repo_root, **overrides):
    payload = {
        "repo_root": str(repo_root),
        "repo_name": "owner/repo",
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


class TestControlCenterShutdownEndpoint:
    """Test /control/shutdown force-stop options."""

    def test_shutdown_does_not_stop_engines_when_not_requested(self):
        mock_supervisor = MagicMock()
        set_supervisor(mock_supervisor)
        try:
            with patch("threading.Thread") as mock_thread:
                client = TestClient(control_app)
                response = client.post(
                    "/control/shutdown", json={"stop_orchestrators": False}
                )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "shutting_down"
            mock_supervisor.stop.assert_not_called()
            mock_thread.assert_called_once()
        finally:
            set_supervisor(build_default_supervisor_ops())

    def test_shutdown_force_stops_running_engines_when_requested(self):
        from issue_orchestrator.entrypoints import control_api

        mock_supervisor = MagicMock()
        mock_supervisor.status.return_value = SimpleNamespace(state="running")
        mock_supervisor.stop_all_instances.return_value = 1
        set_supervisor(mock_supervisor)
        repos = [SimpleNamespace(path="/tmp/repo-a")]
        try:
            with patch.object(
                control_api, "_schedule_control_center_exit", return_value=None
            ):
                with patch(
                    "issue_orchestrator.infra.repo_registry.list_repos",
                    return_value=repos,
                ):
                    with patch("pathlib.Path.exists", return_value=True):
                        with patch("threading.Thread") as mock_thread:
                            client = TestClient(control_app)
                            response = client.post(
                                "/control/shutdown",
                                json={
                                    "stop_orchestrators": True,
                                    "force_orchestrators": True,
                                },
                            )
                            # Worker runs in background thread; execute target inline for deterministic assertions.
                            target = mock_thread.call_args.kwargs.get("target")
                            assert callable(target)
                            target()

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "shutting_down"
            assert data["stopped_orchestrators"] == []
            mock_supervisor.stop_all_instances.assert_called_once()
            stop_args, stop_kwargs = mock_supervisor.stop_all_instances.call_args
            assert str(stop_args[0]) == "/tmp/repo-a"
            assert stop_kwargs["force"] is True
            assert stop_kwargs["force_if_graceful_fails"] is True
            assert stop_kwargs["graceful_timeout_seconds"] == 120
            mock_thread.assert_called_once()
        finally:
            set_supervisor(build_default_supervisor_ops())

    def test_shutdown_marks_failed_when_running_engine_cannot_be_stopped(self):
        from issue_orchestrator.entrypoints import control_api

        mock_supervisor = MagicMock()
        mock_supervisor.status.return_value = SimpleNamespace(state="running")
        mock_supervisor.stop_all_instances.return_value = 0
        set_supervisor(mock_supervisor)
        repos = [SimpleNamespace(path="/tmp/repo-a")]
        try:
            with patch.object(
                control_api, "_schedule_control_center_exit", return_value=None
            ) as schedule_exit:
                with patch(
                    "issue_orchestrator.infra.repo_registry.list_repos",
                    return_value=repos,
                ):
                    with patch("pathlib.Path.exists", return_value=True):
                        with patch("threading.Thread") as mock_thread:
                            client = TestClient(control_app)
                            response = client.post(
                                "/control/shutdown",
                                json={
                                    "stop_orchestrators": True,
                                    "force_orchestrators": True,
                                },
                            )
                            target = mock_thread.call_args.kwargs.get("target")
                            assert callable(target)
                            target()

            assert response.status_code == 200
            op = control_api_shutdown_state.snapshot_shutdown_ops()["global_shutdown"]
            assert op is not None
            assert op["state"] == "failed"
            assert op["failed_orchestrators"] == ["/tmp/repo-a"]
            schedule_exit.assert_not_called()
        finally:
            control_api_shutdown_state.reset_shutdown_operations_for_testing()
            set_supervisor(build_default_supervisor_ops())

    def test_force_and_timeout_updates_reach_current_stop_controller(self, tmp_path):
        from threading import Event

        from fastapi import FastAPI

        from issue_orchestrator.entrypoints.control_api_shutdown_routes import (
            control_shutdown_router,
        )
        from issue_orchestrator.entrypoints.control_api_shutdown_support import (
            ControlApiShutdownDependencies,
            install_control_api_shutdown_dependencies,
        )
        from issue_orchestrator.infra import repo_registry
        from issue_orchestrator.infra.shutdown_timing import (
            InterruptibleStopController,
            StopPolicySnapshot,
        )
        from tests.unit.threading_helpers import wait_for_event

        wait_started = Event()
        resume_probe = Event()
        force_stop_called = Event()
        shutdown_finished = Event()
        observed_policies: list[StopPolicySnapshot] = []

        class RecordingPolicy:
            def __init__(self, policy):  # noqa: ANN001
                self.policy = policy

            def snapshot(self) -> StopPolicySnapshot:
                current = self.policy.snapshot()
                observed_policies.append(current)
                return current

        def process_probe(_pid: int) -> bool:
            wait_started.set()
            wait_for_event(resume_probe, 2, label="resume stop probe")
            return True

        def force_stop() -> bool:
            force_stop_called.set()
            return True

        def stop_all_instances(*args, stop_policy, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
            controller = InterruptibleStopController(
                RecordingPolicy(stop_policy),
                pid=4242,
                force_requested=False,
                force_on_timeout=True,
                request_graceful=lambda: True,
                terminate=lambda: None,
                force_stop=force_stop,
                on_stopped=lambda: None,
                clock=lambda: 0.0,
                sleeper=lambda _seconds: None,
                process_probe=process_probe,
            )
            return 1 if controller.stop() else 0

        fake_supervisor = MagicMock()
        fake_supervisor.status.return_value = SimpleNamespace(state="running")
        fake_supervisor.stop_all_instances.side_effect = stop_all_instances
        app = FastAPI()
        app.include_router(control_shutdown_router)
        install_control_api_shutdown_dependencies(
            app,
            ControlApiShutdownDependencies(
                get_supervisor=lambda: fake_supervisor,
                schedule_control_center_exit=shutdown_finished.set,
            ),
        )
        try:
            with patch.object(
                repo_registry,
                "list_repos",
                return_value=[SimpleNamespace(path=str(tmp_path))],
            ):
                client = TestClient(app)
                response = client.post(
                    "/control/shutdown",
                    json={"stop_orchestrators": True, "force_orchestrators": False},
                )
                assert response.status_code == 200
                wait_for_event(wait_started, 2, label="current stop wait")

                update = client.post(
                    "/control/shutdown/update",
                    json={"graceful_timeout_seconds": 30},
                )
                force = client.post("/control/shutdown/force")
                resume_probe.set()

                assert update.status_code == 200
                assert force.status_code == 200
                wait_for_event(force_stop_called, 2, label="force stop")
                wait_for_event(shutdown_finished, 2, label="global shutdown")

            assert observed_policies[0].graceful_timeout_seconds == 120
            assert observed_policies[0].force is False
            assert observed_policies[-1].graceful_timeout_seconds == 30
            assert observed_policies[-1].force is True
        finally:
            resume_probe.set()
            control_api_shutdown_state.reset_shutdown_operations_for_testing()

    def test_shutdown_reports_superseded_engine_shutdowns(self):
        mock_supervisor = MagicMock()
        set_supervisor(mock_supervisor)
        try:
            with patch(
                "issue_orchestrator.infra.repo_registry.list_repos", return_value=[]
            ):
                with patch("threading.Thread") as mock_thread:
                    control_api_shutdown_state.begin_engine_shutdown_operation(
                        Path("/tmp/repo-a"),
                        force=False,
                        force_if_timeout=False,
                        graceful_timeout_seconds=2,
                    )
                    client = TestClient(control_app)
                    response = client.post(
                        "/control/shutdown",
                        json={"stop_orchestrators": True, "force_orchestrators": False},
                    )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "shutting_down"
            assert data["superseded_engine_shutdowns"] == ["/tmp/repo-a"]
            mock_thread.assert_called_once()
        finally:
            control_api_shutdown_state.reset_shutdown_operations_for_testing()
            set_supervisor(build_default_supervisor_ops())

    def test_shutdown_state_endpoint_returns_global_operation(self):
        try:
            begin_result = control_api_shutdown_state.begin_global_shutdown_operation(
                stop_orchestrators=True,
                force_orchestrators=False,
                graceful_timeout_seconds=2,
            )
            assert not isinstance(
                begin_result, control_api_shutdown_state.GlobalShutdownConflict
            )
            operation_id, _ = begin_result
            client = TestClient(control_app)
            response = client.get("/control/shutdown/state")
            assert response.status_code == 200
            data = response.json()
            assert data["global_shutdown"]["operation_id"] == operation_id
        finally:
            control_api_shutdown_state.reset_shutdown_operations_for_testing()

    def test_shutdown_control_endpoints_update_state(self):
        try:
            begin_result = control_api_shutdown_state.begin_global_shutdown_operation(
                stop_orchestrators=True,
                force_orchestrators=False,
                graceful_timeout_seconds=2,
            )
            assert not isinstance(
                begin_result, control_api_shutdown_state.GlobalShutdownConflict
            )

            client = TestClient(control_app)
            update = client.post(
                "/control/shutdown/update", json={"graceful_timeout_seconds": 30}
            )
            force = client.post("/control/shutdown/force")
            abort = client.post("/control/shutdown/abort")

            assert update.status_code == 200
            assert force.status_code == 200
            assert abort.status_code == 200
            op = control_api_shutdown_state.snapshot_shutdown_ops()["global_shutdown"]
            assert op is not None
            assert op["graceful_timeout_seconds"] == 30
            assert op["force_orchestrators"] is True
            assert op["force_now_requested"] is True
            assert op["abort_requested"] is True
        finally:
            control_api_shutdown_state.reset_shutdown_operations_for_testing()


class TestControlCenterSetupRoutes:
    """Test extracted setup-wizard route behavior."""

    @pytest.fixture(autouse=True)
    def _verified_github_authorization(self):
        from issue_orchestrator.domain.repository_setup_auth import (
            RepositorySetupGitHubAuthorization,
        )
        from issue_orchestrator.ports.repository_setup import (
            RepositorySetupGitHubVerification,
        )

        verification = RepositorySetupGitHubVerification(
            identity="setup-user",
            repository="owner/repo",
            auth_kind="personal",
            source="Environment variable ISSUE_ORCH_GITHUB_TOKEN",
            normalized_authorization=RepositorySetupGitHubAuthorization(
                kind="personal",
                token_env="ISSUE_ORCH_GITHUB_TOKEN",
            ),
        )
        with patch(
            "issue_orchestrator.execution.providers."
            "verify_repository_setup_github_authorization",
            return_value=verification,
        ):
            yield

    def test_setup_prereqs_matches_typed_response_contract(self, tmp_path):
        from issue_orchestrator.contracts.ui_openapi_models import (
            RepositorySetupPrerequisitesPayload,
        )

        response = TestClient(control_app).get(
            "/control/setup/prereqs",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        payload = RepositorySetupPrerequisitesPayload.model_validate(response.json())
        assert "git" in payload.checks
        assert payload.agent_checks

    def test_setup_preview_builds_complete_default_review_pipeline(self, tmp_path):
        """Preview renders the validated reviewer and tech-lead pipeline."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        client = TestClient(control_app)

        response = client.post(
            "/control/setup/preview",
            json=_complete_setup_payload(repo_root),
        )

        assert response.status_code == 200
        data = response.json()
        assert "Issue Orchestrator Configuration" in data["yaml"]
        assert "repo:\n  name: owner/repo\n" in data["yaml"]
        assert f"base: ../worktrees/{repo_root.name}" in data["yaml"]
        assert "sandbox: true" in data["yaml"]
        assert "agent:reviewer" in data["yaml"]
        assert "effort: high" in data["yaml"]
        assert "enabled: true" in data["yaml"]
        assert "mode: via-local-loop" in data["yaml"]
        assert data["worktree_base"] == str(
            repo_root.parent / "worktrees" / repo_root.name
        )
        assert "agent:tech-lead" in data["yaml"]
        assert "tech_lead_follow_up_agent: agent:dev" in data["yaml"]
        assert data["files"][0]["size"] == len(data["yaml"])
        assert {
            row["agent"] for row in data["files"] if row.get("type") == "prompt"
        } == {"agent:dev", "agent:reviewer", "agent:tech-lead"}
        assert not (repo_root / ".issue-orchestrator").exists()

    def test_setup_preview_marks_existing_config_for_overwrite(self, tmp_path):
        """Preview must make replacement of an existing config explicit."""
        repo_root = tmp_path / "repo"
        config_path = (
            repo_root
            / ".issue-orchestrator"
            / "config"
            / "modes"
            / "default"
            / "default.yaml"
        )
        config_path.parent.mkdir(parents=True)
        config_path.write_text("repo:\n  name: old/repo\n")
        client = TestClient(control_app)

        response = client.post(
            "/control/setup/preview",
            json=_complete_setup_payload(repo_root),
        )

        assert response.status_code == 200
        assert response.json()["files"][0]["action"] == "overwrite"

    @pytest.mark.parametrize(
        "config_name",
        ["", "../escaped", "nested/default", "/tmp/escaped.yaml"],
    )
    @pytest.mark.parametrize(
        "endpoint",
        ["/control/setup/preview", "/control/setup/save"],
    )
    def test_setup_routes_reject_unsafe_config_names(
        self,
        tmp_path,
        endpoint,
        config_name,
    ):
        """Preview and save share one traversal-safe config-name contract."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        client = TestClient(control_app)

        response = client.post(
            endpoint,
            json=_complete_setup_payload(
                repo_root,
                config_name=config_name,
                create_labels=False,
            ),
        )

        assert response.status_code in {400, 422}
        assert not (tmp_path / "escaped.yaml").exists()

    @pytest.mark.parametrize(
        "endpoint",
        ["/control/setup/preview", "/control/setup/save"],
    )
    @pytest.mark.parametrize(
        "worker_agent_label",
        ["agent:", "agent:reviewer", "agent:tech-lead"],
    )
    def test_setup_routes_reject_non_worker_agent_labels(
        self,
        tmp_path,
        endpoint,
        worker_agent_label,
    ):
        """Generated request validation and command policy share the label rule."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        response = TestClient(control_app).post(
            endpoint,
            json=_complete_setup_payload(
                repo_root,
                worker_agent_label=worker_agent_label,
                create_labels=False,
            ),
        )

        assert response.status_code == 422
        assert not (repo_root / ".issue-orchestrator").exists()

    def test_setup_detect_ignores_non_default_config_files(self, tmp_path):
        """Detect should only surface the legacy default config file."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        config_dir = repo_root / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "custom.yaml").write_text("repo:\n  name: owner/repo\n")
        (repo_root / "Makefile").write_text(
            (
                "validate-fast:\n\t@true\n\n"
                "validate-pr-raw:\n\t@true\n\n"
                "validate-pr:\n\t@true\n"
            )
        )

        client = TestClient(control_app)
        response = client.get(
            "/control/setup/detect",
            params={"repo_root": str(repo_root)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["config_path"] is None
        assert data["existing_config"] is None
        assert data["worktree_base_default"] == f"../worktrees/{repo_root.name}"
        assert data["worktree_base_resolved"] == str(
            repo_root.parent / "worktrees" / repo_root.name
        )
        assert data["validation_defaults"] == {
            "quick_command": "make validate-fast",
            "publish_command": "make validate-pr-raw",
            "source": "Makefile targets",
        }

    def test_setup_detect_never_returns_existing_inline_token(self, tmp_path):
        """Existing legacy secrets stay server-side until the user replaces them."""
        repo_root = tmp_path / "repo"
        config_path = repo_root / ".issue-orchestrator/config/default.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "repo:\n"
            "  name: owner/repo\n"
            "  github:\n"
            "    token: ghp_super_secret\n"
            "    api_url: https://github.example/api/v3\n"
            "    http_timeout_seconds: 47\n"
        )

        response = TestClient(control_app).get(
            "/control/setup/detect",
            params={"repo_root": str(repo_root)},
        )

        assert response.status_code == 200
        assert "ghp_super_secret" not in response.text
        data = response.json()
        assert "token" not in data["existing_config"]["repo"]["github"]
        assert data["github_authorization"] == {
            "authorization": {
                "kind": "detected",
                "api_url": "https://github.example/api/v3",
                "http_timeout_seconds": 47,
            },
            "configured_kind": "personal",
            "inline_token_migration_required": True,
        }

    def test_setup_github_verify_is_non_mutating_and_explains_scope(self, tmp_path):
        """The guided gate verifies access before any repository artifacts exist."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        response = TestClient(control_app).post(
            "/control/setup/github-auth/verify",
            json={
                "repo_root": str(repo_root),
                "repo_name": "owner/repo",
                "authorization": {
                    "kind": "detected",
                    "api_url": "https://api.github.com",
                    "http_timeout_seconds": 20,
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["verified"] is True
        assert data["identity"] == "setup-user"
        assert data["authorization"] == {
            "kind": "personal",
            "token_env": "ISSUE_ORCH_GITHUB_TOKEN",
            "api_url": "https://api.github.com",
            "http_timeout_seconds": 20,
        }
        assert "without making GitHub writes" in data["verification_note"]
        assert "Pull requests: read and write" in data["required_permissions"]
        assert not (repo_root / ".issue-orchestrator").exists()

    def test_setup_github_app_verify_explains_bot_authorship(self, tmp_path):
        """App mode leaves the operator eligible to review the bot-authored PR."""
        from issue_orchestrator.domain.repository_setup_auth import (
            RepositorySetupGitHubAuthorization,
        )
        from issue_orchestrator.ports.repository_setup import (
            RepositorySetupGitHubVerification,
        )

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        authorization = RepositorySetupGitHubAuthorization(
            kind="github_app",
            app_client_id="Iv23example",
            app_installation_id="145305179",
            app_private_key_env="ISSUE_ORCH_GITHUB_APP_PRIVATE_KEY",
        )
        verification = RepositorySetupGitHubVerification(
            identity="porchpin-bot[bot]",
            repository="owner/repo",
            auth_kind="github_app",
            source="GitHub App installation 145305179",
            normalized_authorization=authorization,
        )
        dependencies = control_app.state.control_api_setup_dependencies

        with patch.object(
            dependencies.setup_owner,
            "verify_github_authorization",
            return_value=verification,
        ):
            response = TestClient(control_app).post(
                "/control/setup/github-auth/verify",
                json={
                    "repo_root": str(repo_root),
                    "repo_name": "owner/repo",
                    "authorization": {
                        "kind": "github_app",
                        "api_url": "https://api.github.com",
                        "http_timeout_seconds": 20,
                        "app_client_id": "Iv23example",
                        "app_installation_id": "145305179",
                        "app_private_key_env": ("ISSUE_ORCH_GITHUB_APP_PRIVATE_KEY"),
                    },
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "operator remains eligible to approve" in data["authorship_notice"]
        assert "Checks: read" in data["required_permissions"]
        assert "Commit statuses: read" in data["required_permissions"]

    def test_setup_personal_token_is_verified_then_stored_by_reference(
        self,
        tmp_path,
    ):
        """The raw PAT is never returned and YAML receives only its keyring locator."""
        from issue_orchestrator.domain.repository_setup_auth import (
            RepositorySetupGitHubAuthorization,
        )
        from issue_orchestrator.ports.repository_setup import (
            RepositorySetupGitHubVerification,
        )

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        stored = RepositorySetupGitHubAuthorization(
            kind="personal",
            keyring_service="issue-orchestrator",
            keyring_username="github-token:github.example:owner/repo",
            api_url="https://github.example/api/v3",
            http_timeout_seconds=47,
        )
        inline_verification = RepositorySetupGitHubVerification(
            identity="setup-user",
            repository="owner/repo",
            auth_kind="personal",
            source="Inline token from setup request",
            normalized_authorization=RepositorySetupGitHubAuthorization(
                kind="personal",
                token="ghp_super_secret",
                api_url="https://github.example/api/v3",
                http_timeout_seconds=47,
            ),
        )
        stored_verification = RepositorySetupGitHubVerification(
            identity="setup-user",
            repository="owner/repo",
            auth_kind="personal",
            source=(
                "Keyring "
                "(issue-orchestrator/github-token:github.example:owner/repo)"
            ),
            normalized_authorization=stored,
        )
        dependencies = control_app.state.control_api_setup_dependencies

        with (
            patch.object(
                dependencies.setup_owner,
                "verify_github_authorization",
                side_effect=[inline_verification, stored_verification],
            ) as verify,
            patch(
                "issue_orchestrator.execution.providers."
                "store_repository_setup_github_token",
                return_value=stored,
            ) as store,
        ):
            response = TestClient(control_app).post(
                "/control/setup/github-auth/store-personal-token",
                json={
                    "repo_root": str(repo_root),
                    "repo_name": "owner/repo",
                    "token": "ghp_super_secret",
                    "api_url": "https://github.example/api/v3",
                    "http_timeout_seconds": 47,
                },
            )

        assert response.status_code == 200
        assert "ghp_super_secret" not in response.text
        assert response.json()["authorization"] == {
            "kind": "personal",
            "keyring_service": "issue-orchestrator",
            "keyring_username": "github-token:github.example:owner/repo",
            "api_url": "https://github.example/api/v3",
            "http_timeout_seconds": 47,
        }
        assert verify.call_count == 2
        store.assert_called_once_with(
            RepositorySetupGitHubAuthorization(
                kind="personal",
                token="ghp_super_secret",
                api_url="https://github.example/api/v3",
                http_timeout_seconds=47,
            ),
            repo="owner/repo",
        )

    def test_setup_save_executes_complete_default_review_pipeline(self, tmp_path):
        """Save writes runnable worker, reviewer, and tech-lead artifacts."""
        from issue_orchestrator.infra.config import Config

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        host = MagicMock()
        host.list_labels.return_value = []

        with patch(
            "issue_orchestrator.execution.providers.create_repository_setup_host",
            return_value=host,
        ):
            client = TestClient(control_app)
            response = client.post(
                "/control/setup/save",
                json=_complete_setup_payload(
                    repo_root,
                    worker_agent_label="agent:backend",
                    config_name="default",
                    create_prompts=True,
                    create_labels=True,
                ),
            )

        assert response.status_code == 200
        data = response.json()
        assert "priority:high" in data["created_labels"]
        assert "agent:backend" in data["created_labels"]
        assert "agent:reviewer" in data["created_labels"]
        assert "agent:tech-lead" in data["created_labels"]
        assert "needs-code-review" in data["created_labels"]
        assert "code-reviewed" in data["created_labels"]
        assert "needs-tech-lead-review" in data["created_labels"]
        assert "tech-lead-reviewed" in data["created_labels"]

        config_path = (
            repo_root
            / ".issue-orchestrator"
            / "config"
            / "modes"
            / "default"
            / "default.yaml"
        )
        config_text = config_path.read_text()
        assert "Issue Orchestrator Configuration" in config_text
        assert "repo:\n  name: owner/repo\n" in config_text
        assert f"base: ../worktrees/{repo_root.name}" in config_text
        assert config_text.count("sandbox: true") == 3
        assert config_text.count("effort: high") == 3
        assert "default: agent:reviewer" in config_text
        assert "tech_lead_review_threshold: 1" in config_text
        assert (repo_root / ".io" / "dev.md").is_file()
        assert (repo_root / ".io" / "reviewer.md").is_file()
        assert (repo_root / ".io" / "tech-lead.md").is_file()
        assert Config.load(config_path).validate() == []

    @pytest.mark.parametrize("stage", ["files", "labels"])
    def test_setup_save_surfaces_required_artifact_failures(
        self,
        tmp_path,
        stage,
    ):
        """The HTTP adapter must not convert owner failures into saved results."""
        from issue_orchestrator.control.repository_setup import (
            RepositorySetupExecutionError,
        )

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        dependencies = control_app.state.control_api_setup_dependencies
        failure = RepositorySetupExecutionError(
            stage=stage,
            detail=f"{stage} failed",
            applied_files=(repo_root / "partial",),
            created_labels=("agent:dev",) if stage == "labels" else (),
        )

        with patch.object(
            dependencies.setup_owner,
            "execute",
            side_effect=failure,
        ):
            response = TestClient(control_app).post(
                "/control/setup/save",
                json=_complete_setup_payload(repo_root),
            )

        assert response.status_code == 500
        assert response.json() == {
            "error": "repository_setup_failed",
            "stage": stage,
            "detail": f"{stage} failed",
            "applied_files": [str(repo_root / "partial")],
            "created_labels": ["agent:dev"] if stage == "labels" else [],
        }
