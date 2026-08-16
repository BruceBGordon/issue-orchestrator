"""Control Center E2E run and observability routes."""

from __future__ import annotations

from dataclasses import replace
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import E2ERunTimelinePayload
from .control_api_e2e_support import (
    ControlApiE2EDependencies,
    ControlApiE2EDependency,
)
from .control_api_e2e_issue_creation import create_e2e_sub_issues
from .e2e_affordances import (
    _filter_nest_and_project_agent_events,
    _load_worktree_agent_events,
    collect_issue_affordances,
)
from .timeline_presentation import (
    _build_phase_toc,
    _build_timeline_cycles,
    _promote_e2e_test_event_fields,
)
from .timeline_projection_boundary import TIMELINE_PROJECTION_BOUNDARY_EXCEPTIONS, timeline_projection_endpoint
from ..view_models.lifecycle_projection import project_e2e_suite_lifecycle_container_for_run
from ..view_models.timeline_view import normalize_timeline_view

logger = logging.getLogger(__name__)

control_e2e_runs_router = APIRouter()


def _execution_spec_for_start_request(
    body: dict[str, Any],
    config,
):
    """Resolve the execution spec for a manual start request."""
    execution_spec = config.e2e.execution_spec()
    allow_retry_once = body.get("allow_retry_once", execution_spec.allow_retry_once)
    if execution_spec.runner_kind == "pytest" and body.get("pytest_args") is not None:
        return replace(
            execution_spec,
            pytest_args=tuple(body["pytest_args"]),
            allow_retry_once=allow_retry_once,
        )
    if execution_spec.runner_kind == "command" and body.get("command") is not None:
        return replace(
            execution_spec,
            command=tuple(body["command"]),
            allow_retry_once=allow_retry_once,
        )
    if allow_retry_once != execution_spec.allow_retry_once:
        return replace(execution_spec, allow_retry_once=allow_retry_once)
    return execution_spec


@control_e2e_runs_router.post("/control/e2e/start")
async def e2e_start(
    request: Request,
    deps: ControlApiE2EDependency,
) -> JSONResponse:
    """Start an E2E test run for a repository."""
    from ..infra.e2e_runner import E2EAlreadyRunning, get_e2e_runner_manager

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    config_name = body.get("config_name")
    if not config_name or not isinstance(config_name, str):
        return JSONResponse(
            {"error": "Missing or invalid config_name"},
            status_code=400,
        )

    try:
        config = deps.load_config(repo_root, config_name, body.get("mode", "default"))
    except FileNotFoundError:
        return JSONResponse(
            {"error": "Config not found", "detail": f"Config file not found: {config_name}"},
            status_code=404,
        )

    if not config.e2e.enabled:
        return JSONResponse(
            {"error": "e2e_disabled", "detail": "E2E runner not enabled in config"},
            status_code=400,
        )

    execution_spec = _execution_spec_for_start_request(body, config)
    orchestrator_id = config.orchestrator_id

    runner = get_e2e_runner_manager()

    try:
        orchestrator = deps.get_orchestrator()
        instance_id = orchestrator.deps.services.instance_id if orchestrator else ""
        result = runner.start(
            repo_root=repo_root,
            orchestrator_id=orchestrator_id,
            execution_spec=execution_spec,
            quarantine_file=config.e2e.quarantine_file,
            auto_quarantine=config.e2e.auto_quarantine,
            orchestrator_instance_id=instance_id,
            run_retention_count=config.e2e.run_retention_count,
        )

        try:
            from .web import broadcast_event

            await broadcast_event(
                "e2e.started",
                {
                    "pid": result["pid"],
                    "orchestrator_id": orchestrator_id,
                },
            )
        except Exception as exc:
            logger.debug("Could not broadcast e2e.started event: %s", exc)

        return JSONResponse(
            {
                "status": "started",
                "pid": result["pid"],
                "log_path": result["log_path"],
            },
        )
    except E2EAlreadyRunning as exc:
        return JSONResponse(
            {"error": "already_running", "pid": exc.pid},
            status_code=409,
        )
    except Exception as exc:
        logger.exception("Failed to start E2E: %s", exc)
        return JSONResponse(
            {"error": "start_failed", "detail": str(exc)},
            status_code=500,
        )


@control_e2e_runs_router.post("/control/e2e/stop")
async def e2e_stop(
    request: Request,
    deps: ControlApiE2EDependency,
) -> JSONResponse:
    """Stop a running E2E test."""
    from ..infra.e2e_runner import get_e2e_runner_manager

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = deps.validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    config_name = body.get("config_name")
    if not config_name or not isinstance(config_name, str):
        return JSONResponse(
            {"error": "Missing or invalid config_name"},
            status_code=400,
        )

    try:
        config = deps.load_config(repo_root, config_name, body.get("mode", "default"))
    except FileNotFoundError:
        return JSONResponse(
            {"error": "config_not_found", "detail": f"Config file not found: {config_name}"},
            status_code=400,
        )

    runner = get_e2e_runner_manager()
    stopped = runner.stop(config.orchestrator_id, repo_root)

    if stopped:
        try:
            from .web import broadcast_event

            await broadcast_event(
                "e2e.stopped",
                {"orchestrator_id": config.orchestrator_id},
            )
        except Exception as exc:
            logger.debug("Could not broadcast e2e.stopped event: %s", exc)

    return JSONResponse({"status": "stopped" if stopped else "not_running"})


@control_e2e_runs_router.get("/control/e2e/status")
async def e2e_status(
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
    config_name: str = Query(...), mode: str = Query("default"),
) -> JSONResponse:
    """Get E2E test runner status."""
    from ..infra.e2e_db import E2EDB
    from ..infra.e2e_runner import get_e2e_runner_manager, get_next_run_info

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    try:
        config = deps.load_config(validated_root, config_name, mode)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "config_not_found", "detail": f"Config file not found: {config_name}"},
            status_code=400,
        )

    runner = get_e2e_runner_manager()
    proc_status = runner.status(config.orchestrator_id)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    last_run = None
    next_run = None
    run_obj = None
    signal_score = None
    progress = None
    needs_attention = False
    untriaged_count = 0

    if db_path.exists():
        try:
            db = E2EDB(db_path)
            run_obj = db.latest_run(config.orchestrator_id)
            if run_obj:
                last_run = run_obj.to_dict()
                if run_obj.status == "running":
                    progress = db.get_progress(run_obj.id)
                elif run_obj.status == "failed":
                    untriaged_count = _count_untriaged_failures(db, run_obj.id)
                    needs_attention = untriaged_count > 0
                _auto_create_e2e_issues_if_needed(deps, config, db, run_obj, proc_status)
            signal_score = db.compute_signal_score(config.orchestrator_id)
        except Exception as exc:
            logger.warning("Failed to read E2E DB: %s", exc)

    if config.e2e.enabled:
        next_run = get_next_run_info(config, validated_root, run_obj)

    return JSONResponse(
        {
            "enabled": config.e2e.enabled,
            "running": proc_status["running"],
            "pid": proc_status["pid"],
            "exit_code": proc_status["exit_code"],
            "last_run": last_run,
            "signal_score": signal_score,
            "progress": progress,
            "next_run": next_run,
            "needs_attention": needs_attention,
            "untriaged_count": untriaged_count,
        },
    )


@control_e2e_runs_router.get("/control/e2e/runs")
async def e2e_runs(
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
    config_name: str = Query(...), mode: str = Query("default"),
    limit: int = Query(20, ge=1, le=100),
) -> JSONResponse:
    """List recent E2E runs."""
    from ..infra.e2e_db import E2EDB

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    try:
        config = deps.load_config(validated_root, config_name, mode)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "config_not_found", "detail": f"Config file not found: {config_name}"},
            status_code=400,
        )

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse({"runs": []})

    try:
        db = E2EDB(db_path)
        runs = db.list_runs(config.orchestrator_id, limit=limit)
        return JSONResponse({"runs": [run.to_dict() for run in runs]})
    except Exception as exc:
        logger.exception("Failed to list E2E runs: %s", exc)
        return JSONResponse(
            {"error": "db_error", "detail": str(exc)},
            status_code=500,
        )


@control_e2e_runs_router.get("/control/e2e/run/{run_id}")
async def e2e_run_details(
    run_id: int,
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
    config_name: str = Query(...), mode: str = Query("default"),
    enhanced: bool = Query(
        False,
        description="Use enhanced response with categories and history",
    ),
) -> JSONResponse:
    """Get details of a specific E2E run."""
    from ..infra.e2e_db import E2EDB

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    e2e_config = deps.load_config(validated_root, config_name, mode).e2e if enhanced else None
    try:
        db = E2EDB(db_path)
        if enhanced:
            assert e2e_config is not None
            details = db.run_details_enhanced(
                run_id,
                history_limit=5,
                flake_threshold_percent=float(e2e_config.flake_threshold),
            )
        else:
            details = db.run_details(run_id)

        if details is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )
        return JSONResponse(details)
    except Exception as exc:
        logger.exception("Failed to get E2E run details: %s", exc)
        return JSONResponse(
            {"error": "db_error", "detail": str(exc)},
            status_code=500,
        )


@control_e2e_runs_router.get("/control/e2e/run/{run_id}/timeline", response_model=E2ERunTimelinePayload)
@timeline_projection_endpoint("control_e2e_run_timeline", issue_number_key=None)
async def e2e_run_timeline_endpoint(
    run_id: int,
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
    view: str = Query(
        "user",
        description="Timeline view: user (story), ops, debug, or raw",
    ),
) -> JSONResponse:
    """Get timeline events for a specific E2E run."""
    from ..domain.timeline_key import TimelineKey
    from ..timeline import TimelineStream

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    timeline_db_path = validated_root / ".issue-orchestrator" / "state" / "timeline.sqlite"
    if not timeline_db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "Timeline database not found"},
            status_code=404,
        )

    try:
        from ..execution.timeline_store import read_timeline_records

        store_key = TimelineKey.for_e2e_run(run_id).to_store_key()
        records = read_timeline_records(timeline_db_path, store_key)

        if not records:
            return JSONResponse(
                _validated_e2e_run_timeline_payload(
                    {
                        "events": [],
                        "phase_toc": [],
                        "cycles": [],
                        "issue_affordances": [],
                        "lifecycle": _e2e_run_lifecycle_payload(
                            run_id=run_id,
                            events=[],
                            agent_events=[],
                        ),
                    }
                ),
            )

        e2e_records = [record for record in records if record.event != "e2e.agent_snapshot"]
        snapshot_records = [record for record in records if record.event == "e2e.agent_snapshot"]

        stream = TimelineStream.from_records(store_key, e2e_records)
        e2e_events = [event.to_dict() for event in stream.events]
        _promote_e2e_test_event_fields(
            e2e_events,
            e2e_records,
            run_id=run_id,
            e2e_db_path=validated_root / ".issue-orchestrator" / "e2e.db",
        )
        agent_events = [record.data for record in snapshot_records if isinstance(record.data, dict)]

        if not agent_events:
            agent_events = _load_worktree_agent_events(validated_root, run_id)

        view = normalize_timeline_view(view)
        issue_affordances = collect_issue_affordances(
            agent_events,
            run_id=run_id,
            view=view,
        )
        events = _filter_nest_and_project_agent_events(
            e2e_events,
            agent_events,
            view,
            run_id=run_id,
        )

        return JSONResponse(
            _validated_e2e_run_timeline_payload(
                {
                    "events": events,
                    "phase_toc": _build_phase_toc(events),
                    "cycles": _build_timeline_cycles(events),
                    "issue_affordances": issue_affordances,
                    "lifecycle": _e2e_run_lifecycle_payload(
                        run_id=run_id,
                        events=events,
                        agent_events=agent_events,
                    ),
                }
            ),
        )
    except TIMELINE_PROJECTION_BOUNDARY_EXCEPTIONS:
        raise
    except Exception as exc:
        logger.exception("Failed to get E2E run timeline: %s", exc)
        return JSONResponse(
            {"error": "db_error", "detail": str(exc)},
            status_code=500,
        )

def _e2e_run_lifecycle_payload(
    *,
    run_id: int,
    events: list[dict[str, Any]],
    agent_events: list[dict[str, Any]],
) -> dict[str, Any]:
    return project_e2e_suite_lifecycle_container_for_run(
        run_id=run_id,
        events=events,
        agent_events=agent_events,
        subject_label="E2E Suite",
    ).model_dump(
        mode="json",
    )

def _validated_e2e_run_timeline_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return E2ERunTimelinePayload.model_validate(payload).model_dump(
        mode="json",
        exclude_unset=True,
    )
@control_e2e_runs_router.get("/control/e2e/logs/{run_id}")
async def e2e_logs(
    run_id: int,
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
    tail: int = Query(500, ge=1, le=10000),
) -> JSONResponse:
    """Get logs for a specific E2E run."""
    from ..infra.e2e_db import E2EDB

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)
        details = db.run_details(run_id)
        if details is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )

        log_path = details["run"].get("log_path")
        if not log_path:
            return JSONResponse(
                {"error": "no_logs", "detail": "No log file for this run"},
                status_code=404,
            )

        log_file = Path(log_path)
        if not log_file.exists():
            return JSONResponse(
                {"error": "log_missing", "detail": f"Log file not found: {log_path}"},
                status_code=404,
            )

        with log_file.open() as handle:
            lines = handle.readlines()
            content = "".join(lines[-tail:])

        return JSONResponse(
            {
                "log_path": str(log_path),
                "total_lines": len(lines),
                "returned_lines": min(tail, len(lines)),
                "content": content,
            },
        )
    except Exception as exc:
        logger.exception("Failed to get E2E logs: %s", exc)
        return JSONResponse(
            {"error": "read_error", "detail": str(exc)},
            status_code=500,
        )


@control_e2e_runs_router.get("/control/e2e/failed/{run_id}")
async def e2e_failed_tests(
    run_id: int,
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Get failed tests from a specific run."""
    from ..infra.e2e_db import E2EDB

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)
        failed = db.get_failed_tests(run_id)
        return JSONResponse({"failed_tests": [test.to_dict() for test in failed]})
    except Exception as exc:
        logger.exception("Failed to get failed tests: %s", exc)
        return JSONResponse(
            {"error": "db_error", "detail": str(exc)},
            status_code=500,
        )


@control_e2e_runs_router.get("/control/e2e/quarantine")
async def e2e_quarantine_list(
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
    config_name: str = Query(...), mode: str = Query("default"),
) -> JSONResponse:
    """Get the quarantine list for a repository."""
    from ..infra.e2e_db import load_quarantine_list

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    e2e_config = deps.load_config(validated_root, config_name, mode).e2e
    quarantine_file = e2e_config.quarantine_file
    quarantine_path = validated_root / quarantine_file
    tests = load_quarantine_list(quarantine_path)

    return JSONResponse(
        {
            "quarantine_file": quarantine_file,
            "tests": sorted(tests),
            "count": len(tests),
            "exists": quarantine_path.exists(),
        },
    )


def _apply_quarantine_changes(
    action: str,
    nodeids: list,
    current_tests: set,
) -> tuple[list, list]:
    """Apply add or remove actions to the quarantine set."""
    added: list = []
    removed: list = []
    if action == "add":
        for nodeid in nodeids:
            if nodeid not in current_tests:
                current_tests.add(nodeid)
                added.append(nodeid)
    else:
        for nodeid in nodeids:
            if nodeid in current_tests:
                current_tests.remove(nodeid)
                removed.append(nodeid)
    return added, removed


@control_e2e_runs_router.post("/control/e2e/quarantine")
async def e2e_quarantine_modify(
    request: Request,
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
    config_name: str = Query(...), mode: str = Query("default"),
) -> JSONResponse:
    """Add or remove tests from the quarantine list."""
    from ..infra.e2e_db import load_quarantine_list, save_quarantine_list

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    action = body.get("action", "").strip()
    nodeids = body.get("nodeids", [])

    if action not in {"add", "remove"}:
        return JSONResponse({"error": "action must be 'add' or 'remove'"}, status_code=400)
    if not nodeids:
        return JSONResponse({"error": "nodeids is required"}, status_code=400)

    e2e_config = deps.load_config(validated_root, config_name, mode).e2e
    quarantine_file = e2e_config.quarantine_file
    quarantine_path = validated_root / quarantine_file
    current_tests = load_quarantine_list(quarantine_path)

    added, removed = _apply_quarantine_changes(action, nodeids, current_tests)
    save_quarantine_list(quarantine_path, current_tests)

    logger.info(
        "[quarantine] Modified quarantine list: added=%d, removed=%d",
        len(added),
        len(removed),
    )

    return JSONResponse(
        {
            "quarantine_file": quarantine_file,
            "tests": sorted(current_tests),
            "count": len(current_tests),
            "added": added,
            "removed": removed,
        },
    )


@control_e2e_runs_router.get("/control/e2e/stats")
async def e2e_stats(
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
    config_name: str = Query(...), mode: str = Query("default"),
) -> JSONResponse:
    """Get E2E statistics for the stats modal."""
    from ..infra.e2e_db import E2EDB, load_quarantine_list
    from ..infra.e2e_runner import get_next_run_info

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    try:
        config = deps.load_config(validated_root, config_name, mode)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "config_not_found", "detail": f"Config file not found: {config_name}"},
            status_code=400,
        )

    e2e_config = config.e2e
    flake_window = e2e_config.flake_window_runs
    flake_threshold = float(e2e_config.flake_threshold)

    pass_rate = None
    pass_rate_percent = None
    runs_analyzed = 0
    flaky_count = 0
    next_check = None
    next_check_reason = None

    quarantine_path = validated_root / e2e_config.quarantine_file
    quarantine_count = len(load_quarantine_list(quarantine_path))

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if db_path.exists():
        db = E2EDB(db_path)
        signal_score = db.compute_signal_score(config.orchestrator_id)
        if signal_score:
            pass_rate = signal_score.get("pass_rate")
            if pass_rate is not None:
                pass_rate_percent = int(pass_rate * 100)
            runs_analyzed = signal_score.get("runs_analyzed", 0)

        all_stability = db.get_all_test_stability(
            window_runs=flake_window,
            flake_threshold_percent=flake_threshold,
        )
        flaky_count = sum(1 for stability in all_stability if stability.is_likely_flaky)

        run_obj = db.latest_run(config.orchestrator_id)
        if config.e2e.enabled:
            next_info = get_next_run_info(config, validated_root, run_obj)
            if next_info:
                next_check = next_info.get("scheduled_time")
                next_check_reason = next_info.get("reason")

    return JSONResponse(
        {
            "pass_rate": pass_rate,
            "pass_rate_percent": pass_rate_percent,
            "runs_analyzed": runs_analyzed,
            "flaky_count": flaky_count,
            "quarantine_count": quarantine_count,
            "next_check": next_check,
            "next_check_reason": next_check_reason,
            "flake_window_runs": flake_window,
        },
    )


@control_e2e_runs_router.get("/control/e2e/flaky-tests")
async def e2e_flaky_tests(
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
    config_name: str = Query(...), mode: str = Query("default"),
    threshold: int = Query(default=20),
    window: int = Query(default=10),
) -> JSONResponse:
    """Get tests that exhibit flaky behavior via flip-rate analysis."""
    from ..infra.e2e_db import E2EDB, load_quarantine_list

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    e2e_config = deps.load_config(validated_root, config_name, mode).e2e
    quarantine_file = e2e_config.quarantine_file
    quarantined = load_quarantine_list(validated_root / quarantine_file)

    db = E2EDB(db_path)
    all_stability = db.get_all_test_stability(
        window_runs=window,
        flake_threshold_percent=float(threshold),
    )

    flaky_tests = []
    for stability in all_stability:
        if not stability.is_likely_flaky:
            continue
        entry = stability.to_dict()
        entry["is_quarantined"] = stability.nodeid in quarantined
        entry["flake_count"] = stability.flip_count
        flaky_tests.append(entry)

    return JSONResponse(
        {
            "flaky_tests": flaky_tests,
            "threshold": threshold,
            "window": window,
            "quarantine_file": quarantine_file,
        },
    )


@control_e2e_runs_router.get("/control/e2e/summary/{run_id}")
async def e2e_test_summary(
    run_id: int,
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Get comprehensive test summary for a run."""
    from ..infra.e2e_db import E2EDB

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)
        return JSONResponse(db.get_test_summary(run_id))
    except Exception as exc:
        logger.exception("Failed to get test summary: %s", exc)
        return JSONResponse(
            {"error": "db_error", "detail": str(exc)},
            status_code=500,
        )


@control_e2e_runs_router.get("/control/e2e/diagnosis/{run_id}")
async def e2e_run_diagnosis(
    run_id: int,
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Get comprehensive diagnosis for an E2E run failure."""
    from ..infra.e2e_db import E2EDB
    from ..infra.e2e_run_diagnosis import create_e2e_run_diagnosis

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)
        diagnosis = create_e2e_run_diagnosis(run_id, db)
        if diagnosis is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )
        return JSONResponse(diagnosis.to_dict())
    except Exception as exc:
        logger.exception("Failed to create E2E diagnosis: %s", exc)
        return JSONResponse(
            {"error": "diagnosis_error", "detail": str(exc)},
            status_code=500,
        )


def _count_untriaged_failures(db: object, run_id: int) -> int:
    """Count failures without corresponding open issues."""
    failed_tests = db.get_failed_tests(run_id)  # type: ignore[attr-defined]
    count = 0
    for result in failed_tests:
        existing = db.find_open_failure_issue(result.nodeid)  # type: ignore[attr-defined]
        if not existing:
            count += 1
    return count


def _auto_create_e2e_issues_if_needed(
    deps: ControlApiE2EDependencies,
    config: Any,
    db: Any,
    run: Any,
    proc_status: dict,
) -> None:
    """Auto-create E2E failure issues when enabled and not already created."""
    if not config.e2e.auto_create_issues:
        return
    if run is None or run.status != "failed":
        return
    if proc_status.get("running"):
        return

    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        logger.warning("[e2e-auto-issues] Orchestrator not running; cannot create issues")
        return

    existing_run_issue = db.get_run_issue(run.id)
    if existing_run_issue:
        return

    failed_results = db.get_failed_tests(run.id)
    if not failed_results:
        return

    try:
        tracker = orchestrator.deps.e2e_issue_tracker
        parent_issue = tracker.create_run_issue(
            run=run,
            failed_count=len(failed_results),
            labels=["e2e:run"],
        )
        if parent_issue is None:
            return

        db.record_run_issue(run.id, parent_issue.issue_number)

        results_by_nodeid = {result.nodeid: result for result in failed_results}
        create_e2e_sub_issues(
            tracker,
            parent_issue,
            list(results_by_nodeid.keys()),
            results_by_nodeid,
            run,
            db,
            run.id,
            config.e2e.issue_agent_label,
        )
        logger.info(
            "[e2e-auto-issues] Created parent #%d with %d sub-issues for run #%d",
            parent_issue.issue_number,
            len(results_by_nodeid),
            run.id,
        )
    except Exception as exc:
        logger.exception("[e2e-auto-issues] Failed to auto-create issues: %s", exc)


__all__ = ["control_e2e_runs_router"]
