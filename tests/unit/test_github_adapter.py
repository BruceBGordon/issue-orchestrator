"""Comprehensive unit tests for GitHubAdapter.

This test suite achieves high coverage of the GitHubAdapter by mocking the
HTTP client layer and testing:
- Issue operations (get, list, create)
- Label operations (add, remove, get, has)
- PR operations (get, list, create)
- Caching behavior
- Error handling
- Write verification
"""

import pytest
from unittest.mock import MagicMock, Mock, patch, call
from issue_orchestrator.adapters.github import GitHubAdapter
from issue_orchestrator.adapters.github.cache import GitHubCache
from issue_orchestrator.adapters.github.http_client import (
    CommitCheckRollup,
    GitHubHttpError,
    GitHubTransportError,
)
from issue_orchestrator.adapters.github.github_issue import GitHubIssue
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import (
    MergeQueueEntry,
    MergeQueueRead,
    PRInfo,
    StatusCheckRollupRead,
)
from issue_orchestrator.ports.repository_host import DependencyIssueSnapshot
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.ports.verification import VerificationResult, FailureType


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Config()
    config.github_token = "test-token"
    config.github_api_url = "https://api.github.com"
    config.github_http_timeout_seconds = 20.0
    config.github_cache_ttl_seconds = 60
    config.gh_write_verify_timeout_seconds = 5
    config.gh_write_verify_initial_delay_ms = 100
    config.gh_write_verify_max_delay_ms = 500
    config.gh_write_verify_backoff = 1.5
    config.gh_write_verify_jitter_ms = 0
    return config


@pytest.fixture
def mock_http_client():
    """Create a mock HTTP client."""
    return MagicMock()


@pytest.fixture
def mock_verification_service():
    """Create a mock verification service."""
    service = MagicMock()
    # Default to successful verification
    service.verify_condition.return_value = (VerificationResult.SUCCESS, None)
    return service


@pytest.fixture
def cache():
    """Create a cache for testing."""
    return GitHubCache(default_ttl=60.0)


@pytest.fixture
def adapter(mock_config, mock_http_client, mock_verification_service, cache):
    """Create an adapter with mocked dependencies."""
    return GitHubAdapter(
        repo="owner/repo",
        config=mock_config,
        cache=cache,
        verification_service=mock_verification_service,
        http_client=mock_http_client,
        verify_writes=True,
    )


class TestInitialization:
    """Test adapter initialization."""

    def test_init_with_repo_string(self, mock_config):
        """Test initialization with explicit repo string."""
        adapter = GitHubAdapter(repo="owner/repo", config=mock_config)
        assert adapter.repo == "owner/repo"

    def test_init_without_repo_uses_git_remote(self, mock_config):
        """Test initialization without repo falls back to git remote."""
        with patch("issue_orchestrator.adapters.github.github_adapter.get_repo_from_git") as mock_get_repo:
            mock_get_repo.return_value = "owner/inferred-repo"
            adapter = GitHubAdapter(repo=None, config=mock_config)
            assert adapter.repo == "owner/inferred-repo"
            mock_get_repo.assert_called_once()

    # Note: Tests for cache_enabled internal state were removed.
    # Cache behavior is tested through observable API call patterns in TestCaching.

    def test_init_verification_service_is_used(self, mock_config, mock_verification_service, mock_http_client):
        """Test that injected verification service is used for write verification."""
        adapter = GitHubAdapter(
            repo="owner/repo",
            config=mock_config,
            verification_service=mock_verification_service,
            http_client=mock_http_client,
        )
        mock_http_client.get_issue_labels.return_value = ["bug"]
        # Exercise a write operation that triggers verification
        adapter.add_label(42, "bug")
        # Verify that the injected service was used
        mock_verification_service.verify_condition.assert_called()


class TestIssueOperations:
    """Test issue-related operations."""

    def test_get_issue_success(self, adapter, mock_http_client):
        """Test successful issue retrieval."""
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "title": "Test Issue",
            "labels": [{"name": "bug"}, {"name": "priority:high"}],
            "state": "open",
            "body": "Issue body",
            "milestone": {"title": "v1.0", "number": 1, "due_on": "2024-01-01"},
            "created_at": "2026-07-10T10:00:00Z",
            "updated_at": "2026-07-11T12:00:00Z",
            "comments": 4,
        }

        issue = adapter.get_issue(42)

        assert isinstance(issue, GitHubIssue)
        assert issue.number == 42
        assert issue.title == "Test Issue"
        assert issue.labels == ("bug", "priority:high")
        assert issue.state == "open"
        assert issue.body == "Issue body"
        assert issue.milestone == "v1.0"
        assert issue.milestone_number == 1
        assert issue.created_at == "2026-07-10T10:00:00Z"
        assert issue.updated_at == "2026-07-11T12:00:00Z"
        assert issue.comment_count == 4
        mock_http_client.get_issue.assert_called_once_with(42)

    def test_get_issue_not_found(self, adapter, mock_http_client):
        """Test get_issue returns None when issue not found."""
        mock_http_client.get_issue.side_effect = GitHubHttpError("Not found", status_code=404)

        issue = adapter.get_issue(999)

        assert issue is None

    def test_get_issue_http_error_propagates(self, adapter, mock_http_client):
        """get_issue preserves upstream HTTP failures."""
        mock_http_client.get_issue.side_effect = GitHubHttpError("API error", status_code=500)

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.get_issue(42)
        assert exc_info.value.status_code == 500

    def test_get_issue_invalid_response(self, adapter, mock_http_client):
        """Test get_issue returns None on invalid response."""
        mock_http_client.get_issue.return_value = "not a dict"

        issue = adapter.get_issue(42)

        assert issue is None

    def test_list_issues_success(self, adapter, mock_http_client):
        """Test successful issue listing."""
        mock_http_client.list_issues.return_value = [
            {
                "number": 1,
                "title": "Issue 1",
                "labels": [{"name": "bug"}],
                "state": "open",
                "updated_at": "2026-07-11T12:00:00Z",
                "comments": 3,
            },
            {
                "number": 2,
                "title": "Issue 2",
                "labels": [{"name": "feature"}],
                "state": "open",
            },
        ]

        issues = adapter.list_issues(labels=["bug"], state="open", limit=10)

        assert len(issues) == 2
        assert all(isinstance(i, GitHubIssue) for i in issues)
        assert issues[0].number == 1
        assert issues[0].updated_at == "2026-07-11T12:00:00Z"
        assert issues[0].comment_count == 3
        assert issues[1].number == 2
        mock_http_client.list_issues.assert_called_once_with(
            labels=["bug"],
            state="open",
            milestone=None,
            limit=10,
            use_cache=True,
            exhaustive=False,
        )

    def test_list_issues_with_milestone(self, adapter, mock_http_client):
        """Test listing issues filtered by milestone."""
        mock_http_client.list_issues.return_value = [
            {
                "number": 1,
                "title": "Issue 1",
                "labels": [],
                "state": "open",
                "milestone": {"title": "v1.0"},
            },
        ]

        issues = adapter.list_issues(milestone="v1.0")

        assert len(issues) == 1
        mock_http_client.list_issues.assert_called_once_with(
            labels=None,
            state="open",
            milestone="v1.0",
            limit=100,
            use_cache=True,
            exhaustive=False,
        )

    def test_list_issues_http_error_propagates(self, adapter, mock_http_client):
        """list_issues preserves upstream HTTP failures."""
        mock_http_client.list_issues.side_effect = GitHubHttpError("API error", status_code=503)

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.list_issues()
        assert exc_info.value.status_code == 503

    def test_list_issues_retries_without_cache_when_required_ids_missing(
        self, adapter, mock_http_client
    ):
        """Test list_issues retries without cache when required IDs are missing."""
        # First call returns stale data without issue 2
        cached_response = [
            {"number": 1, "title": "Issue 1", "labels": [], "state": "open"},
        ]
        # Second call returns fresh data with issue 2
        fresh_response = [
            {"number": 1, "title": "Issue 1", "labels": [], "state": "open"},
            {"number": 2, "title": "Issue 2", "labels": [], "state": "open"},
        ]
        mock_http_client.list_issues.side_effect = [cached_response, fresh_response]

        required_ids = {"2"}  # Require issue 2's stable_id
        issues = adapter.list_issues(required_stable_ids=required_ids)

        # Should have made two calls
        assert mock_http_client.list_issues.call_count == 2
        # First with cache, second without
        assert mock_http_client.list_issues.call_args_list[0].kwargs["use_cache"] is True
        assert mock_http_client.list_issues.call_args_list[1].kwargs["use_cache"] is False
        # Result should include both issues
        assert len(issues) == 2

    def test_list_issues_no_retry_when_required_ids_found(self, adapter, mock_http_client):
        """Test list_issues doesn't retry when all required IDs are present."""
        mock_http_client.list_issues.return_value = [
            {"number": 1, "title": "Issue 1", "labels": [], "state": "open"},
        ]

        required_ids = {"1"}
        issues = adapter.list_issues(required_stable_ids=required_ids)

        # Should only make one call
        assert mock_http_client.list_issues.call_count == 1
        assert len(issues) == 1

    def test_list_issues_delta_success(self, adapter, mock_http_client):
        mock_http_client.list_issues_since.return_value = (
            [
                {"number": 1, "title": "Issue 1", "labels": [], "state": "open"},
                {"number": 2, "title": "Issue 2", "labels": [], "state": "closed"},
            ],
            "2026-01-01T01:00:00Z",
        )

        issues, watermark = adapter.list_issues_delta(since="2026-01-01T00:00:00Z", limit=25)

        assert len(issues) == 2
        assert watermark == "2026-01-01T01:00:00Z"
        mock_http_client.list_issues_since.assert_called_once_with(
            since="2026-01-01T00:00:00Z",
            state="all",
            limit=25,
            use_cache=False,
        )

    def test_list_issues_delta_error_propagates(self, adapter, mock_http_client):
        mock_http_client.list_issues_since.side_effect = GitHubHttpError("boom", status_code=502)

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.list_issues_delta(since="2026-01-01T00:00:00Z", limit=25)
        assert exc_info.value.status_code == 502

    def test_get_issue_by_key_github_key_numeric(self, adapter, mock_http_client):
        """Test get_issue_by_key with numeric GitHub key."""
        key = GitHubIssueKey(repo="owner/repo", external_id="42")
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "title": "Test",
            "labels": [],
            "state": "open",
        }

        issue = adapter.get_issue_by_key(key)

        assert issue is not None
        assert issue.number == 42
        mock_http_client.get_issue.assert_called_once_with(42)

    def test_get_issue_by_key_non_numeric_returns_none(self, adapter):
        """Test get_issue_by_key returns None for non-numeric external_id."""
        key = GitHubIssueKey(repo="owner/repo", external_id="M1-011")

        issue = adapter.get_issue_by_key(key)

        assert issue is None

    def test_create_issue(self, adapter, mock_http_client, mock_verification_service):
        """Test creating a new issue."""
        mock_http_client.create_issue.return_value = {"number": 42, "html_url": "https://github.com/owner/repo/issues/42"}
        mock_http_client.get_issue_labels.return_value = ["bug", "priority:high"]

        result = adapter.create_issue(
            title="New Issue",
            body="Issue body",
            labels=["bug", "priority:high"],
        )

        assert result == {"number": 42, "html_url": "https://github.com/owner/repo/issues/42"}
        mock_http_client.create_issue.assert_called_once_with(
            title="New Issue",
            body="Issue body",
            labels=["bug", "priority:high"],
            milestone=None,
        )
        # Should verify the labels were applied
        mock_verification_service.verify_condition.assert_called_once()

    def test_create_issue_without_labels(self, adapter, mock_http_client, mock_verification_service):
        """Test creating issue without labels doesn't trigger verification."""
        mock_http_client.create_issue.return_value = {"number": 42, "html_url": "https://github.com/owner/repo/issues/42"}

        result = adapter.create_issue(title="New Issue", body="Body", labels=None)

        assert result == {"number": 42, "html_url": "https://github.com/owner/repo/issues/42"}
        # No verification should occur without labels
        mock_verification_service.verify_condition.assert_not_called()

    def test_create_issue_returns_none_on_failure(self, adapter, mock_http_client):
        """Test create_issue returns None on failure."""
        mock_http_client.create_issue.return_value = None

        issue_number = adapter.create_issue(title="Test", body="Body")

        assert issue_number is None


class TestLabelOperations:
    """Test label-related operations."""

    def test_get_issue_labels_from_cache(self, adapter, cache, mock_http_client):
        """Test getting labels from cache when available."""
        cache.set_issue_labels(42, ["bug", "feature"])

        labels = adapter.get_issue_labels(42)

        assert labels == ["bug", "feature"]
        # Should not call HTTP client
        mock_http_client.get_issue_labels.assert_not_called()

    def test_get_issue_labels_from_api_when_cache_disabled(self, mock_config, mock_http_client, mock_verification_service):
        """Test getting labels from API when cache is disabled."""
        mock_config.github_cache_ttl_seconds = 0  # Disable cache via config
        adapter = GitHubAdapter(
            repo="owner/repo",
            config=mock_config,
            http_client=mock_http_client,
            verification_service=mock_verification_service,
        )
        mock_http_client.get_issue_labels.return_value = ["bug"]

        labels = adapter.get_issue_labels(42)

        assert labels == ["bug"]
        mock_http_client.get_issue_labels.assert_called_once_with(42, use_cache=True)

    def test_get_issue_labels_updates_cache(self, adapter, mock_http_client, cache):
        """Test that fetching labels updates the cache."""
        mock_http_client.get_issue_labels.return_value = ["bug", "feature"]

        labels = adapter.get_issue_labels(42)

        assert labels == ["bug", "feature"]
        # Cache should be updated
        cached = cache.get_issue_labels(42)
        assert cached == ["bug", "feature"]

    def test_get_issue_labels_fresh_bypasses_cache(self, adapter, mock_http_client, cache):
        """Test that fresh label reads bypass adapter/ETag caches."""
        cache.set_issue_labels(42, ["stale"])
        mock_http_client.get_issue_labels.return_value = ["fresh"]

        labels = adapter.get_issue_labels_fresh(42)

        assert labels == ["fresh"]
        mock_http_client.get_issue_labels.assert_called_once_with(42, use_cache=False)
        assert cache.get_issue_labels(42) == ["fresh"]
    def test_get_issue_labels_not_found_returns_empty_list(self, adapter, mock_http_client):
        """Test get_issue_labels returns empty list when the issue is absent."""
        mock_http_client.get_issue_labels.side_effect = GitHubHttpError("Not found", status_code=404)

        labels = adapter.get_issue_labels(42)

        assert labels == []

    def test_get_issue_labels_http_error_propagates(self, adapter, mock_http_client):
        """Label reads preserve upstream HTTP failures."""
        mock_http_client.get_issue_labels.side_effect = GitHubHttpError("API error", status_code=500)

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.get_issue_labels(42)
        assert exc_info.value.status_code == 500

    def test_add_label_success(self, adapter, mock_http_client, mock_verification_service):
        """Test successfully adding a label."""
        mock_http_client.get_issue_labels.return_value = ["bug"]

        adapter.add_label(42, "bug")

        mock_http_client.add_label.assert_called_once_with(42, "bug")
        # Verification should be called
        mock_verification_service.verify_condition.assert_called_once()

    def test_add_label_verifies_with_fresh_read(self, adapter, mock_http_client, mock_verification_service):
        """Test that add_label verification uses a fresh (no-cache) label read."""
        def _verify_condition(*_args, check=None, **_kwargs):
            assert check is not None
            check()
            return (VerificationResult.SUCCESS, None)

        mock_verification_service.verify_condition.side_effect = _verify_condition
        mock_http_client.get_issue_labels.return_value = ["bug"]

        adapter.add_label(42, "bug")

        mock_http_client.get_issue_labels.assert_any_call(42, use_cache=False)

    def test_add_label_invalidates_cache(self, adapter, mock_http_client, mock_verification_service, cache):
        """Test that adding a label invalidates the cache."""
        # Pre-populate cache
        cache.set_issue_labels(42, ["old-label"])
        cache.set_pr(42, {"number": 42, "labels": ["old-label"]})
        mock_http_client.get_issue_labels.return_value = ["old-label", "new-label"]

        adapter.add_label(42, "new-label")

        # Cache should be invalidated (empty after invalidation)
        assert cache.get_issue_labels(42) is None
        assert cache.get_pr(42) is None
        mock_http_client.invalidate_pr_etag.assert_called_once_with(42)

    def test_add_label_updates_pr_cache_labels(self, adapter, mock_http_client, mock_verification_service, cache):
        """Test that adding a label updates PR cache labels."""
        # Pre-populate PR cache
        pr_data = {
            "number": 10,
            "title": "Test PR",
            "url": "https://github.com/owner/repo/pull/10",
            "branch": "42-test",
            "body": "body",
            "state": "open",
            "labels": ["old-label"],
            "issue_number": 42,
        }
        cache.set_pr_by_issue(42, pr_data, branch="42-test")
        mock_http_client.get_issue_labels.return_value = ["old-label", "new-label"]

        adapter.add_label(42, "new-label")

        # Cache should be invalidated
        assert cache.get_issue_labels(42) is None
        assert cache.get_pr_by_issue(42) is None

    def test_add_label_verification_failure_raises(self, adapter, mock_http_client, mock_verification_service):
        """Test that add_label raises on verification failure."""
        mock_verification_service.verify_condition.return_value = (VerificationResult.FAILED_FATAL, "state")

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.add_label(42, "bug")

        assert "Failed to verify write" in str(exc_info.value)
        assert exc_info.value.is_issue_local()

    def test_remove_label_success(self, adapter, mock_http_client, mock_verification_service):
        """Test successfully removing a label."""
        mock_http_client.get_issue_labels.return_value = []

        adapter.remove_label(42, "bug")

        mock_http_client.remove_label.assert_called_once_with(42, "bug")
        mock_verification_service.verify_condition.assert_called_once()

    def test_remove_label_404_is_noop(self, adapter, mock_http_client, mock_verification_service):
        """Removing an already-absent label should not raise."""
        mock_http_client.remove_label.side_effect = GitHubHttpError("Not found", status_code=404)

        adapter.remove_label(42, "bug")

        mock_http_client.remove_label.assert_called_once_with(42, "bug")
        mock_verification_service.verify_condition.assert_not_called()

    def test_remove_label_invalidates_cache(self, adapter, mock_http_client, mock_verification_service, cache):
        """Test that removing a label invalidates the cache."""
        cache.set_issue_labels(42, ["bug"])
        cache.set_pr(42, {"number": 42, "labels": ["bug"]})
        mock_http_client.get_issue_labels.return_value = []

        adapter.remove_label(42, "bug")

        assert cache.get_issue_labels(42) is None
        assert cache.get_pr(42) is None
        mock_http_client.invalidate_pr_etag.assert_called_once_with(42)

    def test_has_label_true(self, adapter, mock_http_client):
        """Test has_label returns True when label exists."""
        mock_http_client.get_issue_labels.return_value = ["bug", "feature"]

        assert adapter.has_label(42, "bug") is True

    def test_has_label_false(self, adapter, mock_http_client):
        """Test has_label returns False when label doesn't exist."""
        mock_http_client.get_issue_labels.return_value = ["bug"]

        assert adapter.has_label(42, "feature") is False

    def test_has_label_error_propagates(self, adapter, mock_http_client):
        """has_label preserves upstream label-read failures."""
        mock_http_client.get_issue_labels.side_effect = GitHubHttpError("API error", status_code=500)

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.has_label(42, "bug")
        assert exc_info.value.status_code == 500

    def test_update_label_cache_refreshes_issue_labels_only(self, adapter, cache):
        """Issue-label refresh updates issue labels and leaves PR labels intact.

        Regression for #6595/#6670 F1: PR-scoped review labels (code-reviewed /
        needs-rework) are a distinct fact owned by PR reads. An issue-label
        refresh — which commonly yields [] — must NOT be mirrored onto the
        cached PR, or it would erase a still-current review label and make the
        stack predecessor work-gate read the PR as unreviewed.
        """
        # Pre-populate PR cache with a PR-scoped review label.
        pr_data = {
            "number": 10,
            "branch": "42-test",
            "labels": ["code-reviewed"],
            "issue_number": 42,
        }
        cache.set_pr_by_issue(42, pr_data, branch="42-test")

        adapter.update_label_cache(42, [])  # issue itself carries no labels

        # Issue labels are refreshed...
        assert cache.get_issue_labels(42) == []
        # ...but the cached PR's review label is preserved, not overwritten.
        cached_pr = cache.get_pr_by_issue(42)
        assert cached_pr["labels"] == ["code-reviewed"]

    def test_invalidate_label_cache(self, adapter, cache):
        """Test invalidate_label_cache removes cached labels."""
        cache.set_issue_labels(42, ["bug"])

        adapter.invalidate_label_cache(42)

        assert cache.get_issue_labels(42) is None


class TestPROperations:
    """Test PR-related operations."""

    def test_get_pr_success(self, adapter, mock_http_client):
        """get_pr is REST-only and does NOT fetch the rollup. Hot
        lifecycle paths must not pay the extra GraphQL round-trip."""
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature-branch"},
            "body": "PR description",
            "state": "open",
            "labels": [{"name": "bug"}],
            "mergeable_state": "UNSTABLE",
        }

        pr = adapter.get_pr(10)

        assert pr is not None
        assert pr.number == 10
        assert pr.title == "Test PR"
        assert pr.branch == "feature-branch"
        assert pr.labels == ["bug"]
        assert pr.mergeable_state == "unstable"
        assert pr.status_check_rollup is None
        mock_http_client.get_pr.assert_called_once_with(10)
        mock_http_client.get_pr_status_check_rollup.assert_not_called()

    def test_get_pr_reports_merged_state_from_merged_at(self, adapter, mock_http_client):
        """GitHub's REST `state` is only open/closed; a merged PR carries
        `merged_at`. PRInfo.state must distinguish merged from closed so
        reconciliation never mistakes a merged PR for a closed-unmerged one."""
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Merged PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature-branch"},
            "body": "",
            "state": "closed",
            "merged_at": "2026-06-01T20:00:50Z",
            "labels": [],
        }

        pr = adapter.get_pr(10)

        assert pr is not None
        assert pr.state == "merged"

    def test_get_pr_reports_merged_state_from_merged_flag(self, adapter, mock_http_client):
        """The REST detail payload's `merged` boolean also marks a merge."""
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Merged PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature-branch"},
            "body": "",
            "state": "closed",
            "merged": True,
            "merged_at": None,
            "labels": [],
        }

        pr = adapter.get_pr(10)

        assert pr is not None
        assert pr.state == "merged"

    def test_get_pr_closed_unmerged_stays_closed(self, adapter, mock_http_client):
        """A genuinely closed-without-merge PR keeps state == "closed" so the
        closed-unmerged drift path still flags it."""
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Closed PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature-branch"},
            "body": "",
            "state": "closed",
            "merged": False,
            "merged_at": None,
            "labels": [],
        }

        pr = adapter.get_pr(10)

        assert pr is not None
        assert pr.state == "closed"

    def test_read_pr_status_check_rollup_returns_ok_state(
        self, adapter, mock_http_client
    ):
        """read_pr_status_check_rollup reads ONLY the rollup (no REST PR
        fetch) and reports it as an `ok` capability — the awaiting-merge
        classifier needs this to disambiguate unstable+PENDING vs
        unstable+FAILURE."""
        mock_http_client.get_pr_status_check_rollup.return_value = "PENDING"

        read = adapter.read_pr_status_check_rollup(10)

        assert read == StatusCheckRollupRead(state="PENDING", capability="ok")
        assert read.permission_denied is False
        mock_http_client.get_pr_status_check_rollup.assert_called_once_with(10)
        # The rollup read must not pay for a REST PR fetch.
        mock_http_client.get_pr.assert_not_called()

    def test_read_pr_status_check_rollup_no_checks_is_ok_none(
        self, adapter, mock_http_client
    ):
        """No checks configured → `ok` with state=None, distinct from a
        permission failure."""
        mock_http_client.get_pr_status_check_rollup.return_value = None

        read = adapter.read_pr_status_check_rollup(10)

        assert read == StatusCheckRollupRead(state=None, capability="ok")

    def test_read_pr_status_check_rollup_rest_fallback_finds_failure(
        self, adapter, mock_http_client
    ):
        """A GraphQL permission wall still falls back to REST check state.

        This preserves post-publish failure detection for tokens that cannot
        read ``statusCheckRollup`` but can read check-runs/combined status.
        """
        mock_http_client.get_pr_status_check_rollup.side_effect = GitHubHttpError(
            "Resource not accessible by personal access token",
            status_code=403,
        )
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature-branch", "sha": "deadbeef"},
            "body": "",
            "state": "open",
            "labels": [],
            "mergeable_state": "UNSTABLE",
        }
        mock_http_client.get_commit_check_rollup.return_value = CommitCheckRollup(
            state="FAILURE",
            capability="ok",
        )

        read = adapter.read_pr_status_check_rollup(10)

        # The GraphQL source was denied even though the REST fallback found the
        # failure, so the read carries primary_source_denied=True to keep the
        # gate's GraphQL backoff armed.
        assert read == StatusCheckRollupRead(
            state="FAILURE", capability="ok", primary_source_denied=True
        )
        mock_http_client.get_pr.assert_called_once_with(10)
        mock_http_client.get_commit_check_rollup.assert_called_once_with("deadbeef")

    def test_read_pr_status_check_rollup_rest_fallback_permission_gap_stays_permission_denied(
        self, adapter, mock_http_client
    ):
        """A REST fallback that is unreadable for a SCOPE gap stays an
        operator-visible permission_denied read gap."""
        mock_http_client.get_pr_status_check_rollup.side_effect = GitHubHttpError(
            "Resource not accessible by personal access token",
            status_code=403,
        )
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature-branch", "sha": "deadbeef"},
            "body": "",
            "state": "open",
            "labels": [],
            "mergeable_state": "UNSTABLE",
        }
        mock_http_client.get_commit_check_rollup.return_value = CommitCheckRollup(
            state="SUCCESS",
            capability="permission_denied",
        )

        read = adapter.read_pr_status_check_rollup(10)

        assert read == StatusCheckRollupRead(
            state=None,
            capability="permission_denied",
            primary_source_denied=True,
        )
        mock_http_client.get_commit_check_rollup.assert_called_once_with("deadbeef")

    def test_read_pr_status_check_rollup_rest_fallback_transient_stays_transient(
        self, adapter, mock_http_client
    ):
        """A GraphQL permission wall plus a TRANSIENT REST source failure (5xx /
        rate-limit) must NOT be reported as permission_denied: it stays
        transient_error so the reconciler retries next tick instead of arming
        the repo-wide permission backoff and escalating a bogus missing-scope
        diagnostic (issue #6589 F1/A1)."""
        mock_http_client.get_pr_status_check_rollup.side_effect = GitHubHttpError(
            "Resource not accessible by personal access token",
            status_code=403,
        )
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature-branch", "sha": "deadbeef"},
            "body": "",
            "state": "open",
            "labels": [],
            "mergeable_state": "UNSTABLE",
        }
        mock_http_client.get_commit_check_rollup.return_value = CommitCheckRollup(
            state="SUCCESS",
            capability="transient_error",
        )

        read = adapter.read_pr_status_check_rollup(10)

        # A transient REST blip is still a GraphQL denial underneath, so the
        # read backs off the GraphQL source while staying retry-safe.
        assert read == StatusCheckRollupRead(
            state=None,
            capability="transient_error",
            primary_source_denied=True,
        )
        assert read.permission_denied is False
        mock_http_client.get_commit_check_rollup.assert_called_once_with("deadbeef")

    def test_read_pr_status_check_rollup_skip_primary_source_reads_rest_only(
        self, adapter, mock_http_client
    ):
        """During a GraphQL backoff window the gate passes
        ``skip_primary_source=True``: the wasted GraphQL probe is skipped, but
        the REST fallback is still read so a now-readable failure is classified.
        The read carries ``primary_source_denied=True`` so the gate keeps the
        GraphQL backoff armed (issue #6589 F1/A1)."""
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature-branch", "sha": "deadbeef"},
            "body": "",
            "state": "open",
            "labels": [],
            "mergeable_state": "UNSTABLE",
        }
        mock_http_client.get_commit_check_rollup.return_value = CommitCheckRollup(
            state="FAILURE",
            capability="ok",
        )

        read = adapter.read_pr_status_check_rollup(10, skip_primary_source=True)

        # GraphQL is never probed; the REST fallback classified the failure.
        mock_http_client.get_pr_status_check_rollup.assert_not_called()
        mock_http_client.get_commit_check_rollup.assert_called_once_with("deadbeef")
        assert read == StatusCheckRollupRead(
            state="FAILURE", capability="ok", primary_source_denied=True
        )

    def test_read_pr_status_check_rollup_forbidden_is_permission_denied(
        self, adapter, mock_http_client
    ):
        """A 403 must surface as `permission_denied`, never a silent
        `None` rollup — the token genuinely cannot read check status."""
        mock_http_client.get_pr_status_check_rollup.side_effect = GitHubHttpError(
            "forbidden", status_code=403
        )

        read = adapter.read_pr_status_check_rollup(10)

        assert read == StatusCheckRollupRead(
            state=None, capability="permission_denied", primary_source_denied=True
        )
        assert read.permission_denied is True

    def test_read_pr_status_check_rollup_graphql_scope_error_is_permission_denied(
        self, adapter, mock_http_client
    ):
        """GraphQL surfaces an insufficient-scope error as HTTP 200 with an
        `errors` array, so message-sniffing (not status code) classifies it."""
        mock_http_client.get_pr_status_check_rollup.side_effect = GitHubHttpError(
            "GitHub GraphQL error: Resource not accessible by personal access token",
            status_code=200,
        )

        read = adapter.read_pr_status_check_rollup(10)

        assert read.capability == "permission_denied"

    def test_read_pr_status_check_rollup_unauthorized_401_is_permission_denied(
        self, adapter, mock_http_client
    ):
        """A 401 is an authentication failure: the token cannot identify itself
        at all. That is an operator problem, not a retryable blip, so it stays
        on the permission_denied path regardless of body."""
        mock_http_client.get_pr_status_check_rollup.side_effect = GitHubHttpError(
            "GitHub GraphQL request failed: 401",
            status_code=401,
            response_text="Bad credentials",
        )

        read = adapter.read_pr_status_check_rollup(10)

        assert read == StatusCheckRollupRead(
            state=None, capability="permission_denied", primary_source_denied=True
        )
        assert read.permission_denied is True

    def test_read_pr_status_check_rollup_rate_limit_403_is_transient(
        self, adapter, mock_http_client
    ):
        """GitHub returns HTTP 403 for retryable throttling, not just missing
        scope. A primary rate-limit 403 body names no permission, so it must
        classify as transient_error — never the missing-scope path that arms
        the repo-wide backoff and escalates a bogus permission error."""
        mock_http_client.get_pr_status_check_rollup.side_effect = GitHubHttpError(
            "GitHub GraphQL request failed: 403",
            status_code=403,
            response_text="API rate limit exceeded for installation ID 12345.",
        )

        read = adapter.read_pr_status_check_rollup(10)

        assert read == StatusCheckRollupRead(state=None, capability="transient_error")
        assert read.permission_denied is False

    def test_read_pr_status_check_rollup_secondary_rate_limit_403_is_transient(
        self, adapter, mock_http_client
    ):
        """A 403 secondary-rate-limit body is throttling, not a scope gap, so
        it is PENDING-equivalent retry, not permission_denied."""
        mock_http_client.get_pr_status_check_rollup.side_effect = GitHubHttpError(
            "GitHub GraphQL request failed: 403",
            status_code=403,
            response_text=(
                "You have exceeded a secondary rate limit and have been "
                "temporarily blocked from content creation."
            ),
        )

        read = adapter.read_pr_status_check_rollup(10)

        assert read == StatusCheckRollupRead(state=None, capability="transient_error")
        assert read.permission_denied is False

    def test_read_pr_status_check_rollup_transient_failure_is_transient(
        self, adapter, mock_http_client
    ):
        """A 5xx GraphQL failure is transient: state=None, capability
        transient_error. The reconciler treats it as PENDING-equivalent so
        a transient error makes us wait, not escalate or rework."""
        mock_http_client.get_pr_status_check_rollup.side_effect = GitHubHttpError(
            "graphql boom", status_code=500
        )

        read = adapter.read_pr_status_check_rollup(10)

        assert read == StatusCheckRollupRead(state=None, capability="transient_error")
        assert read.permission_denied is False

    def test_read_pr_status_check_rollup_transport_failure_is_transient(
        self, adapter, mock_http_client
    ):
        """A pre-response transport failure (timeout/network) raises
        GitHubTransportError, which has no status code. It must classify as
        transient_error so the reconciler waits and retries next tick instead
        of letting the failure abort the awaiting-merge scan."""
        mock_http_client.get_pr_status_check_rollup.side_effect = GitHubTransportError(
            "GitHub GraphQL transport error",
            method="POST",
            url="/graphql",
            original=TimeoutError("read timed out"),
        )

        read = adapter.read_pr_status_check_rollup(10)

        assert read == StatusCheckRollupRead(state=None, capability="transient_error")
        assert read.permission_denied is False

    def test_read_pr_status_check_rollup_unknown_state_coerced_to_none(
        self, adapter, mock_http_client
    ):
        mock_http_client.get_pr_status_check_rollup.return_value = "FUTURE_STATE"

        read = adapter.read_pr_status_check_rollup(10)

        assert read == StatusCheckRollupRead(state=None, capability="ok")

    def test_get_pr_not_found(self, adapter, mock_http_client):
        """Test get_pr returns None when PR not found."""
        mock_http_client.get_pr.side_effect = GitHubHttpError("Not found", status_code=404)

        pr = adapter.get_pr(999)

        assert pr is None

    def test_get_pr_http_error_propagates(self, adapter, mock_http_client):
        """get_pr preserves upstream HTTP failures."""
        mock_http_client.get_pr.side_effect = GitHubHttpError("API error", status_code=500)

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.get_pr(10)
        assert exc_info.value.status_code == 500

    def test_list_prs_success(self, adapter, mock_http_client):
        """Test successfully listing PRs."""
        mock_http_client.list_prs.return_value = [
            {
                "number": 10,
                "title": "PR 1",
                "html_url": "https://github.com/owner/repo/pull/10",
                "head": {"ref": "branch-1"},
                "body": "",
                "state": "open",
                "labels": [],
            },
            {
                "number": 11,
                "title": "PR 2",
                "html_url": "https://github.com/owner/repo/pull/11",
                "head": {"ref": "branch-2"},
                "body": "",
                "state": "open",
                "labels": [],
            },
        ]

        prs = adapter.list_prs(state="open", limit=10)

        assert len(prs) == 2
        assert all(isinstance(pr, PRInfo) for pr in prs)
        assert prs[0].number == 10
        assert prs[1].number == 11
        mock_http_client.list_prs.assert_called_once_with(state="open", limit=10)

    def test_list_prs_error_propagates(self, adapter, mock_http_client):
        """list_prs preserves upstream HTTP failures."""
        mock_http_client.list_prs.side_effect = GitHubHttpError("API error", status_code=503)

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.list_prs()
        assert exc_info.value.status_code == 503

    def test_get_prs_for_branch_from_cache(self, adapter, mock_http_client, cache):
        """Test getting PRs for branch from cache."""
        pr_data = {
            "number": 10,
            "title": "Test PR",
            "url": "https://github.com/owner/repo/pull/10",
            "branch": "feature",
            "body": "",
            "state": "open",
            "labels": [],
        }
        cache.set_pr_by_branch("feature", pr_data)

        prs = adapter.get_prs_for_branch("feature")

        assert len(prs) == 1
        assert prs[0].number == 10
        # Should not call API
        mock_http_client.get_prs_for_branch.assert_not_called()

    def test_get_prs_for_branch_from_api(self, adapter, mock_http_client):
        """Test getting PRs for branch from API when not cached."""
        mock_http_client.get_prs_for_branch.return_value = [
            {
                "number": 10,
                "title": "Test PR",
                "html_url": "https://github.com/owner/repo/pull/10",
                "head": {"ref": "feature"},
                "body": "",
                "state": "open",
                "labels": [],
            },
        ]

        prs = adapter.get_prs_for_branch("feature")

        assert len(prs) == 1
        assert prs[0].branch == "feature"
        mock_http_client.get_prs_for_branch.assert_called_once_with("feature", state="open")

    def test_get_prs_for_branch_error_propagates(self, adapter, mock_http_client):
        """Branch PR lookup preserves upstream HTTP failures."""
        mock_http_client.get_prs_for_branch.side_effect = GitHubHttpError(
            "API error",
            status_code=503,
        )

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.get_prs_for_branch("feature")
        assert exc_info.value.status_code == 503

    def test_get_prs_for_issue_from_cache(self, adapter, cache):
        """Test getting PRs for issue from cache."""
        pr_data = {
            "number": 10,
            "title": "#42: Test",
            "url": "https://github.com/owner/repo/pull/10",
            "branch": "42-feature",
            "body": "",
            "state": "open",
            "labels": [],
            "issue_number": 42,
        }
        cache.set_pr_by_issue(42, pr_data, branch="42-feature")

        prs = adapter.get_prs_for_issue(42)

        assert len(prs) == 1
        assert prs[0].number == 10

    def test_get_prs_for_issue_all_bypasses_single_pr_cache(
        self, adapter, cache, mock_http_client
    ):
        """``state="all"`` must return the authoritative full list, never a single
        cached PR.

        Regression for #6430: the awaiting-merge reconciler suppresses
        ``blocked:pr-closed`` only after confirming no associated PR is open. The
        by-issue cache holds one PR; if an older closed PR is cached while a newer
        open PR exists on GitHub, satisfying ``all`` from the cache would hide the
        open PR and resurrect the false ``blocked:pr-closed`` path. So ``all``
        bypasses the single-PR cache and fetches the complete set from the API.
        """
        # Prime the by-issue cache with an OLDER closed PR.
        cache.set_pr_by_issue(
            42,
            {
                "number": 10,
                "title": "#42: Older closed",
                "url": "https://github.com/owner/repo/pull/10",
                "branch": "42-feature",
                "body": "",
                "state": "closed",
                "labels": [],
                "issue_number": 42,
            },
            branch="42-feature",
        )
        # The authoritative GitHub view also has a NEWER open PR.
        mock_http_client.get_prs_for_issue.return_value = [
            {"number": 10}, {"number": 11},
        ]
        full_by_number = {
            10: {
                "number": 10,
                "title": "#42: Older closed",
                "html_url": "https://github.com/owner/repo/pull/10",
                "head": {"ref": "42-feature"},
                "body": "",
                "state": "closed",
                "labels": [],
            },
            11: {
                "number": 11,
                "title": "#42: Newer open",
                "html_url": "https://github.com/owner/repo/pull/11",
                "head": {"ref": "42-feature-2"},
                "body": "",
                "state": "open",
                "labels": [],
            },
        }
        mock_http_client.get_pr.side_effect = lambda n: full_by_number[n]

        prs = adapter.get_prs_for_issue(42, state="all")

        # Cache was bypassed: the API list was fetched...
        mock_http_client.get_prs_for_issue.assert_called_once_with(42)
        # ...and the open PR the reconciler needs to see is in the result.
        assert {pr.number for pr in prs} == {10, 11}
        assert any(pr.state == "open" for pr in prs)

    def test_get_prs_for_issue_from_api(self, adapter, mock_http_client):
        """Test getting PRs for issue from API."""
        mock_http_client.get_prs_for_issue.return_value = [
            {
                "number": 10,
                "title": "#42: Test",
                "html_url": "https://github.com/owner/repo/pull/10",
                "head": {"ref": "42-feature"},
                "body": "",
                "state": "open",
                "labels": [],
            },
        ]
        # Need to mock the full PR fetch
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "#42: Test",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "42-feature"},
            "body": "",
            "state": "open",
            "labels": [],
        }

        prs = adapter.get_prs_for_issue(42)

        assert len(prs) == 1
        assert prs[0].number == 10

    def test_get_prs_for_issue_open_filters_out_closed(
        self, adapter, mock_http_client
    ):
        """The documented ``state`` filter is honored on the API path.

        The broad association search returns PRs in any state; a ``state="open"``
        query must exclude a closed PR. Regression for the stack work-gate base
        selection (#6595 F2), which relied on this filter to avoid launching a
        successor from a closed predecessor PR.
        """
        mock_http_client.get_prs_for_issue.return_value = [
            {"number": 10}, {"number": 11},
        ]
        full_by_number = {
            10: {
                "number": 10,
                "title": "#42: Old closed",
                "html_url": "https://github.com/owner/repo/pull/10",
                "head": {"ref": "42-old"},
                "body": "",
                "state": "closed",
                "labels": [],
            },
            11: {
                "number": 11,
                "title": "#42: New open",
                "html_url": "https://github.com/owner/repo/pull/11",
                "head": {"ref": "42-new"},
                "body": "",
                "state": "open",
                "labels": [],
            },
        }
        mock_http_client.get_pr.side_effect = lambda n: full_by_number[n]

        prs = adapter.get_prs_for_issue(42, state="open")

        assert [pr.number for pr in prs] == [11]
        assert prs[0].state == "open"

    def test_search_pr_refs_for_issue_is_search_only_no_hydration(
        self, adapter, mock_http_client
    ):
        """Refs are built from the search items themselves — the marker lives in
        the body the search already returns — so there is no per-PR ``get_pr``
        fan-out. This is the cheap lookup the retrospective prior-PR resolver
        relies on (one GitHub call regardless of candidate count)."""
        mock_http_client.get_prs_for_issue.return_value = [
            {
                "number": 511,
                "title": "Manual",
                "html_url": "https://github.com/owner/repo/pull/511",
                "body": "hand-written, no marker",
                "state": "closed",
            },
            {
                "number": 512,
                "title": "Orchestrator",
                "html_url": "https://github.com/owner/repo/pull/512",
                "body": "Generated by issue-orchestrator\nwork",
                "state": "closed",
            },
        ]

        refs = adapter.search_pr_refs_for_issue(42)

        assert [r.number for r in refs] == [511, 512]
        assert refs[1].url == "https://github.com/owner/repo/pull/512"
        assert refs[1].body == "Generated by issue-orchestrator\nwork"
        mock_http_client.get_prs_for_issue.assert_called_once_with(42)
        mock_http_client.get_pr.assert_not_called()

    def test_get_prs_for_issue_error_propagates(self, adapter, mock_http_client):
        """Issue PR lookup preserves upstream HTTP failures."""
        mock_http_client.get_prs_for_issue.side_effect = GitHubHttpError(
            "API error",
            status_code=503,
        )

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.get_prs_for_issue(42)
        assert exc_info.value.status_code == 503

    def test_get_prs_with_label_graphql(self, adapter, mock_http_client):
        """Test getting PRs with a specific label via GraphQL (primary path)."""
        mock_http_client.get_prs_with_label_graphql.return_value = [
            {
                "number": 10,
                "title": "Test PR",
                "html_url": "https://github.com/owner/repo/pull/10",
                "head": {"ref": "feature"},
                "body": "",
                "state": "open",
                "labels": [{"name": "bug"}],
            },
        ]

        prs = adapter.get_prs_with_label("bug")

        assert len(prs) == 1
        assert "bug" in prs[0].labels
        mock_http_client.get_prs_with_label_graphql.assert_called_once_with("bug", state="open")
        # REST fallback should NOT be called
        mock_http_client.get_prs_with_label.assert_not_called()

    def test_get_prs_with_label_graphql_http_error_propagates(self, adapter, mock_http_client):
        """GraphQL PR label lookup preserves upstream HTTP failures."""
        mock_http_client.get_prs_with_label_graphql.side_effect = GitHubHttpError(
            "GitHub unavailable",
            status_code=503,
        )

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.get_prs_with_label("bug")

        assert exc_info.value.status_code == 503
        mock_http_client.get_prs_with_label.assert_not_called()

    def test_get_prs_with_label_graphql_transport_error_propagates(self, adapter, mock_http_client):
        """GraphQL PR label lookup preserves upstream transport failures."""
        mock_http_client.get_prs_with_label_graphql.side_effect = GitHubTransportError(
            "connection failed",
        )

        with pytest.raises(GitHubTransportError):
            adapter.get_prs_with_label("bug")

        mock_http_client.get_prs_with_label.assert_not_called()

    def test_get_prs_with_label(self, adapter, mock_http_client):
        """Test getting PRs with a specific label via REST fallback."""
        # GraphQL fails, triggering REST fallback
        mock_http_client.get_prs_with_label_graphql.side_effect = Exception("GraphQL unavailable")
        mock_http_client.get_prs_with_label.return_value = [
            {
                "number": 10,
                "title": "Test PR",
                "html_url": "https://github.com/owner/repo/pull/10",
                "head": {"ref": "feature"},
                "body": "",
                "state": "open",
                "labels": [{"name": "bug"}],
            },
        ]
        # Mock full PR fetch for _fetch_pr_info_from_search
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature"},
            "body": "",
            "state": "open",
            "labels": [{"name": "bug"}],
        }

        prs = adapter.get_prs_with_label("bug")

        assert len(prs) == 1
        assert "bug" in prs[0].labels

    def test_get_prs_with_label_fallback_error_propagates(self, adapter, mock_http_client):
        """REST fallback preserves upstream HTTP failures."""
        mock_http_client.get_prs_with_label_graphql.side_effect = Exception("GraphQL unavailable")
        mock_http_client.get_prs_with_label.side_effect = GitHubHttpError(
            "API error",
            status_code=503,
        )

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.get_prs_with_label("bug")
        assert exc_info.value.status_code == 503

    def test_create_pr_success(self, adapter, mock_http_client, mock_verification_service):
        """Test creating a new PR."""
        # No existing PR
        mock_http_client.get_prs_for_branch.return_value = []
        mock_http_client.create_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature"},
            "body": "PR body",
            "state": "open",
            "labels": [],
        }
        # Mock get_pr for verification
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature"},
            "body": "PR body",
            "state": "open",
            "labels": [],
        }

        pr = adapter.create_pr(
            title="Test PR",
            body="PR body",
            head="feature",
            base="main",
        )

        assert pr.number == 10
        assert pr.title == "Test PR"
        mock_http_client.create_pr.assert_called_once_with(
            title="Test PR",
            body="PR body",
            head="feature",
            base="main",
            draft=None,
        )
        # Should verify PR creation
        mock_verification_service.verify_condition.assert_called_once()

    def test_create_pr_returns_existing_if_present(self, adapter, mock_http_client, cache):
        """Test create_pr returns existing PR if one exists for the branch."""
        # Mock get_prs_for_branch to return existing PR via cache
        cache.set_pr_by_branch("feature", {
            "number": 10,
            "title": "Existing PR",
            "url": "https://github.com/owner/repo/pull/10",
            "branch": "feature",
            "body": "",
            "state": "open",
            "labels": [],
        })

        pr = adapter.create_pr(title="New PR", body="Body", head="feature")

        assert pr.number == 10
        assert pr.title == "Existing PR"
        # Should not call create API
        mock_http_client.create_pr.assert_not_called()

    def test_add_comment_success(self, adapter, mock_http_client, mock_verification_service):
        """Test successfully adding a comment."""
        mock_http_client.add_comment.return_value = "https://github.com/owner/repo/issues/42#comment"
        mock_http_client.get_issue_comments.return_value = [
            {"body": "Test comment"}
        ]

        url = adapter.add_comment(42, "Test comment")

        assert url == "https://github.com/owner/repo/issues/42#comment"
        mock_http_client.add_comment.assert_called_once_with(42, "Test comment")
        # Should verify comment was added
        mock_verification_service.verify_condition.assert_called_once()

    def test_close_pr(self, adapter, mock_http_client, mock_verification_service):
        """Test closing a PR."""
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature"},
            "body": "",
            "state": "closed",
            "labels": [],
        }

        adapter.close_pr(10)

        mock_http_client.close_pr.assert_called_once_with(10)
        mock_verification_service.verify_condition.assert_called_once()

    def test_get_pr_reviews_delegates_to_client(self, adapter, mock_http_client):
        """Test getting PR reviews delegates to HTTP client."""
        mock_http_client.get_pr_reviews.return_value = [
            {"id": 1, "state": "CHANGES_REQUESTED", "body": "Add tests", "user": {"login": "alice"}},
            {"id": 2, "state": "APPROVED", "body": "LGTM", "user": {"login": "bob"}},
        ]

        reviews = adapter.get_pr_reviews(42)

        mock_http_client.get_pr_reviews.assert_called_once_with(42)
        assert len(reviews) == 2
        assert reviews[0]["state"] == "CHANGES_REQUESTED"
        assert reviews[1]["state"] == "APPROVED"

    def test_invalidate_pr_cache(self, adapter, cache):
        """Test invalidating PR cache by issue and branch."""
        pr_data = {
            "number": 10,
            "branch": "feature",
            "issue_number": 42,
        }
        cache.set_pr_by_issue(42, pr_data, branch="feature")

        adapter.invalidate_pr_cache(issue_number=42)

        assert cache.get_pr_by_issue(42) is None

    def test_pr_caching_through_public_api(self, adapter, cache, mock_http_client):
        """Test that PR info gets cached when fetched through public API."""
        mock_http_client.get_prs_for_branch.return_value = [{
            "number": 10,
            "title": "#42: Test",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "42-feature"},
            "body": "",
            "state": "open",
            "labels": [{"name": "bug"}],
        }]

        # Fetch through public API
        prs = adapter.get_prs_for_branch("42-feature")

        assert len(prs) == 1
        assert prs[0].number == 10

        # PR should be cached (extracted issue number from branch "42-feature")
        cached = cache.get_pr_by_issue(42)
        assert cached is not None
        assert cached["number"] == 10


class TestCacheBehavior:
    """Test caching behavior."""

    def test_cache_disabled_no_caching(self, mock_config, mock_http_client, mock_verification_service):
        """Test that cache is bypassed when disabled."""
        mock_config.github_cache_ttl_seconds = 0
        adapter = GitHubAdapter(
            repo="owner/repo",
            config=mock_config,
            http_client=mock_http_client,
            verification_service=mock_verification_service,
        )
        mock_http_client.get_issue_labels.return_value = ["bug"]

        # First call
        labels1 = adapter.get_issue_labels(42)
        # Second call
        labels2 = adapter.get_issue_labels(42)

        # Should call API both times (no caching)
        assert mock_http_client.get_issue_labels.call_count == 2
        assert labels1 == labels2

    def test_pr_info_from_cache_via_get_prs_for_branch(self, adapter, cache):
        """Test that cached PR data is correctly converted when fetching PRs."""
        # Pre-populate cache with PR data
        cached = {
            "number": 10,
            "title": "Test PR",
            "url": "https://github.com/owner/repo/pull/10",
            "branch": "feature",
            "body": "Body",
            "state": "open",
            "labels": ["bug", "feature"],
        }
        cache.set_pr_by_branch("feature", cached)

        # Fetch through public API - should use cache
        prs = adapter.get_prs_for_branch("feature")

        assert len(prs) == 1
        pr_info = prs[0]
        assert pr_info.number == 10
        assert pr_info.title == "Test PR"
        assert pr_info.branch == "feature"
        assert pr_info.labels == ["bug", "feature"]

    def test_merged_pr_cache_round_trip_preserves_merged_at(
        self, adapter, cache
    ):
        """F3 regression: merged_at is close-on-merge transition evidence; the
        port contract must not depend on whether the adapter hit its cache. A
        merged PR cached and re-read through the public API keeps its merge
        timestamp."""
        from issue_orchestrator.ports.pull_request_tracker import PRInfo

        merged = PRInfo(
            number=69,
            title="audit scaffold",
            url="https://github.com/owner/repo/pull/69",
            branch="audit-scaffold",
            body="",
            state="merged",
            labels=[],
            merged_at="2026-08-03T13:52:09Z",
        )
        adapter._adapter_cache.cache_pr_info(merged)  # noqa: SLF001 - seed cache

        prs = adapter.get_prs_for_branch("audit-scaffold", state="merged")

        assert len(prs) == 1
        assert prs[0].state == "merged"
        assert prs[0].merged_at == "2026-08-03T13:52:09Z"

    def test_empty_cache_returns_no_prs(self, adapter, mock_http_client):
        """Test that empty cache falls through to API."""
        mock_http_client.get_prs_for_branch.return_value = []

        prs = adapter.get_prs_for_branch("nonexistent")

        assert prs == []
        mock_http_client.get_prs_for_branch.assert_called_once()

    def test_issue_number_extraction_from_branch(self, adapter, cache, mock_http_client):
        """Test that issue number is extracted from branch name for caching."""
        # When a PR with branch "42-feature" is fetched, it should be cached by issue 42
        mock_http_client.get_prs_for_branch.return_value = [{
            "number": 10,
            "title": "Test PR",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "42-feature"},
            "body": "",
            "state": "open",
            "labels": [],
        }]

        adapter.get_prs_for_branch("42-feature")

        # Should be cached by issue 42
        cached = cache.get_pr_by_issue(42)
        assert cached is not None
        assert cached["number"] == 10

    def test_issue_number_extraction_from_title(self, adapter, cache, mock_http_client):
        """Test that issue number is extracted from PR title for caching."""
        # When a PR with title "#123: Feature" is fetched, it should be cached by issue 123
        mock_http_client.get_prs_for_branch.return_value = [{
            "number": 20,
            "title": "#123: Feature",
            "html_url": "https://github.com/owner/repo/pull/20",
            "head": {"ref": "feature-branch"},  # No issue number in branch
            "body": "",
            "state": "open",
            "labels": [],
        }]

        adapter.get_prs_for_branch("feature-branch")

        # Should be cached by issue 123 (from title)
        cached = cache.get_pr_by_issue(123)
        assert cached is not None
        assert cached["number"] == 20

    def test_no_issue_number_caches_by_branch(self, adapter, cache, mock_http_client):
        """Test that PRs without issue number are cached by branch only."""
        mock_http_client.get_prs_for_branch.return_value = [{
            "number": 30,
            "title": "Random PR",
            "html_url": "https://github.com/owner/repo/pull/30",
            "head": {"ref": "random-branch"},
            "body": "",
            "state": "open",
            "labels": [],
        }]

        adapter.get_prs_for_branch("random-branch")

        # Should be cached by branch since no issue number found
        cached = cache.get_pr_by_branch("random-branch")
        assert cached is not None
        assert cached["number"] == 30


class TestWriteVerification:
    """Test write verification behavior through public API."""

    def test_verification_disabled_skips_check(self, mock_config, mock_http_client, mock_verification_service, cache):
        """Test that verification is skipped when disabled via constructor."""
        adapter = GitHubAdapter(
            repo="owner/repo",
            config=mock_config,
            cache=cache,
            verification_service=mock_verification_service,
            http_client=mock_http_client,
            verify_writes=False,
        )
        mock_http_client.get_issue_labels.return_value = ["bug"]

        # Perform a write operation
        adapter.add_label(42, "bug")

        # Verification should not be called
        mock_verification_service.verify_condition.assert_not_called()

    def test_verification_success_completes_normally(self, adapter, mock_verification_service, mock_http_client):
        """Test successful write verification completes without error."""
        mock_verification_service.verify_condition.return_value = (VerificationResult.SUCCESS, None)
        mock_http_client.get_issue_labels.return_value = ["bug"]

        # Should not raise
        adapter.add_label(42, "bug")

        mock_verification_service.verify_condition.assert_called_once()

    def test_verification_timeout_raises_systemic_error(self, adapter, mock_verification_service, mock_http_client):
        """Test that timeout raises systemic error on write operations."""
        mock_verification_service.verify_condition.return_value = (VerificationResult.TIMED_OUT, None)

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.add_label(42, "bug")

        assert exc_info.value.is_systemic()
        assert "Timed out verifying write" in str(exc_info.value)

    def test_verification_failed_raises_issue_local_error(self, adapter, mock_verification_service, mock_http_client):
        """Test that verification failure raises issue-local error."""
        mock_verification_service.verify_condition.return_value = (VerificationResult.FAILED_FATAL, "state")

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.add_label(42, "bug")

        assert exc_info.value.is_issue_local()
        assert exc_info.value.issue_number == 42
        assert "Failed to verify write" in str(exc_info.value)


class TestRepositoryOperations:
    """Test repository-related operations."""

    def test_get_dependency_issue_snapshot_same_repo_fetches_once(self, adapter, mock_http_client):
        """Dependency snapshot reads state and milestone from one issue payload."""
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "state": "open",
            "title": "Test",
            "labels": [],
            "milestone": {"title": "M1"},
        }

        snapshot = adapter.get_dependency_issue_snapshot(42)

        assert snapshot == DependencyIssueSnapshot(state="open", milestone="M1")
        mock_http_client.get_issue.assert_called_once_with(42)

    def test_get_dependency_issue_snapshot_different_repo_fetches_once(self, adapter):
        """Cross-repo dependency snapshots use one temp-client issue read."""
        with patch("issue_orchestrator.adapters.github.github_adapter.GitHubHttpClient") as mock_client_class:
            mock_temp_client = MagicMock()
            mock_client_class.return_value = mock_temp_client
            mock_temp_client.get_issue.return_value = {
                "number": 42,
                "state": "closed",
                "title": "Test",
                "labels": [],
                "milestone": {"title": "M0"},
            }

            snapshot = adapter.get_dependency_issue_snapshot(42, repo="other/repo")

            assert snapshot == DependencyIssueSnapshot(state="closed", milestone="M0")
            mock_temp_client.get_issue.assert_called_once_with(42)
            mock_temp_client.close.assert_called_once()

    def test_get_issue_state_same_repo(self, adapter, mock_http_client):
        """Test getting issue state from same repo."""
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "state": "open",
            "title": "Test",
            "labels": [],
        }

        state = adapter.get_issue_state(42)

        assert state == "open"
        mock_http_client.get_issue.assert_called_once_with(42)

    def test_get_issue_state_different_repo(self, adapter, mock_http_client):
        """Test getting issue state from different repo creates temp client."""
        # Mock the temp client creation
        with patch("issue_orchestrator.adapters.github.github_adapter.GitHubHttpClient") as mock_client_class:
            mock_temp_client = MagicMock()
            mock_client_class.return_value = mock_temp_client
            mock_temp_client.get_issue.return_value = {
                "number": 42,
                "state": "closed",
                "title": "Test",
                "labels": [],
            }

            state = adapter.get_issue_state(42, repo="other/repo")

            assert state == "closed"
            mock_temp_client.get_issue.assert_called_once_with(42)
            mock_temp_client.close.assert_called_once()

    def test_get_issue_state_not_found_returns_none(self, adapter, mock_http_client):
        """Test get_issue_state returns None when issue not found."""
        mock_http_client.get_issue.side_effect = GitHubHttpError("Not found", status_code=404)

        state = adapter.get_issue_state(999)

        assert state is None

    def test_get_issue_state_http_error_propagates(self, adapter, mock_http_client):
        """Dependency issue state checks preserve upstream failures."""
        mock_http_client.get_issue.side_effect = GitHubHttpError("API error", status_code=500)

        with pytest.raises(GitHubHttpError) as exc_info:
            adapter.get_issue_state(999)
        assert exc_info.value.status_code == 500

    def test_create_issue_key_with_external_id(self, adapter, mock_http_client):
        """Test creating issue key with external ID from title."""
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "title": "[M1-011] Test Issue",
            "labels": [],
            "state": "open",
        }

        key = adapter.create_issue_key(42)

        assert isinstance(key, GitHubIssueKey)
        assert key.external_id == "M1-011"
        assert key.repo == "owner/repo"

    def test_create_issue_key_fallback_to_number(self, adapter, mock_http_client):
        """Test creating issue key falls back to number if no external ID."""
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "title": "Test Issue",
            "labels": [],
            "state": "open",
        }

        key = adapter.create_issue_key(42)

        assert key.external_id == "42"

    def test_create_issue_key_when_issue_not_found(self, adapter, mock_http_client):
        """Test creating issue key when issue fetch fails."""
        mock_http_client.get_issue.return_value = None

        key = adapter.create_issue_key(42)

        # Should fall back to issue number
        assert key.external_id == "42"

    def test_list_branches(self, adapter, mock_http_client):
        """Test listing branches."""
        mock_http_client.list_branches.return_value = ["main", "feature", "bugfix"]

        branches = adapter.list_branches()

        assert branches == ["main", "feature", "bugfix"]

    def test_branch_exists(self, adapter, mock_http_client):
        """Test checking if branch exists."""
        mock_http_client.branch_exists.return_value = True

        exists = adapter.branch_exists("feature")

        assert exists is True
        mock_http_client.branch_exists.assert_called_once_with("feature")

    def test_delete_branch(self, adapter, mock_http_client, mock_verification_service):
        """Test deleting a branch."""
        mock_http_client.branch_exists.return_value = False

        adapter.delete_branch("feature")

        mock_http_client.delete_branch.assert_called_once_with("feature")
        mock_verification_service.verify_condition.assert_called_once()

    def test_update_issue_state(self, adapter, mock_http_client, mock_verification_service):
        """Test updating issue state."""
        mock_http_client.get_issue.return_value = {
            "number": 42,
            "state": "closed",
            "title": "Test",
            "labels": [],
        }

        adapter.update_issue_state(42, "closed")

        mock_http_client.update_issue_state.assert_called_once_with(42, "closed")
        mock_verification_service.verify_condition.assert_called_once()

    def test_get_issue_comments(self, adapter, mock_http_client):
        """Test getting issue comments."""
        mock_http_client.get_issue_comments.return_value = [
            {"id": 1, "body": "Comment 1"},
            {"id": 2, "body": "Comment 2"},
        ]

        comments = adapter.get_issue_comments(42)

        assert len(comments) == 2
        assert comments[0]["body"] == "Comment 1"

    def test_issue_comment_marker_present_forwards_to_client(
        self, adapter, mock_http_client
    ):
        """The marker-presence read delegates to the paginating client method
        (the one that scans all comment pages), not a first-page-only read."""
        mock_http_client.issue_comment_marker_present.return_value = True

        present = adapter.issue_comment_marker_present(42, "<!-- io:marker -->")

        assert present is True
        mock_http_client.issue_comment_marker_present.assert_called_once_with(
            42, "<!-- io:marker -->"
        )


    def test_issue_closed_on_or_after_forwards_to_client(
        self, adapter, mock_http_client
    ):
        """The close-event evidence read delegates to the paginating client
        method so callers get the fail-loud full-scan contract."""
        mock_http_client.issue_closed_on_or_after.return_value = True

        closed = adapter.issue_closed_on_or_after(45, "2026-08-03T13:52:09Z")

        assert closed is True
        mock_http_client.issue_closed_on_or_after.assert_called_once_with(
            45, "2026-08-03T13:52:09Z"
        )

    def test_list_labels(self, adapter, mock_http_client):
        """Test listing repository labels."""
        mock_http_client.list_labels.return_value = [
            {"name": "bug", "color": "ff0000"},
            {"name": "feature", "color": "00ff00"},
        ]

        labels = adapter.list_labels()

        assert len(labels) == 2
        assert labels[0]["name"] == "bug"

    def test_create_label(self, adapter, mock_http_client, mock_verification_service):
        """Test creating a label."""
        mock_http_client.list_labels.return_value = [
            {"name": "new-label", "color": "ededed"},
        ]

        adapter.create_label("new-label", color="ededed", description="Test label")

        mock_http_client.create_label.assert_called_once_with(
            "new-label",
            color="ededed",
            description="Test label",
            force=False,
        )
        mock_verification_service.verify_condition.assert_called_once()

    def test_create_label_force_updates_existing(self, adapter, mock_http_client, mock_verification_service):
        """Test creating label with force updates existing label."""
        mock_http_client.list_labels.return_value = [
            {"name": "existing", "color": "000000"},
        ]

        adapter.create_label("existing", color="ffffff", force=True)

        mock_http_client.create_label.assert_called_once_with(
            "existing",
            color="ffffff",
            description=None,
            force=True,
        )

    def test_delete_label(self, adapter, mock_http_client, mock_verification_service):
        """Test deleting a label."""
        mock_http_client.list_labels.return_value = []

        adapter.delete_label("old-label")

        mock_http_client.delete_label.assert_called_once_with("old-label")
        mock_verification_service.verify_condition.assert_called_once()

    def test_get_rate_limit_snapshot(self, adapter, mock_http_client):
        """Test getting rate limit snapshot."""
        from issue_orchestrator.adapters.github.http_client import GitHubRateLimitSnapshot

        mock_snapshot = GitHubRateLimitSnapshot(
            core_remaining=5000,
            core_limit=5000,
            core_reset=1234567890,
            search_remaining=30,
            search_limit=30,
            search_reset=1234567890,
            graphql_remaining=5000,
            graphql_limit=5000,
            graphql_reset=1234567890,
        )
        mock_http_client.get_rate_limit_snapshot.return_value = mock_snapshot

        snapshot = adapter.get_rate_limit_snapshot()

        assert snapshot is not None
        assert snapshot["core"]["remaining"] == 5000

    def test_get_rate_limit_snapshot_none(self, adapter, mock_http_client):
        """Test getting rate limit snapshot returns None when not available."""
        mock_http_client.get_rate_limit_snapshot.return_value = None

        snapshot = adapter.get_rate_limit_snapshot()

        assert snapshot is None

    def test_get_token_scopes(self, adapter, mock_http_client):
        """Test getting token scopes."""
        mock_http_client.get_token_scopes.return_value = ["repo", "read:org"]

        scopes = adapter.get_token_scopes()

        assert scopes == ["repo", "read:org"]


class TestEdgeCases:
    """Test edge cases and error handling through public API."""

    def test_get_pr_handles_missing_number(self, adapter, mock_http_client):
        """Test get_pr handles API response with missing number."""
        mock_http_client.get_pr.return_value = {
            "number": None,
            "title": "Test",
            "html_url": "https://github.com/owner/repo/pull/0",
            "head": {"ref": "feature"},
            "body": "",
            "state": "open",
            "labels": [],
        }

        pr_info = adapter.get_pr(0)

        assert pr_info.number == 0

    def test_get_pr_handles_invalid_number(self, adapter, mock_http_client):
        """Test get_pr handles API response with invalid number type."""
        mock_http_client.get_pr.return_value = {
            "number": "not-a-number",
            "title": "Test",
            "html_url": "https://github.com/owner/repo/pull/0",
            "head": {"ref": "feature"},
            "body": "",
            "state": "open",
            "labels": [],
        }

        pr_info = adapter.get_pr(0)

        assert pr_info.number == 0

    def test_get_pr_handles_invalid_labels(self, adapter, mock_http_client):
        """Test get_pr handles API response with invalid labels."""
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature"},
            "body": "",
            "state": "open",
            "labels": [
                {"name": "bug"},
                "invalid-label",  # Not a dict
                {"not_name": "feature"},  # Missing 'name' key
            ],
        }

        pr_info = adapter.get_pr(10)

        # Should only include valid labels
        assert pr_info.labels == ["bug"]

    def test_get_pr_uses_headRefName_fallback(self, adapter, mock_http_client):
        """Test get_pr uses headRefName when head.ref not available."""
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {},
            "headRefName": "fallback-branch",
            "body": "",
            "state": "open",
            "labels": [],
        }

        pr_info = adapter.get_pr(10)

        assert pr_info.branch == "fallback-branch"

    def test_get_prs_with_label_handles_invalid_search_results(self, adapter, mock_http_client):
        """Test get_prs_with_label handles invalid search result items."""
        # GraphQL fails, triggering REST fallback
        mock_http_client.get_prs_with_label_graphql.side_effect = Exception("GraphQL unavailable")
        # Return mix of valid and invalid items
        mock_http_client.get_prs_with_label.return_value = [
            "not-a-dict",  # Invalid - not a dict
            {},  # Invalid - missing number
            {"number": 10},  # Valid - has number
        ]
        # Mock get_pr for the valid item
        mock_http_client.get_pr.return_value = {
            "number": 10,
            "title": "Test",
            "html_url": "https://github.com/owner/repo/pull/10",
            "head": {"ref": "feature"},
            "body": "",
            "state": "open",
            "labels": [{"name": "bug"}],
        }

        prs = adapter.get_prs_with_label("bug")

        # Should only include the valid PR
        assert len(prs) == 1
        assert prs[0].number == 10

    def test_get_prs_with_label_handles_fetch_failure(self, adapter, mock_http_client):
        """Test get_prs_with_label handles failure when fetching full PR data."""
        # GraphQL fails, triggering REST fallback
        mock_http_client.get_prs_with_label_graphql.side_effect = Exception("GraphQL unavailable")
        mock_http_client.get_prs_with_label.return_value = [{"number": 10}]
        mock_http_client.get_pr.return_value = None  # Full fetch fails

        prs = adapter.get_prs_with_label("bug")

        # Should return empty list when full fetch fails
        assert prs == []

    def test_list_issues_filters_invalid_items(self, adapter, mock_http_client):
        """Test list_issues filters out non-dict items."""
        mock_http_client.list_issues.return_value = [
            {"number": 1, "title": "Valid", "labels": [], "state": "open"},
            "invalid-item",
            None,
            {"number": 2, "title": "Also valid", "labels": [], "state": "open"},
        ]

        issues = adapter.list_issues()

        # Should only include valid items
        assert len(issues) == 2
        assert issues[0].number == 1
        assert issues[1].number == 2

    def test_list_prs_filters_invalid_items(self, adapter, mock_http_client):
        """Test list_prs filters out non-dict items."""
        mock_http_client.list_prs.return_value = [
            {"number": 10, "title": "Valid", "html_url": "url", "head": {"ref": "feature"}, "body": "", "state": "open", "labels": []},
            "invalid-item",
            {"number": 11, "title": "Also valid", "html_url": "url", "head": {"ref": "feature2"}, "body": "", "state": "open", "labels": []},
        ]

        prs = adapter.list_prs()

        # Should only include valid items
        assert len(prs) == 2

    def test_list_all_labels(self, adapter, mock_http_client):
        """Test listing all labels with pagination."""
        mock_http_client.list_all_labels.return_value = [
            {"name": "label1"},
            {"name": "label2"},
            {"name": "label3"},
        ]

        labels = adapter.list_all_labels()

        assert len(labels) == 3
        assert labels[0]["name"] == "label1"


class TestMergeQueue:
    """Merge queue methods delegate to the HTTP client and coerce results."""

    def test_enqueue_delegates_to_client(self, adapter, mock_http_client):
        adapter.enqueue_to_merge_queue(318)
        mock_http_client.enqueue_pull_request.assert_called_once_with(318)

    def test_read_entry_coerces_typed_entry(self, adapter, mock_http_client):
        mock_http_client.get_merge_queue_entry.return_value = {
            "state": "QUEUED",
            "position": 3,
        }
        read = adapter.read_merge_queue_entry(318)
        assert read == MergeQueueRead.present(
            MergeQueueEntry(state="QUEUED", position=3)
        )

    def test_read_entry_absent_when_not_queued(self, adapter, mock_http_client):
        mock_http_client.get_merge_queue_entry.return_value = None
        read = adapter.read_merge_queue_entry(318)
        assert read == MergeQueueRead.absent()
        # An absent read must NOT look like a present entry.
        assert read.entry is None

    def test_read_entry_unmodeled_state_is_indeterminate(
        self, adapter, mock_http_client
    ):
        # An entry object exists but its state is one we do not model: the PR IS
        # in the queue, so this must be INDETERMINATE (non-actionable), never
        # ABSENT — otherwise a queued PR could be wrongly re-enqueued/reworked.
        mock_http_client.get_merge_queue_entry.return_value = {"state": "BOGUS"}
        read = adapter.read_merge_queue_entry(318)
        assert read == MergeQueueRead.indeterminate()
        assert read.is_indeterminate

    def test_enqueue_propagates_http_error(self, adapter, mock_http_client):
        mock_http_client.enqueue_pull_request.side_effect = GitHubHttpError(
            "boom", status_code=500
        )
        with pytest.raises(GitHubHttpError):
            adapter.enqueue_to_merge_queue(318)


class TestMarkerRecoveryContracts:
    """#6957 round-5 review F13: which lookup runs, and what its "no" means.

    ``find_issue_by_marker`` answers "did a previous process already create this
    issue?" for two callers with different stakes, so it carries two contracts.
    Getting the negative one wrong is what files a second issue for a signature
    that already has one.
    """

    MARKER = "<!-- issue-orchestrator:tech-lead-case-file:v1:abc -->"

    def test_authoritative_absence_is_proven_by_a_title_independent_walk(
        self, adapter, mock_http_client
    ):
        """The exact miss F13 describes: aged out of the window AND retitled."""
        mock_http_client.list_issues.return_value = [
            *({"number": n, "body": "unrelated", "html_url": "u"} for n in range(200)),
            {"number": 999, "body": f"kept its marker {self.MARKER}", "html_url": "u"},
        ]
        # Nothing matches the title the caller remembers.
        mock_http_client.search_issues_by_title.return_value = []

        found = adapter.find_issue_by_marker(
            title="a title that was since edited",
            marker=self.MARKER,
            authoritative=True,
        )

        assert found == 999
        kwargs = mock_http_client.list_issues.call_args.kwargs
        # Fresh, all states, and under the fail-loud completeness contract
        # (#6779 R17) rather than a bounded best-effort read.
        assert kwargs["state"] == "all"
        assert kwargs["use_cache"] is False
        assert kwargs["exhaustive"] is True
        assert kwargs["limit"] > 100
        mock_http_client.search_issues_by_title.assert_not_called()

    def test_an_unprovable_authoritative_scan_raises_rather_than_saying_absent(
        self, adapter, mock_http_client
    ):
        """Cap exhaustion / a later-page failure is "unknown", never "absent"."""
        mock_http_client.list_issues.side_effect = GitHubHttpError(
            "refusing to treat the partial issue set as complete", status_code=500
        )

        with pytest.raises(GitHubHttpError):
            adapter.find_issue_by_marker(
                title="t", marker=self.MARKER, authoritative=True
            )

    def test_the_default_lookup_stays_bounded_and_best_effort(
        self, adapter, mock_http_client
    ):
        """A miss here only risks a duplicate another invariant already blocks,
        so it must not pay for an exhaustive walk on every new case file."""
        mock_http_client.list_issues.return_value = []
        mock_http_client.search_issues_by_title.return_value = []

        assert adapter.find_issue_by_marker(title="t", marker=self.MARKER) is None

        kwargs = mock_http_client.list_issues.call_args.kwargs
        assert kwargs.get("exhaustive", False) is False
        assert kwargs["limit"] == 100
