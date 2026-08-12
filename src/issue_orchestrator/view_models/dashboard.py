"""Dashboard view model builder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
from typing import Any, Callable, assert_never

from ..domain.issue_key import format_issue_label, parse_external_id
from ..domain.models import BLOCKED_HISTORY_STATUSES, DONE_HISTORY_STATUSES, SessionHistoryStatus
from ..domain.session_key import TaskKind
from ..history import latest_history_entries_by_issue
from ..control.label_manager import LabelManager
from ..infra.audit import get_issue_dependencies
from ..infra import gh_audit
from ..ports.provider_resilience import ProviderCircuitStatusReader
from .dependency_gate import (
    stack_chip,
    stack_chip_payload,
    stack_dependency_payload,
    stack_dependency_view,
    stack_signal,
)
from .issue_card_labels import blocked_summary, display_labels as _display_labels
from .issue_card_labels import provider_badge, provider_badge_payload, provider_signal
from .provider_circuit import ProviderCircuitStatusView, read_provider_circuit_status
from .tech_lead_run_actions import TechLeadRunActionsView, read_tech_lead_run_actions
from .dashboard_e2e import E2E_PAGE_SIZE
from .dashboard_e2e import build_e2e_items
from .dashboard_e2e import build_e2e_view_model
from .dashboard_e2e import build_recent_e2e_runs_payload
from .dashboard_e2e import get_e2e_status
from .lifecycle_semantics import RecentE2ERunsPayload
from .dashboard_assets import DASHBOARD_CSS_CHUNKS
from .dashboard_assets import DASHBOARD_JS_CHUNKS
from .dashboard_flow import apply_lane_precedence
from .dashboard_flow import build_awaiting_merge_items
from .dashboard_flow import build_flow_columns
from .dashboard_flow import exclude_flow_overlaps
from .dashboard_flow import normalize_dashboard_tab, select_issues_for_tab
from .dashboard_flow import stamp_issue_item_stale_badge_visibility
from .rework_status import queued_rework_issue_numbers, resolve_queued_rework
from .timestamp_values import dashboard_timestamp_source

QUEUE_PAGE_SIZE = 20


@dataclass(frozen=True)
class DashboardViewModel:
    """View model for the web dashboard."""

    issues: list[dict[str, Any]]
    active_items: list[dict[str, Any]]
    queue_items: list[dict[str, Any]]
    blocked_items: list[dict[str, Any]]
    history_items: list[dict[str, Any]]
    e2e_items: list[dict[str, Any]]
    completed_items: list[dict[str, Any]]
    awaiting_merge_items: list[dict[str, Any]]
    flow_columns: list[dict[str, Any]]
    scope_summary: dict[str, Any]

    active_count: int
    queue_count: int
    blocked_count: int
    e2e_count: int
    completed_count: int
    awaiting_merge_count: int

    active_tab: str
    paused: bool
    shutdown_requested: bool
    active_session_count: int
    startup_status: str
    startup_message: str

    repo: str
    repo_root: str
    config_name: str
    config_mode: str
    github_owner: str
    github_repo: str
    validation_configured: bool

    queue_page: int
    queue_total_pages: int
    queue_total: int
    queue_refresh_seconds: int

    e2e_status: dict[str, Any]
    e2e_page: int
    e2e_total_pages: int
    e2e_total: int
    # Issue #6334: typed payload powering the inline runs-as-rows
    # panel.  Template embeds it as JSON; the JS chunk
    # ``e2e_runs_list.js`` reads it on DOMContentLoaded.
    recent_e2e_runs: RecentE2ERunsPayload

    agents: dict[str, Any]
    agent_names: list[str]

    provider_circuit: ProviderCircuitStatusView
    tech_lead_runs: TechLeadRunActionsView

    def template_context(self) -> dict[str, Any]:
        return {
            "issues": self.issues,
            "active_items": self.active_items,
            "queue_items": self.queue_items,
            "blocked_items": self.blocked_items,
            "history_items": self.history_items,
            "e2e_items": self.e2e_items,
            "completed_items": self.completed_items,
            "awaiting_merge_items": self.awaiting_merge_items,
            "flow_columns": self.flow_columns,
            "scope_summary": self.scope_summary,
            "active_count": self.active_count,
            "queue_count": self.queue_count,
            "blocked_count": self.blocked_count,
            "e2e_count": self.e2e_count,
            "completed_count": self.completed_count,
            "awaiting_merge_count": self.awaiting_merge_count,
            "active_tab": self.active_tab,
            "paused": self.paused,
            "shutdown_requested": self.shutdown_requested,
            "active_session_count": self.active_session_count,
            "startup_status": self.startup_status,
            "startup_message": self.startup_message,
            "repo": self.repo,
            "repo_root": self.repo_root,
            "github_owner": self.github_owner,
            "github_repo": self.github_repo,
            "validation_configured": self.validation_configured,
            "queue_page": self.queue_page,
            "queue_total_pages": self.queue_total_pages,
            "queue_total": self.queue_total,
            "queue_refresh_seconds": self.queue_refresh_seconds,
            "agents": self.agents,
            "e2e_status": self.e2e_status,
            "e2e_page": self.e2e_page,
            "e2e_total_pages": self.e2e_total_pages,
            "e2e_total": self.e2e_total,
            "recent_e2e_runs": self.recent_e2e_runs.model_dump(mode="json"),
            "dashboard_css_chunks": DASHBOARD_CSS_CHUNKS,
            "dashboard_js_chunks": DASHBOARD_JS_CHUNKS,
            "dashboard_data": self.dashboard_data(),
        }

    def dashboard_data(self) -> dict[str, Any]:
        github_usage = gh_audit.get_live_usage_snapshot()
        return {
            "startupComplete": self.startup_status == "complete",
            "paused": self.paused,
            "e2eRunning": bool(self.e2e_status.get("running")),
            "queueRefreshSeconds": self.queue_refresh_seconds,
            "repo": self.repo,
            "repoRoot": self.repo_root,
            "configName": self.config_name,
            "configMode": self.config_mode,
            "githubOwner": self.github_owner,
            "githubRepo": self.github_repo,
            "validationConfigured": self.validation_configured,
            "e2eLastRun": self.e2e_status.get("last_run"),
            "e2eNeedsAttention": bool(self.e2e_status.get("needs_attention")),
            "e2eFailedTests": self.e2e_status.get("failed_tests") or [],
            "agents": self.agent_names,
            "scope": self.scope_summary,
            "refresh": self.scope_summary.get("refresh", {}),
            "githubUsage": github_usage,
            "providerCircuit": self.provider_circuit.model_dump(mode="json"),
            "techLeadRuns": self.tech_lead_runs.model_dump(mode="json", by_alias=True),
            "fetchLayerVisibilityAwareEnabled": self.scope_summary.get("refresh", {}).get("visibilityAwareEnabled", False),
            "fetchLayerSelectiveSyncPlannerEnabled": self.scope_summary.get("refresh", {}).get("selectiveSyncPlannerEnabled", False),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": self.issues,
            "active_items": self.active_items,
            "queue_items": self.queue_items,
            "blocked_items": self.blocked_items,
            "history_items": self.history_items,
            "e2e_items": self.e2e_items,
            "completed_items": self.completed_items,
            "awaiting_merge_items": self.awaiting_merge_items,
            "flow_columns": self.flow_columns,
            "scope_summary": self.scope_summary,
            "active_count": self.active_count,
            "queue_count": self.queue_count,
            "blocked_count": self.blocked_count,
            "e2e_count": self.e2e_count,
            "completed_count": self.completed_count,
            "awaiting_merge_count": self.awaiting_merge_count,
            "active_tab": self.active_tab,
            "paused": self.paused,
            "shutdown_requested": self.shutdown_requested,
            "active_session_count": self.active_session_count,
            "startup_status": self.startup_status,
            "startup_message": self.startup_message,
            "repo": self.repo,
            "repo_root": self.repo_root,
            "github_owner": self.github_owner,
            "github_repo": self.github_repo,
            # validation_configured is carried inside ``dashboard_data`` (the
            # typed DashboardDataContract), not as a top-level view-model key.
            "queue_page": self.queue_page,
            "queue_total_pages": self.queue_total_pages,
            "queue_total": self.queue_total,
            "queue_refresh_seconds": self.queue_refresh_seconds,
            "agents": self.agent_names,
            "e2e_status": self.e2e_status,
            "e2e_page": self.e2e_page,
            "e2e_total_pages": self.e2e_total_pages,
            "e2e_total": self.e2e_total,
            "recent_e2e_runs": self.recent_e2e_runs.model_dump(mode="json"),
            "dashboard_data": self.dashboard_data(),
        }


@dataclass(frozen=True)
class HistoryLaneProjection:
    """Typed owner for session-history lane candidates.

    `history_items` and `completed_items` intentionally overlap for terminal
    done history that should remain visible in the History tab and the
    Completed kanban lane.
    """

    history_items: list[dict[str, Any]]
    blocked_items: list[dict[str, Any]]
    completed_items: list[dict[str, Any]]


def _issue_label_fields(issue_number: int, title: str) -> tuple[dict[str, Any], str]:
    """Build the issue_key/issue_label pair and the display title for a card.

    Returns (label_fields, display_title). The display title strips any
    ``[M9-009]`` prefix already in ``title`` because the same id is rendered
    by the label, and showing it twice ("M9-009 · #4057 [M9-009] …") is noise.
    Falls back to the original title when no prefix is present.
    """
    parsed = parse_external_id(title)
    issue_key = parsed.external_id
    label_fields = {
        "issue_key": issue_key,
        "issue_label": format_issue_label(issue_number, issue_key),
    }
    display_title = parsed.raw_title if issue_key else title
    return label_fields, display_title


def issue_url_for(config, issue_number: int) -> str:
    if config and config.repo:
        return f"https://github.com/{config.repo}/issues/{issue_number}"
    return ""


def flow_steps_for(stage: str) -> list[dict[str, str]]:
    if stage == "not_eligible":
        return [
            {"key": "not_eligible", "label": "Not Eligible"},
            {"key": "queued", "label": "Queued"},
            {"key": "in_progress", "label": "In Progress"},
            {"key": "review", "label": "Review"},
            {"key": "done", "label": "Done"},
        ]
    if stage == "rework":
        return [
            {"key": "queued", "label": "Queued"},
            {"key": "in_progress", "label": "In Progress"},
            {"key": "review", "label": "Review"},
            {"key": "rework", "label": "Rework"},
            {"key": "done", "label": "Done"},
        ]
    if stage == "tech_lead":
        return [
            {"key": "queued", "label": "Queued"},
            {"key": "in_progress", "label": "In Progress"},
            {"key": "review", "label": "Review"},
            {"key": "tech_lead", "label": "Tech Lead"},
            {"key": "done", "label": "Done"},
        ]
    return [
        {"key": "queued", "label": "Queued"},
        {"key": "in_progress", "label": "In Progress"},
        {"key": "review", "label": "Review"},
        {"key": "done", "label": "Done"},
    ]


def flow_stage_label(steps: list[dict[str, str]], stage: str) -> str:
    for step in steps:
        if step["key"] == stage:
            return step["label"]
    return stage.replace("_", " ").title()


def _queue_wait_reason(
    *,
    state,
    config,
    issue_number: int,
    dep_problem: Any | None,
    queue_position: int,
) -> str:
    if state.paused:
        return "Waiting: orchestrator paused"

    active_count = len(state.active_sessions)
    max_sessions = max(1, int(getattr(config, "max_concurrent_sessions", 1)))
    if active_count >= max_sessions:
        return f"Waiting: at capacity ({active_count}/{max_sessions} running)"

    if dep_problem is not None:
        return f"Waiting: {dep_problem.summary or 'dependency details unavailable'}"

    if issue_number in state.failed_this_cycle:
        return "Waiting: previous launch/action failed (manual retry may be needed)"

    if any(entry.issue_number == issue_number for entry in state.session_history):
        return "Waiting: previous run state"

    if queue_position <= 1:
        return "Waiting: next scheduler tick"
    return f"Waiting: {queue_position - 1} runnable queued ahead"


def _format_age_seconds(seconds: float | int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


_TICK_STALL_FLOOR_SECONDS = 5  # Floor so a misconfiguration can't false-positive every tick


def _refresh_meta_for_issue(state, config, issue_number: int, now_ts: float) -> dict[str, Any]:
    per_issue = state.issue_last_refreshed_at.get(issue_number)
    fallback = state.queue_last_refresh_at if state.queue_last_refresh_at > 0 else None
    last_refreshed_at = per_issue or fallback
    age_seconds = (now_ts - last_refreshed_at) if last_refreshed_at else None
    stale_threshold = max(60, int(getattr(config, "flow_refresh_stale_seconds", 900)))
    tick_stall_threshold = max(
        _TICK_STALL_FLOOR_SECONDS,
        int(getattr(config, "tick_stall_threshold_seconds", 60)),
    )
    is_stale = age_seconds is None or age_seconds > stale_threshold
    freshness_label = f"{_format_age_seconds(age_seconds)} ago" if age_seconds is not None else "never refreshed"
    stale_reason = _stale_reason_for(
        state=state,
        age_seconds=age_seconds,
        is_stale=is_stale,
        stale_threshold=stale_threshold,
        tick_stall_threshold=tick_stall_threshold,
        now_ts=now_ts,
    )
    return {
        "last_refreshed_at": last_refreshed_at or 0.0,
        "last_refreshed_age_seconds": age_seconds if age_seconds is not None else -1,
        "last_refreshed_label": freshness_label,
        "is_stale": is_stale,
        "stale_reason": stale_reason,
    }


def _stale_reason_for(
    *,
    state,
    age_seconds: float | None,
    is_stale: bool,
    stale_threshold: int,
    tick_stall_threshold: int,
    now_ts: float,
) -> str:
    """Explain staleness using the orchestrator heartbeat when possible.

    Plain "refresh age > threshold" is rarely the real story: the underlying
    cause is almost always that the main loop is stuck doing something slow
    (subprocess, GH API call, lock). When ``last_tick_completed_at`` shows
    the loop hasn't finished a tick recently, say so and name the phase —
    that's actionable. Fall back to the legacy threshold text when the
    heartbeat looks healthy but GitHub refresh happens to lag.
    """
    if age_seconds is None:
        return "Not refreshed from GitHub yet"
    tick_stall_reason = _tick_stall_reason(state, now_ts, tick_stall_threshold)
    if tick_stall_reason:
        return tick_stall_reason
    if is_stale:
        return f"Older than {_format_age_seconds(stale_threshold)} stale threshold"
    return ""


def _tick_stall_reason(state, now_ts: float, threshold_seconds: int) -> str:
    last_completed = getattr(state, "last_tick_completed_at", 0.0) or 0.0
    last_started = getattr(state, "last_tick_started_at", 0.0) or 0.0
    if last_completed <= 0 and last_started <= 0:
        return ""  # orchestrator hasn't reported a heartbeat yet
    reference = last_completed if last_completed > 0 else last_started
    tick_age = now_ts - reference
    if tick_age <= threshold_seconds:
        return ""
    phase = (getattr(state, "current_tick_phase", "") or "").strip() or "idle"
    age_label = _format_age_seconds(tick_age)
    return (
        f"Orchestrator tick stalled — last completion {age_label} ago (phase: {phase})"
    )


def _attach_refresh_meta(items: list[dict[str, Any]], state, config, now_ts: float) -> None:
    for item in items:
        issue_number = item.get("issue_number")
        if not isinstance(issue_number, int):
            continue
        item.update(_refresh_meta_for_issue(state, config, issue_number, now_ts))


def _attach_card_projections(items: list[Any], state, lm: LabelManager) -> None:
    """Finalization owner for the "every visible card carries X" policies.

    Projects each card's producer ``stack_dependency`` / ``stack_signal`` from the
    state snapshot, plus the provider-outage badge from the card's own
    orchestrator labels. Runs over each assembled lane list — after all card
    builders, before columns and awaiting-merge derive from them — so no
    individual builder can omit the fields and a new lane cannot regress a chip
    or badge by forgetting to set them. This is the single enrichment boundary;
    builders never set them.
    """
    for item in items:
        issue_number = item.get("issue_number")
        if not isinstance(issue_number, int):
            continue
        view = stack_dependency_view(state, issue_number)
        item["stack_dependency"] = stack_dependency_payload(view)
        item["stack_signal"] = stack_signal(view)
        # Precomputed chip/badge display so the server template and JS render
        # identically (the first-paint DOM must match what the client rebuilds).
        item["stack_chip"] = stack_chip_payload(stack_chip(view))
        badge = provider_badge(item.get("orchestrator_labels") or (), lm)
        item["provider_badge"] = provider_badge_payload(badge)
        item["provider_signal"] = provider_signal(badge)


def _refresh_meta(state, config, issue_number: int) -> dict[str, Any]:
    refreshed_at = state.issue_refresh_timestamps.get(issue_number, 0.0)
    queue_interval = config.queue_refresh_seconds if config else 600
    full_scan_interval = config.fetch_layer_full_scan_interval_seconds if config else 1800
    if queue_interval > 0:
        stale_after = max(queue_interval * 2, 120)
    else:
        stale_after = max(full_scan_interval, 300)

    if refreshed_at <= 0:
        return {
            "refresh_age_seconds": None,
            "refresh_age_label": "not refreshed",
            "is_stale": True,
        }

    age_seconds = max(0, int(time.time() - refreshed_at))
    if age_seconds >= 3600:
        age_label = f"{age_seconds // 3600}h"
    elif age_seconds >= 60:
        age_label = f"{age_seconds // 60}m"
    else:
        age_label = f"{age_seconds}s"
    return {
        "refresh_age_seconds": age_seconds,
        "refresh_age_label": age_label,
        "is_stale": age_seconds >= stale_after,
    }


def _pending_issue_numbers(state) -> dict[str, set[int]]:
    pending_review_numbers = {r.issue_number for r in state.pending_reviews} | {
        r.issue_number for r in state.discovered_reviews
    }
    pending_retrospective_numbers = {
        r.issue_number for r in state.pending_retrospective_reviews
    } | {
        r.issue_number for r in state.discovered_retrospective_reviews
    }
    pending_rework_numbers = {r.issue_number for r in state.pending_reworks} | {
        r.issue_number for r in state.discovered_reworks
    }
    pending_tech_lead_numbers = {r.issue_number for r in state.pending_tech_lead_reviews}
    return {
        "review": pending_review_numbers,
        "retrospective_review": pending_retrospective_numbers,
        "rework": pending_rework_numbers,
        "tech_lead": pending_tech_lead_numbers,
    }


def _is_synthetic_session_title(title: str) -> bool:
    normalized = title.strip()
    return (
        normalized.startswith("Review PR #")
        or normalized.startswith("Rework #")
        or normalized.startswith("Rework PR #")
    )


def _canonical_issue_title(state, issue_number: int, fallback_title: str) -> str:
    for issue in state.cached_queue_issues:
        if issue.number == issue_number and issue.title:
            return issue.title
    for entry in reversed(state.session_history):
        if entry.issue_number != issue_number:
            continue
        title = (entry.title or "").strip()
        if title and not _is_synthetic_session_title(title):
            return title
    return fallback_title


def _build_active_items(state, config, queue_page: int, seen_issues: set[int], *, lm: LabelManager) -> tuple[list[dict[str, Any]], set[int]]:
    if queue_page != 1:
        return [], seen_issues

    items: list[dict[str, Any]] = []
    for session in state.active_sessions:
        runtime = session.runtime_minutes
        timeout = session.agent_config.timeout_minutes
        tmux_name = session.terminal_id or ""
        is_review = tmux_name.startswith("review-")
        phase = "Reviewing" if is_review else "Coding"

        agent_label = (session.issue.agent_type or "unknown").replace("agent:", "")
        if runtime >= timeout:
            status = "slow"
            status_reason = f"Over timeout ({runtime} min / {timeout} min)"
        else:
            status = "active"
            status_reason = f"Running for {runtime} min"

        seen_issues.add(session.issue.number)
        if session.key.task == TaskKind.REVIEW:
            flow_stage = "review"
        elif session.key.task == TaskKind.RETROSPECTIVE_REVIEW:
            flow_stage = "review"
            phase = "Retro review"
            status_reason = f"Reviewing existing implementation for {runtime} min"
        elif session.key.task == TaskKind.REWORK:
            flow_stage = "rework"
        elif session.key.task == TaskKind.TECH_LEAD:
            flow_stage = "tech_lead"
        else:
            flow_stage = "in_progress"
        flow_steps = flow_steps_for(flow_stage)
        flow_stage_label_value = flow_stage_label(flow_steps, flow_stage)

        blocked = blocked_summary(
            list(session.issue.labels),
            lm,
            state.dependency_problems.get(session.issue.number).summary
            if session.issue.number in state.dependency_problems
            else None,
        )

        terminal_hint = "Click to focus terminal session"
        if config and config.terminal_adapter == "subprocess":
            terminal_hint = "Click to view agent UI log"

        canonical_title = _canonical_issue_title(state, session.issue.number, session.issue.title)
        label_fields, display_title = _issue_label_fields(session.issue.number, canonical_title)
        items.append({
            "card_id": session.terminal_id,
            "issue_number": session.issue.number,
            **label_fields,
            "title": display_title,
            "agent_type": agent_label,
            "status": status,
            "status_reason": status_reason,
            "detail_label": f"agent: {agent_label}",
            "detail_reason": status_reason,
            "phase": phase,
            "time": f"{runtime} min",
            "action": "focus",
            "action_icon": "→",
            "action_hint": terminal_hint,
            "url": "",
            "issue_url": issue_url_for(config, session.issue.number),
            "pr_url": "",
            "has_terminal": True,
            "worktree_path": str(session.worktree_path) if session.worktree_path else "",
            # Run-scoped dir for this active session, so the prompt action can
            # open the run manifest's launch prompt instead of the static
            # agent template.
            "run_dir": str(session.run_dir),
            "flow_stage": flow_stage,
            "flow_stage_label": flow_stage_label_value,
            "flow_steps": flow_steps,
            "blocked_summary": blocked,
            "orchestrator_labels": _display_labels(list(session.issue.labels), lm),
            **_refresh_meta(state, config, session.issue.number),
        })

    return items, seen_issues


def _timeline_snapshot_text(events: list[dict[str, Any]]) -> str | None:
    visible_events = []
    for event in events:
        views = event.get("views")
        if views is None or "user" in views:
            visible_events.append(event)
    for event in reversed(visible_events):
        narrative = str(event.get("narrative") or "").strip()
        if narrative:
            return narrative
        summary = str(event.get("summary") or "").strip()
        if summary:
            return summary
    return None


def _attach_running_timeline_snapshots(orchestrator: Any, active_items: list[dict[str, Any]]) -> None:
    deps = getattr(orchestrator, "deps", None)
    reader = getattr(deps, "timeline_reader", None)
    if reader is None:
        return

    snapshot_by_issue: dict[int, str | None] = {}
    for item in active_items:
        issue_number = item.get("issue_number")
        if not isinstance(issue_number, int):
            continue
        if issue_number in snapshot_by_issue:
            snapshot = snapshot_by_issue[issue_number]
            if snapshot:
                item["summary"] = snapshot
            continue
        try:
            stream = reader.read(issue_number, limit=40)
        except RuntimeError:
            snapshot_by_issue[issue_number] = None
            continue
        snapshot = _timeline_snapshot_text(stream.to_dict().get("events", []))
        snapshot_by_issue[issue_number] = snapshot
        if snapshot:
            item["summary"] = snapshot


def _build_queue_items(  # noqa: C901, PLR0912 — aggregates queue from multiple state sources
    state,
    config,
    seen_issues: set[int],
    pending_numbers: dict[str, set[int]],
    *,
    lm: LabelManager,
) -> tuple[list[dict[str, Any]], int, set[int]]:
    queue_items: list[dict[str, Any]] = []
    queue_total = 0

    if state.startup_status != "complete":
        return queue_items, queue_total, seen_issues

    queue_issues = state.cached_queue_issues
    queue_total = len(queue_issues)
    dependency_info = get_issue_dependencies(queue_issues, config)

    queued_position = 0
    for issue in queue_issues:
        if issue.number in seen_issues:
            continue
        seen_issues.add(issue.number)

        dep_info = dependency_info.get(issue.number)
        has_deps = dep_info.has_dependencies if dep_info else False
        deps_json = json.dumps([
            {"number": d[0], "title": d[1]}
            for d in (dep_info.dependencies if dep_info else [])
        ])
        dep_summary = dep_info.summary if dep_info else ""

        dep_problem = state.dependency_problems.get(issue.number)
        blocked = blocked_summary(
            list(issue.labels),
            lm,
            dep_problem.summary if dep_problem else None,
        )
        # Separate dependency-blocked (stays in queue) from agent-blocked (goes to blocked column)
        is_dependency_blocked = dep_problem is not None
        is_blocked = lm.is_blocking_any(issue.labels)  # Only label-based blocks go to the blocked column
        agent_label = (issue.agent_type or "unknown").replace("agent:", "")
        if is_blocked:
            status = "blocked"
            status_reason = _normalize_status_reason(dep_summary) or "blocked"
            detail_label = blocked or "blocked"
        elif is_dependency_blocked:
            status = "queue"
            status_reason = _normalize_status_reason(dep_summary)
            detail_label = f"agent: {agent_label}"
        else:
            status = "queue"
            status_reason = _normalize_status_reason(dep_summary)
            detail_label = f"agent: {agent_label}"

        rework_status = None
        if is_blocked:
            flow_stage = "blocked"
        elif issue.number in pending_numbers["rework"]:
            flow_stage = "rework"
            rework_status = resolve_queued_rework(state, issue.number)
        elif issue.number in pending_numbers["tech_lead"]:
            flow_stage = "tech_lead"
        elif issue.number in pending_numbers["retrospective_review"]:
            flow_stage = "review"
            detail_label = "Retro review queued"
            status_reason = "Waiting for review of existing implementation"
        elif issue.number in pending_numbers["review"] or lm.is_pr_pending(issue.labels):
            flow_stage = "review"
        elif lm.is_in_progress(issue.labels):
            flow_stage = "in_progress"
        else:
            flow_stage = "queued"
            if (
                not is_dependency_blocked
                and issue.number not in state.failed_this_cycle
                and not any(entry.issue_number == issue.number for entry in state.session_history)
            ):
                queued_position += 1
        flow_steps = flow_steps_for(flow_stage)
        flow_stage_label_value = flow_stage_label(flow_steps, flow_stage)
        queue_reason = (
            _queue_wait_reason(
                state=state,
                config=config,
                issue_number=issue.number,
                dep_problem=dep_problem,
                queue_position=queued_position,
            )
            if flow_stage == "queued"
            else None
        )
        # A rework-queued issue explains itself with the PR number, cycle, and
        # reason so operators see "Queued for rework — PR #469 …" on the card
        # before the rework session launches, rather than a bare "Rework" chip.
        if rework_status is not None:
            queue_reason = rework_status.summary
            status_reason = rework_status.summary
        if queue_reason:
            detail_label = queue_reason

        label_fields, display_title = _issue_label_fields(issue.number, issue.title)
        item = {
            "issue_number": issue.number,
            **label_fields,
            "title": display_title,
            "agent_type": agent_label,
            "status": status,
            "status_reason": status_reason,
            "detail_label": detail_label,
            "detail_reason": status_reason,
            "time": "",
            "action": "open",
            "action_icon": "↗",
            "action_hint": "Click to open issue on GitHub",
            "url": issue_url_for(config, issue.number),
            "issue_url": issue_url_for(config, issue.number),
            "pr_url": "",
            "has_terminal": False,
            "worktree_path": "",
            "has_dependencies": has_deps,
            "dependencies": deps_json,
            "dependency_summary": dep_summary,
            "flow_stage": flow_stage,
            "flow_stage_label": flow_stage_label_value,
            "flow_steps": flow_steps,
            "blocked_summary": blocked,
            "queue_wait_reason": queue_reason,
            # A rework-queued issue may still carry a stale pr-pending label for
            # a tick until the planner strips it; treat it as rework (Queued),
            # not awaiting-merge, so it does not linger in the merge lane.
            "merge_pending": lm.is_pr_pending(issue.labels) and rework_status is None,
            "dependency_blocked": is_dependency_blocked,
            "orchestrator_labels": _display_labels(list(issue.labels), lm),
            **_refresh_meta(state, config, issue.number),
        }
        if not is_blocked:
            queue_items.append(item)  # Dependency-blocked items stay in queue

    return queue_items, queue_total, seen_issues


def _scope_issues_for_blocked_projection(state) -> list[Any]:
    return state.cached_scope_issues if state.cached_scope_issues else state.cached_queue_issues


def _build_scope_blocked_items(
    state,
    config,
    seen_issues: set[int],
    *,
    lm: LabelManager,
) -> tuple[list[dict[str, Any]], set[int]]:
    blocked_items: list[dict[str, Any]] = []
    if state.startup_status != "complete":
        return blocked_items, seen_issues

    scope_issues = _scope_issues_for_blocked_projection(state)
    dependency_info = get_issue_dependencies(scope_issues, config)

    for issue in scope_issues:
        is_blocked = lm.is_blocking_any(issue.labels)
        if issue.number in seen_issues or not is_blocked:
            continue
        seen_issues.add(issue.number)
        dep_info = dependency_info.get(issue.number)
        dep_summary = dep_info.summary if dep_info else ""
        dep_problem = state.dependency_problems.get(issue.number)
        blocked = blocked_summary(
            list(issue.labels),
            lm,
            dep_problem.summary if dep_problem else None,
        )
        agent_label = (issue.agent_type or "unknown").replace("agent:", "")
        label_fields, display_title = _issue_label_fields(issue.number, issue.title)
        blocked_items.append({
            "issue_number": issue.number,
            **label_fields,
            "title": display_title,
            "agent_type": agent_label,
            "status": "blocked",
            "status_reason": _normalize_status_reason(dep_summary) or "blocked",
            "detail_label": blocked or "blocked",
            "detail_reason": _normalize_status_reason(dep_summary) or "blocked",
            "time": "",
            "action": "open",
            "action_icon": "↗",
            "action_hint": "Click to open issue on GitHub",
            "url": issue_url_for(config, issue.number),
            "issue_url": issue_url_for(config, issue.number),
            "pr_url": "",
            "has_terminal": False,
            "worktree_path": "",
            "flow_stage": "blocked",
            "flow_stage_label": flow_stage_label(flow_steps_for("blocked"), "blocked"),
            "flow_steps": flow_steps_for("blocked"),
            "blocked_summary": blocked,
            "merge_pending": lm.is_pr_pending(issue.labels),
            "dependency_blocked": False,
            "orchestrator_labels": _display_labels(list(issue.labels), lm),
            **_refresh_meta(state, config, issue.number),
        })

    return blocked_items, seen_issues


def _history_status_label(status: SessionHistoryStatus) -> str:
    match status:
        case "completed":
            return "Completed"
        case "merged":
            return "Merged"
        case "closed":
            return "Closed"
        case "failed":
            return "Failed"
        case "validation_failed":
            return "Validation Failed"
        case "blocked":
            return "Blocked"
        case "needs_human":
            return "Needs Human"
        case "timed_out":
            return "Timed Out"
    assert_never(status)


def _history_status_belongs_in_completed_lane(
    status: SessionHistoryStatus,
    *,
    merge_pending: bool,
) -> bool:
    """Only terminal done rows that are no longer awaiting merge enter Completed."""
    return status in DONE_HISTORY_STATUSES and not merge_pending


def _build_history_items(state, config) -> HistoryLaneProjection:
    history_items: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []
    completed_items: list[dict[str, Any]] = []
    for entry in latest_history_entries_by_issue(state.session_history, limit=50):
        # Completed-without-PR is not a terminal lane; let queue data drive placement.
        if entry.status == "completed" and not entry.pr_url:
            continue
        url = entry.pr_url if entry.pr_url else issue_url_for(config, entry.issue_number)
        action_hint = "Click to open PR" if entry.pr_url else "Click to open issue on GitHub"
        status_reason = _normalize_status_reason(getattr(entry, "status_reason", None))
        if not status_reason:
            status_reason = _history_status_label(entry.status)

        if entry.status in DONE_HISTORY_STATUSES:
            flow_stage = "done"
        elif entry.status in BLOCKED_HISTORY_STATUSES:
            flow_stage = "blocked"
        else:
            flow_stage = "in_progress"
        flow_steps = flow_steps_for(flow_stage)
        flow_stage_label_value = flow_stage_label(flow_steps, flow_stage)

        worktree_path = str(entry.worktree_path) if entry.worktree_path else ""

        label_fields, display_title = _issue_label_fields(entry.issue_number, entry.title)
        merge_pending = entry.status == "completed" and bool(entry.pr_url)
        history_time = _history_time_fields(entry)
        item = {
            "issue_number": entry.issue_number,
            **label_fields,
            "title": display_title,
            "agent_type": entry.agent_type.replace("agent:", ""),
            "status": entry.status,
            "status_reason": status_reason,
            "detail_label": _history_status_label(entry.status),
            "detail_reason": status_reason,
            "time": history_time[0],
            "time_is_timestamp": history_time[1],
            "runtime_label": f"{entry.runtime_minutes} min" if history_time[1] and entry.runtime_minutes else "",
            "action": "open",
            "action_icon": "↗",
            "action_hint": action_hint,
            "url": url,
            "issue_url": issue_url_for(config, entry.issue_number),
            "pr_url": entry.pr_url or "",
            "has_terminal": False,
            "worktree_path": worktree_path,
            "flow_stage": flow_stage,
            "flow_stage_label": flow_stage_label_value,
            "flow_steps": flow_steps,
            "blocked_summary": (
                status_reason
                if entry.status not in DONE_HISTORY_STATUSES
                else None
            ),
            # History records with an open PR belong in Awaiting Merge, not Completed.
            "merge_pending": merge_pending,
            **_refresh_meta(state, config, entry.issue_number),
        }
        if entry.status in BLOCKED_HISTORY_STATUSES:
            blocked_items.append(item)
        else:
            history_items.append(item)
            if _history_status_belongs_in_completed_lane(
                entry.status,
                merge_pending=merge_pending,
            ):
                completed_items.append(item)

    return HistoryLaneProjection(
        history_items=history_items,
        blocked_items=blocked_items,
        completed_items=completed_items,
    )


def _build_pending_validation_retry_items(state, config) -> list[dict[str, Any]]:
    blocked_items: list[dict[str, Any]] = []
    active_numbers = {session.issue.number for session in state.active_sessions}
    for retry in state.pending_validation_retries:
        if retry.issue_number in active_numbers:
            continue

        validation_cmd = retry.validation_cmd or "validation"
        retry_attempt = retry.retry_count + 1
        status_reason = (
            f"Validation retry pending after {validation_cmd} failed "
            f"(attempt {retry_attempt})"
        )
        if retry.validation_error:
            status_reason = f"{status_reason}: {retry.validation_error}"

        label_fields, display_title = _issue_label_fields(retry.issue_number, retry.issue_title)
        flow_steps = flow_steps_for("blocked")
        blocked_items.append({
            "issue_number": retry.issue_number,
            **label_fields,
            "title": display_title,
            "agent_type": retry.agent_label.replace("agent:", ""),
            "status": "validation_retry",
            "status_reason": status_reason,
            "detail_label": "Validation Retry Pending",
            "detail_reason": status_reason,
            "time": "",
            "action": "open",
            "action_icon": "↗",
            "action_hint": "Click to open issue on GitHub",
            "url": issue_url_for(config, retry.issue_number),
            "issue_url": issue_url_for(config, retry.issue_number),
            "pr_url": "",
            "has_terminal": False,
            "worktree_path": retry.worktree_path,
            "flow_stage": "blocked",
            "flow_stage_label": flow_stage_label(flow_steps, "blocked"),
            "flow_steps": flow_steps,
            "blocked_summary": status_reason,
            "merge_pending": False,
            "dependency_blocked": False,
            "orchestrator_labels": [],
            **_refresh_meta(state, config, retry.issue_number),
        })
    return blocked_items


def _build_pending_retrospective_review_items(
    state,
    config,
    seen_issues: set[int],
    *,
    lm: LabelManager,
) -> tuple[list[dict[str, Any]], set[int]]:
    items: list[dict[str, Any]] = []
    for review in state.pending_retrospective_reviews:
        if review.issue_number in seen_issues:
            continue
        seen_issues.add(review.issue_number)
        label_fields, display_title = _issue_label_fields(
            review.issue_number,
            review.issue_title,
        )
        flow_steps = flow_steps_for("review")
        labels = [review.agent_label, review.trigger_label]
        items.append({
            "issue_number": review.issue_number,
            **label_fields,
            "title": display_title,
            "agent_type": review.agent_label.replace("agent:", ""),
            "status": "queue",
            "status_reason": "Waiting for retrospective review",
            "detail_label": "Retro review queued",
            "detail_reason": "Review existing implementation before coder rework",
            "time": "",
            "action": "open",
            "action_icon": "↗",
            "action_hint": "Click to open issue on GitHub",
            "url": issue_url_for(config, review.issue_number),
            "issue_url": issue_url_for(config, review.issue_number),
            "pr_url": review.prior_pr_url or "",
            "has_terminal": False,
            "worktree_path": "",
            "flow_stage": "review",
            "flow_stage_label": flow_stage_label(flow_steps, "review"),
            "flow_steps": flow_steps,
            "blocked_summary": None,
            "queue_wait_reason": "Waiting: retrospective review queued",
            "merge_pending": False,
            "dependency_blocked": False,
            "orchestrator_labels": _display_labels(labels, lm),
            **_refresh_meta(state, config, review.issue_number),
        })
    return items, seen_issues


def _merge_blocked_items(
    scope_blocked: list[dict[str, Any]],
    history_blocked: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged_by_issue: dict[int, dict[str, Any]] = {}
    for item in scope_blocked:
        issue_number = _issue_number_value(item)
        if issue_number is not None:
            merged_by_issue[issue_number] = item
    for item in history_blocked:
        issue_number = _issue_number_value(item)
        if issue_number is not None:
            merged_by_issue[issue_number] = item
    return list(merged_by_issue.values())


def _normalize_status_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    trimmed = reason.strip()
    if not trimmed:
        return None
    # Freshness is represented by stale-dot metadata; avoid duplicating noisy sync text on cards.
    if trimmed.lower().startswith("synced "):
        return None
    return trimmed


def _sort_by_issue_number(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic default ordering for dashboard issue lists."""
    return sorted(items, key=lambda item: int(item.get("issue_number", 0)))


def _issue_number_value(item: dict[str, Any]) -> int | None:
    raw = item.get("issue_number")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _unique_issue_count(items: list[dict[str, Any]]) -> int:
    return len({
        issue_number
        for item in items
        for issue_number in [_issue_number_value(item)]
        if issue_number is not None
    })


def _queue_ordered_page(
    queue_items: list[dict[str, Any]],
    queue_order: dict[int, int],
    queue_page: int,
) -> list[dict[str, Any]]:
    ordered_items = sorted(
        queue_items,
        key=lambda item: queue_order.get(_issue_number_value(item) or -1, len(queue_order)),
    )
    page_items, _, _ = _paginate(ordered_items, queue_page, QUEUE_PAGE_SIZE)
    return _sort_by_issue_number(page_items)


def _build_backlog_items(state, config, *, lm: LabelManager) -> list[dict[str, Any]]:
    if state.startup_status != "complete":
        return []
    dependency_info = get_issue_dependencies(state.cached_queue_issues, config)
    cards: list[dict[str, Any]] = []
    for issue in state.cached_queue_issues:
        dep_info = dependency_info.get(issue.number)
        dep_summary = dep_info.summary if dep_info else ""
        blocked = blocked_summary(
            list(issue.labels),
            lm,
            dep_summary if dep_summary else None,
        )
        label_fields, display_title = _issue_label_fields(issue.number, issue.title)
        cards.append({
            "issue_number": issue.number,
            **label_fields,
            "title": display_title,
            "status": "backlog",
            "detail_label": "In execution scope",
            "flow_stage": "queued",
            "flow_stage_label": "Queued",
            "blocked_summary": blocked,
            "time": "",
            "issue_url": issue_url_for(config, issue.number),
            "url": issue_url_for(config, issue.number),
            "orchestrator_labels": _display_labels(list(issue.labels), lm),
            **_refresh_meta(state, config, issue.number),
        })
    return cards


def _paginate(items: list[dict[str, Any]], page: int, page_size: int) -> tuple[list[dict[str, Any]], int, int]:
    total = len(items)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1
    if page > total_pages:
        page = total_pages
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    return items[start_idx:end_idx], total_pages, page


def _history_time_fields(entry) -> tuple[str, bool]:
    completed_at = getattr(entry, "completed_at", None)
    if completed_at:
        return dashboard_timestamp_source(completed_at), True
    runtime = entry.runtime_minutes
    return (f"{runtime} min", False) if runtime else ("", False)


def build_dashboard_view_model(
    orchestrator,
    *,
    provider_circuit: ProviderCircuitStatusReader,
    queue_page: int = 1,
    active_tab: str = "kanban",
    e2e_page: int = 1,
    e2e_status_provider: Callable[[Any], dict[str, Any]] | None = None,
) -> DashboardViewModel:
    """Build dashboard view model for templates and APIs.

    ``provider_circuit`` is a required behaviour-level port (#5980 F2/A1): the
    projection never traverses the orchestrator's dependency container, and
    omitting the wiring is a hard error rather than a silently healthy fleet.
    """
    active_tab = normalize_dashboard_tab(active_tab)
    queue_page = max(queue_page, 1)
    e2e_page = max(e2e_page, 1)

    state = orchestrator.state if orchestrator else None
    config = orchestrator.config if orchestrator else None

    active_items: list[dict[str, Any]] = []
    queue_items: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []
    history_items: list[dict[str, Any]] = []
    e2e_items: list[dict[str, Any]] = []
    backlog_items: list[dict[str, Any]] = []
    awaiting_merge_items: list[dict[str, Any]] = []
    completed_items: list[dict[str, Any]] = []
    flow_columns: list[dict[str, Any]] = []
    scope_summary: dict[str, Any] = {}
    seen_issues: set[int] = set()

    queue_total = 0

    if state and config:
        lm = LabelManager(config)
        active_numbers = {s.issue.number for s in state.active_sessions}
        seen_issues.update(active_numbers)

        pending_numbers = _pending_issue_numbers(state)
        active_items, seen_issues = _build_active_items(state, config, queue_page, seen_issues, lm=lm)
        _attach_running_timeline_snapshots(orchestrator, active_items)
        scope_blocked, seen_issues = _build_scope_blocked_items(state, config, seen_issues, lm=lm)
        queue_items, queue_total, seen_issues = _build_queue_items(
            state, config, seen_issues, pending_numbers, lm=lm,
        )
        retrospective_queue_items, seen_issues = _build_pending_retrospective_review_items(
            state,
            config,
            seen_issues,
            lm=lm,
        )
        queue_items.extend(retrospective_queue_items)
        queue_order = {
            issue_number: index
            for index, item in enumerate(queue_items)
            for issue_number in [_issue_number_value(item)]
            if issue_number is not None
        }
        backlog_items = _build_backlog_items(state, config, lm=lm)
        history_projection = _build_history_items(state, config)
        history_items = history_projection.history_items
        history_blocked = history_projection.blocked_items
        pending_validation_blocked = _build_pending_validation_retry_items(state, config)
        blocked_items.extend(
            _merge_blocked_items(
                scope_blocked,
                history_blocked + pending_validation_blocked,
            )
        )

        active_items = _sort_by_issue_number(active_items)
        queue_items = _sort_by_issue_number(queue_items)
        blocked_items = _sort_by_issue_number(blocked_items)
        history_items = _sort_by_issue_number(history_items)

        now_ts = datetime.now(timezone.utc).timestamp()
        for items in (active_items, queue_items, blocked_items, history_items, backlog_items):
            _attach_refresh_meta(items, state, config, now_ts)
            # Single owner: every lane (incl. label-blocked / validation-retry /
            # retrospective) gets the stack view and provider badge; derived
            # lanes inherit them.
            _attach_card_projections(items, state, lm)
        stamp_issue_item_stale_badge_visibility(history_items, mode="when_stale_and_merge_pending")

        completed_items = history_projection.completed_items
        completed_items = _sort_by_issue_number(completed_items)

        # Awaiting merge = PRs ready for human merge; queued-rework issues stay owned by Queued.
        awaiting_merge_items = build_awaiting_merge_items(queue_items, blocked_items, history_items, exclude_issue_numbers=queued_rework_issue_numbers(state))
        awaiting_merge_items = _sort_by_issue_number(awaiting_merge_items)

        queue_items, blocked_items, awaiting_merge_items, completed_items = apply_lane_precedence(
            queue_items=queue_items,
            active_items=active_items,
            blocked_items=blocked_items,
            awaiting_merge_items=awaiting_merge_items,
            completed_items=completed_items,
        )
        queue_preview_items = _queue_ordered_page(queue_items, queue_order, queue_page)

        queue_items = _sort_by_issue_number(queue_items)
        blocked_items = _sort_by_issue_number(blocked_items)
        awaiting_merge_items = _sort_by_issue_number(awaiting_merge_items)
        completed_items = _sort_by_issue_number(completed_items)

        # Backlog used only for scope_summary.in_scope_total (not a kanban column)
        backlog_items = exclude_flow_overlaps(
            backlog_items,
            queue_items,
            active_items,
            blocked_items,
            completed_items,
        )
        flow_columns = build_flow_columns(
            queue_items,
            queue_preview_items,
            active_items,
            blocked_items,
            awaiting_merge_items,
            completed_items,
        )

    e2e_status_provider = e2e_status_provider or get_e2e_status
    e2e_status = e2e_status_provider(config)

    e2e_items = build_e2e_items(config, e2e_status)
    e2e_total = len(e2e_items)

    e2e_items_paginated, e2e_total_pages, e2e_page = _paginate(e2e_items, e2e_page, E2E_PAGE_SIZE)
    e2e_status = dict(e2e_status)
    e2e_status["view_model"] = build_e2e_view_model(
        e2e_status,
        e2e_items_paginated,
        e2e_total,
        e2e_page,
        e2e_total_pages,
        list((config.agents if config else {}).keys()),
    )
    issues = select_issues_for_tab(
        active_tab, active_items, queue_items, blocked_items,
        e2e_items_paginated, awaiting_merge_items, completed_items,
    )

    queue_total_pages = (queue_total + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE if queue_total > 0 else 1
    if queue_page > queue_total_pages:
        queue_page = queue_total_pages

    active_count = len(state.active_sessions) if state else 0
    shutdown_requested = orchestrator.shutdown_requested if orchestrator else False

    agents = config.agents if config else {}

    repo = config.repo if config else ""
    repo_root = str(config.repo_root) if config and config.repo_root else ""
    config_name = config.config_path.name if config and config.config_path else ""
    config_mode = config.configuration_mode if config else "default"
    github_owner = repo.split("/")[0] if repo and "/" in repo else ""
    github_repo = repo.split("/")[1] if repo and "/" in repo else ""
    # When there is no config at all (pre-bootstrap render) we cannot know
    # whether validation is configured, so suppress the warning rather than
    # cry wolf. Once config is loaded the warning tracks the real setting.
    validation_configured = config.is_validation_enabled() if config else True

    queue_refresh_seconds = config.queue_refresh_seconds if config else 600
    queue_last_refresh_age = (
        max(0.0, datetime.now(timezone.utc).timestamp() - state.queue_last_refresh_at)
        if state and state.queue_last_refresh_at > 0
        else -1
    )
    refresh_status = {
        "mode": state.queue_last_refresh_mode if state else "none",
        "lastRefreshAt": state.queue_last_refresh_at if state else 0.0,
        "lastNetworkSyncAt": state.queue_last_network_sync_at if state else 0.0,
        "lastRefreshAgeSeconds": queue_last_refresh_age,
        "lastRefreshLabel": (
            f"{_format_age_seconds(queue_last_refresh_age)} ago"
            if queue_last_refresh_age >= 0
            else "never"
        ),
        "inProgress": bool(state.queue_refresh_in_progress) if state else False,
        "requested": bool(state.queue_refresh_requested) if state else False,
        "lastFullScanAt": state.queue_last_full_scan_at if state else 0.0,
        "deltaWatermark": state.queue_delta_watermark if state else None,
        "refreshCount": state.queue_refresh_count if state else 0,
        "fetchLayerEnabled": config.fetch_layer_enabled if config else True,
        "networkSyncSeconds": config.fetch_layer_network_sync_seconds if config else 60,
        "fullScanIntervalSeconds": config.fetch_layer_full_scan_interval_seconds if config else 1800,
        "discoveryLimit": config.fetch_layer_discovery_limit if config else 25,
        "maxHotIssuesPerCycle": config.fetch_layer_max_hot_issues_per_cycle if config else 40,
        "prScanEveryNRefreshes": config.fetch_layer_pr_scan_every_n_refreshes if config else 2,
        "dependencyScanEveryNRefreshes": config.fetch_layer_dependency_scan_every_n_refreshes if config else 1,
        "flowLazyEnabled": bool(config.flow_refresh_enabled) if config else True,
        "flowStaleSeconds": int(config.flow_refresh_stale_seconds) if config else 900,
        "flowCooldownSeconds": int(config.flow_refresh_cooldown_seconds) if config else 120,
        "freshnessMode": str(config.flow_freshness_mode) if config else "balanced",
        "apiBudget": str(config.flow_api_budget) if config else "medium",
        "attentionPriority": str(config.flow_attention_priority) if config else "strict",
        "visibilityAwareEnabled": config.fetch_layer_visibility_aware_enabled if config else False,
        "selectiveSyncPlannerEnabled": config.fetch_layer_selective_sync_planner_enabled if config else False,
    }
    if config:
        milestones = config.get_filter_milestones()
        in_scope_total = _unique_issue_count(
            backlog_items
            + queue_items
            + active_items
            + blocked_items
            + awaiting_merge_items
            + completed_items
        )
        scope_summary = {
            "repo_open_total": queue_total,
            "in_scope_total": in_scope_total,
            "filter_label": config.filtering.label or "",
            "filter_milestones": milestones,
            "exclude_labels": list(config.filtering.exclude_labels),
            "refresh_mode": state.queue_last_refresh_mode if state else "none",
            "refresh": refresh_status,
        }
    else:
        scope_summary = {
            "repo_open_total": queue_total,
            "in_scope_total": len(backlog_items),
            "filter_label": "",
            "filter_milestones": [],
            "exclude_labels": [],
            "refresh_mode": "none",
            "refresh": refresh_status,
        }

    recent_e2e_runs = build_recent_e2e_runs_payload(config)

    return DashboardViewModel(
        issues=issues,
        active_items=active_items,
        queue_items=queue_items,
        blocked_items=blocked_items,
        history_items=history_items,
        e2e_items=e2e_items_paginated,
        completed_items=completed_items,
        awaiting_merge_items=awaiting_merge_items,
        flow_columns=flow_columns,
        scope_summary=scope_summary,
        active_count=len(active_items),
        queue_count=len(queue_items),
        blocked_count=len(blocked_items),
        e2e_count=e2e_total,
        completed_count=len(completed_items),
        awaiting_merge_count=len(awaiting_merge_items),
        active_tab=active_tab,
        paused=state.paused if state else False,
        shutdown_requested=shutdown_requested,
        active_session_count=active_count,
        startup_status=state.startup_status if state else "pending",
        startup_message=state.startup_message if state else "",
        repo=repo,
        repo_root=repo_root,
        config_name=config_name,
        config_mode=config_mode,
        github_owner=github_owner,
        github_repo=github_repo,
        validation_configured=validation_configured,
        queue_page=queue_page,
        queue_total_pages=queue_total_pages,
        queue_total=queue_total,
        queue_refresh_seconds=queue_refresh_seconds,
        e2e_status=e2e_status,
        e2e_page=e2e_page,
        e2e_total_pages=e2e_total_pages,
        e2e_total=e2e_total,
        recent_e2e_runs=recent_e2e_runs,
        agents=agents,
        agent_names=list(agents.keys()) if agents else [],
        provider_circuit=read_provider_circuit_status(provider_circuit),
        tech_lead_runs=read_tech_lead_run_actions(config, state),
    )
