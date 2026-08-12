"""Shared dependency wiring for Control Center E2E routers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

_E2E_DEPENDENCIES_STATE_KEY = "control_api_e2e_dependencies"

if TYPE_CHECKING:
    from ..domain.repository_launch_selection import RepositoryLaunchSelection
    from ..infra.config import Config
    from ..infra.orchestrator import Orchestrator

class InvalidE2ESelectionError(ValueError):
    """An E2E request supplied an invalid mode/config selection."""


@dataclass(frozen=True)
class ControlApiE2EDependencies:
    """Dependency hooks needed by Control Center E2E route modules."""

    get_orchestrator: Callable[[], Orchestrator | None]
    load_config_selection: Callable[[Path, RepositoryLaunchSelection], Config]
    validate_repo_root: Callable[[str | None], Path | None]

    def load_config(self, repo_root: Path, config_name: str, mode: str) -> Config:
        """Resolve untrusted E2E request fields into one typed selection."""
        from ..domain.repository_launch_selection import RepositoryLaunchSelection

        try:
            selection = RepositoryLaunchSelection.parse(
                mode=mode,
                config_name=config_name,
            )
        except ValueError as exc:
            raise InvalidE2ESelectionError(str(exc)) from exc
        return self.load_config_selection(repo_root, selection)


async def _invalid_e2e_selection_handler(
    _request: Request,
    exc: Exception,
) -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_configuration_selection", "detail": str(exc)},
        status_code=400,
    )


def install_control_api_e2e_dependencies(
    app: FastAPI,
    deps: ControlApiE2EDependencies,
) -> None:
    """Install shared dependencies for Control Center E2E routers."""
    setattr(app.state, _E2E_DEPENDENCIES_STATE_KEY, deps)
    app.add_exception_handler(
        InvalidE2ESelectionError,
        _invalid_e2e_selection_handler,
    )


def get_control_api_e2e_dependencies(request: Request) -> ControlApiE2EDependencies:
    """Resolve router dependencies from the FastAPI application state."""
    deps = getattr(request.app.state, _E2E_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control Center E2E dependencies not configured")
    return deps


ControlApiE2EDependency = Annotated[
    ControlApiE2EDependencies,
    Depends(get_control_api_e2e_dependencies),
]


__all__ = [
    "ControlApiE2EDependency",
    "ControlApiE2EDependencies",
    "InvalidE2ESelectionError",
    "get_control_api_e2e_dependencies",
    "install_control_api_e2e_dependencies",
]
