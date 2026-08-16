"""Typed payloads shared by Repository Engine command surfaces."""

from typing import Any, TypedDict


class RepositoryEngineStartPayload(TypedDict, total=False):
    """Public result owned by the shared Repository Engine start command."""

    status: str
    launch_status: str
    error: str
    detail: str
    repo_root: str
    mode: str
    config_name: str
    config_fingerprint: str
    repo_identity: dict[str, Any]
    doctor: dict[str, Any]
    pid: int
    port: int | None
    supervisor: dict[str, Any] | None
    conflict: dict[str, Any] | None
    requested: dict[str, str]
    active: list[dict[str, str]]
    ports: list[int]
    health: str
    tick_age_seconds: float
    instances: list[dict[str, Any]]


class McpLaunchPayload(RepositoryEngineStartPayload, total=False):
    """Stable nested launch member returned by ``orchestrator.start``."""

    launched: bool
    error_code: str


__all__ = ["McpLaunchPayload", "RepositoryEngineStartPayload"]
