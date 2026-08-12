"""Multi-repo registry for the supervisor.

Persists a list of registered repositories in ~/.config/issue-orchestrator/repos.json.
The supervisor can manage orchestrators for any registered repo.
"""

from __future__ import annotations

import json
import os
import fcntl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, TypeVar

from ..domain.repository_launch_selection import RepositoryLaunchSelection
from .atomic_json import atomic_write_json

_T = TypeVar("_T")


def _config_dir() -> Path:
    """Get the config directory for issue-orchestrator.

    Checks ISSUE_ORCHESTRATOR_CONFIG_DIR first (for testing),
    then XDG_CONFIG_HOME, otherwise ~/.config.
    """
    # Allow override for testing - isolates test registry from production
    if override := os.environ.get("ISSUE_ORCHESTRATOR_CONFIG_DIR"):
        return Path(override)
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        base = Path(xdg_config)
    else:
        base = Path.home() / ".config"
    return base / "issue-orchestrator"


def _repos_file() -> Path:
    """Get the path to the repos registry file."""
    return _config_dir() / "repos.json"


@dataclass
class RepoHealth:
    """Health status of a repository."""

    status: str  # "valid", "invalid", "unknown"
    checked_at: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.checked_at:
            self.checked_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "errors": self.errors,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepoHealth:
        """Create from dict."""
        return cls(
            status=data.get("status", "unknown"),
            checked_at=data.get("checked_at", ""),
            errors=data.get("errors", []),
            warnings=data.get("warnings", []),
        )


@dataclass
class RegisteredRepo:
    """A registered repository."""

    path: str
    name: str = ""
    added_at: str = ""
    health: RepoHealth | None = None  # Cached health status
    selected_config: str = "default.yaml"  # Last used config file
    selected_mode: str = "default"  # Last used directory-backed mode

    def __post_init__(self) -> None:
        self.select_launch_configuration(self.launch_selection)
        if not self.added_at:
            self.added_at = datetime.now(timezone.utc).isoformat()
        if not self.name:
            # Default name is the directory name
            self.name = Path(self.path).name

    @property
    def launch_selection(self) -> RepositoryLaunchSelection:
        """Return the complete desired mode/config pair as one typed value."""
        return RepositoryLaunchSelection.parse(
            mode=self.selected_mode,
            config_name=self.selected_config,
        )

    def select_launch_configuration(
        self,
        selection: RepositoryLaunchSelection,
    ) -> None:
        """Replace the complete desired launch selection atomically."""
        self.selected_mode = selection.mode.value
        self.selected_config = selection.config.value

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        result: dict[str, Any] = {
            "path": self.path,
            "name": self.name,
            "added_at": self.added_at,
            "selected_config": self.selected_config,
            "selected_mode": self.selected_mode,
        }
        if self.health:
            result["health"] = self.health.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RegisteredRepo:
        """Create from dict."""
        health = None
        if "health" in data:
            health = RepoHealth.from_dict(data["health"])
        return cls(
            path=data["path"],
            name=data.get("name", ""),
            added_at=data.get("added_at", ""),
            health=health,
            selected_config=data.get("selected_config", "default.yaml"),
            selected_mode=data.get("selected_mode", "default"),
        )


@dataclass
class RepoRegistry:
    """Registry of all managed repositories."""

    repos: list[RegisteredRepo] = field(default_factory=list)

    def add(
        self,
        repo_path: str | Path,
        *,
        name: str | None = None,
    ) -> RegisteredRepo:
        """Add a repository to the registry.

        Args:
            repo_path: Path to the repository root

        Returns:
            The registered repo entry

        Raises:
            ValueError: If the repo is already registered
        """
        normalized = str(Path(repo_path).resolve())

        # Check if already registered
        for repo in self.repos:
            if repo.path == normalized:
                raise ValueError(f"Repository already registered: {normalized}")

        repo = RegisteredRepo(path=normalized, name=name or "")
        self.repos.append(repo)
        return repo

    def remove(self, repo_path: str | Path) -> bool:
        """Remove a repository from the registry.

        Args:
            repo_path: Path to the repository root

        Returns:
            True if removed, False if not found
        """
        normalized = str(Path(repo_path).resolve())

        for i, repo in enumerate(self.repos):
            if repo.path == normalized:
                self.repos.pop(i)
                return True
        return False

    def get(self, repo_path: str | Path) -> RegisteredRepo | None:
        """Get a registered repo by path.

        Args:
            repo_path: Path to the repository root

        Returns:
            The registered repo or None if not found
        """
        normalized = str(Path(repo_path).resolve())

        for repo in self.repos:
            if repo.path == normalized:
                return repo
        return None

    def list_all(self) -> list[RegisteredRepo]:
        """Get all registered repos."""
        return list(self.repos)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "repos": [r.to_dict() for r in self.repos],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepoRegistry:
        """Create from dict."""
        repos = [RegisteredRepo.from_dict(r) for r in data.get("repos", [])]
        return cls(repos=repos)


def _load_registry_file(path: Path) -> RepoRegistry:
    if not path.exists():
        return RepoRegistry()
    with open(path, encoding="utf-8") as registry_file:
        return RepoRegistry.from_dict(json.load(registry_file))


def load_registry() -> RepoRegistry:
    """Load the repo registry from disk.

    Returns:
        The repo registry (empty if file doesn't exist)
    """
    return _load_registry_file(_repos_file())


def save_registry(registry: RepoRegistry) -> None:
    """Save the repo registry to disk.

    Args:
        registry: The registry to save
    """
    atomic_write_json(_repos_file(), registry.to_dict())


class RepoRegistryTransactionOwner:
    """Serialize each registry read-modify-write transaction."""

    def __init__(self) -> None:
        self._thread_gate = Lock()

    def mutate(self, mutation: Callable[[RepoRegistry], _T]) -> _T:
        path = _repos_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        gate_path = path.with_name(f".{path.name}.lock")
        with self._thread_gate:
            gate_fd = os.open(gate_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(gate_fd, fcntl.LOCK_EX)
                registry = _load_registry_file(path)
                result = mutation(registry)
                atomic_write_json(path, registry.to_dict())
                return result
            finally:
                fcntl.flock(gate_fd, fcntl.LOCK_UN)
                os.close(gate_fd)


_REGISTRY_TRANSACTIONS = RepoRegistryTransactionOwner()


def add_repo(
    repo_path: str | Path,
    *,
    name: str | None = None,
) -> RegisteredRepo:
    """Add a repository to the registry.

    Convenience function that loads, adds, and saves.

    Args:
        repo_path: Path to the repository root

    Returns:
        The registered repo entry
    """
    return _REGISTRY_TRANSACTIONS.mutate(
        lambda registry: registry.add(repo_path, name=name)
    )


def remove_repo(repo_path: str | Path) -> bool:
    """Remove a repository from the registry.

    Convenience function that loads, removes, and saves.

    Args:
        repo_path: Path to the repository root

    Returns:
        True if removed, False if not found
    """
    return _REGISTRY_TRANSACTIONS.mutate(lambda registry: registry.remove(repo_path))


def list_repos() -> list[RegisteredRepo]:
    """List all registered repositories.

    Returns:
        List of registered repos
    """
    return load_registry().list_all()


def cleanup_stale_repos() -> int:
    """Remove repos whose paths no longer exist on disk.

    This cleans up the registry when repo directories have been deleted
    (e.g., old pytest temp directories, moved projects).

    Returns:
        Number of repos removed
    """
    def remove_stale(registry: RepoRegistry) -> int:
        stale_paths = [
            repo.path for repo in registry.repos if not Path(repo.path).exists()
        ]
        for path in stale_paths:
            registry.remove(path)
        return len(stale_paths)

    return _REGISTRY_TRANSACTIONS.mutate(remove_stale)


def check_repo_health(
    repo_path: str | Path,
    config_name: str = "default.yaml",
    mode: str = "default",
) -> RepoHealth:
    """Run doctor checks for a repository and return health status.

    Args:
        repo_path: Path to the repository root
        config_name: Name of config file to check (default: default.yaml)

    Returns:
        RepoHealth with status, errors, and warnings
    """
    from .doctor import run_doctor
    from .config import Config, get_config_path, list_configs
    from ..execution.command_runner import LocalCommandRunner

    repo_path = Path(repo_path)

    # Check if any configs exist
    selection = RepositoryLaunchSelection.parse(mode=mode, config_name=config_name)
    available_configs = list_configs(repo_path, selection.mode)
    if not available_configs:
        return RepoHealth(
            status="invalid",
            errors=["No configuration found. Run the setup wizard to create one."],
        )

    # Try to load the specified config
    config = None
    config_path = get_config_path(
        repo_path,
        selection.config.value,
        selection.mode,
    )

    if config_path.exists():
        try:
            config = Config.load(config_path)
        except Exception as e:
            return RepoHealth(
                status="invalid",
                errors=[f"Failed to load config: {e}"],
            )
    else:
        return RepoHealth(
            status="invalid",
            errors=[f"Config file not found: {config_path}"],
        )

    # Run doctor (change to repo directory for worktree checks)
    import os

    original_cwd = os.getcwd()
    try:
        os.chdir(repo_path)
        result = run_doctor(
            config=config, config_path=config_path, runner=LocalCommandRunner()
        )
    finally:
        os.chdir(original_cwd)

    # Convert to RepoHealth
    errors = [f"{c.name}: {c.detail}" for c in result.checks if c.status == "error"]
    warnings = [f"{c.name}: {c.detail}" for c in result.checks if c.status == "warning"]

    if result.overall == "error":
        status = "invalid"
    elif result.overall == "warning":
        status = "valid"  # Warnings don't block, just notify
    else:
        status = "valid"

    return RepoHealth(status=status, errors=errors, warnings=warnings)


def update_repo_health(
    repo_path: str | Path,
    config_name: str | None = None,
    mode: str | None = None,
) -> RepoHealth:
    """Run doctor checks and persist the health status.

    Args:
        repo_path: Path to the repository root
        config_name: Config file to check (uses selected_config if not provided)

    Returns:
        The updated RepoHealth
    """
    registered = load_registry().get(repo_path)
    selection = registered.launch_selection if registered is not None else None
    health = check_repo_health(
        repo_path,
        config_name or (
            selection.config.value if selection is not None else "default.yaml"
        ),
        mode or (selection.mode.value if selection is not None else "default"),
    )
    if registered is None:
        return health

    def persist_health(registry: RepoRegistry) -> RepoHealth:
        current = registry.get(repo_path)
        if current is not None:
            current.health = health
        return health

    return _REGISTRY_TRANSACTIONS.mutate(persist_health)


def set_selected_launch_selection(
    repo_path: str | Path,
    selection: RepositoryLaunchSelection,
) -> bool:
    """Persist one complete desired launch selection for a repository."""
    def select(registry: RepoRegistry) -> bool:
        repo = registry.get(repo_path)
        if repo is None:
            return False
        repo.select_launch_configuration(selection)
        return True

    return _REGISTRY_TRANSACTIONS.mutate(select)


def record_launched_selection(
    repo_path: str | Path,
    selection: RepositoryLaunchSelection,
) -> None:
    """Register a launched repository and persist its effective selection."""
    def record(registry: RepoRegistry) -> None:
        repo = registry.get(repo_path)
        if repo is None:
            repo = registry.add(repo_path)
        repo.select_launch_configuration(selection)

    _REGISTRY_TRANSACTIONS.mutate(record)


def set_selected_config(repo_path: str | Path, config_name: str) -> bool:
    """Set the selected config for a repository.

    Args:
        repo_path: Path to the repository root
        config_name: Config file name to select

    Returns:
        True if updated, False if repo not found
    """
    def select_config(registry: RepoRegistry) -> bool:
        repo = registry.get(repo_path)
        if repo is None:
            return False
        repo.select_launch_configuration(
            RepositoryLaunchSelection.parse(
                mode=repo.launch_selection.mode,
                config_name=config_name,
            )
        )
        return True

    return _REGISTRY_TRANSACTIONS.mutate(select_config)
