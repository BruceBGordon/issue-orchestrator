"""Ports and typed artifacts for repository setup execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from ..domain.repository_setup_auth import RepositorySetupGitHubAuthorization
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
            raise ValueError(
                "Repository setup config path must be an absolute file path"
            )


RepositorySetupConfigTarget = RepositorySetupNamedConfig | RepositorySetupExplicitConfig


@dataclass(frozen=True, slots=True)
class RepositorySetupValidationDefaults:
    """Repository-meaningful validation commands detected for guided setup."""

    quick_command: str | None
    publish_command: str | None
    source: str

    def __post_init__(self) -> None:
        if bool(self.quick_command) != bool(self.publish_command):
            raise ValueError(
                "Repository setup validation defaults require both commands or neither"
            )
        if not self.source.strip():
            raise ValueError("Repository setup validation source is required")


class RepositorySetupValidationDetector(Protocol):
    """Detect repository-native validation gates without executing them."""

    def __call__(self, repo_root: Path) -> RepositorySetupValidationDefaults: ...


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


class RepositorySetupGitHubAuthorizationCodec(Protocol):
    """Own every external representation of setup GitHub authorization."""

    def from_config(
        self,
        config: Mapping[str, Any],
    ) -> RepositorySetupGitHubAuthorization: ...

    def to_config(
        self,
        authorization: RepositorySetupGitHubAuthorization,
    ) -> dict[str, Any]: ...

    def from_public(
        self,
        payload: Mapping[str, Any],
    ) -> RepositorySetupGitHubAuthorization: ...

    def to_public(
        self,
        authorization: RepositorySetupGitHubAuthorization,
        *,
        redact_inline_token: bool = False,
    ) -> dict[str, Any]: ...

    def adapter_kwargs(
        self,
        authorization: RepositorySetupGitHubAuthorization,
    ) -> dict[str, str | None]: ...


class RepositorySetupHostFactory(Protocol):
    """Resolve the repository host used for setup label mutations."""

    def __call__(
        self,
        repo_name: str,
        authorization: RepositorySetupGitHubAuthorization,
    ) -> RepositoryHost: ...


@dataclass(frozen=True, slots=True)
class RepositorySetupGitHubVerification:
    """Verified, non-secret GitHub identity and source shown during setup."""

    identity: str
    repository: str
    auth_kind: Literal["personal", "github_app"]
    source: str
    normalized_authorization: RepositorySetupGitHubAuthorization


class RepositorySetupGitHubVerifier(Protocol):
    """Verify a setup authorization without mutating GitHub or repository files."""

    def __call__(
        self,
        repo_name: str,
        authorization: RepositorySetupGitHubAuthorization,
    ) -> RepositorySetupGitHubVerification: ...


class RepositorySetupGitHubTokenStore(Protocol):
    """Persist a verified personal token at a repo-scoped secret reference."""

    def __call__(
        self,
        authorization: RepositorySetupGitHubAuthorization,
        *,
        repo: str,
    ) -> RepositorySetupGitHubAuthorization: ...


__all__ = [
    "RepositorySetupArtifactPlan",
    "RepositorySetupConfigTarget",
    "RepositorySetupExplicitConfig",
    "RepositorySetupFileAction",
    "RepositorySetupFileKind",
    "RepositorySetupFileSystem",
    "RepositorySetupFileSystemError",
    "RepositorySetupGitHubAuthorizationCodec",
    "RepositorySetupGitHubVerification",
    "RepositorySetupGitHubTokenStore",
    "RepositorySetupGitHubVerifier",
    "RepositorySetupHostFactory",
    "RepositorySetupNamedConfig",
    "RepositorySetupPlannedFile",
    "RepositorySetupValidationDefaults",
    "RepositorySetupValidationDetector",
]
