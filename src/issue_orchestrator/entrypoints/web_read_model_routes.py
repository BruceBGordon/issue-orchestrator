"""Dashboard read-model and row-rendering routes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..contracts.ui_openapi_models import (
    DashboardViewModelPayload,
    IssueRowsPayload,
    ViewModelSnapshotPayload,
)
from ..ports.provider_resilience import (
    NO_PROVIDER_CIRCUIT_STATUS,
    ProviderCircuitStatusReader,
)
from ..ports.tech_lead_run_record_store import (
    NO_TECH_LEAD_RUN_HISTORY,
    TechLeadRunHistoryReader,
)
from ..view_models.dashboard import build_dashboard_view_model
from .web_session_context import WebOrchestratorDependency
from .web_templates import get_templates

logger = logging.getLogger(__name__)

web_read_model_router = APIRouter()


@dataclass(frozen=True)
class DashboardQueryParams:
    """Shared dashboard pagination and tab query parameters."""

    queue_page: int
    e2e_page: int
    active_tab: str


def _dashboard_query_params(request: Request) -> DashboardQueryParams:
    queue_page = int(request.query_params.get("page", 1))
    if queue_page < 1:
        queue_page = 1
    e2e_page = int(request.query_params.get("e2e_page", 1))
    if e2e_page < 1:
        e2e_page = 1
    return DashboardQueryParams(
        queue_page=queue_page,
        e2e_page=e2e_page,
        active_tab=request.query_params.get("tab", "flow"),
    )


def _provider_circuit_reader(orchestrator: Any) -> ProviderCircuitStatusReader:
    """Resolve the dashboard's provider circuit-status reader (#5980 F2/A1).

    The single place the "there is no orchestrator to read" case is decided.
    ``GET /`` is a public browser page that must render (not 503) before the
    orchestrator is installed, and with no orchestrator there is no provider
    fleet to report on. A *present* orchestrator always goes through its
    required ``provider_circuit`` facade property, so broken wiring surfaces as
    a hard failure instead of an empty, healthy-looking payload.
    """
    if orchestrator is None:
        return NO_PROVIDER_CIRCUIT_STATUS
    return orchestrator.provider_circuit


def _tech_lead_history_reader(orchestrator: Any) -> TechLeadRunHistoryReader:
    """Resolve the dashboard's local tech-lead run history (ADR-0033 / #6858).

    The same "there is no orchestrator to read" decision the provider-circuit
    reader makes, made once, in the same place. With no engine there is no
    local history — and a PRESENT engine always goes through its required
    facade property, so broken wiring fails loudly instead of rendering an
    empty panel that looks like a tech lead which has simply never run.
    """
    if orchestrator is None:
        return NO_TECH_LEAD_RUN_HISTORY
    return orchestrator.tech_lead_run_history


def _build_dashboard_vm_sync(orchestrator: Any, queue_page: int, active_tab: str, e2e_page: int):
    return build_dashboard_view_model(
        orchestrator,
        provider_circuit=_provider_circuit_reader(orchestrator),
        tech_lead_history=_tech_lead_history_reader(orchestrator),
        queue_page=queue_page,
        active_tab=active_tab,
        e2e_page=e2e_page,
    )


def _render_issue_rows_sync(template, view_model) -> list[dict[str, Any]]:
    rows = []
    for issue in view_model.issues:
        html = template.render(
            issue=issue,
            active_tab=view_model.active_tab,
            github_owner=view_model.github_owner,
            github_repo=view_model.github_repo,
        )
        rows.append({
            "issue_number": issue.get("issue_number"),
            "html": html,
        })
    return rows


def _issue_numbers(items: list[dict[str, Any]]) -> list[int]:
    numbers: list[int] = []
    for item in items:
        issue_number = item.get("issue_number")
        if isinstance(issue_number, int):
            numbers.append(issue_number)
    return numbers


def _flow_column_summary(view_model: Any) -> str:
    parts: list[str] = []
    for column in getattr(view_model, "flow_columns", []) or []:
        column_id = column.get("id", "?")
        items = column.get("items", []) or []
        count = column.get("count", len(items))
        preview = _issue_numbers(items)
        parts.append(f"{column_id}={count}:{preview}")
    return " ".join(parts) if parts else "(none)"


def _reset_retry_pending_issue_numbers(view_model: Any, orchestrator: Any) -> list[int]:
    lm = getattr(getattr(orchestrator, "deps", None), "label_manager", None)
    if lm is None:
        return []
    pending_labels = {
        lm.reset_retry_pending,
        lm.reset_retry_scratch_pending,
    }
    numbers: set[int] = set()
    item_groups = (
        "queue_items",
        "blocked_items",
        "awaiting_merge_items",
        "active_items",
        "completed_items",
    )
    for group in item_groups:
        for item in getattr(view_model, group, []) or []:
            labels = set(item.get("orchestrator_labels", []) or [])
            issue_number = item.get("issue_number")
            if labels.intersection(pending_labels) and isinstance(issue_number, int):
                numbers.add(issue_number)
    return sorted(numbers)


def _log_view_model_response(
    *,
    kind: str,
    query: DashboardQueryParams,
    view_model: Any,
    orchestrator: Any,
    row_count: int | None = None,
) -> None:
    logger.debug(
        "[dashboard] %s response: page=%s tab=%s rows=%s flow=%s "
        "reset_retry_pending=%s",
        kind,
        query.queue_page,
        query.active_tab,
        row_count if row_count is not None else "(not-rendered)",
        _flow_column_summary(view_model),
        _reset_retry_pending_issue_numbers(view_model, orchestrator),
    )


@web_read_model_router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, orchestrator: WebOrchestratorDependency) -> HTMLResponse:
    """Render the main dashboard, or the login form when unauthenticated.

    Auth model: ``/`` is marked public in the middleware (otherwise an
    anonymous browser would hit a raw 401 JSON), so this handler has
    to decide for itself whether to serve the dashboard or the login
    form. When auth is disabled entirely (``configure_dashboard_admin_token(None)``
    — the TestClient default) we render the dashboard directly, which
    keeps every non-auth unit test working.
    """
    from ._auth_middleware import resolve_browser_page_auth
    from .web import get_configured_dashboard_admin_token

    page_auth = resolve_browser_page_auth(
        request, auth_enabled=get_configured_dashboard_admin_token() is not None
    )
    if isinstance(page_auth, HTMLResponse):
        return page_auth

    request_start = time.time()

    query = _dashboard_query_params(request)
    logger.info("[dashboard] Request URL: %s, page=%s, tab=%s", request.url, query.queue_page, query.active_tab)

    templates = get_templates()
    template = templates.get_template("dashboard.html")
    vm_start = time.time()
    view_model = await asyncio.to_thread(
        _build_dashboard_vm_sync,
        orchestrator,
        query.queue_page,
        query.active_tab,
        query.e2e_page,
    )
    _log_view_model_response(
        kind="page",
        query=query,
        view_model=view_model,
        orchestrator=orchestrator,
    )
    vm_elapsed = time.time() - vm_start
    render_start = time.time()
    # Render the flash diagnostic probe as a parser-blocking <script> tag
    # only when ?debug=flash is in the URL. The localStorage-based toggle
    # is handled by an inline gate in the template (which is allowed to
    # load the script asynchronously — that path is for ad-hoc debugging,
    # not the e2e regression test that needs to observe the very first
    # mutations).
    flash_debug = request.query_params.get("debug") == "flash"
    html = await asyncio.to_thread(
        template.render,
        **view_model.template_context(),
        browser_auth_required=page_auth.browser_auth_required,
        csrf_token=page_auth.csrf_token,
        flash_debug=flash_debug,
    )
    render_elapsed = time.time() - render_start
    total_elapsed = time.time() - request_start
    logger.info(
        "[dashboard] Total request time: %.2fs (view_model=%.2fs render=%.2fs)",
        total_elapsed,
        vm_elapsed,
        render_elapsed,
    )
    return HTMLResponse(content=html)


@web_read_model_router.get("/api/view-model", response_model=DashboardViewModelPayload)
async def get_view_model(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> DashboardViewModelPayload | JSONResponse:
    """Get the dashboard view model as JSON."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    query = _dashboard_query_params(request)

    view_model = await asyncio.to_thread(
        _build_dashboard_vm_sync,
        orchestrator,
        query.queue_page,
        query.active_tab,
        query.e2e_page,
    )
    _log_view_model_response(
        kind="view-model",
        query=query,
        view_model=view_model,
        orchestrator=orchestrator,
    )
    return DashboardViewModelPayload.model_validate(view_model.to_dict())


@web_read_model_router.get("/api/view-model-snapshot", response_model=ViewModelSnapshotPayload)
async def get_view_model_snapshot(
    # Keep the dependency-injected orchestrator before Query defaults so
    # FastAPI and Python signature ordering both remain valid.
    orchestrator: WebOrchestratorDependency,
    tab: str = Query("flow"),
    page: int = Query(1, ge=1),
    e2e_page: int = Query(1, ge=1),
) -> ViewModelSnapshotPayload | JSONResponse:
    """Get view-model and rendered rows from a single snapshot.

    This keeps tab counts and rendered list rows in lockstep for UI refreshes.
    """
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    query = DashboardQueryParams(
        queue_page=page,
        e2e_page=e2e_page,
        active_tab=tab,
    )

    templates = get_templates()
    row_template = templates.get_template("issue_row.html")

    def _build_snapshot_sync() -> tuple[Any, list[dict[str, Any]]]:
        vm = _build_dashboard_vm_sync(
            orchestrator,
            query.queue_page,
            query.active_tab,
            query.e2e_page,
        )
        return vm, _render_issue_rows_sync(row_template, vm)

    view_model, rows = await asyncio.to_thread(_build_snapshot_sync)
    _log_view_model_response(
        kind="snapshot",
        query=query,
        view_model=view_model,
        orchestrator=orchestrator,
        row_count=len(rows),
    )

    return ViewModelSnapshotPayload.model_validate({
        "view_model": view_model.to_dict(),
        "rows": rows,
        "active_tab": view_model.active_tab,
        "count": len(rows),
    })


@web_read_model_router.get("/api/issue-rows", response_model=IssueRowsPayload)
async def get_issue_rows(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> IssueRowsPayload | JSONResponse:
    """Get rendered issue rows for the current view."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    query = _dashboard_query_params(request)

    templates = get_templates()
    template = templates.get_template("issue_row.html")

    def _build_rows_sync() -> tuple[Any, list[dict[str, Any]]]:
        vm = _build_dashboard_vm_sync(orchestrator, query.queue_page, query.active_tab, query.e2e_page)
        return vm, _render_issue_rows_sync(template, vm)

    view_model, rows = await asyncio.to_thread(_build_rows_sync)
    _log_view_model_response(
        kind="issue-rows",
        query=query,
        view_model=view_model,
        orchestrator=orchestrator,
        row_count=len(rows),
    )

    return IssueRowsPayload.model_validate({
        "rows": rows,
        "active_tab": view_model.active_tab,
        "count": len(rows),
    })
