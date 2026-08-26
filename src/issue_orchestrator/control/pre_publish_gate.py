"""Pre-publish gate that reuses the worktree's effective pre-push hook.

This gate exists to catch push-time policy failures before the orchestrator
attempts the real authenticated push. The real push still keeps hooks enabled;
the later hook pass is expected to be cheap because the validation command is
cache-aware on the same commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..control.isolation import build_runtime_tool_env
from ..ports.command_runner import CommandRunner


@dataclass(frozen=True)
class PrePublishGateResult:
    allowed: bool
    reason: str
    command: str
    started_at: str
    ended_at: str
    exit_code: int
    stdout: str
    stderr: str
    hook_path: str
    head_sha: str | None = None
    ran: bool = True


@dataclass(frozen=True)
class _ResolvedHookPath:
    path: Path | None
    error: str | None = None
    exit_code: int = 0
    stderr: str = ""


class PrePublishGate:
    """Run the worktree's effective pre-push hook before publish."""

    def __init__(self, command_runner: CommandRunner) -> None:
        self._command_runner = command_runner

    def check(self, worktree: Path) -> PrePublishGateResult:
        started_at = datetime.now(timezone.utc)
        resolved_hook = self._resolve_hook_path(worktree)
        if resolved_hook.path is None:
            ended_at = datetime.now(timezone.utc)
            return PrePublishGateResult(
                allowed=False,
                reason=resolved_hook.error or "Failed to resolve pre-push hook",
                command="pre-push hook",
                started_at=started_at.isoformat(),
                ended_at=ended_at.isoformat(),
                exit_code=resolved_hook.exit_code or 1,
                stdout="",
                stderr=resolved_hook.stderr,
                hook_path="",
                head_sha=self._resolve_head_sha(worktree),
                ran=False,
            )
        hook_path = resolved_hook.path

        result = self._command_runner.run(
            [str(hook_path), "origin", "origin"],
            cwd=worktree,
            env=build_runtime_tool_env(worktree),
        )
        ended_at = datetime.now(timezone.utc)
        summary = (
            "Pre-push hook passed"
            if result.returncode == 0
            else self._summarize_failure_output(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
            )
        )
        return PrePublishGateResult(
            allowed=result.returncode == 0,
            reason=summary,
            command=str(hook_path),
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            hook_path=str(hook_path),
            head_sha=self._resolve_head_sha(worktree),
        )

    def _resolve_hook_path(self, worktree: Path) -> _ResolvedHookPath:
        result = self._command_runner.run(
            ["git", "rev-parse", "--git-path", "hooks/pre-push"],
            cwd=worktree,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "git rev-parse failed").strip()
            return _ResolvedHookPath(
                path=None,
                error=f"Failed to resolve worktree pre-push hook: {detail}",
                exit_code=result.returncode,
                stderr=result.stderr,
            )
        raw_path = result.stdout.strip()
        if not raw_path:
            return _ResolvedHookPath(
                path=None,
                error="Failed to resolve worktree pre-push hook: git returned an empty hook path",
                exit_code=1,
            )
        hook_path = Path(raw_path)
        if not hook_path.is_absolute():
            hook_path = (worktree / hook_path).resolve()
        if not hook_path.exists() or not hook_path.is_file():
            return _ResolvedHookPath(
                path=None,
                error=f"Resolved pre-push hook does not exist: {hook_path}",
                exit_code=1,
            )
        return _ResolvedHookPath(path=hook_path)

    def _resolve_head_sha(self, worktree: Path) -> str | None:
        result = self._command_runner.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha or None

    @staticmethod
    def _summarize_failure_output(
        *,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> str:
        for stream in (stderr, stdout):
            for raw_line in stream.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("[orchestrator]"):
                    continue
                if set(line) <= {"═", "╔", "╗", "╚", "╝", "╠", "╣", "║", "╬"}:
                    continue
                return line[:300]
        return f"Pre-push hook failed with exit code {exit_code}"
