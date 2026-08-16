"""Control Center orchestrator management routes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ..domain.repository_launch_selection import RepositoryLaunchSelection
from ..execution.control_center_actions import (
    DoctorActionRequest,
    RefreshActionRequest,
    RepoActionRequest,
)
from ..execution.control_center_runtime import (
    confirm_orchestrator_at_port,
    detect_repository_orchestrators,
    enrich_runtime_health,
    get_selected_launch_selection,
)
from ..infra.config import Config, get_config_path
from ..infra.repo_guardrails import (
    RepoGuardrailsError,
    setup_repo_guardrails,
)
from ..infra.supervisor import (
    DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS,
    MultiInstanceStatus,
)
from ..ports.repository_engine_supervisor import SupervisorOps
from .control_api_orchestrator_support import (
    ControlApiOrchestratorDependency,
    read_last_n_lines,
    serialize_guardrails_result,
    stopped_engine_payload,
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
    if (
        config_path.is_absolute()
        or config_path.name != config_name
        or config_name == ".yaml"
    ):
        return None
    return config_name


@control_orchestrator_router.post("/control/orchestrator/start")
async def control_start(  # noqa: C901, PLR0912 - startup orchestration spans validation and supervisor handoff
    request: Request,
    deps: ControlApiOrchestratorDependency,
) -> JSONResponse:
    """Start an orchestrator for a repository."""
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

    try:
        selection = RepositoryLaunchSelection.parse(
            mode=body.get("mode"),
            config_name=body.get("config_name"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    from ..execution.repository_engine_start import RepositoryEngineStartRequest

    result = deps.get_control_actions().start_repo_engine_cmd.execute(
        RepositoryEngineStartRequest(
            repo_root=repo_root,
            selection=selection,
            port=port,
            force_restart=bool(body.get("force_restart", False)),
            start_paused=bool(body.get("start_paused", False)),
            actor="control-center",
        )
    )
    if result.status_code == 200:
        deps.track_launched_pids(result.payload)
    return JSONResponse(dict(result.payload), status_code=result.status_code)


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
    if port_override is not None and (
        not isinstance(port_override, int) or port_override < 1 or port_override > 65535
    ):
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

    logger.info(
        "[control_stop] Calling supervisor.stop(%s, force=%s)", repo_root, force
    )

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
                port_override,
                force=force,
                reason=reason,
                actor=actor,
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
        logger.info(
            "[control_stop] supervisor.stop_all_instances returned: %d", stopped_count
        )

        if stopped:
            return JSONResponse(stopped_engine_payload(repo_root, stopped_count))
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
        selection = repo.launch_selection
        reconciliation = _reconcile_repo_runtime(
            sv=sv,
            repo_path=Path(repo.path),
            selected_config=selection.config.value,
            selected_mode=selection.mode.value,
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
    selected_mode: str,
    stop_orphaned: bool,
    stop_unresponsive: bool,
    force: bool,
) -> dict[str, Any] | None:
    """Reconcile one repository and return aggregated reconciliation outcomes."""
    if not repo_path.exists():
        return None

    multi_status = sv.status_all_instances(
        repo_path,
        config_name=selected_config,
        **({"mode": selected_mode} if selected_mode != "default" else {}),
    )
    tracked_ports = {
        status.port
        for status in multi_status.instances
        if status.state == "running" and status.port
    }
    orphaned = [
        detected
        for detected in detect_repository_orchestrators(repo_path)
        if detected.get("port") not in tracked_ports
    ]
    orphaned_entries = [
        {
            "repo_root": str(repo_path),
            "port": detected.get("port"),
            "active_selection": detected.get("active_selection")
            or detected.get("probed_selection"),
        }
        for detected in orphaned
    ]
    orphan_stop_results = (
        [
            sv.stop_by_port(
                int(detected["port"]),
                force=force,
                reason="reconcile-runtime: stop orphaned orchestrator with no lock",
                actor="control-center.reconcile",
            )
            for detected in orphaned
            if detected.get("port")
        ]
        if stop_orphaned
        else []
    )
    orphan_fields = {
        "orphaned_detected": orphaned_entries,
        "stopped_orphaned": bool(orphan_stop_results)
        and all(orphan_stop_results),
    }
    if _is_multi_instance_repo(multi_status):
        result = _reconcile_multi_instance_repo_runtime(
            sv=sv,
            repo_path=repo_path,
            multi_status=multi_status,
            stop_unresponsive=stop_unresponsive,
            force=force,
        )
        result.update(orphan_fields)
        return result

    status_info = sv.status(repo_path)
    if status_info.state == "failed":
        return {
            "reconciled_stale_lock": sv.stop(
                repo_path,
                force=False,
                reason="reconcile-runtime: stale lock for failed orchestrator",
                actor="control-center.reconcile",
            ),
            **orphan_fields,
            "unresponsive_detected": [],
            "stopped_unresponsive": False,
        }

    if status_info.state != "running":
        return {
            "reconciled_stale_lock": False,
            **orphan_fields,
            "unresponsive_detected": [],
            "stopped_unresponsive": False,
        }

    payload = enrich_runtime_health(repo_path, status_info.to_dict())
    if payload is None or payload.get("runtime_health") != "unresponsive":
        return {
            "reconciled_stale_lock": False,
            **orphan_fields,
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
        **orphan_fields,
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
    instance_ids.extend(
        f"orchestrator-{i}" for i in range(1, multi_status.expected_count + 1)
    )
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
        stopped = (
            sv.stop_by_port(
                port,
                force=force,
                reason=unresponsive_reason,
                actor="control-center.reconcile",
            )
            if isinstance(port, int)
            else sv.stop(
                repo_path,
                force=force,
                instance_id=instance_id,
                reason=unresponsive_reason,
                actor="control-center.reconcile",
            )
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
    mode: str | None = Query(None),
) -> JSONResponse:
    """Get the status of the orchestrator for a repository."""
    sv = deps.get_supervisor()

    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    desired = get_selected_launch_selection(path)
    try:
        selection = RepositoryLaunchSelection.parse(
            mode=mode or desired.mode.value,
            config_name=config_name or desired.config.value,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    selected = selection.config.value
    multi_status = sv.status_all_instances(
        path, config_name=selected, mode=selection.mode.value
    )

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
        detected_engines = detect_repository_orchestrators(path)
        if detected_engines:
            detected = detected_engines[0]
            status_data = detected.get("status", {})
            info = detected.get("info", {})
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
                "configuration_mode": info.get("configuration_mode"),
                "config_name": info.get("config_name"),
                "config_fingerprint": info.get("config_fingerprint"),
            }
            return JSONResponse(
                enrich_runtime_health(path, orphaned_payload, orphaned=True)
                or orphaned_payload
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
    config_name: str | None = Query(None),
    mode: str | None = Query(None),
) -> JSONResponse:
    """Run diagnostics for a repository."""
    path = deps.validate_repo_root(repo_root)
    if path is None:
        return JSONResponse({"error": "Invalid or missing repo_root"}, status_code=400)

    actions = deps.get_control_actions()
    desired = get_selected_launch_selection(path)
    try:
        selection = RepositoryLaunchSelection.parse(
            mode=mode or desired.mode,
            config_name=config_name or desired.config,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    result = await actions.doctor_cmd.execute(
        DoctorActionRequest(repo_root=path, selection=selection)
    )
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

    try:
        selection = RepositoryLaunchSelection.parse(
            mode=body.get("mode"),
            config_name=config_name,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    config_path = get_config_path(repo_root, config_name, selection.mode)
    if not config_path.exists():
        return JSONResponse(
            {
                "error": "config_not_found",
                "detail": f"Config file not found: {config_name}",
                "config_name": config_name,
                "mode": selection.mode.value,
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
                "mode": selection.mode.value,
            },
            status_code=400,
        )

    payload = serialize_guardrails_result(result)
    payload["config_name"] = config_name
    payload["mode"] = selection.mode.value
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

    identity = deps.get_control_actions().effective_configuration_identity(repo_root)
    result = run_ai_diagnose(
        repo_root,
        timeout_seconds=timeout,
        identity=identity,
    )
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
        lines, total_lines = read_last_n_lines(log_path, n)
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


__all__ = ["control_orchestrator_router"]
