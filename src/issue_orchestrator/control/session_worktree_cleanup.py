"""Worktree cleanup policy applied at a session lifecycle boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from ..infra.logging_config import issue_log
from ..ports.timeline_evidence import TimelineEvidence
from ..ports.worktree_manager import WorktreeManager
from .actions import CleanupSessionAction

logger = logging.getLogger(__name__)


def cleanup_session_worktree(
    action: CleanupSessionAction,
    errors: list[str],
    *,
    worktree_manager: WorktreeManager | None,
    timeline_evidence: TimelineEvidence,
    on_worktree_removed: Callable[[str], int] | None,
) -> None:
    """Archive evidence, safely remove the checkout, and publish removal."""
    if not (action.remove_worktrees and action.worktree_path):
        return
    if worktree_manager is None:
        errors.append("no worktree_manager configured")
        return

    worktree_path = Path(action.worktree_path)
    try:
        if not action.disposable_worktree:
            timeline_evidence.archive_worktree(action.issue_number, worktree_path)
        # Scratch worktrees contain throwaway agent artifacts and may be forced.
        # Normal coding worktrees stay non-forced so user work is never discarded.
        remove_worktree = worktree_manager.remove_checkout
        if action.disposable_worktree:
            remove_worktree = worktree_manager.remove_checkout_and_branch
        remove_worktree(worktree_path, force=action.disposable_worktree)
        logger.info(
            issue_log(action.issue_number, "Removed worktree: %s"),
            action.worktree_path,
        )
    except Exception as exc:
        errors.append(f"remove worktree: {exc}")
        logger.warning(
            issue_log(action.issue_number, "Failed to remove worktree: %s"),
            exc,
        )
        return

    # Removal has already succeeded. A notification failure must not turn it
    # into a retry against an absent checkout.
    if on_worktree_removed is not None:
        try:
            on_worktree_removed(action.worktree_path)
        except Exception as exc:
            logger.warning(
                issue_log(
                    action.issue_number,
                    "worktree-removed callback failed (worktree already gone): %s",
                ),
                exc,
            )
