"""Owner for the runtime setup sequence applied to orchestrator worktrees.

``_worktree.py`` owns worktree *lifecycle* decisions — reuse this path, rebase
that branch, recreate when validation fails. It must not also own what "ready
for an agent session" means. This module does: it holds the setup steps, their
order, and the failure semantics of each, so a policy change lands in exactly
one place instead of drifting across the three lifecycle paths (fresh create,
reuse by branch, reuse by path) that all need identical setup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ._worktree_hooks import install_hooks
from ._worktree_runtime import (
    _configure_no_verify_dry_run,
    _hide_runtime_artifacts_from_git_status,
    _install_worktree_identity,
    _link_repo_venv_into_worktree,
    install_claude_settings,
    sync_cli_tools,
)

logger = logging.getLogger(__name__)

__all__ = ["WorktreeRuntimeSetup", "WorktreeRuntimeState"]


@dataclass(frozen=True)
class WorktreeRuntimeState:
    """What runtime setup actually put in place for one worktree.

    Returned so callers can observe the outcome (and tests can assert on it)
    without re-deriving it from the filesystem or from the setup inputs.
    """

    worktree_path: Path
    worktree_id: str
    hooks_installed: bool
    no_verify_dry_run_allowed: bool
    synced_cli_tool_paths: tuple[Path, ...]


@dataclass(frozen=True)
class WorktreeRuntimeSetup:
    """Composes the runtime setup a worktree needs before an agent runs in it.

    Built once per ``create_worktree`` call from the caller's hook and preflight
    options, then applied to whichever worktree the lifecycle settles on.

    Args:
        repo_root: Repository the worktree belongs to; source of the shared venv.
        enforce_hooks: Whether guardrail git hooks are installed into the worktree.
        pre_push_hook: Custom pre-push hook to install instead of the bundled one.
        allow_no_verify_dry_run_preflight: Whether the worktree may use
            ``--no-verify`` for the reuse push preflight.
    """

    repo_root: Path
    enforce_hooks: bool = True
    pre_push_hook: Path | None = None
    allow_no_verify_dry_run_preflight: bool = False

    def apply(self, worktree_path: Path) -> WorktreeRuntimeState:
        """Bring ``worktree_path`` to a runnable state for an agent session.

        Idempotent: a reused worktree runs the same sequence as a fresh one.
        Artifact hiding runs last because it needs the CLI tool paths the sync
        step planted.

        Raises:
            WorktreeError: If a step the session depends on cannot complete —
                hook install, Claude settings, the no-verify flag, or worktree
                identity. A half-set-up worktree is a worse outcome than a
                failed create the lifecycle can retry or recreate from.
        """
        if self.enforce_hooks:
            install_hooks(worktree_path, self.pre_push_hook)
        install_claude_settings(worktree_path)
        _configure_no_verify_dry_run(
            worktree_path, self.allow_no_verify_dry_run_preflight
        )
        _link_repo_venv_into_worktree(self.repo_root, worktree_path)
        synced_cli_tool_paths = list(sync_cli_tools(worktree_path))
        worktree_id = _install_worktree_identity(worktree_path)
        _hide_runtime_artifacts_from_git_status(worktree_path, synced_cli_tool_paths)

        logger.debug(
            "Worktree runtime setup applied: path=%s id=%s hooks=%s",
            worktree_path,
            worktree_id,
            self.enforce_hooks,
        )
        return WorktreeRuntimeState(
            worktree_path=worktree_path,
            worktree_id=worktree_id,
            hooks_installed=self.enforce_hooks,
            no_verify_dry_run_allowed=self.allow_no_verify_dry_run_preflight,
            synced_cli_tool_paths=tuple(synced_cli_tool_paths),
        )
