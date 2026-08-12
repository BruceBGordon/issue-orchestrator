"""Maintenance operations for issue cleanup and reset.

This module provides the IssueResetter class for performing "nuclear reset"
operations on issues - cleaning up all local and remote state to allow a
fresh retry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.pull_request_tracker import PullRequestTracker
    from ..ports.worktree_manager import WorktreeManager
    from ..ports.working_copy import WorkingCopy
    from ..ports.timeline_store import TimelineStore
    from ..domain.models import SessionHistoryEntry
    from .action_applier import ActionApplier
    from .label_manager import LabelManager
    from ..ports.label_store import LabelStore

from .actions import RemoveLabelAction, SupersedePullRequestAction
from .worktree_manager import get_worktree_path

logger = logging.getLogger(__name__)


def _find_issue_branches(
    working_copy: "WorkingCopy",
    repo_root: Path,
    issue_number: int,
) -> list[str]:
    """Find all remote branches for an issue number.

    Looks for branches that start with the issue number (e.g., "3767-fix-something").

    Args:
        working_copy: Working copy adapter for git operations.
        repo_root: Path to the repository root.
        issue_number: The issue number to find a branch for.

    Returns:
        Branch names (without remote prefix), preserving remote listing order.
    """
    branches = working_copy.list_remote_branches(repo_root)
    matches: list[str] = []
    for raw in branches:
        branch = raw.strip()
        if branch.startswith("origin/"):
            branch = branch[len("origin/"):]
        if branch and branch[0].isdigit():
            parts = branch.split("-", 1)
            if parts[0].isdigit() and int(parts[0]) == issue_number:
                matches.append(branch)
    return matches


@dataclass
class ResetResult:
    """Result of an issue reset operation."""

    success: bool
    issue_number: int
    deleted_worktree: str | None = None
    deleted_branch: str | None = None
    deleted_branches: list[str] | None = None
    superseded_prs: list[int] | None = None
    timeline_events_deleted: int | None = None
    labels_removed: list[str] | None = None
    error: str | None = None


def _remove_local_worktree(
    *,
    issue_number: int,
    config: "Config",
    worktree_manager: "WorktreeManager",
    from_scratch: bool,
) -> str | None:
    worktree_path = get_worktree_path(config, issue_number)
    logger.info(
        "[reset] Begin issue reset: issue=%d from_scratch=%s worktree=%s exists=%s",
        issue_number,
        from_scratch,
        worktree_path,
        worktree_path.exists(),
    )
    if not worktree_path.exists():
        return None

    try:
        if from_scratch:
            worktree_manager.remove_checkout_and_branch(worktree_path, force=True)
        else:
            worktree_manager.remove_checkout(worktree_path)
        if worktree_path.exists():
            message = f"Worktree still exists after removal: {worktree_path}"
            if from_scratch:
                raise RuntimeError(message)
            logger.warning("[reset] %s", message)
        else:
            logger.info("[reset] Deleted worktree: %s", worktree_path)
    except Exception as e:
        logger.warning("[reset] Failed to delete worktree %s: %s", worktree_path, e)
        if from_scratch:
            raise RuntimeError(
                f"Scratch reset failed to delete worktree {worktree_path}: {e}"
            ) from e
    return str(worktree_path)


def _supersede_open_prs(
    *,
    issue_number: int,
    repository_host: "PullRequestTracker | None",
    action_applier: "ActionApplier",
    superseded_prs: list[int],
) -> None:
    """Comment and close open orchestrator PRs superseded by scratch reset."""
    if repository_host is None:
        raise RuntimeError("Scratch reset requires repository_host to supersede open PRs")

    for pr in repository_host.get_prs_for_issue(issue_number, state="open"):
        comment = (
            "Superseded by reset and retry from scratch.\n\n"
            "The orchestrator is discarding prior work, branch state, "
            "validation, and review approvals for this issue. A future "
            "attempt will use a fresh branch from the configured base."
        )
        result = action_applier.apply(
            SupersedePullRequestAction(
                issue_number=issue_number,
                pr_number=pr.number,
                comment=comment,
                reason="reset and retry from scratch",
            )
        )
        if not result.success:
            logger.error(
                "[reset] Failed to supersede PR #%d for scratch reset of issue #%d; "
                "partial_superseded_prs=%s",
                pr.number,
                issue_number,
                superseded_prs,
            )
            raise RuntimeError(
                f"failed to supersede PR #{pr.number}: "
                f"{result.error or 'unknown error'}"
            )
        superseded_prs.append(pr.number)
        logger.info(
            "[reset] Superseded PR #%d for scratch reset of issue #%d",
            pr.number,
            issue_number,
        )


def _delete_issue_branches(
    *,
    issue_number: int,
    config: "Config",
    working_copy: "WorkingCopy",
    from_scratch: bool,
) -> list[str]:
    deleted_branches: list[str] = []
    branch_names = _find_issue_branches(working_copy, config.repo_root, issue_number)
    for branch_name in branch_names:
        try:
            deleted = working_copy.delete_remote_branch(config.repo_root, branch_name)
            if deleted is False:
                raise RuntimeError("delete_remote_branch returned False")
            deleted_branches.append(branch_name)
            logger.info("[reset] Deleted remote branch: %s", branch_name)
        except Exception as e:
            logger.warning("[reset] Failed to delete remote branch %s: %s", branch_name, e)
            if from_scratch:
                raise RuntimeError(
                    f"Scratch reset failed to delete remote branch {branch_name}: {e}"
                ) from e
    if len(deleted_branches) > 1:
        logger.info(
            "[reset] Deleted %d remote branches for issue #%d: %s",
            len(deleted_branches),
            issue_number,
            deleted_branches,
        )
    return deleted_branches


def _remove_orchestrator_labels(
    *,
    issue_number: int,
    current_labels: list[str],
    label_manager: "LabelManager",
    action_applier: "ActionApplier",
    from_scratch: bool,
) -> list[str]:
    labels_removed: list[str] = []
    ours = label_manager.get_ours(current_labels)
    for label in ours:
        action = RemoveLabelAction(
            issue_number=issue_number,
            label=label,
            reason="reset via web",
        )
        result = action_applier.apply(action)
        if result.success:
            labels_removed.append(label)
            logger.info("[reset] Removed label '%s' from issue #%d", label, issue_number)
            continue
        logger.warning(
            "[reset] Failed to remove label '%s' from #%d: %s",
            label,
            issue_number,
            result.error or "unknown error",
        )
        if from_scratch:
            raise RuntimeError(
                f"Scratch reset failed to remove label '{label}' from issue #{issue_number}: "
                f"{result.error or 'unknown error'}"
            )
    return labels_removed


def _clear_label_persistence(
    *,
    issue_number: int,
    label_store: "LabelStore | None",
    from_scratch: bool,
) -> None:
    if label_store is None:
        return
    try:
        label_store.remove_issue(issue_number)
    except Exception as e:
        logger.warning("[reset] Failed to clear label store for #%d: %s", issue_number, e)
        if from_scratch:
            raise RuntimeError(
                f"Scratch reset failed to clear label store for #{issue_number}: {e}"
            ) from e


def _clear_history_gates(
    *,
    issue_number: int,
    session_history: list["SessionHistoryEntry"],
    completed_today: list[int],
) -> None:
    session_history[:] = [
        entry for entry in session_history
        if entry.issue_number != issue_number
    ]
    if issue_number in completed_today:
        completed_today.remove(issue_number)


def _clear_timeline(
    *,
    issue_number: int,
    timeline_store: "TimelineStore | None",
    from_scratch: bool,
) -> int | None:
    if timeline_store is None:
        return None
    try:
        deleted_count = timeline_store.delete(issue_number)
        logger.info("[reset] Cleared %d timeline events for issue #%d", deleted_count, issue_number)
        return deleted_count
    except Exception as e:
        logger.warning("[reset] Failed to clear timeline for #%d: %s", issue_number, e)
        if from_scratch:
            raise RuntimeError(
                f"Scratch reset failed to clear timeline for #{issue_number}: {e}"
            ) from e
    return None


def reset_issue(
    issue_number: int,
    config: "Config",
    worktree_manager: "WorktreeManager",
    working_copy: "WorkingCopy",
    action_applier: "ActionApplier",
    label_manager: "LabelManager",
    current_labels: list[str],
    session_history: list["SessionHistoryEntry"],
    completed_today: list[int],
    label_store: "LabelStore | None" = None,
    timeline_store: "TimelineStore | None" = None,
    from_scratch: bool = False,
    repository_host: "PullRequestTracker | None" = None,
) -> ResetResult:
    """Reset an issue to pristine state for fresh retry.

    This "nuclear option" cleans up all local and remote state:
    1. Deletes the local worktree
    2. Supersedes open PRs when retrying from scratch
    3. Deletes matching remote branches
    4. Removes ALL orchestrator-owned labels (not just blocking)
    5. Clears label persistence, session history, and timeline state

    Args:
        issue_number: The issue to reset
        config: Orchestrator configuration
        worktree_manager: Manager for worktree lifecycle operations
        working_copy: Working copy adapter for git operations
        action_applier: For applying label changes
        label_manager: For identifying orchestrator-owned labels
        current_labels: Current labels on the issue (from GitHub)
        session_history: Session history list (will be mutated)
        completed_today: Completed today list (will be mutated)
        label_store: Optional label persistence store to clean
        timeline_store: Optional timeline store to clean for scratch retries
        from_scratch: Whether to enforce hard cleanup boundaries
        repository_host: Pull request tracker required for scratch retries

    Returns:
        ResetResult with details of what was cleaned up
    """
    deleted_worktree: str | None = None
    deleted_branches: list[str] = []
    superseded_prs: list[int] = []
    timeline_events_deleted: int | None = None
    labels_removed: list[str] = []

    try:
        deleted_worktree = _remove_local_worktree(
            issue_number=issue_number,
            config=config,
            worktree_manager=worktree_manager,
            from_scratch=from_scratch,
        )
        if from_scratch:
            _supersede_open_prs(
                issue_number=issue_number,
                repository_host=repository_host,
                action_applier=action_applier,
                superseded_prs=superseded_prs,
            )

        deleted_branches = _delete_issue_branches(
            issue_number=issue_number,
            config=config,
            working_copy=working_copy,
            from_scratch=from_scratch,
        )
        labels_removed = _remove_orchestrator_labels(
            issue_number=issue_number,
            current_labels=current_labels,
            label_manager=label_manager,
            action_applier=action_applier,
            from_scratch=from_scratch,
        )
        _clear_label_persistence(
            issue_number=issue_number,
            label_store=label_store,
            from_scratch=from_scratch,
        )
        _clear_history_gates(
            issue_number=issue_number,
            session_history=session_history,
            completed_today=completed_today,
        )
        timeline_events_deleted = _clear_timeline(
            issue_number=issue_number,
            timeline_store=timeline_store,
            from_scratch=from_scratch,
        )

        logger.info(
            "[reset] Issue #%d reset complete: from_scratch=%s worktree=%s branches=%s "
            "superseded_prs=%s labels=%s timeline_events_deleted=%s",
            issue_number,
            from_scratch,
            deleted_worktree or "(none)",
            deleted_branches or "(none)",
            superseded_prs or "(none)",
            labels_removed or "(none)",
            timeline_events_deleted,
        )

        return ResetResult(
            success=True,
            issue_number=issue_number,
            deleted_worktree=deleted_worktree,
            deleted_branch=deleted_branches[0] if deleted_branches else None,
            deleted_branches=deleted_branches,
            superseded_prs=superseded_prs,
            timeline_events_deleted=timeline_events_deleted,
            labels_removed=labels_removed,
        )

    except Exception as e:
        logger.error(
            "[reset] Failed to reset issue #%d: %s; partial cleanup: "
            "worktree=%s branches=%s superseded_prs=%s labels=%s "
            "timeline_events_deleted=%s",
            issue_number,
            e,
            deleted_worktree or "(none)",
            deleted_branches or "(none)",
            superseded_prs or "(none)",
            labels_removed or "(none)",
            timeline_events_deleted,
        )
        return ResetResult(
            success=False,
            issue_number=issue_number,
            deleted_worktree=deleted_worktree,
            deleted_branch=deleted_branches[0] if deleted_branches else None,
            deleted_branches=deleted_branches,
            superseded_prs=superseded_prs,
            timeline_events_deleted=timeline_events_deleted,
            labels_removed=labels_removed,
            error=str(e),
        )
