"""Control Center setup-wizard routes."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import (
    RepositorySetupCommandPayload,
    RepositorySetupConflictPayload,
    RepositorySetupDetectionPayload,
    RepositorySetupFailurePayload,
    RepositorySetupFilePayload,
    RepositorySetupGitHubAuthorizationPayload,
    RepositorySetupGitHubTokenPayload,
    RepositorySetupGitHubVerificationPayload,
    RepositorySetupGitHubVerifyRequestPayload,
    RepositorySetupPreviewPayload,
    RepositorySetupPrerequisitesPayload,
    RepositorySetupResultPayload,
)
from ..control.repository_setup import (
    RepositorySetupCommand,
    RepositorySetupConflictError,
    RepositorySetupExecutionError,
    RepositorySetupRequest,
)
from ..domain.repository_setup_auth import RepositorySetupGitHubAuthorization
from ..domain.repository_config_name import RepositoryConfigName
from ..domain.worktree_paths import (
    default_worktree_base_config,
    resolve_worktree_base,
)
from ..ports.repository_setup import (
    RepositorySetupGitHubVerification,
    RepositorySetupPlannedFile,
)
from .control_api_setup_support import ControlApiSetupDependency
from .setup_wizard_common import (
    build_agent_checks,
    build_any_ai_provider_check,
    detect_repo,
    find_existing_default_config,
    find_prompt_candidates,
    load_config_for_repo,
    run_git,
)

control_setup_router = APIRouter()

_REQUIRED_GITHUB_PERMISSIONS = (
    "Contents: read and write",
    "Issues: read and write",
    "Pull requests: read and write",
    "Metadata: read",
)


def _authorization_from_payload(
    payload: RepositorySetupGitHubAuthorizationPayload,
    deps: ControlApiSetupDependency,
) -> RepositorySetupGitHubAuthorization:
    return deps.setup_owner.authorization_from_public(
        payload.model_dump(exclude_none=True),
    )


def _authorization_payload(
    authorization: RepositorySetupGitHubAuthorization,
    deps: ControlApiSetupDependency,
    *,
    redact_inline_token: bool = False,
) -> RepositorySetupGitHubAuthorizationPayload:
    return RepositorySetupGitHubAuthorizationPayload.model_validate(
        deps.setup_owner.authorization_to_public(
            authorization,
            redact_inline_token=redact_inline_token,
        )
    )


def _verification_payload(
    verification: RepositorySetupGitHubVerification,
    deps: ControlApiSetupDependency,
) -> RepositorySetupGitHubVerificationPayload:
    app_auth = verification.auth_kind == "github_app"
    permissions: list[str] = list(_REQUIRED_GITHUB_PERMISSIONS)
    if app_auth:
        permissions.extend(("Checks: read", "Commit statuses: read"))
    authorship_notice = (
        "GitHub API operations and authenticated branch pushes use the GitHub "
        "App bot; pull requests are created by the bot, so the operator remains "
        "eligible to approve them."
        if app_auth
        else "GitHub API operations and pull-request creation use this user. "
        "Branch pushes continue to use the repository's configured git "
        "transport. GitHub does not allow a pull-request author to approve "
        "their own pull request."
    )
    return RepositorySetupGitHubVerificationPayload(
        verified=True,
        identity=verification.identity,
        repository=verification.repository,
        auth_kind=verification.auth_kind,
        source=verification.source,
        authorship_notice=authorship_notice,
        verification_note=(
            "Setup verified the identity and repository access without making "
            "GitHub writes. Confirm the listed write permissions in GitHub; "
            "they are exercised when the orchestrator performs each operation."
        ),
        required_permissions=permissions,
        authorization=_authorization_payload(
            verification.normalized_authorization,
            deps,
        ),
    )


def repository_setup_request_from_payload(
    payload: RepositorySetupCommandPayload,
    deps: ControlApiSetupDependency,
) -> RepositorySetupRequest:
    """Translate the HTTP command contract into setup-owner policy."""
    repo_root = deps.validate_repo_root(payload.repo_root)
    if repo_root is None:
        raise ValueError("Invalid or missing repo_root")
    command = RepositorySetupCommand(
        repo_root=repo_root,
        repo_name=payload.repo_name,
        worker_agent_label=payload.worker_agent_label,
        model=payload.model,
        effort=payload.effort,
        configure_reviewer=payload.configure_reviewer,
        reviewer_model=payload.reviewer_model,
        reviewer_effort=payload.reviewer_effort,
        configure_internal_reviewer=payload.configure_internal_reviewer,
        internal_review_max_rounds=payload.internal_review_max_rounds,
        internal_review_instructions=payload.internal_review_instructions,
        validation_quick_command=payload.validation_quick_command,
        validation_publish_command=payload.validation_publish_command,
        worktree_base=payload.worktree_base,
        github_authorization=_authorization_from_payload(
            payload.github_authorization,
            deps,
        ),
        configure_tech_lead=payload.configure_tech_lead,
        tech_lead_model=payload.tech_lead_model,
        tech_lead_effort=payload.tech_lead_effort,
        tech_lead_review_threshold=payload.tech_lead_review_threshold,
        config_name=RepositoryConfigName.parse(
            payload.config_name,
            default="default.yaml",
        ),
        create_prompts=payload.create_prompts is not False,
        create_labels=payload.create_labels is not False,
        replace_existing=payload.replace_existing is True,
    )
    return deps.setup_owner.request_from_command(command)


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


@control_setup_router.get(
    "/control/setup/prereqs",
    response_model=RepositorySetupPrerequisitesPayload,
)
async def setup_prereqs(
    deps: ControlApiSetupDependency,
    repo_root: str | None = Query(default=None),
) -> RepositorySetupPrerequisitesPayload:
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
    agent_checks = build_agent_checks(config)
    for agent_check in agent_checks:
        checks[f"agent:{agent_check.get('name', 'Agent CLI')}"] = agent_check
    all_ok = all(c.get("ok", False) for c in checks.values()) and all(
        c.get("ok", False) for c in agent_checks
    )

    return RepositorySetupPrerequisitesPayload.model_validate(
        {"all_ok": all_ok, "checks": checks, "agent_checks": agent_checks}
    )


@control_setup_router.get(
    "/control/setup/detect",
    response_model=RepositorySetupDetectionPayload,
)
async def setup_detect(
    deps: ControlApiSetupDependency,
    repo_root: str = Query(...),
) -> RepositorySetupDetectionPayload | JSONResponse:
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
        "worktree_base_default": default_worktree_base_config(path),
        "github_authorization": {
            "authorization": _authorization_payload(
                RepositorySetupGitHubAuthorization(kind="detected"),
                deps,
            ).model_dump(exclude_none=True),
            "configured_kind": "detected",
            "inline_token_migration_required": False,
        },
    }

    result["repo"] = detect_repo(cwd=path)

    config_path, existing_config = find_existing_default_config(path)
    if config_path is not None:
        result["config_path"] = str(config_path)
    if existing_config is not None:
        browser_config = deepcopy(existing_config)
        browser_repo = browser_config.get("repo")
        browser_github = (
            browser_repo.get("github") if isinstance(browser_repo, dict) else None
        )
        if isinstance(browser_github, dict):
            browser_github.pop("token", None)
        result["existing_config"] = browser_config

        try:
            existing_authorization = deps.setup_owner.authorization_from_config(
                existing_config
            )
            inline_token = existing_authorization.contains_inline_token
            result["github_authorization"] = {
                "authorization": _authorization_payload(
                    existing_authorization,
                    deps,
                    redact_inline_token=inline_token,
                ).model_dump(exclude_none=True),
                "configured_kind": existing_authorization.kind,
                "inline_token_migration_required": inline_token,
            }
        except (TypeError, ValueError) as exc:
            result["github_authorization"] = {
                "authorization": _authorization_payload(
                    RepositorySetupGitHubAuthorization(kind="detected"),
                    deps,
                ).model_dump(exclude_none=True),
                "configured_kind": "invalid",
                "inline_token_migration_required": False,
                "configuration_error": str(exc),
            }

    existing_worktrees = (
        existing_config.get("worktrees") if isinstance(existing_config, dict) else None
    )
    configured_worktree_base = (
        existing_worktrees.get("base") if isinstance(existing_worktrees, dict) else None
    )
    result["worktree_base_resolved"] = str(
        resolve_worktree_base(configured_worktree_base, path)
    )
    validation_defaults = deps.validation_detector(path)
    result["validation_defaults"] = {
        "quick_command": validation_defaults.quick_command,
        "publish_command": validation_defaults.publish_command,
        "source": validation_defaults.source,
    }

    prompt_candidates = []
    for candidate in find_prompt_candidates(path):
        try:
            prompt_candidates.append(str(candidate.relative_to(path)))
        except ValueError:
            prompt_candidates.append(str(candidate))
    result["prompt_candidates"] = prompt_candidates[:20]

    detection = RepositorySetupDetectionPayload.model_validate(result)
    response = detection.model_dump()
    response["github_authorization"] = detection.github_authorization.model_dump(
        exclude_none=True
    )
    response["github_authorization"]["authorization"] = (
        detection.github_authorization.authorization.model_dump(exclude_none=True)
    )
    return JSONResponse(response)


@control_setup_router.post(
    "/control/setup/github-auth/verify",
    response_model=RepositorySetupGitHubVerificationPayload,
    response_model_exclude_none=True,
)
async def setup_verify_github_authorization(
    payload: RepositorySetupGitHubVerifyRequestPayload,
    deps: ControlApiSetupDependency,
) -> RepositorySetupGitHubVerificationPayload:
    """Verify the selected GitHub identity without writing config or labels."""
    if deps.validate_repo_root(payload.repo_root) is None:
        raise HTTPException(status_code=400, detail="Invalid or missing repo_root")
    try:
        verification = deps.setup_owner.verify_github_authorization(
            payload.repo_name,
            _authorization_from_payload(payload.authorization, deps),
        )
        return _verification_payload(verification, deps)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@control_setup_router.post(
    "/control/setup/github-auth/store-personal-token",
    response_model=RepositorySetupGitHubVerificationPayload,
    response_model_exclude_none=True,
)
async def setup_store_personal_token(
    payload: RepositorySetupGitHubTokenPayload,
    deps: ControlApiSetupDependency,
) -> RepositorySetupGitHubVerificationPayload:
    """Verify a personal token, store it in Keychain, then verify the reference."""
    if deps.validate_repo_root(payload.repo_root) is None:
        raise HTTPException(status_code=400, detail="Invalid or missing repo_root")
    try:
        inline = deps.setup_owner.authorization_from_public(
            {
                "kind": "personal",
                "token": payload.token,
                "api_url": payload.api_url,
                "http_timeout_seconds": payload.http_timeout_seconds,
            }
        )
        deps.setup_owner.verify_github_authorization(payload.repo_name, inline)
        stored = deps.github_token_store(inline, repo=payload.repo_name)
        verification = deps.setup_owner.verify_github_authorization(
            payload.repo_name,
            stored,
        )
        return _verification_payload(verification, deps)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        worktree_base=str(preview.worktree_base),
        github_authorization=_verification_payload(
            preview.github_authorization,
            deps,
        ),
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
