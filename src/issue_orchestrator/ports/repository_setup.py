"""Ports and typed artifacts for repository setup execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from ..domain.repository_config_name import RepositoryConfigName
from .repository_host import RepositoryHost

RepositorySetupFileAction = Literal["create", "overwrite"]
RepositorySetupFileKind = Literal["config", "prompt"]


@dataclass(frozen=True, slots=True)
class RepositorySetupNamedConfig:
    """A config stored in the repository's managed config directory."""

    name: RepositoryConfigName


@dataclass(frozen=True, slots=True)
class RepositorySetupExplicitConfig:
    """An explicit config path selected by an interactive CLI user."""

    path: Path

    def __post_init__(self) -> None:
        if not self.path.is_absolute() or not self.path.name:
            raise ValueError("Repository setup config path must be an absolute file path")


RepositorySetupConfigTarget = (
    RepositorySetupNamedConfig | RepositorySetupExplicitConfig
)


@dataclass(frozen=True, slots=True)
class RepositorySetupPlannedFile:
    """One exact filesystem mutation in a repository setup plan."""

    path: Path
    content: str
    action: RepositorySetupFileAction
    kind: RepositorySetupFileKind
    agent: str | None = None


@dataclass(frozen=True, slots=True)
class RepositorySetupArtifactPlan:
    """Rendered config plus every file mutation required to make it runnable."""

    config_yaml: str
    files: tuple[RepositorySetupPlannedFile, ...]


@dataclass(frozen=True, slots=True)
class RepositorySetupFileSystemError(Exception):
    """A setup file plan failed after applying the listed paths."""

    operation: str
    applied_paths: tuple[Path, ...]
    cause: Exception

    def __str__(self) -> str:
        return f"{self.operation}: {self.cause}"


class RepositorySetupFileSystem(Protocol):
    """Plan and apply repository-local setup artifacts."""

    def plan(
        self,
        *,
        repo_root: Path,
        config_target: RepositorySetupConfigTarget,
        config: Mapping[str, Any],
        include_prompts: bool,
    ) -> RepositorySetupArtifactPlan: ...

    def apply(self, plan: RepositorySetupArtifactPlan) -> tuple[Path, ...]: ...


class RepositorySetupHostFactory(Protocol):
    """Resolve the repository host used for setup label mutations."""

    def __call__(self, repo_name: str) -> RepositoryHost: ...


__all__ = [
    "RepositorySetupArtifactPlan",
    "RepositorySetupConfigTarget",
    "RepositorySetupExplicitConfig",
    "RepositorySetupFileAction",
    "RepositorySetupFileKind",
    "RepositorySetupFileSystem",
    "RepositorySetupFileSystemError",
    "RepositorySetupHostFactory",
    "RepositorySetupNamedConfig",
    "RepositorySetupPlannedFile",
]
