"""Real-Git worktree helpers for worktree adapter tests.

Hook installation can only be judged against a real repository. The guardrail
counts as installed when Git's *effective* ``core.hooksPath`` for the worktree
is the directory holding the hook — a hand-built ``.git`` directory can never
answer that question, and a test built on one proves only that a file was
copied somewhere. Tests that assert on installation outcomes therefore need a
genuine ``git init`` + ``git worktree add``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GitWorktree",
    "block_worktree_config_writes",
    "effective_hooks_path",
    "make_git_worktree",
]


@dataclass(frozen=True)
class GitWorktree:
    """A real repository plus one linked worktree, and where its hooks live."""

    main_repo: Path
    worktree_path: Path
    gitdir: Path
    hooks_dir: Path


def _git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True)


def make_git_worktree(
    tmp_path: Path, *, name: str = "wt-feature", branch: str = "feature"
) -> GitWorktree:
    """Create ``tmp_path/main`` with one seed commit and a linked worktree."""
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git("init", cwd=main_repo)
    _git("config", "user.email", "t@example.com", cwd=main_repo)
    _git("config", "user.name", "T", cwd=main_repo)
    (main_repo / "seed").write_text("seed\n")
    _git("add", "seed", cwd=main_repo)
    _git("commit", "-m", "seed", cwd=main_repo)

    worktree_path = tmp_path / name
    _git("worktree", "add", str(worktree_path), "-b", branch, cwd=main_repo)

    gitdir = main_repo / ".git" / "worktrees" / name
    return GitWorktree(
        main_repo=main_repo,
        worktree_path=worktree_path,
        gitdir=gitdir,
        hooks_dir=gitdir / "hooks",
    )


def effective_hooks_path(worktree_path: Path) -> str:
    """Return the hooks directory Git will actually use for this worktree.

    Empty when ``core.hooksPath`` is unset, which is itself a meaningful answer:
    Git falls back to ``$GIT_DIR/hooks`` and our worktree hooks directory is not
    in play.
    """
    result = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def block_worktree_config_writes(gitdir: Path) -> None:
    """Make every ``git config --worktree`` write fail, deterministically.

    Git takes ``config.worktree.lock`` before writing, so a pre-existing lock
    file turns each worktree config write into a nonzero exit. Unlike ``chmod``
    this also holds when the test runner is root, and it leaves the repository
    otherwise intact so the failure is isolated to the config write.
    """
    gitdir.mkdir(parents=True, exist_ok=True)
    (gitdir / "config.worktree.lock").write_text("held by test\n")
