"""Repository-aware worktree path policy."""

from __future__ import annotations

from pathlib import Path

WORKTREE_COLLECTION_DIR = "worktrees"


def default_worktree_base_config(repo_root: Path) -> str:
    """Return the portable default worktree-base value for one repository."""
    return str(Path("..") / WORKTREE_COLLECTION_DIR / repo_root.name)


def default_worktree_base(repo_root: Path) -> Path:
    """Return the resolved default base for issue worktrees."""
    return resolve_worktree_base(None, repo_root)


def resolve_worktree_base(path: str | Path | None, repo_root: Path) -> Path:
    """Resolve an explicit worktree base or the repository-aware default."""
    target = (
        Path(default_worktree_base_config(repo_root))
        if path is None
        else Path(path)
    )
    if target.is_absolute():
        return target.resolve()
    return (repo_root / target).resolve()


__all__ = [
    "WORKTREE_COLLECTION_DIR",
    "default_worktree_base",
    "default_worktree_base_config",
    "resolve_worktree_base",
]
