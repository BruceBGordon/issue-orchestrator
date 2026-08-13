"""Integration coverage for review-gated checkout retention cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from issue_orchestrator.control.cleanup_manager import CleanupManager
from issue_orchestrator.control.worktree_reconciliation import (
    StartupWorktreeReconciler,
    WorktreeAuditOwner,
)
from issue_orchestrator.domain.models import OrchestratorState, PendingCleanup
from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
from issue_orchestrator.execution.reviewer_worktree import create_reviewer_worktree
from issue_orchestrator.ports.pull_request_tracker import PRInfo


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _repository_with_local_only_worktree(
    tmp_path: Path,
    issue_number: int,
) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    if not repo.exists():
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(repo)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test User")
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "base")

    branch = f"{issue_number}-local-only"
    worktree = tmp_path / f"repo-{issue_number}"
    _git(repo, "worktree", "add", "-b", branch, str(worktree), "main")
    (worktree / "local-only.txt").write_text(f"issue {issue_number}\n", encoding="utf-8")
    _git(worktree, "add", "local-only.txt")
    _git(worktree, "commit", "-m", f"local-only work for {issue_number}")
    return repo, worktree, branch


def _cleanup_manager(
    *,
    repository_host: MagicMock,
    worktree_path: Path,
) -> CleanupManager:
    config = SimpleNamespace(
        tech_lead_enabled=False,
        code_review_agent="agent:reviewer",
        code_reviewed_label="code-reviewed",
        cleanup=SimpleNamespace(
            without_tech_lead=SimpleNamespace(
                close_ai_session_tabs=True,
                remove_worktrees=True,
            ),
        ),
        agents={"agent:backend": SimpleNamespace()},
    )
    return CleanupManager(
        config=config,
        repository_host=repository_host,
        worktree_manager=GitWorktreeManager(),
        kill_session_fn=lambda _terminal_id: None,
        session_exists_fn=lambda _terminal_id: False,
        get_worktree_path_fn=lambda _issue, _agent: worktree_path,
        get_session_name_fn=lambda issue, _kind: f"issue-{issue}",
    )


def _assert_local_only_branch_survives(repo: Path, branch: str) -> None:
    assert _git(repo, "branch", "--list", branch).stdout.strip() == branch
    assert _git(repo, "show", f"{branch}:local-only.txt").stdout.startswith("issue ")


def test_graceful_review_cleanup_removes_checkout_but_preserves_branch(
    tmp_path: Path,
) -> None:
    repo, worktree, branch = _repository_with_local_only_worktree(tmp_path, 123)
    host = MagicMock()
    host.get_prs_with_label.return_value = [
        PRInfo(
            number=456,
            url="https://example.test/pr/456",
            title="PR",
            branch=branch,
            labels=["code-reviewed"],
            body="",
            state="open",
        )
    ]
    cleanup = _cleanup_manager(repository_host=host, worktree_path=worktree)
    pending = PendingCleanup(
        issue=SimpleNamespace(number=123),
        pr_number=456,
        pr_url="https://example.test/pr/456",
        branch_name=branch,
        terminal_id="issue-123",
        worktree_path=worktree,
    )

    assert cleanup.process_deferred_cleanups([pending]) == []

    assert not worktree.exists()
    _assert_local_only_branch_survives(repo, branch)


def test_startup_review_cleanup_removes_checkout_but_preserves_branch(
    tmp_path: Path,
) -> None:
    repo, worktree, branch = _repository_with_local_only_worktree(tmp_path, 124)
    host = MagicMock()
    host.get_prs_with_label.return_value = [
        PRInfo(
            number=457,
            url="https://example.test/pr/457",
            title="PR",
            branch=branch,
            labels=["code-reviewed"],
            body="",
            state="open",
        )
    ]
    cleanup = _cleanup_manager(repository_host=host, worktree_path=worktree)

    assert cleanup.recover_orphaned_cleanups() == 1

    assert not worktree.exists()
    _assert_local_only_branch_survives(repo, branch)


def test_startup_retains_clean_reviewer_with_detached_commit(tmp_path: Path) -> None:
    repo, coder, branch = _repository_with_local_only_worktree(tmp_path, 125)
    reviewer = create_reviewer_worktree(
        coder_worktree=coder,
        coder_branch=branch,
        timestamp="20260812T010203123456Z",
    ).path
    (reviewer / "reviewer-only.txt").write_text(
        "must survive startup cleanup\n",
        encoding="utf-8",
    )
    _git(reviewer, "add", "reviewer-only.txt")
    _git(reviewer, "commit", "-m", "reviewer-only detached commit")
    reviewer_head = _git(reviewer, "rev-parse", "HEAD").stdout.strip()
    assert _git(reviewer, "status", "--porcelain").stdout.strip() == (
        "?? .issue-orchestrator/"
    )

    manager = GitWorktreeManager()
    cleanup = MagicMock()
    cleanup.recover_orphaned_cleanups.return_value = 0
    config = SimpleNamespace(repo_root=repo, worktree_base=tmp_path)

    summary = StartupWorktreeReconciler(
        config,
        cleanup,
        manager,
        WorktreeAuditOwner(manager),
    ).recover(OrchestratorState())

    assert summary.disposable_removed == 0
    assert reviewer.exists()
    assert _git(reviewer, "rev-parse", "HEAD").stdout.strip() == reviewer_head
    assert _git(reviewer, "show", "HEAD:reviewer-only.txt").stdout == (
        "must survive startup cleanup\n"
    )
    assert _git(coder, "rev-parse", "HEAD").stdout.strip() != reviewer_head


def test_startup_removes_owned_reviewer_after_coder_advances(tmp_path: Path) -> None:
    """A normal coder commit after the reviewer's last owned tip is harmless."""
    repo, coder, branch = _repository_with_local_only_worktree(tmp_path, 126)
    reviewer = create_reviewer_worktree(
        coder_worktree=coder,
        coder_branch=branch,
        timestamp="20260812T010203123456Z",
    ).path
    reviewer_head = _git(reviewer, "rev-parse", "HEAD").stdout.strip()

    (coder / "coder-progress.txt").write_text("later coder work\n", encoding="utf-8")
    _git(coder, "add", "coder-progress.txt")
    _git(coder, "commit", "-m", "coder advances after review")
    assert _git(coder, "rev-parse", "HEAD").stdout.strip() != reviewer_head

    manager = GitWorktreeManager()
    cleanup = MagicMock()
    cleanup.recover_orphaned_cleanups.return_value = 0
    config = SimpleNamespace(repo_root=repo, worktree_base=tmp_path)

    summary = StartupWorktreeReconciler(
        config,
        cleanup,
        manager,
        WorktreeAuditOwner(manager),
    ).recover(OrchestratorState())

    assert summary.disposable_removed == 1
    assert not reviewer.exists()
    assert coder.exists()
