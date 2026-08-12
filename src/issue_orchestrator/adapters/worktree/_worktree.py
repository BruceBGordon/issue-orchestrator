"""Git worktree management module."""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from ...infra.runtime_artifacts import is_cleanup_safe_untracked_path
from ...infra.logging_config import issue_log
from ...ports.git import GitResult
from ...ports.worktree_policy import WorktreePolicy
from ...ports.worktree_manager import RegisteredWorktree, WorktreeReuseOptions
from ...infra.worktree_base import resolve_base_branch
from ._worktree_errors import WorktreeError as WorktreeError
from ._worktree_git import _git, _git_env_no_prompt, _git_run
from ._worktree_hooks import HOOKS_DIR as HOOKS_DIR
from ._worktree_runtime_setup import WorktreeRuntimeSetup


@dataclass
class ResetInfo:
    """Information about work discarded during worktree reset.

    This is returned by _update_worktree_onto_main and propagated
    to WorktreeInfo for event emission.
    """
    success: bool
    uncommitted_discarded: int = 0  # Count of uncommitted changes discarded
    commits_discarded: int = 0  # Count of commits discarded (on rebase failure)
    reason: str | None = None


@dataclass
class _WorktreeReuseResult:
    """Result of attempting to reuse an existing worktree."""
    success: bool  # True if worktree was successfully reused
    worktree_path: Path | None = None
    branch_name: str | None = None
    reset_info: ResetInfo | None = None
    recreated_reason: str | None = None  # Reason if worktree was deleted/recreated


logger = logging.getLogger(__name__)

# Git writes index.lock during operations; treat short-lived locks as in-flight.
STALE_GIT_LOCK_SECONDS = 5
STALE_GIT_LOCK_RECHECK_SECONDS = 2
_BRANCH_IN_USE_BY_WORKTREE_RE = re.compile(
    r"fatal:\s+'([^']+)'\s+is already used by worktree at '([^']+)'"
)


def _ensure_origin_branch(repo_root: Path, branch: str) -> None:
    fetch_result = _git_run(
        repo_root,
        ["fetch", "origin", branch],
        check=False,
        env=_git_env_no_prompt(),
    )
    if fetch_result.returncode != 0:
        raise WorktreeError(
            f"Failed to fetch origin/{branch}: {fetch_result.stderr.strip()}"
        )
    ref_result = _git_run(
        repo_root,
        ["rev-parse", "--verify", f"origin/{branch}"],
        check=False,
    )
    if ref_result.returncode != 0:
        raise WorktreeError(f"origin/{branch} does not exist after fetch")


def get_default_branch(repo_root: Path) -> str:
    """
    Get the repository's default branch name (main, master, etc.).

    Attempts to detect from remote HEAD, falls back to 'main'.

    Args:
        repo_root: Path to the repository root

    Returns:
        The default branch name (e.g., 'main', 'master')
    """
    branch = _git.default_branch(repo_root)
    logger.debug("Detected default branch: %s", branch)
    return branch


def _resolve_base_branch(repo_root: Path, base_branch_override: str | None) -> str:
    """Resolve the base branch used for worktree creation/reset."""
    resolved = resolve_base_branch(
        repo_root,
        config_override=base_branch_override,
        default_branch_resolver=_git.default_branch,
        log=logger,
    )
    return resolved.branch


def _update_worktree_onto_main(
    worktree_path: Path,
    repo_root: Path,
    base_branch: str | None = None,
) -> ResetInfo:
    """
    Update a worktree's branch onto the latest base branch.

    This is crucial for reruns - branches created from old base need to be
    rebased onto current base to get the latest code changes.

    Strategy: Success > preserving agent work. If rebase fails for any reason
    (conflicts, stale branch, etc.), discard all local work and reset to main.
    This ensures agents always work with the latest code.

    The sequence:
    1. Fetch origin to get latest base branch
    2. Discard any uncommitted changes
    3. Try to rebase current branch onto origin/<base>
    4. If rebase fails, hard reset to origin/<base> (discards all local commits)

    Args:
        worktree_path: Path to the worktree to update
        repo_root: Path to the main repository
        base_branch: Branch name to rebase onto (e.g., main, master)

    Returns:
        ResetInfo with success status and counts of discarded work
    """
    uncommitted_discarded = 0
    commits_discarded = 0
    try:
        base = _resolve_base_branch(repo_root, base_branch)

        # Step 1: Fetch origin to get latest base branch
        fetch_result = _git_run(
            worktree_path,
            ["fetch", "origin", base],
            check=False,
            env=_git_env_no_prompt(),
        )
        if fetch_result.returncode != 0:
            return ResetInfo(
                success=False,
                reason=f"fetch_failed: {fetch_result.stderr.strip()}",
            )

        ref_result = _git_run(
            worktree_path,
            ["rev-parse", "--verify", f"origin/{base}"],
            check=False,
        )
        if ref_result.returncode != 0:
            return ResetInfo(
                success=False,
                reason=f"origin_ref_missing: origin/{base}",
            )

        # Step 2: Get current branch
        branch_result = _git_run(
            worktree_path,
            ["rev-parse", "--abbrev-ref", "HEAD"],
            check=False,
        )
        if branch_result.returncode != 0:
            logger.warning("Could not determine current branch in %s", worktree_path)
            return ResetInfo(success=False, reason="branch_unknown")

        current_branch = branch_result.stdout.strip()
        if current_branch == base:
            # Already on base branch, just pull
            _git_run(
                worktree_path,
                ["pull", "--ff-only"],
                check=False,
            )
            return ResetInfo(success=True)

        # Step 3: Discard any uncommitted changes
        # We prioritize success over preserving uncommitted work
        status_result = _git_run(
            worktree_path,
            ["status", "--porcelain"],
            check=False,
        )
        if status_result.returncode == 0 and status_result.stdout.strip():
            uncommitted_discarded = len(status_result.stdout.strip().split("\n"))
            logger.warning(
                "[WORKTREE_RESET] Discarding %d uncommitted changes in %s (branch: %s)",
                uncommitted_discarded,
                worktree_path,
                current_branch,
            )
        _git_run(
            worktree_path,
            ["reset", "--hard", "HEAD"],
            check=False,
        )
        _git_run(
            worktree_path,
            ["clean", "-fd"],
            check=False,
        )

        # Step 4: Try to rebase onto origin/<base>
        logger.info(
            "Rebasing branch %s onto origin/%s in %s",
            current_branch,
            base,
            worktree_path,
        )
        rebase_result = _git_run(
            worktree_path,
            ["rebase", f"origin/{base}"],
            check=False,
        )

        if rebase_result.returncode != 0:
            # Rebase failed - discard everything and reset to main
            # Abort the rebase first
            _git_run(
                worktree_path,
                ["rebase", "--abort"],
                check=False,
            )

            # Count how many commits we're discarding
            commits_result = _git_run(
                worktree_path,
                ["rev-list", "--count", f"origin/{base}..HEAD"],
                check=False,
            )
            if commits_result.returncode == 0 and commits_result.stdout.strip():
                try:
                    commits_discarded = int(commits_result.stdout.strip())
                except ValueError:
                    pass

            logger.warning(
                "[WORKTREE_RESET] Rebase failed, discarding %d commits and resetting to %s "
                "(branch: %s, path: %s, error: %s)",
                commits_discarded,
                base,
                current_branch,
                worktree_path,
                rebase_result.stderr.strip(),
            )

            # Hard reset to origin/<base> - discards all local commits
            _git_run(
                worktree_path,
                ["reset", "--hard", f"origin/{base}"],
                check=False,
            )
            return ResetInfo(
                success=True,
                uncommitted_discarded=uncommitted_discarded,
                commits_discarded=commits_discarded,
            )

        logger.info(
            "Successfully rebased branch %s onto origin/%s in %s",
            current_branch,
            base,
            worktree_path,
        )
        return ResetInfo(success=True, uncommitted_discarded=uncommitted_discarded)

    except Exception as e:
        logger.exception("Error updating worktree onto main: %s", e)
        return ResetInfo(success=False, reason=str(e))


def _push_dry_run_preflight(
    worktree_path: Path,
    branch_name: str,
    *,
    allow_no_verify: bool,
) -> tuple[bool, str]:
    """Check if a dry-run push would succeed for a reused worktree."""
    import time

    cmd = ["push", "--dry-run", "--force-with-lease"]
    if allow_no_verify:
        cmd.append("--no-verify")
    cmd += ["-u", "origin", branch_name]
    last_error = ""
    for attempt in range(1, 4):
        result = _git_run(worktree_path, cmd, check=False)
        if result.returncode == 0:
            return True, ""
        stderr = (result.stderr or "").strip()
        last_error = stderr
        if attempt < 3:
            time.sleep(0.5 * (2 ** (attempt - 1)))
    return False, f"push dry-run failed: {last_error}"


def slugify(text: str, max_length: int = 40) -> str:
    """
    Convert text to a branch-friendly slug.

    Args:
        text: Text to slugify
        max_length: Maximum length of the resulting slug

    Returns:
        Slugified text suitable for use in branch names
    """
    # Lowercase and replace non-alphanumeric with hyphens
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower())
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    # Truncate and remove trailing hyphens that may result from truncation
    return slug[:max_length].rstrip('-')


def generate_branch_name(issue_number: int, issue_title: str) -> str:
    """
    Generate a branch name from issue number and title.

    Args:
        issue_number: GitHub issue number
        issue_title: GitHub issue title

    Returns:
        Branch name in format: {number}-{slugified-title}
        Example: 123-add-user-authentication
    """
    slug = slugify(issue_title, max_length=50)
    return f"{issue_number}-{slug}"


# Regex pattern for extracting issue number from branch name
# Branch format: {issue_number}-{title-slug} (e.g., "328-add-feature")
BRANCH_ISSUE_PATTERN = re.compile(r'^(\d+)-')


def extract_issue_number_from_branch(branch_name: str) -> int | None:
    """
    Extract issue number from a branch name.

    This is the inverse of generate_branch_name(). All code that needs to
    extract issue numbers from branch names should use this function.

    Args:
        branch_name: Branch name (e.g., "328-add-feature")

    Returns:
        Issue number if found, None otherwise
    """
    match = BRANCH_ISSUE_PATTERN.match(branch_name)
    if match:
        return int(match.group(1))
    return None


def _branch_matches_issue(branch_name: str, issue_number: int) -> bool:
    extracted = extract_issue_number_from_branch(branch_name)
    return extracted == issue_number


def _list_branch_names(repo_root: Path) -> list[str]:
    result = _git_run(
        repo_root,
        ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin"],
        check=False,
    )
    if result.returncode != 0:
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


def next_branch_name(repo_root: Path, branch_name: str) -> str:
    base = re.sub(r"-r\d+$", "", branch_name)
    existing = _list_branch_names(repo_root)
    pattern = re.compile(rf"^{re.escape(base)}-r(\d+)$")
    max_suffix = 0
    for name in existing:
        match = pattern.match(name)
        if match:
            try:
                max_suffix = max(max_suffix, int(match.group(1)))
            except ValueError:
                continue
    return f"{base}-r{max_suffix + 1}"


def _delete_remote_branch(repo_root: Path, branch_name: str) -> bool:
    result = _git_run(
        repo_root,
        # Bypass local hooks (completion commands) for branch deletion only.
        ["push", "--no-verify", "origin", "--delete", branch_name],
        check=False,
        env=_git_env_no_prompt(),
    )
    if result.returncode == 0:
        return True
    stderr = (result.stderr or "").lower()
    if "remote ref does not exist" in stderr or "remote ref not found" in stderr:
        return True
    return False


def find_worktree_for_branch(repo_root: Path, branch_name: str) -> Path | None:
    """
    Find an existing worktree that has the given branch checked out.

    Args:
        repo_root: Path to the main git repository
        branch_name: Branch name to search for

    Returns:
        Path to the worktree if found, None otherwise
    """
    logger.debug("Searching for worktree: repo=%s branch=%s", repo_root, branch_name)
    result = _git_run(
        repo_root,
        ["worktree", "list", "--porcelain"],
        check=False,
    )
    if result.returncode != 0:
        logger.debug("worktree list failed: repo=%s returncode=%s", repo_root, result.returncode)
        return None

    # Parse porcelain output:
    # worktree /path/to/worktree
    # HEAD abc123
    # branch refs/heads/branch-name
    # (blank line)
    current_worktree = None
    for line in result.stdout.split("\n"):
        if line.startswith("worktree "):
            current_worktree = Path(line.split(" ", 1)[1])
        elif line.startswith("branch refs/heads/"):
            current_branch = line.split("refs/heads/", 1)[1]
            if current_branch == branch_name and current_worktree:
                logger.info("Found existing worktree for branch %s: %s", branch_name, current_worktree)
                return current_worktree

    return None


def _get_worktree_git_env(worktree_path: Path) -> dict[str, str] | None:
    """Get environment variables for worktree git operations.

    Returns:
        Environment dict with GIT_DIR and GIT_WORK_TREE set, or None if not a worktree.
    """
    git_file = worktree_path / ".git"
    if not git_file.exists():
        return None
    content = git_file.read_text().strip()
    if not content.startswith("gitdir:"):
        return None
    git_dir = content.split(":", 1)[1].strip()
    env = os.environ.copy()
    env["GIT_DIR"] = git_dir
    env["GIT_WORK_TREE"] = str(worktree_path)
    return env


def _handle_stale_lock_and_retry(
    worktree_path: Path,
    cmd: list[str],
    env: dict[str, str] | None,
    result: GitResult,
) -> GitResult | None:
    """Handle stale git lock file and retry operation.

    Returns:
        Successful GitResult if retry worked, None otherwise.
    """
    lock_match = re.search(r"Unable to create '([^']+index\.lock)'", result.stderr or "")
    if not lock_match:
        return None

    lock_path = Path(lock_match.group(1))
    if not lock_path.exists():
        return None

    age_seconds = time.time() - lock_path.stat().st_mtime
    if age_seconds < STALE_GIT_LOCK_SECONDS:
        time.sleep(STALE_GIT_LOCK_RECHECK_SECONDS)
        if lock_path.exists():
            age_seconds = time.time() - lock_path.stat().st_mtime

    if age_seconds <= STALE_GIT_LOCK_SECONDS:
        return None

    logger.warning(
        "Removing stale git lock before detach: path=%s age=%.1fs",
        lock_path,
        age_seconds,
    )
    try:
        lock_path.unlink()
    except OSError:
        return None

    retry = _git_run(worktree_path, cmd, check=False, env=env)
    if retry.returncode == 0:
        return retry
    return None


def _detach_worktree_branch(worktree_path: Path, branch_name: str) -> None:
    logger.info(
        "Detaching worktree branch to free branch: path=%s branch=%s",
        worktree_path,
        branch_name,
    )
    cmd = ["checkout", "--detach"]
    env = _get_worktree_git_env(worktree_path)
    if env:
        logger.info("Detaching with explicit GIT_DIR for worktree: %s", worktree_path)

    result = _git_run(worktree_path, cmd, check=False, env=env)
    if result.returncode == 0:
        return

    # Try to handle stale lock
    retry_result = _handle_stale_lock_and_retry(worktree_path, cmd, env, result)
    if retry_result is not None:
        return

    raise WorktreeError(
        "Failed to detach worktree branch: "
        f"path={worktree_path} branch={branch_name} stderr={result.stderr}"
    )


def _resolve_repo_root_from_worktree(worktree_path: Path) -> Path | None:
    worktree_path = Path(worktree_path)
    git_entry = worktree_path / ".git"
    if not git_entry.exists():
        return None
    if git_entry.is_dir():
        return worktree_path
    try:
        content = git_entry.read_text().strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    git_dir = Path(content.split(":", 1)[1].strip()).resolve()
    if git_dir.name == ".git":
        return git_dir.parent
    if git_dir.parent.name == "worktrees":
        return git_dir.parent.parent.parent
    return git_dir.parent


def _remove_existing_worktree_path(repo_root: Path, worktree_path: Path) -> None:
    logger.info("Removing existing worktree path for fresh create: %s", worktree_path)
    result = _git_run(
        repo_root,
        ["worktree", "remove", "--force", str(worktree_path)],
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to remove worktree via git, deleting directory: path=%s stderr=%s",
            worktree_path,
            result.stderr.strip(),
        )
        shutil.rmtree(worktree_path, ignore_errors=True)


def _try_reuse_worktree(
    worktree_path: Path,
    branch_name: str,
    repo_root: Path,
    issue_number: int,
    policy: WorktreePolicy,
    reuse_push_preflight: bool,
    allow_no_verify_dry_run_preflight: bool,
    base_branch: str | None,
    preserve_branch: bool = False,
) -> _WorktreeReuseResult:
    """Try to reuse an existing worktree, validating and preparing it.

    When ``preserve_branch`` is True the branch is left exactly as-is: the
    rebase/hard-reset onto the base branch is skipped entirely. This is used
    for tech_lead investigations, which read the subject's branch as evidence and
    must never mutate it (rebasing/resetting a stranded branch would discard
    its unpushed work).

    Returns:
        _WorktreeReuseResult indicating success/failure with details.
    """
    # Policy: validate worktree can be reused
    validation = policy.validate_for_reuse(worktree_path, branch_name, repo_root)
    if not validation.can_reuse:
        logger.warning(
            issue_log(issue_number, "Worktree failed validation, deleting: %s"),
            validation.reason,
        )
        policy.delete_worktree(worktree_path, repo_root)
        return _WorktreeReuseResult(
            success=False,
            recreated_reason=f"validation_failed: {validation.reason}",
        )

    # Rebase onto latest base branch (critical for reruns with stale branches).
    # A tech_lead investigation reads the subject's branch as evidence, so it must
    # never rebase or hard-reset it — skip the update and leave it intact.
    if preserve_branch:
        logger.info(
            issue_log(
                issue_number,
                "[WORKTREE_PRESERVE] skipping rebase/reset for tech_lead "
                "investigation, branch %s left intact",
            ),
            branch_name,
        )
        reset_info = ResetInfo(success=True)
    else:
        reset_info = _update_worktree_onto_main(worktree_path, repo_root, base_branch)

    # Policy: sync remote refs to prevent stale-info push failures
    sync_result = policy.sync_remote_refs(worktree_path, branch_name)
    if not sync_result.success:
        logger.warning(
            issue_log(issue_number, "Failed to sync remote refs, deleting worktree: %s"),
            sync_result.reason,
        )
        policy.delete_worktree(worktree_path, repo_root)
        return _WorktreeReuseResult(
            success=False,
            recreated_reason=f"sync_failed: {sync_result.reason}",
        )

    if not reset_info.success:
        logger.warning(
            issue_log(issue_number, "Reset to base branch failed, deleting worktree: %s"),
            reset_info.reason or "unknown",
        )
        policy.delete_worktree(worktree_path, repo_root)
        return _WorktreeReuseResult(
            success=False,
            recreated_reason=f"reset_failed: {reset_info.reason or 'rebase failed'}",
        )

    # Optional push preflight check
    if reuse_push_preflight:
        ok, reason = _push_dry_run_preflight(
            worktree_path,
            branch_name,
            allow_no_verify=allow_no_verify_dry_run_preflight,
        )
        if not ok:
            logger.warning(
                issue_log(issue_number, "Push preflight failed, deleting worktree: %s"),
                reason,
            )
            policy.delete_worktree(worktree_path, repo_root)
            return _WorktreeReuseResult(
                success=False,
                recreated_reason=f"push_preflight_failed: {reason}",
            )
        logger.info(issue_log(issue_number, "Push preflight ok for reused worktree"))

    return _WorktreeReuseResult(
        success=True,
        worktree_path=worktree_path,
        branch_name=branch_name,
        reset_info=reset_info,
    )


def _handle_branch_on_recreate(
    repo_root: Path,
    branch_name: str,
    issue_number: int,
    reuse_options: WorktreeReuseOptions,
) -> str:
    """Handle branch options when recreating a worktree.

    Returns:
        Possibly modified branch name.
    """
    worktree_branch_on_recreate = reuse_options.worktree_branch_on_recreate
    if worktree_branch_on_recreate == "delete":
        if not reuse_options.allow_remote_branch_delete:
            logger.info(
                issue_log(issue_number, "Skipping remote branch delete before recreate: %s"),
                branch_name,
            )
        elif _delete_remote_branch(repo_root, branch_name):
            logger.info(issue_log(issue_number, "Deleted remote branch before recreate: %s"), branch_name)
        else:
            logger.warning(issue_log(issue_number, "Failed to delete remote branch before recreate: %s"), branch_name)
    elif worktree_branch_on_recreate == "create_new_branch":
        new_branch = next_branch_name(repo_root, branch_name)
        logger.info(
            issue_log(issue_number, "Creating new branch for recreated worktree: %s -> %s"),
            branch_name,
            new_branch,
        )
        return new_branch
    return branch_name


def _build_worktree_add_command(
    repo_root: Path,
    worktree_path: Path,
    branch_name: str,
    base_branch: str | None,
    seed_ref: str | None,
) -> list[str]:
    """Build the git worktree add command.

    Returns:
        The git command arguments list.
    """
    # Check if branch already exists
    branch_check = _git_run(
        repo_root,
        ["rev-parse", "--verify", branch_name],
        check=False,
    )
    branch_exists = branch_check.returncode == 0
    logger.debug(
        "Branch exists check: repo=%s branch=%s exists=%s",
        repo_root,
        branch_name,
        branch_exists,
    )

    if branch_exists:
        # Use existing branch
        return ["worktree", "add", str(worktree_path), branch_name]

    # Try to fetch remote branch (for review/rework sessions)
    fetch_result = _git_run(
        repo_root,
        ["fetch", "origin", branch_name],
        check=False,
    )
    if fetch_result.returncode == 0:
        return [
            "worktree", "add",
            str(worktree_path), "-b", branch_name, f"origin/{branch_name}"
        ]

    if seed_ref:
        seed_ref_result = _git_run(
            repo_root,
            ["rev-parse", "--verify", seed_ref],
            check=False,
        )
        if seed_ref_result.returncode != 0:
            raise WorktreeError(
                f"Invalid worktree seed ref {seed_ref!r}: {seed_ref_result.stderr.strip()}"
            )
        logger.info("Creating new branch from worktree seed ref: %s", seed_ref)
        return [
            "worktree", "add",
            str(worktree_path), "-b", branch_name, seed_ref
        ]

    # Create new branch from default branch, NOT from HEAD
    # This ensures agent worktrees don't inherit commits from user's feature branch
    default_branch = _resolve_base_branch(repo_root, base_branch)
    logger.info("Creating new branch from default branch: %s", default_branch)
    _ensure_origin_branch(repo_root, default_branch)
    return [
        "worktree", "add",
        str(worktree_path), "-b", branch_name, f"origin/{default_branch}"
    ]


@dataclass
class _WorktreeCreateContext:
    """Context for worktree creation."""
    repo_root: Path
    worktree_path: Path
    branch_name: str
    base_branch: str | None
    seed_ref: str | None
    issue_number: int
    policy: WorktreePolicy
    reuse_options: WorktreeReuseOptions
    runtime_setup: WorktreeRuntimeSetup
    disable_reuse: bool


def _init_worktree_context(
    repo_root: Path,
    issue_number: int,
    issue_title: str,
    worktree_base: Path | None,
    base_branch: str | None,
    seed_ref: str | None,
    branch_name: str | None,
    reuse_options: WorktreeReuseOptions | None,
    policy: WorktreePolicy | None,
    enforce_hooks: bool,
    pre_push_hook: Path | None,
    worktree_name: str | None = None,
) -> _WorktreeCreateContext:
    """Initialize context for worktree creation."""
    if policy is None:
        from .worktree_policy import default_policy
        policy = default_policy
    if reuse_options is None:
        reuse_options = WorktreeReuseOptions()

    repo_root = Path(repo_root).resolve()
    if not (repo_root / ".git").exists():
        raise WorktreeError(f"Not a git repository: {repo_root}")

    worktree_base = Path(worktree_base).resolve() if worktree_base else repo_root.parent
    worktree_base.mkdir(parents=True, exist_ok=True)
    branch_name = branch_name or generate_branch_name(issue_number, issue_title)
    # A run-scoped scratch worktree overrides the derived per-issue directory
    # basename so a disposable investigation never collides with — or reuses —
    # its subject's worktree.
    worktree_path = worktree_base / (worktree_name or f"{repo_root.name}-{issue_number}")
    disable_reuse = (
        os.environ.get("ORCHESTRATOR_DISABLE_WORKTREE_REUSE") == "1"
        or reuse_options.disable_reuse
    )
    base_branch = _resolve_base_branch(repo_root, base_branch)

    logger.info(
        issue_log(issue_number, "Create worktree requested: branch=%s base=%s"),
        branch_name,
        worktree_base,
    )

    return _WorktreeCreateContext(
        repo_root=repo_root,
        worktree_path=worktree_path,
        branch_name=branch_name,
        base_branch=base_branch,
        seed_ref=seed_ref,
        issue_number=issue_number,
        policy=policy,
        reuse_options=reuse_options,
        runtime_setup=WorktreeRuntimeSetup(
            repo_root=repo_root,
            enforce_hooks=enforce_hooks,
            pre_push_hook=pre_push_hook,
            allow_no_verify_dry_run_preflight=(
                reuse_options.allow_no_verify_dry_run_preflight
            ),
        ),
        disable_reuse=disable_reuse,
    )


def create_worktree(
    repo_root: Path,
    issue_number: int,
    issue_title: str,
    worktree_base: Path | None = None,
    base_branch: str | None = None,
    seed_ref: str | None = None,
    enforce_hooks: bool = True,
    pre_push_hook: Path | None = None,
    branch_name: str | None = None,
    reuse_options: WorktreeReuseOptions | None = None,
    policy: WorktreePolicy | None = None,
    worktree_name: str | None = None,
) -> tuple[Path, str, str, str | None, bool, int, int]:
    """
    Create a new git worktree for the given issue.

    Uses a "validate or delete" policy: if an existing worktree cannot be
    prepared for a clean session, it is deleted and a fresh one is created.

    Args:
        repo_root: Path to the main git repository
        issue_number: GitHub issue number
        issue_title: GitHub issue title (used to generate branch name if not provided)
        worktree_base: Base directory for worktrees. Defaults to parent of repo_root.
        base_branch: Base branch override (e.g., "main" or "master")
        seed_ref: Optional local ref used to seed fresh issue worktrees
        enforce_hooks: Whether to install pre-push hooks
        pre_push_hook: Custom pre-push hook path
        branch_name: Specific branch to use (for checking out existing branches like PR reviews)
        reuse_options: Options controlling reuse behavior
        policy: Worktree setup policy (defaults to ValidateOrDeletePolicy)
        worktree_name: Overrides the derived worktree directory basename
            (default ``<repo>-<issue_number>``) for a run-scoped scratch worktree.

    Returns:
        Tuple of (worktree_path, branch_name, reuse_status, reuse_reason,
        rebase_failed, uncommitted_discarded, commits_discarded)
        where rebase_failed is True if rebase failed and work was discarded to reset to main.
        uncommitted_discarded: count of uncommitted changes that were discarded
        commits_discarded: count of commits that were discarded (on rebase failure)

    Raises:
        WorktreeError: If worktree creation fails
    """
    try:
        ctx = _init_worktree_context(
            repo_root, issue_number, issue_title, worktree_base, base_branch, seed_ref, branch_name,
            reuse_options, policy, enforce_hooks, pre_push_hook, worktree_name,
        )

        # Prune stale worktrees
        prune_result = _git_run(ctx.repo_root, ["worktree", "prune"], check=False)
        logger.debug("Worktree prune: returncode=%s", prune_result.returncode)

        reuse_result, recreated_reason = _attempt_reuse(ctx)
        if reuse_result is not None:
            return reuse_result

        # Handle branch on recreate
        final_branch = ctx.branch_name
        if recreated_reason and _branch_matches_issue(ctx.branch_name, ctx.issue_number):
            final_branch = _handle_branch_on_recreate(
                ctx.repo_root, ctx.branch_name, ctx.issue_number, ctx.reuse_options
            )

        return _create_fresh_worktree(
            ctx.repo_root, ctx.worktree_path, final_branch, ctx.base_branch, ctx.seed_ref, ctx.issue_number,
            ctx.runtime_setup, recreated_reason,
        )
    except WorktreeError:
        raise
    except Exception as e:
        raise WorktreeError(f"Error creating worktree: {e}")


def _attempt_reuse(
    ctx: _WorktreeCreateContext,
) -> tuple[tuple[Path, str, str, str | None, bool, int, int] | None, str | None]:
    """Attempt to reuse an existing worktree, returning (result, recreated_reason)."""
    recreated_reason: str | None = None
    if ctx.disable_reuse:
        recreated_reason = _handle_reuse_disabled(
            ctx.repo_root, ctx.worktree_path, ctx.branch_name, ctx.issue_number
        )
        return None, recreated_reason

    reuse_result, reuse_recreated_reason = _try_reuse_by_branch(
        ctx.repo_root, ctx.branch_name, ctx.issue_number, ctx.policy,
        ctx.reuse_options, ctx.runtime_setup, ctx.base_branch,
    )
    if reuse_result is not None:
        return reuse_result, None
    if reuse_recreated_reason:
        recreated_reason = reuse_recreated_reason

    if ctx.worktree_path.exists():
        reuse_result, reuse_recreated_reason = _try_reuse_by_path(
            ctx.worktree_path, ctx.repo_root, ctx.issue_number, ctx.policy,
            ctx.reuse_options, ctx.runtime_setup, ctx.base_branch,
        )
        if reuse_result is not None:
            return reuse_result, None
        if reuse_recreated_reason and not recreated_reason:
            recreated_reason = reuse_recreated_reason

    return None, recreated_reason


def _handle_reuse_disabled(
    repo_root: Path,
    worktree_path: Path,
    branch_name: str | None,
    issue_number: int,
) -> str | None:
    """Handle worktree cleanup when reuse is disabled."""
    logger.info("Worktree reuse disabled (ORCHESTRATOR_DISABLE_WORKTREE_REUSE=1)")
    recreated_reason = None
    if worktree_path.exists():
        recreated_reason = "reuse_disabled: existing worktree path removed"
        _remove_existing_worktree_path(repo_root, worktree_path)
    if branch_name:
        existing_worktree = find_worktree_for_branch(repo_root, branch_name)
        if existing_worktree and existing_worktree.exists():
            recreated_reason = "reuse_disabled: existing worktree branch removed"
            _detach_worktree_branch(existing_worktree, branch_name)
    return recreated_reason


def _try_reuse_by_branch(
    repo_root: Path,
    branch_name: str,
    issue_number: int,
    policy: WorktreePolicy,
    reuse_options: WorktreeReuseOptions,
    runtime_setup: WorktreeRuntimeSetup,
    base_branch: str | None,
) -> tuple[tuple[Path, str, str, str | None, bool, int, int] | None, str | None]:
    """Try to reuse an existing worktree by branch name.

    Returns:
        Tuple of (result, recreated_reason) where:
        - result is the full result tuple if successful, None if failed
        - recreated_reason is set if the worktree was deleted (for branch_on_recreate handling)
    """
    existing_worktree = find_worktree_for_branch(repo_root, branch_name)
    if not existing_worktree or not existing_worktree.exists():
        return (None, None)

    logger.info(issue_log(issue_number, "Reusing existing worktree: branch=%s path=%s"), branch_name, existing_worktree)

    result = _try_reuse_worktree(
        existing_worktree,
        branch_name,
        repo_root,
        issue_number,
        policy,
        reuse_options.reuse_push_preflight,
        reuse_options.allow_no_verify_dry_run_preflight,
        base_branch,
        preserve_branch=reuse_options.preserve_branch,
    )

    if not result.success:
        # Return the recreated_reason so create_worktree can handle branch_on_recreate
        return (None, result.recreated_reason)

    # Success - finalize and return
    runtime_setup.apply(existing_worktree)
    logger.info(issue_log(issue_number, "Worktree reuse complete: path=%s"), existing_worktree)
    reset_info = result.reset_info or ResetInfo(success=True)
    return (
        (
            existing_worktree,
            branch_name,
            "reused",
            "existing_worktree_by_branch",
            False,
            reset_info.uncommitted_discarded,
            reset_info.commits_discarded,
        ),
        None,
    )


def _try_reuse_by_path(
    worktree_path: Path,
    repo_root: Path,
    issue_number: int,
    policy: WorktreePolicy,
    reuse_options: WorktreeReuseOptions,
    runtime_setup: WorktreeRuntimeSetup,
    base_branch: str | None,
) -> tuple[tuple[Path, str, str, str | None, bool, int, int] | None, str | None]:
    """Try to reuse an existing worktree by path.

    Returns:
        Tuple of (result, recreated_reason) where:
        - result is the full result tuple if successful, None if failed
        - recreated_reason is set if the worktree was deleted (for branch_on_recreate handling)
    """
    logger.info(issue_log(issue_number, "Reusing existing worktree by path: %s"), worktree_path)

    # Validate first (no expected branch - we use whatever is there)
    validation = policy.validate_for_reuse(worktree_path, None, repo_root)
    if not validation.can_reuse:
        logger.warning(
            issue_log(issue_number, "Worktree failed validation, deleting: %s"),
            validation.reason,
        )
        policy.delete_worktree(worktree_path, repo_root)
        return (None, f"validation_failed: {validation.reason}")

    # Get current branch
    branch_result = _git_run(
        worktree_path,
        ["rev-parse", "--abbrev-ref", "HEAD"],
        check=False,
    )
    if branch_result.returncode != 0:
        logger.warning(issue_log(issue_number, "Could not get branch, deleting worktree"))
        policy.delete_worktree(worktree_path, repo_root)
        return (None, "validation_failed: could not determine branch")

    existing_branch = branch_result.stdout.strip()
    logger.info(issue_log(issue_number, "Existing worktree branch: %s"), existing_branch)

    result = _try_reuse_worktree(
        worktree_path,
        existing_branch,
        repo_root,
        issue_number,
        policy,
        reuse_options.reuse_push_preflight,
        reuse_options.allow_no_verify_dry_run_preflight,
        base_branch,
        preserve_branch=reuse_options.preserve_branch,
    )

    if not result.success:
        # Return the recreated_reason so create_worktree can handle branch_on_recreate
        return (None, result.recreated_reason)

    # Success - finalize and return
    runtime_setup.apply(worktree_path)
    logger.info(issue_log(issue_number, "Worktree reuse complete: path=%s"), worktree_path)
    reset_info = result.reset_info or ResetInfo(success=True)
    return (
        (
            worktree_path,
            existing_branch,
            "reused",
            "existing_worktree_by_path",
            False,
            reset_info.uncommitted_discarded,
            reset_info.commits_discarded,
        ),
        None,
    )


def _create_fresh_worktree(
    repo_root: Path,
    worktree_path: Path,
    branch_name: str,
    base_branch: str | None,
    seed_ref: str | None,
    issue_number: int,
    runtime_setup: WorktreeRuntimeSetup,
    recreated_reason: str | None,
) -> tuple[Path, str, str, str | None, bool, int, int]:
    """Create a fresh worktree."""
    try:
        cmd = _build_worktree_add_command(
            repo_root,
            worktree_path,
            branch_name,
            base_branch,
            seed_ref,
        )

        logger.info(issue_log(issue_number, "Creating worktree: branch=%s path=%s"), branch_name, worktree_path)
        result = _git_run(repo_root, cmd, check=False)
        if result.returncode != 0 and _recover_stale_branch_worktree_registration(
            repo_root=repo_root,
            issue_number=issue_number,
            branch_name=branch_name,
            stderr=result.stderr or "",
        ):
            logger.info(issue_log(issue_number, "Retrying worktree create after prune: branch=%s"), branch_name)
            result = _git_run(repo_root, cmd, check=False)

        if result.returncode != 0:
            logger.error(
                issue_log(issue_number, "Worktree creation FAILED: branch=%s error=%s"),
                branch_name,
                result.stderr.strip(),
            )
            raise WorktreeError(f"Failed to create worktree: {result.stderr}")

        runtime_setup.apply(worktree_path)

        logger.info(issue_log(issue_number, "Worktree created: branch=%s path=%s"), branch_name, worktree_path)
        reuse_status = "recreated" if recreated_reason else "created"
        reuse_reason = recreated_reason or "no_existing_worktree"
        return worktree_path, branch_name, reuse_status, reuse_reason, False, 0, 0

    except WorktreeError:
        raise
    except Exception as e:
        raise WorktreeError(f"Error creating worktree: {e}")


def _recover_stale_branch_worktree_registration(
    repo_root: Path,
    issue_number: int,
    branch_name: str,
    stderr: str,
) -> bool:
    """Prune stale git worktree metadata when branch is bound to a missing path."""
    match = _BRANCH_IN_USE_BY_WORKTREE_RE.search(stderr)
    if not match:
        return False
    conflict_branch = match.group(1)
    conflict_path = Path(match.group(2))
    if conflict_branch != branch_name:
        return False
    if conflict_path.exists():
        return False

    logger.warning(
        issue_log(
            issue_number,
            "Detected stale worktree registration for branch=%s at missing path=%s; pruning",
        ),
        branch_name,
        conflict_path,
    )
    prune_result = _git_run(repo_root, ["worktree", "prune"], check=False)
    if prune_result.returncode != 0:
        logger.warning(
            issue_log(issue_number, "Failed to prune stale worktree registration: %s"),
            (prune_result.stderr or "").strip(),
        )
        return False
    return True


def _remove_worktree_path(repo_root: Path, worktree_path: Path, *, force: bool) -> None:
    cmd = ["worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(worktree_path))
    result = _git_run(
        repo_root,
        cmd,
        check=False,
    )

    if result.returncode == 0:
        return
    if not force:
        raise WorktreeError(f"Failed to remove worktree: {result.stderr}")
    logger.warning(
        "Forced worktree removal via git failed; deleting directory: path=%s stderr=%s",
        worktree_path,
        result.stderr.strip(),
    )
    _force_delete_worktree_path(worktree_path)


def _force_delete_worktree_path(worktree_path: Path) -> None:
    if worktree_path.is_dir() and not worktree_path.is_symlink():
        shutil.rmtree(worktree_path, ignore_errors=True)
        return
    try:
        worktree_path.unlink()
    except FileNotFoundError:
        return


def _delete_worktree_branch(repo_root: Path, branch_name: str | None) -> None:
    if not branch_name:
        return
    # Branch deletion is best effort; stale branch cleanup should not mask a
    # successfully removed worktree.
    _git_run(
        repo_root,
        ["branch", "-D", branch_name],
        check=False,
    )


def remove_worktree(
    worktree_path: Path,
    *,
    force: bool = False,
    delete_branch: bool = True,
) -> None:
    """
    Remove a git worktree and optionally its associated branch.

    Args:
        worktree_path: Path to the worktree to remove
        force: If true, use ``git worktree remove --force`` and fallback to
            deleting the directory when git cannot remove it cleanly.
        delete_branch: Whether to delete the associated local branch after the
            checkout is removed. Retention cleanup passes false; disposable
            scratch/reset owners pass true.

    Raises:
        WorktreeError: If removal fails
    """
    worktree_path = Path(worktree_path)
    logger.info("Removing worktree: path=%s", worktree_path)

    if not worktree_path.exists():
        if force:
            # Idempotent for disposable/forced removal (#6824 R3): ``force`` means
            # "discard this local worktree", and an already-absent path already
            # satisfies that. Raising here would make a partial-success retry
            # (removal succeeded, a later step failed → cleanup re-planned) loop
            # forever on the now-missing path. The non-forced coding-worktree path
            # keeps raising so an unexpected disappearance is still surfaced.
            logger.info("Worktree already absent; forced removal is a no-op: path=%s", worktree_path)
            return
        raise WorktreeError(f"Worktree does not exist at {worktree_path}")

    try:
        repo_root = _resolve_repo_root_from_worktree(worktree_path)
        if repo_root is None:
            if force:
                logger.warning(
                    "Forced worktree removal cannot resolve repo root; deleting path directly: %s",
                    worktree_path,
                )
                _force_delete_worktree_path(worktree_path)
                if worktree_path.exists():
                    raise WorktreeError(
                        f"Failed to remove orphaned worktree path after forced cleanup: {worktree_path}"
                    )
                logger.info("Orphaned worktree path removed: path=%s", worktree_path)
                return
            raise WorktreeError(f"Unable to resolve repo root for {worktree_path}")
        branch_name = get_worktree_branch(worktree_path)

        _remove_worktree_path(repo_root, worktree_path, force=force)
        if worktree_path.exists():
            raise WorktreeError(
                f"Failed to remove worktree path after git/rmtree cleanup: {worktree_path}"
            )

        if delete_branch:
            _delete_worktree_branch(repo_root, branch_name)
        logger.info(
            "Worktree removed: path=%s branch=%s",
            worktree_path,
            branch_name or "(unknown)",
        )

    except Exception as e:
        if isinstance(e, WorktreeError):
            raise
        raise WorktreeError(f"Error removing worktree: {e}")


def list_worktrees(repo_root: Path) -> list[Path]:
    """
    List all git worktree paths.

    Returns:
        List of paths to all worktrees

    Raises:
        WorktreeError: If listing fails
    """
    try:
        cmd = ["worktree", "list", "--porcelain"]
        result = _git_run(
            repo_root,
            cmd,
            check=False,
        )

        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to list worktrees: {result.stderr}"
            )

        worktrees = []
        for line in result.stdout.strip().split("\n"):
            if line.startswith("worktree "):
                path_str = line.split(maxsplit=1)[1]
                worktrees.append(Path(path_str))

        return worktrees

    except Exception as e:
        if isinstance(e, WorktreeError):
            raise
        raise WorktreeError(f"Error listing worktrees: {e}")


def _parse_registered_worktree(record: str) -> RegisteredWorktree | None:
    """Parse one blank-line-delimited porcelain record."""
    fields: dict[str, object] = {}
    for line in record.splitlines():
        if line.startswith("worktree "):
            fields["path"] = Path(line.removeprefix("worktree "))
        elif line.startswith("HEAD "):
            fields["head"] = line.removeprefix("HEAD ")
        elif line.startswith("branch refs/heads/"):
            fields["branch"] = line.removeprefix("branch refs/heads/")
        elif line == "detached":
            fields["branch"] = None
        elif line == "locked" or line.startswith("locked "):
            fields["locked"] = True
        elif line == "prunable" or line.startswith("prunable "):
            fields["prunable"] = True

    path = fields.get("path")
    head = fields.get("head")
    if not isinstance(path, Path) or not isinstance(head, str):
        return None
    branch = fields.get("branch")
    return RegisteredWorktree(
        path=path,
        head=head,
        branch=branch if isinstance(branch, str) else None,
        locked=bool(fields.get("locked", False)),
        prunable=bool(fields.get("prunable", False)),
    )


def parse_registered_worktrees(output: str) -> tuple[RegisteredWorktree, ...]:
    """Parse ``git worktree list --porcelain`` without guessing from folders."""
    parsed = (
        _parse_registered_worktree(record)
        for record in output.strip().split("\n\n")
        if record.strip()
    )
    return tuple(item for item in parsed if item is not None)


def list_registered_worktrees(repo_root: Path) -> tuple[RegisteredWorktree, ...]:
    """List registered worktrees with branch and safety metadata."""
    result = _git_run(repo_root, ["worktree", "list", "--porcelain"], check=False)
    if result.returncode != 0:
        raise WorktreeError(f"Failed to list worktrees: {result.stderr}")
    return parse_registered_worktrees(result.stdout)


def worktree_exists(worktree_path: Path, repo_root: Path) -> bool:
    """
    Check if a worktree exists.

    Args:
        worktree_path: Path to check

    Returns:
        True if the worktree exists, False otherwise

    Raises:
        WorktreeError: If the check fails
    """
    try:
        worktrees = list_worktrees(repo_root)
        worktree_path = Path(worktree_path)
        return worktree_path in worktrees

    except Exception as e:
        if isinstance(e, WorktreeError):
            raise
        raise WorktreeError(f"Error checking worktree existence: {e}")


def has_uncommitted_changes(worktree_path: Path) -> bool:
    """
    Check if a worktree has uncommitted changes.

    Args:
        worktree_path: Path to the worktree

    Returns:
        True if there are uncommitted changes, False otherwise

    Raises:
        WorktreeError: If the check fails
    """
    worktree_path = Path(worktree_path)

    if not worktree_path.exists():
        raise WorktreeError(f"Worktree does not exist at {worktree_path}")

    try:
        cmd = ["status", "--porcelain"]
        result = _git_run(
            worktree_path,
            cmd,
            check=False,
        )

        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to check worktree status: {result.stderr}"
            )

        # If output is empty, there are no uncommitted changes
        return bool(result.stdout.strip())

    except Exception as e:
        if isinstance(e, WorktreeError):
            raise
        raise WorktreeError(f"Error checking uncommitted changes: {e}")


def can_remove_without_user_changes(worktree_path: Path) -> bool:
    """Return true when forced removal would not discard user source changes."""
    worktree_path = Path(worktree_path)

    if not worktree_path.exists():
        raise WorktreeError(f"Worktree does not exist at {worktree_path}")

    try:
        result = _git_run(
            worktree_path,
            ["status", "--porcelain", "--untracked-files=all"],
            check=False,
        )

        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to check worktree status: {result.stderr}"
            )

        for line in result.stdout.splitlines():
            if line.startswith("?? "):
                if not is_cleanup_safe_untracked_path(line[3:], worktree_path):
                    return False
                continue
            return False
        return True

    except Exception as e:
        if isinstance(e, WorktreeError):
            raise
        raise WorktreeError(f"Error checking worktree removal safety: {e}")


def get_worktree_branch(worktree_path: Path) -> str | None:
    """
    Get the branch name for a worktree.

    Args:
        worktree_path: Path to the worktree

    Returns:
        Branch name or None if it cannot be determined

    Raises:
        WorktreeError: If the operation fails
    """
    try:
        cmd = ["rev-parse", "--abbrev-ref", "HEAD"]
        result = _git_run(
            worktree_path,
            cmd,
            check=False,
        )

        if result.returncode != 0:
            return None

        return result.stdout.strip() or None

    except Exception:
        return None
