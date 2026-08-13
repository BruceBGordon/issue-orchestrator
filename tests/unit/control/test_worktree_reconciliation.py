"""Safety and recovery tests for startup worktree reconciliation."""

from pathlib import Path
from unittest.mock import MagicMock, call

from issue_orchestrator.control.worktree_reconciliation import (
    StartupWorktreeReconciler,
    WorktreeActivityEvidence,
    WorktreeAuditOwner,
    audit_registered_worktrees,
)
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.ports.worktree_manager import (
    RegisteredWorktree,
    ReviewerHeadOwnership,
)


def _owned(path: Path) -> None:
    marker = path / ".issue-orchestrator" / "worktree-id"
    marker.parent.mkdir(parents=True)
    marker.write_text("wt-test-owned", encoding="utf-8")


def _registered(
    path: Path,
    *,
    branch: str | None,
    head: str = "a" * 40,
    locked: bool = False,
) -> RegisteredWorktree:
    return RegisteredWorktree(path=path, head=head, branch=branch, locked=locked)


def test_audit_requires_exact_identity_branch_and_owner_marker(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    scratch = tmp_path / "project-tech-lead-42-abcdef123456"
    scratch.mkdir()
    _owned(scratch)
    manual = tmp_path / "project-wt-local"
    manual.mkdir()
    _owned(manual)
    lookalike = tmp_path / "project-tech-lead-43-fedcba654321"
    lookalike.mkdir()
    _owned(lookalike)

    entries = audit_registered_worktrees(
        repo_root=repo,
        worktree_base=tmp_path,
        registered=(
            _registered(
                scratch,
                branch="tech-lead-investigation-42-abcdef123456",
            ),
            _registered(manual, branch="local"),
            _registered(lookalike, branch="unrelated"),
        ),
        activity=WorktreeActivityEvidence.known(set()),
    )

    by_name = {entry.path.name: entry for entry in entries}
    assert by_name[scratch.name].disposition == "cleanup_candidate"
    assert by_name[scratch.name].kind == "tech_lead_scratch"
    assert by_name[manual.name].disposition == "retained"
    assert by_name[manual.name].kind == "external"
    assert by_name[lookalike.name].disposition == "retained"
    assert by_name[lookalike.name].kind == "external"


def test_recover_removes_only_inactive_owned_disposables(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    scratch = tmp_path / "project-tech-lead-42-abcdef123456"
    coder = tmp_path / "project-41"
    reviewer = tmp_path / "project-41-review-20260812T010203123456Z"
    locked_reviewer = tmp_path / "project-40-review-20260812T010203123456Z"
    issue_tree = tmp_path / "project-39"
    manual = tmp_path / "project-wt-local"
    for path in (scratch, coder, reviewer, locked_reviewer, issue_tree, manual):
        path.mkdir()
        _owned(path)

    registered = (
        _registered(repo, branch="main"),
        _registered(scratch, branch="tech-lead-investigation-42-abcdef123456"),
        _registered(coder, branch="41-fix", head="b" * 40),
        _registered(reviewer, branch=None, head="b" * 40),
        _registered(locked_reviewer, branch=None, locked=True),
        _registered(issue_tree, branch="39-fix"),
        _registered(manual, branch="local"),
    )
    config = MagicMock(repo_root=repo, worktree_base=tmp_path)
    worktrees = MagicMock()
    worktrees.list_registered.return_value = registered
    worktrees.read_reviewer_head_ownership.return_value = ReviewerHeadOwnership(
        marker_present=False,
        expected_head=None,
    )
    worktrees.can_remove_without_user_changes.return_value = True
    cleanup = MagicMock()
    cleanup.recover_orphaned_cleanups.return_value = 2

    summary = StartupWorktreeReconciler(
        config,
        cleanup,
        worktrees,
        WorktreeAuditOwner(worktrees),
    ).recover(OrchestratorState())

    assert worktrees.remove_checkout_and_branch.call_args_list == [
        call(scratch, force=True),
        call(reviewer, force=True),
    ]
    cleanup.recover_orphaned_cleanups.assert_called_once()
    assert summary.disposable_removed == 2
    assert summary.ordinary_removed == 2
    assert summary.retained == 4


def test_recover_retains_reviewer_with_local_changes(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    reviewer = tmp_path / "project-41-review-20260812T010203123456Z"
    coder = tmp_path / "project-41"
    for path in (coder, reviewer):
        path.mkdir()
        _owned(path)
    config = MagicMock(repo_root=repo, worktree_base=tmp_path)
    worktrees = MagicMock()
    worktrees.list_registered.return_value = (
        _registered(coder, branch="41-fix"),
        _registered(reviewer, branch=None),
    )
    worktrees.read_reviewer_head_ownership.return_value = ReviewerHeadOwnership(
        marker_present=False,
        expected_head=None,
    )
    worktrees.can_remove_without_user_changes.return_value = False
    cleanup = MagicMock()
    cleanup.recover_orphaned_cleanups.return_value = 0

    summary = StartupWorktreeReconciler(
        config,
        cleanup,
        worktrees,
        WorktreeAuditOwner(worktrees),
    ).recover(OrchestratorState())

    worktrees.remove_checkout_and_branch.assert_not_called()
    assert summary.disposable_removed == 0
    assert summary.retained == 2


def test_recover_retains_clean_reviewer_when_detached_head_diverged(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    coder = tmp_path / "project-41"
    reviewer = tmp_path / "project-41-review-20260812T010203123456Z"
    for path in (coder, reviewer):
        path.mkdir()
        _owned(path)
    config = MagicMock(repo_root=repo, worktree_base=tmp_path)
    worktrees = MagicMock()
    worktrees.list_registered.return_value = (
        _registered(coder, branch="41-fix", head="a" * 40),
        _registered(reviewer, branch=None, head="b" * 40),
    )
    worktrees.read_reviewer_head_ownership.return_value = ReviewerHeadOwnership(
        marker_present=True,
        expected_head=None,
    )
    worktrees.can_remove_without_user_changes.return_value = True
    cleanup = MagicMock()
    cleanup.recover_orphaned_cleanups.return_value = 0

    summary = StartupWorktreeReconciler(
        config,
        cleanup,
        worktrees,
        WorktreeAuditOwner(worktrees),
    ).recover(OrchestratorState())

    worktrees.can_remove_without_user_changes.assert_not_called()
    worktrees.remove_checkout_and_branch.assert_not_called()
    assert summary.disposable_removed == 0
    assert summary.retained == 2


def test_audit_retains_active_scratch(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    scratch = tmp_path / "project-tech-lead-42-abcdef123456"
    scratch.mkdir()
    _owned(scratch)

    entries = audit_registered_worktrees(
        repo_root=repo,
        worktree_base=tmp_path,
        registered=(
            _registered(scratch, branch="tech-lead-investigation-42-abcdef123456"),
        ),
        activity=WorktreeActivityEvidence.known({scratch}),
    )

    assert entries[0].disposition == "retained"
    assert "active session" in entries[0].reason


def test_audit_retains_reviewer_while_parent_session_is_active(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    coder = tmp_path / "project-41"
    reviewer = tmp_path / "project-41-review-20260812T010203123456Z"
    coder.mkdir()
    reviewer.mkdir()
    _owned(reviewer)

    entries = audit_registered_worktrees(
        repo_root=repo,
        worktree_base=tmp_path,
        registered=(_registered(reviewer, branch=None),),
        activity=WorktreeActivityEvidence.known({coder}),
    )

    assert entries[0].disposition == "retained"
    assert entries[0].reason == "review exchange parent session is active"


def test_audit_retains_disposable_worktrees_when_activity_is_unknown(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    scratch = tmp_path / "project-tech-lead-42-abcdef123456"
    reviewer = tmp_path / "project-41-review-20260812T010203123456Z"
    for path in (scratch, reviewer):
        path.mkdir()
        _owned(path)

    entries = audit_registered_worktrees(
        repo_root=repo,
        worktree_base=tmp_path,
        registered=(
            _registered(scratch, branch="tech-lead-investigation-42-abcdef123456"),
            _registered(reviewer, branch=None),
        ),
        activity=WorktreeActivityEvidence.unknown(),
    )

    assert {entry.disposition for entry in entries} == {"retained"}
    assert all("could not be verified" in entry.reason for entry in entries)
