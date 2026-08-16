"""Control Center E2E diagnosis, triage, and issue-management routes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .control_api_e2e_support import (
    ControlApiE2EDependencies,
    ControlApiE2EDependency,
)
from .control_api_e2e_issue_creation import create_e2e_sub_issues

logger = logging.getLogger(__name__)

control_e2e_triage_router = APIRouter()


@control_e2e_triage_router.post("/control/e2e/diagnosis/{run_id}/issue")
async def create_e2e_diagnostic_issue(
    request: Request,
    run_id: int,
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Create a GitHub issue for diagnosing E2E test failures."""
    from ..infra.e2e_db import E2EDB
    from ..infra.e2e_run_diagnosis import (
        create_e2e_run_diagnosis,
        generate_diagnostic_issue_body,
        write_e2e_diagnostic,
    )

    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        body = {}

    agent = body.get("agent", "").strip()
    if not agent:
        return JSONResponse({"error": "Agent label is required"}, status_code=400)

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

        diagnostic_ref = write_e2e_diagnostic(validated_root, diagnosis)
        title = f"E2E Test Failures - Run #{run_id} ({diagnosis.failed_count} failures)"
        issue_body = generate_diagnostic_issue_body(diagnosis, diagnostic_ref)
        labels = [agent, "e2e-failure", "bug"]

        result = orchestrator.repository_host.create_issue(
            title=title,
            body=issue_body,
            labels=labels,
        )
        if result is None:
            return JSONResponse({"error": "Failed to create issue"}, status_code=500)

        return JSONResponse(
            {
                "status": "created",
                "issue_number": result.get("number"),
                "url": result.get("html_url"),
                "diagnostic_file": diagnostic_ref.relative_path if diagnostic_ref else None,
            },
        )
    except Exception as exc:
        logger.exception("Failed to create E2E diagnostic issue: %s", exc)
        return JSONResponse(
            {"error": "issue_creation_error", "detail": str(exc)},
            status_code=500,
        )


def _build_issue_status(
    deps: ControlApiE2EDependencies,
    run_issue: Any,
    db: Any,
) -> dict:
    """Build issue status dict for triage response."""
    if not run_issue:
        return {
            "parent_issue_url": None,
            "parent_issue_closed": False,
            "sub_issues": [],
            "sub_issues_summary": {"total": 0, "resolved": 0},
        }

    orchestrator = deps.get_orchestrator()
    repo = orchestrator.config.repo if orchestrator else None
    parent_issue_url = (
        f"https://github.com/{repo}/issues/{run_issue.github_issue_number}"
        if repo
        else None
    )
    parent_issue_closed = run_issue.closed_at is not None

    sub_issues = []
    sub_issues_summary = {"total": 0, "resolved": 0}

    failure_issues = db.get_failure_issues_for_parent(run_issue.github_issue_number)
    for failure_issue in failure_issues:
        is_resolved = failure_issue.resolved_at is not None
        sub_issues.append(
            {
                "issue_number": failure_issue.github_issue_number,
                "nodeid": failure_issue.nodeid,
                "resolved": is_resolved,
                "resolution": failure_issue.resolution,
                "url": (
                    f"https://github.com/{repo}/issues/{failure_issue.github_issue_number}"
                    if repo
                    else None
                ),
            },
        )
        sub_issues_summary["total"] += 1
        if is_resolved:
            sub_issues_summary["resolved"] += 1

    return {
        "parent_issue_url": parent_issue_url,
        "parent_issue_closed": parent_issue_closed,
        "sub_issues": sub_issues,
        "sub_issues_summary": sub_issues_summary,
    }


@control_e2e_triage_router.get("/control/e2e/triage/{run_id}")
async def e2e_triage_data(
    run_id: int,
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
    config_name: str = Query(...),
    mode: str = Query("default"),
) -> JSONResponse:
    """Get triage data for an E2E run."""
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

    e2e_config = deps.load_config(validated_root, config_name, mode).e2e
    try:
        db = E2EDB(db_path)
        run = db.get_run(run_id)
        if run is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )

        failed_results = db.get_failed_tests(run_id)
        run_issue = db.get_run_issue(run_id)

        flake_threshold = e2e_config.flake_threshold
        flake_window = e2e_config.flake_window_runs

        failures = []
        for result in failed_results:
            existing = db.find_open_failure_issue(result.nodeid)
            stability = db.get_test_stability(
                result.nodeid,
                window_runs=flake_window,
                flake_threshold_percent=float(flake_threshold),
            )
            failures.append(
                {
                    "nodeid": result.nodeid,
                    "longrepr": result.longrepr,
                    "duration_seconds": result.duration_seconds,
                    "existing_issue": existing.to_dict() if existing else None,
                    "flake_count": stability.flip_count,
                    "flip_count": stability.flip_count,
                    "flip_rate": stability.flip_rate,
                    "flip_rate_percent": stability.flip_rate_percent,
                    "category": stability.category,
                    "is_likely_flaky": stability.is_likely_flaky,
                },
            )

        issue_status = _build_issue_status(deps, run_issue, db)

        return JSONResponse(
            {
                "run": run.to_dict(),
                "failures": failures,
                "has_parent_issue": run_issue is not None,
                "parent_issue_number": run_issue.github_issue_number if run_issue else None,
                **issue_status,
                "flake_threshold": flake_threshold,
            },
        )
    except Exception as exc:
        logger.exception("Failed to get triage data: %s", exc)
        return JSONResponse(
            {"error": "triage_error", "detail": str(exc)},
            status_code=500,
        )


def _extract_test_log_excerpt(log_path: str | None, nodeid: str) -> str | None:
    """Extract log lines relevant to a specific test."""
    if not log_path:
        return None

    from ..infra.e2e_run_diagnosis import _read_log_content

    log_exists, log_content = _read_log_content(log_path)
    if not log_exists or not log_content:
        return None

    short_name = nodeid.split("::")[-1]
    lines = log_content.split("\n")
    relevant_lines = []
    in_test = False

    for line in lines:
        if short_name in line or nodeid in line:
            in_test = True
        if in_test:
            relevant_lines.append(line)
            if len(relevant_lines) > 100:
                break

    return "\n".join(relevant_lines) if relevant_lines else None


def _calculate_history_summary(history: list[dict]) -> dict:
    """Calculate pass/fail summary from test history."""
    if not history:
        return {"total": 0, "passed": 0, "failed": 0, "pass_rate": None}

    passed = sum(1 for entry in history if entry["outcome"] == "passed")
    failed = sum(1 for entry in history if entry["outcome"] in ("failed", "error"))
    total = len(history)
    pass_rate = passed / total if total > 0 else None

    return {"total": total, "passed": passed, "failed": failed, "pass_rate": pass_rate}


@control_e2e_triage_router.get("/control/e2e/test/{run_id}")
async def e2e_test_detail(
    run_id: int,
    deps: ControlApiE2EDependency,
    nodeid: str = Query(...),
    repo_root: str = Query(...),
    config_name: str = Query(...),
    mode: str = Query("default"),
) -> JSONResponse:
    """Get detailed information for a single test failure."""
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

    e2e_config = deps.load_config(validated_root, config_name, mode).e2e
    try:
        db = E2EDB(db_path)
        run = db.get_run(run_id)
        if run is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )

        test_result = db.get_test_result(run_id, nodeid)
        if test_result is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Test {nodeid} not found in run {run_id}"},
                status_code=404,
            )

        stability = db.get_test_stability(
            nodeid,
            window_runs=e2e_config.flake_window_runs,
            flake_threshold_percent=float(e2e_config.flake_threshold),
        )

        existing_issue = db.find_open_failure_issue(nodeid)
        history = db.get_test_history(nodeid, limit=10)
        history_data = [
            {"run_id": item["run_id"], "outcome": item["outcome"], "timestamp": item["started_at"]}
            for item in history
        ]

        return JSONResponse(
            {
                "test": {
                    "nodeid": test_result.nodeid,
                    "outcome": test_result.outcome,
                    "longrepr": test_result.longrepr,
                    "duration_seconds": test_result.duration_seconds,
                    "retry_outcome": test_result.retry_outcome,
                },
                "run": {
                    "id": run.id,
                    "status": run.status,
                    "started_at": run.started_at,
                    "commit_sha": run.commit_sha,
                    "branch": run.branch,
                },
                "history": history_data,
                "history_summary": _calculate_history_summary(history),
                "flake_count": stability.flip_count,
                "flip_count": stability.flip_count,
                "flip_rate": stability.flip_rate,
                "flip_rate_percent": stability.flip_rate_percent,
                "category": stability.category,
                "is_likely_flaky": stability.is_likely_flaky,
                "existing_issue": existing_issue.to_dict() if existing_issue else None,
                "log_excerpt": _extract_test_log_excerpt(run.log_path, nodeid),
            },
        )
    except Exception as exc:
        logger.exception("Failed to get test detail: %s", exc)
        return JSONResponse(
            {"error": "test_detail_error", "detail": str(exc)},
            status_code=500,
        )


def _validate_e2e_create_request(
    deps: ControlApiE2EDependencies,
    nodeids: list,
    agent: str,
    repo_root: str,
) -> JSONResponse | Path:
    """Validate request params for E2E issue creation."""
    if not nodeids:
        return JSONResponse({"error": "No test failures selected"}, status_code=400)
    if not agent:
        return JSONResponse({"error": "Agent label is required"}, status_code=400)

    validated_root = deps.validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )
    return db_path


@control_e2e_triage_router.post("/control/e2e/create-issues/{run_id}")
async def e2e_create_issues(
    request: Request,
    run_id: int,
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Create GitHub issues from E2E test failures."""
    from ..infra.e2e_db import E2EDB

    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    nodeids = body.get("nodeids", [])
    agent = body.get("agent", "").strip()

    validation_result = _validate_e2e_create_request(deps, nodeids, agent, repo_root)
    if isinstance(validation_result, JSONResponse):
        return validation_result
    db_path = validation_result

    try:
        db = E2EDB(db_path)
        run = db.get_run(run_id)
        if run is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )

        existing_run_issue = db.get_run_issue(run_id)
        if existing_run_issue:
            return JSONResponse(
                {
                    "error": "Issues already created for this run",
                    "parent_issue_number": existing_run_issue.github_issue_number,
                },
                status_code=409,
            )

        tracker = orchestrator.deps.e2e_issue_tracker
        parent_issue = tracker.create_run_issue(
            run=run,
            failed_count=len(nodeids),
            labels=["e2e:run"],
        )
        if parent_issue is None:
            return JSONResponse({"error": "Failed to create parent issue"}, status_code=500)

        db.record_run_issue(run_id, parent_issue.issue_number)

        failed_results = db.get_failed_tests(run_id)
        results_by_nodeid = {result.nodeid: result for result in failed_results}
        sub_issues = create_e2e_sub_issues(
            tracker,
            parent_issue,
            nodeids,
            results_by_nodeid,
            run,
            db,
            run_id,
            agent,
        )

        logger.info(
            "[e2e-create-issues] Created parent #%d with %d sub-issues for run #%d",
            parent_issue.issue_number,
            len(sub_issues),
            run_id,
        )

        return JSONResponse(
            {
                "status": "created",
                "parent_issue": {
                    "number": parent_issue.issue_number,
                    "url": parent_issue.html_url,
                },
                "sub_issues": sub_issues,
            },
        )
    except Exception as exc:
        logger.exception("Failed to create E2E issues: %s", exc)
        return JSONResponse(
            {"error": "issue_creation_error", "detail": str(exc)},
            status_code=500,
        )


def _sync_close_passing_issues(
    tracker: Any,
    open_issues: list[Any],
    passing_nodeids: set[str],
    run_id: int,
    commit_sha: str,
    db: Any,
) -> tuple[list[dict], set[int]]:
    """Close sub-issues for tests that now pass."""
    closed_issues: list[dict] = []
    parent_issues_to_check: set[int] = set()

    for issue in open_issues:
        if issue.nodeid not in passing_nodeids:
            continue
        comment = (
            f"Test now passing as of run #{run_id} "
            f"(commit `{commit_sha[:12]}`)\n\n"
            f"_Auto-closed by orchestrator._"
        )
        if tracker.close_issue_with_comment(issue.github_issue_number, comment):
            db.resolve_failure_issue(issue.nodeid, "passed")
            closed_issues.append(
                {
                    "number": issue.github_issue_number,
                    "nodeid": issue.nodeid,
                },
            )
            parent_issues_to_check.add(issue.parent_issue_number)
            logger.info(
                "[e2e-sync] Closed issue #%d for passing test: %s",
                issue.github_issue_number,
                issue.nodeid,
            )

    return closed_issues, parent_issues_to_check


def _sync_close_parent_issues(
    tracker: Any,
    parent_issues_to_check: set[int],
    run_id: int,
    db: Any,
) -> list[int]:
    """Close parent issues if all their sub-issues are resolved."""
    closed_parents: list[int] = []
    for parent_number in parent_issues_to_check:
        unresolved = db.get_unresolved_failure_count(parent_number)
        if unresolved != 0:
            continue
        comment = (
            f"All sub-issues resolved as of run #{run_id}\n\n"
            f"_Auto-closed by orchestrator._"
        )
        if tracker.close_issue_with_comment(parent_number, comment):
            closed_parents.append(parent_number)
            logger.info("[e2e-sync] Closed parent issue #%d", parent_number)
    return closed_parents


@control_e2e_triage_router.post("/control/e2e/sync-issues/{run_id}")
async def e2e_sync_issues(
    run_id: int,
    deps: ControlApiE2EDependency,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Sync E2E issue state based on test results from a run."""
    from ..infra.e2e_db import E2EDB

    orchestrator = deps.get_orchestrator()
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

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
        run = db.get_run(run_id)
        if run is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )

        summary = db.get_test_summary(run_id)
        passing_nodeids = {test["nodeid"] for test in summary["passed"]}
        passing_nodeids.update(test["nodeid"] for test in summary["passed_on_retry"])

        tracker = orchestrator.deps.e2e_issue_tracker
        closed_issues, parent_issues_to_check = _sync_close_passing_issues(
            tracker,
            db.get_all_open_failure_issues(),
            passing_nodeids,
            run_id,
            run.commit_sha or "unknown",
            db,
        )
        closed_parents = _sync_close_parent_issues(
            tracker,
            parent_issues_to_check,
            run_id,
            db,
        )

        logger.info(
            "[e2e-sync] Run #%d: closed %d sub-issues, %d parent issues",
            run_id,
            len(closed_issues),
            len(closed_parents),
        )

        return JSONResponse(
            {
                "status": "synced",
                "closed_issues": closed_issues,
                "closed_parent_issues": closed_parents,
            },
        )
    except Exception as exc:
        logger.exception("Failed to sync E2E issues: %s", exc)
        return JSONResponse(
            {"error": "sync_error", "detail": str(exc)},
            status_code=500,
        )


__all__ = ["control_e2e_triage_router"]
