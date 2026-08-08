"""Control Center orchestrator management routes."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ..execution.control_center_actions import (
    DoctorActionRequest,
    RefreshActionRequest,
    RepoActionRequest,
)
from ..execution.control_center_runtime import (
    build_repo_identity,
    confirm_orchestrator_at_port,
    detect_orchestrator_by_port,
    enrich_runtime_health,
    get_selected_config,
    is_shutdown_complete,
)
from ..infra.config import Config, get_config_path
from ..infra.repo_guardrails import (
    RepoGuardrailsError,
    RepoGuardrailsInstallResult,
    setup_repo_guardrails,
)
from ..infra.supervisor import DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS, MultiInstanceStatus, SupervisorOps
from .control_api_orchestrator_support import (
    ControlApiOrchestratorDependency,
)
from .shutdown_reason_support import parse_shutdown_reason

logger = logging.getLogger(__name__)

control_orchestrator_router = APIRouter()


def _normalize_config_name(raw: object) -> str | None:
    """Normalize a repo config name while preventing path traversal."""
    if raw in (None, ""):
        config_name = "default.yaml"
    elif isinstance(raw, str):
        config_name = raw
    else:
        return None

    if not config_name.endswith(".yaml"):
        config_name += ".yaml"

    config_path = Path(config_name)
    if config_path.is_absolute() or config_path.name != config_name or config_name == ".yaml":
        return None
    return config_name


def _repo_relative_path(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _serialize_guardrails_result(result: RepoGuardrailsInstallResult) -> dict[str, object]:
    repo_root = result.repo_root
    return {
        "status": "repaired",
        "repo_root": str(repo_root),
        "hooks_path": result.hooks_path_config,
        "hooks_dir": _repo_relative_path(repo_root, result.hooks_dir),
        "pre_push_hook": _repo_relative_path(repo_root, result.pre_push_hook),
        "verify_script": _repo_relative_path(repo_root, result.verify_script),
        "helper_script": _repo_relative_path(repo_root, result.helper_script),
        "installed_files": [
            _repo_relative_path(repo_root, path) for path in result.installed_files
        ],
        "preserved_files": [
            _repo_relative_path(repo_root, path) for path in result.preserved_files
        ],
        "agent_hook_files": {
            agent_type: [_repo_relative_path(repo_root, path) for path in paths]
            for agent_type, paths in result.agent_hook_files.items()
        },
    }


def _summarize_doctor_failures(doctor_result: Any) -> str:
    """Return a short human-readable summary of failed doctor checks."""
    checks = getattr(doctor_result, "checks", []) or []
    failed = [check for check in checks if getattr(check, "status", None) == "error"]
    if not failed:
        return "Pre-flight checks failed"
    parts: list[str] = []
    for check in failed[:2]:
        name = getattr(check, "name", "Check")
        detail = (getattr(check, "detail", "") or "").strip()
        parts.append(f"{name}: {detail}" if detail else name)
    if len(failed) > 2:
        parts.append(f"+{len(failed) - 2} more")
    return "Pre-flight checks failed: " + "; ".join(parts)


@control_orchestrator_router.post("/control/orchestrator/start")
async def control_start(  # noqa: C901, PLR0912 - startup orchestration spans validation and supervisor handoff
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Start an orchestrator for a repository."""
    from ..infra.repo_lock import AlreadyRunning
    from ..infra.repo_registry import set_selected_config

    sv = deps.get_supervisor()

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    port = body.get("port")
    if port is not None and (not isinstance(port, int) or port < 1 or port > 65535):
        return JSONResponse({"error": "Invalid port"}, status_code=400)

    config_name = body.get("config_name", "default.yaml")
    if not config_name.endswith(".yaml"):
        config_name += ".yaml"
    force_restart = bool(body.get("force_restart", False))
    start_paused = bool(body.get("start_paused", False))
    expected_identity = build_repo_identity(repo_root)

    try:
        detected = detect_orchestrator_by_port(
            repo_root,
            config_name,
            expected_identity=expected_identity,
        )
        if detected and detected.get("identity_mismatch"):
            stopped = sv.stop_by_port(
                detected["port"],
                force=True,
                reason="engine identity mismatch detected on /control/start",
                actor="control-center",
            )
            if not stopped:
                return JSONResponse(
                    {
                        "error": "engine_identity_mismatch",
                        "detail": "Mismatched engine detected and could not be stopped",
                        "port": detected["port"],
                        "expected_identity": detected.get("expected_identity"),
                        "observed_identity": detected.get("observed_identity"),
                        "identity_mismatch": detected.get("identity_mismatch"),
                    },
                    status_code=409,
                )
        elif detected and not force_restart:
            return JSONResponse(
                {
                    "error": "orphaned_running",
                    "status": "running",
                    "port": detected["port"],
                    "repo_root": str(repo_root),
                    "health": detected.get("health", "unknown"),
                    "tick_age_seconds": detected.get("tick_age_seconds"),
                },
                status_code=409,
            )
        if detected and force_restart:
            stopped = sv.stop_by_port(
                detected["port"],
                force=True,
                reason="force_restart=true on /control/start",
                actor="control-center",
            )
            if not stopped:
                return JSONResponse(
                    {
                        "error": "stop_failed",
                        "detail": "Unable to stop existing orchestrator process.",
                    },
                    status_code=500,
                )

        set_selected_config(repo_root, config_name)

        from ..infra.launcher import LaunchStatus, launch_subprocess

        config_path = get_config_path(repo_root, config_name)
        config = Config.load(config_path)

        launch_result = launch_subprocess(
            repo_root=repo_root,
            config=config,
            config_name=config_name,
            supervisor_ops=sv,
            expected_identity=expected_identity.to_dict(),
            start_paused=start_paused,
        )

        if LaunchStatus.parse(launch_result.status) is LaunchStatus.DOCTOR_ERROR:
            return JSONResponse(
                {
                    "error": "doctor_failed",
                    "detail": _summarize_doctor_failures(launch_result.doctor),
                    "doctor": launch_result.doctor.to_dict(),
                },
                status_code=422,
            )

        if LaunchStatus.parse(launch_result.status) is LaunchStatus.ALREADY_RUNNING:
            response = {
                "error": "already_running",
                "detail": launch_result.error or "Orchestrator already running",
                "doctor": launch_result.doctor.to_dict(),
            }
            if launch_result.supervisor:
                response.update(launch_result.supervisor)
            return JSONResponse(response, status_code=409)

        if not launch_result.launched:
            return JSONResponse(
                {
                    "error": "launch_failed",
                    "detail": launch_result.error or "Unknown launch error",
                    "doctor": launch_result.doctor.to_dict(),
                },
                status_code=500,
            )

        response_data: dict[str, Any] = {
            "status": "started",
            "repo_root": str(repo_root),
            "config_name": config_name,
            "repo_identity": expected_identity.to_dict(),
            "doctor": launch_result.doctor.to_dict(),
        }
        if launch_result.supervisor:
            response_data.update(launch_result.supervisor)
            deps.track_launched_pids(launch_result.supervisor)
        return JSONResponse(response_data)
    except FileNotFoundError as exc:
        return JSONResponse(
            {
                "error": "config_not_found",
                "detail": str(exc),
            },
            status_code=404,
        )
    except AlreadyRunning as exc:
        if is_shutdown_complete(exc.port):
            logger.info("Orchestrator in shutdown-complete state, restarting: %s", repo_root)
            try:
                sv.stop(
                    repo_root,
                    reason="restart after shutdown-complete state on /control/start",
                    actor="control-center.start",
                )
                time.sleep(0.5)
                info = sv.start(
                    repo_root,
                    config_name=config_name,
                    expected_identity=expected_identity.to_dict(),
                )
                deps.track_launched_pids({"pid": info.pid})
                return JSONResponse(
                    {
                        "status": "restarted",
                        "pid": info.pid,
                        "port": info.http_port,
                        "repo_root": str(repo_root),
                        "config_name": config_name,
                    }
                )
            except Exception as restart_err:
                logger.exception("Failed to restart orchestrator for %s", repo_root)
                return JSONResponse(
                    {
                        "error": "restart_failed",
                        "detail": str(restart_err),
                    },
                    status_code=500,
                )
        return JSONResponse(
            {
                "error": "already_running",
                "pid": exc.pid,
                "port": exc.port,
                "repo_root": str(exc.repo_root),
            },
            status_code=409,
        )
    except Exception as exc:
        logger.exception("Failed to start orchestrator for %s", repo_root)
        return JSONResponse(
            {
                "error": "start_failed",
                "detail": str(exc),
            },
            status_code=500,
        )


@control_orchestrator_router.post("/control/orchestrator/stop")
async def control_stop(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Stop the orchestrator for a repository."""
    sv = deps.get_supervisor()

    logger.info("[control_stop] Received stop request")

    try:
        body = await request.json()
        logger.info("[control_stop] Body: %s", body)
    except json.JSONDecodeError:
        logger.error("[control_stop] Invalid JSON")
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        logger.error("[control_stop] Invalid repo_root: %s", body.get("repo_root"))
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    parsed = parse_shutdown_reason(
        body,
        endpoint="/control/orchestrator/stop",
        default_actor="control-center.stop",
    )
    if isinstance(parsed, JSONResponse):
        logger.error("[control_stop] Missing 'reason' in body")
        return parsed
    reason = parsed.reason
    actor = parsed.actor

    force = body.get("force", False)
    force_if_timeout = bool(body.get("force_if_timeout", True))
    graceful_timeout_seconds = deps.coerce_graceful_timeout_seconds(
        body.get("graceful_timeout_seconds"),
        DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS,
    )
    port_override = body.get("port")
    if port_override is not None and (not isinstance(port_override, int) or port_override < 1 or port_override > 65535):
        return JSONResponse({"error": "Invalid port"}, status_code=400)

    if deps.global_shutdown_in_progress():
        return JSONResponse(
            {
                "error": "global_shutdown_in_progress",
                "detail": "Global shutdown is in progress and already controls engine shutdown behavior.",
                "actions": [
                    "View global shutdown status",
                    "Change global shutdown",
                    "Abort global shutdown",
                ],
            },
            status_code=409,
        )

    deps.begin_engine_shutdown_operation(
        repo_root,
        bool(force),
        force_if_timeout,
        graceful_timeout_seconds,
    )

    logger.info("[control_stop] Calling supervisor.stop(%s, force=%s)", repo_root, force)

    try:
        status_info = sv.status(repo_root)
        if status_info.state != "running" and port_override:
            if not confirm_orchestrator_at_port(repo_root, port_override):
                return JSONResponse(
                    {
                        "error": "port_mismatch",
                        "detail": "No matching orchestrator found on the provided port.",
                    },
                    status_code=409,
                )
            stopped = sv.stop_by_port(
                port_override, force=force, reason=reason, actor=actor,
            )
            stopped_count = 1 if stopped else 0
        else:
            stopped_count = sv.stop_all_instances(
                repo_root,
                force=force,
                reason=reason,
                actor=actor,
                graceful_timeout_seconds=graceful_timeout_seconds,
                force_if_graceful_fails=force_if_timeout or force,
            )
            stopped = stopped_count > 0
        logger.info("[control_stop] supervisor.stop_all_instances returned: %d", stopped_count)

        if stopped:
            return JSONResponse(
                {
                    "status": "stopped",
                    "repo_root": str(repo_root),
                    "stopped_count": stopped_count,
                }
            )
        return JSONResponse({"status": "not_running", "repo_root": str(repo_root)})
    finally:
        deps.finish_engine_shutdown_operation(repo_root)


@control_orchestrator_router.post("/control/orchestrator/reconcile")
async def control_reconcile(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Reconcile stale runtime metadata and optionally stop orphaned/unresponsive engines."""
    from ..infra.repo_registry import list_repos

    sv = deps.get_supervisor()
    stop_orphaned, stop_unresponsive, force = await _parse_reconcile_options(request)

    reconciled_stale_locks: list[str] = []
    orphaned_detected: list[dict[str, Any]] = []
    stopped_orphaned: list[str] = []
    unresponsive_detected: list[dict[str, Any]] = []
    stopped_unresponsive: list[str] = []

    for repo in list_repos():
        reconciliation = _reconcile_repo_runtime(
            sv=sv,
            repo_path=Path(repo.path),
            selected_config=repo.selected_config or "default.yaml",
            stop_orphaned=stop_orphaned,
            stop_unresponsive=stop_unresponsive,
            force=force,
        )
        if reconciliation is None:
            continue

        if reconciliation["reconciled_stale_lock"]:
            reconciled_stale_locks.append(repo.path)
        orphaned_detected.extend(reconciliation["orphaned_detected"])
        if reconciliation["stopped_orphaned"]:
            stopped_orphaned.append(repo.path)
        unresponsive_detected.extend(reconciliation["unresponsive_detected"])
        if reconciliation["stopped_unresponsive"]:
            stopped_unresponsive.append(repo.path)

    return JSONResponse(
        {
            "status": "ok",
            "reconciled_stale_locks": reconciled_stale_locks,
            "orphaned_detected": orphaned_detected,
            "stopped_orphaned": stopped_orphaned,
            "unresponsive_detected": unresponsive_detected,
            "stopped_unresponsive": stopped_unresponsive,
        }
    )


async def _parse_reconcile_options(request: Request) -> tuple[bool, bool, bool]:
    stop_orphaned = False
    stop_unresponsive = False
    force = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            stop_orphaned = bool(body.get("stop_orphaned", False))
            stop_unresponsive = bool(body.get("stop_unresponsive", False))
            force = bool(body.get("force", False))
    except Exception:
        pass
    return stop_orphaned, stop_unresponsive, force


def _reconcile_repo_runtime(
    *,
    sv: SupervisorOps,
    repo_path: Path,
    selected_config: str,
    stop_orphaned: bool,
    stop_unresponsive: bool,
    force: bool,
) -> dict[str, Any] | None:
    """Reconcile one repository and return aggregated reconciliation outcomes."""
    if not repo_path.exists():
        return None

    multi_status = sv.status_all_instances(repo_path, config_name=selected_config)
    if _is_multi_instance_repo(multi_status):
        return _reconcile_multi_instance_repo_runtime(
            sv=sv,
            repo_path=repo_path,
            multi_status=multi_status,
            stop_unresponsive=stop_unresponsive,
            force=force,
        )

    status_info = sv.status(repo_path)
    if status_info.state == "failed":
        return {
            "reconciled_stale_lock": sv.stop(
                repo_path,
                force=False,
                reason="reconcile-runtime: stale lock for failed orchestrator",
                actor="control-center.reconcile",
            ),
            "orphaned_detected": [],
            "stopped_orphaned": False,
            "unresponsive_detected": [],
            "stopped_unresponsive": False,
        }

    if status_info.state != "running":
        detected = detect_orchestrator_by_port(repo_path, selected_config)
        if not detected:
            return {
                "reconciled_stale_lock": False,
                "orphaned_detected": [],
                "stopped_orphaned": False,
                "unresponsive_detected": [],
                "stopped_unresponsive": False,
            }
        orphaned_entry = {"repo_root": str(repo_path), "port": detected.get("port")}
        if stop_orphaned and detected.get("port"):
            stopped = sv.stop_by_port(
                int(detected["port"]),
                force=force,
                reason="reconcile-runtime: stop orphaned orchestrator with no lock",
                actor="control-center.reconcile",
            )
            return {
                "reconciled_stale_lock": False,
                "orphaned_detected": [orphaned_entry],
                "stopped_orphaned": stopped,
                "unresponsive_detected": [],
                "stopped_unresponsive": False,
            }
        return {
            "reconciled_stale_lock": False,
            "orphaned_detected": [orphaned_entry],
            "stopped_orphaned": False,
            "unresponsive_detected": [],
            "stopped_unresponsive": False,
        }

    payload = enrich_runtime_health(repo_path, status_info.to_dict())
    if payload is None or payload.get("runtime_health") != "unresponsive":
        return {
            "reconciled_stale_lock": False,
            "orphaned_detected": [],
            "stopped_orphaned": False,
            "unresponsive_detected": [],
            "stopped_unresponsive": False,
        }

    unresponsive_entry = {
        "repo_root": str(repo_path),
        "instance_id": None,
        "heartbeat_age_seconds": payload.get("heartbeat_age_seconds"),
        "pid": payload.get("pid"),
        "port": payload.get("port"),
    }
    if stop_unresponsive:
        return {
            "reconciled_stale_lock": False,
            "orphaned_detected": [],
            "stopped_orphaned": False,
            "unresponsive_detected": [unresponsive_entry],
            "stopped_unresponsive": sv.stop(
                repo_path,
                force=force,
                reason="reconcile-runtime: stop unresponsive orchestrator",
                actor="control-center.reconcile",
            ),
        }
    return {
        "reconciled_stale_lock": False,
        "orphaned_detected": [],
        "stopped_orphaned": False,
        "unresponsive_detected": [unresponsive_entry],
        "stopped_unresponsive": False,
    }


def _is_multi_instance_repo(multi_status: MultiInstanceStatus) -> bool:
    return multi_status.expected_count > 1 or any(
        inst.instance_id is not None for inst in multi_status.instances
    )


def _reconcile_multi_instance_repo_runtime(
    *,
    sv: SupervisorOps,
    repo_path: Path,
    multi_status: MultiInstanceStatus,
    stop_unresponsive: bool,
    force: bool,
) -> dict[str, Any]:
    """Reconcile a multi-instance repository."""
    reconciled_stale_lock = False
    unresponsive_detected: list[dict[str, Any]] = []
    stopped_unresponsive = False

    instance_ids: list[str | None] = [None]
    instance_ids.extend(f"orchestrator-{i}" for i in range(1, multi_status.expected_count + 1))
    instance_ids.extend(
        inst.instance_id
        for inst in multi_status.instances
        if inst.instance_id is not None
    )

    deduped_ids: list[str | None] = []
    for instance_id in instance_ids:
        if instance_id not in deduped_ids:
            deduped_ids.append(instance_id)

    for instance_id in deduped_ids:
        status_info = sv.status(repo_path, instance_id=instance_id)
        if status_info.state == "failed":
            if sv.stop(
                repo_path,
                force=False,
                instance_id=instance_id,
                reason="reconcile-runtime: stale lock for failed multi-instance orchestrator",
                actor="control-center.reconcile",
            ):
                reconciled_stale_lock = True
            continue

        if status_info.state != "running":
            continue

        payload = enrich_runtime_health(
            repo_path,
            status_info.to_dict(),
            instance_id=instance_id,
        )
        if payload is None or payload.get("runtime_health") != "unresponsive":
            continue

        unresponsive_detected.append(
            {
                "repo_root": str(repo_path),
                "instance_id": instance_id,
                "heartbeat_age_seconds": payload.get("heartbeat_age_seconds"),
                "pid": payload.get("pid"),
                "port": payload.get("port"),
            }
        )

        if not stop_unresponsive:
            continue

        port = payload.get("port")
        unresponsive_reason = (
            "reconcile-runtime: stop unresponsive multi-instance orchestrator"
        )
        stopped = sv.stop_by_port(
            port,
            force=force,
            reason=unresponsive_reason,
            actor="control-center.reconcile",
        ) if isinstance(port, int) else sv.stop(
            repo_path,
            force=force,
            instance_id=instance_id,
            reason=unresponsive_reason,
            actor="control-center.reconcile",
        )
        if stopped:
            stopped_unresponsive = True

    return {
        "reconciled_stale_lock": reconciled_stale_lock,
        "orphaned_detected": [],
        "stopped_orphaned": False,
        "unresponsive_detected": unresponsive_detected,
        "stopped_unresponsive": stopped_unresponsive,
    }


@control_orchestrator_router.get("/control/orchestrator/status")
async def control_status(
    deps: ControlApiOrchestratorDependency,
    repo_root: str = Query(...),
    config_name: str | None = Query(None),
) -> JSONResponse:
    """Get the status of the orchestrator for a repository."""
    sv = deps.get_supervisor()

    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    selected = config_name or get_selected_config(path) or "default.yaml"
    multi_status = sv.status_all_instances(path, config_name=selected)

    if multi_status.expected_count > 1 or len(multi_status.instances) > 1:
        return JSONResponse(
            {
                "multi_instance": True,
                "repo_root": str(path),
                "expected_count": multi_status.expected_count,
                "running_count": sum(
                    1 for status in multi_status.instances if status.state == "running"
                ),
                "instances": [status.to_dict() for status in multi_status.instances],
            }
        )

    if multi_status.instances and len(multi_status.instances) == 1:
        payload = enrich_runtime_health(path, multi_status.instances[0].to_dict())
        return JSONResponse(payload or multi_status.instances[0].to_dict())

    status_info = sv.status(path)
    if status_info.state != "running":
        detected = detect_orchestrator_by_port(path, selected)
        if detected:
            status_data = detected.get("status", {})
            orphaned_payload = {
                "state": "running",
                "pid": None,
                "port": detected["port"],
                "started_at": None,
                "recovered": False,
                "error": None,
                "orphaned": True,
                "health": detected.get("health", "unknown"),
                "tick_age_seconds": detected.get("tick_age_seconds"),
                "shutdown_requested": status_data.get("shutdown_requested", False),
                "active_session_count": len(status_data.get("active_sessions", [])),
            }
            return JSONResponse(
                enrich_runtime_health(path, orphaned_payload, orphaned=True) or orphaned_payload
            )

    payload = enrich_runtime_health(path, status_info.to_dict())
    return JSONResponse(payload or status_info.to_dict())


@control_orchestrator_router.post("/control/orchestrator/pause")
async def control_pause(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Pause the orchestrator for a repository."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    actions = deps.get_control_actions()
    result = await actions.pause_cmd.execute(RepoActionRequest(repo_root=repo_root))
    return JSONResponse(result.payload, status_code=result.status_code)


@control_orchestrator_router.post("/control/orchestrator/resume")
async def control_resume(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Resume the orchestrator for a repository."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    actions = deps.get_control_actions()
    result = await actions.resume_cmd.execute(RepoActionRequest(repo_root=repo_root))
    return JSONResponse(result.payload, status_code=result.status_code)


@control_orchestrator_router.post("/control/orchestrator/refresh")
async def control_refresh(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Trigger refresh on the orchestrator for a repository."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    actions = deps.get_control_actions()
    result = await actions.refresh_cmd.execute(
        RefreshActionRequest(
            repo_root=repo_root,
            inflight_stable_ids=body.get("inflight_stable_ids"),
        )
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@control_orchestrator_router.get("/control/orchestrator/last_failure")
async def control_last_failure(
    deps: ControlApiOrchestratorDependency,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Get the last startup failure for a repository."""
    from ..infra.repo_identity import state_dir

    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    failure_path = state_dir(path) / "last_failure.json"
    if not failure_path.exists():
        return JSONResponse({"last_failure": None})

    try:
        with open(failure_path) as handle:
            data = json.load(handle)
        return JSONResponse({"last_failure": data})
    except (json.JSONDecodeError, OSError) as exc:
        return JSONResponse(
            {
                "error": "read_failed",
                "detail": str(exc),
            },
            status_code=500,
        )


@control_orchestrator_router.get("/control/orchestrator/doctor")
async def control_doctor(
    deps: ControlApiOrchestratorDependency,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Run diagnostics for a repository."""
    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    actions = deps.get_control_actions()
    result = await actions.doctor_cmd.execute(DoctorActionRequest(repo_root=path))
    return JSONResponse(result.payload, status_code=result.status_code)


@control_orchestrator_router.post("/control/orchestrator/guardrails/repair")
async def control_repair_guardrails(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Repair repo-local guardrails by running the standard setup-guardrails flow."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    config_name = _normalize_config_name(body.get("config_name"))
    if config_name is None:
        return JSONResponse({"error": "Invalid config_name"}, status_code=400)

    config_path = get_config_path(repo_root, config_name)
    if not config_path.exists():
        return JSONResponse(
            {
                "error": "config_not_found",
                "detail": f"Config file not found: {config_name}",
                "config_name": config_name,
            },
            status_code=404,
        )

    try:
        config = Config.load(config_path)
        result = setup_repo_guardrails(config, target_root=repo_root)
    except RepoGuardrailsError as exc:
        return JSONResponse(
            {
                "error": "repair_failed",
                "detail": str(exc),
                "config_name": config_name,
            },
            status_code=400,
        )

    payload = _serialize_guardrails_result(result)
    payload["config_name"] = config_name
    payload["message"] = (
        "Repo guardrails repaired. Review and commit changed files if this updated "
        "tracked files."
    )
    return JSONResponse(payload)


@control_orchestrator_router.post("/control/orchestrator/ai_diagnose")
async def control_ai_diagnose(
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Run AI-powered diagnostics for a repository."""
    from ..infra.ai_diagnose import run_ai_diagnose

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    timeout = body.get("timeout", 120)
    if not isinstance(timeout, int) or timeout < 10 or timeout > 600:
        timeout = 120

    result = run_ai_diagnose(repo_root, timeout_seconds=timeout)
    return JSONResponse(result.to_dict())


@control_orchestrator_router.get("/control/orchestrator/log_tail")
async def control_log_tail(
    deps: ControlApiOrchestratorDependency,
    repo_root: str = Query(...),
    n: int = Query(200, ge=1, le=10000),
) -> JSONResponse:
    """Get the last N lines of the orchestrator log."""
    from ..infra.repo_identity import state_dir

    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    log_path = state_dir(path) / "logs" / "orchestrator.log"
    if not log_path.exists():
        return JSONResponse({"lines": [], "total_lines": 0})

    try:
        lines, total_lines = _read_last_n_lines(log_path, n)
    except OSError as exc:
        return JSONResponse(
            {
                "error": "read_failed",
                "detail": str(exc),
            },
            status_code=500,
        )

    return JSONResponse(
        {
            "lines": lines,
            "total_lines": total_lines,
            "returned_lines": len(lines),
        }
    )


def _read_last_n_lines(log_path: Path, n: int) -> tuple[list[str], int]:
    """Read the last N lines of a log file plus its total line count."""
    with open(log_path, "rb") as handle:
        handle.seek(0, 2)
        file_size = handle.tell()

        lines: list[str] = []
        chunk_size = 8192
        remaining = file_size

        while len(lines) < n + 1 and remaining > 0:
            read_size = min(chunk_size, remaining)
            remaining -= read_size
            handle.seek(remaining)
            chunk = handle.read(read_size).decode("utf-8", errors="replace")
            chunk_lines = chunk.split("\n")

            if lines:
                lines[0] = chunk_lines[-1] + lines[0]
                chunk_lines = chunk_lines[:-1]

            lines = chunk_lines + lines

        lines = lines[-n:] if len(lines) > n else lines

        handle.seek(0)
        total_lines = sum(1 for _ in handle)

    return lines, total_lines


__all__ = ["control_orchestrator_router"]
