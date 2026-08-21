"""Manage a persistent git worktree for E2E test isolation.

E2E tests run from their own worktree so fixtures that delete
``.issue-orchestrator/state/`` cannot destroy the live orchestrator's
SQLite files.
"""

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def get_e2e_worktree_path(repo_root: Path) -> Path:
    """Derive the deterministic sibling path for the E2E worktree.

    Convention mirrors CLAUDE.md worktree naming:
    ``<repo_root>/../<repo_name>-e2e-worktree/``
    """
    return repo_root.parent / f"{repo_root.name}-e2e-worktree"


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command, raising on failure."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )


def _resolve_e2e_ref(repo_root: Path) -> str:
    """Determine which git ref the E2E worktree should check out.

    When the orchestrator runs from a worktree or feature branch, e2e tests
    should run against that code — not origin/main.  This lets developers
    iterate on e2e fixes without merging to main first.
    """
    try:
        result = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        logger.warning("Could not resolve HEAD, falling back to origin/main")
        return "origin/main"


def _create_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Create a new detached worktree at *worktree_path*."""
    ref = _resolve_e2e_ref(repo_root)
    logger.info("Creating E2E worktree at %s (ref=%s)", worktree_path, ref[:12])
    _run_git(
        ["worktree", "add", "--detach", str(worktree_path), ref],
        cwd=repo_root,
    )


def _update_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Reset an existing worktree to the orchestrator's current commit.

    Preserves:
    - .venv (Python dependencies, expensive to recreate)
    - .issue-orchestrator/state/timeline.sqlite* (agent timeline events
      for E2E run nesting — snapshot reads from here at completion)
    - .issue-orchestrator/sessions (session artifacts: terminal recordings,
      validation records, review feedback — needed for rich timeline rendering)
    - .issue-orchestrator/e2e-results (run-scoped report artifacts used for
      lazy stdout/stderr retrieval in the dashboard)
    """
    ref = _resolve_e2e_ref(repo_root)
    logger.info("Updating E2E worktree at %s (ref=%s)", worktree_path, ref[:12])
    _run_git(["checkout", "-f", "--detach", ref], cwd=worktree_path)
    _run_git(
        [
            "clean", "-fdx",
            "--exclude=.venv",
            "--exclude=.issue-orchestrator/state/timeline.sqlite*",
            "--exclude=.issue-orchestrator/sessions",
            "--exclude=.issue-orchestrator/e2e-results",
        ],
        cwd=worktree_path,
    )


class VenvMutationRefused(RuntimeError):
    """Raised when the guard will not authorize mutating a worktree venv."""


def _require_owned_venv(worktree_path: Path) -> None:
    """Refuse to install this worktree's project into a venv it does not own.

    The E2E worktree is created by the orchestrator, and orchestrator worktrees
    have their ``.venv`` symlinked at the base repo's venv
    (``_link_repo_venv_into_worktree``). Syncing through that link reinstalls
    this worktree's project into the shared venv and rewrites its editable
    pointer, so this path is a first-class contamination vector, not an
    incidental one.

    Routed through ``scripts/venv_guard.sh`` -- the single mutation
    authorization owner -- rather than re-deriving the rule. Fails CLOSED: a
    missing guard or an unrecognised outcome is not evidence of ownership.
    """
    guard = worktree_path / "scripts" / "venv_guard.sh"
    outcome = (
        subprocess.run(
            [str(guard), "--quiet"], cwd=worktree_path, capture_output=True, timeout=30
        ).returncode
        if guard.exists()
        else 3
    )
    if outcome == 0:
        return
    reasons = {
        1: "its .venv is shared from another checkout",
        2: "its .venv is a dangling symlink",
    }
    raise VenvMutationRefused(
        f"Refusing to sync the E2E worktree venv at {worktree_path}: "
        + reasons.get(outcome, f"the venv guard was unavailable or returned {outcome}")
    )


def _sync_venv(worktree_path: Path) -> None:
    """Ensure the worktree venv is up-to-date (fast when deps unchanged)."""
    pyproject = worktree_path / "pyproject.toml"
    uv_lock = worktree_path / "uv.lock"
    venv_python = worktree_path / ".venv" / "bin" / "python"

    if pyproject.exists():
        _require_owned_venv(worktree_path)
        logger.info("Syncing project venv in E2E worktree")
        sync_cmd = ["uv", "sync", "--all-extras"]
        if uv_lock.exists():
            sync_cmd.insert(2, "--frozen")
        else:
            logger.info(
                "No uv.lock in E2E worktree; resolving dependencies without --frozen"
            )
        subprocess.run(
            sync_cmd,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        # The fallback install is intentionally all-or-nothing: a partial
        # project environment is not a usable E2E worker environment.
        pytest_check = subprocess.run(
            [str(venv_python), "-c", "import defusedxml, pytest"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if pytest_check.returncode != 0:
            logger.info(
                "E2E worker dependencies not available in synced worktree; installing fallbacks"
            )
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(venv_python),
                    "defusedxml>=0.7",
                    "pytest>=8.0",
                ],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=300,
                check=True,
            )
        return

    logger.info("No pyproject.toml in E2E worktree; preparing minimal pytest venv")
    subprocess.run(
        ["uv", "venv", ".venv"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(worktree_path / ".venv" / "bin" / "python"),
            "defusedxml>=0.7",
            "pytest>=8.0",
        ],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )


def _recover_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Remove a broken worktree and recreate it."""
    logger.warning("Recovering E2E worktree at %s", worktree_path)
    try:
        _run_git(
            ["worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_root,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # The directory may still exist on disk if it was never a registered
    # worktree (e.g. partial cleanup or corruption).  Remove it so that
    # ``git worktree add`` doesn't fail with "already exists".
    if worktree_path.exists():
        logger.warning("Removing stale directory %s", worktree_path)
        shutil.rmtree(worktree_path)
    _create_worktree(repo_root, worktree_path)


def ensure_e2e_worktree(repo_root: Path) -> Path:
    """Return a ready-to-use E2E worktree, creating or updating as needed.

    The worktree is a sibling directory checked out at the orchestrator's
    current HEAD commit (so e2e tests run against the code that's actually
    running).  On failure a ``RuntimeError`` is raised (fail-fast per
    codebase design).

    Returns:
        Resolved ``Path`` to the worktree root.
    """
    worktree_path = get_e2e_worktree_path(repo_root)

    try:
        if worktree_path.exists():
            try:
                _update_worktree(repo_root, worktree_path)
            except subprocess.CalledProcessError:
                _recover_worktree(repo_root, worktree_path)
        else:
            _create_worktree(repo_root, worktree_path)

        _sync_venv(worktree_path)

    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Failed to prepare E2E worktree at {worktree_path}: {exc.stderr}"
        ) from exc
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Required tool not found while preparing E2E worktree: {exc}"
        ) from exc

    logger.info("E2E worktree ready at %s", worktree_path)
    return worktree_path
