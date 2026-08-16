"""Dependency wiring for Control Center repo-status route modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Callable

from fastapi import Depends, FastAPI, Request

_REPO_DEPENDENCIES_STATE_KEY = "control_api_repo_dependencies"

if TYPE_CHECKING:
    from ..execution.control_center_actions import ControlCenterActions
    from ..ports.repository_engine_supervisor import SupervisorOps


@dataclass(frozen=True)
class ControlApiRepoDependencies:
    """Dependency hooks needed by Control Center repo-status routes."""

    get_supervisor: Callable[[], SupervisorOps]
    get_control_actions: Callable[[], ControlCenterActions]
    validate_repo_root: Callable[[str | None], Path | None]
    get_preferred_repo_root: Callable[[], Path | None]
    get_expected_engine_identity_raw: Callable[[], str | None]


def install_control_api_repo_dependencies(
    app: FastAPI,
    deps: ControlApiRepoDependencies,
) -> None:
    """Install shared dependencies for Control Center repo-status routes."""
    setattr(app.state, _REPO_DEPENDENCIES_STATE_KEY, deps)


def get_control_api_repo_dependencies(request: Request) -> ControlApiRepoDependencies:
    """Resolve repo-route dependencies from the FastAPI application state."""
    deps = getattr(request.app.state, _REPO_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control Center repo dependencies not configured")
    return deps


ControlApiRepoDependency = Annotated[
    ControlApiRepoDependencies,
    Depends(get_control_api_repo_dependencies),
]


__all__ = [
    "ControlApiRepoDependency",
    "ControlApiRepoDependencies",
    "get_control_api_repo_dependencies",
    "install_control_api_repo_dependencies",
]
