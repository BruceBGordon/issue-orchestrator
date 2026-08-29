"""Scoped stale orchestrator cleanup for E2E tests."""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_E2E_CONFIG_FILE_RE = re.compile(r"^issue-orchestrator\.e2e\.(?P<owner_pid>\d+)\..*\.ya?ml$")
_E2E_CONFIG_DIR_RE = re.compile(r"^e2e-orchestrator-config-(?P<owner_pid>\d+)(?:-|$)")


@dataclass(frozen=True)
class E2EOrchestratorProcess:
    """A process launched by the E2E orchestrator harness."""

    pid: int
    command: str
    config_path: str
    owner_pid: int | None


CommandRunner = Callable[..., subprocess.CompletedProcess[Any]]
PidExists = Callable[[int], bool]


def stale_e2e_orchestrator_processes(
    ps_output: str,
    *,
    current_pid: int | None = None,
    pid_exists: PidExists | None = None,
) -> list[E2EOrchestratorProcess]:
    """Return E2E-owned orchestrator processes that are safe to terminate."""
    current_pid = os.getpid() if current_pid is None else current_pid
    pid_exists = _pid_exists if pid_exists is None else pid_exists

    stale_processes: list[E2EOrchestratorProcess] = []
    for line in ps_output.splitlines():
        process = _parse_e2e_orchestrator_process(line)
        if process is None:
            continue
        if _owned_by_other_live_worker(process, current_pid, pid_exists):
            logger.info(
                "[E2E CLEANUP] Skipping e2e orchestrator pid=%s owned by live worker pid=%s",
                process.pid,
                process.owner_pid,
            )
            continue
        stale_processes.append(process)
    return stale_processes


def kill_stale_e2e_orchestrators(
    *,
    run: CommandRunner = subprocess.run,
    current_pid: int | None = None,
    pid_exists: PidExists | None = None,
) -> int:
    """Terminate stale E2E-owned orchestrator processes.

    This deliberately avoids broad process-name matching. Repository engines
    can contain ``issue-orchestrator`` and ``start`` in their command lines
    without being E2E children.
    """
    try:
        ps_result = run(
            # -ww: without it procps truncates COMMAND to $COLUMNS even
            # into a pipe, and the substring match below decides what to
            # KILL. A truncated table quietly matches nothing (#7142).
            ["ps", "ax", "-ww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except PermissionError as exc:
        logger.info("[E2E CLEANUP] Skipping ps scan (permission denied): %s", exc)
        return 0
    except OSError as exc:
        logger.info("[E2E CLEANUP] Skipping ps scan: %s", exc)
        return 0

    if ps_result.returncode != 0:
        logger.info("[E2E CLEANUP] Skipping ps scan (returncode=%s)", ps_result.returncode)
        return 0

    killed = 0
    for process in stale_e2e_orchestrator_processes(
        str(ps_result.stdout or ""),
        current_pid=current_pid,
        pid_exists=pid_exists,
    ):
        try:
            kill_result = run(["kill", str(process.pid)], capture_output=True, check=False)
        except OSError as exc:
            logger.warning(
                "[E2E CLEANUP] Failed to kill stale e2e orchestrator pid=%s: %s",
                process.pid,
                exc,
            )
            continue

        if kill_result.returncode == 0:
            killed += 1
            logger.info("[E2E CLEANUP] Killed stale e2e orchestrator pid=%s", process.pid)
        else:
            logger.warning(
                "[E2E CLEANUP] Failed to kill stale e2e orchestrator pid=%s "
                "(returncode=%s)",
                process.pid,
                kill_result.returncode,
            )
    return killed


def is_e2e_orchestrator_start_command(command: str) -> bool:
    """Return whether a command is an E2E-owned orchestrator start command."""
    return _parse_e2e_orchestrator_process(f"0 {command}") is not None


def _parse_e2e_orchestrator_process(line: str) -> E2EOrchestratorProcess | None:
    line = line.strip()
    if not line:
        return None
    parts = line.split(None, 1)
    if len(parts) != 2:
        return None

    pid_text, command = parts
    try:
        pid = int(pid_text)
    except ValueError:
        return None

    try:
        args = shlex.split(command)
    except ValueError:
        return None

    if not _is_orchestrator_entrypoint(args):
        return None

    if "start" not in args:
        return None

    config_path = _extract_config_path(args)
    if config_path is None:
        return None

    owner_pid = _extract_e2e_owner_pid(config_path)
    if owner_pid is None:
        return None

    return E2EOrchestratorProcess(
        pid=pid,
        command=command,
        config_path=config_path,
        owner_pid=owner_pid,
    )


def _extract_config_path(args: list[str]) -> str | None:
    for index, arg in enumerate(args):
        if arg == "--config" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    return None


def _is_orchestrator_entrypoint(args: list[str]) -> bool:
    for arg in args:
        if Path(arg).name == "issue-orchestrator":
            return True
        if arg.startswith("issue_orchestrator.entrypoints."):
            return True
    return False


def _extract_e2e_owner_pid(config_path: str) -> int | None:
    path = Path(config_path)
    filename_match = _E2E_CONFIG_FILE_RE.match(path.name)
    if filename_match:
        return int(filename_match.group("owner_pid"))

    for parent in path.parents:
        dir_match = _E2E_CONFIG_DIR_RE.match(parent.name)
        if dir_match:
            return int(dir_match.group("owner_pid"))
    return None


def _owned_by_other_live_worker(
    process: E2EOrchestratorProcess,
    current_pid: int,
    pid_exists: PidExists,
) -> bool:
    return (
        process.owner_pid is not None
        and process.owner_pid != current_pid
        and pid_exists(process.owner_pid)
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
