"""What a failed publish means for the rest of a completion.

Publishing is not the same kind of event for every outcome. For finished work
the push *is* the result, so a failure has to stop everything after it. For an
escalation the push is a convenience: the label and the comment are the result,
and suppressing them turns a reported problem into silence -- the agent exits
having escalated successfully while no human is ever told.

That distinction was previously implicit in a generic action-loop halt, which
is why the escalation kept losing its human routing at whichever publish step
failed. It is one decision, made here, for both the pre-publish gate and the
push itself.
"""

import logging
from pathlib import Path

from ..domain.dirty_remediation import publish_is_best_effort
from ..domain.models import CompletionRecord

logger = logging.getLogger(__name__)


def route_despite_publish_failure(
    *,
    record: CompletionRecord,
    worktree: Path,
    branch: str | None,
    reason: str,
    step: str,
    actions_taken: list[str],
) -> bool:
    """Continue the completion after a failed publish, when the outcome allows.

    Returns ``True`` when the remaining actions must still run. The caller keeps
    reporting the failure either way -- this decides only whether the human
    routing survives it.
    """
    if not publish_is_best_effort(record.outcome.value):
        return False

    actions_taken.append(f"Branch not pushed ({step}); continued to human routing")
    # Deliberately not appended to the agent's comment. Two reasons: the comment
    # body is validated against GitHub's 64 KiB limit *before* this point, so
    # appending here can push an accepted body over it and lose the escalation
    # context entirely; and a "recover the commits before cleanup" note would be
    # untrue -- escalations get immediate cleanup with remove_worktrees=True, so
    # there is no interval to recover in.
    #
    # Retaining and recovering that work is a real gap, tracked as #7110: it
    # needs one typed outcome driving cleanup strategy, the cleanup state owner,
    # the action planner and retry admission together. A comment cannot paper
    # over it, and this module must not imply otherwise.
    logger.warning(
        "Escalation published nothing; local commits on %s in %s are not retained",
        branch or "the current branch",
        worktree,
    )
    logger.info(
        "Publish failed for an escalation (%s); routing to a human anyway: %s",
        step,
        reason,
    )
    return True
