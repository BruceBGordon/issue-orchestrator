"""Supervisor control route tests split from test_control_api."""

# ruff: noqa: F403,F405

from tests.unit import test_control_api as _support
from tests.unit.test_control_api import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)


class TestSupervisorStatus:
    """Tests for GET /control/orchestrator/status endpoint."""

    def test_status_returns_stopped_when_no_lock(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return stopped state when no orchestrator is running."""
        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "stopped"

    def test_status_returns_running_with_lock(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return running state when lock exists and process is alive."""
        # Create lock file with current process PID
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        lock_data = {
            "repo_root": str(tmp_path),
            "pid": os.getpid(),
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8080,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "running"
        assert data["pid"] == os.getpid()

    def test_status_returns_orphaned_when_detected(
        self, supervisor_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Return running state when untracked orchestrator is detected."""
        from issue_orchestrator.entrypoints import control_api_orchestrator_routes

        def fake_detect(repo_root: Path, config_name: str, **_: object) -> dict:
            return {
                "port": 19080,
                "health": "ok",
                "tick_age_seconds": 1.2,
                "status": {"shutdown_requested": False, "active_sessions": []},
            }

        monkeypatch.setattr(
            control_api_orchestrator_routes,
            "detect_orchestrator_by_port",
            fake_detect,
        )

        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "running"
        assert data["orphaned"] is True
        assert data["port"] == 19080

    def test_status_rejects_invalid_repo_root(
        self, supervisor_client: TestClient
    ) -> None:
        """Return 400 for invalid repo_root."""
        response = supervisor_client.get(
            "/control/orchestrator/status",
            params={"repo_root": "/nonexistent/path"},
        )

        assert response.status_code == 400
        assert "Invalid" in response.json()["error"]

    def test_status_rejects_missing_repo_root(
        self, supervisor_client: TestClient
    ) -> None:
        """Return 422 when repo_root is missing."""
        response = supervisor_client.get("/control/orchestrator/status")

        assert response.status_code == 422  # FastAPI validation error


class TestSupervisorStop:
    """Tests for POST /control/orchestrator/stop endpoint."""

    def test_stop_returns_stopped_when_no_lock(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return stopped when no orchestrator is running (goal achieved)."""
        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": str(tmp_path), "reason": "test stop with no lock"},
        )

        assert response.status_code == 200
        data = response.json()
        # When no lock exists, the orchestrator is already stopped - goal achieved
        assert data["status"] == "stopped"

    def test_stop_uses_cleanup_safe_graceful_timeout_by_default(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_supervisor: MagicMock,
    ) -> None:
        mock_supervisor.status.return_value = SupervisorStatus(state="running")
        mock_supervisor.stop_all_instances.return_value = 1

        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": str(tmp_path), "reason": "test graceful default"},
        )

        assert response.status_code == 200
        assert mock_supervisor.stop_all_instances.call_args.kwargs[
            "graceful_timeout_seconds"
        ] == 120

    def test_stop_rejects_missing_reason(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return 400 when 'reason' is missing — the contract is
        "tell us why" so the target log records the calling intent
        (the signal handler can't attribute SIGTERM to a caller)."""
        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 400
        payload = response.json()
        assert payload["error"] == "reason is required"
        assert "hint" in payload

    def test_stop_rejects_empty_reason(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Whitespace-only reason is treated as missing."""
        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": str(tmp_path), "reason": "   "},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "reason is required"

    def test_stop_rejects_invalid_repo_root(
        self, supervisor_client: TestClient
    ) -> None:
        """Return 400 for invalid repo_root."""
        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": "/nonexistent/path", "reason": "test"},
        )

        assert response.status_code == 400
        assert "Invalid" in response.json()["error"]

    def test_stop_rejects_invalid_json(self, supervisor_client: TestClient) -> None:
        """Return 400 for invalid JSON."""
        response = supervisor_client.post(
            "/control/orchestrator/stop",
            content="not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["error"]

    def test_stop_rejects_invalid_port(self, supervisor_client: TestClient, tmp_path: Path) -> None:
        """Return 400 for invalid port."""
        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": str(tmp_path), "reason": "test", "port": -1},
        )

        assert response.status_code == 400
        assert "Invalid port" in response.json()["error"]

    def test_stop_returns_port_mismatch(
        self, supervisor_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        """Return 409 when port does not match orchestrator."""
        from issue_orchestrator.entrypoints import control_api_orchestrator_routes

        mock_supervisor.status.return_value = SupervisorStatus(state="stopped")
        monkeypatch.setattr(
            control_api_orchestrator_routes,
            "confirm_orchestrator_at_port",
            lambda *_, **__: False,
        )

        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": str(tmp_path), "reason": "test", "port": 19080},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "port_mismatch"

    def test_stop_blocked_when_global_shutdown_in_progress(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            control_app.state,
            "control_api_orchestrator_dependencies",
            replace(
                control_app.state.control_api_orchestrator_dependencies,
                global_shutdown_in_progress=lambda: True,
            ),
        )

        response = supervisor_client.post(
            "/control/orchestrator/stop",
            json={"repo_root": str(tmp_path), "reason": "test"},
        )

        assert response.status_code == 409
        payload = response.json()
        assert payload["error"] == "global_shutdown_in_progress"


class TestSupervisorReconcile:
    """Tests for POST /control/orchestrator/reconcile endpoint."""

    def test_reconcile_cleans_stale_locks(
        self, supervisor_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_supervisor: MagicMock
    ) -> None:
        mock_supervisor.status_all_instances.return_value = MultiInstanceStatus(
            repo_root=str(tmp_path),
            expected_count=1,
            instances=[],
        )
        mock_supervisor.status.return_value = SupervisorStatus(state="failed", pid=123, error="stale lock")
        monkeypatch.setattr(
            "issue_orchestrator.infra.repo_registry.list_repos",
            lambda: [SimpleNamespace(path=str(tmp_path), selected_config="default.yaml")],
        )

        response = supervisor_client.post("/control/orchestrator/reconcile", json={})

        assert response.status_code == 200
        data = response.json()
        assert str(tmp_path) in data["reconciled_stale_locks"]

    def test_reconcile_reports_orphaned_and_can_stop(
        self, supervisor_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_supervisor: MagicMock
    ) -> None:
        from issue_orchestrator.entrypoints import control_api_orchestrator_routes

        mock_supervisor.status_all_instances.return_value = MultiInstanceStatus(
            repo_root=str(tmp_path),
            expected_count=1,
            instances=[],
        )
        mock_supervisor.status.return_value = SupervisorStatus(state="stopped")
        monkeypatch.setattr(
            "issue_orchestrator.infra.repo_registry.list_repos",
            lambda: [SimpleNamespace(path=str(tmp_path), selected_config="default.yaml")],
        )
        monkeypatch.setattr(
            control_api_orchestrator_routes,
            "detect_orchestrator_by_port",
            lambda *_, **__: {"port": 19080, "status": {}},
        )

        response = supervisor_client.post("/control/orchestrator/reconcile", json={"stop_orphaned": True})

        assert response.status_code == 200
        data = response.json()
        assert data["orphaned_detected"][0]["port"] == 19080
        assert str(tmp_path) in data["stopped_orphaned"]


@pytest.fixture
def mock_control_actions():
    """Inject mocked command-backed actions for endpoint mapping tests."""
    actions = MagicMock()
    actions.pause_cmd = MagicMock()
    actions.pause_cmd.execute = AsyncMock(return_value=ActionResult({"status": "paused"}))
    actions.resume_cmd = MagicMock()
    actions.resume_cmd.execute = AsyncMock(return_value=ActionResult({"status": "resumed"}))
    actions.refresh_cmd = MagicMock()
    actions.refresh_cmd.execute = AsyncMock(return_value=ActionResult({"status": "refresh_requested"}))
    actions.doctor_cmd = MagicMock()
    actions.doctor_cmd.execute = AsyncMock(return_value=ActionResult({"overall": "ok", "checks": []}))
    actions.audit_cmd = MagicMock()
    actions.audit_cmd.execute = AsyncMock(return_value=ActionResult({"entries": []}))
    actions.trace_cmd = MagicMock()
    actions.trace_cmd.execute = AsyncMock(return_value=ActionResult({"entries": ["ok"], "total": 1, "truncated": False}))
    actions.labels_cmd = MagicMock()
    actions.labels_cmd.execute = AsyncMock(return_value=ActionResult({"created": [], "updated": [], "failed": []}))
    actions.stale_worktrees_cmd = MagicMock()
    actions.stale_worktrees_cmd.execute = AsyncMock(return_value=ActionResult({"stale_worktrees": [], "message": "ok"}))
    set_control_actions(actions)
    yield actions
    set_control_actions(ControlCenterActions(supervisor=get_supervisor()))


class TestActionEndpointMapping:
    """Ensure endpoints delegate to command-backed action objects."""

    def test_trace_endpoint_delegates_to_command(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_control_actions: MagicMock,
    ) -> None:
        response = supervisor_client.get(
            "/control/tools/trace",
            params={
                "repo_root": str(tmp_path),
                "issue_number": 4070,
            },
        )

        assert response.status_code == 200
        assert response.json()["entries"] == ["ok"]
        mock_control_actions.trace_cmd.execute.assert_awaited_once()

    def test_worktrees_endpoint_delegates_to_command(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_control_actions: MagicMock,
    ) -> None:
        response = supervisor_client.post(
            "/control/tools/worktrees/cleanup",
            json={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        assert response.json()["message"] == "ok"
        mock_control_actions.stale_worktrees_cmd.execute.assert_awaited_once()

    def test_pause_endpoint_delegates_to_command(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_control_actions: MagicMock,
    ) -> None:
        response = supervisor_client.post(
            "/control/orchestrator/pause",
            json={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "paused"
        mock_control_actions.pause_cmd.execute.assert_awaited_once()

    def test_resume_endpoint_delegates_to_command(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_control_actions: MagicMock,
    ) -> None:
        response = supervisor_client.post(
            "/control/orchestrator/resume",
            json={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "resumed"
        mock_control_actions.resume_cmd.execute.assert_awaited_once()

    def test_refresh_endpoint_delegates_to_command(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_control_actions: MagicMock,
    ) -> None:
        response = supervisor_client.post(
            "/control/orchestrator/refresh",
            json={"repo_root": str(tmp_path), "inflight_stable_ids": ["I_123"]},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "refresh_requested"
        mock_control_actions.refresh_cmd.execute.assert_awaited_once()

    def test_doctor_endpoint_delegates_to_command(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_control_actions: MagicMock,
    ) -> None:
        response = supervisor_client.get(
            "/control/orchestrator/doctor",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        assert response.json()["overall"] == "ok"
        mock_control_actions.doctor_cmd.execute.assert_awaited_once()

    def test_repair_guardrails_runs_setup_repo_guardrails(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
    ) -> None:
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "repo:\n  name: owner/repo\nvalidation:\n  publish:\n    cmd: pytest\n",
            encoding="utf-8",
        )
        repo_root = tmp_path.resolve()
        result = SimpleNamespace(
            repo_root=repo_root,
            hooks_path_config=".githooks",
            hooks_dir=repo_root / ".githooks",
            pre_push_hook=repo_root / ".githooks" / "pre-push",
            verify_script=repo_root / "scripts" / "verify-pr.sh",
            helper_script=repo_root / "scripts" / "agent-hooks" / "block_no_verify.py",
            installed_files=[
                repo_root / "scripts" / "verify-pr.sh",
                repo_root / "scripts" / "agent-hooks" / "block_no_verify.py",
                repo_root / ".githooks" / "pre-push",
            ],
            preserved_files=[repo_root / ".githooks" / "pre-push.project"],
            agent_hook_files={
                "claude-code": [repo_root / ".claude" / "hooks" / "block-no-verify.sh"]
            },
        )

        with patch(
            "issue_orchestrator.entrypoints.control_api_orchestrator_routes.setup_repo_guardrails",
            return_value=result,
        ) as setup_guardrails_mock:
            response = supervisor_client.post(
                "/control/orchestrator/guardrails/repair",
                json={"repo_root": str(tmp_path), "config_name": "default"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "repaired"
        assert data["config_name"] == "default.yaml"
        assert data["installed_files"] == [
            "scripts/verify-pr.sh",
            "scripts/agent-hooks/block_no_verify.py",
            ".githooks/pre-push",
        ]
        assert data["preserved_files"] == [".githooks/pre-push.project"]
        assert data["agent_hook_files"] == {
            "claude-code": [".claude/hooks/block-no-verify.sh"]
        }
        assert "Review and commit changed files" in data["message"]
        setup_guardrails_mock.assert_called_once()
        guardrails_config = setup_guardrails_mock.call_args.args[0]
        assert guardrails_config.repo == "owner/repo"
        assert setup_guardrails_mock.call_args.kwargs["target_root"] == repo_root

    def test_repair_guardrails_rejects_invalid_config_name(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
    ) -> None:
        response = supervisor_client.post(
            "/control/orchestrator/guardrails/repair",
            json={"repo_root": str(tmp_path), "config_name": "../default"},
        )

        assert response.status_code == 400
        assert response.json()["error"] == "Invalid config_name"

    def test_repair_guardrails_returns_missing_config(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
    ) -> None:
        response = supervisor_client.post(
            "/control/orchestrator/guardrails/repair",
            json={"repo_root": str(tmp_path), "config_name": "missing"},
        )

        assert response.status_code == 404
        assert response.json()["error"] == "config_not_found"
        assert response.json()["config_name"] == "missing.yaml"

    def test_repair_guardrails_reports_guardrails_errors(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
    ) -> None:
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "validation:\n  publish:\n    cmd: pytest\n",
            encoding="utf-8",
        )

        with patch(
            "issue_orchestrator.entrypoints.control_api_orchestrator_routes.setup_repo_guardrails",
            side_effect=RepoGuardrailsError("validation.publish.cmd is not configured"),
        ):
            response = supervisor_client.post(
                "/control/orchestrator/guardrails/repair",
                json={"repo_root": str(tmp_path), "config_name": "default.yaml"},
            )

        assert response.status_code == 400
        assert response.json()["error"] == "repair_failed"
        assert response.json()["detail"] == "validation.publish.cmd is not configured"

    def test_audit_endpoint_delegates_to_command(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_control_actions: MagicMock,
    ) -> None:
        response = supervisor_client.get(
            "/control/tools/audit",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        assert response.json()["entries"] == []
        mock_control_actions.audit_cmd.execute.assert_awaited_once()

    def test_labels_endpoint_delegates_to_command(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        mock_control_actions: MagicMock,
    ) -> None:
        response = supervisor_client.post(
            "/control/tools/labels/init",
            json={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        assert response.json()["created"] == []
        mock_control_actions.labels_cmd.execute.assert_awaited_once()


class TestSupervisorReconcileMultiInstance:
    def test_reconcile_multi_instance_handles_stale_and_unresponsive(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        from issue_orchestrator.entrypoints import control_api_orchestrator_routes

        mock_supervisor.status_all_instances.return_value = MultiInstanceStatus(
            repo_root=str(tmp_path),
            expected_count=3,
            instances=[
                SupervisorStatus(state="running", instance_id="orchestrator-1", pid=101, port=19101),
                SupervisorStatus(state="running", instance_id="orchestrator-2", pid=102, port=19102),
            ],
        )

        def status_for_instance(repo_root: Path, instance_id: str | None = None) -> SupervisorStatus:
            del repo_root
            if instance_id is None:
                return SupervisorStatus(state="stopped")
            if instance_id == "orchestrator-1":
                return SupervisorStatus(state="running", instance_id=instance_id, pid=101, port=19101)
            if instance_id == "orchestrator-2":
                return SupervisorStatus(state="running", instance_id=instance_id, pid=102, port=19102)
            if instance_id == "orchestrator-3":
                return SupervisorStatus(state="failed", instance_id=instance_id, pid=103, error="stale lock")
            raise AssertionError(f"Unexpected instance_id {instance_id}")

        mock_supervisor.status.side_effect = status_for_instance

        monkeypatch.setattr(
            "issue_orchestrator.infra.repo_registry.list_repos",
            lambda: [SimpleNamespace(path=str(tmp_path), selected_config="multi.yaml")],
        )
        monkeypatch.setattr(
            control_api_orchestrator_routes,
            "detect_orchestrator_by_port",
            lambda *_, **__: pytest.fail("orphan detector should not run for multi-instance reconcile"),
        )

        def fake_enrich(repo_path: Path, payload: dict[str, object] | None, *, orphaned: bool = False, instance_id: str | None = None):
            del repo_path
            del orphaned
            if payload is None:
                return None
            data = dict(payload)
            if instance_id == "orchestrator-2":
                data["runtime_health"] = "unresponsive"
                data["heartbeat_age_seconds"] = 200
                data["port"] = 19102
                return data
            data["runtime_health"] = "healthy"
            return data

        monkeypatch.setattr(
            control_api_orchestrator_routes,
            "enrich_runtime_health",
            fake_enrich,
        )

        response = supervisor_client.post("/control/orchestrator/reconcile", json={"stop_unresponsive": True})

        assert response.status_code == 200
        data = response.json()
        assert str(tmp_path) in data["reconciled_stale_locks"]
        assert str(tmp_path) in data["stopped_unresponsive"]
        assert data["orphaned_detected"] == []
        assert data["unresponsive_detected"] == [{
            "repo_root": str(tmp_path),
            "instance_id": "orchestrator-2",
            "heartbeat_age_seconds": 200,
            "pid": 102,
            "port": 19102,
        }]
        mock_supervisor.stop.assert_any_call(
            tmp_path,
            force=False,
            instance_id="orchestrator-3",
            reason="reconcile-runtime: stale lock for failed multi-instance orchestrator",
            actor="control-center.reconcile",
        )
        mock_supervisor.stop_by_port.assert_any_call(
            19102,
            force=False,
            reason="reconcile-runtime: stop unresponsive multi-instance orchestrator",
            actor="control-center.reconcile",
        )


class TestSupervisorStart:
    """Tests for POST /control/orchestrator/start endpoint."""

    def test_start_rejects_invalid_repo_root(
        self, supervisor_client: TestClient
    ) -> None:
        """Return 400 for invalid repo_root."""
        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={"repo_root": "/nonexistent/path"},
        )

        assert response.status_code == 400
        assert "Invalid" in response.json()["error"]


    def test_start_rejects_invalid_port(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return 400 for invalid port."""
        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(tmp_path), "port": -1},
        )

        assert response.status_code == 400
        assert "Invalid port" in response.json()["error"]

    def test_start_rejects_invalid_port_type(
        self, supervisor_client: TestClient, tmp_path: Path
    ) -> None:
        """Return 400 for non-integer port."""
        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(tmp_path), "port": "not a number"},
        )

        assert response.status_code == 400
        assert "Invalid port" in response.json()["error"]

    def test_start_reports_orphaned_when_detected(
        self, supervisor_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Return 409 when an untracked orchestrator is detected."""
        from issue_orchestrator.entrypoints import control_api_orchestrator_routes

        monkeypatch.setattr(
            control_api_orchestrator_routes,
            "detect_orchestrator_by_port",
            lambda *_, **__: {"port": 19080, "health": "ok"},
        )

        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(tmp_path), "config_name": "default.yaml"},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "orphaned_running"

    def test_start_auto_restarts_identity_mismatch(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        """Identity mismatch should be stopped and relaunched without user intervention."""
        from issue_orchestrator.entrypoints import control_api_orchestrator_routes
        from issue_orchestrator.infra import launcher
        from issue_orchestrator.infra.doctor.types import DoctorResult
        from issue_orchestrator.infra.launcher import LaunchResult, LaunchStatus

        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        monkeypatch.setattr(
            control_api_orchestrator_routes,
            "detect_orchestrator_by_port",
            lambda *_, **__: {
                "port": 19080,
                "identity_mismatch": {"commit_sha": {"expected": "abc", "observed": "def"}},
                "expected_identity": {"commit_sha": "abc"},
                "observed_identity": {"commit_sha": "def"},
            },
        )
        monkeypatch.setattr(
            launcher,
            "launch_subprocess",
            lambda **kwargs: LaunchResult(
                doctor=DoctorResult(checks=[]),
                launched=True,
                status=LaunchStatus.OK,
                supervisor={"pid": 123, "port": 19080},
            ),
        )

        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(tmp_path), "config_name": "default.yaml"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "started"
        mock_supervisor.stop_by_port.assert_called_once_with(
            19080,
            force=True,
            reason="engine identity mismatch detected on /control/start",
            actor="control-center",
        )

    def test_annotate_identity_mismatch_ignores_dirty_state_drift(
        self,
    ) -> None:
        """Volatile dirty-state fields should not trigger identity mismatch."""
        from issue_orchestrator.execution.control_center_runtime import annotate_identity_mismatch
        from issue_orchestrator.infra.repo_identity import RepoIdentity

        expected = RepoIdentity(
            repo_root="/repo",
            commit_sha="abc",
            branch="main",
            working_tree_dirty=False,
            dirty_fingerprint=None,
            source_root="/src",
        )
        info = {
            "repo_identity": {
                "repo_root": "/repo",
                "commit_sha": "abc",
                "branch": "main",
                "working_tree_dirty": True,
                "dirty_fingerprint": "abcd1234",
                "source_root": "/src",
            }
        }
        details: dict[str, object] = {}

        annotate_identity_mismatch(
            details,
            info,
            expected,
        )
        assert "identity_mismatch" not in details

    def test_start_identity_mismatch_stop_failure_returns_409(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        """Identity mismatch with failed stop should fail closed."""
        from issue_orchestrator.entrypoints import control_api_orchestrator_routes

        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        mock_supervisor.stop_by_port.return_value = False
        monkeypatch.setattr(
            control_api_orchestrator_routes,
            "detect_orchestrator_by_port",
            lambda *_, **__: {
                "port": 19080,
                "identity_mismatch": {"commit_sha": {"expected": "abc", "observed": "def"}},
                "expected_identity": {"commit_sha": "abc"},
                "observed_identity": {"commit_sha": "def"},
            },
        )

        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(tmp_path), "config_name": "default.yaml"},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "engine_identity_mismatch"

    def test_start_force_restart_stops_orphaned(
        self, supervisor_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        """Force restart should stop the orphaned process before starting."""
        from issue_orchestrator.entrypoints import control_api_orchestrator_routes
        from issue_orchestrator.infra import launcher
        from issue_orchestrator.infra.repo_lock import LockInfo
        from issue_orchestrator.infra.doctor.types import DoctorResult

        # Create config file (required since start endpoint loads config to check instances)
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_DIR", str(tmp_path / "config"))
        # Mock doctor checks to pass (launcher runs doctor before supervisor.start)
        monkeypatch.setattr(
            launcher,
            "run_doctor",
            lambda **_kwargs: DoctorResult(checks=[]),
        )
        monkeypatch.setattr(
            control_api_orchestrator_routes,
            "detect_orchestrator_by_port",
            lambda *_, **__: {"port": 19080, "health": "ok"},
        )
        mock_supervisor.stop_by_port.return_value = True
        mock_supervisor.start.return_value = LockInfo(
            repo_root=str(tmp_path),
            pid=123,
            started_at="",
            http_port=19080,
            state_dir=str(tmp_path / ".issue-orchestrator" / "state"),
            recovered=False,
        )

        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={
                "repo_root": str(tmp_path),
                "config_name": "default.yaml",
                "force_restart": True,
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "started"

    def test_start_forwards_start_paused_to_launcher(
        self,
        supervisor_client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Start Paused request body is preserved across the control route."""
        from issue_orchestrator.entrypoints import control_api_orchestrator_routes
        from issue_orchestrator.infra import launcher
        from issue_orchestrator.infra.doctor.types import DoctorResult
        from issue_orchestrator.infra.launcher import LaunchResult, LaunchStatus

        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        captured: dict[str, object] = {}

        monkeypatch.setattr(
            control_api_orchestrator_routes,
            "detect_orchestrator_by_port",
            lambda *_, **__: None,
        )

        def fake_launch_subprocess(**kwargs: object) -> LaunchResult:
            captured.update(kwargs)
            return LaunchResult(
                doctor=DoctorResult(checks=[]),
                launched=True,
                status=LaunchStatus.OK,
                supervisor={"pid": 123, "port": 19080},
            )

        monkeypatch.setattr(launcher, "launch_subprocess", fake_launch_subprocess)

        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={
                "repo_root": str(tmp_path),
                "config_name": "default.yaml",
                "start_paused": True,
            },
        )

        assert response.status_code == 200
        assert captured["start_paused"] is True

    def test_start_returns_422_when_doctor_fails(
        self, supervisor_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        mock_supervisor: MagicMock,
    ) -> None:
        """Return 422 with doctor_failed when preflight checks fail."""
        from issue_orchestrator.infra import launcher
        from issue_orchestrator.infra.doctor.types import Check, DoctorResult
        from issue_orchestrator.infra.launcher import LaunchResult, LaunchStatus

        # Create config file
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        # Launcher returns a doctor failure with a concrete failing check.
        monkeypatch.setattr(
            launcher,
            "launch_subprocess",
            lambda **_kw: LaunchResult(
                doctor=DoctorResult(
                    checks=[Check(name="Hooks", status="error", detail="not installed")]
                ),
                launched=False,
                status=LaunchStatus.DOCTOR_ERROR,
            ),
        )

        response = supervisor_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(tmp_path), "config_name": "default.yaml"},
        )

        assert response.status_code == 422
        data = response.json()
        assert data["error"] == "doctor_failed"
        assert data["detail"] == "Pre-flight checks failed: Hooks: not installed"
        assert data["doctor"]["overall"] == "error"
        mock_supervisor.start.assert_not_called()
