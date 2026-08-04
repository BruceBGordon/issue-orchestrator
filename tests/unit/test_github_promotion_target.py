"""GitHub adapter for the finding-promotion target port (#6957).

Covers the two behaviors the lane's correctness turns on: a promotion issue is
never created before its labels exist (GitHub silently drops unknown labels,
which would leave an ungated, immediately schedulable issue), and "closed by a
merged PR" is distinguished from "closed" — that distinction is what separates
a shipped fix from an operator decline.
"""

from unittest.mock import Mock

import pytest

from issue_orchestrator.adapters.github.errors import GitHubHttpError
from issue_orchestrator.adapters.github.promotion_target import (
    GitHubPromotionTargetHost,
)

REPO = "porchpin/porchpin"


@pytest.fixture
def http_client():
    client = Mock()
    client.list_all_labels.return_value = [{"name": "agent:backend"}]
    client.create_issue.return_value = {
        "number": 501,
        "html_url": "https://github.com/porchpin/porchpin/issues/501",
    }
    client.get_prs_for_issue.return_value = []
    return client


@pytest.fixture
def target(http_client):
    adapter = Mock()
    adapter.repo = REPO
    adapter.http_client = http_client
    return GitHubPromotionTargetHost(adapter)


class TestFiling:
    def test_missing_labels_are_provisioned_before_the_issue_is_created(
        self, target, http_client
    ):
        calls: list[str] = []
        http_client.create_label.side_effect = lambda name, **_: calls.append(
            f"label:{name}"
        )
        http_client.create_issue.side_effect = lambda **_: (
            calls.append("issue") or {"number": 501, "html_url": "u"}
        )

        filed = target.file_issue(
            repo=REPO,
            title="t",
            body="b",
            labels=["proposed-tech-lead", "agent:backend"],
        )

        assert filed.number == 501
        # The gate label was provisioned FIRST; a dropped gate would leave an
        # ungated, immediately schedulable promotion.
        assert calls == ["label:proposed-tech-lead", "issue"]

    def test_existing_labels_are_not_recreated(self, target, http_client):
        target.file_issue(repo=REPO, title="t", body="b", labels=["agent:backend"])
        http_client.create_label.assert_not_called()

    def test_a_creation_without_an_issue_number_raises(self, target, http_client):
        http_client.create_issue.return_value = {}
        with pytest.raises(GitHubHttpError):
            target.file_issue(repo=REPO, title="t", body="b", labels=[])


class TestOutcomeReads:
    def test_open_issue_costs_one_read(self, target, http_client):
        http_client.get_issue.return_value = {"state": "open"}

        outcome = target.read_outcome(repo=REPO, issue_number=501)

        assert outcome is not None and not outcome.closed
        http_client.get_prs_for_issue.assert_not_called()

    def test_closed_with_a_merged_pr_reports_the_merge_url(self, target, http_client):
        http_client.get_issue.return_value = {"state": "closed"}
        http_client.get_prs_for_issue.return_value = [
            {"html_url": "https://github.com/x/y/pull/1", "pull_request": {}},
            {
                "html_url": "https://github.com/x/y/pull/6956",
                "pull_request": {"merged_at": "2026-08-03T00:00:00Z"},
            },
        ]

        outcome = target.read_outcome(repo=REPO, issue_number=501)

        assert outcome is not None
        assert outcome.closed
        assert outcome.merged_pr_url == "https://github.com/x/y/pull/6956"

    def test_closed_without_a_merged_pr_reports_no_merge_url(
        self, target, http_client
    ):
        http_client.get_issue.return_value = {"state": "closed"}
        http_client.get_prs_for_issue.return_value = [
            {"html_url": "https://github.com/x/y/pull/1", "pull_request": {}}
        ]

        outcome = target.read_outcome(repo=REPO, issue_number=501)

        assert outcome is not None
        assert outcome.closed
        assert outcome.merged_pr_url == ""

    def test_missing_issue_reads_as_unknown(self, target, http_client):
        http_client.get_issue.return_value = None
        assert target.read_outcome(repo=REPO, issue_number=501) is None


class TestWritability:
    def test_a_writable_repo_reports_no_reason(self, target, http_client):
        http_client.get_repository.return_value = {"permissions": {"push": True}}
        assert target.check_writable(repo=REPO) is None

    def test_a_read_only_repo_reports_the_reason(self, target, http_client):
        http_client.get_repository.return_value = {
            "permissions": {"push": False, "admin": False}
        }
        reason = target.check_writable(repo=REPO)
        assert reason is not None and "not writable" in reason

    def test_issues_disabled_reports_the_reason(self, target, http_client):
        http_client.get_repository.return_value = {
            "has_issues": False,
            "permissions": {"push": True},
        }
        reason = target.check_writable(repo=REPO)
        assert reason is not None and "issues disabled" in reason

    def test_a_missing_repo_reports_the_reason(self, target, http_client):
        error = GitHubHttpError("not found")
        error.status_code = 404
        http_client.get_repository.side_effect = error

        reason = target.check_writable(repo=REPO)

        assert reason is not None and "not found" in reason

    def test_a_payload_without_permissions_is_treated_as_writable(
        self, target, http_client
    ):
        """Some token kinds omit permissions; an inconclusive read must not
        block startup — the applier's filing failure is still loud."""
        http_client.get_repository.return_value = {"name": "porchpin"}
        assert target.check_writable(repo=REPO) is None
