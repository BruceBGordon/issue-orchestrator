"""Codex config discovery required before permission-profile launches."""

from __future__ import annotations

import os
from pathlib import Path
import tomllib
from typing import Any

from issue_orchestrator.domain.sandbox_scope import SandboxUnsupportedError

__all__ = [
    "resolve_codex_home",
    "validate_codex_permission_profile_compatibility",
]


def resolve_codex_home() -> Path:
    """Return the documented Codex state/config root."""
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser().resolve() if raw else Path.home() / ".codex"


def _read_codex_config(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SandboxUnsupportedError(
            f"Codex sandbox could not validate config layer {path}: {exc}"
        ) from exc


def _base_config_layers(
    *, codex_home: Path | None = None
) -> tuple[tuple[Path, dict[str, Any]], ...]:
    """Load the lower-precedence system and user config layers."""
    effective_home = (
        codex_home.expanduser().resolve()
        if codex_home is not None
        else resolve_codex_home()
    )
    paths = (Path("/etc/codex/config.toml"), effective_home / "config.toml")
    return tuple((path, _read_codex_config(path)) for path in paths if path.is_file())


def _project_root_markers(
    base_layers: tuple[tuple[Path, dict[str, Any]], ...],
) -> tuple[str, ...]:
    """Resolve the root markers needed before project layers can be found.

    https://learn.chatgpt.com/docs/config-file/config-advanced#project-root-detection
    """
    markers = (".git",)
    source: Path | None = None
    for path, config in base_layers:
        if "project_root_markers" not in config:
            continue
        raw = config["project_root_markers"]
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise SandboxUnsupportedError(
                "Codex sandbox requires project_root_markers to be a list of "
                f"strings in {path}"
            )
        markers = tuple(raw)
        source = path
    if markers and ".git" not in markers:
        raise SandboxUnsupportedError(
            "Codex sandbox cannot validate project config discovery when "
            f"project_root_markers in {source} omits '.git'; include '.git' or "
            "remove the custom setting before using agent.sandbox"
        )
    return markers


def _project_config_files(
    working_directory: Path,
    *,
    root_markers: tuple[str, ...],
) -> tuple[Path, ...]:
    """Return project config layers from the detected root through pinned CWD."""
    resolved_cwd = working_directory.resolve()
    if root_markers:
        project_root = next(
            (
                candidate
                for candidate in (resolved_cwd, *resolved_cwd.parents)
                if any((candidate / marker).exists() for marker in root_markers)
            ),
            resolved_cwd,
        )
    else:
        project_root = resolved_cwd
    directories = [project_root]
    current = project_root
    for part in resolved_cwd.relative_to(project_root).parts:
        current /= part
        directories.append(current)
    return tuple(
        path
        for directory in directories
        if (path := directory / ".codex" / "config.toml").is_file()
    )


def _loaded_config_layers(
    working_directory: Path,
    *,
    codex_home: Path | None = None,
) -> tuple[tuple[Path, dict[str, Any]], ...]:
    """Load every documented config layer that can disable profiles.

    Codex documents the complete order under Config basics > Configuration
    precedence: CLI overrides, project files from the project root to CWD,
    an explicitly selected profile, user config, and system config. The
    issue-orchestrator adapter owns its CLI overrides and never emits
    ``--profile``; the stable environment surface has no sandbox-setting
    variable, while ``CODEX_HOME`` only relocates the user layer.

    https://learn.chatgpt.com/docs/config-file/config-basic#configuration-precedence
    """
    base_layers = _base_config_layers(codex_home=codex_home)
    root_markers = _project_root_markers(base_layers)
    project_layers = tuple(
        (path, _read_codex_config(path))
        for path in _project_config_files(
            working_directory,
            root_markers=root_markers,
        )
    )
    return (*base_layers, *project_layers)


def validate_codex_permission_profile_compatibility(
    working_directory: Path,
    *,
    codex_home: Path | None = None,
) -> None:
    """Fail if a loaded legacy setting would silently disable the profile.

    Codex documents that the presence of ``sandbox_mode`` in *any* loaded
    config layer selects its legacy sandbox and disables permission profiles,
    even when ``default_permissions`` is supplied at higher precedence. There
    is no invocation-level unset. Detect every documented layer before launch
    so ``sandbox: true`` cannot silently degrade.
    """
    for path, config in _loaded_config_layers(
        working_directory,
        codex_home=codex_home,
    ):
        if "sandbox_mode" in config:
            raise SandboxUnsupportedError(
                "Codex sandbox permission profiles are disabled by legacy "
                f"sandbox_mode in {path}; remove that key before using agent.sandbox"
            )
