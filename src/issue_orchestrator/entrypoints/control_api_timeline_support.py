"""Dependency wiring for standalone Control Center Timeline routes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Callable

from fastapi import Depends, FastAPI, Request

if TYPE_CHECKING:
    from ..execution.control_center_timeline_evidence import (
        ControlCenterTimelineEvidenceAccess,
    )

_TIMELINE_DEPENDENCIES_STATE_KEY = "control_api_timeline_dependencies"


@dataclass(frozen=True)
class ControlApiTimelineDependencies:
    """Behavior owners needed by Control Center Timeline routes."""

    timeline_evidence: ControlCenterTimelineEvidenceAccess
    validate_repo_root: Callable[[str | None], Path | None]


def install_control_api_timeline_dependencies(
    app: FastAPI,
    deps: ControlApiTimelineDependencies,
) -> None:
    setattr(app.state, _TIMELINE_DEPENDENCIES_STATE_KEY, deps)


def get_control_api_timeline_dependencies(
    request: Request,
) -> ControlApiTimelineDependencies:
    deps = getattr(request.app.state, _TIMELINE_DEPENDENCIES_STATE_KEY, None)
    if deps is None:
        raise RuntimeError("Control Center Timeline dependencies not configured")
    return deps


ControlApiTimelineDependency = Annotated[
    ControlApiTimelineDependencies,
    Depends(get_control_api_timeline_dependencies),
]


__all__ = [
    "ControlApiTimelineDependencies",
    "ControlApiTimelineDependency",
    "install_control_api_timeline_dependencies",
]
