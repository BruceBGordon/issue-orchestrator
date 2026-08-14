"""Dashboard history and retry action routes."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..control.queue_cache import (
    QueueCache,
    QueueMutationStatus,
    clear_issue_refresh,
    record_issue_refreshes,
)
from ..control.retry_history_state import RetryHistoryState
from ..events import EventName
from ..history import latest_history_entries_by_issue
from ..ports.event_sink import make_trace_event
from .web_issue_number_payload import parse_issue_numbers_payload
from .web_session_context import WebOrchestratorDependency
from .web_retry_runtime import terminate_reset_retry_runtime

if TYPE_CHECKING:
    from ..control.maintenance import ResetResult
    from ..domain.models import OrchestratorState

logger = logging.getLogger(__name__)

web_retry_history_router = APIRouter()


@web_retry_history_router.get("/api/history")
async def get_history(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get session history entries for completed sessions."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    entries = []
    for entry in latest_history_entries_by_issue(
        orchestrator.state.session_history,
        limit=50,
    ):
        entries.append({
            "issue_number": entry.issue_number,
            "title": entry.title,
            "agent_type": entry.agent_type,
            "status": entry.status,
            "runtime_minutes": entry.runtime_minutes,
            "pr_url": entry.pr_url,
            "status_reason": entry.status_reason,
            "worktree_path": str(entry.worktree_path) if entry.worktree_path else None,
        })

    return JSONResponse({"history": entries, "count": len(entries)})


@web_retry_history_router.post("/api/history/clear")
async def clear_history(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Clear all session history."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    result = RetryHistoryState(orchestrator.state).clear_history()
    return JSONResponse({"cleared": result.cleared_history_entries})


@web_retry_history_router.post("/api/history/dismiss/{issue_number}")
async def dismiss_history_entry(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Dismiss a single history entry."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    result = RetryHistoryState(orchestrator.state).remove_issue_from_history(
        issue_number
    )
    return JSONResponse({"dismissed": result.removed_history_entries})


@web_retry_history_router.post("/api/retry/{issue_number}")
async def retry_issue(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Remove issue from history so it can be retried."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    RetryHistoryState(orchestrator.state).remove_issue_from_history(issue_number)
    return JSONResponse({
        "retrying": issue_number,
        "message": "Issue will be picked up on next cycle",
    })


@web_retry_history_router.post("/api/issues/{issue_number}/retry-publish")
async def retry_publish_issue(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Retry publish for a publish-failed issue using its persisted retry locators."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    result = orchestrator.deps.publish_recovery.retry_publish(
        issue_number,
        orchestrator.state,
    )
    if result.status == "rejected":
        return JSONResponse({"error": result.message}, status_code=409)

    return JSONResponse({
        "status": result.status,
        "message": result.message,
        "issue_number": issue_number,
        "job_id": result.job_id,
        "pr_url": result.pr_url,
        "pr_number": result.pr_number,
    })


@web_retry_history_router.post("/api/bulk-retry")
async def bulk_retry(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Re-queue multiple blocked issues for retry."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    body = await request.json()
    issue_numbers = body.get("issue_numbers", [])
    retried = RetryHistoryState(orchestrator.state).remove_issues_from_history(
        issue_numbers
    )
    return JSONResponse({"retried": retried})


@web_retry_history_router.post("/api/bulk-deprioritize")
async def bulk_deprioritize(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Remove issues from the priority queue."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    body = await request.json()
    issue_numbers = body.get("issue_numbers", [])
    removed = RetryHistoryState(orchestrator.state).deprioritize_issues(issue_numbers)
    return JSONResponse({"deprioritized": removed})


@web_retry_history_router.post("/api/unblock-retry")
async def unblock_and_retry(  # noqa: C901 - multi-step unblock with state transitions
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Remove retry-blocking labels from issues and trigger a refresh."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    # Lazy imports keep this action-heavy path out of module import time and preserve
    # tests' ability to patch action classes at the source module.
    from ..control.actions import RemoveLabelAction
    from ..control.retry_policy import labels_to_remove_for_retry

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    issue_numbers = body.get("issues", [])
    if not issue_numbers or not isinstance(issue_numbers, list):
        return JSONResponse(
            {"error": "issues must be a non-empty list"},
            status_code=400,
        )

    state = orchestrator.state
    retry_history = RetryHistoryState(state)
    repository_host = orchestrator.repository_host
    action_applier = orchestrator.deps.action_applier
    lm = orchestrator.deps.label_manager

    unblocked = []
    failed = []

    for issue_number in issue_numbers:
        try:
            current_labels = repository_host.get_issue_labels(issue_number)
            labels_to_remove = labels_to_remove_for_retry(current_labels, lm)

            if labels_to_remove:
                for label in labels_to_remove:
                    action = RemoveLabelAction(
                        issue_number=issue_number,
                        label=label,
                        reason="unblock via web",
                    )
                    result = action_applier.apply(action)
                    if result.success:
                        logger.info(
                            "[unblock] Removed label '%s' from issue #%d",
                            label,
                            issue_number,
                        )
                    else:
                        logger.warning(
                            "[unblock] Failed to remove label '%s' from #%d: %s",
                            label,
                            issue_number,
                            result.error or "unknown error",
                        )

            retry_history.remove_issue_from_history(issue_number)

            unblocked.append(issue_number)
        except Exception as e:
            logger.error("[unblock] Failed to unblock issue #%d: %s", issue_number, e)
            failed.append({"issue": issue_number, "error": str(e)})

    if unblocked:
        orchestrator.request_refresh()
        logger.info("[unblock] Unblocked %d issues, refresh triggered", len(unblocked))

    return JSONResponse({
        "unblocked": unblocked,
        "failed": failed,
        "refresh_triggered": len(unblocked) > 0,
    })


@web_retry_history_router.post("/api/reset-retry")
async def reset_and_retry(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Reset issues completely and trigger retry."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    # reset_issue pulls in maintenance dependencies only needed by this endpoint.
    from ..control.maintenance import reset_issue

    parsed = await parse_issue_numbers_payload(request)
    if parsed.error_response is not None:
        return parsed.error_response

    issue_numbers = parsed.issue_numbers
    from_scratch = bool(parsed.body.get("from_scratch", False))
    request_started_at = time.monotonic()
    logger.debug(
        "[reset-retry] Request received: issues=%s from_scratch=%s count=%d",
        issue_numbers,
        from_scratch,
        len(issue_numbers),
    )

    state = orchestrator.state
    config = orchestrator.config
    repository_host = orchestrator.repository_host
    deps = orchestrator.deps
    lm = deps.label_manager
    queue_cache = QueueCache(config, state, deps.queue_cache_store)

    reset_results: list[dict] = []
    failed: list[dict] = []
    pending_label = lm.reset_retry_pending
    scratch_pending_label = lm.reset_retry_scratch_pending

    for issue_number in issue_numbers:
        success_payload, failure_payload = reset_and_retry_issue(
            issue_number=issue_number,
            from_scratch=from_scratch,
            pending_label=pending_label,
            scratch_pending_label=scratch_pending_label,
            repository_host=repository_host,
            queue_cache=queue_cache,
            state=state,
            deps=deps,
            config=config,
            reset_issue_fn=reset_issue,
        )
        if success_payload is not None:
            reset_results.append(success_payload)
            continue
        if failure_payload is not None:
            failed.append(failure_payload)
            continue
        failed.append({"issue": issue_number, "error": "Unknown reset+retry failure"})

    logger.info(
        "[reset-retry] Request complete: issues=%s reset=%s failed=%s "
        "from_scratch=%s duration_ms=%d",
        issue_numbers,
        [result["issue"] for result in reset_results],
        [failure.get("issue") for failure in failed],
        from_scratch,
        elapsed_ms(request_started_at),
    )
    return JSONResponse({
        "reset": reset_results,
        "failed": failed,
        "from_scratch": from_scratch,
        "refresh_triggered": False,
    })


def reset_and_retry_issue(  # noqa: PLR0913
    *,
    issue_number: int,
    from_scratch: bool,
    pending_label: str,
    scratch_pending_label: str,
    repository_host: Any,
    queue_cache: QueueCache,
    state: "OrchestratorState",
    deps: Any,
    config: Any,
    reset_issue_fn: Callable[..., "ResetResult"],
    current_labels: Sequence[str] | None = None,
    extra_pending_labels: Sequence[str] = (),
    source: str = "web.reset-retry",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    # AddLabelAction is lazy for the same reason as RemoveLabelAction above.
    from ..control.actions import AddLabelAction

    issue_started_at = time.monotonic()
    try:
        logger.debug(
            "[reset-retry] Begin issue reset: issue=%d from_scratch=%s",
            issue_number,
            from_scratch,
        )
        # Reset is a hard issue-runtime boundary. Stop visible issue/rework
        # terminals and hidden review-exchange pair/job work before local
        # state or worktrees are removed, otherwise a live subprocess can keep
        # writing into a reset attempt or leave stale active-session gating.
        terminate_reset_retry_runtime(
            issue_number=issue_number,
            state=state,
            deps=deps,
        )

        provided_marks = current_labels
        if provided_marks is None:
            labels_started_at = time.monotonic()
            current_marks = repository_host.get_issue_labels(issue_number)
            logger.debug(
                "[reset-retry] Current labels fetched: issue=%d labels=%s "
                "duration_ms=%d",
                issue_number,
                current_marks,
                elapsed_ms(labels_started_at),
            )
        else:
            current_marks = list(provided_marks)
            logger.debug(
                "[reset-retry] Current labels reused: issue=%d labels=%s",
                issue_number,
                current_marks,
            )
        reset_started_at = time.monotonic()
        result = reset_issue_fn(
            issue_number=issue_number,
            config=config,
            worktree_manager=deps.worktree_manager,
            working_copy=deps.working_copy,
            action_applier=deps.action_applier,
            label_manager=deps.label_manager,
            current_labels=current_marks,
            session_history=state.session_history,
            completed_today=state.completed_today,
            label_store=deps.label_store,
            timeline_store=deps.timeline_store if from_scratch else None,
            from_scratch=from_scratch,
            repository_host=repository_host,
        )
        logger.debug(
            "[reset-retry] Reset operation returned: issue=%d success=%s "
            "labels_removed=%s superseded_prs=%s deleted_branches=%s "
            "timeline_events_deleted=%s duration_ms=%d",
            issue_number,
            result.success,
            result.labels_removed or [],
            result.superseded_prs or [],
            result.deleted_branches or [],
            result.timeline_events_deleted,
            elapsed_ms(reset_started_at),
        )
        if not result.success:
            return None, _make_reset_failure(
                issue_number,
                result,
                result.error or "Unknown error",
            )
        if from_scratch:
            _clear_scratch_retry_pending_state(state, issue_number, result)

        pending_labels_to_add = _pending_labels_for_retry(
            from_scratch=from_scratch,
            pending_label=pending_label,
            scratch_pending_label=scratch_pending_label,
            extra_pending_labels=extra_pending_labels,
        )
        pending_label_error = _apply_reset_retry_pending_labels(
            issue_number=issue_number,
            labels=pending_labels_to_add,
            action_applier=deps.action_applier,
            add_label_action_cls=AddLabelAction,
        )
        if pending_label_error is not None:
            failure = _make_reset_failure(
                issue_number,
                result,
                pending_label_error,
                from_scratch=from_scratch,
            )
            return None, failure

        enqueue_error = _enqueue_reset_retry_issue(
            issue_number=issue_number,
            repository_host=repository_host,
            queue_cache=queue_cache,
            state=state,
            pending_labels_to_add=pending_labels_to_add,
            from_scratch=from_scratch,
            result=result,
        )
        if enqueue_error is not None:
            return None, enqueue_error

        _emit_reset_retry_unblocked(
            issue_number=issue_number,
            from_scratch=from_scratch,
            pending_label=pending_label,
            pending_labels_to_add=pending_labels_to_add,
            events=deps.events,
            source=source,
        )
        success = _make_reset_success(
            issue_number,
            result,
            from_scratch,
            pending_label,
            pending_labels_to_add,
        )
        logger.debug(
            "[reset-retry] Reset issue #%d: worktree=%s branch=%s labels=%s "
            "pending=%s from_scratch=%s queued_now=true",
            issue_number,
            result.deleted_worktree or "(none)",
            result.deleted_branch or "(none)",
            result.labels_removed or "(none)",
            pending_label,
            from_scratch,
        )
        logger.debug(
            "[reset-retry] Issue reset complete: issue=%d from_scratch=%s "
            "duration_ms=%d",
            issue_number,
            from_scratch,
            elapsed_ms(issue_started_at),
        )
        return success, None
    except Exception as exc:
        logger.error(
            "[reset-retry] Failed to reset issue #%d (from_scratch=%s): %s",
            issue_number,
            from_scratch,
            exc,
            exc_info=True,
        )
        return None, {"issue": issue_number, "error": str(exc)}


def _clear_scratch_retry_pending_state(
    state: "OrchestratorState",
    issue_number: int,
    result: "ResetResult",
) -> None:
    clear_result = RetryHistoryState(state).clear_scratch_retry_pending_state(
        issue_number=issue_number,
        superseded_prs=result.superseded_prs or (),
    )
    logger.debug(
        "[reset-retry] Cleared scratch retry pending state for issue #%d: "
        "reviews %d->%d reworks %d->%d cleanups %d->%d superseded_prs=%s",
        issue_number,
        clear_result.review_count_before,
        clear_result.review_count_after,
        clear_result.rework_count_before,
        clear_result.rework_count_after,
        clear_result.cleanup_count_before,
        clear_result.cleanup_count_after,
        list(clear_result.superseded_prs),
    )


def _pending_labels_for_retry(
    *,
    from_scratch: bool,
    pending_label: str,
    scratch_pending_label: str,
    extra_pending_labels: Sequence[str] = (),
) -> list[str]:
    labels = [pending_label]
    if from_scratch:
        labels.append(scratch_pending_label)
    labels.extend(label for label in extra_pending_labels if label not in labels)
    return labels


def _apply_reset_retry_pending_labels(
    *,
    issue_number: int,
    labels: list[str],
    action_applier: Any,
    add_label_action_cls: Any,
) -> str | None:
    for label in labels:
        result = action_applier.apply(
            add_label_action_cls(
                issue_number=issue_number,
                label=label,
                reason="reset+retry requested via web",
            )
        )
        if not result.success:
            return result.error or f"Failed to set {label}"
    return None


def _enqueue_reset_retry_issue(
    *,
    issue_number: int,
    repository_host: Any,
    queue_cache: QueueCache,
    state: Any,
    pending_labels_to_add: list[str],
    from_scratch: bool,
    result: Any,
) -> dict[str, Any] | None:
    enqueue_started_at = time.monotonic()
    refreshed_issue = repository_host.get_issue(issue_number)
    if refreshed_issue is None:
        return _make_reset_failure(
            issue_number,
            result,
            f"Issue #{issue_number} not found after reset",
        )

    outcome = queue_cache.upsert_refreshed_issue(refreshed_issue)
    refreshed_at = time.time()
    if outcome.status == QueueMutationStatus.ACCEPTED:
        record_issue_refreshes(state, {issue_number}, refreshed_at)
        queue_cache.prune_refresh_timestamps()
        queue_cache.save_snapshot()
        priority_inserted = RetryHistoryState(state).prioritize_issue_front(issue_number)
        logger.debug(
            "[reset-retry] Queue cache accepted issue: issue=%d labels=%s "
            "updated_existing=%s priority_inserted=%s queue_size=%d "
            "duration_ms=%d",
            issue_number,
            list(refreshed_issue.labels),
            outcome.updated,
            priority_inserted,
            len(state.cached_queue_issues),
            elapsed_ms(enqueue_started_at),
        )
        return None

    clear_issue_refresh(state, issue_number)
    queue_cache.prune_refresh_timestamps()
    queue_cache.save_snapshot()
    logger.debug(
        "[reset-retry] Queue cache rejected issue: issue=%d status=%s labels=%s "
        "queue_size=%d duration_ms=%d",
        issue_number,
        outcome.status.value,
        list(refreshed_issue.labels),
        len(state.cached_queue_issues),
        elapsed_ms(enqueue_started_at),
    )
    return _make_reset_failure(
        issue_number,
        result,
        (
            f"Issue #{issue_number} is not queue-eligible after reset "
            f"({outcome.status.value})"
        ),
        pending_labels=pending_labels_to_add,
        from_scratch=from_scratch,
    )


def _emit_reset_retry_unblocked(
    *,
    issue_number: int,
    from_scratch: bool,
    pending_label: str,
    pending_labels_to_add: list[str],
    events: Any,
    source: str,
) -> None:
    events.publish(
        make_trace_event(
            EventName.ISSUE_UNBLOCKED,
            {
                "issue_number": issue_number,
                "reason": "reset_retry_requested",
                "source": source,
                "pending_label": pending_label,
                "pending_labels": pending_labels_to_add,
                "from_scratch": from_scratch,
            },
        )
    )
    logger.debug(
        "[reset-retry] Published UI refresh event: event=%s issue=%d "
        "from_scratch=%s pending_labels=%s",
        EventName.ISSUE_UNBLOCKED.value,
        issue_number,
        from_scratch,
        pending_labels_to_add,
    )


def _make_reset_success(
    issue_number: int,
    result: "ResetResult",
    from_scratch: bool,
    pending_label: str,
    pending_labels_to_add: list[str],
) -> dict[str, Any]:
    return {
        "issue": issue_number,
        "deleted_worktree": result.deleted_worktree,
        "deleted_branch": result.deleted_branch,
        "deleted_branches": result.deleted_branches,
        "superseded_prs": result.superseded_prs,
        "timeline_events_deleted": result.timeline_events_deleted,
        "labels_removed": result.labels_removed,
        "pending_label": pending_label,
        "pending_labels": pending_labels_to_add,
        "from_scratch": from_scratch,
        "queued_now": True,
    }


def _make_reset_failure(
    issue_number: int,
    result: "ResetResult",
    error: str,
    *,
    pending_labels: list[str] | None = None,
    from_scratch: bool | None = None,
) -> dict[str, Any]:
    partial: dict[str, Any] = {
        "deleted_worktree": result.deleted_worktree,
        "deleted_branch": result.deleted_branch,
        "deleted_branches": result.deleted_branches,
        "superseded_prs": result.superseded_prs,
        "timeline_events_deleted": result.timeline_events_deleted,
        "labels_removed": result.labels_removed,
    }
    if pending_labels:
        partial["pending_labels"] = pending_labels
    if from_scratch is not None:
        partial["from_scratch"] = from_scratch
    return {
        "issue": issue_number,
        "error": error,
        "partial": partial,
    }


def elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
