"""E2E run control route tests split from test_control_api."""

# ruff: noqa: F403,F405

from tests.unit import test_control_api as _support
from tests.unit.test_control_api import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)


def test_e2e_dependency_forwards_typed_mode_selection(tmp_path: Path) -> None:
    from issue_orchestrator.entrypoints.control_api_e2e_support import (
        ControlApiE2EDependencies,
    )

    captured: list[object] = []
    expected = MagicMock(spec=Config)
    deps = ControlApiE2EDependencies(
        get_orchestrator=lambda: None,
        load_config_selection=lambda _repo, selection: (
            captured.append(selection) or expected
        ),
        validate_repo_root=lambda value: Path(value) if value else None,
    )

    loaded = deps.load_config(tmp_path, "main.yaml", "codex")

    assert loaded is expected
    assert captured[0].to_dict() == {
        "mode": "codex",
        "config_name": "main.yaml",
    }


def test_e2e_routes_reject_invalid_mode_as_bad_request(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from issue_orchestrator.entrypoints.control_api_e2e_runs import (
        control_e2e_runs_router,
    )
    from issue_orchestrator.entrypoints.control_api_e2e_triage import (
        control_e2e_triage_router,
    )
    from issue_orchestrator.entrypoints.control_api_e2e_support import (
        ControlApiE2EDependencies,
        install_control_api_e2e_dependencies,
    )

    app = FastAPI()
    install_control_api_e2e_dependencies(
        app,
        ControlApiE2EDependencies(
            get_orchestrator=lambda: None,
            load_config_selection=lambda _repo, _selection: MagicMock(spec=Config),
            validate_repo_root=lambda _value: tmp_path,
        ),
    )
    app.include_router(control_e2e_runs_router)
    app.include_router(control_e2e_triage_router)
    (tmp_path / ".issue-orchestrator").mkdir()
    (tmp_path / ".issue-orchestrator/e2e.db").touch()

    client = TestClient(app)
    requests = [
        ("/control/e2e/status", {}),
        ("/control/e2e/triage/1", {}),
        ("/control/e2e/test/1", {"nodeid": "test_example"}),
        ("/control/e2e/run/1", {"enhanced": "true"}),
    ]
    for endpoint, extras in requests:
        response = client.get(
            endpoint,
            params={
            "repo_root": str(tmp_path),
            "config_name": "main.yaml",
            "mode": "../codex",
            **extras,
            },
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_configuration_selection"

class TestE2ELogsEndpoint:
    """Test the /control/e2e/logs/{run_id} endpoint."""

    @pytest.fixture
    def e2e_client(self):
        """Create a test client for E2E endpoints (no orchestrator needed)."""
        return TestClient(control_app)

    def test_logs_returns_400_for_invalid_repo_root(self, e2e_client):
        """Invalid repo_root should return 400."""
        response = e2e_client.get(
            "/control/e2e/logs/1",
            params={"repo_root": "../invalid/path"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_logs_returns_404_when_db_not_found(self, e2e_client, tmp_path):
        """Missing E2E database should return 404."""
        response = e2e_client.get(
            "/control/e2e/logs/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
        assert "E2E database not found" in response.json()["detail"]

    def test_logs_returns_404_when_run_not_found(self, e2e_client, tmp_path):
        """Non-existent run_id should return 404."""
        # Create the .issue-orchestrator directory and an empty DB
        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"

        # Create a minimal valid database
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_runs (
                id INTEGER PRIMARY KEY,
                repo_root TEXT NOT NULL,
                orchestrator_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                exit_code INTEGER,
                pytest_args TEXT NOT NULL DEFAULT '[]',
                commit_sha TEXT,
                branch TEXT,
                retry_of INTEGER,
                is_retry_run INTEGER DEFAULT 0,
                duration_seconds REAL,
                note TEXT,
                log_path TEXT,
                artifacts_dir TEXT,
                worker_pid INTEGER,
                total_tests INTEGER,
                current_test TEXT
            )
        """)
        conn.close()

        response = e2e_client.get(
            "/control/e2e/logs/999",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
        assert "Run 999 not found" in response.json()["detail"]

    def test_logs_returns_404_when_no_log_path(self, e2e_client, tmp_path):
        """Run without log_path should return 404."""
        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_runs (
                id INTEGER PRIMARY KEY,
                repo_root TEXT NOT NULL,
                orchestrator_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                exit_code INTEGER,
                pytest_args TEXT NOT NULL DEFAULT '[]',
                commit_sha TEXT,
                branch TEXT,
                retry_of INTEGER,
                is_retry_run INTEGER DEFAULT 0,
                duration_seconds REAL,
                note TEXT,
                log_path TEXT,
                artifacts_dir TEXT,
                worker_pid INTEGER,
                total_tests INTEGER,
                current_test TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_test_results (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                nodeid TEXT NOT NULL,
                outcome TEXT NOT NULL,
                duration_seconds REAL,
                longrepr TEXT,
                retry_outcome TEXT,
                is_quarantined INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        # Insert a run without log_path
        conn.execute("""
            INSERT INTO e2e_runs (repo_root, orchestrator_id, started_at, status, pytest_args, log_path)
            VALUES (?, 'test-orch', '2024-01-01T00:00:00', 'completed', '[]', NULL)
        """, (str(tmp_path),))
        conn.commit()
        conn.close()

        response = e2e_client.get(
            "/control/e2e/logs/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "no_logs"

    def test_logs_returns_content_successfully(self, e2e_client, tmp_path):
        """Valid run with log file should return content."""
        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"

        # Create a log file
        log_file = tmp_path / "test.log"
        log_file.write_text("Line 1\nLine 2\nLine 3\n")

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_runs (
                id INTEGER PRIMARY KEY,
                repo_root TEXT NOT NULL,
                orchestrator_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                exit_code INTEGER,
                pytest_args TEXT NOT NULL DEFAULT '[]',
                commit_sha TEXT,
                branch TEXT,
                retry_of INTEGER,
                is_retry_run INTEGER DEFAULT 0,
                duration_seconds REAL,
                note TEXT,
                log_path TEXT,
                artifacts_dir TEXT,
                worker_pid INTEGER,
                total_tests INTEGER,
                current_test TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_test_results (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                nodeid TEXT NOT NULL,
                outcome TEXT NOT NULL,
                duration_seconds REAL,
                longrepr TEXT,
                retry_outcome TEXT,
                is_quarantined INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO e2e_runs (repo_root, orchestrator_id, started_at, status, pytest_args, log_path)
            VALUES (?, 'test-orch', '2024-01-01T00:00:00', 'completed', '[]', ?)
        """, (str(tmp_path), str(log_file)))
        conn.commit()
        conn.close()

        response = e2e_client.get(
            "/control/e2e/logs/1",
            params={"repo_root": str(tmp_path), "tail": 10}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total_lines"] == 3
        assert data["returned_lines"] == 3
        assert "Line 1" in data["content"]
        assert "Line 3" in data["content"]


# --- Test: E2E Summary Endpoint ---


class TestE2ESummaryEndpoint:
    """Test the /control/e2e/summary/{run_id} endpoint."""

    @pytest.fixture
    def e2e_client(self):
        """Create a test client for E2E endpoints (no orchestrator needed)."""
        return TestClient(control_app)

    def test_summary_returns_400_for_invalid_repo_root(self, e2e_client):
        """Invalid repo_root should return 400."""
        response = e2e_client.get(
            "/control/e2e/summary/1",
            params={"repo_root": "../invalid/path"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_summary_returns_404_when_db_not_found(self, e2e_client, tmp_path):
        """Missing E2E database should return 404."""
        response = e2e_client.get(
            "/control/e2e/summary/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_summary_returns_test_counts(self, e2e_client, tmp_path):
        """Valid run should return test summary with counts."""
        db_dir = tmp_path / ".issue-orchestrator"
        db_dir.mkdir()
        db_path = db_dir / "e2e.db"

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_runs (
                id INTEGER PRIMARY KEY,
                repo_root TEXT NOT NULL,
                orchestrator_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                exit_code INTEGER,
                pytest_args TEXT NOT NULL DEFAULT '[]',
                commit_sha TEXT,
                branch TEXT,
                retry_of INTEGER,
                is_retry_run INTEGER DEFAULT 0,
                duration_seconds REAL,
                note TEXT,
                log_path TEXT,
                artifacts_dir TEXT,
                worker_pid INTEGER,
                total_tests INTEGER,
                current_test TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS e2e_test_results (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                nodeid TEXT NOT NULL,
                outcome TEXT NOT NULL,
                duration_seconds REAL,
                longrepr TEXT,
                retry_outcome TEXT,
                is_quarantined INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        # Insert a run
        conn.execute("""
            INSERT INTO e2e_runs (repo_root, orchestrator_id, started_at, status, pytest_args)
            VALUES (?, 'test-orch', '2024-01-01T00:00:00', 'completed', '[]')
        """, (str(tmp_path),))
        # Insert test results
        conn.execute("""
            INSERT INTO e2e_test_results (run_id, nodeid, outcome, updated_at)
            VALUES (1, 'test_a.py::test_pass', 'passed', '2024-01-01T00:00:00')
        """)
        conn.execute("""
            INSERT INTO e2e_test_results (run_id, nodeid, outcome, longrepr, updated_at)
            VALUES (1, 'test_b.py::test_fail', 'failed', 'AssertionError', '2024-01-01T00:00:00')
        """)
        conn.execute("""
            INSERT INTO e2e_test_results (run_id, nodeid, outcome, updated_at)
            VALUES (1, 'test_c.py::test_skip', 'skipped', '2024-01-01T00:00:00')
        """)
        conn.commit()
        conn.close()

        response = e2e_client.get(
            "/control/e2e/summary/1",
            params={"repo_root": str(tmp_path)}
        )
        assert response.status_code == 200
        data = response.json()
        assert "counts" in data
        counts = data["counts"]
        assert counts["passed"] == 1
        assert counts["failed"] == 1
        assert counts["skipped"] == 1
        assert counts["total"] == 3
        # Check failed tests list
        assert len(data["failed"]) == 1
        assert data["failed"][0]["nodeid"] == "test_b.py::test_fail"


# --- Test: Triage Endpoint ---


class TestE2ETriageEndpoint:
    """Test the /control/e2e/triage/{run_id} endpoint."""

    @pytest.fixture
    def e2e_client(self):
        """Create a test client for E2E endpoints (no orchestrator needed)."""
        return TestClient(control_app)

    def test_triage_returns_400_for_invalid_repo_root(self, e2e_client):
        """Invalid repo_root should return 400."""
        response = e2e_client.get(
            "/control/e2e/triage/1",
            params={"repo_root": "../invalid/path", "config_name": "default.yaml"}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "Invalid repo_root"

    def test_triage_returns_404_when_db_not_found(self, e2e_client, tmp_path):
        """Missing E2E database should return 404."""
        response = e2e_client.get(
            "/control/e2e/triage/1",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_triage_returns_failures_with_metadata(self, e2e_client, tmp_path):
        """Triage should return failures with flake counts and existing issue info."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        # Create config file for _load_config_by_name
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("repo:\n  name: test/repo\ne2e:\n  enabled: true\n  pytest_paths: ['tests/e2e']\n")

        db_dir = tmp_path / ".issue-orchestrator"
        db_path = db_dir / "e2e.db"

        # Create DB using E2EDB to get proper schema
        db = E2EDB(db_path)

        # Start a run
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
            commit_sha="abc123",
        )

        # Add test results
        db.upsert_test_result(
            run_id=run_id,
            nodeid="test_a.py::test_pass",
            outcome="passed",
        )
        db.upsert_test_result(
            run_id=run_id,
            nodeid="test_b.py::test_fail",
            outcome="failed",
            longrepr="AssertionError: expected True",
        )

        # Finish run
        db.finish_run(run_id, status="failed", exit_code=1)

        response = e2e_client.get(
            f"/control/e2e/triage/{run_id}",
            params={"repo_root": str(tmp_path), "config_name": "default.yaml"}
        )
        assert response.status_code == 200
        data = response.json()

        # Check structure
        assert "run" in data
        assert "failures" in data
        assert "has_parent_issue" in data
        assert "flake_threshold" in data

        # Check run info
        assert data["run"]["id"] == run_id
        assert data["run"]["commit_sha"] == "abc123"

        # Check failures
        failures = data["failures"]
        assert len(failures) == 1
        assert failures[0]["nodeid"] == "test_b.py::test_fail"
        assert failures[0]["longrepr"] == "AssertionError: expected True"
        assert failures[0]["existing_issue"] is None
        assert failures[0]["flake_count"] == 0
        assert failures[0]["is_likely_flaky"] is False

        # No parent issue yet
        assert data["has_parent_issue"] is False
        assert data["parent_issue_number"] is None

        # Issue status fields should have defaults
        assert data["parent_issue_url"] is None
        assert data["parent_issue_closed"] is False
        assert data["sub_issues"] == []
        assert data["sub_issues_summary"] == {"total": 0, "resolved": 0}

    def test_triage_returns_issue_status_when_parent_exists(self, tmp_path):
        """Triage should return issue URLs and sub-issue details when issues exist."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        # Create config file for _load_config_by_name
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("repo:\n  name: test/repo\ne2e:\n  enabled: true\n  pytest_paths: ['tests/e2e']\n")

        db_dir = tmp_path / ".issue-orchestrator"
        db_path = db_dir / "e2e.db"

        # Create DB and add test data
        db = E2EDB(db_path)
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
            commit_sha="def456",
        )

        # Add test results
        db.upsert_test_result(run_id=run_id, nodeid="test_a.py::test_one", outcome="failed")
        db.upsert_test_result(run_id=run_id, nodeid="test_b.py::test_two", outcome="failed")
        db.finish_run(run_id, status="failed", exit_code=1)

        # Create parent issue for the run
        db.record_run_issue(run_id=run_id, github_issue_number=100)

        # Create sub-issues for failures
        db.record_failure_issue(
            nodeid="test_a.py::test_one",
            github_issue_number=101,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="def456",
        )
        db.record_failure_issue(
            nodeid="test_b.py::test_two",
            github_issue_number=102,
            parent_issue_number=100,
            first_failing_run_id=run_id,
            first_failing_sha="def456",
        )

        # Resolve one sub-issue
        db.resolve_failure_issue(nodeid="test_b.py::test_two", resolution="passed")

        # Create mock orchestrator with config.repo for URL generation
        mock_orch = create_mock_orchestrator()
        mock_orch.config.repo = "owner/repo"
        set_orchestrator(mock_orch)

        try:
            client = TestClient(control_app)
            response = client.get(
                f"/control/e2e/triage/{run_id}",
                params={"repo_root": str(tmp_path), "config_name": "default.yaml"}
            )
            assert response.status_code == 200
            data = response.json()

            # Verify parent issue info
            assert data["has_parent_issue"] is True
            assert data["parent_issue_number"] == 100
            assert data["parent_issue_url"] == "https://github.com/owner/repo/issues/100"
            assert data["parent_issue_closed"] is False

            # Verify sub-issues
            assert data["sub_issues_summary"] == {"total": 2, "resolved": 1}
            sub_issues = data["sub_issues"]
            assert len(sub_issues) == 2

            # Find sub-issues by nodeid
            sub_by_nodeid = {s["nodeid"]: s for s in sub_issues}

            # Check unresolved sub-issue
            sub1 = sub_by_nodeid["test_a.py::test_one"]
            assert sub1["issue_number"] == 101
            assert sub1["resolved"] is False
            assert sub1["resolution"] is None
            assert sub1["url"] == "https://github.com/owner/repo/issues/101"

            # Check resolved sub-issue
            sub2 = sub_by_nodeid["test_b.py::test_two"]
            assert sub2["issue_number"] == 102
            assert sub2["resolved"] is True
            assert sub2["resolution"] == "passed"
            assert sub2["url"] == "https://github.com/owner/repo/issues/102"
        finally:
            set_orchestrator(None)
