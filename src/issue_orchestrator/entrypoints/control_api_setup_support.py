"""Dependency wiring for Control Center setup-wizard route modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Callable

from fastapi import Depends, FastAPI, Request

from ..control.repository_setup import RepositorySetupOwner
from ..ports.repository_setup import (
    RepositorySetupGitHubTokenStore,
    RepositorySetupValidationDetector,
)

_SETUP_DEPENDENCIES_STATE_KEY = "control_api_setup_dependencies"


@dataclass(frozen=True)
class ControlApiSetupDependencies:
    """Dependency hooks needed by Control Center setup-wizard routes."""

    validate_repo_root: Callable[[str | None], Path | None]
    setup_owner: RepositorySetupOwner
    github_token_store: RepositorySetupGitHubTokenStore
    validation_detector: RepositorySetupValidationDetector


def install_control_api_setup_dependencies(
    app: FastAPI,
    deps: ControlApiSetupDependencies,
) -> None:
    """Install shared dependencies for Control Center setup-wizard routes."""
    setattr(app.state, _SETUP_DEPENDENCIES_STATE_KEY, deps)


def get_control_api_setup_dependencies(request: Request) -> ControlApiSetupDependencies:
    """Resolve setup-route dependencies from FastAPI application state."""
    deps = getattr(request.app.state, _SETUP_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control Center setup dependencies not configured")
    return deps


ControlApiSetupDependency = Annotated[
    ControlApiSetupDependencies,
    Depends(get_control_api_setup_dependencies),
]


__all__ = [
    "ControlApiSetupDependency",
    "ControlApiSetupDependencies",
    "get_control_api_setup_dependencies",
    "install_control_api_setup_dependencies",
]
