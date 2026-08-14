"""History, debug, and test-data web route tests split from test_web."""

# ruff: noqa: F403,F405

from tests.unit import test_web as _support
from tests.unit.test_web import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestHistoryEndpoints:
    """Test history management endpoints."""

    def test_configured_attr_ignores_unconfigured_mock_children(self):
        """Dependency probing must not treat MagicMock child mocks as wiring."""
        from issue_orchestrator.entrypoints.web_retry_runtime import configured_attr

        deps = MagicMock()

        assert configured_attr(deps, "session_manager") is None
        _ = deps.session_manager
        assert configured_attr(deps, "session_manager") is None

        session_manager = Mock()
        deps.session_manager = session_manager

        assert configured_attr(deps, "session_manager") is session_manager

    def test_configured_attr_supports_slotted_runtime_objects(self):
        """Real slotted runtime collaborators still support explicit lookup."""
        from issue_orchestrator.entrypoints.web_retry_runtime import configured_attr

        class SlottedDeps:
            __slots__ = ("session_manager",)

            def __init__(self, session_manager):
                self.session_manager = session_manager

        session_manager = object()

        assert (
            configured_attr(SlottedDeps(session_manager), "session_manager")
            is session_manager
        )

    def test_clear_history_success(self):
        """Test clearing all history."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add some history entries
        entry1 = SessionHistoryEntry(
            issue_number=1,
            title="Issue 1",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=10,
        )
        entry2 = SessionHistoryEntry(
            issue_number=2,
            title="Issue 2",
            agent_type="agent:web",
            status="failed",
            runtime_minutes=5,
        )
        mock_orch.state.session_history = [entry1, entry2]
        mock_orch.state.completed_today = [1, 2]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/history/clear")

        assert response.status_code == 200
        assert response.json()["cleared"] == 2
        assert len(mock_orch.state.session_history) == 0
        assert len(mock_orch.state.completed_today) == 0

    def test_dismiss_history_entry_success(self):
        """Test dismissing a single history entry."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        entry1 = SessionHistoryEntry(
            issue_number=1,
            title="Issue 1",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=10,
        )
        entry2 = SessionHistoryEntry(
            issue_number=2,
            title="Issue 2",
            agent_type="agent:web",
            status="failed",
            runtime_minutes=5,
        )
        mock_orch.state.session_history = [entry1, entry2]
        mock_orch.state.completed_today = [1, 2]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/history/dismiss/1")

        assert response.status_code == 200
        assert response.json()["dismissed"] == 1
        assert len(mock_orch.state.session_history) == 1
        assert mock_orch.state.session_history[0].issue_number == 2
        assert 1 not in mock_orch.state.completed_today

    def test_retry_issue_success(self):
        """Test retrying an issue."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        entry = SessionHistoryEntry(
            issue_number=1,
            title="Issue 1",
            agent_type="agent:web",
            status="failed",
            runtime_minutes=10,
        )
        mock_orch.state.session_history = [entry]
        mock_orch.state.completed_today = [1]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/retry/1")

        assert response.status_code == 200
        assert response.json()["retrying"] == 1
        assert len(mock_orch.state.session_history) == 0
        assert 1 not in mock_orch.state.completed_today

    def test_unblock_retry_removes_blocking_and_pr_pending_labels(self):
        """Unblock endpoint removes all labels that prevent scheduling."""
        mock_orch = create_mock_orchestrator()
        lm = LabelManager(mock_orch.config)
        mock_orch.deps.label_manager = lm
        mock_orch.deps.action_applier = MagicMock()
        mock_orch.deps.action_applier.apply.return_value = Mock(success=True, error=None)
        mock_orch.repository_host.get_issue_labels.return_value = [
            "agent:web",
            lm.blocked,
            lm.pr_pending,
        ]
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=4057,
                title="Issue 4057",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=5,
            ),
        ]
        mock_orch.state.completed_today = [4057]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/unblock-retry", json={"issues": [4057]})

        assert response.status_code == 200
        assert response.json()["unblocked"] == [4057]
        removed = [call.args[0].label for call in mock_orch.deps.action_applier.apply.call_args_list]
        assert lm.blocked in removed
        assert lm.pr_pending in removed
        assert all(entry.issue_number != 4057 for entry in mock_orch.state.session_history)
        assert 4057 not in mock_orch.state.completed_today
        mock_orch.request_refresh.assert_called_once()

    def test_reset_retry_sets_pending_label_and_queues_immediately(self):
        """Reset+retry should persist pending state and enqueue without waiting for refresh."""
        from issue_orchestrator.control.maintenance import ResetResult

        mock_orch = create_mock_orchestrator()
        lm = LabelManager(mock_orch.config)
        mock_orch.deps.label_manager = lm
        mock_orch.deps.action_applier = MagicMock()
        mock_orch.deps.action_applier.apply.return_value = Mock(success=True, error=None)
        mock_orch.deps.events = MagicMock()
        mock_orch.deps.queue_cache_store = MagicMock()
        mock_orch.repository_host.get_issue_labels.return_value = [
            "agent:web",
            lm.blocked_failed,
            lm.pr_pending,
        ]
        mock_orch.repository_host.get_issue.return_value = create_issue(
            4057,
            labels=["agent:web", lm.reset_retry_pending],
        )
        mock_orch.state.cached_scope_issues = [
            create_issue(4057, labels=["agent:web", lm.blocked_failed])
        ]
        mock_orch.state.cached_queue_issues = list(mock_orch.state.cached_scope_issues)

        set_orchestrator(mock_orch)

        with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue_mock:
            reset_issue_mock.return_value = ResetResult(
                success=True,
                issue_number=4057,
                deleted_worktree="/tmp/worktree-4057",
                deleted_branch="4057-fix",
                labels_removed=[lm.blocked_failed, lm.pr_pending],
            )
            client = TestClient(app)
            response = client.post("/api/reset-retry", json={"issues": [4057]})

        assert response.status_code == 200
        payload = response.json()
        assert payload["failed"] == []
        assert payload["refresh_triggered"] is False
        assert payload["reset"][0]["issue"] == 4057
        assert payload["reset"][0]["queued_now"] is True
        assert payload["reset"][0]["pending_label"] == lm.reset_retry_pending
        assert mock_orch.state.priority_queue[0] == 4057
        mock_orch.request_refresh.assert_not_called()
        saved_issues, saved_watermark = mock_orch.deps.queue_cache_store.save_snapshot.call_args.args[:2]
        assert saved_watermark == mock_orch.state.queue_delta_watermark
        assert [issue.number for issue in saved_issues] == [4057]
        assert saved_issues[0].labels == ["agent:web", lm.reset_retry_pending]

        added_labels = [call.args[0].label for call in mock_orch.deps.action_applier.apply.call_args_list]
        assert lm.reset_retry_pending in added_labels
        event_arg = mock_orch.deps.events.publish.call_args.args[0]
        assert event_arg.event_type == EventName.ISSUE_UNBLOCKED
        assert event_arg.data["issue_number"] == 4057
        assert event_arg.data["reason"] == "reset_retry_requested"
        assert event_arg.data["source"] == "web.reset-retry"
        assert event_arg.data["pending_labels"] == [lm.reset_retry_pending]
        assert event_arg.data["from_scratch"] is False

    def test_reset_retry_cancels_review_exchange_through_completion_owner(self):
        """Reset delegates review-run teardown to the completion owner."""
        from issue_orchestrator.control.maintenance import ResetResult

        mock_orch = create_mock_orchestrator()
        lm = LabelManager(mock_orch.config)
        pair_registry = Mock()
        job_supervisor = Mock()
        job_supervisor.cancel_matching.return_value = [
            "review-exchange:4057:coding-1"
        ]
        legacy_pair_registry = Mock()
        mock_orch.deps.pair_registry = legacy_pair_registry
        mock_orch.deps.services = SimpleNamespace(
            pair_registry=pair_registry,
            background_job_supervisor=job_supervisor,
        )
        cancel_review_exchange = MagicMock(
            return_value=SimpleNamespace(
                issue_number=4057,
                cancelled_job_ids=("review-exchange:4057:coding-1",),
            )
        )
        mock_orch.deps.completion_processor.cancel_review_exchange_for_issue = (
            cancel_review_exchange
        )
        session_manager = Mock()
        session_manager.exists.side_effect = (
            lambda ref: ref.name in {"issue-4057", "rework-4057"}
        )
        mock_orch.deps.session_manager = session_manager
        mock_orch.deps.label_manager = lm
        mock_orch.deps.action_applier = MagicMock()
        mock_orch.deps.action_applier.apply.return_value = Mock(
            success=True,
            error=None,
        )
        mock_orch.deps.events = MagicMock()
        mock_orch.deps.queue_cache_store = MagicMock()
        mock_orch.repository_host.get_issue_labels.return_value = [
            "agent:web",
            lm.blocked_failed,
        ]
        mock_orch.repository_host.get_issue.return_value = create_issue(
            4057,
            labels=["agent:web", lm.reset_retry_pending],
        )
        mock_orch.state.active_sessions = [
            SimpleNamespace(
                terminal_id="issue-4057",
                issue=SimpleNamespace(number=4057),
            ),
            SimpleNamespace(
                terminal_id="rework-4057",
                issue=SimpleNamespace(number=4057),
            ),
            SimpleNamespace(
                terminal_id="issue-999",
                issue=SimpleNamespace(number=999),
            ),
        ]

        set_orchestrator(mock_orch)

        with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue_mock:
            reset_issue_mock.return_value = ResetResult(
                success=True,
                issue_number=4057,
                deleted_worktree="/tmp/worktree-4057",
                deleted_branch="4057-fix",
                labels_removed=[lm.blocked_failed],
            )
            client = TestClient(app)
            response = client.post("/api/reset-retry", json={"issues": [4057]})

        assert response.status_code == 200
        assert response.json()["failed"] == []
        cancel_review_exchange.assert_called_once_with(4057, "reset-retry")
        pair_registry.release.assert_not_called()
        legacy_pair_registry.release.assert_not_called()
        job_supervisor.cancel_matching.assert_not_called()
        assert [call.args[0].name for call in session_manager.stop.call_args_list] == [
            "issue-4057",
            "rework-4057",
        ]
        assert [session.terminal_id for session in mock_orch.state.active_sessions] == [
            "issue-999",
        ]

    def test_reset_retry_from_scratch_sets_scratch_pending_label(self):
        """Reset+retry from scratch should persist scratch pending label and queue immediately."""
        from issue_orchestrator.control.maintenance import ResetResult

        mock_orch = create_mock_orchestrator()
        lm = LabelManager(mock_orch.config)
        mock_orch.deps.label_manager = lm
        mock_orch.deps.action_applier = MagicMock()
        mock_orch.deps.action_applier.apply.return_value = Mock(success=True, error=None)
        mock_orch.deps.events = MagicMock()
        mock_orch.repository_host.get_issue_labels.return_value = [
            "agent:web",
            lm.blocked_failed,
            lm.pr_pending,
        ]
        mock_orch.repository_host.get_issue.return_value = create_issue(
            4057,
            labels=["agent:web", lm.reset_retry_pending, lm.reset_retry_scratch_pending],
        )

        set_orchestrator(mock_orch)

        with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue_mock:
            reset_issue_mock.return_value = ResetResult(
                success=True,
                issue_number=4057,
                deleted_worktree="/tmp/worktree-4057",
                deleted_branch="4057-fix",
                labels_removed=[lm.blocked_failed, lm.pr_pending],
            )
            client = TestClient(app)
            response = client.post("/api/reset-retry", json={"issues": [4057], "from_scratch": True})

        assert response.status_code == 200
        payload = response.json()
        assert payload["failed"] == []
        assert payload["from_scratch"] is True
        assert payload["reset"][0]["issue"] == 4057
        assert payload["reset"][0]["from_scratch"] is True
        assert lm.reset_retry_pending in payload["reset"][0]["pending_labels"]
        assert lm.reset_retry_scratch_pending in payload["reset"][0]["pending_labels"]
        assert reset_issue_mock.call_args.kwargs["from_scratch"] is True
        assert reset_issue_mock.call_args.kwargs["repository_host"] is mock_orch.repository_host

        added_labels = [call.args[0].label for call in mock_orch.deps.action_applier.apply.call_args_list]
        assert lm.reset_retry_pending in added_labels
        assert lm.reset_retry_scratch_pending in added_labels
        assert lm.retrospective_review not in added_labels
        event_arg = mock_orch.deps.events.publish.call_args.args[0]
        assert event_arg.data["from_scratch"] is True
        assert event_arg.data["source"] == "web.reset-retry"
        assert event_arg.data["pending_labels"] == [
            lm.reset_retry_pending,
            lm.reset_retry_scratch_pending,
        ]

    def test_reset_retry_abandons_publish_retry_work(self):
        """Reset must abandon in-flight publish-retry work on its issue boundary.

        Publish retries run on their own owner/runner outside the review-exchange
        supervisor, so the reset boundary has to tell PublishRecoveryService to
        drop the attempt or a late completion could repopulate it.
        """
        from issue_orchestrator.control.maintenance import ResetResult

        mock_orch = create_mock_orchestrator()
        lm = LabelManager(mock_orch.config)
        mock_orch.deps.label_manager = lm
        mock_orch.deps.action_applier = MagicMock()
        mock_orch.deps.action_applier.apply.return_value = Mock(success=True, error=None)
        mock_orch.deps.events = MagicMock()
        publish_recovery = Mock()
        mock_orch.deps.publish_recovery = publish_recovery
        mock_orch.repository_host.get_issue_labels.return_value = [
            "agent:web",
            lm.publish_failed,
        ]
        mock_orch.repository_host.get_issue.return_value = create_issue(
            4057,
            labels=["agent:web", lm.reset_retry_pending],
        )

        set_orchestrator(mock_orch)

        with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue_mock:
            reset_issue_mock.return_value = ResetResult(
                success=True,
                issue_number=4057,
                deleted_worktree="/tmp/worktree-4057",
                deleted_branch="4057-fix",
                labels_removed=[lm.publish_failed],
            )
            client = TestClient(app)
            response = client.post("/api/reset-retry", json={"issues": [4057]})

        assert response.status_code == 200
        assert response.json()["failed"] == []
        publish_recovery.abandon_issue.assert_called_once_with(4057)

    def test_retrospective_review_preflight_keeps_closed_issue_closed(self):
        """Retrospective review preview should not plan reopen/reset mutations."""
        from issue_orchestrator.domain.models import ORCHESTRATOR_PR_MARKER

        mock_orch = create_mock_orchestrator()
        mock_orch.config.retrospective_review_enabled = True
        mock_orch.config.code_review_agent = "agent:reviewer"
        mock_orch.config.retrospective_review_trigger_label = "lack-of-review-redo"
        mock_orch.config.retrospective_reviewed_label = "lack-of-review-reviewed"
        mock_orch.config.retrospective_changes_requested_label = "lack-of-review-needs-work"
        issue = create_issue(
            4057,
            title="Previously completed implementation",
            labels=["agent:web", "lack-of-review-redo", "blocked-failed"],
        )
        issue.state = "closed"
        mock_orch.repository_host.get_issue.return_value = issue
        mock_orch.repository_host.search_pr_refs_for_issue.return_value = [
            SimpleNamespace(
                number=812,
                url="https://github.com/owner/repo/pull/812",
                body=f"{ORCHESTRATOR_PR_MARKER}\n",
            )
        ]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post(
            "/api/retrospective-review/preflight",
            json={"issues": [4057]},
        )

        assert response.status_code == 200
        assert response.json() == {
            "decisions": [
                {
                    "issue": 4057,
                    "title": "Previously completed implementation",
                    "state": "closed",
                    "labels": ["agent:web", "lack-of-review-redo", "blocked-failed"],
                    "eligible": True,
                    "action": "queue_review",
                    "reason": (
                        "Closed issue will stay closed unless retrospective review "
                        "requests changes"
                    ),
                    "agent_label": "agent:web",
                    "trigger_label": "lack-of-review-redo",
                    "prior_pr_number": 812,
                    "prior_pr_url": "https://github.com/owner/repo/pull/812",
                }
            ],
            "eligible": [4057],
            "skipped": [],
            "workflow": "retrospective_review",
            "trigger_label": "lack-of-review-redo",
        }
        mock_orch.repository_host.update_issue_state.assert_not_called()

    def test_retrospective_review_allows_filtered_issue_without_reopening(self):
        """Normal queue filtering must not block explicit retrospective review."""
        mock_orch = create_mock_orchestrator()
        lm = LabelManager(mock_orch.config)
        mock_orch.config.retrospective_review_enabled = True
        mock_orch.config.code_review_agent = "agent:reviewer"
        mock_orch.config.retrospective_review_trigger_label = "lack-of-review-redo"
        mock_orch.config.filtering.label = "redo-poorly-reviewed"
        mock_orch.deps.label_manager = lm
        mock_orch.deps.action_applier = MagicMock()
        mock_orch.deps.action_applier.apply.return_value = Mock(success=True, error=None)
        mock_orch.deps.events = MagicMock()
        issue = create_issue(4057, labels=["agent:web"])
        issue.state = "closed"
        mock_orch.repository_host.get_issue.return_value = issue
        mock_orch.repository_host.get_prs_for_issue.return_value = []
        mock_orch.repository_host.create_issue_key.return_value = FakeIssueKey("4057")

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post(
            "/api/retrospective-review",
            json={"issues": [4057]},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["failed"] == []
        assert payload["skipped"] == []
        assert payload["queued"][0]["issue"] == 4057
        assert payload["queued"][0]["queued"] is True
        mock_orch.repository_host.update_issue_state.assert_not_called()
        mock_orch.repository_host.get_issue_labels.assert_not_called()
        added_labels = [call.args[0].label for call in mock_orch.deps.action_applier.apply.call_args_list]
        assert "lack-of-review-redo" in added_labels
        assert mock_orch.state.pending_retrospective_reviews[0].issue_number == 4057

    def test_retrospective_review_skips_missing_agent_label(self):
        """Retrospective review should reject issues that cannot launch."""
        mock_orch = create_mock_orchestrator()
        mock_orch.config.retrospective_review_enabled = True
        mock_orch.config.code_review_agent = "agent:reviewer"
        issue = create_issue(4057, labels=[])
        mock_orch.repository_host.get_issue.return_value = issue

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post(
            "/api/retrospective-review/preflight",
            json={"issues": [4057]},
        )

        assert response.status_code == 200
        decision = response.json()["decisions"][0]
        assert decision["eligible"] is False
        assert decision["action"] == "skipped"
        assert "no agent:* label" in decision["reason"]
        mock_orch.repository_host.update_issue_state.assert_not_called()

    def test_retrospective_review_execute_applies_label_without_resetting(self):
        """Eligible issues get the review trigger label and in-memory review queue only."""
        from issue_orchestrator.domain.models import ORCHESTRATOR_PR_MARKER

        mock_orch = create_mock_orchestrator()
        lm = LabelManager(mock_orch.config)
        mock_orch.config.retrospective_review_enabled = True
        mock_orch.config.code_review_agent = "agent:reviewer"
        mock_orch.config.retrospective_review_trigger_label = "lack-of-review-redo"
        mock_orch.deps.label_manager = lm
        mock_orch.deps.action_applier = MagicMock()
        mock_orch.deps.action_applier.apply.return_value = Mock(success=True, error=None)
        mock_orch.deps.events = MagicMock()
        closed_issue = create_issue(
            4057,
            labels=["agent:web", lm.blocked_failed],
        )
        closed_issue.state = "closed"
        mock_orch.repository_host.get_issue.return_value = closed_issue
        mock_orch.repository_host.search_pr_refs_for_issue.return_value = [
            SimpleNamespace(
                number=812,
                url="https://github.com/owner/repo/pull/812",
                body=f"{ORCHESTRATOR_PR_MARKER}\n",
            )
        ]
        mock_orch.repository_host.create_issue_key.return_value = FakeIssueKey("4057")

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post(
            "/api/retrospective-review",
            json={"issues": [4057]},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["skipped"] == []
        assert payload["failed"] == []
        assert payload["queued"][0]["issue"] == 4057
        assert payload["queued"][0]["queued"] is True
        assert payload["queued"][0]["prior_pr_number"] == 812
        assert payload["queued"][0]["prior_pr_url"] == "https://github.com/owner/repo/pull/812"
        assert payload["queued"][0]["labels"] == ["agent:web", lm.blocked_failed]
        assert payload["queued"][0]["state"] == "closed"
        assert payload["trigger_label"] == "lack-of-review-redo"
        mock_orch.repository_host.update_issue_state.assert_not_called()
        mock_orch.repository_host.get_issue_labels.assert_not_called()
        added_labels = [call.args[0].label for call in mock_orch.deps.action_applier.apply.call_args_list]
        assert "lack-of-review-redo" in added_labels
        assert len(mock_orch.state.pending_retrospective_reviews) == 1
        queued = mock_orch.state.pending_retrospective_reviews[0]
        assert queued.issue_key == FakeIssueKey("4057")
        assert queued.issue_number == 4057
        assert queued.issue_title == "Test Issue"
        assert queued.agent_label == "agent:web"
        assert queued.trigger_label == "lack-of-review-redo"
        assert queued.prior_pr_number == 812
        assert queued.prior_pr_url == "https://github.com/owner/repo/pull/812"

    def test_reset_retry_from_scratch_clears_pending_review_rework_and_cleanup_state(self):
        """Scratch reset should remove stale in-memory PR/rework state before requeue."""
        from pathlib import Path

        from issue_orchestrator.adapters.github.github_issue import GitHubIssue
        from issue_orchestrator.control.maintenance import ResetResult
        from issue_orchestrator.domain.models import OrchestratorState, PendingCleanup
        from issue_orchestrator.entrypoints.web_retry_history_routes import _clear_scratch_retry_pending_state

        state = OrchestratorState(
            pending_reviews=[
                PendingReview(
                    issue_key=FakeIssueKey("4057"),
                    pr_number=376,
                    pr_url="https://example/pr/376",
                    branch_name="4057-old",
                    _issue_number=4057,
                ),
                PendingReview(
                    issue_key=FakeIssueKey("999"),
                    pr_number=999,
                    pr_url="https://example/pr/999",
                    branch_name="999-other",
                    _issue_number=999,
                ),
            ],
            pending_reworks=[
                PendingRework(
                    issue_key=FakeIssueKey("4057"),
                    agent_type="agent:web",
                    issue_number=4057,
                    pr_number=376,
                ),
                PendingRework(
                    issue_key=FakeIssueKey("999"),
                    agent_type="agent:web",
                    issue_number=999,
                    pr_number=999,
                ),
            ],
            pending_cleanups=[
                PendingCleanup(
                    issue=GitHubIssue(number=4057, repo="o/r", title="Issue 4057"),
                    pr_number=376,
                    pr_url="https://example/pr/376",
                    branch_name="4057-old",
                    terminal_id="issue-4057",
                    worktree_path=Path("/tmp/issue-4057"),
                ),
                PendingCleanup(
                    issue=GitHubIssue(number=999, repo="o/r", title="Issue 999"),
                    pr_number=999,
                    pr_url="https://example/pr/999",
                    branch_name="999-other",
                    terminal_id="issue-999",
                    worktree_path=Path("/tmp/issue-999"),
                ),
            ],
        )

        _clear_scratch_retry_pending_state(
            state,
            4057,
            ResetResult(success=True, issue_number=4057, superseded_prs=[376]),
        )

        assert [review.pr_number for review in state.pending_reviews] == [999]
        assert [rework.pr_number for rework in state.pending_reworks] == [999]
        assert [cleanup.pr_number for cleanup in state.pending_cleanups] == [999]

    def test_reset_retry_reports_error_when_pending_label_cannot_be_set(self):
        """Reset+retry should fail the issue when pending label persistence fails."""
        from issue_orchestrator.control.maintenance import ResetResult

        mock_orch = create_mock_orchestrator()
        lm = LabelManager(mock_orch.config)
        mock_orch.deps.label_manager = lm
        mock_orch.deps.action_applier = MagicMock()
        mock_orch.deps.action_applier.apply.return_value = Mock(success=False, error="label add failed")
        mock_orch.deps.events = MagicMock()
        mock_orch.repository_host.get_issue_labels.return_value = ["agent:web", lm.blocked_failed]

        set_orchestrator(mock_orch)

        with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue_mock:
            reset_issue_mock.return_value = ResetResult(
                success=True,
                issue_number=4057,
                deleted_worktree="/tmp/worktree-4057",
                deleted_branch="4057-fix",
                labels_removed=[lm.blocked_failed],
            )
            client = TestClient(app)
            response = client.post("/api/reset-retry", json={"issues": [4057]})

        assert response.status_code == 200
        payload = response.json()
        assert payload["reset"] == []
        assert payload["failed"][0]["issue"] == 4057
        assert "label add failed" in payload["failed"][0]["error"]
        assert mock_orch.state.priority_queue == []
        mock_orch.request_refresh.assert_not_called()

    def test_reset_retry_reports_error_when_issue_not_queue_eligible(self):
        """Reset+retry should report when refreshed issue cannot enter queue."""
        from issue_orchestrator.control.maintenance import ResetResult

        mock_orch = create_mock_orchestrator()
        lm = LabelManager(mock_orch.config)
        mock_orch.deps.label_manager = lm
        mock_orch.deps.action_applier = MagicMock()
        mock_orch.deps.action_applier.apply.return_value = Mock(success=True, error=None)
        mock_orch.deps.events = MagicMock()
        mock_orch.repository_host.get_issue_labels.return_value = ["agent:web", lm.blocked_failed]
        mock_orch.repository_host.get_issue.return_value = create_issue(4057, labels=["agent:web"])
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=4057,
                title="Issue 4057",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=1,
            ),
        ]

        set_orchestrator(mock_orch)

        with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue_mock:
            reset_issue_mock.return_value = ResetResult(
                success=True,
                issue_number=4057,
                deleted_worktree="/tmp/worktree-4057",
                deleted_branch="4057-fix",
                labels_removed=[lm.blocked_failed],
            )
            client = TestClient(app)
            response = client.post("/api/reset-retry", json={"issues": [4057]})

        assert response.status_code == 200
        payload = response.json()
        assert payload["reset"] == []
        assert payload["failed"][0]["issue"] == 4057
        assert "not queue-eligible" in payload["failed"][0]["error"]
        assert mock_orch.state.priority_queue == []

    def test_retry_publish_endpoint_submits_manual_publish_retry(self):
        mock_orch = create_mock_orchestrator()
        mock_orch.deps.publish_recovery.retry_publish.return_value = SimpleNamespace(
            status="submitted",
            message="Publish retry queued",
            job_id="job-123",
            pr_url=None,
            pr_number=None,
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/issues/4057/retry-publish")
            assert response.status_code == 200
            payload = response.json()
            assert payload["status"] == "submitted"
            assert payload["job_id"] == "job-123"
            mock_orch.deps.publish_recovery.retry_publish.assert_called_once_with(
                4057,
                mock_orch.state,
            )
        finally:
            set_orchestrator(None)

    def test_retry_publish_endpoint_returns_conflict_when_not_allowed(self):
        mock_orch = create_mock_orchestrator()
        mock_orch.deps.publish_recovery.retry_publish.return_value = SimpleNamespace(
            status="rejected",
            message="Issue is not blocked by a publish failure",
            job_id=None,
            pr_url=None,
            pr_number=None,
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/issues/4057/retry-publish")
            assert response.status_code == 409
            assert response.json()["error"] == "Issue is not blocked by a publish failure"
        finally:
            set_orchestrator(None)

    def test_validation_failure_dialog_endpoint_returns_failed_tests(self, tmp_path: Path):
        from issue_orchestrator.domain.artifact_contracts import ValidationFailed
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-validation-dialog"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "coding-1", issue_number=4057)
        session_output.update_validation_outcome(
            run.run_dir,
            ValidationFailed(reason="Validation failed for deadbeef (exit_code=2)"),
        )
        session_output.update_manifest(
            run.run_dir,
            {
                "validation_record_path": ".issue-orchestrator/sessions/r1/validation-record.json",
                "validation_stdout": ".issue-orchestrator/sessions/r1/validation-stdout.log",
                "validation_stderr": ".issue-orchestrator/sessions/r1/validation-stderr.log",
            },
        )
        (run.run_dir / "validation-record.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "suite": "publish_gate",
                    "head_sha": "deadbeef",
                    "passed": False,
                    "exit_code": 2,
                    "command": "make validate",
                    "started_at": "2026-03-22T04:53:14Z",
                    "ended_at": "2026-03-22T04:53:58Z",
                    "timed_out": False,
                }
            ),
            encoding="utf-8",
        )
        (run.run_dir / "validation-stdout.log").write_text(
            "FAILED tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_open\n",
            encoding="utf-8",
        )
        (run.run_dir / "validation-stderr.log").write_text("make: *** [validate] Error 2\n", encoding="utf-8")
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=4057,
                title="Issue 4057",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=5,
                worktree_path=worktree,
            ),
        ]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/dialog/validation-failure/4057?run_dir={run.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["reason"] == "Validation failed for deadbeef (exit_code=2)"
            assert payload["failed_tests"] == [
                "tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_open"
            ]
            assert payload["summary_rows"][-1] == {"label": "Failing Tests", "value": "1"}
            assert [section["title"] for section in payload["action_sections"]] == [
                "Validation Artifacts",
                "Session Evidence",
                "Diagnostics",
            ]
            diagnostics_actions = payload["action_sections"][-1]["actions"]
            assert any(action.get("type") == "open_session_diagnostics" for action in diagnostics_actions)
            assert "actions" not in payload
        finally:
            set_orchestrator(None)

    def test_get_history_dedupes_to_latest_per_issue(self):
        """History endpoint returns only the latest entry for each issue."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=1,
                title="Issue 1 (old)",
                agent_type="agent:web",
                status="failed",
                runtime_minutes=10,
            ),
            SessionHistoryEntry(
                issue_number=1,
                title="Issue 1 (latest)",
                agent_type="agent:web",
                status="blocked",
                runtime_minutes=3,
            ),
            SessionHistoryEntry(
                issue_number=2,
                title="Issue 2",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=8,
            ),
        ]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/history")

        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 2
        issue1_entries = [e for e in payload["history"] if e["issue_number"] == 1]
        assert len(issue1_entries) == 1
        assert issue1_entries[0]["status"] == "blocked"


class TestDebugEndpoint:
    """Test the GET /api/debug endpoint."""

    def test_get_debug_success(self):
        """Test successful debug info retrieval."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/debug")

        assert response.status_code == 200
        data = response.json()
        assert "paused" in data
        assert "config_path" in data
        assert "repo_root" in data
        assert "agents" in data
        assert "startup_options" in data

    def test_get_debug_includes_agents(self):
        """Test debug endpoint includes agent configuration."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/debug")

        data = response.json()
        assert "agent:web" in data["agents"]
        assert data["agents"]["agent:web"]["timeout"] == 45


class TestTestDataEndpoints:
    """Test the test data creation/cleanup endpoints."""

    def test_create_test_issues_success(self):
        """Test creating test issues."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        with patch("issue_orchestrator.testing.support.test_data.create_test_issues") as mock_create:
            mock_create.return_value = [
                "https://github.com/owner/repo/issues/1",
                "https://github.com/owner/repo/issues/2",
            ]

            client = TestClient(app)
            response = client.post("/api/test/create")

            assert response.status_code == 200
            data = response.json()
            assert len(data["created"]) == 2
            assert mock_orch.config.filtering.label == "test-data"

    def test_create_test_issues_no_repo(self):
        """Test creating test issues without repo configured."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.config.repo = None
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/test/create")

        assert response.status_code == 400
        assert "error" in response.json()

    def test_cleanup_test_issues_success(self):
        """Test cleaning up test issues."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add test issue to history
        entry = SessionHistoryEntry(
            issue_number=1,
            title="[TEST] Test Issue",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=10,
        )
        mock_orch.state.session_history = [entry]

        set_orchestrator(mock_orch)

        with patch("issue_orchestrator.testing.support.test_data.cleanup_test_issues") as mock_cleanup:
            mock_cleanup.return_value = 2

            client = TestClient(app)
            response = client.post("/api/test/cleanup")

            assert response.status_code == 200
            assert response.json()["closed"] == 2
            # Test issues should be removed from history
            assert len(mock_orch.state.session_history) == 0

    def test_cleanup_test_issues_preserves_non_test(self):
        """Test cleanup preserves non-test issues in history."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add both test and non-test issues to history
        test_entry = SessionHistoryEntry(
            issue_number=1,
            title="[TEST] Test Issue",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=10,
        )
        normal_entry = SessionHistoryEntry(
            issue_number=2,
            title="Normal Issue",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=15,
        )
        mock_orch.state.session_history = [test_entry, normal_entry]

        set_orchestrator(mock_orch)

        with patch("issue_orchestrator.testing.support.test_data.cleanup_test_issues") as mock_cleanup:
            mock_cleanup.return_value = 1

            client = TestClient(app)
            response = client.post("/api/test/cleanup")

            assert response.status_code == 200
            # Only normal issue should remain
            assert len(mock_orch.state.session_history) == 1
            assert mock_orch.state.session_history[0].issue_number == 2


class TestOrchestratorNotInitialized:
    """Test endpoints when orchestrator is not initialized."""

    def test_endpoints_return_503_when_orchestrator_none(self):
        """Test that all endpoints return 503 when orchestrator is None."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)

        endpoints = [
            ("GET", "/api/status"),
            ("POST", "/api/pause"),
            ("POST", "/api/resume"),
            ("POST", "/api/focus/1"),
            ("POST", "/api/host/reveal-worktree/1"),
            ("POST", "/api/finder/1"),
            ("POST", "/api/prompt/web"),
            ("POST", "/api/shutdown"),
            ("GET", "/api/info"),
            ("GET", "/api/config"),
            ("POST", "/api/test/create"),
            ("POST", "/api/test/cleanup"),
            ("POST", "/api/history/clear"),
            ("POST", "/api/history/dismiss/1"),
            ("POST", "/api/retry/1"),
            ("GET", "/api/debug"),
        ]

        for method, path in endpoints:
            if method == "GET":
                response = client.get(path)
            else:
                response = client.post(path)

            assert response.status_code == 503, f"{method} {path} should return 503"
            assert "error" in response.json(), f"{method} {path} should have error message"
