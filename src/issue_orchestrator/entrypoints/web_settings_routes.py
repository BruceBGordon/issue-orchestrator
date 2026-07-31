"""Dashboard settings, milestone, and issue creation routes."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from ..infra.config import Config
from ..infra.config_document_patch import save_config_document_patch
from ..infra.settings_schema import (
    TAB_DEFINITIONS,
    apply_to,
    build_save_plan,
    from_config,
    get_settings_json_schema,
)
from .web_session_context import WebOrchestratorDependency
from .web_templates import get_templates

logger = logging.getLogger(__name__)

web_settings_router = APIRouter()


@web_settings_router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, orchestrator: WebOrchestratorDependency
) -> HTMLResponse:
    """Render the settings page.

    Resolves the browser session's CSRF token (when dashboard auth is
    enabled) and hands it to the template so ``browser_auth.js`` can
    attach ``X-CSRF-Token`` to the ``POST /api/settings`` save. Without
    this the save fetch carries no CSRF header and the auth gate rejects
    it with ``missing or invalid csrf token``. Mirrors the dashboard
    root handler in ``web_read_model_routes.py``.
    """
    from ._auth_middleware import resolve_browser_page_auth
    from .web import get_configured_dashboard_admin_token

    page_auth = resolve_browser_page_auth(
        request, auth_enabled=get_configured_dashboard_admin_token() is not None
    )
    if isinstance(page_auth, HTMLResponse):
        return page_auth

    templates = get_templates()
    template = templates.get_template("settings.html")

    config = Config() if orchestrator is None else orchestrator.config
    tab_values = from_config(config)
    schemas = get_settings_json_schema()
    values_dump = {k: v.model_dump() for k, v in tab_values.items()}

    html = template.render(
        tabs=TAB_DEFINITIONS,
        schemas=schemas,
        values=values_dump,
        csrf_token=page_auth.csrf_token,
        browser_auth_required=page_auth.browser_auth_required,
    )
    return HTMLResponse(content=html)


@web_settings_router.get("/api/settings")
async def get_settings(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get current settings as JSON for the settings UI."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    tab_values = from_config(orchestrator.config)
    return JSONResponse({k: v.model_dump() for k, v in tab_values.items()})


@web_settings_router.post("/api/settings")
async def update_settings(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Update settings and save to YAML.

    Validates via Pydantic, applies to config, runs doctor validation,
    and saves to YAML. Rolls back on any failure.

    JSON body: {tab_key: {field: value, ...}, ...}
    """
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    config = orchestrator.config

    # Snapshot for rollback
    snapshot = from_config(config)
    snapshot_dump = {k: v.model_dump() for k, v in snapshot.items()}

    # Validate + parse via Pydantic. Absent tabs fall back to the current
    # snapshot so the live-config apply below always sees a complete tab set.
    # Which tabs get *persisted* is decided later by comparing against the
    # snapshot, not by request key presence (see the save step below).
    try:
        new_tabs = {}
        for tab in TAB_DEFINITIONS:
            key = tab["key"]
            if key in body:
                new_tabs[key] = tab["model"].model_validate(body[key])
            else:
                new_tabs[key] = snapshot[key]
    except ValidationError as e:
        return JSONResponse({
            "error": "Validation failed",
            "errors": [{"name": err["loc"][-1] if err["loc"] else "unknown",
                         "detail": err["msg"]} for err in e.errors()],
        }, status_code=400)

    # Apply to config
    restart_required = apply_to(new_tabs, config)

    # Run doctor validation
    from ..execution.command_runner import LocalCommandRunner
    from ..infra.doctor import run_doctor

    result = run_doctor(config=config, runner=LocalCommandRunner())

    # Check for errors - rollback on validation failure
    errors = [c for c in result.checks if c.status == "error"]
    if errors:
        rollback_tabs = {tab["key"]: tab["model"](**snapshot_dump[tab["key"]])
                         for tab in TAB_DEFINITIONS}
        apply_to(rollback_tabs, config)
        return JSONResponse({
            "error": "Validation failed",
            "errors": [{"name": c.name, "detail": c.detail} for c in errors],
        }, status_code=400)

    # Save config to YAML.
    #
    # Build a field-granular patch plan and write ONLY the settings-owned
    # yaml_paths whose value actually changed, instead of rewriting the whole
    # file through the lossy full-config serializer (Config.save/to_dict) or
    # re-projecting every field of a changed tab. `build_save_plan` compares
    # each submitted field against the current-config snapshot; the settings
    # form posts every tab on every save, and the snapshot/submitted values come
    # from `from_config` after Config.load expanded every ${VAR}, so anything
    # coarser would (a) materialize defaults for untouched sections and (b)
    # rewrite an unedited sibling "${SECRET}" reference with its expanded value.
    # An empty plan is a no-op save: skip the file write so main.yaml's
    # non-leading comments, anchors, and quoting stay byte-for-byte intact.
    save_plan = build_save_plan(snapshot, new_tabs)
    if save_plan.is_empty:
        logger.info("[settings] No settings changed; config file left untouched")
    elif config.config_path:
        try:
            save_config_document_patch(config, save_plan.entries)
            logger.info(
                "[settings] Config saved to %s (paths: %s)",
                config.config_path,
                ", ".join(save_plan.changed_yaml_paths),
            )
        except Exception as e:
            logger.error("[settings] Failed to save config: %s", e)
            rollback_tabs = {tab["key"]: tab["model"](**snapshot_dump[tab["key"]])
                             for tab in TAB_DEFINITIONS}
            apply_to(rollback_tabs, config)
            return JSONResponse({
                "error": f"Failed to save config: {e}",
            }, status_code=500)

    return JSONResponse({
        "success": True,
        "restart_required": restart_required,
        "warnings": [{"name": c.name, "detail": c.detail} for c in result.checks if c.status == "warning"],
    })


@web_settings_router.get("/api/milestones")
async def get_milestones(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get available milestones, indicating which are included/excluded."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    config = orchestrator.config

    try:
        # Get all milestones from GitHub via repository_host protocol
        all_milestones = orchestrator.repository_host.list_milestones(state="open")

        # Get filter milestones from config
        filter_milestones = config.get_filter_milestones()

        milestones = []
        for m in all_milestones:
            title = m.get("title", "")
            number = m.get("number")
            is_included = not filter_milestones or title in filter_milestones
            milestones.append({
                "title": title,
                "number": number,
                "description": m.get("description", ""),
                "due_on": m.get("due_on"),
                "open_issues": m.get("open_issues", 0),
                "included": is_included,
            })

        return JSONResponse({
            "milestones": milestones,
            "filter_active": bool(filter_milestones),
            "filter_milestones": filter_milestones,
        })
    except Exception as e:
        logger.error("[settings] Failed to fetch milestones: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@web_settings_router.post("/api/issues")
async def create_issue(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Create a new issue with specified labels and milestone.

    JSON body:
        title: str - Issue title (required)
        body: str - Issue body/description
        milestone: int - Milestone number (optional)
        agent: str - Agent label (e.g., "agent:backend")
        priority: str - Priority label (e.g., "P1")
        labels: list[str] - Additional labels (optional)
    """
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    title = body.get("title", "").strip()
    if not title:
        return JSONResponse({"error": "Title is required"}, status_code=400)

    issue_body = body.get("body", "")
    milestone = body.get("milestone")  # milestone number
    agent = body.get("agent")
    priority = body.get("priority")
    extra_labels = body.get("labels", [])

    # Build labels list
    labels = []
    if agent:
        labels.append(agent)
    if priority:
        labels.append(priority)
    labels.extend(extra_labels)

    try:
        # Create the issue via repository_host protocol
        result = orchestrator.repository_host.create_issue(
            title=title,
            body=issue_body,
            labels=labels,
            milestone=milestone,
        )

        if result is None:
            return JSONResponse({"error": "Failed to create issue"}, status_code=500)

        issue_number = result.get("number")
        issue_url = result.get("html_url")

        return JSONResponse({
            "status": "created",
            "issue_number": issue_number,
            "url": issue_url,
        })
    except Exception as e:
        logger.error("[settings] Failed to create issue: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
