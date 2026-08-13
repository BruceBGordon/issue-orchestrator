"""Public worktree API for tests and adapters."""

from ._worktree import (
    create_worktree,
    remove_worktree,
    list_worktrees,
    worktree_exists,
    has_uncommitted_changes,
    can_remove_without_user_changes,
    slugify,
    generate_branch_name,
    get_worktree_branch,
    next_branch_name,
    find_worktree_for_branch,
)
from ._worktree_errors import WorktreeError
from ._worktree_hooks import HOOKS_DIR, install_hooks
from ._worktree_runtime import (
    install_claude_settings,
    install_worktree_identity,
    read_reviewer_head_ownership,
    sync_cli_tools,
)
from ._worktree_runtime_setup import WorktreeRuntimeSetup, WorktreeRuntimeState


__all__ = [
    "WorktreeError",
    "WorktreeRuntimeSetup",
    "WorktreeRuntimeState",
    "create_worktree",
    "remove_worktree",
    "list_worktrees",
    "worktree_exists",
    "has_uncommitted_changes",
    "can_remove_without_user_changes",
    "install_hooks",
    "slugify",
    "generate_branch_name",
    "get_worktree_branch",
    "next_branch_name",
    "find_worktree_for_branch",
    "HOOKS_DIR",
    "install_claude_settings",
    "install_worktree_identity",
    "read_reviewer_head_ownership",
    "sync_cli_tools",
]
