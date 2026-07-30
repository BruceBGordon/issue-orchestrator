#!/usr/bin/env python3
"""Create and initialize a development worktree in one command."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


class WorktreeCreationError(RuntimeError):
    """The requested development worktree could not be created."""


def _run_capture(command: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        command,
        check=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _primary_worktree(repo_root: Path) -> Path:
    common_git_dir = _run_capture(
        [
            "git",
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        cwd=repo_root,
    )
    git_dir = Path(common_git_dir)
    if git_dir.name != ".git":
        raise WorktreeCreationError(
            f"Unsupported Git layout: common directory is {git_dir}, not a .git directory"
        )
    return git_dir.parent.resolve()


def _default_worktree_path(primary_worktree: Path, branch: str) -> Path:
    branch_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")
    if not branch_slug:
        raise WorktreeCreationError(
            f"Branch {branch!r} does not produce a usable worktree directory name"
        )
    return primary_worktree.parent / f"{primary_worktree.name}-wt-{branch_slug}"


def _validate_request(
    *,
    source_worktree: Path,
    branch: str,
    base_ref: str,
    worktree_path: Path,
) -> None:
    subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        check=True,
        cwd=source_worktree,
        stdout=subprocess.DEVNULL,
    )

    base_check = subprocess.run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{base_ref}^{{commit}}",
        ],
        cwd=source_worktree,
        stdout=subprocess.DEVNULL,
    )
    if base_check.returncode != 0:
        raise WorktreeCreationError(
            f"Base ref {base_ref!r} does not resolve to a commit"
        )

    branch_check = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=source_worktree,
    )
    if branch_check.returncode == 0:
        raise WorktreeCreationError(f"Local branch {branch!r} already exists")

    if worktree_path.exists():
        raise WorktreeCreationError(f"Worktree path already exists: {worktree_path}")


def create_dev_worktree(
    *,
    repo_root: Path,
    branch: str,
    base_ref: str = "HEAD",
    worktree_path: Path | None = None,
    make_command: str = "make",
) -> Path:
    """Create a sibling worktree and run its complete setup target."""
    if not branch:
        raise WorktreeCreationError(
            "BRANCH is required; use `make worktree-create BRANCH=my-branch`"
        )

    source_root = repo_root.resolve()
    primary_worktree = _primary_worktree(source_root)
    target = (
        worktree_path.resolve()
        if worktree_path is not None
        else _default_worktree_path(primary_worktree, branch)
    )
    _validate_request(
        source_worktree=source_root,
        branch=branch,
        base_ref=base_ref,
        worktree_path=target,
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"Creating worktree {target} from {base_ref} on branch {branch}...",
        flush=True,
    )
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            str(target),
            "-b",
            branch,
            base_ref,
        ],
        check=True,
        cwd=source_root,
    )

    print(f"Running complete setup in {target}...", flush=True)
    try:
        subprocess.run(
            [make_command, "-C", str(target), "worktree-setup"],
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        print(
            "\nWorktree creation succeeded, but setup failed. "
            "The worktree was preserved.\n"
            f"Retry with: {make_command} -C {target} worktree-setup",
            file=sys.stderr,
        )
        raise

    print(f"\nWorktree ready: {target}")
    print(f"Activate with: source {target}/.venv/bin/activate")
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and fully initialize a development worktree."
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--base-ref", default="HEAD")
    parser.add_argument(
        "--path",
        help="Target path; defaults to ../<repo>-wt-<branch>",
    )
    parser.add_argument("--make", dest="make_command", default="make")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        create_dev_worktree(
            repo_root=args.repo_root,
            branch=args.branch,
            base_ref=args.base_ref,
            worktree_path=Path(args.path) if args.path else None,
            make_command=args.make_command,
        )
    except WorktreeCreationError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except FileNotFoundError as error:
        print(f"Error: command not found: {error.filename}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        return error.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
