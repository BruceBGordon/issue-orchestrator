from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ...ports.command_runner import CommandRunner, CommandResult, OutputNewlines
from ...ports.git import Git, GitError, GitResult


GIT_ENV_STRIP = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)


class SubprocessCommandRunner:
    """Minimal command runner to avoid importing execution layer from adapters."""

    def run(
        self,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        shell: bool = False,
        newlines: OutputNewlines = OutputNewlines.TRANSLATED,
    ) -> CommandResult:
        try:
            result = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                # Text mode is where universal-newline translation happens, so
                # a byte-exact capture has to decode the output itself.
                text=not newlines.capture_bytes,
                timeout=timeout_seconds,
                env=env,
                shell=shell,
            )
            return CommandResult(
                returncode=result.returncode,
                stdout=newlines.decode(result.stdout),
                stderr=newlines.decode(result.stderr),
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            # A timed-out capture can end mid-character, and its output is only
            # ever diagnostic, so replace here instead of losing the result.
            return CommandResult(
                returncode=-1,
                stdout=newlines.decode(exc.stdout, errors="replace"),
                stderr=newlines.decode(exc.stderr, errors="replace"),
                timed_out=True,
            )
        except FileNotFoundError as exc:
            return CommandResult(
                returncode=127,
                stdout="",
                stderr=str(exc),
                timed_out=False,
            )


@dataclass
class GitCLI(Git):
    """Git implementation that shells out via CommandRunner (no shell)."""

    runner: CommandRunner
    default_timeout_s: int = 30

    def clean_env(self) -> dict[str, str]:
        """Return a copy of the environment with git-specific vars stripped."""
        env = dict(os.environ)
        for var in GIT_ENV_STRIP:
            env.pop(var, None)
        return env

    def run(
        self,
        repo: Path,
        argv: list[str],
        *,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
        newlines: OutputNewlines = OutputNewlines.TRANSLATED,
    ) -> GitResult:
        cmd = ["git", "-C", str(repo)] + argv
        result = self.runner.run(
            cmd,
            cwd=None,
            env=env if env is not None else self.clean_env(),
            timeout_seconds=timeout_s or self.default_timeout_s,
            shell=False,
            newlines=newlines,
        )
        git_result = GitResult(
            argv=cmd,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        if result.timed_out:
            raise GitError(git_result, message="git command timed out")
        if check and git_result.returncode != 0:
            raise GitError(git_result)
        return git_result

    def status_porcelain(self, repo: Path) -> str:
        return self.run(repo, ["status", "--porcelain"]).stdout

    def current_branch(self, repo: Path) -> str:
        return self.run(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    def head_sha(self, repo: Path) -> str:
        return self.run(repo, ["rev-parse", "HEAD"]).stdout.strip()

    def branch_exists(self, repo: Path, branch: str) -> bool:
        result = self.run(repo, ["rev-parse", "--verify", branch], check=False)
        return result.returncode == 0

    def default_branch(self, repo: Path, remote: str = "origin") -> str:
        result = self.run(
            repo,
            ["symbolic-ref", f"refs/remotes/{remote}/HEAD"],
            check=False,
        )
        if result.returncode == 0:
            ref = result.stdout.strip()
            return ref.split("/")[-1]
        if self.branch_exists(repo, "main"):
            return "main"
        if self.branch_exists(repo, "master"):
            return "master"
        return "main"

    def fetch(self, repo: Path, remote: str = "origin", ref: str | None = None) -> None:
        argv = ["fetch", remote]
        if ref:
            argv.append(ref)
        self.run(repo, argv)

    def checkout_new_branch(self, repo: Path, branch: str, base_ref: str) -> None:
        self.run(repo, ["checkout", "-B", branch, base_ref])

    def worktree_add(self, repo: Path, path: Path, branch: str) -> None:
        self.run(repo, ["worktree", "add", str(path), branch])

    def worktree_remove(self, repo: Path, path: Path, force: bool = True, prune: bool = True) -> None:
        argv = ["worktree", "remove"]
        if force:
            argv.append("--force")
        argv.append(str(path))
        self.run(repo, argv, check=False)
        if prune:
            self.run(repo, ["worktree", "prune"], check=False)

    def commit(self, repo: Path, message: str) -> None:
        self.run(repo, ["commit", "-am", message])

    def rebase(self, repo: Path, target: str) -> GitResult:
        return self.run(repo, ["rebase", target])

    def rebase_abort(self, repo: Path) -> GitResult:
        return self.run(repo, ["rebase", "--abort"], check=False)

    def push(
        self,
        repo: Path,
        remote: str,
        branch: str,
        *,
        set_upstream: bool = True,
        force_with_lease: bool = False,
        skip_hooks: bool = False,
    ) -> None:
        argv = ["push"]
        if skip_hooks:
            argv.append("--no-verify")
        if set_upstream:
            argv.extend(["-u", remote, branch])
        else:
            argv.append(remote)
            argv.append(branch)
        if force_with_lease:
            argv.append("--force-with-lease")
        self.run(repo, argv)
