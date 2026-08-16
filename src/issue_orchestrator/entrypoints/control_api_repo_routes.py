"""Control Center repo registry and status routes."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ..domain.repository_launch_selection import RepositoryLaunchSelection
from sse_starlette.sse import EventSourceResponse

from ..execution.control_center_repo_status import build_repos_status
from ..execution.control_center_actions import SelectLaunchConfigurationRequest
from ..execution.control_center_runtime import (
    build_repo_identity,
    get_selected_launch_selection,
)
from ..infra.repo_identity import deserialize_repo_identity
from .control_api_repo_support import ControlApiRepoDependency

logger = logging.getLogger(__name__)

control_repo_router = APIRouter()


def _repo_status_snapshot(deps: ControlApiRepoDependency) -> list[dict[str, Any]]:
    """Build a current repo-status snapshot from the configured dependencies."""
    return build_repos_status(
        supervisor=deps.get_supervisor(),
        preferred_repo_root=deps.get_preferred_repo_root(),
    )


@control_repo_router.get("/control/repos")
async def list_repos_endpoint(deps: ControlApiRepoDependency) -> JSONResponse:
    """List all registered repositories with their status."""
    return JSONResponse({"repos": _repo_status_snapshot(deps)})


@control_repo_router.get("/control/info")
async def control_info(deps: ControlApiRepoDependency) -> JSONResponse:
    """Get Control Center build and identity info."""
    repo_root = Path.cwd()
    identity = build_repo_identity(repo_root)
    preferred_root = deps.get_preferred_repo_root()
    expected_identity_raw = deps.get_expected_engine_identity_raw()
    expected_identity = None
    if expected_identity_raw:
        try:
            expected_identity = deserialize_repo_identity(
                expected_identity_raw
            ).to_dict()
        except Exception:
            expected_identity = None
    return JSONResponse(
        {
            "repo_root": str(repo_root),
            "preferred_repo_root": str(preferred_root) if preferred_root else None,
            "commit_sha": identity.commit_sha,
            "commit_short": identity.commit_sha[:7] if identity.commit_sha else None,
            "repo_identity": identity.to_dict(),
            "expected_engine_identity": expected_identity,
        }
    )


@control_repo_router.get("/control/events")
async def control_events(
    request: Request,
    deps: ControlApiRepoDependency,
):
    """Stream Control Center repo status updates via SSE."""
    logger.info("[Control SSE] Client connected")

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    logger.info("[Control SSE] Client disconnected")
                    break

                yield {
                    "event": "status",
                    "data": json.dumps({"repos": _repo_status_snapshot(deps)}),
                }
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            logger.info("[Control SSE] Stream cancelled")
            raise

    return EventSourceResponse(event_generator())


@control_repo_router.post("/control/repos")
async def add_repo_endpoint(
    request: Request,
    deps: ControlApiRepoDependency,
) -> JSONResponse:
    """Add a repository to the registry."""
    from ..infra import repo_registry

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    try:
        repo = repo_registry.add_repo(repo_root, name=body.get("name"))
        return JSONResponse({"status": "added", "repo": repo.to_dict()})
    except ValueError as exc:
        return JSONResponse(
            {"error": "already_registered", "detail": str(exc)},
            status_code=409,
        )


@control_repo_router.delete("/control/repos")
async def remove_repo_endpoint(
    request: Request,
    deps: ControlApiRepoDependency,
) -> JSONResponse:
    """Remove a repository from the registry."""
    from ..infra.repo_registry import remove_repo

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = body.get("repo_root")
    if not repo_root:
        return JSONResponse({"error": "Missing repo_root"}, status_code=400)

    try:
        normalized = str(Path(repo_root).resolve())
    except (ValueError, OSError):
        return JSONResponse({"error": "Invalid repo_root path"}, status_code=400)

    if body.get("stop_orchestrator", True):
        path = Path(normalized)
        if path.exists():
            deps.get_supervisor().stop(
                path,
                reason="repo removed via /control/repos/remove",
                actor="control-center.repos.remove",
            )

    if remove_repo(normalized):
        return JSONResponse({"status": "removed", "repo_root": normalized})
    return JSONResponse(
        {"error": "not_found", "repo_root": normalized},
        status_code=404,
    )


@control_repo_router.post("/control/repos/select-config")
async def select_config_endpoint(
    request: Request,
    deps: ControlApiRepoDependency,
) -> JSONResponse:
    """Persist the selected config for a repository."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    if not body.get("config_name"):
        return JSONResponse({"error": "Missing config_name"}, status_code=400)
    desired = get_selected_launch_selection(repo_root)
    try:
        selection = RepositoryLaunchSelection.parse(
            mode=body.get("mode") or desired.mode,
            config_name=body.get("config_name"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    result = await deps.get_control_actions().select_launch_config_cmd.execute(
        SelectLaunchConfigurationRequest(
            repo_root=repo_root,
            selection=selection,
        )
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@control_repo_router.post("/control/repos/validate")
async def validate_repo_config(
    request: Request,
    deps: ControlApiRepoDependency,
) -> JSONResponse:
    """Validate a repository configuration without starting an engine."""
    from ..infra.config import CONFIG_DIR, Config, get_config_path, list_configs

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    try:
        selection = RepositoryLaunchSelection.parse(
            mode=body.get("mode"),
            config_name=body.get("config_name"),
        )
    except ValueError as exc:
        return JSONResponse({"valid": False, "errors": [str(exc)]}, status_code=400)
    config_name = selection.config.value

    available = list_configs(repo_root, selection.mode)
    if not available:
        return JSONResponse(
            {
                "valid": False,
                "has_config": False,
                "config_path": None,
                "errors": [f"No configs found in {CONFIG_DIR}/"],
                "warnings": [],
            }
        )

    config_path = get_config_path(repo_root, config_name, selection.mode)
    if not config_path.exists():
        return JSONResponse(
            {
                "valid": False,
                "has_config": True,
                "config_path": None,
                "available_configs": available,
                "errors": [
                    f"Config '{config_name}' not found. Available: {', '.join(available)}"
                ],
                "warnings": [],
            }
        )

    try:
        config = Config.load(config_path)
        errors = config.validate()
        warnings: list[str] = []
        if not config.code_review_agent:
            warnings.append(
                "No code review agent configured - PRs won't be auto-reviewed"
            )
        if not config.tech_lead_enabled:
            warnings.append("Tech lead is disabled - no tech-lead runs will be started")

        return JSONResponse(
            {
                "valid": len(errors) == 0,
                "has_config": True,
                "config_path": str(config_path),
                "mode": selection.mode.value,
                "errors": errors,
                "warnings": warnings,
                "config_summary": {
                    "repo": config.repo,
                    "agents": list(config.agents.keys()),
                    "ui_mode": config.ui_mode,
                    "review_enabled": config.review_enabled,
                },
            }
        )
    except Exception as exc:
        return JSONResponse(
            {
                "valid": False,
                "has_config": True,
                "config_path": str(config_path),
                "errors": [f"Failed to load config: {exc}"],
                "warnings": [],
            }
        )


@control_repo_router.post("/control/repos/doctor")
async def doctor_repo(
    request: Request,
    deps: ControlApiRepoDependency,
) -> JSONResponse:
    """Run doctor checks and persist repo health."""
    from ..infra.repo_registry import update_repo_health

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    desired = get_selected_launch_selection(repo_root)
    try:
        selection = RepositoryLaunchSelection.parse(
            mode=body.get("mode") or desired.mode,
            config_name=body.get("config_name") or desired.config,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        health = update_repo_health(
            repo_root,
            config_name=selection.config.value,
            mode=selection.mode.value,
        )
    except Exception as exc:
        logger.exception("Doctor check failed for %s", repo_root)
        return JSONResponse(
            {
                "status": "error",
                "checked_at": "",
                "errors": [f"Doctor check failed: {exc}"],
                "warnings": [],
                "can_start": False,
            },
            status_code=500,
        )

    return JSONResponse(
        {
            "status": health.status,
            "checked_at": health.checked_at,
            "errors": health.errors,
            "warnings": health.warnings,
            "can_start": health.status == "valid",
        }
    )


@control_repo_router.get("/control/repos/config")
async def get_repo_config(
    deps: ControlApiRepoDependency,
    repo_root: str = Query(..., description="Repository root path"),
    config_name: str = Query(default="default.yaml", description="Config file name"),
    mode: str = Query(default="default", description="Configuration mode"),
) -> JSONResponse:
    """Get the YAML contents of a repo config file."""
    from ..infra.config import get_config_path

    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    try:
        selection = RepositoryLaunchSelection.parse(
            mode=mode,
            config_name=config_name,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    config_path = get_config_path(path, selection.config.value, selection.mode)
    if not config_path.exists():
        return JSONResponse(
            {"error": "config_not_found", **selection.to_dict()},
            status_code=404,
        )

    try:
        content = config_path.read_text()
    except Exception as exc:
        return JSONResponse(
            {"error": "read_failed", "detail": str(exc)}, status_code=500
        )

    return JSONResponse(
        {
            **selection.to_dict(),
            "config_path": str(config_path),
            "content": content,
        }
    )


@control_repo_router.get("/control/repos/discover")
async def discover_repos_endpoint(
    search_paths: str = Query(
        default="",
        description="Comma-separated paths to search (default: ~/dev, ~/projects, ~/code, ~/repos)",
    ),
    max_depth: int = Query(default=3, description="Max directory depth to search"),
) -> JSONResponse:
    """Discover unregistered git repositories and classify setup readiness."""
    from ..observation.instance_detector import discover_repos

    paths_to_search = None
    if search_paths:
        paths_to_search = [
            Path(path.strip()).expanduser()
            for path in search_paths.split(",")
            if path.strip()
        ]

    discovered = discover_repos(search_paths=paths_to_search, max_depth=max_depth)
    return JSONResponse({"discovered": discovered})


__all__ = ["control_repo_router"]
