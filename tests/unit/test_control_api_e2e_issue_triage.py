"""E2E issue triage control route tests split from test_control_api."""

# ruff: noqa: F403,F405,SLF001

from tests.unit import test_control_api as _support
from tests.unit.test_control_api import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestE2ESyncIssuesEndpoint:
    """Test the POST /control/e2e/sync-issues/{run_id} endpoint."""

    @pytest.fixture
    def mock_orchestrator_with_tracker(self):
        """Create a mock orchestrator with GitHub client for E2E issue tracking."""
        mock = create_mock_orchestrator()

        # Mock repository_host with http_client
        mock.repository_host = MagicMock()
        mock.repository_host.http_client = MagicMock()

        # Mock close_issue_with_comment behavior
        mock.repository_host.http_client.add_comment = MagicMock()
        mock.repository_host.http_client.update_issue_state = MagicMock()

        return mock

    @pytest.fixture
    def sync_client(self, mock_orchestrator_with_tracker):
        """Create a test client with orchestrator for sync endpoint."""
        set_orchestrator(mock_orchestrator_with_tracker)
        yield TestClient(control_app)
        set_orchestrator(None)

    def test_sync_returns_503_when_no_orchestrator(self, tmp_path):
        """Should return 503 when orchestrator is not running."""
        set_orchestrator(None)
        client = TestClient(control_app)
        response = client.post(
            "/control/e2e/sync-issues/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 503
        assert response.json()["error"] == "Orchestrator not running"

    def test_sync_returns_400_for_invalid_repo_root(self, sync_client):
        """Invalid repo_root should return 400."""
        response = sync_client.post(
            "/control/e2e/sync-issues/1",
            params={"repo_root": "../invalid/path"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_sync_returns_404_when_db_not_found(self, sync_client, tmp_path):
        """Missing E2E database should return 404."""
        response = sync_client.post(
            "/control/e2e/sync-issues/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_sync_returns_404_for_unknown_run(self, sync_client, tmp_path):
        """Unknown run_id should return 404."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        E2EDB(db_dir / "e2e.db")

        response = sync_client.post(
            "/control/e2e/sync-issues/999",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_sync_closes_issues_for_passing_tests(self, sync_client, tmp_path):
        """Sync should close issues for tests that now pass."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db = E2EDB(db_dir / "e2e.db")

        # Create a run where test_a failed
        run1_id = db.start_run(str(tmp_path), "test-orch", ["tests/e2e"], commit_sha="sha1")
        db.upsert_test_result(run1_id, "test_a.py::test_failing", "failed", longrepr="Error")
        db.finish_run(run1_id, "failed", exit_code=1)

        # Record a failure issue for test_a
        db.record_failure_issue(
            nodeid="test_a.py::test_failing",
            github_issue_number=101,
            parent_issue_number=100,
            first_failing_run_id=run1_id,
            first_failing_sha="sha1",
        )
        db.record_run_issue(run1_id, 100)

        # Create a new run where test_a now passes
        run2_id = db.start_run(str(tmp_path), "test-orch", ["tests/e2e"], commit_sha="sha2")
        db.upsert_test_result(run2_id, "test_a.py::test_failing", "passed")
        db.finish_run(run2_id, "passed", exit_code=0)

        response = sync_client.post(
            f"/control/e2e/sync-issues/{run2_id}",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "synced"
        assert len(data["closed_issues"]) == 1
        assert data["closed_issues"][0]["number"] == 101
        assert data["closed_issues"][0]["nodeid"] == "test_a.py::test_failing"
        # Parent should also be closed since all sub-issues are resolved
        assert 100 in data["closed_parent_issues"]

    def test_sync_does_not_close_still_failing_tests(self, sync_client, tmp_path):
        """Sync should not close issues for tests that still fail."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db = E2EDB(db_dir / "e2e.db")

        # Create a run where test_a failed
        run1_id = db.start_run(str(tmp_path), "test-orch", ["tests/e2e"], commit_sha="sha1")
        db.upsert_test_result(run1_id, "test_a.py::test_failing", "failed", longrepr="Error")
        db.finish_run(run1_id, "failed", exit_code=1)

        db.record_failure_issue(
            nodeid="test_a.py::test_failing",
            github_issue_number=101,
            parent_issue_number=100,
            first_failing_run_id=run1_id,
            first_failing_sha="sha1",
        )

        # Create a new run where test_a STILL fails
        run2_id = db.start_run(str(tmp_path), "test-orch", ["tests/e2e"], commit_sha="sha2")
        db.upsert_test_result(run2_id, "test_a.py::test_failing", "failed", longrepr="Error")
        db.finish_run(run2_id, "failed", exit_code=1)

        response = sync_client.post(
            f"/control/e2e/sync-issues/{run2_id}",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "synced"
        assert len(data["closed_issues"]) == 0
        assert len(data["closed_parent_issues"]) == 0


class TestE2EQuarantineModifyEndpoint:
    """Test the POST /control/e2e/quarantine endpoint."""

    @pytest.fixture
    def quarantine_client(self):
        """Create a test client for quarantine endpoint (no orchestrator needed)."""
        return TestClient(control_app)

    @staticmethod
    def _write_config(tmp_path: Path) -> None:
        """Write a minimal config with quarantine_file set."""
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "default.yaml").write_text(
            "repo:\n  name: test/repo\ne2e:\n  enabled: true\n"
            "  pytest_paths: ['tests/e2e']\n  quarantine_file: tests/e2e/quarantine.txt\n"
        )

    def test_quarantine_modify_returns_400_for_invalid_repo_root(self, quarantine_client):
        """Invalid repo_root should return 400."""
        response = quarantine_client.post(
            "/control/e2e/quarantine",
            params={"repo_root": "../invalid/path", "config_name": "default.yaml"},
            json={"action": "add", "nodeids": ["test::foo"]}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_quarantine_modify_requires_action(self, quarantine_client, tmp_path):
        """Missing action should return 400."""
        response = quarantine_client.post(
            "/control/e2e/quarantine",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"},
            json={"nodeids": ["test::foo"]}
        )
        assert response.status_code == 400
        assert "action" in response.json()["error"]

    def test_quarantine_modify_requires_nodeids(self, quarantine_client, tmp_path):
        """Empty nodeids should return 400."""
        response = quarantine_client.post(
            "/control/e2e/quarantine",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"},
            json={"action": "add", "nodeids": []}
        )
        assert response.status_code == 400
        assert "nodeids" in response.json()["error"]

    def test_quarantine_add_tests(self, quarantine_client, tmp_path):
        """Should add tests to quarantine file."""
        self._write_config(tmp_path)
        # Create empty quarantine file
        quarantine_dir = tmp_path / "tests" / "e2e"
        quarantine_dir.mkdir(parents=True)
        quarantine_file = quarantine_dir / "quarantine.txt"
        quarantine_file.write_text("# Header\n")

        response = quarantine_client.post(
            "/control/e2e/quarantine",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"},
            json={"action": "add", "nodeids": ["test::foo", "test::bar"]}
        )
        assert response.status_code == 200
        data = response.json()

        assert len(data["added"]) == 2
        assert "test::foo" in data["tests"]
        assert "test::bar" in data["tests"]
        assert data["count"] == 2

        # Verify file was updated
        content = quarantine_file.read_text()
        assert "test::foo" in content
        assert "test::bar" in content

    def test_quarantine_remove_tests(self, quarantine_client, tmp_path):
        """Should remove tests from quarantine file."""
        self._write_config(tmp_path)
        # Create quarantine file with tests
        quarantine_dir = tmp_path / "tests" / "e2e"
        quarantine_dir.mkdir(parents=True)
        quarantine_file = quarantine_dir / "quarantine.txt"
        quarantine_file.write_text("# Header\ntest::foo\ntest::bar\ntest::baz\n")

        response = quarantine_client.post(
            "/control/e2e/quarantine",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"},
            json={"action": "remove", "nodeids": ["test::foo", "test::bar"]}
        )
        assert response.status_code == 200
        data = response.json()

        assert len(data["removed"]) == 2
        assert "test::foo" not in data["tests"]
        assert "test::bar" not in data["tests"]
        assert "test::baz" in data["tests"]
        assert data["count"] == 1


class TestE2EFlakyTestsEndpoint:
    """Test the GET /control/e2e/flaky-tests endpoint."""

    @pytest.fixture
    def flaky_client(self):
        """Create a test client for flaky tests endpoint (no orchestrator needed)."""
        return TestClient(control_app)

    @staticmethod
    def _write_config(tmp_path: Path) -> None:
        """Write a minimal config with quarantine_file set."""
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "default.yaml").write_text(
            "repo:\n  name: test/repo\ne2e:\n  enabled: true\n"
            "  pytest_paths: ['tests/e2e']\n  quarantine_file: tests/e2e/quarantine.txt\n"
        )

    def test_flaky_returns_400_for_invalid_repo_root(self, flaky_client):
        """Invalid repo_root should return 400."""
        response = flaky_client.get(
            "/control/e2e/flaky-tests",
            params={"repo_root": "../invalid/path", "config_name": "default.yaml"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_flaky_returns_404_when_db_not_found(self, flaky_client, tmp_path):
        """Missing E2E database should return 404."""
        response = flaky_client.get(
            "/control/e2e/flaky-tests",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_flaky_returns_empty_when_no_flaky_tests(self, flaky_client, tmp_path):
        """Should return empty list when no flaky tests."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        self._write_config(tmp_path)
        db_dir = tmp_path / ".issue-orchestrator"
        E2EDB(db_dir / "e2e.db")

        response = flaky_client.get(
            "/control/e2e/flaky-tests",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"}
        )
        assert response.status_code == 200
        data = response.json()

        assert data["flaky_tests"] == []
        assert data["threshold"] == 20
        assert data["window"] == 10

    def test_flaky_returns_tests_above_threshold(self, flaky_client, tmp_path):
        """Should return tests that exceed flip-rate threshold."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        self._write_config(tmp_path)
        db_dir = tmp_path / ".issue-orchestrator"
        db = E2EDB(db_dir / "e2e.db")

        # Create alternating pass/fail runs for flaky_one (100% flip rate)
        # and stable runs for stable_test (0% flip rate)
        for i in range(6):
            run_id = db.start_run(f"{tmp_path}/repo{i}", "test-orch", ["tests/e2e"])
            # Alternating: flaky
            db.upsert_test_result(run_id, "test::flaky_one", "passed" if i % 2 == 0 else "failed")
            # Always passing: stable
            db.upsert_test_result(run_id, "test::stable_test", "passed")
            db.finish_run(run_id, "passed" if i % 2 == 0 else "failed")

        response = flaky_client.get(
            "/control/e2e/flaky-tests",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml", "threshold": 20}
        )
        assert response.status_code == 200
        data = response.json()

        # test::flaky_one has 100% flip rate, exceeds threshold
        # test::stable_test has 0% flip rate, below threshold
        nodeids = [t["nodeid"] for t in data["flaky_tests"]]
        assert "test::flaky_one" in nodeids
        assert "test::stable_test" not in nodeids

        # Check new response fields
        flaky_entry = next(t for t in data["flaky_tests"] if t["nodeid"] == "test::flaky_one")
        assert "flip_rate" in flaky_entry
        assert "flip_rate_percent" in flaky_entry
        assert "category" in flaky_entry
        assert flaky_entry["category"] == "flaky"
        assert flaky_entry["flip_rate_percent"] == 100.0
        # Backward compat alias
        assert "flake_count" in flaky_entry


# --- Test: E2E Test Detail Endpoint ---


class TestE2ETestDetailEndpoint:
    """Test the GET /control/e2e/test/{run_id} endpoint."""

    @pytest.fixture
    def e2e_client(self):
        """Create a test client for E2E endpoints (no orchestrator needed)."""
        return TestClient(control_app)

    def test_returns_400_for_invalid_repo_root(self, e2e_client):
        """Invalid repo_root should return 400."""
        response = e2e_client.get(
            "/control/e2e/test/1",
            params={"repo_root": "../invalid/path", "nodeid": "test::foo", "config_name": "default.yaml"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_returns_404_when_db_not_found(self, e2e_client, tmp_path):
        """Missing E2E database should return 404."""
        response = e2e_client.get(
            "/control/e2e/test/1",
            params={"repo_root": str(tmp_path), "nodeid": "test::foo", "config_name": "default.yaml"}
        )
        assert response.status_code == 404

    def test_returns_404_when_test_not_found(self, e2e_client, tmp_path):
        """Test not found should return 404."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        config_dir = (
            tmp_path / ".issue-orchestrator" / "config" / "modes" / "default"
        )
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            "repo:\n  name: test/repo\ne2e:\n  enabled: true\n"
            "  pytest_paths: ['tests/e2e']\n"
        )
        db_dir = tmp_path / ".issue-orchestrator"
        db_path = db_dir / "e2e.db"
        db = E2EDB(db_path)

        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.finish_run(run_id, status="passed")

        response = e2e_client.get(
            f"/control/e2e/test/{run_id}",
            params={"repo_root": str(tmp_path), "nodeid": "test::nonexistent", "config_name": "default.yaml"}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_returns_test_detail_with_history(self, e2e_client, tmp_path):
        """Should return test details including history."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        # Create config file for _load_config_by_name
        config_dir = (
            tmp_path / ".issue-orchestrator" / "config" / "modes" / "default"
        )
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "default.yaml").write_text("repo:\n  name: test/repo\ne2e:\n  enabled: true\n  pytest_paths: ['tests/e2e']\n")

        db_dir = tmp_path / ".issue-orchestrator"
        db_path = db_dir / "e2e.db"
        db = E2EDB(db_path)

        # Create first run with a failure
        run1_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.upsert_test_result(
            run_id=run1_id,
            nodeid="test_foo.py::test_bar",
            outcome="failed",
            longrepr="AssertionError: expected 1, got 2",
            duration_seconds=1.5,
        )
        db.finish_run(run1_id, status="failed")

        # Create second run with same test passing
        run2_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.upsert_test_result(
            run_id=run2_id,
            nodeid="test_foo.py::test_bar",
            outcome="passed",
            duration_seconds=1.2,
        )
        db.finish_run(run2_id, status="passed")

        # Query the first run's failure
        response = e2e_client.get(
            f"/control/e2e/test/{run1_id}",
            params={"repo_root": str(tmp_path), "nodeid": "test_foo.py::test_bar", "config_name": "default.yaml"}
        )
        assert response.status_code == 200
        data = response.json()

        # Check test details
        assert data["test"]["nodeid"] == "test_foo.py::test_bar"
        assert data["test"]["outcome"] == "failed"
        assert "AssertionError" in data["test"]["longrepr"]
        assert data["test"]["duration_seconds"] == 1.5

        # Check history includes both runs
        assert len(data["history"]) == 2
        assert data["history_summary"]["total"] == 2
        assert data["history_summary"]["passed"] == 1
        assert data["history_summary"]["failed"] == 1


# --- Test: E2E Status Attention Fields ---


class TestE2EStatusAttentionFields:
    """Test needs_attention and untriaged_count in /control/e2e/status."""

    @pytest.fixture
    def e2e_client(self):
        """Create a test client for E2E endpoints."""
        return TestClient(control_app)

    def test_needs_attention_true_when_failed_run_with_no_issues(self, e2e_client, tmp_path):
        """Failed run with untriaged failures should set needs_attention=True."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        # Create config with correct name (default.yaml)
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text("""
repo:
  name: test/repo
e2e:
  enabled: true
  pytest_paths: ["tests/e2e"]
""")

        db_dir = tmp_path / ".issue-orchestrator"
        db_path = db_dir / "e2e.db"
        db = E2EDB(db_path)

        # Use directory name as orchestrator_id (matches config.orchestrator_id)
        orchestrator_id = tmp_path.name

        # Create a failed run with a failing test
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id=orchestrator_id,
            pytest_args=["tests/e2e"],
        )
        db.upsert_test_result(
            run_id=run_id,
            nodeid="test_foo.py::test_bar",
            outcome="failed",
            longrepr="AssertionError",
        )
        db.finish_run(run_id, status="failed")

        response = e2e_client.get(
            "/control/e2e/status",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"}
        )
        assert response.status_code == 200
        data = response.json()

        # Failed run with no issues created should need attention
        assert data["needs_attention"] is True
        assert data["untriaged_count"] == 1

    def test_needs_attention_false_when_issues_created(self, e2e_client, tmp_path):
        """Failed run with existing issues should set needs_attention=False."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        # Create config
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text("""
repo:
  name: test/repo
e2e:
  enabled: true
  pytest_paths: ["tests/e2e"]
""")

        db_dir = tmp_path / ".issue-orchestrator"
        db_path = db_dir / "e2e.db"
        db = E2EDB(db_path)

        # Use directory name as orchestrator_id (matches config.orchestrator_id)
        orchestrator_id = tmp_path.name

        # Create a failed run
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id=orchestrator_id,
            pytest_args=["tests/e2e"],
        )
        db.upsert_test_result(
            run_id=run_id,
            nodeid="test_foo.py::test_bar",
            outcome="failed",
            longrepr="AssertionError",
        )
        db.finish_run(run_id, status="failed")

        # Record that an issue exists for this failure
        db.record_failure_issue(
            nodeid="test_foo.py::test_bar",
            github_issue_number=123,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="abc123",
        )

        response = e2e_client.get(
            "/control/e2e/status",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"}
        )
        assert response.status_code == 200
        data = response.json()

        # All failures have issues, so no attention needed
        assert data["needs_attention"] is False
        assert data["untriaged_count"] == 0

    def test_needs_attention_false_for_passing_run(self, e2e_client, tmp_path):
        """Passing run should not need attention."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        # Create config
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text("""
repo:
  name: test/repo
e2e:
  enabled: true
  pytest_paths: ["tests/e2e"]
""")

        db_dir = tmp_path / ".issue-orchestrator"
        db_path = db_dir / "e2e.db"
        db = E2EDB(db_path)

        # Use directory name as orchestrator_id (matches config.orchestrator_id)
        orchestrator_id = tmp_path.name

        # Create a passing run
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id=orchestrator_id,
            pytest_args=["tests/e2e"],
        )
        db.upsert_test_result(
            run_id=run_id,
            nodeid="test_foo.py::test_bar",
            outcome="passed",
        )
        db.finish_run(run_id, status="passed")

        response = e2e_client.get(
            "/control/e2e/status",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"}
        )
        assert response.status_code == 200
        data = response.json()

        # Passing run doesn't need attention
        assert data["needs_attention"] is False
        assert data["untriaged_count"] == 0


# --- Test: Retry Issue Endpoint ---
