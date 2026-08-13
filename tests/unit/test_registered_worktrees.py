"""Tests for registered git worktree inventory parsing."""

from pathlib import Path

from issue_orchestrator.adapters.worktree._worktree import parse_registered_worktrees


def test_parse_registered_worktrees_preserves_branch_lock_and_prunable_state() -> None:
    parsed = parse_registered_worktrees(
        """worktree /repo
HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
branch refs/heads/main

worktree /repo-review
HEAD bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
detached
locked review in progress

worktree /gone
HEAD cccccccccccccccccccccccccccccccccccccccc
detached
prunable gitdir file points to non-existent location
"""
    )

    assert parsed[0].path == Path("/repo")
    assert parsed[0].branch == "main"
    assert parsed[1].branch is None
    assert parsed[1].locked is True
    assert parsed[2].prunable is True
