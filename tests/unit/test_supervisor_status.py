"""Tests for supervisor module."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_identity import ConfigurationFingerprintMismatch
from issue_orchestrator.infra.supervisor import (
    DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS,
    LockInfo,
    SupervisorStatus,
    MultiInstanceStatus,
    status,
    start,
    start_instances,
    stop_tracked_instance,
    find_free_port,
    status_all_instances,
)


def test_graceful_shutdown_default_allows_agent_runtime_cleanup() -> None:
    assert DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS == 120


def test_stop_rejects_a_replacement_process_for_exact_tracked_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement_pid = os.getpid()
    lock_dir = tmp_path / ".issue-orchestrator/locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "orchestrator-1.json").write_text(
        json.dumps(
            {
                "repo_root": str(tmp_path),
                "pid": replacement_pid,
                "started_at": "2026-08-12T00:00:00Z",
                "http_port": 19090,
                "state_dir": str(tmp_path / ".issue-orchestrator/state"),
                "instance_id": "orchestrator-1",
            }
        ),
        encoding="utf-8",
    )
    kill = MagicMock()
    monkeypatch.setattr(
        "issue_orchestrator.infra.supervisor._send_kill_signal",
        kill,
    )

    stopped = stop_tracked_instance(
        tmp_path,
        SupervisorStatus(
            state="running",
            pid=replacement_pid + 1,
            instance_id="orchestrator-1",
        ),
        reason="test exact ownership",
        actor="test",
    )

    assert stopped is False
    kill.assert_not_called()


def test_start_rejects_config_changed_after_preflight_before_spawn(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".issue-orchestrator/config/modes/codex/main.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("agents: {}\n", encoding="utf-8")
    preflight_fingerprint = Config.load(config_path).config_fingerprint
    config_path.write_text("agents: {}\nweb_port: 19090\n", encoding="utf-8")
    spawn = MagicMock()

    with pytest.raises(ConfigurationFingerprintMismatch):
        start(
            tmp_path,
            config_name="main.yaml",
            mode="codex",
            expected_config_fingerprint=preflight_fingerprint,
            spawn_process=spawn,
        )

    spawn.assert_not_called()


def test_stop_controller_consumes_one_budget_across_request_and_signal() -> None:
    from issue_orchestrator.infra.shutdown_timing import (
        InterruptibleStopController,
        StaticStopPolicy,
    )

    now = 0.0
    terminated = False
    forced = False

    def monotonic() -> float:
        return now

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    def request_shutdown() -> bool:
        advance(4.0)
        return False

    def terminate() -> None:
        nonlocal terminated
        terminated = True

    def force_stop() -> bool:
        nonlocal forced
        forced = True
        return True

    controller = InterruptibleStopController(
        StaticStopPolicy(graceful_timeout_seconds=5),
        pid=4242,
        force_requested=False,
        force_on_timeout=True,
        request_graceful=request_shutdown,
        terminate=terminate,
        force_stop=force_stop,
        on_stopped=lambda: None,
        clock=monotonic,
        sleeper=advance,
        process_probe=lambda _pid: True,
    )

    assert controller.stop() is True
    assert now == pytest.approx(5.0)
    assert terminated is True
    assert forced is True


def test_stop_controller_observes_abort_during_current_wait() -> None:
    from issue_orchestrator.infra.shutdown_timing import (
        InterruptibleStopController,
        StopAborted,
        StopPolicySnapshot,
    )

    class MutablePolicy:
        current = StopPolicySnapshot(graceful_timeout_seconds=120)

        def snapshot(self) -> StopPolicySnapshot:
            return self.current

    policy = MutablePolicy()

    def process_probe(_pid: int) -> bool:
        policy.current = StopPolicySnapshot(
            graceful_timeout_seconds=120,
            abort=True,
        )
        return True

    controller = InterruptibleStopController(
        policy,
        pid=4242,
        force_requested=False,
        force_on_timeout=True,
        request_graceful=lambda: True,
        terminate=lambda: None,
        force_stop=lambda: True,
        on_stopped=lambda: None,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        process_probe=process_probe,
    )

    with pytest.raises(StopAborted):
        controller.stop()


class TestSupervisorStatus:
    """Tests for supervisor status function."""

    def test_status_stopped_no_lock(self, tmp_path: Path) -> None:
        """Return stopped state when no lock file exists."""
        result = status(tmp_path)

        assert result.state == "stopped"
        assert result.pid is None
        assert result.port is None

    def test_status_running_with_live_process(self, tmp_path: Path) -> None:
        """Return running state when lock exists and process is alive."""
        # Create lock file with current process PID
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        lock_data = {
            "repo_root": str(tmp_path),
            "pid": os.getpid(),  # Current process is alive
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8080,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
            "recovered": False,
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        result = status(tmp_path)

        assert result.state == "running"
        assert result.pid == os.getpid()
        assert result.port == 8080
        assert result.started_at == "2024-01-01T00:00:00Z"
        assert result.recovered is False

    def test_status_failed_with_dead_process(self, tmp_path: Path) -> None:
        """Return failed state when lock exists but process is dead."""
        # Create lock file with non-existent PID
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        lock_data = {
            "repo_root": str(tmp_path),
            "pid": 999999999,  # Very unlikely to be a real process
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8080,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
            "recovered": False,
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        result = status(tmp_path)

        assert result.state == "failed"
        assert result.pid == 999999999
        assert result.port == 8080
        assert "stale lock" in result.error.lower()

    def test_status_recovered_flag_preserved(self, tmp_path: Path) -> None:
        """Preserve recovered flag from lock file."""
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        lock_data = {
            "repo_root": str(tmp_path),
            "pid": os.getpid(),
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8080,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
            "recovered": True,  # Was recovered from stale lock
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        result = status(tmp_path)

        assert result.state == "running"
        assert result.recovered is True


class TestSupervisorStatusToDict:
    """Tests for SupervisorStatus.to_dict method."""

    def test_to_dict_all_fields(self) -> None:
        """Convert status with all fields to dict."""
        status_obj = SupervisorStatus(
            state="running",
            pid=12345,
            port=8080,
            started_at="2024-01-01T00:00:00Z",
            recovered=True,
            error=None,
        )

        data = status_obj.to_dict()

        assert data["state"] == "running"
        assert data["pid"] == 12345
        assert data["port"] == 8080
        assert data["started_at"] == "2024-01-01T00:00:00Z"
        assert data["recovered"] is True
        assert data["error"] is None

    def test_to_dict_stopped(self) -> None:
        """Convert stopped status to dict."""
        status_obj = SupervisorStatus(state="stopped")

        data = status_obj.to_dict()

        assert data["state"] == "stopped"
        assert data["pid"] is None
        assert data["port"] is None

    def test_to_dict_failed_with_error(self) -> None:
        """Convert failed status with error to dict."""
        status_obj = SupervisorStatus(
            state="failed",
            pid=12345,
            error="Process not running (stale lock)",
        )

        data = status_obj.to_dict()

        assert data["state"] == "failed"
        assert data["error"] == "Process not running (stale lock)"


class TestSupervisorStartErrorSurfacing:
    """Tests for error message extraction when orchestrator fails to start."""

    def test_start_failure_extracts_error_from_log(self, tmp_path: Path) -> None:
        """When process exits immediately, extract ERROR line from log."""
        # Setup: create state directory and log file with an error
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        logs_dir = state_dir / "logs"
        logs_dir.mkdir(parents=True)
        log_file = logs_dir / "orchestrator.log"
        log_file.write_text(
            "2024-01-01 [INFO] Starting...\n"
            "2024-01-01 [ERROR] __main__: Could not determine GitHub repository\n"
        )

        # Also need config dir for start() to work
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        # Fake process that exits immediately
        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_process.poll.return_value = 1  # Exited with code 1

        with pytest.raises(RuntimeError) as exc_info:
            start(tmp_path, spawn_process=lambda *a, **kw: mock_process)

        error_msg = str(exc_info.value)
        assert "exited immediately with code 1" in error_msg
        assert "Could not determine GitHub repository" in error_msg
        assert "Full logs at:" in error_msg

    def test_start_failure_shows_last_line_if_no_error(self, tmp_path: Path) -> None:
        """When no ERROR in log, show last non-empty line as hint."""
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        logs_dir = state_dir / "logs"
        logs_dir.mkdir(parents=True)
        log_file = logs_dir / "orchestrator.log"
        log_file.write_text(
            "2024-01-01 [INFO] Starting...\n"
            "2024-01-01 [INFO] Building orchestrator...\n"
            "2024-01-01 [INFO] Some final message\n"
        )

        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_process.poll.return_value = 1

        with pytest.raises(RuntimeError) as exc_info:
            start(tmp_path, spawn_process=lambda *a, **kw: mock_process)

        error_msg = str(exc_info.value)
        assert "Some final message" in error_msg

    def test_start_failure_handles_missing_log(self, tmp_path: Path) -> None:
        """When log file doesn't exist, still show basic error."""
        # Don't create log file, but need config dir
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)

        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_process.poll.return_value = 1

        with pytest.raises(RuntimeError) as exc_info:
            start(tmp_path, spawn_process=lambda *a, **kw: mock_process)

        error_msg = str(exc_info.value)
        assert "exited immediately with code 1" in error_msg
        # Should still mention where logs would be
        assert "logs" in error_msg.lower()

    def test_start_paused_adds_subprocess_flag(self, tmp_path: Path) -> None:
        """Supervisor passes --start-paused to the child process before launch."""
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        captured: dict[str, object] = {}
        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_process.poll.return_value = 1

        def spawn_process(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return mock_process

        with pytest.raises(RuntimeError):
            start(tmp_path, start_paused=True, spawn_process=spawn_process)

        command = captured["args"][0]
        assert "--start-paused" in command

    def test_start_log_level_adds_subprocess_flag(self, tmp_path: Path) -> None:
        """Supervisor passes explicit engine log level to the child process."""
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        captured: dict[str, object] = {}
        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_process.poll.return_value = 1

        def spawn_process(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return mock_process

        with pytest.raises(RuntimeError):
            start(tmp_path, log_level="DEBUG", spawn_process=spawn_process)

        command = captured["args"][0]
        assert command[command.index("--log-level") + 1] == "DEBUG"

    def test_start_log_level_env_adds_subprocess_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Supervisor lets Control Center opt repository engines into DEBUG logs."""
        from issue_orchestrator.infra.supervisor import ENGINE_LOG_LEVEL_ENV

        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")
        monkeypatch.setenv(ENGINE_LOG_LEVEL_ENV, "debug")

        captured: dict[str, object] = {}
        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_process.poll.return_value = 1

        def spawn_process(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return mock_process

        with pytest.raises(RuntimeError):
            start(tmp_path, spawn_process=spawn_process)

        command = captured["args"][0]
        assert command[command.index("--log-level") + 1] == "DEBUG"

    def test_start_invalid_log_level_fails_before_spawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid engine log-level env should fail fast instead of launching."""
        from issue_orchestrator.infra.supervisor import ENGINE_LOG_LEVEL_ENV

        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")
        monkeypatch.setenv(ENGINE_LOG_LEVEL_ENV, "verbose")
        spawn_process = MagicMock()

        with pytest.raises(ValueError, match=ENGINE_LOG_LEVEL_ENV):
            start(tmp_path, spawn_process=spawn_process)

        spawn_process.assert_not_called()


class TestMultiInstanceSupport:
    """Tests for multi-instance supervisor functionality."""

    def test_find_free_port_returns_valid_port(self) -> None:
        """find_free_port returns a valid port number."""
        port = find_free_port()
        assert 1024 <= port <= 65535

    def test_find_free_port_returns_different_ports(self) -> None:
        """find_free_port returns different ports on successive calls."""
        port1 = find_free_port()
        port2 = find_free_port()
        # They should usually be different (not guaranteed but highly likely)
        # Just check both are valid
        assert 1024 <= port1 <= 65535
        assert 1024 <= port2 <= 65535

    def test_status_with_instance_id(self, tmp_path: Path) -> None:
        """status() returns instance_id when provided."""
        result = status(tmp_path, instance_id="orchestrator-1")
        assert result.state == "stopped"
        assert result.instance_id == "orchestrator-1"

    def test_status_running_with_instance_lock(self, tmp_path: Path) -> None:
        """status() reads instance-specific lock file."""
        # Create instance-specific lock file
        locks_dir = tmp_path / ".issue-orchestrator" / "locks"
        locks_dir.mkdir(parents=True)
        lock_path = locks_dir / "orchestrator-1.json"

        lock_data = {
            "repo_root": str(tmp_path),
            "pid": os.getpid(),
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8081,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
            "recovered": False,
            "instance_id": "orchestrator-1",
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        result = status(tmp_path, instance_id="orchestrator-1")

        assert result.state == "running"
        assert result.pid == os.getpid()
        assert result.port == 8081
        assert result.instance_id == "orchestrator-1"

    def test_start_instances_forwards_start_paused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multi-instance startup passes --start-paused intent to each child."""
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("instances: 2\nagents: {}\n")

        calls: list[dict[str, object]] = []

        def fake_start(
            repo_root: Path | str,
            config_name: str = "default.yaml",
            instance_id: str | None = None,
            port: int | None = None,
            expected_identity: dict[str, object] | None = None,
            start_paused: bool = False,
            log_level: str | None = None,
            *,
            mode: str = "default",
            expected_config_fingerprint: str | None = None,
        ) -> LockInfo:
            calls.append(
                {
                    "instance_id": instance_id,
                    "port": port,
                    "start_paused": start_paused,
                    "log_level": log_level,
                    "mode": mode,
                    "expected_config_fingerprint": expected_config_fingerprint,
                }
            )
            return LockInfo(
                repo_root=str(repo_root),
                pid=1000 + len(calls),
                started_at="",
                http_port=port or 0,
                state_dir=str(tmp_path / ".issue-orchestrator" / "state"),
                recovered=False,
                instance_id=instance_id,
            )

        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.find_free_port",
            MagicMock(side_effect=[19081, 19082]),
        )
        monkeypatch.setattr("issue_orchestrator.infra.supervisor.start", fake_start)

        infos = start_instances(tmp_path, count=2, start_paused=True)

        assert [info.instance_id for info in infos] == [
            "orchestrator-1",
            "orchestrator-2",
        ]
        assert [call["start_paused"] for call in calls] == [True, True]

    def test_start_instances_forwards_log_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multi-instance startup passes log-level intent to each child."""
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("instances: 2\nagents: {}\n")

        calls: list[dict[str, object]] = []

        def fake_start(
            repo_root: Path | str,
            config_name: str = "default.yaml",
            instance_id: str | None = None,
            port: int | None = None,
            expected_identity: dict[str, object] | None = None,
            start_paused: bool = False,
            log_level: str | None = None,
            *,
            mode: str = "default",
            expected_config_fingerprint: str | None = None,
        ) -> LockInfo:
            calls.append(
                {
                    "instance_id": instance_id,
                    "port": port,
                    "log_level": log_level,
                    "mode": mode,
                    "expected_config_fingerprint": expected_config_fingerprint,
                }
            )
            return LockInfo(
                repo_root=str(repo_root),
                pid=1000 + len(calls),
                started_at="",
                http_port=port or 0,
                state_dir=str(tmp_path / ".issue-orchestrator" / "state"),
                recovered=False,
                instance_id=instance_id,
            )

        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.find_free_port",
            MagicMock(side_effect=[19081, 19082]),
        )
        monkeypatch.setattr("issue_orchestrator.infra.supervisor.start", fake_start)

        infos = start_instances(tmp_path, count=2, log_level="DEBUG")

        assert [info.instance_id for info in infos] == [
            "orchestrator-1",
            "orchestrator-2",
        ]
        assert [call["log_level"] for call in calls] == ["DEBUG", "DEBUG"]

    def test_start_instances_rolls_back_prior_children_when_later_start_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / ".issue-orchestrator/config/default.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("instances: 2\nagents: {}\n", encoding="utf-8")
        first = LockInfo(
            repo_root=str(tmp_path),
            pid=1001,
            started_at="",
            http_port=27101,
            state_dir=str(tmp_path / ".issue-orchestrator/state"),
            instance_id="orchestrator-1",
        )
        starts = MagicMock(side_effect=[first, RuntimeError("second failed")])
        rollback = MagicMock(return_value=True)
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.find_free_port",
            MagicMock(side_effect=[27101, 27102]),
        )
        monkeypatch.setattr("issue_orchestrator.infra.supervisor.start", starts)
        monkeypatch.setattr("issue_orchestrator.infra.supervisor.stop", rollback)
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.shutdown_timing.process_is_alive",
            lambda _pid: False,
        )

        with pytest.raises(RuntimeError, match="second failed"):
            start_instances(tmp_path, count=2)

        rollback.assert_called_once_with(
            tmp_path.resolve(),
            force=True,
            instance_id="orchestrator-1",
            reason="rollback partial multi-instance start",
            actor="supervisor.start_instances.rollback",
        )

    def test_start_instances_rolls_back_when_later_port_allocation_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / ".issue-orchestrator/config/default.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("instances: 2\nagents: {}\n", encoding="utf-8")
        first = LockInfo(
            repo_root=str(tmp_path),
            pid=1001,
            started_at="",
            http_port=27201,
            state_dir=str(tmp_path / ".issue-orchestrator/state"),
            instance_id="orchestrator-1",
        )
        starts = MagicMock(return_value=first)
        rollback = MagicMock(return_value=True)
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.find_free_port",
            MagicMock(side_effect=[27201, OSError("no ports available")]),
        )
        monkeypatch.setattr("issue_orchestrator.infra.supervisor.start", starts)
        monkeypatch.setattr("issue_orchestrator.infra.supervisor.stop", rollback)
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.shutdown_timing.process_is_alive",
            lambda _pid: False,
        )

        with pytest.raises(OSError, match="no ports available"):
            start_instances(tmp_path, count=2)

        starts.assert_called_once()
        rollback.assert_called_once_with(
            tmp_path.resolve(),
            force=True,
            instance_id="orchestrator-1",
            reason="rollback partial multi-instance start",
            actor="supervisor.start_instances.rollback",
        )

    def test_start_instances_kills_attempt_pid_when_lock_is_not_published(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / ".issue-orchestrator/config/default.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("instances: 2\nagents: {}\n", encoding="utf-8")
        synthetic = LockInfo(
            repo_root=str(tmp_path),
            pid=27301,
            started_at="",
            http_port=27351,
            state_dir=str(tmp_path / ".issue-orchestrator/state"),
            instance_id="orchestrator-1",
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.find_free_port",
            MagicMock(side_effect=[27351, OSError("no ports available")]),
        )
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.start",
            MagicMock(return_value=synthetic),
        )
        normal_stop = MagicMock(return_value=True)
        monkeypatch.setattr("issue_orchestrator.infra.supervisor.stop", normal_stop)
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.shutdown_timing.process_is_alive",
            lambda pid: pid == synthetic.pid,
        )
        published_lock = MagicMock(side_effect=[None, synthetic])
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.read_lock",
            published_lock,
        )
        exact_kill = MagicMock()
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor._send_kill_signal",
            exact_kill,
        )
        verified_exit = MagicMock(return_value=True)
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor._wait_for_process_exit_after_force",
            verified_exit,
        )
        released = MagicMock()
        monkeypatch.setattr(
            "issue_orchestrator.infra.supervisor.release_lock",
            released,
        )

        with pytest.raises(OSError, match="no ports available"):
            start_instances(tmp_path, count=2)

        normal_stop.assert_called_once()
        exact_kill.assert_called_once_with(synthetic.pid, force=True)
        verified_exit.assert_called_once_with(
            synthetic.pid,
            timeout_iterations=20,
        )
        assert published_lock.call_count == 2
        released.assert_called_once_with(
            tmp_path.resolve(),
            synthetic.pid,
            synthetic.instance_id,
        )


class TestMultiInstanceStatus:
    """Tests for MultiInstanceStatus dataclass."""

    def test_multi_instance_status_to_dict(self) -> None:
        """MultiInstanceStatus.to_dict includes all fields."""
        status1 = SupervisorStatus(
            state="running", pid=111, port=8081, instance_id="orchestrator-1"
        )
        status2 = SupervisorStatus(
            state="running", pid=222, port=8082, instance_id="orchestrator-2"
        )
        multi = MultiInstanceStatus(
            repo_root="/path/to/repo",
            instances=[status1, status2],
            expected_count=2,
        )

        data = multi.to_dict()

        assert data["repo_root"] == "/path/to/repo"
        assert data["expected_count"] == 2
        assert data["running_count"] == 2
        assert len(data["instances"]) == 2
        assert data["instances"][0]["instance_id"] == "orchestrator-1"
        assert data["instances"][1]["instance_id"] == "orchestrator-2"

    def test_multi_instance_status_running_count(self) -> None:
        """running_count only counts running instances."""
        status1 = SupervisorStatus(state="running", instance_id="orchestrator-1")
        status2 = SupervisorStatus(state="stopped", instance_id="orchestrator-2")
        status3 = SupervisorStatus(state="failed", instance_id="orchestrator-3")
        multi = MultiInstanceStatus(
            repo_root="/path/to/repo",
            instances=[status1, status2, status3],
            expected_count=3,
        )

        data = multi.to_dict()

        assert data["expected_count"] == 3
        assert data["running_count"] == 1


class TestStatusAllInstances:
    """Tests for status_all_instances function."""

    def test_status_all_instances_no_running(self, tmp_path: Path) -> None:
        """status_all_instances returns empty list when no instances running."""
        # Create minimal config for expected_count
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        result = status_all_instances(tmp_path)

        assert result.repo_root == str(tmp_path)
        assert result.expected_count == 1  # Default
        assert len(result.instances) == 0

    def test_status_all_instances_with_single_instance(self, tmp_path: Path) -> None:
        """status_all_instances includes single-instance (legacy) lock."""
        # Create legacy single-instance lock
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        lock_data = {
            "repo_root": str(tmp_path),
            "pid": os.getpid(),
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8080,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
            "recovered": False,
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        # Create minimal config
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        result = status_all_instances(tmp_path)

        assert len(result.instances) == 1
        assert result.instances[0].state == "running"
        assert result.instances[0].pid == os.getpid()
        assert result.instances[0].port == 8080

    def test_status_all_instances_with_multi_instance(self, tmp_path: Path) -> None:
        """status_all_instances includes all instance locks."""
        # Create multi-instance locks
        locks_dir = tmp_path / ".issue-orchestrator" / "locks"
        locks_dir.mkdir(parents=True)

        for i in [1, 2]:
            lock_path = locks_dir / f"orchestrator-{i}.json"
            lock_data = {
                "repo_root": str(tmp_path),
                "pid": os.getpid(),
                "started_at": "2024-01-01T00:00:00Z",
                "http_port": 8080 + i,
                "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
                "recovered": False,
                "instance_id": f"orchestrator-{i}",
            }
            with open(lock_path, "w") as f:
                json.dump(lock_data, f)

        # Create config with instances: 2
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\nui:\n  instances: 2\n")

        result = status_all_instances(tmp_path)

        assert result.expected_count == 2
        assert len(result.instances) == 2


class TestSupervisorStatusInstanceId:
    """Tests for instance_id in SupervisorStatus."""

    def test_status_to_dict_includes_instance_id(self) -> None:
        """to_dict includes instance_id when set."""
        status_obj = SupervisorStatus(
            state="running",
            pid=12345,
            port=8080,
            instance_id="orchestrator-1",
        )

        data = status_obj.to_dict()

        assert data["instance_id"] == "orchestrator-1"

    def test_status_to_dict_excludes_instance_id_when_none(self) -> None:
        """to_dict excludes instance_id when None (backward compat)."""
        status_obj = SupervisorStatus(
            state="running",
            pid=12345,
            port=8080,
        )

        data = status_obj.to_dict()

        # instance_id should not be in dict when None
        assert "instance_id" not in data
