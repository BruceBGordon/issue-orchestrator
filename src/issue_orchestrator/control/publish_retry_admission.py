"""May this issue be retried, and are its stored inputs still usable?

Split out of ``PublishRecoveryService``, which owns in-flight submission state
(tokens, tombstones, the pending slot) under a lock. These questions own none of
that: they are decisions about the BOARD and about the durable retry locators,
plus the one repair those locators may need before a republish can read them.

Keeping them here makes the service's ``retry_publish`` read as decide → recover
or submit, and makes the admission rules testable without a runner, a lock, or a
background job. The one check that genuinely needs owner state — "is a
submission already pending for this issue" — deliberately stays in the service.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..domain.publish_retry import PublishRetryLocators

logger = logging.getLogger(__name__)


def board_block_reason(
    *,
    issue_number: int,
    state: "OrchestratorState",
    labels: Sequence[str] | Iterable[str],
    publish_failed_label: str,
) -> str | None:
    """None when the BOARD admits a retry for *issue_number*, else the reason.

    ``labels`` must be a freshly observed set: an unreadable issue is decided
    before this is ever called, because "could not read" is not "not blocked"
    (#6957 round-2 review F4).
    """
    if publish_failed_label not in tuple(labels):
        return "Issue is not blocked by a publish failure"
    if any(session.issue.number == issue_number for session in state.active_sessions):
        return "Issue has an active session"
    return None


def locator_block_reason(locators: "PublishRetryLocators") -> str | None:
    """None when the stored retry inputs are still usable, else the reason."""
    worktree = Path(locators.worktree_path)
    if not worktree.exists():
        return "Retry worktree no longer exists"
    # The live completion path preserves a run-scoped copy and then deletes the
    # agent's original completion file, so a real publish failure leaves only
    # the durable copy. Either source is a valid retry input.
    completion_path = worktree / locators.completion_path
    durable_copy = locators.run_assets.completion_record_copy.path
    if not completion_path.exists() and not durable_copy.exists():
        return "Completion record for retry is missing"
    return None


def restore_completion_record(locators: "PublishRetryLocators") -> None:
    """Put a completion record back where ``process`` reads it.

    ``CompletionProcessor.process`` re-reads ``worktree / completion_path``, but
    the live completion path deletes that original agent file after preserving a
    run-scoped copy. Restore the durable copy to the worktree location so the
    republish has a valid input. No-op when the original is still present or the
    durable copy is gone (the processor then fails loudly on a genuinely missing
    record, keeping the issue retryable).
    """
    target = Path(locators.worktree_path) / locators.completion_path
    if target.exists():
        return
    durable_copy = locators.run_assets.completion_record_copy.path
    if not durable_copy.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(durable_copy, target)
    logger.info(
        "[publish-retry] Restored durable completion record for issue=%s from %s",
        locators.issue_number,
        durable_copy,
    )
