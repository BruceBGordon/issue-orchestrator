"""Behavior port for Repository Engine supervisor operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence, runtime_checkable

RUNNING_SUPERVISOR_STATE = "running"


class RepositoryEngineLock(Protocol):
    """Published runtime identity returned by a successful start."""

    pid: int
    http_port: int | None
    instance_id: str | None
    configuration_mode: str
    config_name: str
    config_fingerprint: str


@dataclass
class SupervisorStatus:
    """Observable state returned by a supervisor status query."""

    state: Literal["running", "stopped", "failed", "unknown"]
    pid: int | None = None
    port: int | None = None
    started_at: str | None = None
    recovered: bool = False
    error: str | None = None
    instance_id: str | None = None
    configuration_mode: str = "default"
    config_name: str = "default.yaml"
    config_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": self.state,
            "pid": self.pid,
            "port": self.port,
            "started_at": self.started_at,
            "recovered": self.recovered,
            "error": self.error,
            "configuration_mode": self.configuration_mode,
            "config_name": self.config_name,
            "config_fingerprint": self.config_fingerprint,
        }
        if self.instance_id is not None:
            result["instance_id"] = self.instance_id
        return result


@dataclass
class MultiInstanceStatus:
    """Observable state for every named instance of one repository."""

    repo_root: str
    instances: list[SupervisorStatus] = field(default_factory=list)
    expected_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "instances": [status.to_dict() for status in self.instances],
            "expected_count": self.expected_count,
            "running_count": sum(
                1
                for status in self.instances
                if status.state == RUNNING_SUPERVISOR_STATE
            ),
        }


class RepositoryEngineStopPolicy(Protocol):
    """Live policy source accepted by graceful supervisor shutdown."""

    def snapshot(self) -> Any: ...


@runtime_checkable
class SupervisorOps(Protocol):
    """Behavior required by launch and Control Center owners."""

    def start(
        self,
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
    ) -> RepositoryEngineLock: ...

    def stop(
        self,
        repo_root: Path | str,
        force: bool = False,
        instance_id: str | None = None,
        *,
        reason: str,
        actor: str = "supervisor.stop",
        graceful_timeout_seconds: float = 120,
        force_if_graceful_fails: bool = True,
        stop_policy: RepositoryEngineStopPolicy | None = None,
    ) -> bool: ...

    def stop_tracked_instance(
        self,
        repo_root: Path | str,
        tracked: SupervisorStatus,
        *,
        reason: str,
        actor: str,
    ) -> bool:
        """Stop only the exact PID/instance represented by ``tracked``."""
        ...

    def stop_by_port(
        self,
        port: int,
        *,
        reason: str,
        actor: str = "supervisor.stop_by_port",
        force: bool = False,
    ) -> bool: ...

    def status(
        self,
        repo_root: Path | str,
        instance_id: str | None = None,
    ) -> SupervisorStatus: ...

    def start_instances(
        self,
        repo_root: Path | str,
        config_name: str = "default.yaml",
        count: int | None = None,
        expected_identity: dict[str, Any] | None = None,
        start_paused: bool = False,
        log_level: str | None = None,
        *,
        mode: str = "default",
        expected_config_fingerprint: str | None = None,
    ) -> Sequence[RepositoryEngineLock]: ...

    def stop_all_instances(
        self,
        repo_root: Path | str,
        force: bool = False,
        *,
        reason: str,
        actor: str = "supervisor.stop_all_instances",
        graceful_timeout_seconds: float = 120,
        force_if_graceful_fails: bool = True,
        stop_policy: RepositoryEngineStopPolicy | None = None,
    ) -> int: ...

    def status_all_instances(
        self,
        repo_root: Path | str,
        config_name: str = "default.yaml",
        *,
        mode: str = "default",
    ) -> MultiInstanceStatus: ...


__all__ = [
    "MultiInstanceStatus",
    "RUNNING_SUPERVISOR_STATE",
    "RepositoryEngineLock",
    "SupervisorOps",
    "SupervisorStatus",
]
