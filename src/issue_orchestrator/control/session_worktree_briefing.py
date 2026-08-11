"""Coder-facing briefing derived from the prepared worktree state."""

from __future__ import annotations

import logging
from pathlib import Path

from ..ports import WorkingCopy

logger = logging.getLogger(__name__)

_REBASE_CONFLICT_WARNING = (
    "WARNING: This branch could not be rebased onto main due to merge conflicts. "
    "The code is out of date. You should resolve the conflicts by running: "
    "git fetch origin main && git rebase origin/main. "
    "If conflicts occur, resolve them and continue with: git rebase --continue. "
    "This is critical to ensure tests pass with the latest code."
)


def detect_existing_work(
    worktree_path: Path,
    working_copy: WorkingCopy,
    *,
    seed_ref: str | None = None,
) -> str | None:
    """Check if a worktree has commits ahead of main and describe them."""
    try:
        if seed_ref:
            head_sha = working_copy.get_head_sha(worktree_path)
            if head_sha and head_sha == seed_ref:
                return None

        commits = working_copy.get_commits_ahead_of_main(worktree_path)
        if not commits:
            return None

        branch = working_copy.get_current_branch(worktree_path) or "unknown"
        commit_list = "\n".join(
            f"  - {commit.short_sha} {commit.message}" for commit in commits[:10]
        )
        if len(commits) > 10:
            commit_list += f"\n  ... and {len(commits) - 10} more"

        return (
            f"This worktree has {len(commits)} existing commit(s) from a previous "
            f"session. Branch: {branch}. Commits: {commit_list}. "
            "EVALUATE this existing work BEFORE starting fresh."
        )
    except Exception as exc:
        logger.warning("Failed to detect existing work: %s", exc)
        return None


def describe_worktree_state(
    worktree_path: Path,
    working_copy: WorkingCopy,
    *,
    seed_ref: str | None = None,
    rebase_failed: bool = False,
) -> str | None:
    """Build the one workspace briefing a coder needs at launch."""
    existing_work = detect_existing_work(
        worktree_path,
        working_copy,
        seed_ref=seed_ref,
    )
    if existing_work:
        logger.info(
            "[launch] Found existing work - agent will evaluate before starting fresh"
        )
    if not rebase_failed:
        return existing_work
    logger.warning(
        "[launch] Rebase failed - agent will need to resolve merge conflicts"
    )
    if existing_work:
        return f"{existing_work}\n\n{_REBASE_CONFLICT_WARNING}"
    return _REBASE_CONFLICT_WARNING
