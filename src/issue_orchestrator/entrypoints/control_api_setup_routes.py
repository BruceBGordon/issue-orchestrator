"""Control Center setup-wizard routes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import (
    RepositorySetupCommandPayload,
    RepositorySetupFilePayload,
    RepositorySetupPreviewPayload,
    RepositorySetupResultPayload,
)
from ..control.repository_setup import RepositorySetupCommand
from .control_api_setup_support import ControlApiSetupDependency
from .setup_wizard_common import (
    FileCollector,
    build_agent_checks,
    build_any_ai_provider_check,
    build_github_auth_check,
    detect_repo,
    find_existing_default_config,
    fetch_github_labels,
    find_prompt_candidates,
    load_config_for_repo,
    plan_setup_labels,
    render_config_yaml,
    run_git,
    write_config,
    write_missing_setup_prompts,
)

logger = logging.getLogger(__name__)

control_setup_router = APIRouter()


def _repository_setup_command(
    payload: RepositorySetupCommandPayload,
) -> RepositorySetupCommand:
    """Translate the HTTP command contract into setup-owner policy."""
    return RepositorySetupCommand(
        repo_name=payload.repo_name,
        worker_agent_label=payload.worker_agent_label,
        model=payload.model,
        configure_tech_lead=payload.configure_tech_lead,
    )


def _preview_prompt_files(
    config: Mapping[str, Any],
    repo_root: str | None,
) -> list[RepositorySetupFilePayload]:
    """Build prompt-file preview rows without mutating the filesystem."""
    if repo_root:
        collector = FileCollector()
        write_missing_setup_prompts(config, Path(repo_root), file_collector=collector)
        prompt_rows: list[RepositorySetupFilePayload] = []
        for write in collector.writes:
            if write.kind != "prompt":
                continue
            prompt_rows.append(
                RepositorySetupFilePayload(
                    path=str(write.path),
                    action=write.action,
                    type="prompt",
                    agent=write.agent,
                )
            )
        return prompt_rows

    prompt_rows = []
    for agent_name, agent_config in (config.get("agents", {}) or {}).items():
        if not isinstance(agent_config, Mapping):
            continue
        prompt_path = agent_config.get("prompt", "")
        if not isinstance(prompt_path, str) or not prompt_path:
            continue
        prompt_rows.append(
            RepositorySetupFilePayload(
                path=prompt_path,
                action="create",
                type="prompt",
                agent=agent_name,
            )
        )
    return prompt_rows


def _normalize_config_name(raw: object) -> str:
    """Normalize a setup-wizard config file name."""
    config_name = str(raw or "default.yaml")
    if not config_name.endswith(".yaml"):
        config_name += ".yaml"
    return config_name


def _persist_setup_config(
    repo_root: Path,
    config: Mapping[str, Any],
    config_name: str,
) -> Path:
    """Write the config file and return its path."""
    from ..infra.config import get_config_dir, get_config_path

    config_dir = get_config_dir(repo_root)
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = get_config_path(repo_root, config_name)
    write_config(config, config_path, include_header=False)
    return config_path


def _create_setup_prompts(
    repo_root: Path,
    config: Mapping[str, Any],
) -> list[str]:
    """Create any missing setup-wizard prompt files."""
    try:
        return [str(path) for path in write_missing_setup_prompts(config, repo_root)]
    except Exception as exc:
        logger.warning("Failed to create setup prompts under %s: %s", repo_root, exc)
        return []


def _create_setup_labels(repo_name: str, config: Mapping[str, Any]) -> list[str]:
    """Create any missing GitHub labels for the setup config."""
    try:
        from ..execution.providers import create_repository_host

        host = create_repository_host(repo=repo_name)
        existing = {
            name
            for label in host.list_labels()
            if isinstance(label, dict)
            and isinstance((name := label.get("name")), str)
        }
    except Exception as exc:
        logger.warning("Failed to load setup labels for %s: %s", repo_name, exc)
        return []

    created_labels: list[str] = []
    for name, color, desc in plan_setup_labels(
        config,
        include_priority_labels=False,
        include_review_labels_without_default=True,
    ):
        if name in existing:
            continue
        try:
            host.create_label(name, color=color, description=desc, force=True)
            created_labels.append(name)
        except Exception as exc:
            logger.warning("Failed to create label %s: %s", name, exc)
    return created_labels


@control_setup_router.get("/control/setup/prereqs")
async def setup_prereqs(
    deps: ControlApiSetupDependency,
    repo_root: str | None = Query(default=None),
) -> JSONResponse:
    """Check setup prerequisites for a repository."""
    validated_root = deps.validate_repo_root(repo_root) if repo_root else None
    config = load_config_for_repo(validated_root)

    checks: dict[str, dict[str, Any]] = {}
    ok, output = run_git(["--version"], timeout_s=5)
    checks["git"] = {
        "ok": ok,
        "detail": output if ok else "Not found",
    }

    checks["ai_provider_clis"] = build_any_ai_provider_check()
    checks["github_auth"] = build_github_auth_check(config)
    agent_checks = build_agent_checks(config)
    for agent_check in agent_checks:
        checks[f"agent:{agent_check.get('name', 'Agent CLI')}"] = agent_check
    all_ok = all(c.get("ok", False) for c in checks.values()) and all(
        c.get("ok", False) for c in agent_checks
    )

    return JSONResponse({
        "all_ok": all_ok,
        "checks": checks,
        "agent_checks": agent_checks,
    })


@control_setup_router.get("/control/setup/detect")
async def setup_detect(
    deps: ControlApiSetupDependency,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Detect repository state for the Control Center setup wizard."""
    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    result: dict[str, Any] = {
        "repo_root": str(path),
        "repo": None,
        "existing_config": None,
        "config_path": None,
        "github_labels": [],
        "agent_labels": [],
        "prompt_candidates": [],
    }

    result["repo"] = detect_repo(cwd=path)

    config_path, existing_config = find_existing_default_config(path)
    if config_path is not None:
        result["config_path"] = str(config_path)
    if existing_config is not None:
        result["existing_config"] = existing_config

    if result["repo"]:
        labels = fetch_github_labels(result["repo"])
        result["github_labels"] = labels
        result["agent_labels"] = [label for label in labels if label.startswith("agent:")]

    prompt_candidates = []
    for candidate in find_prompt_candidates(path):
        try:
            prompt_candidates.append(str(candidate.relative_to(path)))
        except ValueError:
            prompt_candidates.append(str(candidate))
    result["prompt_candidates"] = prompt_candidates[:20]

    return JSONResponse(result)


@control_setup_router.post(
    "/control/setup/preview",
    response_model=RepositorySetupPreviewPayload,
)
async def setup_preview(
    payload: RepositorySetupCommandPayload,
    deps: ControlApiSetupDependency,
) -> RepositorySetupPreviewPayload | JSONResponse:
    """Generate a setup-wizard config preview without saving."""
    try:
        config = _repository_setup_command(payload).build_config()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    repo_root = deps.validate_repo_root(payload.repo_root)
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )
    from ..infra.config import get_config_path

    config_path = str(
        get_config_path(repo_root, _normalize_config_name(payload.config_name))
    )

    yaml_content = render_config_yaml(config, include_header=False)
    files_to_create = [
        RepositorySetupFilePayload(
            path=config_path,
            action="overwrite" if Path(config_path).exists() else "create",
            size=len(yaml_content),
        )
    ]
    files_to_create.extend(_preview_prompt_files(config, str(repo_root)))

    return RepositorySetupPreviewPayload(
        yaml=yaml_content,
        files=files_to_create,
    )


@control_setup_router.post(
    "/control/setup/save",
    response_model=RepositorySetupResultPayload,
)
async def setup_save(
    payload: RepositorySetupCommandPayload,
    deps: ControlApiSetupDependency,
) -> RepositorySetupResultPayload | JSONResponse:
    """Save a setup-wizard config and create requested setup artifacts."""
    try:
        config = _repository_setup_command(payload).build_config()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    repo_root = deps.validate_repo_root(payload.repo_root)
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    create_prompts = payload.create_prompts is not False
    create_labels = payload.create_labels is not False
    created_files: list[str] = []
    created_labels: list[str] = []
    config_name = _normalize_config_name(payload.config_name)

    try:
        config_path = _persist_setup_config(repo_root, config, config_name)
        created_files.append(str(config_path))
    except Exception as exc:
        return JSONResponse({
            "error": "Failed to write config",
            "detail": str(exc),
        }, status_code=500)

    if create_prompts:
        created_files.extend(_create_setup_prompts(repo_root, config))

    repo_config = config.get("repo") or {}
    repo_name = repo_config.get("name") if isinstance(repo_config, Mapping) else repo_config
    if create_labels and repo_name:
        created_labels.extend(_create_setup_labels(repo_name, config))

    return RepositorySetupResultPayload(
        status="saved",
        config_path=str(config_path),
        created_files=created_files,
        created_labels=created_labels,
    )
