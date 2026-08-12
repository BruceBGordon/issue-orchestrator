"""Dependency wiring for Control Center orchestrator route modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Callable, Mapping, TypedDict

from fastapi import Depends, FastAPI, Request

from ..infra.repo_guardrails import RepoGuardrailsInstallResult

_ORCHESTRATOR_DEPENDENCIES_STATE_KEY = "control_api_orchestrator_dependencies"
_GUARDRAILS_REPAIRED_STATUS = "repaired"

if TYPE_CHECKING:
    from ..execution.control_center_actions import ControlCenterActions
    from ..ports.repository_engine_supervisor import SupervisorOps


@dataclass(frozen=True)
class ControlApiOrchestratorDependencies:
    """Dependency hooks needed by Control Center orchestrator routes."""

    get_supervisor: Callable[[], SupervisorOps]
    get_control_actions: Callable[[], ControlCenterActions]
    validate_repo_root: Callable[[str | None], Path | None]
    track_launched_pids: Callable[[Mapping[str, object]], None]
    coerce_graceful_timeout_seconds: Callable[[object, int], int]
    global_shutdown_in_progress: Callable[[], bool]
    begin_engine_shutdown_operation: Callable[[Path, bool, bool, int], None]
    finish_engine_shutdown_operation: Callable[[Path], None]


def install_control_api_orchestrator_dependencies(
    app: FastAPI,
    deps: ControlApiOrchestratorDependencies,
) -> None:
    """Install shared dependencies for Control Center orchestrator routes."""
    setattr(app.state, _ORCHESTRATOR_DEPENDENCIES_STATE_KEY, deps)


def get_control_api_orchestrator_dependencies(
    request: Request,
) -> ControlApiOrchestratorDependencies:
    """Resolve router dependencies from the FastAPI application state."""
    deps = getattr(request.app.state, _ORCHESTRATOR_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control Center orchestrator dependencies not configured")
    return deps


ControlApiOrchestratorDependency = Annotated[
    ControlApiOrchestratorDependencies,
    Depends(get_control_api_orchestrator_dependencies),
]


class GuardrailsRepairPayload(TypedDict, total=False):
    status: str
    repo_root: str
    hooks_path: str
    hooks_dir: str
    pre_push_hook: str
    verify_script: str
    helper_script: str
    installed_files: list[str]
    preserved_files: list[str]
    agent_hook_files: dict[str, list[str]]
    config_name: str
    mode: str
    message: str


class EngineStopStatus(StrEnum):
    STOPPED = "stopped"


class EngineStopPayload(TypedDict):
    status: EngineStopStatus
    repo_root: str
    stopped_count: int


def _repo_relative_path(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def serialize_guardrails_result(
    result: RepoGuardrailsInstallResult,
) -> GuardrailsRepairPayload:
    """Serialize a guardrail repair result with repository-relative paths."""
    repo_root = result.repo_root
    return {
        "status": _GUARDRAILS_REPAIRED_STATUS,
        "repo_root": str(repo_root),
        "hooks_path": result.hooks_path_config,
        "hooks_dir": _repo_relative_path(repo_root, result.hooks_dir),
        "pre_push_hook": _repo_relative_path(repo_root, result.pre_push_hook),
        "verify_script": _repo_relative_path(repo_root, result.verify_script),
        "helper_script": _repo_relative_path(repo_root, result.helper_script),
        "installed_files": [
            _repo_relative_path(repo_root, path) for path in result.installed_files
        ],
        "preserved_files": [
            _repo_relative_path(repo_root, path) for path in result.preserved_files
        ],
        "agent_hook_files": {
            agent_type: [_repo_relative_path(repo_root, path) for path in paths]
            for agent_type, paths in result.agent_hook_files.items()
        },
    }


def stopped_engine_payload(repo_root: Path, stopped_count: int) -> EngineStopPayload:
    """Build the typed successful engine-stop response."""
    return {
        "status": EngineStopStatus.STOPPED,
        "repo_root": str(repo_root),
        "stopped_count": stopped_count,
    }


def read_last_n_lines(log_path: Path, n: int) -> tuple[list[str], int]:
    """Read the last N lines of a log file plus its total line count."""
    with open(log_path, "rb") as handle:
        handle.seek(0, 2)
        file_size = handle.tell()
        lines: list[str] = []
        chunk_size = 8192
        remaining = file_size
        while len(lines) < n + 1 and remaining > 0:
            read_size = min(chunk_size, remaining)
            remaining -= read_size
            handle.seek(remaining)
            chunk = handle.read(read_size).decode("utf-8", errors="replace")
            chunk_lines = chunk.split("\n")
            if lines:
                lines[0] = chunk_lines[-1] + lines[0]
                chunk_lines = chunk_lines[:-1]
            lines = chunk_lines + lines
        lines = lines[-n:] if len(lines) > n else lines
        handle.seek(0)
        total_lines = sum(1 for _ in handle)
    return lines, total_lines


__all__ = [
    "ControlApiOrchestratorDependency",
    "ControlApiOrchestratorDependencies",
    "get_control_api_orchestrator_dependencies",
    "install_control_api_orchestrator_dependencies",
    "read_last_n_lines",
    "serialize_guardrails_result",
    "stopped_engine_payload",
]
