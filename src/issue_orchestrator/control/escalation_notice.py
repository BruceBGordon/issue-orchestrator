"""What a human is told when a PR escalates, and what the UI hears (#6957 shape).

Pure composition plus two event publishes - no applier internals, no label or
claim gating. Split out of :mod:`.action_applier` because the escalation handler
is one of that file's largest, and this is the half of it that depends only on
the action and the event sink: the notice a reviewer reads and the two trace
events the dashboard reacts to.
"""

from __future__ import annotations

from ..events import EventName
from ..ports import EventSink, make_trace_event
from .actions import EscalateToHumanAction


def escalation_comment(
    action: EscalateToHumanAction, latest_review_section: str
) -> str:
    """The reviewer-facing escalation notice.

    ``comment_override`` wins verbatim: the post-publish-stuck path supplies its
    own copy, which must not mention rework cycles it never counted.
    """
    if action.comment_override is not None:
        return action.comment_override
    return f"""## ⚠️ Escalated to Human Review

This PR has gone through {action.rework_cycles - 1} rework cycles without passing review.
Maximum rework cycles ({action.max_rework_cycles}) exceeded.
{latest_review_section}
**A human needs to review and either:**
- Approve the PR manually
- Provide specific guidance for the agent
- Take over the implementation
"""


def publish_escalation_events(
    events: EventSink, action: EscalateToHumanAction, comment_url: str
) -> None:
    """The escalation itself, and the comment only if one actually landed."""
    events.publish(
        make_trace_event(
            EventName.REVIEW_ESCALATED,
            {
                "pr_number": action.pr_number,
                "issue_number": action.issue_number,
                "rework_count": action.rework_cycles - 1,
                "rework_cycle": action.rework_cycles,
                "max_rework_cycles": action.max_rework_cycles,
            },
        )
    )
    if not comment_url:
        return
    events.publish(
        make_trace_event(
            EventName.REVIEW_COMMENT_ADDED,
            {
                "issue_number": action.issue_number,
                "pr_number": action.pr_number,
                "comment_url": comment_url,
                "summary": "Posted escalation comment",
            },
        )
    )


__all__ = ["escalation_comment", "publish_escalation_events"]
