# pyright: strict
"""Fail-fast Git repository identity resolution for executor work."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ._types import ExecutorRepositoryIdentity


class ExecutorRepositoryResolver:
    """Resolve one stable identity shared by all Git worktrees."""

    def resolve(self, working_directory: Path) -> ExecutorRepositoryIdentity:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(working_directory),
                "rev-parse",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        common_text = result.stdout.strip()
        if result.returncode != 0 or not common_text:
            detail = result.stderr.strip() or "git returned no common directory"
            raise RuntimeError(
                f"executor work must run inside a Git repository: {detail}"
            )
        common_directory = Path(common_text)
        if not common_directory.is_absolute():
            common_directory = working_directory / common_directory
        common_directory = common_directory.resolve()
        label_path = (
            common_directory.parent
            if common_directory.name == ".git"
            else common_directory
        )
        return ExecutorRepositoryIdentity(
            common_directory=common_directory,
            label=label_path.name,
        )
