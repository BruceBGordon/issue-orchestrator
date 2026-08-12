"""Control Center shutdown routes.

The shutdown routes import ``control_api_shutdown_state`` directly because
this route family owns the process-local shutdown operation state.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import logging
import os
from pathlib import Path
import threading
from typing import Literal, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..infra.shutdown_timing import StopAborted
from ..infra.supervisor import DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS
from ..ports.repository_engine_supervisor import SupervisorOps
from . import control_api_shutdown_state as shutdown_state
from .control_api_shutdown_support import ControlApiShutdownDependency

logger = logging.getLogger(__name__)

control_shutdown_router = APIRouter()

ShutdownRepoResult = Literal["aborted", "failed", "skipped", "stopped"]


class _RepoRecord(Protocol):
    path: str


@control_shutdown_router.post("/control/shutdown")
async def shutdown_control_center(
    request: Request,
    deps: ControlApiShutdownDependency,
) -> JSONResponse:
    """Shutdown the control center server.

    This stops the supervisor/control center process itself.
    Optionally stops all running orchestrators first.

    JSON body (optional):
        stop_orchestrators: bool - If True, stop all running orchestrators first
        force_orchestrators: bool - If True, force stop orchestrators when stopping first
    """
    from ..infra.repo_registry import list_repos

    sv = deps.get_supervisor()
    client_host = request.client.host if request.client else "unknown"
    stop_orchestrators, force_orchestrators, graceful_timeout_seconds = await _parse_shutdown_request_body(request)
    begin_result = shutdown_state.begin_global_shutdown_operation(
        stop_orchestrators=stop_orchestrators,
        force_orchestrators=force_orchestrators,
        graceful_timeout_seconds=graceful_timeout_seconds,
    )
    if isinstance(begin_result, shutdown_state.GlobalShutdownConflict):
        return JSONResponse(
            {
                "error": "shutdown_in_progress",
                "detail": "Global shutdown is already in progress.",
                "operation_id": begin_result.operation_id,
            },
            status_code=409,
        )
    global_op_id, superseded_engine_shutdowns = begin_result

    logger.info(
        "Shutdown requested (force): source=web_ui, client=%s, stop_orchestrators=%s, force_orchestrators=%s, pid=%d",
        client_host,
        stop_orchestrators,
        force_orchestrators,
        os.getpid(),
    )

    if not stop_orchestrators:
        shutdown_state.mark_global_shutdown_completed_without_orchestrators()
        deps.schedule_control_center_exit()
        return _shutdown_started_response(
            operation_id=global_op_id,
            superseded_engine_shutdowns=superseded_engine_shutdowns,
            graceful_timeout_seconds=graceful_timeout_seconds,
        )

    _start_global_shutdown_worker(
        operation_id=global_op_id,
        supervisor=sv,
        list_repos_fn=list_repos,
        schedule_control_center_exit=deps.schedule_control_center_exit,
    )
    return _shutdown_started_response(
        operation_id=global_op_id,
        superseded_engine_shutdowns=superseded_engine_shutdowns,
        graceful_timeout_seconds=graceful_timeout_seconds,
    )


async def _parse_shutdown_request_body(request: Request) -> tuple[bool, bool, int]:
    stop_orchestrators = False
    force_orchestrators = False
    graceful_timeout_seconds = DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return stop_orchestrators, force_orchestrators, graceful_timeout_seconds

    stop_orchestrators = bool(body.get("stop_orchestrators", False))
    force_orchestrators = bool(body.get("force_orchestrators", False))
    graceful_timeout_seconds = shutdown_state.coerce_graceful_timeout_seconds(
        body.get("graceful_timeout_seconds"),
    )
    return stop_orchestrators, force_orchestrators, graceful_timeout_seconds


def _shutdown_started_response(
    *,
    operation_id: str,
    superseded_engine_shutdowns: list[str],
    graceful_timeout_seconds: int,
) -> JSONResponse:
    return JSONResponse({
        "status": "shutting_down",
        "stopped_orchestrators": [],
        "superseded_engine_shutdowns": superseded_engine_shutdowns,
        "graceful_timeout_seconds": graceful_timeout_seconds,
        "operation_id": operation_id,
    })


def _start_global_shutdown_worker(
    *,
    operation_id: str,
    supervisor: SupervisorOps,
    list_repos_fn: Callable[[], Sequence[_RepoRecord]],
    schedule_control_center_exit: Callable[[], None],
) -> None:
    def _worker() -> None:
        _run_global_shutdown_worker(
            operation_id=operation_id,
            supervisor=supervisor,
            list_repos_fn=list_repos_fn,
            schedule_control_center_exit=schedule_control_center_exit,
        )

    threading.Thread(target=_worker, daemon=True).start()


def _run_global_shutdown_worker(
    *,
    operation_id: str,
    supervisor: SupervisorOps,
    list_repos_fn: Callable[[], Sequence[_RepoRecord]],
    schedule_control_center_exit: Callable[[], None],
) -> None:
    stopped_repos: list[str] = []
    failed_repos: list[str] = []
    try:
        repos = list_repos_fn()
        shutdown_state.set_shutdown_total_repos(operation_id=operation_id, total_repos=len(repos))
        for repo in repos:
            result = _process_shutdown_repo(
                operation_id=operation_id,
                repo_path=repo.path,
                supervisor=supervisor,
            )
            if result == "aborted":
                shutdown_state.record_shutdown_abort(
                    operation_id=operation_id,
                    stopped_repos=stopped_repos,
                    failed_repos=failed_repos,
                )
                return
            if result == "stopped":
                stopped_repos.append(repo.path)
            elif result == "failed":
                failed_repos.append(repo.path)
            shutdown_state.increment_shutdown_completed_repos(operation_id=operation_id)
        should_exit = shutdown_state.record_shutdown_completion(
            operation_id=operation_id,
            stopped_repos=stopped_repos,
            failed_repos=failed_repos,
        )
        if should_exit:
            schedule_control_center_exit()
    except Exception:
        logger.exception("Global shutdown worker failed")
        shutdown_state.record_shutdown_failure(operation_id=operation_id)


def _process_shutdown_repo(*, operation_id: str, repo_path: str, supervisor: SupervisorOps) -> ShutdownRepoResult:
    path = Path(repo_path)
    if not path.exists():
        return "skipped"

    stop_policy = shutdown_state.begin_shutdown_repo_stop(
        operation_id=operation_id,
        repo_path=repo_path,
    )
    if stop_policy is None:
        return "aborted"
    initial_policy = stop_policy.snapshot()
    if initial_policy.abort:
        return "aborted"

    status_info = supervisor.status(path)
    if status_info.state != "running":
        return "skipped"
    logger.info("Stopping orchestrator for %s before shutdown", repo_path)
    try:
        stopped_count = supervisor.stop_all_instances(
            path,
            force=initial_policy.force,
            reason="control-center global shutdown",
            actor="control-center.global-shutdown",
            graceful_timeout_seconds=initial_policy.graceful_timeout_seconds,
            force_if_graceful_fails=True,
            stop_policy=stop_policy,
        )
    except StopAborted:
        return "aborted"
    return "stopped" if stopped_count > 0 else "failed"


@control_shutdown_router.get("/control/shutdown/state")
async def get_shutdown_state() -> JSONResponse:
    """Return current shutdown operation state for UI feedback."""
    return JSONResponse(shutdown_state.snapshot_shutdown_ops())


@control_shutdown_router.post("/control/shutdown/abort")
async def shutdown_abort() -> JSONResponse:
    """Request abort of an in-progress global shutdown operation."""
    if not shutdown_state.request_shutdown_abort():
        return JSONResponse(
            {"error": "no_shutdown_in_progress", "detail": "No global shutdown is in progress."},
            status_code=409,
        )
    return JSONResponse({"status": "abort_requested"})


@control_shutdown_router.post("/control/shutdown/update")
async def shutdown_update(request: Request) -> JSONResponse:
    """Update timeout/force policy for an in-progress global shutdown."""
    body: dict[str, object] = {}
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            body = payload
    except json.JSONDecodeError:
        body = {}

    timeout_seconds = shutdown_state.coerce_graceful_timeout_seconds(body.get("graceful_timeout_seconds"))
    force_override = bool(body.get("force_orchestrators", False)) if "force_orchestrators" in body else None
    update = shutdown_state.update_shutdown_policy(
        graceful_timeout_seconds=timeout_seconds,
        force_orchestrators=force_override,
    )
    if update is None:
        return JSONResponse(
            {"error": "no_shutdown_in_progress", "detail": "No global shutdown is in progress."},
            status_code=409,
        )

    return JSONResponse(
        {
            "status": "updated",
            "graceful_timeout_seconds": update.graceful_timeout_seconds,
            "force_orchestrators": update.force_orchestrators,
        }
    )


@control_shutdown_router.post("/control/shutdown/force")
async def shutdown_force_now() -> JSONResponse:
    """Request force escalation for an in-progress global shutdown."""
    if not shutdown_state.request_shutdown_force_now():
        return JSONResponse(
            {"error": "no_shutdown_in_progress", "detail": "No global shutdown is in progress."},
            status_code=409,
        )
    return JSONResponse({"status": "force_requested"})
