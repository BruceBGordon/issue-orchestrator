"""Configuration path, environment, and section helpers."""

import os
import re
from pathlib import Path
from typing import Any

from ..domain.repository_launch_selection import (
    ConfigurationModeName,
    RepositoryLaunchSelection,
)
from ..domain.worktree_paths import (
    WORKTREE_COLLECTION_DIR as WORKTREE_COLLECTION_DIR,
    default_worktree_base as default_worktree_base,
    default_worktree_base_config as default_worktree_base_config,
    resolve_worktree_base as resolve_worktree_base,
)

# Config directory structure
CONFIG_DIR = ".issue-orchestrator/config"
DEFAULT_CONFIG_NAME = "default.yaml"
MODES_DIR = "modes"
MAINTENANCE_DIR = "maintenance"

# Pattern for ${VAR} environment variable references
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


class ConfigEnvVarError(Exception):
    """Raised when an environment variable referenced in config is not set."""


class ConfigSectionError(ValueError):
    """Raised when a config section has an invalid type."""


def selection_from_config_path(config_path: Path) -> RepositoryLaunchSelection:
    """Derive the typed launch selection encoded by config storage layout."""
    resolved = config_path.expanduser().resolve()
    parent = resolved.parent
    is_mode_path = (
        parent.parent.name == MODES_DIR
        and parent.parent.parent.name == "config"
        and parent.parent.parent.parent.name == ".issue-orchestrator"
    )
    mode = ConfigurationModeName(parent.name) if is_mode_path else None
    return RepositoryLaunchSelection.parse(
        mode=mode,
        config_name=resolved.name,
    )


def expand_env_vars(value: Any, path: str = "") -> Any:
    """Recursively expand ${VAR} environment variable references in config values."""
    if isinstance(value, dict):
        return {
            key: expand_env_vars(item, f"{path}.{key}" if path else key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            expand_env_vars(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(lambda match: _replace_env_var(match, path), value)
    return value


def _replace_env_var(match: re.Match[str], path: str) -> str:
    var_name = match.group(1)
    env_value = os.environ.get(var_name)
    if env_value is None:
        location = f" (in {path})" if path else ""
        raise ConfigEnvVarError(
            f"Environment variable '{var_name}' is not set{location}"
        )
    return env_value


def repo_root_from_config_path(config_path: Path) -> Path:
    """Get the repo root from a config file path.

    Mode configs live at
    ``<repo>/.issue-orchestrator/config/modes/<mode>/<name>.yaml``.
    A centralized ancestor lookup also recognizes the inexpensive legacy flat
    layout used by older fixtures and repositories.

    This is the SINGLE SOURCE OF TRUTH for this calculation.
    """
    resolved = config_path.expanduser().resolve()
    for parent in resolved.parents:
        if parent.name == "config" and parent.parent.name == ".issue-orchestrator":
            return parent.parent.parent.resolve()
    # Explicit ``--config`` paths are a supported boundary and may live
    # outside the conventional directory. Preserve their historical root
    # inference; repository launch selections themselves always use the
    # contained resolver below.
    return resolved.parent.parent.parent.resolve()


def require_engine_launch_config_path(config_path: Path) -> Path:
    """Reject repository-managed maintenance YAML as an engine launch config."""
    lexical = config_path.expanduser().absolute()
    for parent in lexical.parents:
        if parent.name != "config" or parent.parent.name != ".issue-orchestrator":
            continue
        repo_root = parent.parent.parent
        _require_repo_contained_config_path(repo_root, lexical)
        relative = lexical.relative_to(parent)
        is_legacy_launch = len(relative.parts) == 1
        is_mode_launch = (
            len(relative.parts) == 3 and relative.parts[0] == MODES_DIR
        )
        if relative.parts[0] == MAINTENANCE_DIR:
            raise ValueError("A maintenance config cannot launch a Repository Engine")
        if not is_legacy_launch and not is_mode_launch:
            raise ValueError(
                "Repository Engine configs must live under config/modes/<mode>/"
            )
        return lexical.resolve()
    if lexical.is_symlink():
        raise ValueError("Explicit Repository Engine configs must not be symlinks")
    return lexical.resolve()


def resolve_relative_path(path: str | Path, repo_root: Path) -> Path:
    """Resolve a path relative to repo root if not absolute."""
    target = Path(path)
    if target.is_absolute():
        return target.resolve()
    return (repo_root / target).resolve()


def get_config_dir(repo_root: Path) -> Path:
    """Get the config directory for a repo."""
    return repo_root / CONFIG_DIR


def get_modes_dir(repo_root: Path) -> Path:
    """Return the root containing configuration mode directories."""
    return get_config_dir(repo_root) / MODES_DIR


def get_mode_dir(
    repo_root: Path,
    mode: str | ConfigurationModeName = "default",
) -> Path:
    """Return the contained directory for one validated mode."""
    validated = (
        mode if isinstance(mode, ConfigurationModeName) else ConfigurationModeName(mode)
    )
    return get_modes_dir(repo_root) / validated.value


def _require_repo_contained_config_path(repo_root: Path, candidate: Path) -> Path:
    """Reject config paths that escape or alias the repository-managed tree."""
    try:
        relative_candidate = candidate.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(
            f"Configuration path must remain inside repository {repo_root}: {candidate}"
        ) from exc

    current = repo_root
    for part in relative_candidate.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(
                f"Configuration paths must not be symbolic links: {candidate}"
            )
    try:
        candidate.resolve(strict=False).relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"Configuration path must remain inside repository {repo_root}: {candidate}"
        ) from exc
    return candidate


def configuration_mode_from_path(config_path: Path) -> ConfigurationModeName:
    """Derive a mode from a resolved config path, defaulting for flat configs."""
    return selection_from_config_path(config_path).mode


def _is_launchable_mode_directory(directory: Path) -> bool:
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or not any(
            path.is_file() and not path.is_symlink()
            for path in directory.glob("*.yaml")
        )
    ):
        return False
    try:
        ConfigurationModeName(directory.name)
    except ValueError:
        return False
    return True


def get_section(data: dict, key: str, config_path: Path) -> dict:
    """Get a config section, validating it is a dict.

    YAML quirk: `section:` with only comments or nothing becomes None.
    This helper provides clear error messages for this common mistake.
    """
    value = data.get(key)
    if value is None:
        return {}
    if isinstance(value, dict):
        return value

    type_name = type(value).__name__
    if isinstance(value, str):
        hint = (
            f"  Got string: '{value}'\n"
            f"  Expected a mapping like:\n"
            f"    {key}:\n"
            f"      some_option: value"
        )
    elif isinstance(value, (list, tuple)):
        hint = (
            f"  Got a list, but '{key}' should be a mapping.\n"
            f"  Expected:\n"
            f"    {key}:\n"
            f"      some_option: value"
        )
    else:
        hint = f"  Got {type_name}: {value!r}"

    raise ConfigSectionError(
        f"Invalid config section '{key}' in {config_path}\n"
        f"{hint}\n\n"
        f"If you meant to leave '{key}' empty, either:\n"
        f"  - Remove the '{key}:' line entirely, or\n"
        f"  - Use '{key}: {{}}' for an explicit empty mapping"
    )


def list_modes(repo_root: Path) -> list[str]:
    """List modes containing at least one launchable YAML config."""
    modes_dir = get_modes_dir(repo_root)
    _require_repo_contained_config_path(repo_root, modes_dir / "placeholder.yaml")
    modes = (
        sorted(
            directory.name
            for directory in modes_dir.iterdir()
            if _is_launchable_mode_directory(directory)
        )
        if modes_dir.exists()
        else []
    )
    legacy_configs = [
        path
        for path in get_config_dir(repo_root).glob("*.yaml")
        if path.is_file() and not path.is_symlink()
    ]
    default_mode_dir = get_mode_dir(repo_root, ConfigurationModeName("default"))
    if legacy_configs and "default" not in modes and not default_mode_dir.exists():
        modes.insert(0, "default")
    elif "default" in modes:
        modes.remove("default")
        modes.insert(0, "default")
    return modes


def list_configs(
    repo_root: Path,
    mode: str | ConfigurationModeName = "default",
) -> list[str]:
    """List launchable config files for one mode."""
    validated_mode = (
        mode if isinstance(mode, ConfigurationModeName) else ConfigurationModeName(mode)
    )
    mode_dir = get_mode_dir(repo_root, validated_mode)
    _require_repo_contained_config_path(repo_root, mode_dir / "placeholder.yaml")
    if mode_dir.exists():
        configs = sorted(
            file.name
            for file in mode_dir.glob("*.yaml")
            if file.is_file() and not file.is_symlink()
        )
    elif validated_mode.value == "default":
        config_dir = get_config_dir(repo_root)
        configs = (
            sorted(
                file.name
                for file in config_dir.glob("*.yaml")
                if file.is_file() and not file.is_symlink()
            )
            if config_dir.exists()
            else []
        )
    else:
        return []
    if DEFAULT_CONFIG_NAME in configs:
        configs.remove(DEFAULT_CONFIG_NAME)
        configs.insert(0, DEFAULT_CONFIG_NAME)
    return configs


def get_config_path(
    repo_root: Path,
    config_name: str = DEFAULT_CONFIG_NAME,
    mode: str | ConfigurationModeName = "default",
) -> Path:
    """Resolve one typed mode/config pair to a contained config path.

    Flat default configs remain readable as a centralized, low-cost
    compatibility path. New writes resolve into ``modes/default``.
    """
    selection = RepositoryLaunchSelection.parse(mode=mode, config_name=config_name)
    mode_dir = get_mode_dir(repo_root, selection.mode)
    mode_path = _require_repo_contained_config_path(
        repo_root, mode_dir / selection.config.value
    )
    legacy_path = get_config_dir(repo_root) / selection.config.value
    if (
        selection.mode == ConfigurationModeName.default()
        and not mode_dir.exists()
        and legacy_path.exists()
    ):
        return _require_repo_contained_config_path(repo_root, legacy_path)
    return mode_path


def config_exists(
    repo_root: Path,
    config_name: str = DEFAULT_CONFIG_NAME,
    mode: str | ConfigurationModeName = "default",
) -> bool:
    """Check if a config file exists."""
    return get_config_path(repo_root, config_name, mode).exists()


def find_config_file(
    start_path: Path | None = None,
    config_name: str = DEFAULT_CONFIG_NAME,
    mode: str | ConfigurationModeName = "default",
) -> Path | None:
    """Find the config file by searching up the directory tree."""
    search_path = start_path or Path.cwd()

    for path in [search_path, *search_path.parents]:
        config_file = get_config_path(path, config_name, mode)
        if config_file.exists():
            return config_file

    return None
