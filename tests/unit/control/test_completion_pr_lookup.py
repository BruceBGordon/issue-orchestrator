"""Behaviour of ``CompletionPrLookup`` — which PR did this session produce?

These paths existed inside ``CompletionHandler`` and were only reachable by
driving a whole completion, so the review fallback and the two error branches
had no direct coverage at all. The extraction (#6858 rework 1) made them
addressable; this pins them.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from issue_orchestrator.control.completion_pr_lookup import (
    NO_COMPLETION_PR,
    CompletionPrLookup,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, Session, SessionStatus
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from tests.unit.session_run_helpers import make_session_run_assets


def make_session(
    tmp_path: Path,
    *,
    task: TaskKind = TaskKind.CODE,
    pr_number: int | None = None,
) -> Session:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    issue = Issue(number=7, title="Test issue", labels=["agent:test"], repo="owner/repo")
    return Session(
        key=SessionKey(issue=FakeIssueKey("7"), task=task),
        issue=issue,
        agent_config=AgentConfig(prompt_path=tmp_path / "prompt.txt", timeout_minutes=45),
        terminal_id="issue-7",
        worktree_path=worktree,
        branch_name="issue-7",
        run_assets=make_session_run_assets(worktree, session_name="issue-7"),
        pr_number=pr_number,
    )


def make_host(
    *,
    prs_for_branch: list | None = None,
    pr: object | None = None,
    get_pr_raises: bool = False,
    get_prs_raises: bool = False,
) -> SimpleNamespace:
    """A repository host recording the calls the lookup makes."""
    calls: list[str] = []

    def get_prs_for_branch(_branch):
        calls.append("get_prs_for_branch")
        if get_prs_raises:
            raise RuntimeError("branch lookup exploded")
        return prs_for_branch or []

    def get_pr(_number):
        calls.append("get_pr")
        if get_pr_raises:
            raise RuntimeError("PR fetch exploded")
        return pr

    return SimpleNamespace(
        get_prs_for_branch=get_prs_for_branch, get_pr=get_pr, calls=calls
    )


def pr_record(number: int, url: str) -> SimpleNamespace:
    return SimpleNamespace(number=number, url=url)


@pytest.mark.parametrize(
    "status",
    [SessionStatus.FAILED, SessionStatus.BLOCKED, SessionStatus.TIMED_OUT],
)
def test_non_completed_session_produces_no_pr_and_asks_github_nothing(tmp_path, status):
    """A session that did not complete has no PR — and costs no API call."""
    host = make_host(prs_for_branch=[pr_record(11, "https://gh/pull/11")])
    lookup = CompletionPrLookup(host)

    assert lookup.for_session(make_session(tmp_path), status) == NO_COMPLETION_PR
    assert host.calls == []


def test_retrospective_review_produces_no_pr_and_asks_github_nothing(tmp_path):
    """Retrospective review reviews existing work; it never authors a PR."""
    host = make_host(prs_for_branch=[pr_record(11, "https://gh/pull/11")])
    lookup = CompletionPrLookup(host)

    session = make_session(tmp_path, task=TaskKind.RETROSPECTIVE_REVIEW)
    result = lookup.for_session(session, SessionStatus.COMPLETED)

    assert result == NO_COMPLETION_PR
    assert host.calls == []


def test_hint_short_circuits_the_branch_lookup(tmp_path):
    """The completion processor's URL wins; no branch query is made."""
    host = make_host(pr=pr_record(42, "https://gh/pull/42"))
    lookup = CompletionPrLookup(host)

    result = lookup.for_session(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        pr_url_hint="https://github.com/o/r/pull/42",
    )

    assert result.url == "https://github.com/o/r/pull/42"
    assert result.number == 42
    assert result.pull_requests is not None and result.pull_requests[0].number == 42
    assert "get_prs_for_branch" not in host.calls


def test_hint_survives_a_failed_pr_fetch(tmp_path):
    """The URL and number are known from the hint itself, so a fetch failure
    must not throw away what we already know."""
    host = make_host(get_pr_raises=True)
    lookup = CompletionPrLookup(host)

    result = lookup.for_session(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        pr_url_hint="https://github.com/o/r/pull/42",
    )

    assert result.url == "https://github.com/o/r/pull/42"
    assert result.number == 42
    assert result.pull_requests is None


def test_unparseable_hint_keeps_the_url_without_a_number(tmp_path):
    host = make_host()
    lookup = CompletionPrLookup(host)

    result = lookup.for_session(
        make_session(tmp_path),
        SessionStatus.COMPLETED,
        pr_url_hint="https://github.com/o/r/issues/42",
    )

    assert result.url == "https://github.com/o/r/issues/42"
    assert result.number is None
    assert result.pull_requests is None
    assert host.calls == []


def test_branch_lookup_reports_the_first_pr_and_keeps_them_all(tmp_path):
    host = make_host(
        prs_for_branch=[
            pr_record(11, "https://gh/pull/11"),
            pr_record(12, "https://gh/pull/12"),
        ]
    )
    lookup = CompletionPrLookup(host)

    result = lookup.for_session(make_session(tmp_path), SessionStatus.COMPLETED)

    assert (result.url, result.number) == ("https://gh/pull/11", 11)
    assert result.pull_requests is not None and len(result.pull_requests) == 2


def test_review_session_falls_back_to_its_own_pr_when_the_branch_has_none(tmp_path):
    """A review session's branch carries no PR of its own; the PR it reviewed
    is the answer."""
    host = make_host(prs_for_branch=[], pr=pr_record(99, "https://gh/pull/99"))
    lookup = CompletionPrLookup(host)

    session = make_session(tmp_path, task=TaskKind.REVIEW, pr_number=99)
    result = lookup.for_session(session, SessionStatus.COMPLETED)

    assert (result.url, result.number) == ("https://gh/pull/99", 99)
    assert result.pull_requests is not None and result.pull_requests[0].number == 99


def test_review_fallback_that_raises_produces_no_pr(tmp_path):
    """A failed fallback fetch is not a crash — completion still has to finish."""
    host = make_host(prs_for_branch=[], get_pr_raises=True)
    lookup = CompletionPrLookup(host)

    session = make_session(tmp_path, task=TaskKind.REVIEW, pr_number=99)

    assert lookup.for_session(session, SessionStatus.COMPLETED) == NO_COMPLETION_PR


def test_review_fallback_that_finds_nothing_produces_no_pr(tmp_path):
    host = make_host(prs_for_branch=[], pr=None)
    lookup = CompletionPrLookup(host)

    session = make_session(tmp_path, task=TaskKind.REVIEW, pr_number=99)

    assert lookup.for_session(session, SessionStatus.COMPLETED) == NO_COMPLETION_PR


def test_no_branch_pr_and_no_review_pr_produces_no_pr(tmp_path):
    host = make_host(prs_for_branch=[])
    lookup = CompletionPrLookup(host)

    result = lookup.for_session(make_session(tmp_path), SessionStatus.COMPLETED)

    assert result == NO_COMPLETION_PR
    assert host.calls == ["get_prs_for_branch"]
