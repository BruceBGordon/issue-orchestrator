"""What a finished session looks like in history, and why.

An agent's reported outcome and the outcome an operator should *see* are not
always the same: "completed" with a failed push is a red dot, not a green one.
That mapping is a policy with one owner, kept out of the completion handler's
sequencing so the handler reads as steps rather than as judgement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..domain.models import SessionStatus
from .completion_action_planner import critical_processing_errors
from .invalid_record_actions import invalid_record_failure_reason

logger = logging.getLogger(__name__)

_PUBLISH_STAGE_LABELS = {"push_branch": "Push", "create_pr": "PR creation"}

# Maximum length of the blocked-card status-reason line. Cards render the
# reason inline; anything longer wraps ugly or truncates without ellipsis
# depending on the surface. Kept near the body-column width used by the
# dashboard templates so tweaks to layout have one obvious knob to turn.
_PUBLISH_FAILURE_SUMMARY_CHAR_CAP = 160


def _summarize_publish_failure(critical_errors: list[str]) -> str:
    """Card-friendly one-line summary from raw publish error strings.

    Strips the ``push_branch:``/``create_pr:`` stage prefix and caps length so it
    renders inside a card; falls back to generic text on an unexpected shape.
    """
    if not critical_errors:
        return "Push or PR creation failed"
    raw = critical_errors[0].strip()
    stage_prefix, sep, remainder = raw.partition(":")
    stage_label = _PUBLISH_STAGE_LABELS.get(stage_prefix.strip())
    if sep and stage_label:
        message = remainder.strip()
    else:
        message = raw
    message = " ".join(message.split())  # collapse whitespace/newlines
    if not message:
        return "Push or PR creation failed"
    prefix = f"{stage_label} failed: " if stage_label else ""
    available = _PUBLISH_FAILURE_SUMMARY_CHAR_CAP - len(prefix)
    if len(message) > available:
        message = message[: available - 1].rstrip() + "…"
    return f"{prefix}{message}"


@dataclass(frozen=True)
class HistoryStatus:
    """The status an operator sees for a finished session, and its reason."""

    status: SessionStatus
    reason: str | None


def resolve_history_status(
    *,
    status: SessionStatus,
    issue_number: int,
    pr_url: str | None,
    processing_errors: list[str] | None,
    review_exchange_halted: bool,
    completion_detail: dict[str, object] | None,
) -> HistoryStatus:
    """Map an agent-reported outcome onto the one history shows.

    A session that reported COMPLETED but could not publish, or whose review
    exchange halted, did not succeed from anyone's point of view but the
    agent's — history says FAILED and names the cause.
    """
    critical_errors, _downgraded = critical_processing_errors(
        processing_errors,
        pr_url=pr_url,
        issue_number=issue_number,
        log_downgraded=True,
        context="history",
    )
    if status == SessionStatus.COMPLETED and critical_errors:
        logger.info(
            "[COMPLETION] Agent reported completed but push/PR failed - "
            "using FAILED for history: issue=%d",
            issue_number,
        )
        return HistoryStatus(
            SessionStatus.FAILED, _summarize_publish_failure(critical_errors)
        )
    if status == SessionStatus.COMPLETED and review_exchange_halted:
        logger.info(
            "[COMPLETION] Review exchange halted - using FAILED for history/trace: "
            "issue=%d",
            issue_number,
        )
        return HistoryStatus(SessionStatus.FAILED, "Review exchange halted")
    return HistoryStatus(status, invalid_record_failure_reason(completion_detail))


__all__ = ["HistoryStatus", "resolve_history_status"]
