"""Working copy port for local VCS operations.

This module defines the protocol (interface) for local working copy operations.
Unlike PRRepository (which handles remote GitHub operations), this handles
local worktree/working copy operations: push, rebase, commit info, etc.

Naming convention (from architecture review):
- "WorkingCopy" conveys local filesystem + branch + HEAD
- No implication of authority (just execution)
- Common in SCM theory

Separation of concerns:
- WorkingCopy: Local VCS operations (in worktree context) - EXECUTION
- RepoHost (PRRepository, etc.): Remote platform operations - EXECUTION
- LifecycleController: State transitions and decisions - AUTHORITY
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class CommitInfo:
    """Information about a git commit."""
    sha: str
    message: str
    author: str
    # Short SHA for display
    short_sha: str


@dataclass
class BranchStatus:
    """Status of the current branch relative to remote."""
    branch: str
    ahead: int  # Commits ahead of remote
    behind: int  # Commits behind remote
    has_remote: bool  # Whether branch exists on remote
    clean: bool  # No uncommitted changes


@dataclass
class PushResult:
    """Result of a git push operation."""
    success: bool
    branch: str
    remote: str
    message: str  # Success message or error description
    # If failed, whether it can be retried (e.g., network issue vs. force needed)
    retryable: bool = True


@dataclass
class PreflightResult:
    """Result of a push preflight check (dry-run)."""
    would_succeed: bool
    error: str | None = None
    fix_hint: str | None = None


@dataclass(frozen=True)
class DiffResult:
    """Result of reading a branch diff from a working copy."""

    success: bool
    diff_text: str = ""
    error: str | None = None


@dataclass(frozen=True)
class BranchTextFile:
    """Exact tracked text content for one file at the branch tip."""

    path: str
    content: str


@dataclass(frozen=True)
class BranchTextFilesResult:
    """Result of reading selected tracked text files from the branch tip."""

    success: bool
    files: tuple[BranchTextFile, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class BranchPathsResult:
    """Branch-tip post-image paths for a diff against base.

    ``paths`` are the repo-relative files that exist in the branch after the
    diff (added, copied, modified, renamed-to, or type-changed) with deletions
    excluded. Derived from a path-oriented Git query so it is robust against
    empty-file additions, binary changes, and renames/copies — no-hunk diffs
    that a unified-diff text parser cannot see. ``success`` is ``False`` on a
    git failure so callers can fail closed.
    """

    success: bool
    paths: tuple[str, ...] = ()
    error: str | None = None


@dataclass
class RebaseResult:
    """Result of a git rebase operation."""
    success: bool
    message: str
    # If conflicts occurred
    conflicts: list[str] | None = None
    # Whether rebase was aborted automatically after failure
    aborted: bool = False


class WorkingCopy(Protocol):
    """Protocol for local VCS operations in a worktree.

    This protocol defines the interface for git operations that the orchestrator
    needs to perform in worktree directories. It separates local VCS operations
    from remote platform operations.

    Naming: "WorkingCopy" is neutral, implies local filesystem state,
    no authority/policy implication. Implementations handle execution only.

    All methods are expected to operate in the context of a specific worktree,
    passed per-method (stateless adapter pattern).
    """

    def get_current_branch(self, worktree: Path) -> str | None:
        """Get the current branch name in the worktree.

        Args:
            worktree: Path to the worktree directory.

        Returns:
            The branch name, or None if detached HEAD or error.
        """
        ...

    def get_head_sha(self, worktree: Path) -> str | None:
        """Get the HEAD commit SHA in the worktree.

        Args:
            worktree: Path to the worktree directory.

        Returns:
            The full SHA, or None on error.
        """
        ...

    def get_branch_status(self, worktree: Path) -> BranchStatus | None:
        """Get the status of the current branch.

        Args:
            worktree: Path to the worktree directory.

        Returns:
            BranchStatus with ahead/behind counts, or None on error.
        """
        ...

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        """Check if there are uncommitted changes in the worktree.

        Args:
            worktree: Path to the worktree directory.

        Returns:
            True if there are uncommitted changes (staged or unstaged).
        """
        ...

    def get_commits_ahead_of_main(self, worktree: Path) -> list[CommitInfo]:
        """Get commits that are ahead of main branch.

        Args:
            worktree: Path to the worktree directory.

        Returns:
            List of CommitInfo for commits in HEAD but not in main.
            Empty list if none or on error.
        """
        ...

    def fetch(self, worktree: Path, remote: str = "origin") -> bool:
        """Fetch from remote.

        Args:
            worktree: Path to the worktree directory.
            remote: Remote name to fetch from.

        Returns:
            True if fetch succeeded, False otherwise.
        """
        ...

    def list_remote_branches(self, repo_root: Path, remote: str = "origin") -> list[str]:
        """List remote branches.

        Args:
            repo_root: Path to the git repository root.
            remote: Remote name to list branches from.

        Returns:
            List of branch names (may include remote prefix).
        """
        ...

    def get_commits_ahead_count(
        self,
        repo_root: Path,
        branch: str,
        base: str = "origin/main",
    ) -> int:
        """Count commits ahead of base for a remote branch.

        Args:
            repo_root: Path to the git repository root.
            branch: Branch name (without remote prefix).
            base: Base ref to compare against.

        Returns:
            Commit count ahead of base, or 0 on error.
        """
        ...

    def get_last_commit_date(
        self,
        repo_root: Path,
        branch: str,
    ) -> str | None:
        """Get last commit date (relative) for a remote branch.

        Args:
            repo_root: Path to the git repository root.
            branch: Branch name (without remote prefix).

        Returns:
            Relative date string, or None on error.
        """
        ...

    def rebase_on_branch(
        self, worktree: Path, target: str = "origin/main"
    ) -> RebaseResult:
        """Rebase current branch onto target.

        Args:
            worktree: Path to the worktree directory.
            target: Branch/ref to rebase onto.

        Returns:
            RebaseResult indicating success or failure with details.
        """
        ...

    def create_branch_from_current(self, worktree: Path, branch: str) -> None:
        """Create and switch to a branch from the current HEAD.

        Args:
            worktree: Path to the worktree directory.
            branch: Branch name to create (or reset) and switch to.
        """
        ...

    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        set_upstream: bool = True,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> PushResult:
        """Push current branch to remote with --force-with-lease.

        Always uses --force-with-lease for safety after rebase.

        Args:
            worktree: Path to the worktree directory.
            remote: Remote to push to.
            set_upstream: Use -u to set upstream tracking.

        Returns:
            PushResult indicating success or failure.
        """
        ...

    def diff_against_base(self, worktree: Path, base_ref: str) -> DiffResult:
        r"""Return unified diff for changes from *base_ref* to HEAD.

        Implementations should use merge-base semantics (``base_ref...HEAD``)
        so callers scan exactly what the branch contributes.

        ``diff_text`` must reproduce the patch byte-for-byte. Records are
        LF-delimited, so a bare ``\r`` inside one is content: transports that
        translate newlines split that record in two, stripping the ``+`` from
        the trailing half and dropping a real addition from every caller's
        scan. :meth:`read_branch_text_files` must be lossless on the same
        terms, or the two sides number lines differently.
        """
        ...

    def read_branch_text_files(
        self, worktree: Path, paths: tuple[str, ...]
    ) -> BranchTextFilesResult:
        """Return exact tracked ``HEAD`` text for the requested repository paths.

        Implementations must read branch-tip objects rather than mutable
        filesystem paths so callers receive content from the same ``HEAD`` used
        by :meth:`diff_against_base`, and must reproduce the stored blob
        byte-for-byte -- newline translation anywhere in the transport breaks
        the line-for-line correspondence with that diff. Any missing or
        undecodable path must fail the complete request rather than returning a
        partial result.
        """
        ...

    def branch_post_image_paths_against_base(
        self, worktree: Path, base_ref: str
    ) -> BranchPathsResult:
        """Return branch-tip post-image paths for changes from *base_ref* to HEAD.

        Uses merge-base semantics (``base_ref...HEAD``) and a path-oriented Git
        query so callers receive every file present in the branch tip —
        including empty-file additions, binary changes, and renames/copies —
        with deletions excluded. Distinct from :meth:`diff_against_base`, which
        returns unified diff *text* for content-oriented scans and cannot see
        no-hunk changes.
        """
        ...

    def get_issue_number_from_branch(self, worktree: Path) -> int | None:
        """Extract issue number from branch name.

        Expects branch format like "123-fix-bug" where 123 is the issue number.

        Args:
            worktree: Path to the worktree directory.

        Returns:
            The issue number, or None if branch doesn't match pattern.
        """
        ...

    def push_preflight(
        self,
        worktree: Path,
        remote: str = "origin",
    ) -> PreflightResult:
        """Check if a push would succeed (dry-run).

        This performs a git push --dry-run to verify the push would work
        without actually pushing. Useful for catching divergence issues
        while the agent is still active and can fix them.

        Args:
            worktree: Path to the worktree directory.
            remote: Remote to check against.

        Returns:
            PreflightResult indicating whether push would succeed.
        """
        ...

    def delete_remote_branch(
        self,
        repo_root: Path,
        branch: str,
        remote: str = "origin",
    ) -> bool:
        """Delete a branch from the remote.

        Args:
            repo_root: Path to the git repository root.
            branch: Branch name to delete (without remote prefix).
            remote: Remote to delete from.

        Returns:
            True if deletion succeeded, False otherwise.
        """
        ...
