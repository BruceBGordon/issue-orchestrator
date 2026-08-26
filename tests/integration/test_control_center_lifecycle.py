"""Integration tests for Control Center orchestrator lifecycle management.

These tests verify the full start/stop/restart cycle works correctly:
1. Start orchestrator via Control Center API
2. Verify it's actually running (process exists, port responds)
3. Stop via Control Center API
4. Verify it's actually stopped (process gone, port free)
5. Restart cycle works
6. Shutdown via orchestrator's own API triggers exit

This catches the class of bugs where:
- API returns success but process didn't start/stop
- Status is stale or inconsistent
- Process is zombie/orphaned
- Port is still in use after "stop"

Note: These tests require a valid GitHub token because the orchestrator
subprocess needs to create a GitHubAdapter during startup.
"""

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator

import httpx
import pytest
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG

# Fixed admin bearer token used by the integration CC subprocess + its
# HTTP client. Any deterministic non-empty value works; real tokens
# live in ``~/.issue-orchestrator/api-token`` which we override via
# env var on the subprocess.
_INTEGRATION_ADMIN_TOKEN = "integration-test-admin-token"

from .conftest import xdist_timeout

logger = logging.getLogger(__name__)


# Check if GitHub token is available - required for orchestrator startup
def _has_github_token() -> bool:
    """Check if a GitHub token is available for orchestrator startup.

    Checks environment variables and keyring (same sources as resolve_github_token).
    """
    # Check environment variables
    for env_name in ("ISSUE_ORCH_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(env_name):
            return True
    # Check keyring
    try:
        import keyring
        token = keyring.get_password("issue-orchestrator", "github-token")
        if token:
            return True
    except Exception:
        pass
    return False


# Mark entire module as requiring infrastructure (excluded from pre-push, run in CI).
# These tests spawn/stop real control center processes, so they must run in one xdist group.
pytestmark = [
    pytest.mark.requires_infra,
    pytest.mark.xdist_group("control-center-lifecycle"),
]


@pytest.fixture(autouse=True, scope="module")
def require_github_token():
    """Fail fast if no GitHub token is available.

    These integration tests require a real GitHub token because the orchestrator
    subprocess creates a GitHubAdapter during startup. If no token is available,
    the tests should FAIL (not skip) so the issue gets fixed.
    """
    if not _has_github_token():
        pytest.fail(
            "GitHub token required for integration tests!\n"
            "Set GITHUB_TOKEN, GH_TOKEN, or ISSUE_ORCH_GITHUB_TOKEN, "
            "or store in keyring via: issue-orchestrator keys set github"
        )


# --- Fixtures ---


# NOTE: isolated_registry fixture is now in conftest.py and applies to all integration tests


@pytest.fixture
def orchestrator_port() -> int:
    """Get a unique port for the orchestrator."""
    return _find_free_port()


@pytest.fixture
def test_repo(tmp_path: Path, orchestrator_port: int) -> Path:
    """Create a minimal test repository with config."""
    # Create config directory
    config_dir = tmp_path / ".issue-orchestrator" / "config"
    config_dir.mkdir(parents=True)

    # Create prompts directory with minimal prompt
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "developer.md").write_text("Test prompt - complete the task")

    # Create minimal config (use relative path from repo root)
    # Use dynamic port to avoid collisions between tests
    config = {
        "repo": {"name": "test/repo"},
        "ui": {"web_port": orchestrator_port},
        "filtering": {"label": "test-only"},
        "worktrees": {"base": str(tmp_path / "worktrees")},
        "agents": {
            "agent:developer": {
                "prompt": "prompts/developer.md",
                "model": "haiku",
            }
        },
        "execution": {
            "concurrency": {
                "max_concurrent_sessions": 1,
                "session_timeout_minutes": 5,
            }
        },
    }
    config_path = config_dir / "test.yaml"

    import yaml
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    # Initialize as git repo (required for orchestrator)
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        capture_output=True,
    )

    return tmp_path


@pytest.fixture
def control_center_port() -> int:
    """Get a free port for the control center."""
    return _find_free_port()


@pytest.fixture
def control_center_process(
    control_center_port: int,
) -> Generator[subprocess.Popen, None, None]:
    """Start a control center process for testing."""
    cmd = [
        sys.executable,
        "-m",
        "issue_orchestrator.entrypoints.control_center",
        "--port",
        str(control_center_port),
        "--no-browser",
        "--no-tray",
    ]

    logger.info("Starting control center on port %d", control_center_port)
    # Seed a known admin bearer token into the subprocess env so the
    # plain ``httpx.Client`` in tests can authenticate after
    # ``control_api`` enforces auth (#6011). The env var wins over
    # the on-disk token file in ``resolve_api_token``.
    subprocess_env = {**os.environ, "ISSUE_ORCHESTRATOR_API_TOKEN": _INTEGRATION_ADMIN_TOKEN}
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=subprocess_env,
    )

    # Wait for control center to be ready
    if not _wait_for_port(control_center_port, timeout=30):
        process.kill()
        stdout, _ = PROCESS_COMPLETION_WATCHDOG.communicate(
            process,
            operation="failed control center startup cleanup",
        )
        pytest.fail(f"Control center failed to start. Output:\n{stdout}")

    logger.info("Control center ready on port %d", control_center_port)

    yield process

    # Cleanup
    logger.info("Stopping control center")
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        PROCESS_COMPLETION_WATCHDOG.wait(
            process,
            operation="forced control center shutdown",
        )


@pytest.fixture
def cc_client(control_center_port: int) -> Generator[httpx.Client, None, None]:
    """HTTP client for Control Center API — pre-authenticated.

    Carries ``Authorization: Bearer <token>`` using the fixed
    ``_INTEGRATION_ADMIN_TOKEN`` that ``control_center_process``
    injects into the subprocess env.
    """
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{control_center_port}",
        timeout=xdist_timeout(30.0),
        headers={"Authorization": f"Bearer {_INTEGRATION_ADMIN_TOKEN}"},
    )
    yield client
    client.close()


# --- Helper Functions ---


def _find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 10) -> bool:
    """Wait for a port to become available."""
    timeout = xdist_timeout(timeout)
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", port))
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(0.1)
    return False


def _wait_for_port_free(port: int, timeout: float = 10) -> bool:
    """Wait for a port to become free."""
    timeout = xdist_timeout(timeout)
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", port))
                # Port is still in use
                time.sleep(0.1)
        except (ConnectionRefusedError, socket.timeout, OSError):
            # Port is free
            return True
    return False


def _is_process_running(pid: int) -> bool:
    """Check if a process is running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_for_process_dead(pid: int, timeout: float = 10) -> bool:
    """Wait for a process to exit."""
    timeout = xdist_timeout(timeout)
    start = time.time()
    while time.time() - start < timeout:
        if not _is_process_running(pid):
            return True
        time.sleep(0.1)
    return False


def _wait_for_status(
    cc_client: "httpx.Client",
    repo_root: Path,
    expected_states: list[str],
    timeout: float = 10,
) -> dict | None:
    """Wait for Control Center to report one of the expected states."""
    timeout = xdist_timeout(timeout)
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = cc_client.get(
                "/control/orchestrator/status",
                params={"repo_root": str(repo_root)},
            )
            if resp.status_code == 200:
                status = resp.json()
                if status.get("state") in expected_states:
                    return status
        except Exception:
            pass
        time.sleep(0.2)
    return None


# --- Tests ---


@pytest.mark.integration
@pytest.mark.timeout(xdist_timeout(120))
class TestControlCenterLifecycle:
    """Test the full Control Center → Orchestrator lifecycle."""

    def test_start_stop_cycle(
        self,
        test_repo: Path,
        orchestrator_port: int,
        control_center_process: subprocess.Popen,
        cc_client: httpx.Client,
    ) -> None:
        """Test basic start → verify → stop → verify cycle."""

        # 1. Register the test repo
        logger.info("Registering test repo: %s", test_repo)
        resp = cc_client.post(
            "/control/repos",
            json={"repo_root": str(test_repo)},
        )
        assert resp.status_code == 200, f"Failed to register repo: {resp.text}"

        # 2. Start orchestrator
        logger.info("Starting orchestrator")
        resp = cc_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(test_repo), "config_name": "test.yaml"},
        )
        assert resp.status_code == 200, f"Start failed: {resp.text}"
        data = resp.json()
        assert data["status"] == "started", f"Unexpected status: {data}"
        orchestrator_pid = data["pid"]
        logger.info("Orchestrator started with PID %d", orchestrator_pid)

        # 3. Verify process is actually running
        assert _is_process_running(orchestrator_pid), "Process should be running"

        # 4. Verify port is listening
        assert _wait_for_port(orchestrator_port, timeout=30), (
            f"Orchestrator port {orchestrator_port} should be listening"
        )

        # 5. Verify orchestrator API responds
        orch_resp = httpx.get(
            f"http://127.0.0.1:{orchestrator_port}/api/status",
            headers={"Authorization": f"Bearer {_INTEGRATION_ADMIN_TOKEN}"},
            timeout=xdist_timeout(5.0),
        )
        assert orch_resp.status_code == 200, "Orchestrator API should respond"
        orch_status = orch_resp.json()
        assert orch_status["shutdown_requested"] is False

        # 6. Verify Control Center status endpoint
        resp = cc_client.get(
            "/control/orchestrator/status",
            params={"repo_root": str(test_repo)},
        )
        assert resp.status_code == 200
        status = resp.json()
        assert status["state"] == "running", f"Expected running, got: {status}"
        assert status["pid"] == orchestrator_pid

        # 7. Stop orchestrator via Control Center
        logger.info("Stopping orchestrator via Control Center")
        resp = cc_client.post(
            "/control/orchestrator/stop",
            json={
                "repo_root": str(test_repo),
                "reason": "integration test cycle: stop step",
                "actor": "test-control-center-lifecycle",
            },
        )
        assert resp.status_code == 200, f"Stop failed: {resp.text}"
        data = resp.json()
        assert data["status"] == "stopped", f"Expected stopped, got: {data}"

        # 8. Verify process is actually dead (poll, don't sleep)
        assert _wait_for_process_dead(orchestrator_pid, timeout=30), (
            f"Process {orchestrator_pid} should be dead"
        )

        # 9. Verify port is free
        assert _wait_for_port_free(orchestrator_port, timeout=5), (
            f"Port {orchestrator_port} should be free"
        )

        # 10. Verify Control Center status shows stopped
        resp = cc_client.get(
            "/control/orchestrator/status",
            params={"repo_root": str(test_repo)},
        )
        assert resp.status_code == 200
        status = resp.json()
        assert status["state"] == "stopped", f"Expected stopped, got: {status}"

        logger.info("✓ Start/stop cycle completed successfully")

    def test_restart_after_stop(
        self,
        test_repo: Path,
        orchestrator_port: int,
        control_center_process: subprocess.Popen,
        cc_client: httpx.Client,
    ) -> None:
        """Test that orchestrator can be restarted after being stopped."""

        # Register repo
        cc_client.post("/control/repos", json={"repo_root": str(test_repo)})

        # Start
        resp = cc_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(test_repo), "config_name": "test.yaml"},
        )
        assert resp.status_code == 200
        first_pid = resp.json()["pid"]
        _wait_for_port(orchestrator_port, timeout=30)

        # Stop
        resp = cc_client.post(
            "/control/orchestrator/stop",
            json={
                "repo_root": str(test_repo),
                "reason": "integration test restart: stop step",
                "actor": "test-control-center-lifecycle",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        _wait_for_port_free(orchestrator_port, timeout=30)

        # Restart
        logger.info("Restarting orchestrator")
        resp = cc_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(test_repo), "config_name": "test.yaml"},
        )
        assert resp.status_code == 200, f"Restart failed: {resp.text}"
        data = resp.json()
        assert data["status"] == "started", f"Expected started, got: {data}"
        second_pid = data["pid"]

        # Should be a different PID
        assert second_pid != first_pid, "Should be a new process"

        # Verify it's running
        assert _wait_for_port(orchestrator_port, timeout=30)
        assert _is_process_running(second_pid)

        # Cleanup
        cc_client.post(
            "/control/orchestrator/stop",
            json={
                "repo_root": str(test_repo),
                "reason": "integration test restart: cleanup",
                "actor": "test-control-center-lifecycle",
            },
        )

        logger.info("✓ Restart cycle completed successfully")

    # test_stop_already_stopped moved to TestControlCenterAPIInProcess for reliability

    def test_start_already_running(
        self,
        test_repo: Path,
        orchestrator_port: int,
        control_center_process: subprocess.Popen,
        cc_client: httpx.Client,
    ) -> None:
        """Starting an already-running orchestrator should fail gracefully."""

        # Register and start
        cc_client.post("/control/repos", json={"repo_root": str(test_repo)})
        resp = cc_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(test_repo), "config_name": "test.yaml"},
        )
        assert resp.status_code == 200
        first_pid = resp.json()["pid"]
        _wait_for_port(orchestrator_port, timeout=30)

        # Try to start again
        resp = cc_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(test_repo), "config_name": "test.yaml"},
        )
        # Should return error or existing info
        assert resp.status_code in (200, 409), f"Unexpected: {resp.text}"
        data = resp.json()
        # Either "already_running" error or returns existing PID
        if resp.status_code == 409 or data.get("error") == "already_running":
            logger.info("Correctly rejected duplicate start")
        else:
            # Some implementations return the existing process info
            assert data.get("pid") == first_pid

        # Original should still be running
        assert _is_process_running(first_pid)

        # Cleanup
        cc_client.post(
            "/control/orchestrator/stop",
            json={
                "repo_root": str(test_repo),
                "reason": "integration test start-when-running: cleanup",
                "actor": "test-control-center-lifecycle",
            },
        )

        logger.info("✓ Start-when-running handled correctly")

    def test_shutdown_via_orchestrator_api(
        self,
        test_repo: Path,
        orchestrator_port: int,
        control_center_process: subprocess.Popen,
        cc_client: httpx.Client,
    ) -> None:
        """Shutdown via orchestrator's own /api/shutdown should kill the process."""

        # Register and start
        cc_client.post("/control/repos", json={"repo_root": str(test_repo)})
        resp = cc_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(test_repo), "config_name": "test.yaml"},
        )
        assert resp.status_code == 200
        pid = resp.json()["pid"]
        _wait_for_port(orchestrator_port, timeout=30)

        # Shutdown via orchestrator's own API.  Under load the server may
        # tear down its socket before the 200 response is flushed; either
        # a successful response or a bare RemoteProtocolError counts as the
        # endpoint being reached.
        logger.info("Sending shutdown via orchestrator API")
        try:
            orch_resp = httpx.post(
                f"http://127.0.0.1:{orchestrator_port}/api/shutdown",
                json={
                    "reason": "integration test self-shutdown",
                    "actor": "test-control-center-lifecycle",
                },
                headers={"Authorization": f"Bearer {_INTEGRATION_ADMIN_TOKEN}"},
                timeout=xdist_timeout(5.0),
            )
            assert orch_resp.status_code == 200
        except httpx.RemoteProtocolError:
            pass

        # Wait for port to become free (indicates process exited)
        # Graceful shutdown may take several seconds as it drains connections
        assert _wait_for_port_free(orchestrator_port, timeout=20), (
            "Port should be free after shutdown"
        )

        # Process should be dead (poll with generous timeout for graceful shutdown)
        assert _wait_for_process_dead(pid, timeout=30), "Process should have exited"

        # Control Center should show stopped (poll until status changes)
        status = _wait_for_status(cc_client, test_repo, ["stopped", "failed"], timeout=30)
        assert status is not None, "Status should be stopped or failed"
        assert status["state"] in ("stopped", "failed"), f"Unexpected: {status}"

        logger.info("✓ Orchestrator self-shutdown works correctly")

    def test_force_stop(
        self,
        test_repo: Path,
        orchestrator_port: int,
        control_center_process: subprocess.Popen,
        cc_client: httpx.Client,
    ) -> None:
        """Force stop (SIGKILL) should always work."""

        # Register and start
        cc_client.post("/control/repos", json={"repo_root": str(test_repo)})
        resp = cc_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(test_repo), "config_name": "test.yaml"},
        )
        assert resp.status_code == 200
        pid = resp.json()["pid"]
        _wait_for_port(orchestrator_port, timeout=30)

        # Force stop
        logger.info("Force stopping orchestrator")
        resp = cc_client.post(
            "/control/orchestrator/stop",
            json={
                "repo_root": str(test_repo),
                "force": True,
                "reason": "integration test force stop",
                "actor": "test-control-center-lifecycle",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"

        # Should be dead quickly (poll, don't sleep)
        assert _wait_for_process_dead(pid, timeout=5), "Process should be dead after force stop"

        logger.info("✓ Force stop works correctly")


@pytest.mark.integration
@pytest.mark.timeout(xdist_timeout(60))
class TestControlCenterStatusConsistency:
    """Test that status reporting is consistent across all interfaces."""

    def test_status_consistency(
        self,
        test_repo: Path,
        orchestrator_port: int,
        control_center_process: subprocess.Popen,
        cc_client: httpx.Client,
    ) -> None:
        """Status should be consistent between CC API and orchestrator API."""

        # Register and start
        cc_client.post("/control/repos", json={"repo_root": str(test_repo)})
        cc_client.post(
            "/control/orchestrator/start",
            json={"repo_root": str(test_repo), "config_name": "test.yaml"},
        )
        _wait_for_port(orchestrator_port, timeout=30)

        # Get status from both sources
        cc_status = cc_client.get(
            "/control/orchestrator/status",
            params={"repo_root": str(test_repo)},
        ).json()

        orch_status = httpx.get(
            f"http://127.0.0.1:{orchestrator_port}/api/status",
            headers={"Authorization": f"Bearer {_INTEGRATION_ADMIN_TOKEN}"},
            timeout=xdist_timeout(5.0),
        ).json()

        # Both should agree on running state
        assert cc_status["state"] == "running"
        assert orch_status["shutdown_requested"] is False

        # Request shutdown — the server may close the connection before
        # sending a response once it starts tearing down, so a bare
        # RemoteProtocolError is an expected outcome here.
        try:
            httpx.post(
                f"http://127.0.0.1:{orchestrator_port}/api/shutdown",
                json={
                    "reason": "integration test status consistency check",
                    "actor": "test-control-center-lifecycle",
                },
                headers={"Authorization": f"Bearer {_INTEGRATION_ADMIN_TOKEN}"},
                timeout=xdist_timeout(5.0),
            )
        except httpx.RemoteProtocolError:
            pass

        # Wait for shutdown (poll, don't sleep)
        assert _wait_for_port_free(orchestrator_port, timeout=15), "Port should be free after shutdown"

        # CC should reflect stopped state (poll until status changes)
        cc_status = _wait_for_status(cc_client, test_repo, ["stopped", "failed"], timeout=30)
        assert cc_status is not None, "Status should be stopped or failed"
        assert cc_status["state"] in ("stopped", "failed")

        logger.info("✓ Status consistency verified")


@pytest.mark.integration
class TestControlCenterAPIInProcess:
    """Test Control Center API endpoints using in-process TestClient.

    These tests use Starlette's TestClient for synchronous, deterministic testing
    without subprocess management or polling loops. They mock the supervisor module
    to test API behavior in isolation.

    This is more reliable than subprocess-based tests for API-only behavior.
    """

    def test_stop_already_stopped(self, test_repo: Path) -> None:
        """Stopping an already-stopped orchestrator should succeed.

        Uses in-process TestClient to avoid subprocess race conditions.
        """
        from unittest.mock import patch
        from starlette.testclient import TestClient
        from issue_orchestrator.entrypoints.control_api import control_app
        from issue_orchestrator.infra.supervisor import SupervisorStatus

        # Mock supervisor.stop to return True (already stopped)
        # Mock supervisor.status to return stopped state
        mock_status = SupervisorStatus(state="stopped")

        # Patch at the infra.supervisor module level since it's imported locally
        with patch("issue_orchestrator.infra.supervisor.stop", return_value=True), \
             patch("issue_orchestrator.infra.supervisor.status", return_value=mock_status):

            with TestClient(control_app) as client:
                # Register repo
                resp = client.post("/control/repos", json={"repo_root": str(test_repo)})
                assert resp.status_code == 200, f"Failed to register: {resp.text}"

                # Stop (should succeed - nothing to stop means goal achieved)
                resp = client.post(
                    "/control/orchestrator/stop",
                    json={
                        "repo_root": str(test_repo),
                        "reason": "integration test stop-when-stopped",
                        "actor": "test-control-center-lifecycle",
                    },
                )
                assert resp.status_code == 200, f"Stop failed: {resp.text}"
                data = resp.json()
                assert data["status"] == "stopped"

        logger.info("✓ Stop-when-stopped handled correctly (in-process)")
