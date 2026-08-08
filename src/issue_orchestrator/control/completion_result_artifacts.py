"""Completion result artifacts, comments, and durable record helpers."""

import json
import logging
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ..domain.events import SessionEvent
from ..domain.models import COMPLETION_RECORD_PATH, CompletionRecord, RequestedAction
from ..domain.runtime_identity import RuntimeIdentity
from ..domain.session_run import SessionRunAssets
from ..ports.session_output import SessionOutput
from .completion_failure_reporting import (
    build_cleanup_failure_comment,
    build_processing_failure_comment,
    write_failure_diagnostic,
)
from .completion_types import (
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_GOVERNED_LABEL,
    ERROR_PREFIX_PUSH,
    ProcessingResult,
    REVIEW_EXCHANGE_ERROR_PREFIX,
)

logger = logging.getLogger(__name__)

EmitCompletionEvent = Callable[[SessionEvent, int, dict[str, Any]], None]
CleanupRecord = Callable[[Path, str | None], bool]


class PostIssueComment(Protocol):
    def __call__(self, issue_number: int, comment: str, *, context: str) -> None: ...


def build_processing_result(
    *,
    session_output: SessionOutput,
    worktree: Path,
    record: CompletionRecord,
    session_name: str | None,
    issue_number: int,
    issue_title: str,
    branch: str | None,
    pr_url: str | None,
    review_exchange_completed: bool,
    actions_taken: list[str],
    errors: list[str],
    error_details: list[dict[str, Any]],
    total_duration: float,
    completion_path: str | None,
    preserved_completion_path: str | None,
    run_assets: SessionRunAssets,
    emit_completion_event: EmitCompletionEvent,
    post_issue_comment: PostIssueComment,
    cleanup_completion_record_fn: Callable[[Path, str | None, int], None],
) -> ProcessingResult:
    """Build final processing result and handle completion diagnostics."""
    has_publish_error = any(
        error.startswith((ERROR_PREFIX_PUSH, ERROR_PREFIX_CREATE_PR))
        for error in errors
    )
    success = len(errors) == 0 or (
        not has_publish_error
        and RequestedAction.PUSH_BRANCH in record.requested_actions
        and "Pushed branch to remote" in actions_taken
    )
    if any(error.startswith(REVIEW_EXCHANGE_ERROR_PREFIX) for error in errors):
        success = False
    # Forced false, with no "but the push worked" escape (#6999 F2 round 5). The
    # clause above lets a successful push carry a completion to success despite
    # errors; a refused shared-block write must never ride along on that,
    # because the whole point of refusing it is that something downstream has to
    # SEE the request was dropped.
    if any(error.startswith(ERROR_PREFIX_GOVERNED_LABEL) for error in errors):
        success = False
    logger.info(
        "Completion result: issue=%s success=%s actions=%s errors=%s pr_url=%s",
        issue_number,
        success,
        actions_taken,
        errors,
        pr_url,
    )
    logger.info(
        "Completion processing duration: issue=%s elapsed=%.2fs",
        issue_number,
        total_duration,
    )

    diagnostic_path: str | None = None
    if success:
        message = f"Processed {record.outcome.value}: {', '.join(actions_taken)}"
        emit_completion_event(
            SessionEvent.COMPLETED,
            issue_number,
            {
                "outcome": record.outcome.value,
                "actions_taken": actions_taken,
                "pr_url": pr_url,
            },
        )
    else:
        message = f"Processing failed: {'; '.join(errors)}"
        emit_completion_event(
            SessionEvent.FAILED,
            issue_number,
            {
                "outcome": record.outcome.value,
                "actions_taken": actions_taken,
                "errors": errors,
            },
        )
        diagnostic_path = write_failure_diagnostic(
            session_output=session_output,
            worktree=worktree,
            session_name=session_name,
            issue_number=issue_number,
            issue_title=issue_title,
            branch=branch,
            outcome=record.outcome.value,
            requested_actions=[action.value for action in record.requested_actions],
            actions_taken=actions_taken,
            errors=errors,
            error_details=error_details,
            duration_seconds=total_duration,
            run_assets=run_assets,
        )
        comment = build_processing_failure_comment(
            errors=errors,
            actions_taken=actions_taken,
            diagnostic_path=diagnostic_path,
        )
        post_issue_comment(issue_number, comment, context="processing failure")

    cleanup_completion_record_fn(worktree, completion_path, issue_number)

    review_exchange_halted = any(
        error.startswith(REVIEW_EXCHANGE_ERROR_PREFIX) for error in errors
    )

    return ProcessingResult(
        success=success,
        message=message,
        pr_url=pr_url,
        actions_taken=actions_taken if actions_taken else None,
        diagnostic_path=diagnostic_path,
        completion_record_path=preserved_completion_path,
        errors=errors if errors else None,
        review_exchange_completed=review_exchange_completed,
        review_exchange_halted=review_exchange_halted,
    )


def preserve_completion_record(
    *,
    session_output: SessionOutput,
    worktree: Path,
    completion_path: str | None,
    run_assets: SessionRunAssets,
) -> str | None:
    """Persist a run-scoped completion copy before cleanup for timeline/audit use."""
    source_path = worktree / (completion_path or COMPLETION_RECORD_PATH)
    if not source_path.exists():
        return None

    artifact = run_assets.completion_record_copy
    target_path = artifact.path
    try:
        shutil.copy2(source_path, target_path)
        session_output.update_manifest(
            run_assets.run_dir,
            {"completion_record_path": str(target_path)},
        )
        return str(target_path)
    except Exception:
        logger.exception(
            "Failed to preserve completion record for run_dir=%s",
            run_assets.run_dir,
        )
        return None


def cleanup_completion_record(
    *,
    worktree: Path,
    completion_path: str | None,
    issue_number: int,
    cleanup_record: CleanupRecord,
    post_issue_comment: PostIssueComment,
) -> None:
    """Clean up the completion record after processing."""
    record_path = worktree / (completion_path or COMPLETION_RECORD_PATH)
    existed_before = record_path.exists()
    cleanup_ok = cleanup_record(worktree, completion_path)
    exists_after = record_path.exists()
    logger.warning(
        "CLEANUP: issue=%d path=%s existed_before=%s exists_after=%s",
        issue_number,
        record_path,
        existed_before,
        exists_after,
    )
    if existed_before and exists_after and not cleanup_ok:
        comment = build_cleanup_failure_comment(
            issue_number=issue_number,
            worktree=worktree,
            record_path=record_path,
        )
        post_issue_comment(issue_number, comment, context="cleanup warning")


def build_pr_body(
    record: CompletionRecord,
    issue_number: int,
    runtime_identity: RuntimeIdentity | None = None,
) -> str:
    """Build the PR body from the completion record.

    ``runtime_identity=None`` is for direct tests/helper callers. Production PR
    creation injects a runtime identity so the audit section is always present.
    """
    parts = [
        f"Closes #{issue_number}",
        "",
    ]

    if record.implementation:
        parts.extend([
            "## Implementation",
            record.implementation,
            "",
        ])

    if record.problems:
        parts.extend([
            "## Problems Encountered",
            record.problems,
            "",
        ])

    if runtime_identity is not None:
        parts.extend(_build_orchestration_audit(runtime_identity))

    parts.extend([
        "---",
        "*Generated by issue-orchestrator*",
    ])

    return "\n".join(parts)


def _build_orchestration_audit(
    runtime_identity: RuntimeIdentity,
) -> list[str]:
    commit = runtime_identity.source_commit_sha or "unknown"
    return [
        "## Orchestration Audit",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Orchestrator version | `{runtime_identity.package_version}` |",
        f"| Orchestrator commit | `{commit}` |",
        "",
    ]


def write_reviewer_feedback_file(
    run_dir: Path,
    pr_number: int,
    review_issues: str,
) -> Path | None:
    """Write reviewer feedback to the review session's run directory.

    This supports the local-cache pattern: when a rework session starts shortly
    after review, it can read feedback from the review run directory instead of
    depending on GitHub's eventual consistency.
    """
    feedback_file = run_dir / "reviewer-feedback.json"

    feedback_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pr_number": pr_number,
        "review_issues": review_issues,
    }

    try:
        feedback_file.write_text(json.dumps(feedback_data, indent=2))
        logger.info(
            "[REVIEW_FEEDBACK] Wrote reviewer feedback for PR #%d: %s",
            pr_number,
            feedback_file,
        )
        return feedback_file
    except Exception as exc:
        logger.warning(
            "[REVIEW_FEEDBACK] Failed to write feedback file for PR #%d: %s",
            pr_number,
            exc,
        )
        return None
