from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..contracts.public import StackDependencyGateView
from ..contracts.ui_openapi_models import (
    E2ERunDetailPayload,
    E2ETestOutputPayload,
    RecentE2ERunsPayload,
    TestCaseIssueLinkPayload,
    E2ERunResultCategoriesPayload,
    IssueDetailPayload,
)
from ..domain.models import BLOCKED_HISTORY_STATUSES, DONE_HISTORY_STATUSES
from ..infra.timeline_trace import is_timeline_trace_enabled
from ..view_models.dashboard import (
    issue_url_for,
    stack_dependency_payload,
    stack_dependency_view,
)
from ..view_models.issue_detail import IssueStoryContext, build_issue_detail_view_model
from ..view_models.rework_status import resolve_queued_rework
from ..view_models.timeline_view import normalize_timeline_view
from ..view_models.lifecycle_projection import (
    project_dashboard_lifecycle_container,
    project_e2e_suite_lifecycle_container_for_run,
)
from .e2e_affordances import (
    _attach_issue_numbers_to_test_windows,
    collect_issue_affordances,
)
from .timeline_presentation import (
    _build_phase_toc,
    _build_timeline_cycles,
    _decorate_timeline_events,
    _filter_timeline_events,
    _promote_e2e_test_event_fields,
    _retain_semantic_timeline_events,
)
from .timeline_projection_boundary import timeline_projection_endpoint
from .web_session_context import (
    WebOrchestratorDependency,
    issue_title_for,
    resolve_issue_session_context,
)

logger = logging.getLogger(__name__)

web_issue_detail_router = APIRouter()


class E2ERunDatabaseNotFoundError(FileNotFoundError):
    """The E2E database required to build run detail is unavailable."""


class E2ERunRecordNotFoundError(LookupError):
    """The requested E2E run does not exist in the database."""


def _current_run_validation_diagnostic(
    orchestrator: Any,
    issue_number: int,
) -> dict[str, Any] | None:
    """Return a focused diagnostic for the latest/current run's validation state."""
    context = resolve_issue_session_context(orchestrator, issue_number)
    run_dir = context.run_dir
    if run_dir is None:
        return None
    # Use the shared config-aware loader so this path and the
    # `/api/dialog/validation-failure/` route can never disagree on
    # whether structured JUnit cases are surfaced.
    from ..execution.validation_failure_summary import (
        load_validation_failure_summary_with_config,
    )
    # `include_passed=True` so the diagnostic surfaces for passed runs as
    # well — users want to inspect the per-test JUnit results regardless
    # of outcome, not just to triage failures. The dialog endpoint
    # already passes the same flag (web_diagnostics_routes.py); both
    # surfaces share `load_validation_failure_summary_with_config` and
    # so cannot disagree on whether structured cases are surfaced.
    summary = load_validation_failure_summary_with_config(
        run_dir,
        config=orchestrator.config if orchestrator else None,
        include_passed=True,
    )
    if summary is None:
        return None
    state = (
        "validation_passed"
        if summary.status == "passed"
        else "validation_failed"
    )
    return {
        "state": state,
        "run_dir": str(run_dir),
        "session_name": context.session_name,
        "reason": summary.reason,
        "suite": summary.suite,
        "command": summary.command,
        "exit_code": summary.exit_code,
        "failed_tests": list(summary.failed_tests),
        "failed_tests_preview": list(summary.failed_tests[:3]),
        "validation_record_path": summary.validation_record_path,
        "validation_stderr": summary.validation_stderr_path,
        "validation_stdout": summary.validation_stdout_path,
        "junit_cases": [_validation_case_to_public(case) for case in summary.junit_cases],
    }


def _validation_case_to_public(case: Any) -> dict[str, Any]:
    """Project a JUnitCaseResult into the framework-neutral TestCaseResultPayload shape.

    Validation runs don't track flake history, retries, or linked GitHub
    issues — those are E2E concepts. Stub the orchestrator-overlay fields out
    and let the dashboard renderer (which is also used by E2E) skip the
    E2E-specific affordances when these fields are empty.
    """
    outcome = case.outcome
    # Map outcome to a category the shared filter chips understand.
    if outcome == "passed":
        category = "passed"
    elif outcome == "skipped":
        category = "skipped"
    else:
        # Distinct from the E2E "untriaged" category — issue-session
        # validation has no flake-tracking, no GitHub-issue affordances. The
        # shared filter renderer maps any non-recognised category into the
        # failed-result filter group, and the action renderer only shows
        # E2E-specific buttons for E2E-recognised categories.
        category = "failed"
    return {
        "nodeid": case.case_id,
        "case_id": case.case_id,
        "label": case.display_name,
        "display_name": case.display_name,
        "suite_name": case.suite_name,
        "result_source": "junit",
        "outcome": outcome,
        "duration_seconds": case.duration_seconds,
        "longrepr": case.failure_details,
        "failure_summary": case.failure_summary,
        "retry_outcome": None,
        "is_quarantined": False,
        "updated_at": "",
        "history": [],
        "existing_issue": None,
        "category": category,
        "result_category": category,
        "flip_rate": 0.0,
        "flip_rate_percent": 0.0,
        "is_likely_flaky": False,
        # Issue-session validation does not yet have a lazy captured-output
        # endpoint in this shared TestCaseResultPayload path. Keep these
        # false instead of advertising rows the viewer cannot fetch.
        "captured_output": _empty_captured_output_availability(),
    }


def _apply_issue_detail_actions(
    orchestrator: Any,
    issue_number: int,
    payload: dict[str, Any],
) -> None:
    if orchestrator and orchestrator.deps.publish_recovery.can_retry_publish(
        issue_number,
        orchestrator.state,
    ):
        actions = payload.get("actions")
        if isinstance(actions, list):
            actions.insert(0, {"id": "retry_publish", "label": "Retry Publish"})


def _apply_issue_detail_run_diagnostic(
    payload: dict[str, Any], run_diagnostic: dict[str, Any]
) -> None:
    actions = payload.get("actions")
    if isinstance(actions, list):
        actions.insert(
            0,
            {
                "id": "open_validation_failure",
                "label": "Validation Details",
                "run_dir": run_diagnostic.get("run_dir"),
            },
        )
    summary = payload.get("summary")
    if isinstance(summary, dict):
        summary["run_diagnostic"] = run_diagnostic

    # Status explanation differs by outcome: a failed run is a problem
    # the user wants to drill into; a passed run is informational and
    # shouldn't pretend to be a failure.
    #
    # The "N test case(s) available" line was dropped — per-cycle validation
    # badges in the timeline are the discoverable entry point now, and the
    # count was redundant once the modal exposes it directly.
    if run_diagnostic.get("state") == "validation_passed":
        payload["status_explanation"] = "Current run passed validation."
        return

    failed_tests = run_diagnostic.get("failed_tests")
    if isinstance(failed_tests, list) and failed_tests:
        payload["status_explanation"] = (
            f"Current run failed validation: {len(failed_tests)} failing test(s) in "
            f"{run_diagnostic.get('command') or 'validation'}"
        )
        return
    payload["status_explanation"] = (
        f"Current run failed validation: {run_diagnostic.get('reason', 'validation failed')}"
    )


def _e2e_run_execution_details(orchestrator: Any, run_id: int) -> dict[str, Any]:
    from ..infra.e2e_db import E2EDB

    db_path = orchestrator.config.repo_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        raise E2ERunDatabaseNotFoundError(f"E2E database not found for run {run_id}")

    db = E2EDB(db_path)
    details = db.run_details_enhanced(
        run_id,
        history_limit=5,
        flake_threshold_percent=float(orchestrator.config.e2e.flake_threshold),
    )
    if details is None:
        raise E2ERunRecordNotFoundError(f"E2E run {run_id} not found")
    return details


def _canonical_e2e_command(run: dict[str, Any]) -> list[str]:
    raw_command = run.get("command")
    if isinstance(raw_command, list) and all(isinstance(item, str) for item in raw_command):
        return list(raw_command)
    raw_pytest_args = run.get("pytest_args")
    if isinstance(raw_pytest_args, list) and all(isinstance(item, str) for item in raw_pytest_args):
        return ["pytest", *raw_pytest_args]
    return []


def _public_e2e_run_payload(run: dict[str, Any], run_id: int) -> dict[str, Any]:
    log_path = run.get("log_path")
    return {
        "id": run_id,
        "orchestrator_id": str(run.get("orchestrator_id") or ""),
        "started_at": str(run.get("started_at") or ""),
        "finished_at": run.get("finished_at"),
        "status": str(run.get("status") or "unknown"),
        "exit_code": run.get("exit_code"),
        "duration_seconds": run.get("duration_seconds"),
        "pytest_args": list(run.get("pytest_args") or []),
        "command": _canonical_e2e_command(run),
        "runner_kind": str(run.get("runner_kind") or "unknown"),
        "commit_sha": run.get("commit_sha"),
        "branch": run.get("branch"),
        "log_path": log_path,
        "log_excerpt": _read_e2e_log_excerpt(log_path),
        "artifacts_dir": run.get("artifacts_dir"),
        "total_tests": run.get("total_tests"),
        "current_test": run.get("current_test"),
    }


# Tail size for the run-level log excerpt surfaced in the dashboard.
# The worker process captures both stdout and stderr into a single file
# (``e2e_runner`` sets ``stderr=subprocess.STDOUT``), so this is the only
# run-level captured channel the dashboard can show. Cap at ~32 KB so a
# verbose pytest run cannot bloat the JSON payload; the full log is still
# linked via the "Raw Output" artifact row.
_E2E_LOG_EXCERPT_BYTE_CAP = 32 * 1024
_E2E_LOG_EXCERPT_LINE_CAP = 200


def _read_e2e_log_excerpt(log_path: Any) -> list[str]:
    if not isinstance(log_path, str) or not log_path.strip():
        return []
    try:
        path = Path(log_path)
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if size > _E2E_LOG_EXCERPT_BYTE_CAP:
                handle.seek(size - _E2E_LOG_EXCERPT_BYTE_CAP)
                # Drop the partial first line so we never split mid-line.
                handle.readline()
            else:
                handle.seek(0)
            tail_bytes = handle.read()
    except OSError:
        return []
    text = tail_bytes.decode("utf-8", errors="replace")
    # Collapse runs of blank lines to a single blank line. Pytest separates
    # phases with blank lines (collection banner / live log call / summary)
    # and dropping every blank produced a visually dense, structureless
    # excerpt; keeping every blank lets a chatty step degenerate into an
    # all-blank tail. One blank between non-blank lines preserves structure
    # without that failure mode.
    raw_lines = text.splitlines()
    lines: list[str] = []
    prev_blank = True  # suppress leading blanks
    for line in raw_lines:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        lines.append(line)
        prev_blank = is_blank
    # Trim trailing blank line (if any) left by the collapse pass.
    if lines and not lines[-1].strip():
        lines.pop()
    if len(lines) > _E2E_LOG_EXCERPT_LINE_CAP:
        lines = lines[-_E2E_LOG_EXCERPT_LINE_CAP:]
    return lines


def _public_e2e_results_by_category(
    results_by_category: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Project internal E2E result rows to the public UI contract."""
    return {
        category: [
            _public_e2e_result_case(result)
            for result in list(results_by_category[category])
        ]
        for category in E2ERunResultCategoriesPayload.model_fields
    }


def _public_e2e_result_case(result: dict[str, Any]) -> dict[str, Any]:
    captured_output = {
        "stdout_available": result.get("stdout_available") is True,
        "stderr_available": result.get("stderr_available") is True,
    }
    return {
        "nodeid": result["nodeid"],
        "case_id": result["case_id"],
        "label": result["label"],
        "display_name": result["display_name"],
        "suite_name": result["suite_name"],
        "result_source": result["result_source"],
        "outcome": result["outcome"],
        "duration_seconds": result["duration_seconds"],
        "longrepr": result["longrepr"],
        "failure_summary": result["failure_summary"],
        "retry_outcome": result["retry_outcome"],
        "is_quarantined": result["is_quarantined"],
        "updated_at": result["updated_at"],
        "history": result["history"],
        "existing_issue": _public_e2e_existing_issue(result["existing_issue"]),
        "category": result["category"],
        "result_category": result["result_category"],
        "flip_rate": result["flip_rate"],
        "flip_rate_percent": result["flip_rate_percent"],
        "is_likely_flaky": result["is_likely_flaky"],
        "captured_output": captured_output,
    }


def _empty_captured_output_availability() -> dict[str, bool]:
    return {
        "stdout_available": False,
        "stderr_available": False,
    }


def _public_e2e_existing_issue(issue: dict[str, Any] | None) -> dict[str, Any] | None:
    if issue is None:
        return None
    return TestCaseIssueLinkPayload.model_validate(
        {
            "number": issue["number"],
            "status": issue["status"],
            "resolution": issue["resolution"],
        }
    ).model_dump(mode="json")


def _e2e_run_artifacts(run: dict[str, Any], db_artifacts: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    artifacts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    log_path = run.get("log_path")
    if isinstance(log_path, str) and log_path.strip():
        artifacts.append(
            {
                "kind": "raw_log",
                "label": "Raw Output",
                "path": log_path,
            }
        )
        seen.add(("raw_log", log_path))

    for artifact in db_artifacts:
        kind = artifact.get("kind")
        label = artifact.get("label")
        path = artifact.get("path")
        if not isinstance(kind, str) or not isinstance(label, str) or not isinstance(path, str):
            raise ValueError(
                f"Malformed E2E artifact row for run {run.get('id')}: {artifact!r}"
            )
        key = (kind, path)
        if key in seen:
            continue
        artifacts.append(
            {
                "kind": kind,
                "label": label,
                "path": path,
            }
        )
        seen.add(key)

    reports = [
        artifact
        for artifact in artifacts
        if artifact["kind"] in {"junit_xml", "html_report", "json_report", "playwright_report"}
    ]
    return artifacts, reports


def _finalize_issue_detail_payload(
    *,
    orchestrator: Any,
    issue_number: int,
    payload: dict[str, Any],
    stack_dependency: StackDependencyGateView | None,
    raw_events: list[dict[str, Any]],
    filtered_events: list[dict[str, Any]],
    events: list[dict[str, Any]],
    dropped_missing_semantics: int,
) -> dict[str, Any]:
    run_diagnostic = _current_run_validation_diagnostic(orchestrator, issue_number)
    # Stack dependency state is route-scoped and passed in by the caller,
    # never re-derived here from ``orchestrator.state`` by issue number.
    # The live dashboard issue-detail route resolves it from the current
    # ``dependency_gate_snapshot``; the E2E run drawer is scoped to an
    # ephemeral run timeline and passes ``None`` so a production stack gate
    # can never leak into an unrelated E2E scenario issue that happens to
    # share a numeric issue id (#6597/#6680).
    payload["stack_dependency"] = stack_dependency_payload(stack_dependency)
    _apply_issue_detail_actions(orchestrator, issue_number, payload)
    if run_diagnostic:
        _apply_issue_detail_run_diagnostic(payload, run_diagnostic)
    if is_timeline_trace_enabled():
        runs = payload.get("runs")
        run_count = len(runs) if isinstance(runs, list) else 0
        cycle_count = (
            sum(
                len(run.get("cycles", []))
                for run in runs
                if isinstance(run, dict) and isinstance(run.get("cycles"), list)
            )
            if isinstance(runs, list)
            else 0
        )
        logger.info(
            "[TIMELINE] api.issue_detail issue=%s raw=%s filtered=%s semantic=%s "
            "dropped_missing_semantics=%s runs=%s cycles=%s",
            issue_number,
            len(raw_events),
            len(filtered_events),
            len(events),
            dropped_missing_semantics,
            run_count,
            cycle_count,
        )
    diagnostic = _timeline_missing_diagnostic(
        orchestrator,
        issue_number,
        events,
        dropped_missing_semantics=dropped_missing_semantics,
    )
    if diagnostic:
        summary = payload.get("summary")
        if isinstance(summary, dict):
            summary["timeline_diagnostic"] = diagnostic
        if run_diagnostic is None:
            payload["status_explanation"] = (
                f"Timeline data missing ({', '.join(diagnostic.get('signals', []))})"
            )
    return payload

# NOTE (#6421): the legacy ``GET /api/timeline/{issue_number}`` route was
# retired here.  It read the same ``timeline_reader`` stream and ran the same
# filter/retain/decorate/phase-toc/cycles projection as
# ``/api/issue-detail/{issue_number}``, but returned an untyped ``JSONResponse``
# outside the ``timeline_projection_endpoint`` boundary and was therefore
# absent from ``docs/api/ui-openapi.json``.  Its only browser consumer read
# ``events``/``phase_toc``/``cycles`` — all of which the contracted
# issue-detail payload already carries — so the route was deleted rather than
# contracted.  ``/api/issue-detail/{issue_number}`` is the replacement.
@web_issue_detail_router.get(
    "/api/issue-detail/{issue_number}", response_model=IssueDetailPayload
)
@timeline_projection_endpoint("issue_detail")
async def get_issue_detail(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    view: str = "user",
) -> IssueDetailPayload | JSONResponse:
    """Get an issue-detail payload for drawer rendering."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    reader = orchestrator.deps.timeline_reader
    try:
        stream = reader.read(issue_number, limit=2000)
    except RuntimeError as exc:
        logger.error("Timeline read failed for issue %d: %s", issue_number, exc)
        return JSONResponse(
            status_code=503,
            content={"error": "timeline_unavailable", "detail": str(exc)},
        )
    timeline = stream.to_dict()
    raw_events = timeline.get("events", [])
    filtered_events = _filter_timeline_events(raw_events)
    events, dropped_missing_semantics = _retain_semantic_timeline_events(
        filtered_events
    )
    events = _decorate_timeline_events(events, issue_number)
    phase_toc = _build_phase_toc(events)
    cycles = _build_timeline_cycles(events)
    payload = build_issue_detail_view_model(
        issue_number=issue_number,
        title=issue_title_for(orchestrator, issue_number),
        issue_url=issue_url_for(orchestrator.config, issue_number),
        events=events,
        phase_toc=phase_toc,
        cycles=cycles,
        context=_build_issue_story_context(orchestrator, issue_number),
        view=normalize_timeline_view(view),
        raw_events=raw_events,
    )
    payload = _finalize_issue_detail_payload(
        orchestrator=orchestrator,
        issue_number=issue_number,
        payload=payload,
        # Live dashboard route: resolve the producer stack gate snapshot
        # for this issue number from the orchestrator's current state.
        stack_dependency=stack_dependency_view(orchestrator.state, issue_number),
        raw_events=raw_events,
        filtered_events=filtered_events,
        events=events,
        dropped_missing_semantics=dropped_missing_semantics,
    )
    payload["lifecycle"] = _dashboard_lifecycle_payload(
        issue_number=issue_number,
        title=payload["title"],
        events=events,
    )
    return IssueDetailPayload.model_validate(payload)


@web_issue_detail_router.get(
    "/api/e2e-runs/recent",
    response_model=RecentE2ERunsPayload,
)
async def get_recent_e2e_runs(
    orchestrator: WebOrchestratorDependency,
    limit: int = 50,
) -> RecentE2ERunsPayload | JSONResponse:
    """List recent E2E runs as typed ``RecentE2ERunSummary`` rows.

    Backs the inline runs-as-rows panel introduced in issue #6334.
    The runs list itself is intentionally eager (so the dashboard
    renders all visible rows in one request); per-run detail is the
    lazy bit, fetched from ``/api/e2e-run-detail/{run_id}`` only when
    the user expands a row.

    ``limit`` is clamped to ``[1, 200]`` to match the OpenAPI schema
    so a client cannot trigger an unbounded scan of ``e2e_runs``.
    """
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    clamped_limit = max(1, min(int(limit), 200))
    config = orchestrator.config
    db_path = config.repo_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return RecentE2ERunsPayload(runs=[])
    from ..infra.e2e_db import E2EDB
    from ..view_models.dashboard_e2e import build_recent_e2e_runs

    def _build() -> RecentE2ERunsPayload:
        db = E2EDB(db_path)
        payload = build_recent_e2e_runs(db, config, limit=clamped_limit)
        return RecentE2ERunsPayload.model_validate(payload.model_dump())

    import asyncio

    return await asyncio.to_thread(_build)

@web_issue_detail_router.get(
    "/api/e2e-run-detail/{run_id}",
    response_model=E2ERunDetailPayload,
    response_model_exclude_unset=True,
)
@timeline_projection_endpoint("e2e_run_detail", issue_number_key=None)
async def get_e2e_run_detail(
    run_id: int,
    orchestrator: WebOrchestratorDependency,
    view: str = "user",
) -> E2ERunDetailPayload | JSONResponse:
    """Get E2E run detail using the shared issue-detail timeline pipeline."""
    from ..domain.timeline_key import TimelineKey
    from ..timeline import TimelineStream

    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    store_key = TimelineKey.for_e2e_run(run_id).to_store_key()
    try:
        records = orchestrator.deps.timeline_store.read(store_key, limit=5000)
    except RuntimeError as exc:
        logger.error("Timeline read failed for E2E run %d: %s", run_id, exc)
        return JSONResponse(
            status_code=503,
            content={"error": "timeline_unavailable", "detail": str(exc)},
        )

    if not records:
        return JSONResponse(
            {
                "error": "not_found",
                "detail": f"No timeline events for E2E run {run_id}",
            },
            status_code=404,
        )

    e2e_records = [record for record in records if record.event != "e2e.agent_snapshot"]
    snapshot_records = [
        record for record in records if record.event == "e2e.agent_snapshot"
    ]

    stream = TimelineStream.from_records(store_key, e2e_records)
    raw_events = [event.to_dict() for event in stream.events]
    _promote_e2e_test_event_fields(
        raw_events,
        e2e_records,
        run_id=run_id,
        e2e_db_path=orchestrator.config.repo_root / ".issue-orchestrator" / "e2e.db",
    )
    e2e_events = _filter_timeline_events(raw_events)
    agent_events = [
        record.data for record in snapshot_records if isinstance(record.data, dict)
    ]
    if not agent_events:
        agent_events = _load_orchestrator_events_for_run(orchestrator, run_id)

    matcher_view = normalize_timeline_view(view)
    events = _attach_issue_numbers_to_test_windows(
        e2e_events,
        agent_events,
        run_id=run_id,
        view=matcher_view,
    )
    payload = build_issue_detail_view_model(
        issue_number=store_key,
        title=f"E2E Run #{run_id}",
        issue_url="",
        events=events,
        phase_toc=_build_phase_toc(events),
        cycles=_build_timeline_cycles(events),
        context=None,
        view=matcher_view,
        raw_events=raw_events,
    )
    payload["issue_affordances"] = collect_issue_affordances(
        agent_events,
        run_id=run_id,
        view=matcher_view,
    )
    try:
        run_details = _e2e_run_execution_details(orchestrator, run_id)
    except (E2ERunDatabaseNotFoundError, E2ERunRecordNotFoundError) as exc:
        return JSONResponse(
            {"error": "not_found", "detail": str(exc)},
            status_code=404,
        )
    run_payload = _public_e2e_run_payload(dict(run_details["run"]), run_id)
    db_artifacts = list(run_details.get("artifacts") or [])
    try:
        artifacts, reports = _e2e_run_artifacts(run_payload, db_artifacts)
    except ValueError:
        logger.exception("Malformed E2E artifact rows for run %s", run_id)
        return JSONResponse(
            {
                "error": "internal_error",
                "detail": "Malformed E2E run artifacts",
            },
            status_code=500,
        )
    results_summary = dict(run_details["summary"])
    results_by_category = _public_e2e_results_by_category(
        run_details["tests_by_category"],
    )
    payload["run"] = run_payload
    payload["results_summary"] = results_summary
    payload["results_by_category"] = results_by_category
    payload["artifacts"] = artifacts
    payload["reports"] = reports
    from ..view_models.dashboard_e2e import build_e2e_artifact_diagnostic

    payload["artifact_diagnostic"] = build_e2e_artifact_diagnostic(
        orchestrator.config.e2e, collected_count=len(db_artifacts)
    ).model_dump(mode="json")
    payload["lifecycle"] = project_e2e_suite_lifecycle_container_for_run(
        run_id=run_id,
        events=events,
        agent_events=agent_events,
        subject_label="E2E Suite",
    ).model_dump(
        mode="json",
    )
    return E2ERunDetailPayload.model_validate(payload)


@web_issue_detail_router.get(
    "/api/e2e-run/{run_id}/test-output",
    response_model=E2ETestOutputPayload,
    response_model_exclude_unset=False,
)
async def get_e2e_run_test_output(
    run_id: int,
    nodeid: str,
    orchestrator: WebOrchestratorDependency,
) -> E2ETestOutputPayload | JSONResponse:
    """Lazy-load captured stdout/stderr for one test from the run's JUnit XML.

    Captured output bodies are intentionally NOT persisted to SQLite — they can
    be many megabytes per test. We re-parse the on-disk JUnit XML on demand.
    """
    from ..infra.e2e_db import E2EDB

    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    nodeid_clean = nodeid.strip()
    if not nodeid_clean:
        return JSONResponse(
            {"error": "bad_request", "detail": "nodeid is required"},
            status_code=400,
        )

    db_path = orchestrator.config.repo_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": f"E2E database not found for run {run_id}"},
            status_code=404,
        )
    db = E2EDB(db_path)
    run = db.get_run(run_id)
    if run is None:
        return JSONResponse(
            {"error": "not_found", "detail": f"E2E run {run_id} not found"},
            status_code=404,
        )

    junit_paths = [
        Path(artifact.path)
        for artifact in db.list_run_artifacts(run_id)
        if artifact.kind == "junit_xml"
    ]
    from .e2e_test_output_reader import load_e2e_test_output

    payload = load_e2e_test_output(
        repo_root=Path(run.repo_root),
        run_id=run_id,
        nodeid=nodeid_clean,
        junit_paths=junit_paths,
    )
    if payload is not None:
        return payload
    if not junit_paths:
        return JSONResponse(
            {"error": "no_junit", "detail": f"No JUnit XML artifact for run {run_id}"},
            status_code=404,
        )
    return JSONResponse(
        {
            "error": "not_found",
            "detail": f"No captured output recorded for nodeid {nodeid_clean!r}",
        },
        status_code=404,
    )


@web_issue_detail_router.get(
    "/api/e2e-run/{run_id}/issue-detail/{issue_number}",
    response_model=IssueDetailPayload,
)
@timeline_projection_endpoint("e2e_issue_detail")
async def get_e2e_issue_detail(
    run_id: int,
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    view: str = "user",
) -> IssueDetailPayload | JSONResponse:
    """Return issue detail for an ephemeral E2E issue from a specific run."""
    from ..execution.timeline_store import read_timeline_records
    from ..infra.e2e_db import E2EDB
    from ..infra.e2e_worktree import get_e2e_worktree_path
    from ..timeline import TimelineStream

    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    repo_root = orchestrator.config.repo_root
    e2e_db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    if not e2e_db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    db = E2EDB(e2e_db_path)
    run = db.get_run(run_id)
    if run is None:
        return JSONResponse(
            {"error": "not_found", "detail": f"E2E run {run_id} not found"},
            status_code=404,
        )

    worktree_timeline = (
        get_e2e_worktree_path(repo_root)
        / ".issue-orchestrator"
        / "state"
        / "timeline.sqlite"
    )
    if not worktree_timeline.exists():
        return JSONResponse(
            {
                "error": "not_found",
                "detail": f"E2E worktree timeline missing for run {run_id}",
            },
            status_code=404,
        )

    try:
        all_records = read_timeline_records(worktree_timeline, issue_number, limit=2000)
    except Exception as exc:
        logger.exception(
            "Failed to read e2e-worktree timeline for run %d issue %d",
            run_id,
            issue_number,
        )
        return JSONResponse(
            {"error": "db_error", "detail": str(exc)},
            status_code=500,
        )

    window_start = run.started_at
    window_end = run.finished_at or "9999-12-31T23:59:59Z"
    records = [
        record
        for record in all_records
        if window_start <= record.timestamp <= window_end
    ]
    if not records:
        return JSONResponse(
            {
                "error": "not_found",
                "detail": (
                    f"No events for issue {issue_number} in E2E run {run_id} "
                    f"worktree timeline (window {window_start} → {window_end})"
                ),
            },
            status_code=404,
        )

    timeline = TimelineStream.from_records(issue_number, records).to_dict()
    raw_events = timeline.get("events", [])
    filtered_events = _filter_timeline_events(raw_events)
    events, dropped_missing_semantics = _retain_semantic_timeline_events(
        filtered_events
    )
    events = _decorate_timeline_events(events, issue_number)
    payload = build_issue_detail_view_model(
        issue_number=issue_number,
        title=f"Issue #{issue_number}",
        issue_url=issue_url_for(orchestrator.config, issue_number),
        events=events,
        phase_toc=_build_phase_toc(events),
        cycles=_build_timeline_cycles(events),
        context=None,
        view=normalize_timeline_view(view),
        raw_events=raw_events,
    )
    payload = _finalize_issue_detail_payload(
        orchestrator=orchestrator,
        issue_number=issue_number,
        payload=payload,
        # Ephemeral E2E run drawer: no run-scoped stack snapshot exists, so
        # never surface the live dashboard's production stack gates for a
        # scenario issue that happens to reuse this numeric id (#6597/#6680).
        stack_dependency=None,
        raw_events=raw_events,
        filtered_events=filtered_events,
        events=events,
        dropped_missing_semantics=dropped_missing_semantics,
    )
    payload["e2e_run_id"] = run_id
    payload["lifecycle"] = _dashboard_lifecycle_payload(
        issue_number=issue_number,
        title=payload["title"],
        events=events,
    )
    return IssueDetailPayload.model_validate(payload)

def _dashboard_lifecycle_payload(
    *,
    issue_number: int,
    title: str,
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not events:
        return None
    return project_dashboard_lifecycle_container(
        subject_label="Dashboard",
        issue_number=issue_number,
        title=title,
        events=events,
        # Legacy presentation cycles are display groupings. Semantic
        # lifecycle cycles are derived from backend-owned logical fields.
        cycles=(),
    ).model_dump(
        mode="json",
    )


def _load_orchestrator_events_for_run(
    orchestrator: Any,
    run_id: int,
) -> list[dict[str, Any]]:
    """Read orchestrator events from timeline.sqlite for an E2E run's time window."""
    from ..infra.e2e_timeline import read_orchestrator_events_by_window
    from ..infra.e2e_worktree import get_e2e_worktree_path

    if not orchestrator:
        return []

    repo_root = orchestrator.config.repo_root
    db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    worktree_timeline = (
        get_e2e_worktree_path(repo_root)
        / ".issue-orchestrator"
        / "state"
        / "timeline.sqlite"
    )
    if not db_path.exists() or not worktree_timeline.exists():
        return []

    try:
        from ..infra.e2e_db import E2EDB

        db = E2EDB(db_path)
        run = db.get_run(run_id)
        if not run:
            return []
        return read_orchestrator_events_by_window(
            worktree_timeline,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
    except Exception:
        logger.debug(
            "Could not load orchestrator events for E2E run %d", run_id, exc_info=True
        )
        return []


def _build_issue_story_context(  # noqa: C901, PLR0912 - story assembly pulls from multiple orchestrator-owned stores
    orchestrator: Any,
    issue_number: int,
) -> IssueStoryContext | None:
    """Assemble story context from orchestrator state for one issue."""
    if not orchestrator:
        return None

    state = orchestrator.state
    config = orchestrator.config

    active_runtime: int | None = None
    active_task_kind: str | None = None
    for session in state.active_sessions:
        if session.issue.number == issue_number:
            active_runtime = session.runtime_minutes
            active_task_kind = session.key.task.value
            break

    labels: tuple[str, ...] = ()
    for issue in state.cached_queue_issues:
        if issue.number == issue_number:
            labels = tuple(issue.labels)
            break
    if not labels:
        for session in state.active_sessions:
            if session.issue.number == issue_number:
                labels = tuple(session.issue.labels)
                break

    dependency_problem = state.dependency_problems.get(issue_number)
    dependency_summary = dependency_problem.summary if dependency_problem else None

    rework_status = resolve_queued_rework(state, issue_number)
    rework_cycle = rework_status.rework_cycle if rework_status else 0

    pr_url: str | None = None
    pr_number: int | None = None
    for review in state.pending_reviews:
        if review.issue_number == issue_number:
            pr_url = review.pr_url
            pr_number = review.pr_number
            break
    if not pr_url:
        for entry in state.session_history:
            if entry.issue_number == issue_number and entry.pr_url:
                pr_url = entry.pr_url
                break
    # A rework-queued issue may have no pending_review; carry the PR number
    # the rework is against so the drawer can name it.
    if pr_number is None and rework_status is not None:
        pr_number = rework_status.pr_number

    return IssueStoryContext(
        flow_stage=_determine_issue_flow_stage(
            issue_number,
            labels,
            active_task_kind,
            state,
            pr_url,
            is_queued_for_rework=rework_status is not None,
        ),
        active_runtime_minutes=active_runtime,
        active_task_kind=active_task_kind,
        labels=labels,
        dependency_summary=dependency_summary,
        current_rework_cycle=rework_cycle,
        max_rework_cycles=config.max_rework_cycles,
        pr_url=pr_url,
        pr_number=pr_number,
        rework_reason=rework_status.reason if rework_status else None,
    )


def _determine_issue_flow_stage(
    issue_number: int,
    labels: tuple[str, ...],
    active_task_kind: str | None,
    state: Any,
    pr_url: str | None,
    *,
    is_queued_for_rework: bool = False,
) -> str:
    """Determine the flow stage for an issue."""
    from ..domain.models import _base_of, _is_blocking_label

    if active_task_kind is not None:
        return "in_progress"
    if any(_is_blocking_label(label) for label in labels):
        return "blocked"
    # Checked before awaiting_merge: a rework-queued issue may still carry a
    # stale pr-pending label for a tick, which must not mask the rework state.
    if is_queued_for_rework:
        return "queued_for_rework"
    if any(_base_of(label) == "pr-pending" for label in labels):
        return "awaiting_merge"

    for entry in state.session_history:
        if entry.issue_number == issue_number:
            if entry.status in DONE_HISTORY_STATUSES:
                if entry.status == "completed" and pr_url:
                    return "awaiting_merge"
                return "done"
            if entry.status in BLOCKED_HISTORY_STATUSES:
                return "blocked"
    return "queued"


def _timeline_missing_diagnostic(
    orchestrator: Any,
    issue_number: int,
    events: list[dict[str, Any]],
    *,
    dropped_missing_semantics: int = 0,
) -> dict[str, Any] | None:
    """Return diagnostic details when timeline is unexpectedly empty."""
    if events or not orchestrator:
        return None

    state = orchestrator.state
    signals: list[str] = []
    if any(session.issue.number == issue_number for session in state.active_sessions):
        signals.append("active_session_present")
    if any(entry.issue_number == issue_number for entry in state.session_history):
        signals.append("session_history_present")
    if any(review.issue_number == issue_number for review in state.pending_reviews):
        signals.append("pending_review_present")
    if any(
        rework.resolve_issue_number() == issue_number
        for rework in state.pending_reworks
    ):
        signals.append("pending_rework_present")
    if issue_number in state.completed_today:
        signals.append("completed_today_present")

    context = resolve_issue_session_context(orchestrator, issue_number)
    if context.run_dir is not None:
        signals.append("session_run_present")
    if dropped_missing_semantics > 0:
        signals.append("logical_semantics_missing")
    if not signals:
        return None

    logger.warning(
        "Timeline missing for issue #%s despite signals: %s",
        issue_number,
        ", ".join(signals),
    )
    from ..infra.repo_identity import state_dir

    timeline_db_path = state_dir(orchestrator.config.repo_root) / "timeline.sqlite"
    state_name = (
        "logical_semantics_missing"
        if dropped_missing_semantics > 0
        else "expected_history_missing"
    )
    return {
        "state": state_name,
        "signals": signals,
        "expected_timeline_store": str(timeline_db_path),
        "expected_timeline_store_exists": timeline_db_path.exists(),
        "resolved_run_dir": str(context.run_dir) if context.run_dir else None,
        "dropped_missing_semantics": dropped_missing_semantics,
    }
