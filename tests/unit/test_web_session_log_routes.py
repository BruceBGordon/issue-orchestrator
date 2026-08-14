"""Session cancellation and log route tests split from test_web."""

# ruff: noqa: F403,F405,SLF001

from tests.unit import test_web as _support
from tests.unit.test_web import *  # noqa: F403
from issue_orchestrator.control.review_exchange_lifecycle import (
    ReviewExchangeCancellation,
)
from issue_orchestrator.domain.timeline_evidence import (
    TimelineEvidenceIdentity,
    TimelineEvidenceState,
    TimelineEvidenceStatus,
)

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestKillSessionEndpoint:
    """Test the POST /api/kill/{issue_number} endpoint."""

    def test_kill_session_success(self):
        """Terminate-on-kill should stop and hold issue from automatic rerun."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.cancel_review_exchange_for_issue = MagicMock(
            return_value=ReviewExchangeCancellation(
                issue_number=1,
                cancelled_job_ids=("review-exchange:1:issue-1",),
            )
        )

        issue = create_issue(1, "Issue to Kill")
        session = create_session(issue)
        session.pr_number = 4124
        mock_orch.state.active_sessions = [session]
        mock_orch.state.pending_reviews = [
            PendingReview(
                issue_key=FakeIssueKey(name="1"),
                pr_number=4124,
                pr_url="https://example/pr/4124",
                branch_name="feature/1",
                _issue_number=1,
            )
        ]
        mock_orch.state.pending_reworks = [
            PendingRework(issue_key=FakeIssueKey(name="1"), agent_type="agent:web", rework_cycle=3, issue_number=1)
        ]
        mock_orch.state.pending_validation_retries = [
            PendingValidationRetry(
                issue_number=1,
                issue_title="Issue to Kill",
                agent_label="agent:web",
                worktree_path="/tmp/worktree-1",
                branch_name="feature/1",
                original_prompt=None,
                validation_error="boom",
                validation_error_file=None,
                retry_count=1,
                source_task=TaskKind.CODE,
            )
        ]
        mock_orch.state.discovered_reviews = [
            DiscoveredReview(1, 4124, "https://example/pr/4124", "feature/1")
        ]
        mock_orch.state.discovered_reworks = [
            DiscoveredRework(1, 4124, "feature/1", "agent:web", 3)
        ]
        mock_orch.state.discovered_failures = [
            DiscoveredFailure(1, "Issue to Kill", "failed")
        ]
        mock_orch.state.immediate_cleanups = [
            ImmediateCleanup(1, "issue-1", "/tmp/worktree-1", "completed")
        ]
        mock_orch.kill_session = MagicMock()

        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/kill/1")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "terminated"
            assert data["issue_number"] == 1
            assert data["title"] == "Issue to Kill"
            assert data["hold_label"] == "blocked-failed"
            mock_orch.kill_session.assert_called_once_with("issue-1")
            # Session should be removed from active sessions
            assert len(mock_orch.state.active_sessions) == 0
            # Queues/discovered facts should be cleared to prevent re-run.
            assert mock_orch.state.pending_reviews == []
            assert mock_orch.state.pending_reworks == []
            assert mock_orch.state.pending_validation_retries == []
            assert mock_orch.state.discovered_reviews == []
            assert mock_orch.state.discovered_reworks == []
            assert mock_orch.state.discovered_failures == []
            assert mock_orch.state.immediate_cleanups == []
            assert 1 in mock_orch.state.failed_this_cycle
            assert len(mock_orch.state.session_history) == 1
            history_entry = mock_orch.state.session_history[0]
            assert history_entry.issue_number == 1
            assert history_entry.status == "blocked"
            assert history_entry.status_reason == "Terminated by operator"
            # Hold labels: issue + linked PR.
            mock_orch.repository_host.add_label.assert_any_call(1, "blocked-failed")
            mock_orch.repository_host.remove_label.assert_any_call(1, "in-progress")
            mock_orch.repository_host.remove_label.assert_any_call(1, "pr-pending")
            mock_orch.repository_host.add_label.assert_any_call(4124, "blocked-failed")
            mock_orch.repository_host.remove_label.assert_any_call(4124, "needs-rework")
            mock_orch.cancel_review_exchange_for_issue.assert_called_once_with(
                1,
                reason="operator-terminated",
            )
        finally:
            set_orchestrator(None)

    def test_kill_session_not_found(self):
        """Test kill returns 404 when session not found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/kill/999")

            assert response.status_code == 404
            assert "error" in response.json()
        finally:
            set_orchestrator(None)

    def test_kill_session_failure(self):
        """Test kill returns 500 when kill operation fails."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]
        mock_orch.kill_session = MagicMock(side_effect=Exception("Kill failed"))

        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/kill/1")

            assert response.status_code == 500
            assert "error" in response.json()
            assert "Failed to terminate" in response.json()["error"]
            assert any("Kill failed" in item for item in response.json()["details"])
        finally:
            set_orchestrator(None)

    def test_kill_session_when_orchestrator_not_running(self):
        """Test kill returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.post("/api/kill/1")

        assert response.status_code == 503
        assert "error" in response.json()

    def test_bulk_kill_terminates_and_reports_missing(self):
        """Bulk kill should terminate active issues and report non-active ones."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1, "Issue 1")
        session = create_session(issue)
        session.pr_number = 4124
        mock_orch.state.active_sessions = [session]
        mock_orch.kill_session = MagicMock()

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/bulk-kill", json={"issue_numbers": [1, 999]})
            assert response.status_code == 200
            payload = response.json()
            assert payload["terminated"] == [1]
            assert payload["failed"] == [{"issue_number": 999, "error": "Session not found"}]
            mock_orch.kill_session.assert_called_once_with("issue-1")
        finally:
            set_orchestrator(None)

    def test_bulk_cancel_queued_places_issue_on_hold(self):
        """Queued cancel should hold the issue and prune launchable queue state."""
        mock_orch = create_mock_orchestrator()
        lm = LabelManager(mock_orch.config)
        mock_orch.deps.label_manager = lm
        mock_orch.deps.queue_cache_store = MagicMock()
        issue = create_issue(4057, "Queued Issue", labels=["agent:web"])
        mock_orch.state.cached_scope_issues = [issue]
        mock_orch.state.cached_queue_issues = [issue]
        mock_orch.state.pending_reviews = [
            PendingReview(
                issue_key=FakeIssueKey(name="4057"),
                pr_number=4124,
                pr_url="https://example/pr/4124",
                branch_name="feature/4057",
                _issue_number=4057,
            )
        ]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/bulk-cancel-queued", json={"issue_numbers": [4057]})
            assert response.status_code == 200
            payload = response.json()
            assert payload["cancelled"] == [4057]
            assert payload["failed"] == []
            assert mock_orch.state.cached_scope_issues == []
            assert mock_orch.state.cached_queue_issues == []
            assert mock_orch.state.pending_reviews == []
            assert 4057 in mock_orch.state.failed_this_cycle
            history_entry = mock_orch.state.session_history[-1]
            assert history_entry.issue_number == 4057
            assert history_entry.status == "blocked"
            assert history_entry.status_reason == "Cancelled from queue by operator"
            mock_orch.repository_host.add_label.assert_any_call(4057, lm.blocked_failed)
            mock_orch.deps.queue_cache_store.save_snapshot.assert_called_once_with(
                [],
                mock_orch.state.queue_delta_watermark,
                repo="owner/repo",
            )
            mock_orch.repository_host.add_label.assert_any_call(4124, lm.blocked_failed)
            mock_orch.repository_host.remove_label.assert_any_call(4124, lm.code_review)
            mock_orch.repository_host.remove_label.assert_any_call(4124, lm.needs_rework)
        finally:
            set_orchestrator(None)


class TestGetSessionLogEndpoint:
    """Test the GET /api/log/{issue_number} endpoint."""

    def test_get_session_log_from_active_session(self):
        """Test getting log from an active session."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        worktree_path = Path("/tmp/worktree-1")
        session = create_session(issue, worktree_path=str(worktree_path))
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)

        try:
            # Mock the Claude project directory structure
            with patch("issue_orchestrator.entrypoints.web_log_routes.Path.home") as mock_home:
                mock_claude_dir = MagicMock()
                mock_home.return_value = mock_claude_dir

                # Mock the path chain: home/.claude/projects/escaped_path
                mock_claude_projects = MagicMock()
                mock_claude_dir.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value = mock_claude_projects
                mock_claude_projects.exists.return_value = True

                # Mock finding a jsonl file
                mock_log_file = MagicMock()
                mock_log_file.stat.return_value = MagicMock(st_mtime=1234567890)
                mock_log_file.read_text.return_value = "line1\nline2\nline3"
                mock_claude_projects.glob.return_value = [mock_log_file]

                client = TestClient(app)
                response = client.get("/api/log/1")  # GET not POST

                assert response.status_code == 200
                data = response.json()
                assert data["issue_number"] == 1
                assert data["total_lines"] == 3
                assert data["truncated"] is False
                assert len(data["lines"]) == 3
        finally:
            set_orchestrator(None)

    def test_get_session_log_no_worktree_path(self):
        """Test log returns 404 when no worktree path found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.get("/api/log/999")  # GET not POST

            assert response.status_code == 404
            assert "error" in response.json()
        finally:
            set_orchestrator(None)

    def test_get_session_log_truncates_large_logs(self):
        """Test log truncates to last 100 lines."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        worktree_path = Path("/tmp/worktree-1")
        session = create_session(issue, worktree_path=str(worktree_path))
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)

        try:
            with patch("issue_orchestrator.entrypoints.web_log_routes.Path.home") as mock_home:
                mock_claude_dir = MagicMock()
                mock_home.return_value = mock_claude_dir

                mock_claude_projects = MagicMock()
                mock_claude_dir.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value = mock_claude_projects
                mock_claude_projects.exists.return_value = True

                # Create 150 lines
                lines = "\n".join([f"line{i}" for i in range(150)])
                mock_log_file = MagicMock()
                mock_log_file.stat.return_value = MagicMock(st_mtime=1234567890)
                mock_log_file.read_text.return_value = lines
                mock_claude_projects.glob.return_value = [mock_log_file]

                client = TestClient(app)
                response = client.get("/api/log/1")  # GET not POST

                assert response.status_code == 200
                data = response.json()
                assert data["total_lines"] == 150
                assert data["truncated"] is True
                assert len(data["lines"]) == 100
        finally:
            set_orchestrator(None)

    def test_get_session_log_when_orchestrator_not_running(self):
        """Test log returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.get("/api/log/1")  # GET not POST

        assert response.status_code == 503
        assert "error" in response.json()


class TestIssueLogEndpointsUseLatestHistory:
    """Issue log endpoints should resolve latest history entry, not oldest."""

    @staticmethod
    def _write_terminal_recording(run_dir: Path, *chunks: str) -> None:
        recording = run_dir / "terminal-recording.jsonl"
        payload = "\n".join(
            json.dumps(
                {
                    "schema_version": 1,
                    "event_type": "output",
                    "offset_ms": index,
                    "data_b64": base64.b64encode(chunk.encode("utf-8")).decode("ascii"),
                },
                sort_keys=True,
            )
            for index, chunk in enumerate(chunks)
        )
        recording.write_text(f"{payload}\n" if payload else "", encoding="utf-8")

    def test_agent_ui_log_prefers_latest_history_entry(self, tmp_path: Path):
        """GET /api/log/local should read from explicit run_dir only."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()

        old_worktree = tmp_path / "wt-old"
        old_worktree.mkdir(parents=True)
        old_run = session_output.start_run(old_worktree, "issue-123", issue_number=123)
        self._write_terminal_recording(old_run.run_dir, "old run log line\n")

        new_worktree = tmp_path / "wt-new"
        new_worktree.mkdir(parents=True)
        new_run = session_output.start_run(new_worktree, "issue-123", issue_number=123)
        self._write_terminal_recording(new_run.run_dir, "new run log line\n")

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123 old",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=old_worktree,
            ),
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123 new",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=new_worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/log/local/123?run_dir={new_run.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert any("new run log line" in line for line in payload["lines"])
            assert str(new_worktree) in payload["log_path"]
        finally:
            set_orchestrator(None)

    def test_agent_ui_log_requires_run_dir(self, tmp_path: Path):
        """GET /api/log/local should fail fast when run_dir is missing."""
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/log/local/123")
            assert response.status_code == 400
            payload = response.json()
            assert payload["error"] == "run_dir is required"
        finally:
            set_orchestrator(None)

    def test_agent_ui_log_rejects_logically_expired_unpruned_run(
        self, tmp_path: Path
    ) -> None:
        """The legacy exact-run route enforces retention before reading files."""
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )

        mock_orch = create_mock_orchestrator()
        worktree = tmp_path / "wt-expired-local-log"
        worktree.mkdir(parents=True)
        run = FileSystemSessionOutput().start_run(
            worktree, "issue-123", issue_number=123
        )
        self._write_terminal_recording(run.run_dir, "expired but still present\n")
        mock_orch.deps.timeline_evidence.describe.return_value = (
            TimelineEvidenceState(
                identity=TimelineEvidenceIdentity(123, run.run_dir),
                status=TimelineEvidenceStatus.EXPIRED,
                label="Evidence expired",
                available=False,
                pinned=False,
                archived=False,
                help_text="Retention elapsed.",
            )
        )

        set_orchestrator(mock_orch)
        try:
            response = TestClient(app).get(
                f"/api/log/local/123?run_dir={run.run_dir}"
            )
        finally:
            set_orchestrator(None)

        assert response.status_code == 410
        assert response.json()["error"] == "Evidence expired"
        assert run.log_path.is_file()

    def test_agent_ui_log_serves_file_content_directly(self, tmp_path: Path):
        """GET /api/log/local serves pre-cleaned file content, filtering only blanks."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-direct-serve"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        self._write_terminal_recording(run.run_dir, "Line one\n\nLine two\n")

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/log/local/123?run_dir={run.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["lines"] == ["Line one", "Line two"]
            assert payload["total_lines"] == 2
            assert "stream_observation" in payload
            stream_obs = payload["stream_observation"]
            assert stream_obs["terminal_recording"]["path"].endswith("/terminal-recording.jsonl")
            assert stream_obs["provider_stdout"]["path"].endswith("/provider-runner/stdout.log")
            assert stream_obs["provider_stderr"]["path"].endswith("/provider-runner/stderr.log")
        finally:
            set_orchestrator(None)

    def test_agent_ui_log_decodes_claude_stream_json_to_plain_text(self, tmp_path: Path):
        """GET /api/log/local should render stream-json deltas as plain transcript lines."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-stream-json"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        self._write_terminal_recording(
            run.run_dir,
            "\n".join([
                '{"type":"system","subtype":"init"}',
                '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}}',
                '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":" world\\nSecond output"}}}',
            ]) + "\n",
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/log/local/123?run_dir={run.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["lines"] == ["Hello world", "Second output"]
            assert payload["total_lines"] == 2
        finally:
            set_orchestrator(None)

    def test_agent_ui_log_does_not_fallback_to_claude_when_terminal_recording_empty(self, tmp_path: Path):
        """GET /api/log/local should not leak Claude transcript into the agent-log preview."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-no-claude-fallback"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        run.log_path.write_text("", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text(
            "\n".join([
                '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Should"}}}',
                '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":" not appear"}}}',
            ]) + "\n",
            encoding="utf-8",
        )
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/log/local/123?run_dir={run.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["lines"] == []
            assert payload["total_lines"] == 0
            assert payload["log_path"].endswith("/terminal-recording.jsonl")
        finally:
            set_orchestrator(None)

    def test_agent_ui_log_decodes_terminal_recording_to_preview_lines(self, tmp_path: Path):
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-terminal-preview"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        run.log_path.write_text(
            "\n".join([
                '{"data_b64":"TGluZSBvbmUK","event_type":"output","offset_ms":0,"schema_version":1}',
                '{"cols":120,"event_type":"resize","offset_ms":1,"rows":40,"schema_version":1}',
                '{"data_b64":"TGluZSB0d28K","event_type":"output","offset_ms":2,"schema_version":1}',
            ]) + "\n",
            encoding="utf-8",
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/log/local/123?run_dir={run.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["lines"] == ["Line one", "Line two"]
            assert payload["total_lines"] == 2
            assert payload["log_path"].endswith("/terminal-recording.jsonl")
        finally:
            set_orchestrator(None)

    def test_agent_ui_log_prefers_terminal_recording_when_both_artifacts_exist(self, tmp_path: Path):
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-terminal-preferred"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        run.log_path.write_text(
            '{"data_b64":"VGVybWluYWwgbGluZQo=","event_type":"output","offset_ms":0,"schema_version":1}\n',
            encoding="utf-8",
        )
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text(
            '{"type":"assistant","message":{"content":[{"type":"text","text":"Claude line"}]}}\n',
            encoding="utf-8",
        )
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/log/local/123?run_dir={run.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["lines"] == ["Terminal line"]
            assert payload["total_lines"] == 1
            assert payload["log_path"].endswith("/terminal-recording.jsonl")
        finally:
            set_orchestrator(None)

    def test_terminal_recording_requires_run_dir(self, tmp_path: Path):
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/terminal-recording/123")
            assert response.status_code == 400
            payload = response.json()
            assert payload["error"] == "run_dir is required"
        finally:
            set_orchestrator(None)

    def test_terminal_recording_returns_raw_events(self, tmp_path: Path):
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-terminal-recording"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        (run.run_dir / "terminal-recording.jsonl").write_text(
            "\n".join([
                '{"data_b64":"aGVsbG8=","event_type":"output","offset_ms":0,"schema_version":1}',
                '{"cols":120,"event_type":"resize","offset_ms":25,"rows":40,"schema_version":1}',
            ]) + "\n",
            encoding="utf-8",
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/terminal-recording/123?run_dir={run.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["recording_path"].endswith("/terminal-recording.jsonl")
            assert payload["total_events"] == 2
            assert payload["initial_geometry"] == {"rows": 40, "cols": 120}
            assert payload["events"][0]["event_type"] == "output"
            assert payload["events"][1]["event_type"] == "resize"
        finally:
            set_orchestrator(None)

    def test_terminal_recording_can_resolve_review_exchange_phase_recording(self, tmp_path: Path):
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-review-phase-terminal-recording"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        phase_recording = (
            run.run_dir / "review-exchange" / "round-002" / "reviewer" / "terminal-recording.jsonl"
        )
        phase_recording.parent.mkdir(parents=True, exist_ok=True)
        phase_recording.write_text(
            "\n".join([
                '{"data_b64":"cmV2aWV3ZXI=","event_type":"output","offset_ms":0,"schema_version":1}',
                '{"cols":100,"event_type":"resize","offset_ms":5,"rows":30,"schema_version":1}',
            ]) + "\n",
            encoding="utf-8",
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(
                f"/api/session/terminal-recording/123?run_dir={run.run_dir}&round_index=2&session_role=reviewer"
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["recording_path"].endswith("/review-exchange/round-002/reviewer/terminal-recording.jsonl")
            assert payload["total_events"] == 2
            assert payload["initial_geometry"] == {"rows": 30, "cols": 100}
        finally:
            set_orchestrator(None)

    def test_terminal_recording_persistent_layout_uses_chapters_to_scrub(self, tmp_path: Path):
        """Persistent layout: phase-scoped request slices the role's
        recording to the requested round using ``chapters.json``, and
        returns the chapter list + ``recording_event_index`` so the UI
        can render an outline and start playback at the right offset.
        Regression for the #6160 re-review finding that the route was
        returning the entire role recording with no chapter metadata.
        """
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-persistent-chapters"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)

        recording = run.run_dir / "reviewer" / "terminal-recording.jsonl"
        recording.parent.mkdir(parents=True, exist_ok=True)
        events_lines = []
        for index in range(6):
            events_lines.append(
                '{"data_b64":"aGk=","event_type":"output","offset_ms":'
                + str(index)
                + ',"schema_version":1}'
            )
        recording.write_text("\n".join(events_lines) + "\n", encoding="utf-8")

        chapters_path = run.run_dir / "reviewer" / "chapters.json"
        chapters_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "role": "reviewer",
                    "exchange_run_id": "exchange-1",
                    "issue_number": 123,
                    "chapters": [
                        {
                            "cycle_index": 1,
                            "section": "prompt",
                            "recording_event_index": 0,
                            "recorded_at": "2026-05-04T00:00:00Z",
                            "label": "Round 1 Prompt",
                        },
                        {
                            "cycle_index": 1,
                            "section": "feedback",
                            "recording_event_index": 1,
                            "recorded_at": "2026-05-04T00:00:01Z",
                            "label": "Round 1 Feedback",
                        },
                        {
                            "cycle_index": 2,
                            "section": "prompt",
                            "recording_event_index": 3,
                            "recorded_at": "2026-05-04T00:00:02Z",
                            "label": "Round 2 Prompt",
                        },
                        {
                            "cycle_index": 2,
                            "section": "feedback",
                            "recording_event_index": 4,
                            "recorded_at": "2026-05-04T00:00:03Z",
                            "label": "Round 2 Feedback",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)

            response = client.get(
                f"/api/session/terminal-recording/123?run_dir={run.run_dir}"
                "&round_index=1&session_role=reviewer"
            )
            assert response.status_code == 200
            round1 = response.json()
            assert round1["recording_path"].endswith("/reviewer/terminal-recording.jsonl")
            # Round 1 spans events [0, 3) — three of the six events.
            assert round1["total_events"] == 3
            assert round1["recording_event_index"] == 0
            assert isinstance(round1["chapters"], list)
            assert len(round1["chapters"]) == 4
            sections = [(ch["cycle_index"], ch["section"]) for ch in round1["chapters"]]
            assert sections == [
                (1, "prompt"),
                (1, "feedback"),
                (2, "prompt"),
                (2, "feedback"),
            ]

            response = client.get(
                f"/api/session/terminal-recording/123?run_dir={run.run_dir}"
                "&round_index=2&session_role=reviewer"
            )
            assert response.status_code == 200
            round2 = response.json()
            # Round 2 spans events [3, end) — three of the six events,
            # and starts at recording event index 3.
            assert round2["total_events"] == 3
            assert round2["recording_event_index"] == 3
            assert len(round2["chapters"]) == 4
        finally:
            set_orchestrator(None)

    def test_terminal_recording_persistent_layout_404s_when_sidecar_missing(
        self, tmp_path: Path,
    ) -> None:
        """Persistent role recording with no chapters.json must fail
        closed — silently serving the whole role recording for a
        phase-scoped request would render the wrong round (#6160
        re-review feedback).
        """
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-persistent-no-sidecar"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        recording = run.run_dir / "reviewer" / "terminal-recording.jsonl"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_text(
            '{"data_b64":"aGk=","event_type":"output","offset_ms":0,"schema_version":1}\n',
            encoding="utf-8",
        )
        # Note: NO chapters.json next to the recording.

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(
                f"/api/session/terminal-recording/123?run_dir={run.run_dir}"
                "&round_index=2&session_role=reviewer"
            )
            assert response.status_code == 404
            payload = response.json()
            assert payload["diagnostic"]["reason"] == "missing_sidecar"
            assert payload["diagnostic"]["round_index"] == "2"
            assert payload["diagnostic"]["session_role"] == "reviewer"
        finally:
            set_orchestrator(None)

    def test_terminal_recording_persistent_layout_404s_when_sidecar_malformed(
        self, tmp_path: Path,
    ) -> None:
        """Malformed sidecar JSON must not silently degrade to whole-role."""
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-persistent-malformed"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        recording = run.run_dir / "reviewer" / "terminal-recording.jsonl"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_text(
            '{"data_b64":"aGk=","event_type":"output","offset_ms":0,"schema_version":1}\n',
            encoding="utf-8",
        )
        chapters_path = run.run_dir / "reviewer" / "chapters.json"
        chapters_path.write_text("{not valid json", encoding="utf-8")

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(
                f"/api/session/terminal-recording/123?run_dir={run.run_dir}"
                "&round_index=1&session_role=reviewer"
            )
            assert response.status_code == 404
            payload = response.json()
            assert payload["diagnostic"]["reason"] == "malformed_or_unreadable"
            assert payload["diagnostic"]["sidecar_path"].endswith(
                "/reviewer/chapters.json"
            )
        finally:
            set_orchestrator(None)

    def test_terminal_recording_persistent_layout_404s_when_round_not_in_sidecar(
        self, tmp_path: Path,
    ) -> None:
        """A sidecar that exists but has no prompt chapter for the
        requested round must fail closed."""
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-persistent-missing-round"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        recording = run.run_dir / "reviewer" / "terminal-recording.jsonl"
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_text(
            '{"data_b64":"aGk=","event_type":"output","offset_ms":0,"schema_version":1}\n',
            encoding="utf-8",
        )
        chapters_path = run.run_dir / "reviewer" / "chapters.json"
        chapters_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "role": "reviewer",
                    "exchange_run_id": "exchange-1",
                    "issue_number": 123,
                    "chapters": [
                        {
                            "cycle_index": 1,
                            "section": "prompt",
                            "recording_event_index": 0,
                            "recorded_at": "2026-05-04T00:00:00Z",
                            "label": "Round 1 Prompt",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            # Request round 2, which has no entry in the sidecar.
            response = client.get(
                f"/api/session/terminal-recording/123?run_dir={run.run_dir}"
                "&round_index=2&session_role=reviewer"
            )
            assert response.status_code == 404
            payload = response.json()
            assert payload["diagnostic"]["reason"] == "missing_round"
            assert payload["diagnostic"]["round_index"] == "2"
            assert payload["diagnostic"]["available_rounds"] == "1"
        finally:
            set_orchestrator(None)

    def test_terminal_recording_b2_pair_scoped_recording_with_run_scoped_chapters(
        self, tmp_path: Path,
    ) -> None:
        """B2 layout regression (PR #6212 review feedback): the recording
        is pair-scoped (lives under
        ``<coder-worktree>/.issue-orchestrator/persistent-pairs/issue-N/<role>/...``)
        but chapters are still per-exchange and live under the selected
        ``<run_dir>/<role>/chapters.json``. The route's
        ``/api/session/terminal-recording`` must follow the manifest
        to find the recording AND look in ``run_dir`` for chapters,
        otherwise the phase-scoped slice 404s with "Phase-scoped
        recording cannot be resolved" because the sidecar lookup
        would search next to the pair-scoped recording.
        """
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-pair-scoped-recording"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)

        # Recording lives at the pair scope, NOT under run_dir.
        pair_recording_dir = (
            tmp_path
            / "persistent-pairs"
            / "issue-123"
            / "reviewer"
        )
        pair_recording_dir.mkdir(parents=True)
        pair_recording = pair_recording_dir / "terminal-recording.jsonl"
        events_lines = []
        for index in range(6):
            events_lines.append(
                '{"data_b64":"aGk=","event_type":"output","offset_ms":'
                + str(index)
                + ',"schema_version":1}'
            )
        pair_recording.write_text("\n".join(events_lines) + "\n", encoding="utf-8")

        # Manifest tells the accessor the canonical recording is the
        # pair-scoped file.
        session_output.update_manifest(run.run_dir, {
            "reviewer_recording": str(pair_recording),
        })

        # Chapters stay per-exchange under run_dir.
        chapters_path = run.run_dir / "reviewer" / "chapters.json"
        chapters_path.parent.mkdir(parents=True, exist_ok=True)
        chapters_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "role": "reviewer",
                    "exchange_run_id": "exchange-1",
                    "issue_number": 123,
                    "chapters": [
                        {
                            "cycle_index": 1,
                            "section": "prompt",
                            "recording_event_index": 0,
                            "recorded_at": "2026-05-04T00:00:00Z",
                            "label": "Round 1 Prompt",
                        },
                        {
                            "cycle_index": 2,
                            "section": "prompt",
                            "recording_event_index": 3,
                            "recorded_at": "2026-05-04T00:00:02Z",
                            "label": "Round 2 Prompt",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(
                f"/api/session/terminal-recording/123?run_dir={run.run_dir}"
                "&round_index=1&session_role=reviewer"
            )
            assert response.status_code == 200, (
                "B2 route lookup must follow the manifest's pair-scoped "
                "recording AND look in run_dir for chapters; got "
                f"{response.status_code}: {response.json()}"
            )
            payload = response.json()
            # The served recording is the pair-scoped file.
            assert payload["recording_path"] == str(pair_recording)
            # Round 1 spans events [0, 3) — three of the six events.
            assert payload["total_events"] == 3
            assert payload["recording_event_index"] == 0
            # Chapters resolved from the run-scoped sidecar.
            assert payload["chapters"] is not None
            assert len(payload["chapters"]) == 2
            cycle_indices = sorted({ch["cycle_index"] for ch in payload["chapters"]})
            assert cycle_indices == [1, 2]
        finally:
            set_orchestrator(None)

    def test_terminal_recording_b2_pair_scoped_recording_404s_when_chapters_missing(
        self, tmp_path: Path,
    ) -> None:
        """B2 layout, missing run-scoped chapters: route must fail
        closed with the explicit ``run_dir``-scoped sidecar path in
        the diagnostic so the operator can find what's missing
        (PR #6212 review feedback)."""
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-pair-scoped-no-chapters"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)

        pair_recording_dir = (
            tmp_path / "persistent-pairs" / "issue-123" / "reviewer"
        )
        pair_recording_dir.mkdir(parents=True)
        pair_recording = pair_recording_dir / "terminal-recording.jsonl"
        pair_recording.write_text(
            '{"data_b64":"aGk=","event_type":"output","offset_ms":0,"schema_version":1}\n',
            encoding="utf-8",
        )
        session_output.update_manifest(run.run_dir, {
            "reviewer_recording": str(pair_recording),
        })

        # Deliberately do NOT write chapters.json in run_dir.

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(
                f"/api/session/terminal-recording/123?run_dir={run.run_dir}"
                "&round_index=1&session_role=reviewer"
            )
            assert response.status_code == 404
            payload = response.json()
            assert payload["diagnostic"]["reason"] == "missing_sidecar"
            # The diagnostic must point at the run-scoped sidecar path
            # so the operator knows where chapters were expected, not
            # at the pair-scoped recording's parent dir.
            expected_sidecar = run.run_dir / "reviewer" / "chapters.json"
            assert payload["diagnostic"]["sidecar_path"] == str(expected_sidecar)
        finally:
            set_orchestrator(None)

    def test_terminal_recording_legacy_layout_still_serves_when_no_sidecar(
        self, tmp_path: Path,
    ) -> None:
        """Legacy spawn-per-phase layout has no sidecar by design — the
        per-round file is already scoped, so the request must still
        succeed (no 404). Guards against the persistent fail-closed
        path leaking onto the legacy file shape."""
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-legacy-still-served"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        legacy_recording = (
            run.run_dir
            / "review-exchange"
            / "round-002"
            / "reviewer"
            / "terminal-recording.jsonl"
        )
        legacy_recording.parent.mkdir(parents=True, exist_ok=True)
        legacy_recording.write_text(
            '{"data_b64":"aGk=","event_type":"output","offset_ms":0,"schema_version":1}\n',
            encoding="utf-8",
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(
                f"/api/session/terminal-recording/123?run_dir={run.run_dir}"
                "&round_index=2&session_role=reviewer"
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["recording_path"].endswith(
                "/review-exchange/round-002/reviewer/terminal-recording.jsonl"
            )
            # Legacy layout has no chapter sidecar — chapters/recording_event_index null.
            assert payload["chapters"] is None
            assert payload["recording_event_index"] is None
        finally:
            set_orchestrator(None)

    def test_terminal_recording_preserves_initial_geometry_when_tail_is_truncated(self, tmp_path: Path):
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-terminal-truncated"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        events = [
            '{"cols":120,"event_type":"resize","offset_ms":0,"rows":40,"schema_version":1}',
        ]
        events.extend(
            json.dumps(
                {
                    "data_b64": "bGluZQ==",
                    "event_type": "output",
                    "offset_ms": index + 1,
                    "schema_version": 1,
                }
            )
            for index in range(4)
        )
        (run.run_dir / "terminal-recording.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/terminal-recording/123?run_dir={run.run_dir}&limit=2")
            assert response.status_code == 200
            payload = response.json()
            assert payload["truncated"] is True
            assert payload["initial_geometry"] == {"rows": 40, "cols": 120}
            assert len(payload["events"]) == 2
            assert all(event["event_type"] == "output" for event in payload["events"])
        finally:
            set_orchestrator(None)

    def test_claude_log_requires_run_dir(self, tmp_path: Path):
        """GET /api/session/claude-log should fail fast when run_dir is missing."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()

        old_worktree = tmp_path / "wt-old-claude"
        old_worktree.mkdir(parents=True)
        old_run = session_output.start_run(old_worktree, "issue-123", issue_number=123)
        old_claude = old_run.run_dir / "old-claude.jsonl"
        old_claude.write_text('{"type":"assistant","content":"old"}\n')
        session_output.update_manifest(old_run.run_dir, {"claude_log_path": str(old_claude)})

        new_worktree = tmp_path / "wt-new-claude"
        new_worktree.mkdir(parents=True)
        new_run = session_output.start_run(new_worktree, "issue-123", issue_number=123)
        new_claude = new_run.run_dir / "new-claude.jsonl"
        new_claude.write_text('{"type":"assistant","content":"new"}\n')
        session_output.update_manifest(new_run.run_dir, {"claude_log_path": str(new_claude)})

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123 old",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=old_worktree,
            ),
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123 new",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=new_worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/claude-log/123")
            assert response.status_code == 400
            payload = response.json()
            assert payload["error"] == "run_dir is required"
        finally:
            set_orchestrator(None)

    def test_claude_log_honors_run_dir_query(self, tmp_path: Path):
        """GET /api/session/claude-log should read the requested run when run_dir is provided."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()

        worktree = tmp_path / "wt-claude-run-query"
        worktree.mkdir(parents=True)
        run_a = session_output.start_run(worktree, "review-1", issue_number=123)
        log_a = run_a.run_dir / "a.jsonl"
        log_a.write_text('{"type":"assistant","content":"from-run-a"}\n')
        session_output.update_manifest(run_a.run_dir, {"claude_log_path": str(log_a)})

        run_b = session_output.start_run(worktree, "review-2", issue_number=123)
        log_b = run_b.run_dir / "b.jsonl"
        log_b.write_text('{"type":"assistant","content":"from-run-b"}\n')
        session_output.update_manifest(run_b.run_dir, {"claude_log_path": str(log_b)})

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/claude-log/123?run_dir={run_a.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["run_dir"] == str(run_a.run_dir)
            assert payload["entries"][0]["content"] == "from-run-a"
        finally:
            set_orchestrator(None)

    def test_review_transcript_requires_run_dir(self, tmp_path: Path):
        """GET /api/session/review-transcript should fail fast when run_dir is missing."""
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/review-transcript/123")
            assert response.status_code == 400
            payload = response.json()
            assert payload["error"] == "run_dir is required"
        finally:
            set_orchestrator(None)

    def test_review_transcript_honors_run_dir_query(self, tmp_path: Path):
        """GET /api/session/review-transcript should read the requested run transcript."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-review-transcript-run-query"
        worktree.mkdir(parents=True)
        run_a = session_output.start_run(worktree, "review-1", issue_number=123)
        transcript_a = run_a.run_dir / "review-exchange" / "transcript.log"
        transcript_a.parent.mkdir(parents=True, exist_ok=True)
        transcript_a.write_text("from-run-a\n", encoding="utf-8")
        session_output.update_manifest(run_a.run_dir, {"review_exchange_transcript_path": str(transcript_a)})

        run_b = session_output.start_run(worktree, "review-2", issue_number=123)
        transcript_b = run_b.run_dir / "review-exchange" / "transcript.log"
        transcript_b.parent.mkdir(parents=True, exist_ok=True)
        transcript_b.write_text("from-run-b\n", encoding="utf-8")
        session_output.update_manifest(run_b.run_dir, {"review_exchange_transcript_path": str(transcript_b)})

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/review-transcript/123?run_dir={run_a.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["run_dir"] == str(run_a.run_dir)
            assert payload["content"] == "from-run-a\n"
        finally:
            set_orchestrator(None)

    def test_review_transcript_can_filter_to_requested_round_and_role(self, tmp_path: Path):
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-review-transcript-filter"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "review-1", issue_number=123)
        exchange_dir = run.run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        transcript = exchange_dir / "transcript.log"
        transcript.touch(exist_ok=True)
        session_output.update_manifest(
            run.run_dir,
            {"review_exchange_transcript_path": str(transcript)},
        )
        transcript.write_text(
            "[2026-03-20T04:39:32Z] round=1 role=reviewer section=prompt\nReviewer round 1\n\n"
            "[2026-03-20T04:40:42Z] round=1 role=coder section=prompt\nCoder round 1\n\n"
            "[2026-03-20T04:41:42Z] round=2 role=reviewer section=completion\nReviewer round 2\n",
            encoding="utf-8",
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(
                f"/api/session/review-transcript/123?run_dir={run.run_dir}&round_index=1&transcript_role=coder"
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["scope_label"] == "Round 1 coder"
            assert payload["entry_count"] == 1
            assert payload["content"]
            assert "Coder round 1" in payload["content"]
            assert "Reviewer round 1" not in payload["content"]
            assert "Reviewer round 2" not in payload["content"]
        finally:
            set_orchestrator(None)

    def test_session_prompt_requires_run_dir(self, tmp_path: Path):
        """GET /api/session/prompt should fail fast when run_dir is missing."""
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/session/prompt/123")
            assert response.status_code == 400
            payload = response.json()
            assert payload["error"] == "run_dir is required"
        finally:
            set_orchestrator(None)

    def test_session_prompt_honors_run_dir_query(self, tmp_path: Path):
        """GET /api/session/prompt should read the requested run prompt when run_dir is provided."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()

        worktree = tmp_path / "wt-prompt-run-query"
        worktree.mkdir(parents=True)
        run_a = session_output.start_run(worktree, "coding-1", issue_number=123)
        prompt_a = run_a.run_dir / "session-prompt.txt"
        prompt_a.write_text("prompt from run a\n", encoding="utf-8")
        session_output.update_manifest(run_a.run_dir, {"session_prompt_path": str(prompt_a)})

        run_b = session_output.start_run(worktree, "coding-2", issue_number=123)
        prompt_b = run_b.run_dir / "session-prompt.txt"
        prompt_b.write_text("prompt from run b\n", encoding="utf-8")
        session_output.update_manifest(run_b.run_dir, {"session_prompt_path": str(prompt_b)})

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/prompt/123?run_dir={run_a.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["run_dir"] == str(run_a.run_dir)
            assert payload["content"] == "prompt from run a\n"
        finally:
            set_orchestrator(None)

    def test_session_prompt_returns_rework_launch_prompt(self, tmp_path: Path):
        """A rework session's run-scoped launch prompt (manifest
        session_prompt_path) is served and labelled as the launch prompt,
        rather than the static agent template (#6588)."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()

        worktree = tmp_path / "wt-rework-454"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "rework-1", issue_number=454)
        launch_prompt = run.run_dir / "session-prompt.txt"
        launch_prompt.write_text(
            "Resolve the merge conflict in PR #469 against the base branch.\n",
            encoding="utf-8",
        )
        session_output.update_manifest(
            run.run_dir, {"session_prompt_path": str(launch_prompt)}
        )

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=454,
                title="Broken merge",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/prompt/454?run_dir={run.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["prompt_path"] == str(launch_prompt)
            assert payload["content"] == (
                "Resolve the merge conflict in PR #469 against the base branch.\n"
            )
            assert payload["label"] == "Launch prompt"
        finally:
            set_orchestrator(None)

    def test_session_prompt_rejects_out_of_run_manifest_path(self, tmp_path: Path):
        """A manifest whose session_prompt_path points outside the run dir must
        not be served: the endpoint 404s and never returns the out-of-run
        content (path containment is enforced by the run-artifact owner)."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()

        worktree = tmp_path / "wt-escape-123"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "coding-1", issue_number=123)
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("OUT OF RUN SECRET\n", encoding="utf-8")
        session_output.update_manifest(
            run.run_dir, {"session_prompt_path": str(outside)}
        )

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/prompt/123?run_dir={run.run_dir}")
            assert response.status_code == 404
            assert "OUT OF RUN SECRET" not in response.text
        finally:
            set_orchestrator(None)

    def test_orchestrator_log_honors_run_dir_query(self, tmp_path: Path):
        """GET /api/session/orchestrator-log should write tail into requested run_dir."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        mock_orch.config.repo_root = tmp_path / "repo"
        mock_orch.config.repo_root.mkdir(parents=True)
        orch_log = mock_orch.config.repo_root / ".issue-orchestrator" / "state" / "logs" / "orchestrator.log"
        orch_log.parent.mkdir(parents=True, exist_ok=True)
        orch_log.write_text("2026-02-16 [SESSION_RUN_START] run_id=test session=review-1 issue=123\n")

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-orch-run-query"
        worktree.mkdir(parents=True)
        run_a = session_output.start_run(worktree, "review-1", issue_number=123)
        run_b = session_output.start_run(worktree, "review-2", issue_number=123)

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/orchestrator-log/123?run_dir={run_a.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["filtered_log_path"].startswith(str(run_a.run_dir))
            assert not payload["filtered_log_path"].startswith(str(run_b.run_dir))
        finally:
            set_orchestrator(None)

    def test_orchestrator_log_errors_when_no_issue_scoped_lines(self, tmp_path: Path):
        """GET /api/session/orchestrator-log should fail when no issue-scoped lines are present."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        mock_orch.config.repo_root = tmp_path / "repo"
        mock_orch.config.repo_root.mkdir(parents=True)
        orch_log = mock_orch.config.repo_root / ".issue-orchestrator" / "state" / "logs" / "orchestrator.log"
        orch_log.parent.mkdir(parents=True, exist_ok=True)
        orch_log.write_text(
            "\n".join(
                [
                    "planner summary only",
                    "[issue-4048] unrelated line",
                ]
            )
        )

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-orch-run-query"
        worktree.mkdir(parents=True)
        run_a = session_output.start_run(worktree, "review-1", issue_number=123)

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/session/orchestrator-log/123?run_dir={run_a.run_dir}")
            assert response.status_code == 500
            payload = response.json()
            assert "No issue-scoped orchestrator log entries found" in payload["error"]
            assert "full_log_path" not in payload
        finally:
            set_orchestrator(None)

    def test_session_diagnostics_dialog_honors_run_dir_query(self, tmp_path: Path):
        """GET /api/dialog/session-diagnostics should use requested run_dir when provided."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-diag-run-query"
        worktree.mkdir(parents=True)

        run_a = session_output.start_run(worktree, "review-1", issue_number=123)
        session_output.update_manifest(run_a.run_dir, {"validation_record_path": ".issue-orchestrator/validation/a.json"})
        run_b = session_output.start_run(worktree, "review-2", issue_number=123)
        session_output.update_manifest(run_b.run_dir, {"validation_record_path": ".issue-orchestrator/validation/b.json"})

        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=1,
                worktree_path=worktree,
            ),
        ]
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/dialog/session-diagnostics/123?run_dir={run_a.run_dir}")
            assert response.status_code == 200
            payload = response.json()
            settings_paths = [
                action.get("path")
                for action in payload.get("actions", [])
                if action.get("type") == "open_path" and action.get("label") == "Open Session Settings"
            ]
            assert settings_paths == [f"{run_a.run_dir}/session-identity.json"]
            actions = payload.get("actions", [])
            validation_paths = [
                action.get("path")
                for action in actions
                if action.get("type") == "open_path" and "Validation" in str(action.get("label"))
            ]
            assert any(path and path.endswith("a.json") for path in validation_paths)
            assert not any(path and path.endswith("b.json") for path in validation_paths)
        finally:
            set_orchestrator(None)


class TestIssueSessionContextIsolation:
    def test_resolve_context_does_not_scan_sibling_worktrees(self, tmp_path: Path):
        """Session context must not pick runs from sibling worktrees/repos."""
        from issue_orchestrator.entrypoints.web import _resolve_issue_session_context

        mock_orch = create_mock_orchestrator()
        repo_a = tmp_path / "repo-a"
        repo_a.mkdir(parents=True)
        repo_b = tmp_path / "repo-b"
        sibling_run = repo_b / ".issue-orchestrator" / "sessions" / "20260216-120000Z__issue-4057"
        sibling_run.mkdir(parents=True)
        (sibling_run / "manifest.json").write_text(
            json.dumps(
                {
                    "session_name": "issue-4057",
                    "run_id": "20260216-120000Z",
                    "run_dir": str(sibling_run),
                    "issue_number": 4057,
                }
            ),
            encoding="utf-8",
        )
        mock_orch.config.repo_root = repo_a
        mock_orch.state.active_sessions = []
        mock_orch.state.session_history = []
        set_orchestrator(mock_orch)
        try:
            ctx = _resolve_issue_session_context(4057)
            assert ctx.run_dir is None
            assert ctx.worktree_path is None
            assert ctx.session_name is None
        finally:
            set_orchestrator(None)
