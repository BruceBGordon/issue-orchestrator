"""Control Center setup-wizard routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import (
    RepositorySetupCommandPayload,
    RepositorySetupConflictPayload,
    RepositorySetupFailurePayload,
    RepositorySetupFilePayload,
    RepositorySetupPreviewPayload,
    RepositorySetupResultPayload,
)
from ..control.repository_setup import (
    RepositorySetupCommand,
    RepositorySetupConflictError,
    RepositorySetupExecutionError,
    RepositorySetupRequest,
)
from ..domain.repository_config_name import RepositoryConfigName
from ..ports.repository_setup import RepositorySetupPlannedFile
from .control_api_setup_support import ControlApiSetupDependency
from .setup_wizard_common import (
    build_agent_checks,
    build_any_ai_provider_check,
    build_github_auth_check,
    detect_repo,
    find_existing_default_config,
    fetch_github_labels,
    find_prompt_candidates,
    load_config_for_repo,
    run_git,
)

control_setup_router = APIRouter()


def repository_setup_request_from_payload(
    payload: RepositorySetupCommandPayload,
    deps: ControlApiSetupDependency,
) -> RepositorySetupRequest:
    """Translate the HTTP command contract into setup-owner policy."""
    repo_root = deps.validate_repo_root(payload.repo_root)
    if repo_root is None:
        raise ValueError("Invalid or missing repo_root")
    return RepositorySetupCommand(
        repo_root=repo_root,
        repo_name=payload.repo_name,
        worker_agent_label=payload.worker_agent_label,
        model=payload.model,
        configure_tech_lead=payload.configure_tech_lead,
        config_name=RepositoryConfigName.parse(
            payload.config_name,
            default="default.yaml",
        ),
        create_prompts=payload.create_prompts is not False,
        create_labels=payload.create_labels is not False,
        replace_existing=payload.replace_existing is True,
    ).to_request()


def _setup_file_payload(
    planned_file: RepositorySetupPlannedFile,
) -> RepositorySetupFilePayload:
    """Translate the setup-owner file plan into the HTTP contract."""
    if planned_file.kind == "prompt":
        return RepositorySetupFilePayload(
            path=str(planned_file.path),
            action=planned_file.action,
            type="prompt",
            agent=planned_file.agent,
        )
    return RepositorySetupFilePayload(
        path=str(planned_file.path),
        action=planned_file.action,
        size=len(planned_file.content),
    )


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
        preview = deps.setup_owner.preview(
            repository_setup_request_from_payload(payload, deps)
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse(
            {"error": "Failed to prepare setup preview", "detail": str(exc)},
            status_code=500,
        )

    return RepositorySetupPreviewPayload(
        yaml=preview.yaml,
        files=[_setup_file_payload(file) for file in preview.files],
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
        result = deps.setup_owner.execute(
            repository_setup_request_from_payload(payload, deps)
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RepositorySetupConflictError as exc:
        conflict_payload = RepositorySetupConflictPayload(
            error="replace_confirmation_required",
            detail=str(exc),
            config_path=str(exc.config_path),
        )
        return JSONResponse(
            conflict_payload.model_dump(mode="json"),
            status_code=409,
        )
    except RepositorySetupExecutionError as exc:
        failure_payload = RepositorySetupFailurePayload(
            error="repository_setup_failed",
            stage=exc.stage,
            detail=exc.detail,
            applied_files=[str(path) for path in exc.applied_files],
            created_labels=list(exc.created_labels),
        )
        return JSONResponse(
            failure_payload.model_dump(mode="json"),
            status_code=500,
        )

    return RepositorySetupResultPayload(
        status="saved",
        config_path=str(result.config_path),
        created_files=[str(path) for path in result.written_files],
        created_labels=list(result.created_labels),
    )
