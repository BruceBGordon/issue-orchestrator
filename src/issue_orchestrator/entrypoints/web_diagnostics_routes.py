"""Dashboard dialog and diagnostics routes."""

from __future__ import annotations

import json
import platform
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import version as package_version
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import (
    BlockedIssuesDialogPayload,
    ConfigDialogPayload,
    DebugDialogPayload,
    DoctorDialogPayload,
    InfoDialogPayload,
    PhaseDialogPayload,
    SessionDiagnosticsDialogPayload,
    ValidationFailureDialogPayload,
)
from ..control.worktree_manager import get_worktree_path
from ..execution.client_host import ClientHost
from ..execution.command_runner import LocalCommandRunner
from ..execution.recorded_session_runs import RecordedSessionRunLookup
from ..infra.doctor import run_doctor
from ..infra.repo_identity import build_repo_identity
from ..view_models.dialogs import (
    build_blocked_issues_dialog,
    build_config_dialog,
    build_debug_dialog,
    build_doctor_dialog,
    build_info_dialog,
    build_phase_dialog,
    build_session_diagnostics_dialog,
    build_validation_failure_dialog,
)
from .web_session_context import WebOrchestratorDependency
from .web_session_routes import session_manifest_response, session_phases_response

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator

web_diagnostics_router = APIRouter()

_WEB_DIAGNOSTICS_DEPENDENCIES_STATE_KEY = "web_diagnostics_dependencies"


@dataclass(frozen=True)
class WebDiagnosticsDependencies:
    """Runtime adapters needed by diagnostics routes."""

    get_client_host: Callable[[], ClientHost]


def install_web_diagnostics_dependencies(
    app: FastAPI,
    *,
    get_client_host: Callable[[], ClientHost],
) -> None:
    """Install diagnostics route dependencies on the FastAPI app."""
    setattr(
        app.state,
        _WEB_DIAGNOSTICS_DEPENDENCIES_STATE_KEY,
        WebDiagnosticsDependencies(get_client_host=get_client_host),
    )


def get_web_diagnostics_dependencies(request: Request) -> WebDiagnosticsDependencies:
    """Return diagnostics route dependencies for the current app."""
    deps = getattr(request.app.state, _WEB_DIAGNOSTICS_DEPENDENCIES_STATE_KEY, None)
    if not isinstance(deps, WebDiagnosticsDependencies):
        raise RuntimeError("Web diagnostics dependencies are not installed")
    return deps


WebDiagnosticsDependency = Annotated[
    WebDiagnosticsDependencies,
    Depends(get_web_diagnostics_dependencies),
]


def _response_json(response: JSONResponse) -> dict:
    body = response.body
    if isinstance(body, memoryview):
        body = body.tobytes()
    return json.loads(body.decode("utf-8"))


def _orchestrator_not_running() -> JSONResponse:
    return JSONResponse({"error": "Orchestrator not running"}, status_code=503)


def _info_payload(
    orchestrator: "Orchestrator",
    deps: WebDiagnosticsDependencies,
) -> dict[str, Any]:
    state = orchestrator.state
    config = orchestrator.config

    repo_identity = build_repo_identity(config.repo_root)
    commit_sha = repo_identity.commit_sha
    client_capabilities = deps.get_client_host().capabilities()

    return {
        "version": package_version("issue-orchestrator"),
        "repo": config.repo,
        "repo_root": str(config.repo_root) if config.repo_root else None,
        "configuration_mode": config.configuration_mode,
        "config_name": config.config_name,
        "config_fingerprint": config.config_fingerprint,
        "ui_mode": config.ui_mode,
        "terminal_backend": config.terminal_adapter or "subprocess",
        "client_capabilities": {
            "focus_session": (config.terminal_adapter or "subprocess") != "subprocess",
            "open_path": client_capabilities.open_path,
            "reveal_worktree": client_capabilities.reveal_worktree,
            "local_server_paths_only": client_capabilities.local_only,
            "host_platform": platform.system().lower(),
        },
        "commit_sha": commit_sha,
        "commit_short": commit_sha[:7] if commit_sha else None,
        "repo_identity": repo_identity.to_dict(),
        "max_sessions": config.max_concurrent_sessions,
        "active_sessions": len(state.active_sessions),
        "completed_today": len(state.completed_today),
        # Whether the engine has finished its initial GitHub fetch and
        # state reconcile. The Control Center polls this so it can keep
        # the per-repo "Open dashboard" button disabled until the engine
        # would render a settled view — without this, opening during
        # the ~10 s cold-start window shows a procession of SSE-driven
        # UI updates as the dashboard catches up to the engine state.
        "startup_status": state.startup_status,
    }


def _config_payload(orchestrator: "Orchestrator") -> dict[str, str]:
    config = orchestrator.config

    config_text = "Config file not found"
    if config.config_path and config.config_path.exists():
        config_text = config.config_path.read_text()

    return {"config": config_text}


def _blocked_issues_payload(
    orchestrator: "Orchestrator",
) -> dict[str, list[dict[str, Any]]]:
    state = orchestrator.state
    config = orchestrator.config
    lm = orchestrator.deps.label_manager

    def make_issue_url(issue_number: int) -> str:
        if not config.repo:
            return ""
        return f"https://github.com/{config.repo}/issues/{issue_number}"

    blocked_issues = []
    recorded_runs = RecordedSessionRunLookup(orchestrator.deps.session_output)

    if state.startup_status == "complete":
        for issue in state.cached_queue_issues:
            if not issue.is_blocked:
                continue

            blocking_labels = lm.get_blocking(list(issue.labels))
            blocking_label = blocking_labels[0] if blocking_labels else "blocked"
            needs_human = lm.requires_human_any(list(issue.labels))

            failure_reason = None
            for entry in reversed(state.session_history):
                if entry.issue_number == issue.number:
                    failure_reason = (
                        getattr(entry, "status_reason", None) or entry.status
                    )
                    break

            worktree_path = get_worktree_path(config, issue.number)
            worktree_exists = worktree_path.exists()
            resume_target = (
                recorded_runs.debug_resume_target(worktree_path, issue.number)
                if worktree_exists
                else None
            )
            has_completion = (
                resume_target is not None and resume_target.completion_file().exists()
            )

            blocked_issues.append(
                {
                    "issue_number": issue.number,
                    "title": issue.title,
                    "agent_type": (issue.agent_type or "unknown").replace("agent:", ""),
                    "blocking_label": blocking_label,
                    "all_blocking_labels": blocking_labels,
                    "needs_human": needs_human,
                    "failure_reason": failure_reason,
                    "issue_url": make_issue_url(issue.number),
                    "worktree_path": str(worktree_path) if worktree_exists else None,
                    "run_dir": str(resume_target.run_dir) if resume_target else None,
                    "has_completion": has_completion,
                }
            )

    return {"blocked_issues": blocked_issues}


def _debug_payload(orchestrator: "Orchestrator") -> dict[str, Any]:
    state = orchestrator.state
    config = orchestrator.config

    agents = {}
    for name, agent_cfg in config.agents.items():
        agents[name] = {
            "timeout": agent_cfg.timeout_minutes,
            "command": (
                agent_cfg.command[:50] + "..."
                if len(agent_cfg.command) > 50
                else agent_cfg.command
            ),
        }

    startup_options = {
        "ui_mode": config.ui_mode,
        "web_port": config.web_port,
        "test_mode": config.filtering.label == "test-data",
        "filtering": {
            "label": config.filtering.label,
            "milestone": config.filtering.milestone,
            "milestones": config.filtering.get_milestones(),
        },
        "max_sessions": config.max_concurrent_sessions,
    }

    return {
        "paused": state.paused,
        "config_path": str(config.config_path) if config.config_path else "None",
        "repo_root": str(config.repo_root),
        "priority_queue": state.priority_queue,
        "agents": agents,
        "startup_options": startup_options,
    }


def _doctor_payload(orchestrator: "Orchestrator | None") -> dict[str, Any]:
    # Doctor intentionally runs without a live orchestrator so startup failures
    # still have diagnostics.
    config = orchestrator.config if orchestrator is not None else None

    result = run_doctor(config=config, runner=LocalCommandRunner())

    if orchestrator is not None:
        result.checks.insert(
            2,
            type(result.checks[0])(
                name="Orchestrator",
                status="ok",
                detail=f"Running, {'paused' if orchestrator.state.paused else 'active'}",
            ),
        )
    else:
        result.checks.insert(
            2,
            type(result.checks[0])(
                name="Orchestrator",
                status="error",
                detail="Not running",
            ),
        )

    return result.to_dict()


@web_diagnostics_router.get(
    "/api/dialog/info",
    response_model=InfoDialogPayload,
    response_model_exclude_none=True,
)
async def get_info_dialog(
    orchestrator: WebOrchestratorDependency,
    deps: WebDiagnosticsDependency,
) -> InfoDialogPayload | JSONResponse:
    """Get view model for the About dialog."""
    if orchestrator is None:
        return _orchestrator_not_running()
    return InfoDialogPayload.model_validate(
        build_info_dialog(_info_payload(orchestrator, deps))
    )


@web_diagnostics_router.get(
    "/api/dialog/config",
    response_model=ConfigDialogPayload,
)
async def get_config_dialog(
    orchestrator: WebOrchestratorDependency,
) -> ConfigDialogPayload | JSONResponse:
    """Get view model for the configuration dialog."""
    if orchestrator is None:
        return _orchestrator_not_running()
    payload = _config_payload(orchestrator)
    return ConfigDialogPayload.model_validate(
        build_config_dialog(payload.get("config", ""))
    )


@web_diagnostics_router.get(
    "/api/dialog/debug",
    response_model=DebugDialogPayload,
    response_model_exclude_none=True,
)
async def get_debug_dialog(
    orchestrator: WebOrchestratorDependency,
) -> DebugDialogPayload | JSONResponse:
    """Get view model for the debug dialog."""
    if orchestrator is None:
        return _orchestrator_not_running()
    return DebugDialogPayload.model_validate(
        build_debug_dialog(_debug_payload(orchestrator))
    )


@web_diagnostics_router.get(
    "/api/dialog/doctor",
    response_model=DoctorDialogPayload,
)
async def get_doctor_dialog(
    orchestrator: WebOrchestratorDependency,
) -> DoctorDialogPayload | JSONResponse:
    """Get view model for the doctor dialog."""
    return DoctorDialogPayload.model_validate(
        build_doctor_dialog(_doctor_payload(orchestrator))
    )


@web_diagnostics_router.get(
    "/api/dialog/session-diagnostics/{issue_number}",
    response_model=SessionDiagnosticsDialogPayload,
    response_model_exclude_none=True,
)
async def get_session_diagnostics_dialog(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    run_dir: str | None = None,
) -> SessionDiagnosticsDialogPayload | JSONResponse:
    """Get view model for session diagnostics dialog."""
    response = session_manifest_response(
        issue_number,
        orchestrator=orchestrator,
        run_dir=run_dir,
    )
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return SessionDiagnosticsDialogPayload.model_validate(
        build_session_diagnostics_dialog(issue_number, payload)
    )


@web_diagnostics_router.get(
    "/api/dialog/validation-failure/{issue_number}",
    response_model=ValidationFailureDialogPayload,
    response_model_exclude_none=True,
)
async def get_validation_failure_dialog(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    run_dir: str | None = None,
) -> ValidationFailureDialogPayload | JSONResponse:
    """Get a focused dialog for a validation run (passed or failed).

    The path name predates passed-run support; one endpoint now serves
    both outcomes so the same dialog can render either. The payload's
    ``status`` field indicates which.
    """
    response = session_manifest_response(
        issue_number,
        orchestrator=orchestrator,
        run_dir=run_dir,
        include_passed_validation=True,
    )
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    validation = payload.get("validation_failure")
    if not isinstance(validation, dict):
        return JSONResponse(
            {"error": "No validation details found for this run"},
            status_code=404,
        )
    return ValidationFailureDialogPayload.model_validate(
        build_validation_failure_dialog(issue_number, payload)
    )


@web_diagnostics_router.get(
    "/api/dialog/blocked-issues",
    response_model=BlockedIssuesDialogPayload,
)
async def get_blocked_issues_dialog(
    orchestrator: WebOrchestratorDependency,
) -> BlockedIssuesDialogPayload | JSONResponse:
    """Get view model for blocked issues dialog."""
    if orchestrator is None:
        return _orchestrator_not_running()
    return BlockedIssuesDialogPayload.model_validate(
        build_blocked_issues_dialog(_blocked_issues_payload(orchestrator))
    )


@web_diagnostics_router.get(
    "/api/dialog/phase/{issue_number}",
    response_model=PhaseDialogPayload,
)
async def get_phase_dialog(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    phase: str | None = None,
) -> PhaseDialogPayload | JSONResponse:
    """Get view model for phase details dialog."""
    response = session_phases_response(issue_number, orchestrator=orchestrator)
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return PhaseDialogPayload.model_validate(
        build_phase_dialog(payload, issue_number, phase)
    )


@web_diagnostics_router.get("/api/dependency-problems")
async def get_dependency_problems(
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Get current dependency problems for issues."""
    if orchestrator is None:
        return _orchestrator_not_running()

    state = orchestrator.state
    config = orchestrator.config

    def make_issue_url(issue_number: int) -> str:
        if not config.repo:
            return ""
        return f"https://github.com/{config.repo}/issues/{issue_number}"

    problems = {}
    for issue_num, problem in state.dependency_problems.items():
        problems[issue_num] = {
            "issue_number": problem.issue_number,
            "issue_title": problem.issue_title,
            "summary": problem.summary,
            "issue_url": make_issue_url(problem.issue_number),
        }

    return JSONResponse({"problems": problems})


@web_diagnostics_router.get("/api/stale-issues")
async def get_stale_issues(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get issues with stale in-progress labels."""
    if orchestrator is None:
        return _orchestrator_not_running()

    state = orchestrator.state
    config = orchestrator.config
    threshold = config.stale_escalation_ticks

    stale = {}
    for issue_num, ticks in state.stale_issue_ticks.items():
        stale[issue_num] = {
            "issue_number": issue_num,
            "consecutive_ticks": ticks,
            "persistent": threshold > 0 and ticks >= threshold,
            "threshold": threshold,
        }

    return JSONResponse({"stale": stale})


@web_diagnostics_router.get("/api/info")
async def get_info(
    orchestrator: WebOrchestratorDependency,
    deps: WebDiagnosticsDependency,
) -> JSONResponse:
    """Get orchestrator info for the About modal."""
    if orchestrator is None:
        return _orchestrator_not_running()

    return JSONResponse(_info_payload(orchestrator, deps))


@web_diagnostics_router.get("/api/config")
async def get_config(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get the raw config file contents."""
    if orchestrator is None:
        return _orchestrator_not_running()

    return JSONResponse(_config_payload(orchestrator))


@web_diagnostics_router.get("/api/blocked-issues")
async def get_blocked_issues(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get all blocked issues with their blocking labels and context."""
    if orchestrator is None:
        return _orchestrator_not_running()

    return JSONResponse(_blocked_issues_payload(orchestrator))


@web_diagnostics_router.get("/api/failure-diagnosis/{issue_number}")
async def get_failure_diagnosis(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Get detailed failure diagnosis for an issue."""
    if orchestrator is None:
        return _orchestrator_not_running()

    diagnosis = orchestrator.get_failure_diagnosis(issue_number)
    return JSONResponse(diagnosis)


@web_diagnostics_router.post("/api/issues/{issue_number}/audit")
async def force_issue_audit(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Force a fresh session-failure audit for an issue."""
    if orchestrator is None:
        return _orchestrator_not_running()

    diagnosis = orchestrator.get_failure_diagnosis(issue_number)
    return JSONResponse(diagnosis)


@web_diagnostics_router.get("/api/debug")
async def get_debug(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get debug info for troubleshooting."""
    if orchestrator is None:
        return _orchestrator_not_running()

    return JSONResponse(_debug_payload(orchestrator))


@web_diagnostics_router.get("/api/doctor")
async def get_doctor(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Run diagnostics and return health status."""
    return JSONResponse(_doctor_payload(orchestrator))
