"""Manage a persistent git worktree for E2E test isolation.

E2E tests run from their own worktree so fixtures that delete
``.issue-orchestrator/state/`` cannot destroy the live orchestrator's
SQLite files.
"""

import logging
import shutil
import subprocess
from pathlib import Path

from ..ports.command_runner import CommandRunner, CommandResult
from .venv_mutation import VenvMutationAuthority, VenvOperation

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


class _SubprocessCommandRunner:
    """Minimal ``CommandRunner`` for this module's own use.

    This module manages external processes directly (it is a worktree/venv
    provisioner), so it already owns subprocess calls. It depends on the
    *port* rather than importing ``execution``'s adapter, because
    ``infra.orchestrator`` reaches this module and the layering contract
    forbids orchestrator -> execution. Callers and tests inject their own
    runner; this is only the default when none is supplied.
    """

    def run(
        self,
        command,
        *,
        cwd=None,
        env=None,
        timeout_seconds=None,
        shell=False,
        newlines=None,
    ) -> CommandResult:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            timeout=timeout_seconds,
            shell=shell,
            capture_output=True,
            text=True,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            timed_out=False,
        )


def _sync_venv(worktree_path: Path, runner: CommandRunner | None = None) -> None:
    """Ensure the worktree venv is up-to-date (fast when deps unchanged).

    Authorization happens ONCE, above the branch. Both paths mutate
    ``<worktree>/.venv`` -- the project sync below and the no-pyproject fallback
    that runs ``uv venv`` -- and E2E worktrees are orchestrator worktrees whose
    ``.venv`` is symlinked at the owning repo's venv. Guarding only the branch
    that happens to contain a project left the other free to rebuild the shared
    environment through that link.
    """
    pyproject = worktree_path / "pyproject.toml"
    uv_lock = worktree_path / "uv.lock"
    venv_python = worktree_path / ".venv" / "bin" / "python"

    authority = VenvMutationAuthority(runner or _SubprocessCommandRunner())

    # The OPERATION is authorized, not merely the target. Authorizing the venv
    # alone accepted `shared` and then let the no-project branch below run
    # `uv venv`, rebuilding the owning checkout's environment through the
    # symlink: a shared venv permits dependency work, never recreation.
    operation = (
        VenvOperation.SYNC_DEPENDENCIES if pyproject.exists() else VenvOperation.RECREATE
    )
    decision = authority.authorize(checkout=worktree_path, operation=operation)
    uv_env = VenvMutationAuthority.pinned_env(decision)

    if pyproject.exists():
        logger.info("Syncing project venv in E2E worktree")
        # The arguments come from the decision, so a shared environment can
        # never receive this worktree's project install.
        sync_cmd = ["uv", "sync", *decision.sync_args]
        if not uv_lock.exists():
            logger.info(
                "No uv.lock in E2E worktree; resolving dependencies without --frozen"
            )
            sync_cmd = [arg for arg in sync_cmd if arg != "--frozen"]

        subprocess.run(
            sync_cmd,
            cwd=worktree_path,
            env=uv_env,
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
            env=uv_env,
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
            env=uv_env,
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
        env=uv_env,
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
        env=uv_env,
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


def ensure_e2e_worktree(
    repo_root: Path, runner: CommandRunner | None = None
) -> Path:
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

        _sync_venv(worktree_path, runner)

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
