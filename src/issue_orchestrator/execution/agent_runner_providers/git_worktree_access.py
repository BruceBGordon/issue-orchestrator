"""Resolve the exact Git metadata a sandboxed worktree may update."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from issue_orchestrator.domain.sandbox_scope import SandboxUnsupportedError

__all__ = [
    "GitWorktreeAccess",
    "git_worktree_filesystem_rules",
    "resolve_git_worktree_access",
]


@dataclass(frozen=True)
class GitWorktreeAccess:
    """Git paths one worktree needs for status, staging, and commits."""

    git_dir: Path
    common_dir: Path
    head_ref: Path | None

    def __post_init__(self) -> None:
        for name, path in (
            ("git_dir", self.git_dir),
            ("common_dir", self.common_dir),
        ):
            if not path.is_absolute():
                raise SandboxUnsupportedError(
                    f"Agent sandbox {name} must be absolute (got {path})"
                )
        if not self.git_dir.is_relative_to(self.common_dir):
            raise SandboxUnsupportedError(
                "Agent sandbox worktree Git directory must stay inside the "
                f"common Git directory (got {self.git_dir})"
            )
        if self.head_ref is not None:
            self._validate_head_ref()

    def _validate_head_ref(self) -> None:
        assert self.head_ref is not None
        if not self.head_ref.is_absolute():
            raise SandboxUnsupportedError(
                f"Agent sandbox head_ref must be absolute (got {self.head_ref})"
            )
        if not self.head_ref.is_relative_to(self.common_dir):
            raise SandboxUnsupportedError(
                "Agent sandbox current branch ref must stay inside the "
                f"common Git directory (got {self.head_ref})"
            )


def _read_git_path_file(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SandboxUnsupportedError(
            f"Agent sandbox could not read {label} at {path}: {exc}"
        ) from exc


def _resolve_git_directory(worktree: Path) -> Path:
    marker = worktree / ".git"
    if marker.is_dir():
        return marker.resolve()
    if not marker.is_file():
        raise SandboxUnsupportedError(
            f"Agent sandbox working directory is not a Git worktree: {worktree}"
        )

    raw = _read_git_path_file(marker, label="linked-worktree .git pointer")
    prefix = "gitdir:"
    if not raw.lower().startswith(prefix):
        raise SandboxUnsupportedError(
            f"Agent sandbox found a malformed .git pointer at {marker}"
        )
    target = raw[len(prefix) :].strip()
    if not target:
        raise SandboxUnsupportedError(
            f"Agent sandbox found an empty .git pointer at {marker}"
        )
    git_dir = Path(target)
    if not git_dir.is_absolute():
        git_dir = marker.parent / git_dir
    git_dir = git_dir.resolve()
    if not git_dir.is_dir():
        raise SandboxUnsupportedError(
            f"Agent sandbox linked-worktree Git directory does not exist: {git_dir}"
        )
    return git_dir


def _resolve_common_directory(git_dir: Path) -> Path:
    marker = git_dir / "commondir"
    if not marker.exists():
        return git_dir
    raw = _read_git_path_file(marker, label="linked-worktree commondir pointer")
    if not raw:
        raise SandboxUnsupportedError(
            f"Agent sandbox found an empty commondir pointer at {marker}"
        )
    common_dir = Path(raw)
    if not common_dir.is_absolute():
        common_dir = git_dir / common_dir
    common_dir = common_dir.resolve()
    if not common_dir.is_dir():
        raise SandboxUnsupportedError(
            f"Agent sandbox common Git directory does not exist: {common_dir}"
        )
    return common_dir


def _resolve_head_ref(git_dir: Path, common_dir: Path) -> Path | None:
    raw_head = _read_git_path_file(git_dir / "HEAD", label="worktree HEAD")
    if not raw_head.startswith("ref:"):
        return None
    raw_ref = raw_head.removeprefix("ref:").strip()
    ref = PurePosixPath(raw_ref)
    if (
        not raw_ref
        or ref.is_absolute()
        or ".." in ref.parts
        or ref.parts[:2] != ("refs", "heads")
    ):
        raise SandboxUnsupportedError(
            "Agent sandbox only supports symbolic HEAD refs under refs/heads "
            f"(got {raw_ref!r})"
        )
    return common_dir.joinpath(*ref.parts)


def resolve_git_worktree_access(worktree: Path) -> GitWorktreeAccess:
    """Resolve minimal Git metadata paths for the current worktree."""
    git_dir = _resolve_git_directory(worktree)
    common_dir = _resolve_common_directory(git_dir)
    return GitWorktreeAccess(
        git_dir=git_dir,
        common_dir=common_dir,
        head_ref=_resolve_head_ref(git_dir, common_dir),
    )


def _with_lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _private_admin_rules(access: GitWorktreeAccess) -> list[tuple[str, str]]:
    entries = [(str(access.git_dir), "write")]
    protected_names = ["commondir", "gitdir", "config", "config.worktree"]
    if access.head_ref is not None:
        protected_names.insert(0, "HEAD")
    entries.extend((str(access.git_dir / name), "read") for name in protected_names)
    return entries


def _shared_admin_rules(access: GitWorktreeAccess) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for path in (
        access.git_dir / "index",
        access.git_dir / "COMMIT_EDITMSG",
        access.git_dir / "logs" / "HEAD",
    ):
        entries.extend(
            (
                (str(path), "write"),
                (str(_with_lock_path(path)), "write"),
            )
        )
    return entries


def _head_rules(access: GitWorktreeAccess) -> list[tuple[str, str]]:
    if access.head_ref is None:
        paths = (access.git_dir / "HEAD",)
    else:
        branch_log = (
            access.common_dir / "logs" / access.head_ref.relative_to(access.common_dir)
        )
        paths = (access.head_ref, branch_log)
    entries: list[tuple[str, str]] = []
    for path in paths:
        entries.extend(
            (
                (str(path), "write"),
                (str(_with_lock_path(path)), "write"),
            )
        )
    return entries


def git_worktree_filesystem_rules(
    access: GitWorktreeAccess,
) -> list[tuple[str, str]]:
    """Return exact shared Git access rules for status, staging, and commits."""
    entries: list[tuple[str, str]] = [(str(access.common_dir), "read")]
    entries.extend(
        _private_admin_rules(access)
        if access.git_dir != access.common_dir
        else _shared_admin_rules(access)
    )
    objects = access.common_dir / "objects"
    entries.extend(
        (
            (str(objects), "write"),
            (str(objects / "info"), "read"),
            (str(objects / "pack"), "read"),
        )
    )
    entries.extend(_head_rules(access))
    return entries
