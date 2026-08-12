"""Dependency wiring for Control Center shutdown routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Callable

from fastapi import Depends, FastAPI, Request

_SHUTDOWN_DEPENDENCIES_STATE_KEY = "control_api_shutdown_dependencies"

if TYPE_CHECKING:
    from ..ports.repository_engine_supervisor import SupervisorOps


@dataclass(frozen=True)
class ControlApiShutdownDependencies:
    """Dependency hooks needed by Control Center shutdown routes."""

    get_supervisor: Callable[[], SupervisorOps]
    schedule_control_center_exit: Callable[[], None]


def install_control_api_shutdown_dependencies(
    app: FastAPI,
    deps: ControlApiShutdownDependencies,
) -> None:
    """Install shared dependencies for Control Center shutdown routes."""
    setattr(app.state, _SHUTDOWN_DEPENDENCIES_STATE_KEY, deps)


def get_control_api_shutdown_dependencies(request: Request) -> ControlApiShutdownDependencies:
    """Resolve shutdown route dependencies from the FastAPI application state."""
    deps = getattr(request.app.state, _SHUTDOWN_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control Center shutdown dependencies not configured")
    return deps


ControlApiShutdownDependency = Annotated[
    ControlApiShutdownDependencies,
    Depends(get_control_api_shutdown_dependencies),
]


__all__ = [
    "ControlApiShutdownDependencies",
    "ControlApiShutdownDependency",
    "get_control_api_shutdown_dependencies",
    "install_control_api_shutdown_dependencies",
]
