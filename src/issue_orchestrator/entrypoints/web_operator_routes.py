"""Dashboard operator action and session-control routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import threading
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ..control.label_manager import LabelManager
from ..control.queue_cache import QueueCache
from ..control.shutdown_manager import shutdown_manager
from ..execution.client_host import ClientHost
from ..execution.label_ops import LabelOperation, apply_label_operations
from .shutdown_reason_support import parse_shutdown_reason
from .web_session_context import WebOrchestratorDependency

logger = logging.getLogger(__name__)

web_operator_router = APIRouter()

_WEB_OPERATOR_DEPENDENCIES_STATE_KEY = "web_operator_dependencies"


@dataclass(frozen=True)
class WebOperatorDependencies:
    """Runtime adapters needed by operator action routes."""

    get_client_host: Callable[[], ClientHost]
    broadcast_event: Callable[[str, dict | None], Awaitable[None]]
    trigger_server_shutdown: Callable[[], None]
    # Optional accessor for the orchestrator's host repo root. Used to verify
    # that requested paths belong to this repository or one of its worktrees.
    get_host_repo_root: Callable[[], "Path | None"] = lambda: None
    # Optional accessor for the configured worktree base. Timeline actions
    # can point at an issue worktree root (for example ``../repo-360``), which
    # is outside ``.issue-orchestrator`` but still owned by this orchestrator.
    get_worktree_base: Callable[[], "Path | None"] = lambda: None


def install_web_operator_dependencies(
    app: FastAPI,
    *,
    get_client_host: Callable[[], ClientHost],
    broadcast_event: Callable[[str, dict | None], Awaitable[None]],
    trigger_server_shutdown: Callable[[], None],
    get_host_repo_root: Callable[[], "Path | None"] | None = None,
    get_worktree_base: Callable[[], "Path | None"] | None = None,
) -> None:
    """Install operator route dependencies on the FastAPI app."""
    deps_kwargs: dict[str, Any] = {
        "get_client_host": get_client_host,
        "broadcast_event": broadcast_event,
        "trigger_server_shutdown": trigger_server_shutdown,
    }
    if get_host_repo_root is not None:
        deps_kwargs["get_host_repo_root"] = get_host_repo_root
    if get_worktree_base is not None:
        deps_kwargs["get_worktree_base"] = get_worktree_base
    setattr(
        app.state,
        _WEB_OPERATOR_DEPENDENCIES_STATE_KEY,
        WebOperatorDependencies(**deps_kwargs),
    )


def get_web_operator_dependencies(request: Request) -> WebOperatorDependencies:
    """Return operator route dependencies for the current app."""
    deps = getattr(request.app.state, _WEB_OPERATOR_DEPENDENCIES_STATE_KEY, None)
    if not isinstance(deps, WebOperatorDependencies):
        raise RuntimeError("Web operator dependencies are not installed")
    return deps


WebOperatorDependency = Annotated[
    WebOperatorDependencies,
    Depends(get_web_operator_dependencies),
]


def _label_manager_for_api(orchestrator: Any) -> LabelManager:
    deps_lm = getattr(getattr(orchestrator, "deps", None), "label_manager", None)
    if isinstance(deps_lm, LabelManager):
        return deps_lm
    return LabelManager(orchestrator.config)


def _terminate_issue_and_hold(orchestrator: Any, issue_number: int, sessions: list[Any]) -> dict[str, Any]:
    """Terminate running sessions and apply a hold guard to prevent auto-requeue."""
    from ..domain.models import SessionHistoryEntry

    state = orchestrator.state
    repo = orchestrator.repository_host
    lm = _label_manager_for_api(orchestrator)

    killed_sessions: list[str] = []
    pr_numbers = sorted(
        {
            int(s.pr_number)
            for s in sessions
            if getattr(s, "pr_number", None) is not None
        }
    )

    errors = _terminate_sessions(orchestrator=orchestrator, sessions=sessions, killed_sessions=killed_sessions)
    orchestrator.cancel_review_exchange_for_issue(issue_number, reason="operator-terminated")
    _prune_issue_runtime_state(state=state, issue_number=issue_number)
    _append_operator_termination_history(
        state=state,
        issue_number=issue_number,
        primary_session=sessions[0],
        session_entry_cls=SessionHistoryEntry,
        now=datetime.now(),
    )
    state.failed_this_cycle.add(issue_number)

    # Label policy:
    # - issue: add blocked-failed guard, remove in-progress/pr-pending
    # - linked PR(s): add blocked-failed and remove needs-rework (scanner trigger)
    label_ops: list[LabelOperation] = [
        LabelOperation("add", issue_number, lm.blocked_failed),
        LabelOperation("remove", issue_number, lm.in_progress),
        LabelOperation("remove", issue_number, lm.pr_pending),
    ]
    for pr_number in pr_numbers:
        label_ops.extend(
            [
                LabelOperation("add", pr_number, lm.blocked_failed),
                LabelOperation("remove", pr_number, lm.needs_rework),
            ]
        )
    apply_label_operations(
        repo,
        label_ops,
        logger=logger,
        log_prefix="[terminate]",
    )

    return {
        "killed_sessions": killed_sessions,
        "errors": errors,
        "hold_label": lm.blocked_failed,
    }


def _terminate_sessions(*, orchestrator: Any, sessions: list[Any], killed_sessions: list[str]) -> list[str]:
    errors: list[str] = []
    for session in sessions:
        try:
            orchestrator.kill_session(session.terminal_id)
            killed_sessions.append(session.terminal_id)
        except Exception as exc:
            errors.append(f"{session.terminal_id}: {exc}")
    return errors


def _prune_issue_runtime_state(*, state: Any, issue_number: int) -> None:
    state.active_sessions = [s for s in state.active_sessions if s.issue.number != issue_number]
    state.pending_reviews = [r for r in state.pending_reviews if r.issue_number != issue_number]
    state.pending_reworks = [r for r in state.pending_reworks if r.resolve_issue_number() != issue_number]
    state.pending_tech_lead_reviews = [r for r in state.pending_tech_lead_reviews if r.issue_number != issue_number]
    state.pending_validation_retries = [r for r in state.pending_validation_retries if r.issue_number != issue_number]
    state.discovered_reviews = [r for r in state.discovered_reviews if r.issue_number != issue_number]
    state.discovered_reworks = [r for r in state.discovered_reworks if r.issue_number != issue_number]
    state.discovered_failures = [r for r in state.discovered_failures if r.issue_number != issue_number]
    state.immediate_cleanups = [c for c in state.immediate_cleanups if c.issue_number != issue_number]


def _resolve_agent_label(primary_session: Any) -> str:
    agent_label = primary_session.agent_label
    if agent_label:
        return str(agent_label)
    for label in primary_session.issue.labels:
        if label.startswith("agent:"):
            return label
    return "agent:unknown"


def _append_operator_termination_history(
    *,
    state: Any,
    issue_number: int,
    primary_session: Any,
    session_entry_cls: Any,
    now: Any,
) -> None:
    state.session_history.append(
        session_entry_cls(
            issue_number=issue_number,
            title=primary_session.issue.title,
            agent_type=_resolve_agent_label(primary_session),
            status="blocked",
            runtime_minutes=primary_session.runtime_minutes,
            status_reason="Terminated by operator",
            worktree_path=primary_session.worktree_path,
            completed_at=now,
            issue_labels=tuple(primary_session.issue.labels),
        )
    )


def _queue_related_pr_numbers(state: Any, issue_number: int) -> list[int]:
    pr_numbers = {
        int(review.pr_number)
        for review in state.pending_reviews
        if review.issue_number == issue_number
    }
    pr_numbers.update(
        int(rework.pr_number)
        for rework in state.pending_reworks
        if rework.resolve_issue_number() == issue_number and rework.pr_number is not None
    )
    return sorted(pr_numbers)


def _hold_queued_issue(orchestrator: Any, issue_number: int) -> dict[str, Any]:
    """Place a queued issue on hold and remove it from launchable runtime state."""
    from ..domain.models import SessionHistoryEntry

    state = orchestrator.state
    repo = orchestrator.repository_host
    lm = _label_manager_for_api(orchestrator)

    issue = next((candidate for candidate in state.cached_queue_issues if candidate.number == issue_number), None)
    if issue is None:
        raise LookupError(f"Issue #{issue_number} not found in queued state")

    pr_numbers = _queue_related_pr_numbers(state, issue_number)
    label_ops: list[LabelOperation] = [
        LabelOperation("add", issue_number, lm.blocked_failed),
        LabelOperation("remove", issue_number, lm.in_progress),
        LabelOperation("remove", issue_number, lm.pr_pending),
    ]
    for pr_number in pr_numbers:
        label_ops.extend(
            [
                LabelOperation("add", pr_number, lm.blocked_failed),
                LabelOperation("remove", pr_number, lm.code_review),
                LabelOperation("remove", pr_number, lm.needs_rework),
            ]
        )
    apply_label_operations(
        repo,
        label_ops,
        logger=logger,
        log_prefix="[cancel-queued]",
    )

    QueueCache(
        orchestrator.config,
        state,
        orchestrator.deps.queue_cache_store,
    ).remove_issue_and_save(issue_number)
    _prune_issue_runtime_state(state=state, issue_number=issue_number)
    state.session_history.append(
        SessionHistoryEntry(
            issue_number=issue_number,
            title=issue.title,
            agent_type=issue.agent_type or "agent:unknown",
            status="blocked",
            runtime_minutes=0,
            status_reason="Cancelled from queue by operator",
            worktree_path=None,
            completed_at=datetime.now(timezone.utc),
            issue_labels=tuple(issue.labels),
        )
    )
    state.failed_this_cycle.add(issue_number)
    return {
        "issue_number": issue_number,
        "title": issue.title,
        "hold_label": lm.blocked_failed,
        "linked_pr_numbers": pr_numbers,
    }


@web_operator_router.post("/api/kill/{issue_number}")
async def kill_session(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Force terminate an issue session and prevent automatic relaunch."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    sessions = [s for s in orchestrator.state.active_sessions if s.issue.number == issue_number]
    if not sessions:
        return JSONResponse({"error": f"Session #{issue_number} not found"}, status_code=404)

    terminated = _terminate_issue_and_hold(orchestrator, issue_number, sessions)
    if not terminated["killed_sessions"]:
        return JSONResponse(
            {"error": "Failed to terminate session(s)", "details": terminated["errors"]},
            status_code=500,
        )

    return JSONResponse({
        "status": "terminated",
        "issue_number": issue_number,
        "title": sessions[0].issue.title,
        "killed_sessions": terminated["killed_sessions"],
        "hold_label": terminated["hold_label"],
        "errors": terminated["errors"],
    })


@web_operator_router.post("/api/focus/{issue_number}")
async def focus_session(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Focus the terminal session for a specific issue."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    session = None
    for s in orchestrator.state.active_sessions:
        if s.issue.number == issue_number:
            session = s
            break

    if not session:
        return JSONResponse({"error": f"Session #{issue_number} not found"}, status_code=404)

    if orchestrator.session_runner.focus_session(issue_number, session.terminal_id):
        return JSONResponse({"status": "focused", "issue_number": issue_number})
    return JSONResponse({"error": f"Could not focus session #{issue_number}"}, status_code=500)


async def _reveal_worktree(
    issue_number: int,
    orchestrator: Any,
    operator_deps: WebOperatorDependencies,
) -> JSONResponse:
    """Reveal the worktree path in the current client host for a specific session."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    session = None
    for s in orchestrator.state.active_sessions:
        if s.issue.number == issue_number:
            session = s
            break

    if not session:
        return JSONResponse({"error": f"Session #{issue_number} not found"}, status_code=404)

    worktree_path = session.worktree_path
    if not worktree_path.exists():
        return JSONResponse({"error": f"Worktree not found: {worktree_path}"}, status_code=404)

    try:
        result = operator_deps.get_client_host().reveal_worktree(worktree_path)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    status_code = 200 if result.action == "opened" else 409
    return JSONResponse({"issue_number": issue_number, **result.to_dict()}, status_code=status_code)


@web_operator_router.post("/api/host/reveal-worktree/{issue_number}")
async def reveal_worktree(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    operator_deps: WebOperatorDependency,
) -> JSONResponse:
    """Reveal the worktree path in the current client host."""
    return await _reveal_worktree(issue_number, orchestrator, operator_deps)


@web_operator_router.post("/api/finder/{issue_number}")
async def open_in_finder(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    operator_deps: WebOperatorDependency,
) -> JSONResponse:
    """Deprecated alias for revealing a worktree path in the current client host."""
    return await _reveal_worktree(issue_number, orchestrator, operator_deps)


@web_operator_router.post("/api/prompt/{agent_type}")
async def open_agent_prompt(
    agent_type: str,
    orchestrator: WebOrchestratorDependency,
    operator_deps: WebOperatorDependency,
) -> JSONResponse:
    """Open the agent prompt, or return a copy-path action when local opening is unavailable."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    full_label = agent_type if agent_type.startswith("agent:") else f"agent:{agent_type}"
    agent_config = orchestrator.config.agents.get(full_label)
    if not agent_config:
        return JSONResponse({"error": f"Agent type '{agent_type}' not found"}, status_code=404)

    prompt_path = agent_config.prompt_path
    if not prompt_path.is_absolute():
        prompt_path = orchestrator.config.repo_root / prompt_path
    if not prompt_path.exists():
        return JSONResponse({"error": f"Prompt file not found: {prompt_path}"}, status_code=404)

    try:
        result = operator_deps.get_client_host().open_path(prompt_path)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    status_code = 200 if result.action == "opened" else 409
    return JSONResponse({"status": result.action, **result.to_dict()}, status_code=status_code)


@web_operator_router.post("/api/shutdown")
async def shutdown(
    request: Request,
    orchestrator: WebOrchestratorDependency,
    operator_deps: WebOperatorDependency,
    force: bool = False,
) -> JSONResponse:
    """Request orchestrator shutdown.

    Clients MUST include a non-empty ``reason`` in the JSON body so
    later log reads can answer "what caused this shutdown?". Before
    this requirement landed, the signal handler logged "Received
    shutdown signal" with no source info and operators had to guess
    whether it came from the cc, a browser tab, a CLI, or an external
    SIGTERM. Requiring a reason here pushes the "who/why" answer
    into the call site that knew it.

    Body shape: ``{"reason": "<short string>", "actor": "<source>"}``.
    ``actor`` is optional and carries a stable identifier for the
    caller (cc, browser, cli, test-harness) so aggregated logs can
    group shutdowns by source.
    """
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → handled below
        payload = {}
    parsed = parse_shutdown_reason(
        payload,
        endpoint="/api/shutdown",
        default_actor="",
    )
    if isinstance(parsed, JSONResponse):
        return parsed
    reason = parsed.reason
    actor_str = parsed.actor  # "" when caller didn't supply one

    orchestrator.request_shutdown(force=force)
    active_count = len(orchestrator.state.active_sessions)

    shutdown_log_reason = (
        f"API /api/shutdown reason={reason!r}"
        + (f" actor={actor_str!r}" if actor_str else "")
        + (" force=true" if force else "")
    )
    shutdown_manager.request_shutdown(reason=shutdown_log_reason)
    await operator_deps.broadcast_event(
        "shutdown_requested",
        {
            "force": force,
            "active_sessions": active_count,
            "reason": reason,
            "actor": actor_str or None,
        },
    )
    operator_deps.trigger_server_shutdown()

    timer = threading.Timer(0.2, shutdown_manager.exit)
    timer.daemon = False
    timer.start()

    return JSONResponse({
        "status": "force_shutdown" if force else "shutdown_requested",
        "active_sessions": active_count,
        "reason": reason,
        "actor": actor_str or None,
    })


@web_operator_router.post("/api/send/{issue_number}")
async def send_input(
    issue_number: int,
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Send input to a running agent session."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON payload"}, status_code=400)

    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "Text is required"}, status_code=400)

    session = None
    for s in orchestrator.state.active_sessions:
        if s.issue.number == issue_number:
            session = s
            break

    if not session:
        return JSONResponse({"error": f"Session #{issue_number} not found"}, status_code=404)

    ok = orchestrator.session_runner.send_to_session(issue_number, text, session.terminal_id)
    if not ok:
        return JSONResponse({"error": f"Failed to send input to #{issue_number}"}, status_code=500)

    return JSONResponse({"status": "sent", "issue_number": issue_number})


@web_operator_router.post("/api/bulk-kill")
async def bulk_kill(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Terminate sessions and hold issues until explicit retry/unblock."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    body = await request.json()
    issue_numbers = body.get("issue_numbers", [])
    terminated: list[int] = []
    failed: list[dict[str, Any]] = []
    for num in issue_numbers:
        sessions = [s for s in orchestrator.state.active_sessions if s.issue.number == num]
        if not sessions:
            failed.append({"issue_number": num, "error": "Session not found"})
            continue
        result = _terminate_issue_and_hold(orchestrator, num, sessions)
        if result["killed_sessions"]:
            terminated.append(num)
        else:
            failed.append(
                {"issue_number": num, "error": "Failed to terminate", "details": result["errors"]}
            )
    return JSONResponse({"terminated": terminated, "failed": failed})


@web_operator_router.post("/api/bulk-cancel-queued")
async def bulk_cancel_queued(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Place queued issues on hold so they are not launched automatically."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    body = await request.json()
    issue_numbers = body.get("issue_numbers", [])
    if not isinstance(issue_numbers, list):
        return JSONResponse({"error": "issue_numbers must be a list"}, status_code=400)

    cancelled: list[int] = []
    failed: list[dict[str, Any]] = []
    for raw_number in issue_numbers:
        try:
            issue_number = int(raw_number)
        except (TypeError, ValueError):
            failed.append({"issue_number": raw_number, "error": "Invalid issue number"})
            continue

        try:
            result = _hold_queued_issue(orchestrator, issue_number)
        except LookupError:
            failed.append({"issue_number": issue_number, "error": "Issue not found in queue"})
            continue
        except Exception as exc:
            failed.append({"issue_number": issue_number, "error": str(exc)})
            continue
        cancelled.append(result["issue_number"])

    return JSONResponse({"cancelled": cancelled, "failed": failed})


def _is_generated_worktree_path(
    file_path: str,
    *,
    host_repo_root: "Path | None",
    worktree_base: "Path | None",
) -> bool:
    if host_repo_root is None:
        return False
    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute():
        return False
    try:
        host_root = host_repo_root.expanduser().resolve()
        base = (worktree_base or host_root.parent).expanduser().resolve()
        resolved = candidate.resolve()
        relative = resolved.relative_to(base)
    except (OSError, ValueError):
        return False
    if not relative.parts:
        return False
    repo_name = host_root.name
    worktree_name = relative.parts[0]
    pattern = re.compile(rf"^{re.escape(repo_name)}-\d+(?:-review-[^/]+)?$")
    return pattern.fullmatch(worktree_name) is not None


def _is_safe_open_path(
    file_path: str,
    operator_deps: WebOperatorDependencies,
) -> bool:
    safe_prefixes = [
        str(Path.home() / ".claude"),
        str(Path.home() / ".issue-orchestrator"),
        "/tmp/",
    ]
    if "/.issue-orchestrator/" in file_path:
        return True
    if any(file_path.startswith(prefix) for prefix in safe_prefixes):
        return True
    return _is_generated_worktree_path(
        file_path,
        host_repo_root=operator_deps.get_host_repo_root(),
        worktree_base=operator_deps.get_worktree_base(),
    )


async def _open_host_path(request: Request, operator_deps: WebOperatorDependencies) -> JSONResponse:
    """Open a file via the current client-host integration."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    file_path = body.get("path")
    if not file_path:
        return JSONResponse({"error": "path is required"}, status_code=400)

    if not _is_safe_open_path(file_path, operator_deps):
        return JSONResponse({"error": "Cannot open files outside safe directories"}, status_code=403)

    resolved_path = Path(file_path)
    if not resolved_path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    try:
        result = operator_deps.get_client_host().open_path(resolved_path)
        status_code = 200 if result.action == "opened" else 409
        return JSONResponse(result.to_dict(), status_code=status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@web_operator_router.post("/api/host/open-path")
async def open_host_path(
    request: Request,
    operator_deps: WebOperatorDependency,
) -> JSONResponse:
    """Open a path via the current client-host integration."""
    return await _open_host_path(request, operator_deps)


@web_operator_router.post("/api/open-file")
async def open_file(
    request: Request,
    operator_deps: WebOperatorDependency,
) -> JSONResponse:
    """Deprecated alias for opening a path via the current client-host integration."""
    return await _open_host_path(request, operator_deps)
