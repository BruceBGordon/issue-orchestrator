"""Typed runtime configuration identity passed into agent subprocesses."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .repository_launch_selection import (
    ConfigurationModeName,
    RepositoryLaunchSelection,
)


@dataclass(frozen=True, slots=True)
class RuntimeConfigReference:
    """The selected orchestrator config file for a managed runtime action."""

    config_path: Path
    selection: RepositoryLaunchSelection

    def __post_init__(self) -> None:
        if not self.config_path.is_absolute():
            raise ValueError("config_path must be absolute")
        if self.config_path.name != self.selection.config.value:
            raise ValueError("config_path and selection config_name must match")

    @property
    def config_name(self) -> str:
        return self.selection.config.value

    @property
    def mode(self) -> ConfigurationModeName:
        return self.selection.mode

    def to_env(self) -> dict[str, str]:
        return {
            "ISSUE_ORCHESTRATOR_CONFIG_NAME": self.config_name,
            "ISSUE_ORCHESTRATOR_CONFIG_PATH": str(self.config_path),
            "ORCHESTRATOR_CONFIG_NAME": self.config_name,
            "ORCHESTRATOR_CONFIG_PATH": str(self.config_path),
            "ISSUE_ORCHESTRATOR_MODE": self.mode.value,
            "ORCHESTRATOR_MODE": self.mode.value,
        }
