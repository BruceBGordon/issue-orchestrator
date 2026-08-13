"""WorktreeManager port for worktree lifecycle operations.

This module defines the protocol (interface) for worktree management:
- Create worktrees for issues
- Remove worktrees after completion
- Extract issue numbers from branch names

Distinct from WorkingCopy which handles operations *inside* a worktree.
WorktreeManager handles the worktree lifecycle itself.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


WORKTREE_ID_MARKER = Path(".issue-orchestrator/worktree-id")
REVIEWER_OWNED_HEAD_MARKER = Path(".issue-orchestrator/reviewer-owned-head")


@dataclass
class WorktreeInfo:
    """Information about a created worktree."""
    path: Path
    branch_name: str
    reuse_status: str = "created"  # created | reused | recreated
    reuse_reason: str | None = None
    rebase_failed: bool = False  # True if rebase onto main failed (work was discarded)
    uncommitted_discarded: int = 0  # Count of uncommitted changes discarded during reset
    commits_discarded: int = 0  # Count of commits discarded during reset (rebase failure)


@dataclass(frozen=True)
class RegisteredWorktree:
    """A worktree registered in a repository's git metadata."""
    path: Path
    head: str
    branch: str | None = None
    locked: bool = False
    prunable: bool = False


@dataclass(frozen=True)
class ReviewerHeadOwnership:
    """Persisted evidence of the detached tip installed by reviewer lifecycle."""

    marker_present: bool
    expected_head: str | None


@dataclass
class WorktreeReuseOptions:
    """Options controlling worktree reuse behavior."""
    reuse_push_preflight: bool = True
    worktree_branch_on_recreate: str = "delete"
    allow_no_verify_dry_run_preflight: bool = True
    allow_remote_branch_delete: bool = True
    disable_reuse: bool = False
    # When True, worktree reuse must NOT rebase or hard-reset the branch. Used
    # for tech_lead investigations, which read the subject's branch as evidence and
    # must never mutate it — the reuse path leaves the branch exactly as-is
    # instead of freshening it onto the base branch.
    preserve_branch: bool = False


class WorktreeManager(Protocol):
    """Protocol for worktree lifecycle management.

    This protocol defines the interface for creating and removing
    git worktrees. Implementations handle the actual git operations.
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
        """Create a new git worktree for an issue.

        Args:
            repo_root: Path to the main git repository
            issue_number: GitHub issue number
            issue_title: GitHub issue title
            worktree_base: Base directory for worktrees
            enforce_hooks: Whether to install pre-push hooks
            pre_push_hook: Custom pre-push hook path
            branch_name: Specific branch to checkout (for reviews)
            base_branch: Branch to use for new worktree bases (defaults to default branch)
            seed_ref: Optional local ref used to seed fresh worktrees
            reuse_options: Options controlling reuse behavior
            worktree_name: Overrides the derived worktree directory basename
                (default ``<repo>-<issue_number>``). Used for a run-scoped,
                disposable scratch worktree that must NOT collide with — or
                reuse — the issue's own worktree (a tech_lead failure
                investigation reads its subject's worktree as evidence and must
                never mutate it).

        Returns:
            WorktreeInfo with path and branch name

        Raises:
            WorktreeError: If creation fails
        """
        ...

    def remove_checkout(self, worktree_path: Path, *, force: bool = False) -> None:
        """Remove a git worktree checkout while preserving its local branch.

        Args:
            worktree_path: Path to the worktree to remove
            force: If true, use forced worktree removal and fallback directory
                cleanup. This is reserved for hard lifecycle boundaries where
                the orchestrator intentionally discards local state.

        Raises:
            WorktreeError: If removal fails
        """
        ...

    def remove_checkout_and_branch(
        self,
        worktree_path: Path,
        *,
        force: bool = False,
    ) -> None:
        """Remove a disposable checkout and its associated local branch.

        This destructive operation is reserved for owners whose lifecycle
        contract explicitly permits branch deletion, such as scratch/reset
        cleanup. Ordinary review-gated retention cleanup must call
        :meth:`remove_checkout` instead.
        """
        ...

    def can_remove_without_user_changes(self, worktree_path: Path) -> bool:
        """Return true when forced removal would only discard owned artifacts.

        Implementations must return false when tracked changes are present or
        when untracked paths are not known runtime/dependency outputs.
        """
        ...

    def read_reviewer_head_ownership(
        self, worktree_path: Path
    ) -> ReviewerHeadOwnership:
        """Read typed reviewer-tip evidence without exposing marker storage."""
        ...

    def list_registered(self, repo_root: Path) -> tuple[RegisteredWorktree, ...]:
        """Return the worktrees registered for ``repo_root`` by git."""
        ...

    def extract_issue_number(self, branch_name: str) -> int | None:
        """Extract issue number from a branch name.

        Args:
            branch_name: Branch name (e.g., "328-add-feature")

        Returns:
            Issue number if found, None otherwise
        """
        ...
