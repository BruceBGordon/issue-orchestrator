"""Configuration path, environment, and section helpers."""

import os
import re
from pathlib import Path
from typing import Any

from ..domain.worktree_paths import (
    WORKTREE_COLLECTION_DIR as WORKTREE_COLLECTION_DIR,
    default_worktree_base as default_worktree_base,
    default_worktree_base_config as default_worktree_base_config,
    resolve_worktree_base as resolve_worktree_base,
)

# Config directory structure
CONFIG_DIR = ".issue-orchestrator/config"
DEFAULT_CONFIG_NAME = "default.yaml"

# Pattern for ${VAR} environment variable references
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


class ConfigEnvVarError(Exception):
    """Raised when an environment variable referenced in config is not set."""


class ConfigSectionError(ValueError):
    """Raised when a config section has an invalid type."""


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

    Configs live at <repo>/.issue-orchestrator/config/<name>.yaml
    So repo root is 3 levels up from the config file.

    This is the SINGLE SOURCE OF TRUTH for this calculation.
    """
    return config_path.parent.parent.parent.resolve()


def resolve_relative_path(path: str | Path, repo_root: Path) -> Path:
    """Resolve a path relative to repo root if not absolute."""
    target = Path(path)
    if target.is_absolute():
        return target.resolve()
    return (repo_root / target).resolve()


def get_config_dir(repo_root: Path) -> Path:
    """Get the config directory for a repo."""
    return repo_root / CONFIG_DIR


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


def list_configs(repo_root: Path) -> list[str]:
    """List available config files in a repo's config directory."""
    config_dir = get_config_dir(repo_root)
    if not config_dir.exists():
        return []

    configs = sorted(
        file.name for file in config_dir.glob("*.yaml")
        if file.is_file()
    )
    if DEFAULT_CONFIG_NAME in configs:
        configs.remove(DEFAULT_CONFIG_NAME)
        configs.insert(0, DEFAULT_CONFIG_NAME)
    return configs


def get_config_path(repo_root: Path, config_name: str = DEFAULT_CONFIG_NAME) -> Path:
    """Get the full path to a config file."""
    return get_config_dir(repo_root) / config_name


def config_exists(repo_root: Path, config_name: str = DEFAULT_CONFIG_NAME) -> bool:
    """Check if a config file exists."""
    return get_config_path(repo_root, config_name).exists()


def find_config_file(
    start_path: Path | None = None,
    config_name: str = DEFAULT_CONFIG_NAME,
) -> Path | None:
    """Find the config file by searching up the directory tree."""
    search_path = start_path or Path.cwd()

    for path in [search_path, *search_path.parents]:
        config_file = get_config_path(path, config_name)
        if config_file.exists():
            return config_file

    return None
