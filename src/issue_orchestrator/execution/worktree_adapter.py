"""WorktreeManager adapter implementation.

Implements the WorktreeManager port using the git worktree implementation.
"""

from pathlib import Path

from ..ports.worktree_manager import (
    RegisteredWorktree,
    ReviewerHeadOwnership,
    WorktreeInfo,
    WorktreeReuseOptions,
)
from ..adapters.worktree._worktree_runtime import read_reviewer_head_ownership
from ..adapters.worktree._worktree import (
    can_remove_without_user_changes,
    create_worktree,
    remove_worktree,
    extract_issue_number_from_branch,
    list_registered_worktrees,
)


class GitWorktreeManager:
    """Git-based implementation of WorktreeManager.

    Wraps the _worktree_impl functions to implement the port protocol.
    """

    def create(
        self,
        repo_root: Path,
        issue_number: int,
        issue_title: str,
        worktree_base: Path | None = None,
        enforce_hooks: bool = True,
        pre_push_hook: Path | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        seed_ref: str | None = None,
        reuse_options: WorktreeReuseOptions | None = None,
        worktree_name: str | None = None,
    ) -> WorktreeInfo:
        """Create a new git worktree for an issue."""
        path, branch, reuse_status, reuse_reason, rebase_failed, uncommitted_discarded, commits_discarded = create_worktree(
            repo_root=repo_root,
            issue_number=issue_number,
            issue_title=issue_title,
            worktree_base=worktree_base,
            base_branch=base_branch,
            enforce_hooks=enforce_hooks,
            pre_push_hook=pre_push_hook,
            branch_name=branch_name,
            reuse_options=reuse_options,
            seed_ref=seed_ref,
            worktree_name=worktree_name,
        )
        return WorktreeInfo(
            path=path,
            branch_name=branch,
            reuse_status=reuse_status,
            reuse_reason=reuse_reason,
            rebase_failed=rebase_failed,
            uncommitted_discarded=uncommitted_discarded,
            commits_discarded=commits_discarded,
        )

    def remove_checkout(self, worktree_path: Path, *, force: bool = False) -> None:
        """Remove a git worktree checkout while preserving its branch."""
        remove_worktree(worktree_path, force=force, delete_branch=False)

    def remove_checkout_and_branch(
        self,
        worktree_path: Path,
        *,
        force: bool = False,
    ) -> None:
        """Remove a disposable worktree checkout and its local branch."""
        remove_worktree(worktree_path, force=force, delete_branch=True)

    def can_remove_without_user_changes(self, worktree_path: Path) -> bool:
        """Return true when forced removal would not discard user changes."""
        return can_remove_without_user_changes(worktree_path)

    def read_reviewer_head_ownership(
        self, worktree_path: Path
    ) -> ReviewerHeadOwnership:
        """Read the lifecycle-owned detached reviewer tip."""
        return read_reviewer_head_ownership(worktree_path)

    def list_registered(self, repo_root: Path) -> tuple[RegisteredWorktree, ...]:
        """Return worktrees registered in git metadata."""
        return list_registered_worktrees(repo_root)

    def extract_issue_number(self, branch_name: str) -> int | None:
        """Extract issue number from a branch name."""
        return extract_issue_number_from_branch(branch_name)
