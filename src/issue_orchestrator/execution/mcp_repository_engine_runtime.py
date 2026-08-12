"""Exact-identity interpretation for MCP Repository Engine attachment."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from ..contracts.mcp import McpErrorPayload
from ..contracts.repository_engine import McpLaunchPayload
from ..domain.repository_launch_selection import RepositoryConfigurationIdentity
from ..infra.config import Config
from ..infra.launcher import LaunchStatus
from ..ports.repository_engine_supervisor import SupervisorStatus
from .repository_engine_start import RepositoryEngineStartResult

_SUCCESSFUL_REPOSITORY_START_CONFLICTS = frozenset(
    {"already_running", "orphaned_running"}
)
_REPOSITORY_START_ERROR_TYPES = {
    "doctor_failed": "DoctorError",
    "configuration_conflict": "ConfigurationConflict",
}
_REPOSITORY_ERROR_LAUNCH_STATUSES = {
    "already_running": LaunchStatus.ALREADY_RUNNING,
    "orphaned_running": LaunchStatus.ALREADY_RUNNING,
    "doctor_failed": LaunchStatus.DOCTOR_ERROR,
    "configuration_conflict": LaunchStatus.CONFIGURATION_CONFLICT,
}


def repository_start_failure_error(
    result: RepositoryEngineStartResult,
) -> McpErrorPayload | None:
    """Map the shared start command outcome onto the MCP error envelope."""
    if result.succeeded:
        return None
    code = str(result.payload.get("error") or "launch_failed")
    if code in _SUCCESSFUL_REPOSITORY_START_CONFLICTS:
        return None
    return {
        "message": str(result.payload.get("detail") or code),
        "type": _REPOSITORY_START_ERROR_TYPES.get(code, "LaunchError"),
    }


def mcp_launch_payload(result: RepositoryEngineStartResult) -> McpLaunchPayload:
    """Project the shared command result onto the stable MCP launch envelope."""
    payload = dict(result.payload)
    error_code = payload.get("error")
    status = (
        LaunchStatus.parse(str(payload.get("launch_status")))
        if result.succeeded
        else _REPOSITORY_ERROR_LAUNCH_STATUSES.get(
            str(error_code),
            LaunchStatus.LAUNCH_ERROR,
        )
    )
    if error_code is not None:
        payload["error_code"] = error_code
        payload["error"] = str(payload.get("detail") or error_code)
    payload["status"] = status.value
    payload["launched"] = result.succeeded
    return cast(McpLaunchPayload, payload)


def config_identity(config: Config) -> RepositoryConfigurationIdentity:
    return RepositoryConfigurationIdentity.parse(
        mode=config.configuration_mode,
        config_name=config.config_name,
        fingerprint=config.config_fingerprint,
    )


def status_identity(status: SupervisorStatus) -> RepositoryConfigurationIdentity:
    return RepositoryConfigurationIdentity.parse(
        mode=status.configuration_mode,
        config_name=status.config_name,
        fingerprint=status.config_fingerprint,
    )


def require_status_identity(status: SupervisorStatus, config: Config) -> None:
    config_identity(config).require_match(status_identity(status))


def require_identity_if_running(active: SupervisorStatus, config: Config) -> None:
    if active.state == "running":
        require_status_identity(active, config)


def resolve_start_result(
    result: RepositoryEngineStartResult,
    config: Config,
    load_published_status: Callable[[], SupervisorStatus],
) -> SupervisorStatus:
    """Return only an exact matching running status from a shared start result."""
    payload = result.payload
    port = payload.get("port")
    if result.orphaned_running:
        if not isinstance(port, int):
            raise RuntimeError("Orphaned Repository Engine has no usable port")
        active = RepositoryConfigurationIdentity.parse(
            mode=payload.get("mode"),
            config_name=payload.get("config_name"),
            fingerprint=payload.get("config_fingerprint"),
        )
        config_identity(config).require_match(active)
        return SupervisorStatus(
            state="running",
            port=port,
            configuration_mode=active.selection.mode.value,
            config_name=active.selection.config.value,
            config_fingerprint=active.fingerprint,
        )
    if not result.succeeded:
        raise RuntimeError(
            "Failed to start orchestrator: "
            + str(payload.get("detail") or payload.get("error"))
        )
    status = load_published_status()
    require_status_identity(status, config)
    return status


__all__ = [
    "mcp_launch_payload",
    "require_identity_if_running",
    "require_status_identity",
    "repository_start_failure_error",
    "resolve_start_result",
]
