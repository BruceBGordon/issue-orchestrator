"""Git working copy adapter for local VCS operations.

This adapter implements the WorkingCopy protocol for git operations.
It handles local worktree operations: push, rebase, commit info, etc.

Part of the execution layer - performs actions, does not make decisions.
"""

import json
import logging
import os
import re
import time
from pathlib import Path

from ..adapters.git.git_cli import GitCLI
from ..execution import git_push_operations as git_push_ops
from ..execution.command_runner import LocalCommandRunner
from ..execution.git_push_operations import GitAuthEnvProvider
from ..infra.runtime_artifacts import filter_orchestrator_untracked_planted
from ..ports.command_runner import OutputNewlines
from ..ports.git import Git, GitError, GitResult
from ..ports.working_copy import (
    BranchPathsResult,
    BranchTextFile,
    BranchTextFilesResult,
    CommitInfo,
    BranchStatus,
    DiffResult,
    PreflightResult,
    PushResult,
    RebaseResult,
)

logger = logging.getLogger(__name__)


class GitWorkingCopy:
    """Git implementation of the WorkingCopy protocol.

    Performs local git operations in worktree directories.
    This is execution-layer code - it does what it's told,
    without making policy decisions.
    """

    def __init__(self, git: Git | None = None, git_auth: GitAuthEnvProvider | None = None) -> None:
        self._git = git or GitCLI(runner=LocalCommandRunner())
        self._git_auth = git_auth

    def _run_git(
        self,
        worktree: Path,
        args: list[str],
        check: bool = True,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
        newlines: OutputNewlines = OutputNewlines.TRANSLATED,
    ) -> GitResult:
        """Run a git command in the worktree context.

        Args:
            worktree: Path to the worktree directory.
            args: Git command arguments (without 'git').
            check: Whether to raise on non-zero exit.
            capture_output: Whether to capture stdout/stderr.
            newlines: Transport newline fidelity. Prefer
                :meth:`_run_git_output_exact` over passing this directly.

        Returns:
            GitResult with results.
        """
        logger.debug("Running: git -C %s %s", worktree, " ".join(args))
        return self._git.run(
            worktree,
            args,
            check=check,
            timeout_s=timeout_s,
            env=env,
            newlines=newlines,
        )

    def _run_git_output_exact(self, worktree: Path, args: list[str]) -> GitResult:
        r"""Run a git read whose output keeps the delimiters Git wrote.

        Git delimits patch records and blob lines with LF alone and path lists
        with NUL, so a bare ``\r`` anywhere in that output is content: inside a
        source line, or inside a filename, which POSIX permits. The default
        universal-newline transport rewrites it to ``\n``, which splits one
        patch record in two -- detaching added source from the ``+`` that marks
        it -- and silently mutates a path. Every read parsed by Git's own
        delimiter rules must go through here, so no two of them can drift into
        different transport semantics.
        """

        return self._run_git(worktree, args, newlines=OutputNewlines.PRESERVED)

    def _run_git_nul_paths(self, worktree: Path, args: list[str]) -> list[str]:
        """Run a NUL-delimited git path query and return its paths.

        Reading and parsing are one step on purpose: NUL-delimited output is
        only safe to split when the transport left it byte-exact, so there is
        no parse-only entry point a translated read could reach.
        """

        result = self._run_git_output_exact(worktree, args)
        return [path for path in result.stdout.split("\0") if path]

    def _clear_stale_remote_ref(self, worktree: Path, remote: str, branch: str) -> None:
        """Clear stale remote-tracking refs when the remote branch is missing."""
        ref_name = f"refs/remotes/{remote}/{branch}"
        try:
            result = self._run_git(worktree, ["update-ref", "-d", ref_name], check=False)
            if result.returncode == 0:
                logger.info("Cleared stale remote-tracking ref %s", ref_name)
            else:
                logger.warning(
                    "Failed to clear stale remote-tracking ref %s: %s",
                    ref_name,
                    (result.stderr or "").strip(),
                )
        except Exception as e:
            logger.warning("Failed to clear stale remote-tracking ref %s: %s", ref_name, e)

    def get_current_branch(self, worktree: Path) -> str | None:
        """Get the current branch name in the worktree."""
        try:
            result = self._run_git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])
            branch = result.stdout.strip()
            return None if branch == "HEAD" else branch  # HEAD means detached
        except GitError:
            logger.warning("Failed to get current branch in %s", worktree)
            return None

    def _branch_from_metadata(self, worktree: Path) -> str | None:
        metadata_path = worktree / ".issue-orchestrator" / "worktree.json"
        if not metadata_path.exists():
            return None
        try:
            metadata = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Could not read worktree metadata %s: %s", metadata_path, exc)
            return None
        branch = metadata.get("branch_name")
        return branch if branch else None

    def get_head_sha(self, worktree: Path) -> str | None:
        """Get the HEAD commit SHA in the worktree."""
        try:
            result = self._run_git(worktree, ["rev-parse", "HEAD"])
            return result.stdout.strip()
        except GitError:
            logger.warning("Failed to get HEAD SHA in %s", worktree)
            return None

    def get_branch_status(self, worktree: Path) -> BranchStatus | None:
        """Get the status of the current branch."""
        branch = self.get_current_branch(worktree)
        if not branch:
            branch = self._branch_from_metadata(worktree)
        if not branch:
            return None

        try:
            # Check for uncommitted changes
            status_result = self._run_git(worktree, ["status", "--porcelain"])
            clean = len(status_result.stdout.strip()) == 0

            # Check ahead/behind
            ahead = 0
            behind = 0
            has_remote = False

            try:
                # Get upstream tracking info
                upstream_result = self._run_git(
                    worktree,
                    ["rev-list", "--left-right", "--count", f"HEAD...@{{u}}"],
                )
                parts = upstream_result.stdout.strip().split()
                if len(parts) == 2:
                    ahead = int(parts[0])
                    behind = int(parts[1])
                    has_remote = True
            except GitError:
                # No upstream tracking
                has_remote = False

            return BranchStatus(
                branch=branch,
                ahead=ahead,
                behind=behind,
                has_remote=has_remote,
                clean=clean,
            )
        except GitError as e:
            logger.warning("Failed to get branch status: %s", e)
            return None

    def get_status_porcelain_lines(self, worktree: Path) -> list[str]:
        """Return the lines from `git status --porcelain` output."""
        try:
            result = self._run_git(worktree, ["status", "--porcelain"])
            return result.stdout.splitlines()
        except GitError:
            logger.warning("Failed to get git status for %s", worktree)
            return []

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        """Check if there are uncommitted changes in the worktree."""
        try:
            result = self._run_git(worktree, ["status", "--porcelain"])
            return len(result.stdout.strip()) > 0
        except GitError:
            logger.warning("Failed to check uncommitted changes in %s", worktree)
            return True  # Assume dirty on error (safer)

    def has_tracked_changes(self, worktree: Path, include_staged: bool = True) -> bool:
        """Check for dirty tracked files (ignores untracked/ignored)."""
        try:
            unstaged = self._run_git(worktree, ["diff", "--quiet"], check=False)
            if unstaged.returncode == 1:
                return True
            if include_staged:
                staged = self._run_git(worktree, ["diff", "--quiet", "--cached"], check=False)
                if staged.returncode == 1:
                    return True
            return False
        except GitError:
            logger.warning("Failed to check tracked changes in %s", worktree)
            return True  # Assume dirty on error (safer)

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        """List dirty file paths for guard diagnostics.

        Args:
            worktree: Path to the worktree directory.
            mode: One of "tracked", "unstaged", or "all".

        Returns:
            Sorted unique file paths on success. ``None`` when the git
            invocations needed to enumerate dirty state failed — callers
            must distinguish this from an intentionally empty filtered
            list (which is ``[]``) and fail closed accordingly. Without
            this distinction, an enumeration failure during a publish
            gate would silently approve the push (#6159 review feedback).
        """
        try:
            files: set[str] = set()

            files.update(
                self._run_git_nul_paths(worktree, ["diff", "--name-only", "-z"])
            )

            if mode in {"tracked", "all"}:
                files.update(
                    self._run_git_nul_paths(
                        worktree, ["diff", "--cached", "--name-only", "-z"]
                    )
                )

            if mode == "all":
                untracked_paths = self._run_git_nul_paths(
                    worktree,
                    ["ls-files", "--others", "--exclude-standard", "-z"],
                )
                # ``sync_cli_tools`` plants files into every worktree. In a
                # foreign repo they appear here as untracked and must not
                # count as dirty. The filter is scoped to this untracked
                # branch so tracked-modified versions of the same paths in
                # the orchestrator's own repo (picked up above via
                # ``diff --name-only``) still fire the guard.
                untracked_paths = filter_orchestrator_untracked_planted(untracked_paths)
                files.update(untracked_paths)

            return sorted(files)
        except GitError:
            logger.warning("Failed to list dirty files in %s", worktree)
            return None

    def diff_against_base(self, worktree: Path, base_ref: str) -> DiffResult:
        """Return branch diff using merge-base semantics.

        This is execution-only: callers own any policy decisions made from
        the diff. A command failure is a first-class result so control code can
        fail closed with a useful operator-facing message.

        Read byte-exactly: patch records are LF-delimited, so a carriage return
        Git emitted inside one must survive to the caller.
        """
        try:
            result = self._run_git_output_exact(
                worktree,
                [
                    "diff",
                    "--unified=0",
                    "--no-ext-diff",
                    "--no-color",
                    f"{base_ref}...HEAD",
                ],
            )
            return DiffResult(success=True, diff_text=result.stdout)
        except GitError as exc:
            error = _git_error_output(exc)
            logger.warning(
                "Failed to read diff against %s in %s: %s",
                base_ref,
                worktree,
                error,
            )
            return DiffResult(success=False, error=error)

    def read_branch_text_files(
        self, worktree: Path, paths: tuple[str, ...]
    ) -> BranchTextFilesResult:
        """Return exact tracked ``HEAD`` content for selected text files.

        Read byte-exactly, on the same terms as :meth:`diff_against_base`, so
        blob content and the patch text it is matched against agree on where
        every line ends.
        """

        files: list[BranchTextFile] = []
        try:
            for path in paths:
                result = self._run_git_output_exact(worktree, ["show", f"HEAD:{path}"])
                files.append(BranchTextFile(path=path, content=result.stdout))
            return BranchTextFilesResult(success=True, files=tuple(files))
        except GitError as exc:
            error = _git_error_output(exc)
            logger.warning(
                "Failed to read branch-tip text files in %s: %s",
                worktree,
                error,
            )
            return BranchTextFilesResult(success=False, error=error)

    def branch_post_image_paths_against_base(
        self, worktree: Path, base_ref: str
    ) -> BranchPathsResult:
        """Return branch-tip post-image paths via a path-oriented diff.

        ``--name-only --diff-filter=ACMRT -z`` lists branch-tip files (post-image
        name for renames/copies) excluding deletions, intact through spaces.
        Unlike a unified-diff parser it sees no-hunk and binary changes, so
        committed runtime artifacts cannot slip past path-based guards.

        Read byte-exactly: NUL-delimited records make every other byte part of
        a path, including a carriage return a filename is allowed to contain.
        """
        try:
            paths = self._run_git_nul_paths(
                worktree,
                [
                    "diff",
                    "--name-only",
                    "-z",
                    "--no-ext-diff",
                    "--diff-filter=ACMRT",
                    f"{base_ref}...HEAD",
                ],
            )
            return BranchPathsResult(success=True, paths=tuple(paths))
        except GitError as exc:
            error = _git_error_output(exc)
            logger.warning(
                "Failed to read branch paths against %s in %s: %s",
                base_ref,
                worktree,
                error,
            )
            return BranchPathsResult(success=False, error=error)

    def get_commits_ahead_of_main(self, worktree: Path) -> list[CommitInfo]:
        """Get commits that are ahead of main branch."""
        try:
            # Get commits in HEAD but not in origin/main
            result = self._run_git(
                worktree,
                [
                    "log",
                    "origin/main..HEAD",
                    "--format=%H|%s|%an|%h",
                ],
            )

            commits = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 3)
                if len(parts) == 4:
                    commits.append(
                        CommitInfo(
                            sha=parts[0],
                            message=parts[1],
                            author=parts[2],
                            short_sha=parts[3],
                        )
                    )
            return commits
        except GitError:
            logger.warning("Failed to get commits ahead of main in %s", worktree)
            return []

    def fetch(self, worktree: Path, remote: str = "origin") -> bool:
        """Fetch from remote."""
        try:
            self._run_git(worktree, ["fetch", remote])
            return True
        except GitError as e:
            logger.warning("Fetch failed: %s", e)
            return False

    def list_remote_branches(self, repo_root: Path, remote: str = "origin") -> list[str]:
        """List remote branches for a repository."""
        try:
            result = self._run_git(
                repo_root,
                ["branch", "-r", "--list", f"{remote}/*"],
            )
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except GitError as e:
            logger.warning("Failed to list remote branches in %s: %s", repo_root, e)
            return []

    def list_active_worktrees(self, repo_root: Path) -> set[Path]:
        """List paths of active git worktrees for a repository."""
        try:
            result = self._run_git(
                repo_root,
                ["worktree", "list", "--porcelain"],
            )
            active = set()
            for line in result.stdout.splitlines():
                if line.startswith("worktree "):
                    active.add(Path(line[9:]))
            return active
        except GitError as e:
            logger.warning("Failed to list worktrees in %s: %s", repo_root, e)
            return set()

    def list_branch_names(self, worktree: Path) -> list[str]:
        """List local and remote branch names for the repo."""
        try:
            result = self._run_git(
                worktree,
                ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin"],
            )
        except GitError as e:
            logger.warning("Failed to list branches in %s: %s", worktree, e)
            return []
        names: list[str] = []
        for line in (result.stdout or "").splitlines():
            name = line.strip()
            if not name:
                continue
            if name.startswith("origin/"):
                name = name[len("origin/"):]
            if name == "HEAD":
                continue
            names.append(name)
        return names

    def is_git_repo(self, repo_root: Path) -> bool:
        """Check if the path is a git repository."""
        try:
            self._run_git(repo_root, ["rev-parse", "--git-dir"])
            return True
        except GitError:
            return False

    def get_config_value(self, repo_root: Path, key: str) -> str | None:
        """Fetch a git config value from the repository."""
        try:
            result = self._run_git(repo_root, ["config", "--get", key])
            value = result.stdout.strip()
            return value or None
        except GitError:
            return None

    def default_branch(self, repo_root: Path, remote: str = "origin") -> str:
        """Determine the default branch for a repository."""
        return self._git.default_branch(repo_root, remote=remote)

    def get_commits_ahead_count(
        self,
        repo_root: Path,
        branch: str,
        base: str = "origin/main",
    ) -> int:
        """Count commits ahead of base for a remote branch."""
        try:
            result = self._run_git(
                repo_root,
                ["rev-list", "--count", f"{base}..origin/{branch}"],
            )
            return int(result.stdout.strip() or 0)
        except (GitError, ValueError) as e:
            logger.warning("Failed to count commits ahead for %s: %s", branch, e)
            return 0

    def get_last_commit_date(self, repo_root: Path, branch: str) -> str | None:
        """Get last commit date (relative) for a remote branch."""
        try:
            result = self._run_git(
                repo_root,
                ["log", "-1", "--format=%cr", f"origin/{branch}"],
            )
            return result.stdout.strip() or None
        except GitError as e:
            logger.warning("Failed to get last commit date for %s: %s", branch, e)
            return None

    def rebase_on_branch(
        self, worktree: Path, target: str = "origin/main"
    ) -> RebaseResult:
        """Rebase current branch onto target."""
        try:
            self._git.rebase(worktree, target)
            return RebaseResult(success=True, message=f"Rebased onto {target}")
        except GitError as e:
            # Check for conflicts
            try:
                status = self._run_git(worktree, ["status", "--porcelain"])
                conflicts = [
                    line[3:] for line in status.stdout.split("\n")
                    if line.startswith("UU ")
                ]

                # Abort the rebase
                try:
                    self._git.rebase_abort(worktree)
                    aborted = True
                except Exception:
                    aborted = False

                return RebaseResult(
                    success=False,
                    message=f"Rebase failed with conflicts",
                    conflicts=conflicts if conflicts else None,
                    aborted=aborted,
                )
            except Exception:
                error_msg = e.result.stderr or str(e)
                return RebaseResult(
                    success=False,
                    message=f"Rebase failed: {error_msg}",
                )

    def create_branch_from_current(self, worktree: Path, branch: str) -> None:
        """Create and switch to a branch from the current HEAD."""
        self._run_git(worktree, ["checkout", "-B", branch], timeout_s=60)

    def _check_e2e_dry_run(
        self, branch: str | None, remote: str
    ) -> PushResult | None:
        """Check for E2E dry-run mode and return early result if enabled."""
        if os.environ.get("E2E_DRY_RUN_PUSH") == "1":
            logger.info(
                "[E2E_DRY_RUN] Push skipped (would push branch=%s to remote=%s)",
                branch,
                remote,
            )
            return PushResult(
                success=True,
                branch=branch or "unknown",
                remote=remote,
                message=f"[DRY_RUN] Would push {branch} to {remote}",
            )
        return None

    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        set_upstream: bool = True,
        skip_hooks: bool = False,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> PushResult:
        """Push current branch to remote with --force-with-lease.

        Args:
            worktree: Path to the worktree.
            remote: Remote name (default: origin).
            set_upstream: Set upstream tracking (default: True).
            skip_hooks: Skip pre-push hooks with --no-verify (default: False).
        """
        branch = self.get_current_branch(worktree)

        # E2E dry-run mode: verify push would be called but don't actually push
        dry_run_result = self._check_e2e_dry_run(branch, remote)
        if dry_run_result:
            return dry_run_result

        if not branch:
            return PushResult(
                success=False,
                branch="",
                remote=remote,
                message="Could not determine current branch",
                retryable=False,
            )

        # Try to fetch the branch to update tracking refs for --force-with-lease.
        auth_env, auth_error = git_push_ops.push_auth_env_or_failure(
            self._git_auth,
            remote=remote,
            branch=branch,
        )
        if auth_error:
            return auth_error
        fetch_error = git_push_ops.fetch_for_push(
            self._run_git,
            self._clear_stale_remote_ref,
            worktree,
            remote,
            branch,
            env=auth_env,
        )
        if fetch_error:
            return fetch_error

        if extra_env:
            # The orchestrator's own decisions reach the pre-push hook here, on
            # the process it spawns -- never through the worktree.
            auth_env = {**(auth_env or {}), **extra_env}

        args = git_push_ops.build_push_args(remote, branch, set_upstream, skip_hooks)

        start = time.monotonic()
        try:
            _result = self._run_git(worktree, args, timeout_s=300, env=auth_env)
            duration = time.monotonic() - start
            logger.info(
                "Push completed in %.2fs: branch=%s remote=%s skip_hooks=%s",
                duration,
                branch,
                remote,
                skip_hooks,
            )
            return PushResult(
                success=True,
                branch=branch,
                remote=remote,
                message=f"Pushed {branch} to {remote}",
            )
        except GitError as e:
            duration = time.monotonic() - start
            error_msg = _git_error_output(e)
            logger.warning(
                "Push failed in %.2fs: branch=%s remote=%s skip_hooks=%s error=%s",
                duration,
                branch,
                remote,
                skip_hooks,
                error_msg,
            )
            return PushResult(
                success=False,
                branch=branch,
                remote=remote,
                message=error_msg,
                retryable=git_push_ops.determine_retryable(error_msg),
            )

    def get_issue_number_from_branch(self, worktree: Path) -> int | None:
        """Extract issue number from branch name.

        First tries the canonical format ({issue_number}-{title}) via the
        centralized function. Falls back to legacy patterns for externally
        created branches.
        """
        from ..adapters.worktree._worktree import extract_issue_number_from_branch

        branch = self.get_current_branch(worktree)
        if not branch:
            return None

        # Try canonical format first (e.g., "328-feature-name")
        issue_num = extract_issue_number_from_branch(branch)
        if issue_num is not None:
            return issue_num

        # Fallback patterns for legacy or externally created branches
        fallback_patterns = [
            r"issue-(\d+)",      # issue-123 (legacy format)
            r"/(\d+)-",          # feature/123-thing
        ]

        for pattern in fallback_patterns:
            match = re.search(pattern, branch)
            if match:
                return int(match.group(1))

        return None

    def push_preflight(
        self,
        worktree: Path,
        remote: str = "origin",
    ) -> PreflightResult:
        """Check if a push would succeed (dry-run).

        This performs a git push --dry-run to verify the push would work
        without actually pushing. Useful for catching divergence issues
        while the agent is still active and can fix them.
        """
        branch = self.get_current_branch(worktree)
        if not branch:
            branch = self._branch_from_metadata(worktree)
        if not branch:
            return PreflightResult(
                would_succeed=False,
                error="Could not determine current branch",
                fix_hint="Ensure you are on a branch, not in detached HEAD state",
            )

        # Try to fetch the branch to update tracking refs for --force-with-lease.
        auth_env, auth_error = git_push_ops.preflight_auth_env_or_failure(
            self._git_auth,
            remote=remote,
        )
        if auth_error:
            return auth_error
        fetch_error = git_push_ops.fetch_for_preflight(
            self._run_git,
            self._clear_stale_remote_ref,
            worktree,
            remote,
            branch,
            env=auth_env,
        )
        if fetch_error:
            return fetch_error

        args = ["push", "--dry-run", "-u", remote, branch, "--force-with-lease"]

        try:
            self._run_git(worktree, args, timeout_s=60, env=auth_env)
            return PreflightResult(would_succeed=True)
        except GitError as e:
            error_msg = e.result.stderr if e.result.stderr else str(e)
            return PreflightResult(
                would_succeed=False,
                error=error_msg,
                fix_hint=git_push_ops.get_preflight_fix_hint(error_msg),
            )
        except Exception as e:
            error_msg = str(e)
            if "timed out" in error_msg.lower():
                return PreflightResult(
                    would_succeed=False,
                    error="Push check timed out",
                    fix_hint="Network or remote issue - retry later",
                )
            return PreflightResult(
                would_succeed=False,
                error=error_msg,
            )

    def get_worktree_root(self, worktree: Path) -> Path | None:
        """Get the root of the worktree (handles being in subdirectory)."""
        try:
            result = self._run_git(worktree, ["rev-parse", "--show-toplevel"])
            return Path(result.stdout.strip())
        except GitError:
            return None

    def commit_all(
        self, worktree: Path, message: str, allow_empty: bool = False
    ) -> bool:
        """Stage all changes and commit.

        Args:
            worktree: Path to the worktree.
            message: Commit message.
            allow_empty: Whether to allow empty commits.

        Returns:
            True if commit succeeded, False otherwise.
        """
        try:
            # Stage all changes
            self._run_git(worktree, ["add", "-A"])

            # Commit
            args = ["commit", "-m", message]
            if allow_empty:
                args.append("--allow-empty")

            self._run_git(worktree, args)
            return True
        except GitError as e:
            # "nothing to commit" is not an error
            output = (e.result.stdout or "") + (e.result.stderr or "")
            if "nothing to commit" in output:
                return True
            logger.warning("Commit failed: %s", e)
            return False

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
        try:
            self._run_git(
                repo_root,
                ["push", "--no-verify", remote, "--delete", branch],
            )
            logger.info("Deleted remote branch: %s/%s", remote, branch)
            return True
        except GitError as e:
            # Branch might already be deleted
            output = (e.result.stdout or "") + (e.result.stderr or "")
            if "remote ref does not exist" in output:
                logger.info("Remote branch already deleted: %s/%s", remote, branch)
                return True
            logger.warning("Failed to delete remote branch %s/%s: %s", remote, branch, e)
            return False


def _git_error_output(error: GitError) -> str:
    """Return the full user-facing output from a failed git command."""
    parts: list[str] = []
    stdout = (error.result.stdout or "").strip()
    stderr = (error.result.stderr or "").strip()
    if stdout:
        parts.append(stdout)
    if stderr and stderr != stdout:
        parts.append(stderr)
    if parts:
        return "\n".join(parts)
    return str(error)
