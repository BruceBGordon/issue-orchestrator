"""Direct tests for completion PR collision helpers."""

from pathlib import Path

import pytest

from issue_orchestrator.control.completion_pr_collision import (
    NoCommitsBetweenError,
    create_pr_with_collision_handling,
    get_open_pr_for_issue,
    is_no_commits_error,
    is_pr_collision_error,
    next_branch_name,
    pr_matches_issue,
)
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.working_copy import PushResult


def _pr(
    *,
    number: int = 1,
    title: str = "Fixes #123",
    branch: str = "123-feature",
    state: str = "open",
) -> PRInfo:
    return PRInfo(
        number=number,
        title=title,
        url=f"https://github.com/owner/repo/pull/{number}",
        branch=branch,
        body="",
        state=state,
        labels=[],
    )


class FakeGitAdapter:
    def __init__(self, branches: list[str]) -> None:
        self._branches = branches

    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        set_upstream: bool = True,
        skip_hooks: bool = False,
    ) -> PushResult:
        return PushResult(success=True, branch="", remote=remote, message="")

    def create_branch_from_current(self, worktree: Path, branch: str) -> None:
        return None

    def list_branch_names(self, worktree: Path) -> list[str]:
        return self._branches


class FakePrAdapter:
    def __init__(self, prs: list[PRInfo]) -> None:
        self._prs = prs

    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool | None = None,
    ) -> PRInfo:
        return _pr(title=title, branch=head)

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        return [pr for pr in self._prs if state == "all" or pr.state == state]

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        return [
            pr for pr in self._prs
            if pr.branch == branch and (state == "all" or pr.state == state)
        ]


class FailingPrAdapter(FakePrAdapter):
    def __init__(self, error: Exception) -> None:
        super().__init__([])
        self._error = error

    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool | None = None,
    ) -> PRInfo:
        raise self._error


def test_pr_collision_error_detection_matches_github_message() -> None:
    assert is_pr_collision_error(Exception("Validation Failed: pull request already exists"))
    assert not is_pr_collision_error(Exception("Validation Failed: bad base branch"))


def test_no_commits_error_detection_requires_structured_error() -> None:
    assert is_no_commits_error(NoCommitsBetweenError(base="main", head="feature"))
    assert not is_no_commits_error(Exception("No commits between main and feature"))
    assert not is_no_commits_error(Exception("Pull request already exists"))


def test_create_pr_normalizes_github_no_commits_message(tmp_path: Path) -> None:
    actions_taken: list[str] = []

    with pytest.raises(NoCommitsBetweenError) as exc_info:
        create_pr_with_collision_handling(
            pr_adapter=FailingPrAdapter(
                RuntimeError("Validation Failed: No commits between main and feature")
            ),
            git_adapter=FakeGitAdapter([]),
            base_branch=lambda: "main",
            pr_collision_strategy="reuse_open",
            worktree=tmp_path,
            pr_title="Title",
            pr_body="Body",
            branch="feature",
            issue_number=123,
            actions_taken=actions_taken,
            skip_hooks=False,
            draft=False,
        )

    assert is_no_commits_error(exc_info.value)
    assert exc_info.value.base == "main"
    assert exc_info.value.head == "feature"
    assert actions_taken == []


def test_pr_matches_issue_by_branch_or_title() -> None:
    assert pr_matches_issue(_pr(branch="123-feature", title="Other title"), 123)
    assert pr_matches_issue(_pr(branch="feature", title="Fixes #123"), 123)
    assert not pr_matches_issue(_pr(branch="feature", title="Fixes #456"), 123)


def test_next_branch_name_uses_next_numeric_retry_suffix(tmp_path: Path) -> None:
    git_adapter = FakeGitAdapter(["123-feature", "123-feature-r1", "123-feature-r3"])

    assert next_branch_name(git_adapter, tmp_path, "123-feature-r2") == "123-feature-r4"


def test_get_open_pr_for_issue_scopes_to_expected_branch() -> None:
    adapter = FakePrAdapter([
        _pr(number=1, branch="123-old"),
        _pr(number=2, branch="123-current"),
    ])

    pr = get_open_pr_for_issue(adapter, 123, expected_branch="123-current")

    assert pr == _pr(number=2, branch="123-current")
