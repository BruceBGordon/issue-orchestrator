"""Core web route tests split from test_web."""

# ruff: noqa: F403,F405

from collections import Counter

from tests.unit import test_web as _support
from tests.unit.route_helpers import route_path_counts
from tests.unit.test_web import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestWebRouteRegistration:
    """Guard extracted web route families against accidental deregistration."""

    def test_extracted_operator_and_log_routes_are_registered_once(self):
        expected_paths = [
            "/api/log/{issue_number}",
            "/api/log/local/{issue_number}",
            "/api/host/reveal-worktree/{issue_number}",
            "/api/shutdown",
            "/api/bulk-cancel-queued",
            "/api/history",
            "/api/retry/{issue_number}",
            "/api/reset-retry",
            "/api/retrospective-review/preflight",
            "/api/retrospective-review",
        ]

        counts = route_path_counts(app, expected_paths)

        assert counts == Counter({path: 1 for path in expected_paths})



class TestDashboardEndpoint:
    """Test the GET / dashboard endpoint."""

    def test_dashboard_returns_html(self):
        """Test that dashboard returns HTML response."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
        finally:
            set_orchestrator(None)

    def test_dashboard_with_active_sessions(self):
        """Test dashboard displays active sessions."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add an active session
        issue = create_issue(1, "Active Issue")
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            assert "Active Issue" in response.text
            assert "#1" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_escapes_running_timeline_snapshot_summary(self):
        """Timeline snapshot text should be HTML-escaped in server-rendered cards."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1, "Active Issue")
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]
        mock_orch.deps.timeline_reader.read.return_value.to_dict.return_value = {
            "events": [
                {
                    "event": "session.started",
                    "views": ["user"],
                    "narrative": '<img src=x onerror="alert(1)">',
                }
            ]
        }

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            assert '&lt;img src=x onerror=&#34;alert(1)&#34;&gt;' in response.text
            assert '<img src=x onerror="alert(1)">' not in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_with_queue_pagination(self):
        """Test dashboard queue pagination."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Create 25 issues to trigger pagination (page size is 20)
        issues = [create_issue(i, f"Queue Issue {i}") for i in range(1, 26)]

        # Set cached queue issues (dashboard uses cache instead of calling API)
        mock_orch.state.cached_queue_issues = issues

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)

            # Test first page - use queue tab to see queue issues
            response = client.get("/?tab=queue&page=1")
            assert response.status_code == 200
            assert "Queue Issue 1" in response.text

            # Test second page
            response = client.get("/?tab=queue&page=2")
            assert response.status_code == 200
            assert "Queue Issue 21" in response.text
        finally:
            set_orchestrator(None)

    def test_dashboard_when_paused(self):
        """Test dashboard shows paused state."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.state.paused = True

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/")

            assert response.status_code == 200
            # The template should handle paused state
            assert response.status_code == 200
        finally:
            set_orchestrator(None)

    def test_dashboard_with_session_history(self):
        """Test dashboard displays session history on the History tab."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        # Add a history entry
        history_entry = SessionHistoryEntry(
            issue_number=42,
            title="Completed Issue",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=15,
            pr_url="https://github.com/owner/repo/pull/42",
        )
        mock_orch.state.session_history = [history_entry]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            # History now lives on the History tab, not the Work tab
            response = client.get("/?tab=history")

            assert response.status_code == 200
            assert "Completed Issue" in response.text
        finally:
            set_orchestrator(None)


class TestApiStatusEndpoint:
    """Test the GET /api/status endpoint."""

    def test_status_returns_json(self):
        """Test that status endpoint returns JSON."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        set_client_host(_StubClientHost())

        client = TestClient(app)
        response = client.get("/api/status")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        set_orchestrator(None)

    def test_status_includes_basic_info(self):
        """Test status includes basic orchestrator info."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/status")

        data = response.json()
        assert "paused" in data
        assert "active_sessions" in data
        assert "max_sessions" in data
        assert "completed_today" in data
        assert data["paused"] is False
        assert data["max_sessions"] == 3
        set_orchestrator(None)

    def test_status_with_active_sessions(self):
        """Test status includes active session details."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1, "Test Issue")
        session = create_session(issue, branch_name="feature/issue-1")
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/status")

        data = response.json()
        assert len(data["active_sessions"]) == 1
        assert data["active_sessions"][0]["issue_number"] == 1
        assert data["active_sessions"][0]["title"] == "Test Issue"
        assert data["active_sessions"][0]["branch"] == "feature/issue-1"
        assert data["active_sessions"][0]["worktree_path"] == str(
            session.worktree_path
        )
        set_orchestrator(None)

    def test_status_when_orchestrator_not_running(self):
        """Test status returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.get("/api/status")

        assert response.status_code == 503
        assert "error" in response.json()


class TestPauseResumeEndpoints:
    """Test the POST /api/pause and /api/resume endpoints."""

    def test_pause_endpoint(self):
        """Test pause endpoint calls orchestrator.pause()."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/pause")

        assert response.status_code == 200
        assert response.json()["status"] == "paused"
        mock_orch.pause.assert_called_once()

    def test_resume_endpoint(self):
        """Test resume endpoint calls orchestrator.resume()."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/resume")

        assert response.status_code == 200
        assert response.json()["status"] == "resumed"
        mock_orch.resume.assert_called_once()

    def test_pause_when_orchestrator_not_running(self):
        """Test pause returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.post("/api/pause")

        assert response.status_code == 503
        assert "error" in response.json()

    def test_resume_when_orchestrator_not_running(self):
        """Test resume returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.post("/api/resume")

        assert response.status_code == 503
        assert "error" in response.json()


class TestFocusSessionEndpoint:
    """Test the POST /api/focus/{issue_number} endpoint."""

    def test_focus_session_success(self):
        """Test successful session focus."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        # Mock session_runner.focus_session
        mock_orch.session_runner.focus_session.return_value = True
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/focus/1")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "focused"
        assert data["issue_number"] == 1
        mock_orch.session_runner.focus_session.assert_called_once_with(1, "issue-1")

    def test_focus_session_failure(self):
        """Test focus returns error when focus_session fails."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        # Mock session_runner.focus_session returning False (failed to focus)
        mock_orch.session_runner.focus_session.return_value = False
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/focus/1")

        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        mock_orch.session_runner.focus_session.assert_called_once_with(1, "issue-1")

    def test_focus_session_not_found(self):
        """Test focus returns 404 when session not found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/focus/999")

        assert response.status_code == 404
        assert "error" in response.json()

class TestRevealWorktreeEndpoint:
    """Test the POST /api/host/reveal-worktree/{issue_number} endpoint."""

    def test_reveal_worktree_success(self):
        """Test successful worktree reveal via client host."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        worktree_path = Path("/tmp/worktree-1")
        session = create_session(issue, worktree_path=str(worktree_path))
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        set_client_host(_StubClientHost())

        session.worktree_path = MagicMock()
        session.worktree_path.exists.return_value = True
        session.worktree_path.__str__.return_value = str(worktree_path)

        client = TestClient(app)
        response = client.post("/api/host/reveal-worktree/1")

        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "opened"
        assert data["path"] == str(worktree_path)

    def test_reveal_worktree_session_not_found(self):
        """Test worktree reveal returns 404 when session not found."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/host/reveal-worktree/999")

        assert response.status_code == 404
        assert "error" in response.json()

    def test_reveal_worktree_not_found(self):
        """Test worktree reveal returns 404 when worktree doesn't exist."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)

        # Mock the path exists check to return False
        session.worktree_path = MagicMock()
        session.worktree_path.exists.return_value = False

        client = TestClient(app)
        response = client.post("/api/host/reveal-worktree/1")

        assert response.status_code == 404
        assert "error" in response.json()

    def test_reveal_worktree_unsupported_host(self):
        """Test worktree reveal returns copy-path fallback when unsupported."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        set_client_host(UnsupportedClientHost())

        session.worktree_path = MagicMock()
        session.worktree_path.exists.return_value = True
        session.worktree_path.__str__.return_value = "/tmp/worktree-1"

        client = TestClient(app)
        response = client.post("/api/host/reveal-worktree/1")

        assert response.status_code == 409
        data = response.json()
        assert data["action"] == "copy_path"
        assert data["path"] == "/tmp/worktree-1"

    def test_finder_alias_still_reveals_worktree(self):
        """Deprecated Finder alias remains wired during transition."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]

        set_orchestrator(mock_orch)
        set_client_host(_StubClientHost())

        session.worktree_path = MagicMock()
        session.worktree_path.exists.return_value = True
        session.worktree_path.__str__.return_value = "/tmp/worktree-1"

        client = TestClient(app)
        response = client.post("/api/finder/1")

        assert response.status_code == 200
        assert response.json()["action"] == "opened"


class TestHostOpenPathEndpoint:
    """Test the host path opening endpoints."""

    def test_open_host_path_success(self):
        set_client_host(_StubClientHost())

        with patch("issue_orchestrator.entrypoints.web_operator_routes.Path.exists") as mock_exists:
            mock_exists.return_value = True
            client = TestClient(app)
            response = client.post("/api/host/open-path", json={"path": "/tmp/ui-session.log"})

        assert response.status_code == 200
        assert response.json()["action"] == "opened"
        assert response.json()["path"] == "/tmp/ui-session.log"

    def test_open_host_path_unsupported_returns_copy_hint(self):
        set_client_host(UnsupportedClientHost())

        with patch("issue_orchestrator.entrypoints.web_operator_routes.Path.exists") as mock_exists:
            mock_exists.return_value = True
            client = TestClient(app)
            response = client.post("/api/host/open-path", json={"path": "/tmp/ui-session.log"})

        assert response.status_code == 409
        data = response.json()
        assert data["action"] == "copy_path"
        assert data["path"] == "/tmp/ui-session.log"

    def test_open_host_path_allows_configured_issue_worktree(
        self, tmp_path: Path
    ):
        """Timeline worktree actions point at generated issue worktree roots."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web_operator_routes import (
            install_web_operator_dependencies,
        )

        # Avoid pytest's tmp_path because CI places it under /tmp, which is
        # already a legacy safe prefix for this endpoint.
        base = Path("/workspace") / f"issue-orchestrator-{tmp_path.name}"
        host_repo = base / "tixmeup"
        worktree = base / "tixmeup-360"

        client_host = _StubClientHost()
        set_client_host(client_host)
        install_web_operator_dependencies(
            app,
            get_client_host=web.get_client_host,
            broadcast_event=web.broadcast_event,
            trigger_server_shutdown=web.trigger_server_shutdown,
            get_host_repo_root=lambda: host_repo,
            get_worktree_base=lambda: base,
        )

        client = TestClient(app)
        with patch(
            "issue_orchestrator.entrypoints.web_operator_routes.Path.exists"
        ) as mock_exists:
            mock_exists.return_value = True
            response = client.post(
                "/api/host/open-path",
                json={"path": str(worktree)},
            )

        assert response.status_code == 200, response.json()
        assert response.json()["action"] == "opened"
        assert response.json()["path"] == str(worktree)

    def test_open_host_path_rejects_non_worktree_sibling(
        self, tmp_path: Path
    ):
        """The worktree allowance must stay tied to generated issue names."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web_operator_routes import (
            install_web_operator_dependencies,
        )

        # Avoid pytest's tmp_path because CI places it under /tmp, which is
        # already a legacy safe prefix for this endpoint.
        base = Path("/workspace") / f"issue-orchestrator-{tmp_path.name}"
        host_repo = base / "tixmeup"
        unrelated = base / "tixmeup-secrets"

        client_host = _StubClientHost()
        set_client_host(client_host)
        install_web_operator_dependencies(
            app,
            get_client_host=web.get_client_host,
            broadcast_event=web.broadcast_event,
            trigger_server_shutdown=web.trigger_server_shutdown,
            get_host_repo_root=lambda: host_repo,
            get_worktree_base=lambda: tmp_path,
        )

        client = TestClient(app)
        response = client.post(
            "/api/host/open-path",
            json={"path": str(unrelated)},
        )

        assert response.status_code == 403
        assert response.json()["error"] == "Cannot open files outside safe directories"

    def test_open_host_path_falls_back_to_host_repo_session_mirror(
        self, tmp_path: Path
    ):
        """Bug 1 regression: agent worktrees are deleted after PR merge,
        but the SESSION_COMPLETED event payloads still carry absolute
        paths rooted at the now-deleted worktree. The same files survive
        in the host repo's session mirror under the same suffix
        (``.issue-orchestrator/sessions/<session>/<file>``).

        When the menu's ``open_path`` action sends one of these stale
        absolute paths, the endpoint must re-anchor it against the
        host repo and open the surviving copy instead of returning 404.
        """
        from issue_orchestrator.entrypoints import web

        # Create a host repo with a session-mirror file.
        host_repo = tmp_path / "tixmeup-362"
        session_dir = host_repo / ".issue-orchestrator" / "sessions" / "coding-1"
        session_dir.mkdir(parents=True)
        completion_record = session_dir / "completion-record.json"
        completion_record.write_text('{"outcome": "completed"}')

        # The menu sends an absolute path rooted at the agent worktree
        # — which never existed (or was cleaned up). Same suffix.
        stale_agent_worktree_path = (
            f"{tmp_path}/tixmeup-362-coding-1"
            f"/.issue-orchestrator/sessions/coding-1/completion-record.json"
        )
        assert not Path(stale_agent_worktree_path).exists()

        client_host = _StubClientHost()
        set_client_host(client_host)
        # Inject the orchestrator's host repo root via the operator deps.
        from issue_orchestrator.entrypoints.web_operator_routes import (
            install_web_operator_dependencies,
        )
        install_web_operator_dependencies(
            app,
            get_client_host=web.get_client_host,
            broadcast_event=web.broadcast_event,
            trigger_server_shutdown=web.trigger_server_shutdown,
            get_host_repo_root=lambda: host_repo,
        )

        client = TestClient(app)
        response = client.post(
            "/api/host/open-path",
            json={"path": stale_agent_worktree_path},
        )

        assert response.status_code == 200, response.json()
        # The endpoint resolved against the host mirror — the file that
        # actually got opened is the one in the host repo, not the
        # stale agent-worktree path.
        assert response.json()["path"] == str(completion_record)

    def test_open_host_path_rejects_path_traversal_via_dotdot_in_suffix(
        self, tmp_path: Path
    ):
        """Security regression: a request with ``..`` segments after
        ``.issue-orchestrator/sessions/`` could otherwise escape
        ``host_repo_root`` via the OS resolver. Containment check
        must reject the resolved path when it falls outside the
        host repo root, even if a file would exist at the
        traversed location.
        """
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web_operator_routes import (
            install_web_operator_dependencies,
        )

        host_repo = tmp_path / "tixmeup-362"
        host_repo.mkdir(parents=True)
        # Create an attractive target *outside* the host repo. If the
        # resolver were broken, the OS path resolution would happily
        # land on this file via ``..`` segments.
        outside_target = tmp_path / "secret.txt"
        outside_target.write_text("not for the dashboard")

        client_host = _StubClientHost()
        set_client_host(client_host)
        install_web_operator_dependencies(
            app,
            get_client_host=web.get_client_host,
            broadcast_event=web.broadcast_event,
            trigger_server_shutdown=web.trigger_server_shutdown,
            get_host_repo_root=lambda: host_repo,
        )

        traversal = (
            f"{tmp_path}/missing-worktree"
            "/.issue-orchestrator/sessions/x/../../../../secret.txt"
        )
        client = TestClient(app)
        response = client.post("/api/host/open-path", json={"path": traversal})
        assert response.status_code == 404, response.json()

    def test_open_host_path_no_fallback_when_host_repo_unknown(
        self, tmp_path: Path
    ):
        """If the orchestrator hasn't been initialized (no host repo
        root), the endpoint must still return 404 rather than guessing.
        Quiet failure beats silent open of a wrong file."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.entrypoints.web_operator_routes import (
            install_web_operator_dependencies,
        )

        client_host = _StubClientHost()
        set_client_host(client_host)
        install_web_operator_dependencies(
            app,
            get_client_host=web.get_client_host,
            broadcast_event=web.broadcast_event,
            trigger_server_shutdown=web.trigger_server_shutdown,
            get_host_repo_root=lambda: None,
        )

        stale = (
            f"{tmp_path}/missing-worktree"
            f"/.issue-orchestrator/sessions/coding-1/completion-record.json"
        )
        client = TestClient(app)
        response = client.post("/api/host/open-path", json={"path": stale})
        assert response.status_code == 404


class TestPromptEndpoint:
    """Test the POST /api/prompt/{agent_type} endpoint."""

    def test_open_agent_prompt_success(self):
        """Test successful prompt file opening."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        set_client_host(_StubClientHost())

        # Ensure prompt path exists in mock
        prompt_path = MagicMock()
        prompt_path.exists.return_value = True
        prompt_path.is_absolute.return_value = True
        prompt_path.__str__.return_value = "/tmp/prompt.txt"
        mock_orch.config.agents["agent:web"].prompt_path = prompt_path

        client = TestClient(app)
        response = client.post("/api/prompt/web")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "opened"
        assert data["path"] == "/tmp/prompt.txt"

    def test_open_agent_prompt_with_agent_prefix(self):
        """Test opening prompt with 'agent:' prefix."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        set_client_host(_StubClientHost())

        prompt_path = MagicMock()
        prompt_path.exists.return_value = True
        prompt_path.is_absolute.return_value = True
        prompt_path.__str__.return_value = "/tmp/prompt.txt"
        mock_orch.config.agents["agent:web"].prompt_path = prompt_path

        client = TestClient(app)
        response = client.post("/api/prompt/agent:web")

        assert response.status_code == 200

    def test_open_agent_prompt_unsupported_host_returns_copy_hint(self):
        """Non-local UI hosts return a copy-path response instead of opening locally."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        set_client_host(UnsupportedClientHost())

        prompt_path = MagicMock()
        prompt_path.exists.return_value = True
        prompt_path.is_absolute.return_value = True
        prompt_path.__str__.return_value = "/tmp/prompt.txt"
        mock_orch.config.agents["agent:web"].prompt_path = prompt_path

        client = TestClient(app)
        response = client.post("/api/prompt/web")

        assert response.status_code == 409
        data = response.json()
        assert data["status"] == "copy_path"
        assert data["action"] == "copy_path"
        assert data["path"] == "/tmp/prompt.txt"

    def test_open_agent_prompt_not_found(self):
        """Test opening prompt for unknown agent type."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/prompt/unknown")

        assert response.status_code == 404
        assert "error" in response.json()

    def test_open_agent_prompt_file_not_found(self):
        """Test opening prompt when file doesn't exist."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        # Mock prompt_path to not exist
        prompt_path = MagicMock()
        prompt_path.exists.return_value = False
        prompt_path.is_absolute.return_value = True
        mock_orch.config.agents["agent:web"].prompt_path = prompt_path

        client = TestClient(app)
        response = client.post("/api/prompt/web")

        assert response.status_code == 404
        assert "error" in response.json()


class TestShutdownEndpoint:
    """Test the POST /api/shutdown endpoint."""

    def test_shutdown_success(self):
        """Test successful shutdown request with required reason."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post(
            "/api/shutdown",
            json={"reason": "test shutdown", "actor": "unit-test"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "shutdown_requested"
        assert body["reason"] == "test shutdown"
        assert body["actor"] == "unit-test"
        mock_orch.request_shutdown.assert_called_once()

    def test_shutdown_rejects_missing_reason(self):
        """Empty body → 400. Reason is required so the orchestrator
        log records the calling intent (signal handler can't
        attribute SIGTERM to a caller)."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/shutdown")

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "reason is required"
        assert "hint" in body
        mock_orch.request_shutdown.assert_not_called()

    def test_shutdown_rejects_empty_reason(self):
        """Whitespace-only reason is treated as missing."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.post("/api/shutdown", json={"reason": "  "})

        assert response.status_code == 400
        assert response.json()["error"] == "reason is required"
        mock_orch.request_shutdown.assert_not_called()

    def test_shutdown_when_orchestrator_not_running(self):
        """Test shutdown returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.post(
            "/api/shutdown",
            json={"reason": "test shutdown when down"},
        )

        assert response.status_code == 503
        assert "error" in response.json()


class TestInfoEndpoint:
    """Test the GET /api/info endpoint."""

    def test_get_info_success(self):
        """Test successful info retrieval."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        set_client_host(
            _StubClientHost(
                capabilities=ClientHostCapabilities(
                    open_path=True,
                    reveal_worktree=True,
                )
            )
        )

        # Add some active sessions
        issue = create_issue(1)
        session = create_session(issue)
        mock_orch.state.active_sessions = [session]
        mock_orch.state.completed_today = [1, 2, 3]

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/info")

        assert response.status_code == 200
        data = response.json()
        assert data["repo"] == "owner/repo"
        assert data["configuration_mode"] == mock_orch.config.configuration_mode
        assert data["config_name"] == mock_orch.config.config_name
        assert data["config_fingerprint"] == mock_orch.config.config_fingerprint
        assert data["ui_mode"] == "web"
        assert data["max_sessions"] == 3
        assert data["active_sessions"] == 1
        assert data["completed_today"] == 3
        assert data["client_capabilities"]["open_path"] is True
        assert data["client_capabilities"]["reveal_worktree"] is True
        assert data["client_capabilities"]["focus_session"] is False
        assert "repo_identity" in data

    def test_get_info_when_orchestrator_not_running(self):
        """Test info returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.get("/api/info")

        assert response.status_code == 503
        assert "error" in response.json()


class TestConfigEndpoint:
    """Test the GET /api/config endpoint."""

    def test_get_config_success(self):
        """Test successful config file retrieval."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()

        config_content = "agents:\n  agent:web:\n    model: sonnet"

        with patch("issue_orchestrator.entrypoints.web.Path.exists") as mock_exists:
            with patch("issue_orchestrator.entrypoints.web.Path.read_text") as mock_read:
                mock_exists.return_value = True
                mock_read.return_value = config_content

                set_orchestrator(mock_orch)

                client = TestClient(app)
                response = client.get("/api/config")

                assert response.status_code == 200
                assert response.json()["config"] == config_content

    def test_get_config_file_not_found(self):
        """Test config endpoint when file doesn't exist."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.config.config_path = None

        set_orchestrator(mock_orch)

        client = TestClient(app)
        response = client.get("/api/config")

        assert response.status_code == 200
        assert "Config file not found" in response.json()["config"]
