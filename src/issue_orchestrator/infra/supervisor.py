"""Supervisor for managing orchestrator processes.

The supervisor manages the lifecycle of orchestrator processes:
- Starting new orchestrators (one per repo, or multiple instances per repo)
- Stopping running orchestrators
- Querying status

Single-instance mode: One orchestrator per repo (default)
Multi-instance mode: Multiple orchestrators per repo (when instances > 1)

The supervisor itself does NOT run orchestration logic - it only manages processes.
"""

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .config_identity import (
    EXPECTED_CONFIG_FINGERPRINT_ENV,
    assert_expected_config_fingerprint,
)
from .repo_identity import normalize_repo_root, serialize_repo_identity, state_dir
from .repo_lock import (
    AlreadyRunning,
    LockInfo,
    assert_repository_configuration_identity,
    is_locked,
    list_instance_locks,
    read_lock,
    release_lock,
)
from .supervisor_models import MultiInstanceStatus, SupervisorStatus
from . import shutdown_timing

DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS = (
    shutdown_timing.DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS
)

logger = logging.getLogger(__name__)
_EXPECTED_IDENTITY_ENV = "ISSUE_ORCHESTRATOR_EXPECTED_IDENTITY"
ENGINE_LOG_LEVEL_ENV = "ISSUE_ORCHESTRATOR_ENGINE_LOG_LEVEL"
_VALID_ENGINE_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


def find_free_port() -> int:
    """Find a free port on the local machine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


def _ensure_log_dir(repo_root: Path, instance_id: str | None = None) -> Path:
    """Ensure the logs directory exists and return the log file path.

    Args:
        repo_root: Repository root path
        instance_id: Optional instance ID for multi-instance logs

    Returns:
        Path to log file (e.g., orchestrator.log or orchestrator-instance1.log)
    """
    log_dir = state_dir(repo_root) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if instance_id:
        return log_dir / f"orchestrator-{instance_id}.log"
    return log_dir / "orchestrator.log"


def _check_and_cleanup_stale_lock(repo_root: Path, instance_id: str | None) -> None:
    """Check for existing lock and clean up if stale. Raises AlreadyRunning if alive."""
    if not is_locked(repo_root, instance_id):
        return

    info = read_lock(repo_root, instance_id)
    if not info:
        return

    try:
        os.kill(info.pid, 0)
        raise AlreadyRunning(
            pid=info.pid,
            repo_root=repo_root,
            port=info.http_port,
            instance_id=instance_id,
        )
    except OSError:
        logger.info(
            "Cleaning up stale lock for %s instance=%s (pid %d not running)",
            repo_root,
            instance_id or "default",
            info.pid,
        )
        release_lock(repo_root, info.pid, instance_id)


def _extract_error_from_log(log_file: Path) -> str:
    """Extract error hint from log file."""
    if not log_file.exists():
        return ""
    try:
        lines = log_file.read_text().splitlines()
        error_lines = [
            l for l in lines if "ERROR" in l or "Traceback" in l or "ValueError" in l
        ]
        if error_lines:
            return f"\n\nError from log:\n  {error_lines[-1]}"
        for line in reversed(lines):
            if line.strip():
                return f"\n\nLast log entry:\n  {line}"
    except Exception:
        pass
    return ""


def _resolve_engine_log_level(log_level: str | None) -> str | None:
    raw_level = log_level or os.environ.get(ENGINE_LOG_LEVEL_ENV)
    if raw_level is None:
        return None
    normalized = raw_level.strip().upper()
    if not normalized:
        return None
    if normalized not in _VALID_ENGINE_LOG_LEVELS:
        valid = ", ".join(sorted(_VALID_ENGINE_LOG_LEVELS))
        raise ValueError(
            f"{ENGINE_LOG_LEVEL_ENV} must be one of: {valid}; got {raw_level!r}"
        )
    return normalized


def _engine_log_level_args(log_level: str | None) -> list[str]:
    engine_log_level = _resolve_engine_log_level(log_level)
    if engine_log_level is None:
        return []
    return ["--log-level", engine_log_level]


def _engine_subprocess_env(
    expected_identity: dict[str, Any] | None,
    expected_config_fingerprint: str | None,
) -> dict[str, str]:
    env = os.environ.copy()
    if expected_identity is not None:
        env[_EXPECTED_IDENTITY_ENV] = serialize_repo_identity(expected_identity)
    if expected_config_fingerprint is not None:
        env[EXPECTED_CONFIG_FINGERPRINT_ENV] = expected_config_fingerprint
    return env


def start(
    repo_root: Path | str,
    config_name: str = "default.yaml",
    instance_id: str | None = None,
    port: int | None = None,
    expected_identity: dict[str, Any] | None = None,
    start_paused: bool = False,
    log_level: str | None = None,
    *,
    mode: str = "default",
    expected_config_fingerprint: str | None = None,
    spawn_process: Callable[..., Any] | None = None,
) -> LockInfo:
    """Start an orchestrator for the given repository."""
    from .config import Config, get_config_path

    repo_root = normalize_repo_root(repo_root)
    config_path = get_config_path(repo_root, config_name, mode)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    config = Config.load(config_path)
    assert_expected_config_fingerprint(
        config.config_fingerprint,
        expected_config_fingerprint,
    )
    if port is None:
        port = config.web_port

    _check_and_cleanup_stale_lock(repo_root, instance_id)

    # Prepare log file
    log_file = _ensure_log_dir(repo_root, instance_id)

    # Build command
    # Use --no-browser since user can use control center's "Open UI" button
    cmd = [
        sys.executable,
        "-m",
        "issue_orchestrator.entrypoints.run_orchestrator",
        "--repo-root",
        str(repo_root),
        "--port",
        str(port),
        "--no-browser",
        "--config",
        str(config_path),
        "--mode",
        mode,
    ]
    if start_paused:
        cmd.append("--start-paused")
    cmd.extend(_engine_log_level_args(log_level))

    # Set up environment for the subprocess
    env = _engine_subprocess_env(expected_identity, expected_config_fingerprint)
    if instance_id:
        env["INSTANCE_ID"] = instance_id
        cmd.extend(["--instance-id", instance_id])

    instance_str = f" instance={instance_id}" if instance_id else ""
    logger.info(
        "Starting orchestrator for %s%s on port %d", repo_root, instance_str, port
    )
    logger.debug("Command: %s", " ".join(cmd))

    _spawn = spawn_process or subprocess.Popen

    # Open log file for subprocess output
    with open(log_file, "a") as log_f:
        # Start the orchestrator process
        # Use start_new_session=True to detach from parent's process group
        process = _spawn(
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            cwd=str(repo_root),
            start_new_session=True,
            env=env,
        )

    logger.info("Orchestrator started with PID %d", process.pid)

    # Wait for the process to create its lock file
    import time

    for _ in range(50):  # Wait up to 5 seconds
        info = read_lock(repo_root, instance_id)
        if info is not None and info.pid == process.pid:
            assert_expected_config_fingerprint(
                info.config_fingerprint,
                expected_config_fingerprint,
            )
            return info
        if process.poll() is not None:
            break
        time.sleep(0.1)

    poll = process.poll()
    if poll is not None:
        error_hint = _extract_error_from_log(log_file)
        raise RuntimeError(
            f"Orchestrator process exited immediately with code {poll}.{error_hint}\n\n"
            f"Full logs at: {log_file}"
        )

    # Process is running but didn't create lock file yet
    # Return a synthetic LockInfo
    return LockInfo(
        repo_root=str(repo_root),
        pid=process.pid,
        started_at="",
        http_port=port,
        state_dir=str(state_dir(repo_root)),
        recovered=False,
        instance_id=instance_id,
        configuration_mode=mode,
        config_name=config_name,
        config_fingerprint=config.config_fingerprint,
    )


def _kill_by_port(port: int, use_sigkill: bool = False) -> bool:
    """Kill processes using a specific port (fallback method).

    Returns True if any process was killed.
    """
    try:
        result = subprocess.run(
            ["lsof", "-t", f"-i:{port}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            sig = signal.SIGKILL if use_sigkill else signal.SIGTERM
            killed = False
            for pid_str in pids:
                try:
                    pid = int(pid_str)
                    os.kill(pid, sig)
                    logger.warning(
                        "port-based kill: %s pid=%d port=%d (cross-repo "
                        "if another orchestrator)",
                        sig.name,
                        pid,
                        port,
                    )
                    killed = True
                except (ProcessLookupError, ValueError):
                    pass
            return killed
    except FileNotFoundError:
        logger.debug("lsof not available for port-based kill")
    return False


def _is_port_in_use(port: int) -> bool:
    """Return True if any process is bound to the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-t", f"-i:{port}"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except FileNotFoundError:
        logger.debug("lsof not available for port check")
        return False


def _request_graceful_shutdown(
    port: int,
    *,
    reason: str,
    actor: str = "supervisor",
) -> bool:
    """Request HTTP shutdown once; the shared stop budget owns all waiting."""
    import json as _json
    import urllib.request
    import urllib.error

    if not reason or not reason.strip():
        # Fail-fast: the HTTP endpoint will 400 on empty reason
        # anyway, so there's no point making the round-trip and
        # logging a 400 in the target's log just to fall through to
        # signal kill. Surface the bug at the call site.
        raise ValueError(
            "_request_graceful_shutdown requires a non-empty reason; "
            "the /api/shutdown contract rejects unreasoned shutdowns",
        )
    body = _json.dumps({"reason": reason, "actor": actor}).encode("utf-8")

    try:
        logger.info(
            "Requesting graceful shutdown via HTTP on port %d (reason=%r actor=%r)",
            port,
            reason,
            actor,
        )
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown",
            method="POST",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError) as e:
        logger.debug("HTTP shutdown failed: %s, will use signals", e)
    except Exception as e:  # noqa: BLE001 — fall through to signal kill
        logger.debug("HTTP shutdown failed: %s, will use signals", e)
    return False


def stop_by_port(
    port: int,
    *,
    reason: str,
    actor: str = "supervisor.stop_by_port",
    force: bool = False,
) -> bool:
    """Stop an orchestrator by port when no lock file is available.

    ``reason`` is required by the orchestrator's HTTP shutdown
    contract; callers must thread their own reason so the target
    log records "who/why".
    """
    if not port:
        return False

    if not reason or not reason.strip():
        raise ValueError(
            "stop_by_port requires a non-empty reason; "
            "the /api/shutdown contract rejects unreasoned shutdowns",
        )

    if not force:
        import json as _json
        import urllib.request

        try:
            logger.info("Requesting shutdown on port %d (reason=%r)", port, reason)
            body = _json.dumps({"reason": reason, "actor": actor}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/shutdown",
                method="POST",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2.0):
                pass
        except Exception as e:  # noqa: BLE001 — fall through to port kill
            logger.debug("HTTP shutdown failed on port %d: %s", port, e)

        import time

        time.sleep(0.5)
        if not _is_port_in_use(port):
            return True

    killed = _kill_by_port(port, use_sigkill=force)
    if killed:
        import time

        time.sleep(0.5)
        return not _is_port_in_use(port)
    return False


def _wait_for_process_exit_after_force(pid: int, timeout_iterations: int) -> bool:
    """Wait a short fixed interval after SIGKILL delivery."""
    for _ in range(timeout_iterations):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except OSError:
            return True
    return False


def _send_kill_signal(pid: int, force: bool) -> None:
    """Send kill signal to process or process group."""
    sig = signal.SIGKILL if force else signal.SIGTERM
    logger.info("Sending %s to orchestrator process group %d", sig.name, pid)
    try:
        os.killpg(pid, sig)
    except OSError as e:
        logger.warning(
            "Failed to kill process group %d: %s, trying single process", pid, e
        )
        try:
            os.kill(pid, sig)
        except OSError as e2:
            logger.warning("Failed to send signal to pid %d: %s", pid, e2)


def stop(
    repo_root: Path | str,
    force: bool = False,
    instance_id: str | None = None,
    *,
    reason: str,
    actor: str = "supervisor.stop",
    graceful_timeout_seconds: float = DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS,
    force_if_graceful_fails: bool = True,
    stop_policy: shutdown_timing.StopPolicy | None = None,
    expected_pid: int | None = None,
) -> bool:
    """Stop the orchestrator; ``reason`` records the caller's intent."""
    if not reason or not reason.strip():
        raise ValueError(
            "supervisor.stop requires a non-empty reason; "
            "the /api/shutdown contract rejects unreasoned shutdowns",
        )

    repo_root = normalize_repo_root(repo_root)

    info = read_lock(repo_root, instance_id)
    if info is None:
        logger.debug("No lock file found for %s (already stopped)", repo_root)
        return True

    if expected_pid is not None and info.pid != expected_pid:
        logger.warning(
            "Refusing to stop replacement instance %s pid=%d; expected pid=%d",
            instance_id or "default",
            info.pid,
            expected_pid,
        )
        return False

    pid, port = info.pid, info.http_port

    if not shutdown_timing.process_is_alive(pid):
        release_lock(repo_root, pid, instance_id)
        logger.info("Cleaned up stale lock for %s (pid %d not running)", repo_root, pid)
        return True

    controller = shutdown_timing.InterruptibleStopController(
        stop_policy
        or shutdown_timing.StaticStopPolicy(
            graceful_timeout_seconds=graceful_timeout_seconds,
            force=force,
        ),
        pid=pid,
        force_requested=force,
        force_on_timeout=force_if_graceful_fails,
        request_graceful=lambda: bool(
            port and _request_graceful_shutdown(port, reason=reason, actor=actor)
        ),
        terminate=lambda: _send_kill_signal(pid, force=False),
        force_stop=lambda: _force_stop(repo_root, pid, port, instance_id),
        on_stopped=lambda: release_lock(repo_root, pid, instance_id),
    )
    stopped = controller.stop()
    logger.info("Orchestrator stop attempt completed pid=%d stopped=%s", pid, stopped)
    return stopped


def stop_tracked_instance(
    repo_root: Path | str,
    tracked: SupervisorStatus,
    *,
    reason: str,
    actor: str,
) -> bool:
    """Stop and verify only the exact process represented by ``tracked``."""
    if tracked.pid is None:
        return False
    stopped = stop(
        repo_root,
        force=True,
        instance_id=tracked.instance_id,
        reason=reason,
        actor=actor,
        expected_pid=tracked.pid,
    )
    return stopped and not shutdown_timing.process_is_alive(tracked.pid)


def _force_stop(
    repo_root: Path,
    pid: int,
    port: int | None,
    instance_id: str | None,
) -> bool:
    """Force one engine down and verify the process is gone."""
    stopped = _kill_with_signal_then_port(
        repo_root=repo_root,
        pid=pid,
        port=port,
        instance_id=instance_id,
        force=True,
        grace_seconds=0,
    )
    if stopped:
        return True
    if _force_kill_by_port_last_resort(
        repo_root=repo_root,
        pid=pid,
        port=port,
        instance_id=instance_id,
    ):
        return True
    logger.error("Failed to force stop orchestrator pid %d", pid)
    return False


def _kill_with_signal_then_port(
    *,
    repo_root: Path,
    pid: int,
    port: int | None,
    instance_id: str | None,
    force: bool,
    grace_seconds: float,
) -> bool:
    """Send signal, wait for exit; if still alive, try port kill."""
    _send_kill_signal(pid, force)
    if _wait_for_process_exit_after_force(
        pid,
        shutdown_timing.signal_exit_poll_iterations(
            force=force,
            grace_seconds=grace_seconds,
        ),
    ):
        release_lock(repo_root, pid, instance_id)
        logger.info("Orchestrator stopped (pid %d)", pid)
        return True

    if port:
        logger.warning("Process group kill failed, trying to kill by port %d", port)
        _kill_by_port(port, use_sigkill=force)
        if _wait_for_process_exit_after_force(pid, 20):
            release_lock(repo_root, pid, instance_id)
            logger.info("Orchestrator stopped via port kill (pid %d)", pid)
            return True

    return False


def _force_kill_by_port_last_resort(
    *,
    repo_root: Path,
    pid: int,
    port: int | None,
    instance_id: str | None,
) -> bool:
    """Last-resort SIGKILL by port; verify the process actually exited."""
    if not port:
        return False

    import time

    logger.warning("Force killing by port %d", port)
    _kill_by_port(port, use_sigkill=True)
    time.sleep(0.5)
    try:
        os.kill(pid, 0)
    except OSError:
        release_lock(repo_root, pid, instance_id)
        logger.info("Orchestrator force stopped via port kill")
        return True
    return False


def status(repo_root: Path | str, instance_id: str | None = None) -> SupervisorStatus:
    """Get the status of the orchestrator for the given repository (or specific instance).

    Args:
        repo_root: Repository root path
        instance_id: Optional instance ID for multi-instance deployments

    Returns:
        SupervisorStatus with current state
    """
    repo_root = normalize_repo_root(repo_root)

    info = read_lock(repo_root, instance_id)
    if info is None:
        return SupervisorStatus(state="stopped", instance_id=instance_id)

    # Check if process is alive
    try:
        os.kill(info.pid, 0)
    except OSError:
        # Process not running but lock exists = failed/crashed
        return SupervisorStatus(
            state="failed",
            pid=info.pid,
            port=info.http_port,
            started_at=info.started_at,
            recovered=info.recovered,
            error="Process not running (stale lock)",
            instance_id=instance_id,
            configuration_mode=info.configuration_mode,
            config_name=info.config_name,
            config_fingerprint=info.config_fingerprint,
        )

    return SupervisorStatus(
        state="running",
        pid=info.pid,
        port=info.http_port,
        started_at=info.started_at,
        recovered=info.recovered,
        instance_id=instance_id,
        configuration_mode=info.configuration_mode,
        config_name=info.config_name,
        config_fingerprint=info.config_fingerprint,
    )


# =============================================================================
# Multi-instance management functions
# =============================================================================


def start_instances(
    repo_root: Path | str,
    config_name: str = "default.yaml",
    count: int | None = None,
    expected_identity: dict[str, Any] | None = None,
    start_paused: bool = False,
    log_level: str | None = None,
    *,
    mode: str = "default",
    expected_config_fingerprint: str | None = None,
) -> list[LockInfo]:
    """Start multiple orchestrator instances for a repository.

    Args:
        repo_root: Repository root path
        config_name: Name of config file
        count: Number of instances to start (reads from config if not specified)
        start_paused: If True, start every instance paused.

    Returns:
        List of LockInfo for started instances
    """
    from .config import Config, get_config_path

    repo_root = normalize_repo_root(repo_root)
    config_path = get_config_path(repo_root, config_name, mode)
    config = Config.load(config_path)
    assert_expected_config_fingerprint(
        config.config_fingerprint,
        expected_config_fingerprint,
    )

    if count is None:
        count = config.instances

    if count <= 1:
        # Single instance mode - use legacy lock file
        return [
            start(
                repo_root,
                config_name,
                expected_identity=expected_identity,
                start_paused=start_paused,
                log_level=log_level,
                mode=mode,
                expected_config_fingerprint=expected_config_fingerprint,
            )
        ]

    # Multi-instance mode
    assert_repository_configuration_identity(
        repo_root,
        configuration_mode=config.configuration_mode,
        config_name=config.config_name,
        config_fingerprint=config.config_fingerprint,
    )
    results = []
    for i in range(1, count + 1):
        instance_id = f"orchestrator-{i}"
        try:
            port = find_free_port()
            info = start(
                repo_root,
                config_name,
                instance_id=instance_id,
                port=port,
                expected_identity=expected_identity,
                start_paused=start_paused,
                log_level=log_level,
                mode=mode,
                expected_config_fingerprint=expected_config_fingerprint,
            )
            results.append(info)
            logger.info("Started instance %s on port %d", instance_id, port)
        except AlreadyRunning:
            logger.warning("Instance %s already running, skipping", instance_id)
        except Exception as e:
            logger.error("Failed to start instance %s: %s", instance_id, e)
            _rollback_started_instances(repo_root, results, cause=e)
            raise

    return results


def _rollback_started_instances(
    repo_root: Path,
    started: list[LockInfo],
    *,
    cause: Exception,
) -> None:
    """Stop every child published by a failed multi-instance start attempt."""
    failed: list[str] = []
    for info in reversed(started):
        if not _rollback_started_instance(repo_root, info):
            failed.append(f"{info.instance_id}: process {info.pid} survived rollback")
    if failed:
        raise RuntimeError(
            "Multi-instance start failed and rollback was incomplete: "
            + "; ".join(failed)
        ) from cause


def _rollback_started_instance(repo_root: Path, info: LockInfo) -> bool:
    """Stop and verify one exact process created by the current start attempt."""
    try:
        stop(
            repo_root,
            force=True,
            instance_id=info.instance_id,
            reason="rollback partial multi-instance start",
            actor="supervisor.start_instances.rollback",
        )
    except Exception:
        logger.exception("Normal rollback failed for instance %s", info.instance_id)
    if not shutdown_timing.process_is_alive(info.pid):
        return True

    published = read_lock(repo_root, info.instance_id)
    if published is not None and published.pid != info.pid:
        logger.error(
            "Refusing to terminate replacement instance %s pid=%d while rolling back pid=%d",
            info.instance_id,
            published.pid,
            info.pid,
        )
        return False

    _send_kill_signal(info.pid, force=True)
    if not _wait_for_process_exit_after_force(info.pid, timeout_iterations=20):
        return False
    published_after_exit = read_lock(repo_root, info.instance_id)
    if published_after_exit is not None and published_after_exit.pid == info.pid:
        release_lock(repo_root, info.pid, info.instance_id)
    return True


def stop_all_instances(
    repo_root: Path | str,
    force: bool = False,
    *,
    reason: str,
    actor: str = "supervisor.stop_all_instances",
    graceful_timeout_seconds: float = DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS,
    force_if_graceful_fails: bool = True,
    stop_policy: shutdown_timing.StopPolicy | None = None,
) -> int:
    """Stop all orchestrator instances for a repository.

    Args:
        repo_root: Repository root path
        force: If True, use SIGKILL instead of SIGTERM
        reason: Required. The "why" behind this stop, threaded into
            each underlying ``/api/shutdown`` so the target log
            records the calling intent.
        actor: Source identifier (cc, cli, test-harness, ...). Used
            for log-aggregation grouping.

    Returns:
        Number of instances successfully stopped
    """
    if not reason or not reason.strip():
        raise ValueError(
            "stop_all_instances requires a non-empty reason; "
            "the /api/shutdown contract rejects unreasoned shutdowns",
        )

    repo_root = normalize_repo_root(repo_root)

    stopped_count = 0
    if stop(
        repo_root,
        force=force,
        instance_id=None,
        reason=reason,
        actor=actor,
        graceful_timeout_seconds=graceful_timeout_seconds,
        force_if_graceful_fails=force_if_graceful_fails,
        stop_policy=stop_policy,
    ):
        stopped_count += 1

    active_locks = list_instance_locks(repo_root)
    for lock_info in active_locks:
        if stop(
            repo_root,
            force=force,
            instance_id=lock_info.instance_id,
            reason=reason,
            actor=actor,
            graceful_timeout_seconds=graceful_timeout_seconds,
            force_if_graceful_fails=force_if_graceful_fails,
            stop_policy=stop_policy,
        ):
            stopped_count += 1

    return stopped_count


def status_all_instances(
    repo_root: Path | str,
    config_name: str = "default.yaml",
    *,
    mode: str = "default",
) -> MultiInstanceStatus:
    """Get status of all orchestrator instances for a repository.

    Args:
        repo_root: Repository root path
        config_name: Name of config file (to get expected instance count)

    Returns:
        MultiInstanceStatus with all instance statuses
    """
    from .config import Config, get_config_path

    repo_root = normalize_repo_root(repo_root)

    # Load config to get expected instance count
    config_path = get_config_path(repo_root, config_name, mode)
    try:
        config = Config.load(config_path)
        expected_count = config.instances
    except Exception:
        expected_count = 1

    instances: list[SupervisorStatus] = []

    # Check single-instance lock (legacy)
    single_status = status(repo_root, instance_id=None)
    if single_status.state != "stopped":
        instances.append(single_status)

    # Check multi-instance locks
    active_locks = list_instance_locks(repo_root)
    for lock_info in active_locks:
        instance_status = status(repo_root, instance_id=lock_info.instance_id)
        instances.append(instance_status)

    return MultiInstanceStatus(
        repo_root=str(repo_root),
        instances=instances,
        expected_count=expected_count,
    )
