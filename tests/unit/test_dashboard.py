"""Unit tests for the dashboard module.

This test suite focuses on testing the logic and behavior of the dashboard
components without requiring a real terminal. The Textual TUI framework is
tested through mocking.

Test Coverage:
1. StatusBar - Tests content rendering with different orchestrator states
2. SessionsTable - Tests session display, title truncation, and status colors
3. QueueTable - Tests queue display and filtering of active sessions
4. DashboardApp - Tests keyboard actions (pause, resume, attach, quit)
5. Dashboard - Tests the wrapper class and its handlers
6. run_with_dashboard - Tests async orchestration between dashboard and orchestrator

What's NOT tested (requires integration tests):
- Actual Textual rendering and display
- Real keyboard input handling
- Tmux integration (mocked)
- The _refresh_loop method (requires running Textual app)
- The compose() methods (returns Textual widgets)

Notes:
- All curses/Textual components are mocked
- Focus is on data transformation and logic, not UI rendering
- No timing-dependent tests (all synchronous logic)
"""

import asyncio
import pytest

from issue_orchestrator.domain.pause_state import PauseActor, PauseReason, PauseState
from tests.conftest import operator_paused_state
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
from tests.unit.threading_helpers import wait_for_async_event
from issue_orchestrator.entrypoints.dashboard import (
    StatusBar,
    SessionsTable,
    QueueTable,
    DashboardApp,
    Dashboard,
    run_with_dashboard,
)
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    AgentConfig,
    OrchestratorState,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from tests.unit.session_run_helpers import make_session_run_assets


# Helper functions
def create_issue(number, title="Test Issue", labels=None, milestone=None):
    """Helper to create Issue objects for testing."""
    if labels is None:
        labels = ["agent:web"]
    return Issue(
        number=number,
        title=title,
        labels=labels,
        milestone=milestone,
    )


def create_session(issue, worktree_path="/tmp/worktree", branch_name="feature/test"):
    """Helper to create Session objects for testing.

    Note: For tests that need to verify runtime_minutes behavior, use MagicMock
    sessions instead, which most tests already do.
    """
    agent_config = AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        model="sonnet",
        timeout_minutes=45,
    )
    issue_key = FakeIssueKey(name=str(issue.number))
    session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    session = Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id=f"issue-{issue.number}",
        worktree_path=Path(worktree_path),
        branch_name=branch_name,
        run_assets=make_session_run_assets(
            Path(worktree_path),
            session_name=f"issue-{issue.number}",
        ),
    )
    return session


def create_orchestrator(config=None):
    """Helper to create a mock orchestrator for testing."""
    if config is None:
        config = Config()
        config.max_concurrent_sessions = 3
        config.worktree_base = Path("/tmp")  # Top-level worktree_base
        agent_config = AgentConfig(
            prompt_path=Path("/tmp/prompt.txt"),
            model="sonnet",
            timeout_minutes=45,
        )
        config.agents["agent:web"] = agent_config

    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.state = OrchestratorState()
    orchestrator.shutdown_requested = False

    return orchestrator


class TestStatusBar:
    """Test the StatusBar widget."""

    def test_init(self):
        """Test StatusBar initialization."""
        orchestrator = create_orchestrator()
        status_bar = StatusBar(orchestrator)

        assert status_bar.orchestrator == orchestrator

    def test_render_content_running(self):
        """Test rendering content when orchestrator is running."""
        orchestrator = create_orchestrator()
        orchestrator.state.pause_state = PauseState.running()
        orchestrator.state.active_sessions = []
        orchestrator.state.completed_today = []

        status_bar = StatusBar(orchestrator)
        with patch.object(status_bar, 'update') as mock_update:
            status_bar.refresh_content()

            mock_update.assert_called_once()
            content = mock_update.call_args[0][0]
            assert "[green]RUNNING[/green]" in content
            assert "Active: 0/3" in content
            assert "Completed: 0" in content

    def test_render_content_paused(self):
        """Test rendering content when orchestrator is paused."""
        orchestrator = create_orchestrator()
        orchestrator.state.pause_state = operator_paused_state()

        status_bar = StatusBar(orchestrator)
        with patch.object(status_bar, 'update') as mock_update:
            status_bar.refresh_content()

            content = mock_update.call_args[0][0]
            assert "[yellow]PAUSED[/yellow]" in content

    def test_render_content_with_sessions(self):
        """Test rendering content with active sessions."""
        orchestrator = create_orchestrator()
        issue1 = create_issue(1)
        issue2 = create_issue(2)

        # Use MagicMock sessions for runtime_minutes behavior
        session1 = MagicMock()
        session1.issue = issue1
        session2 = MagicMock()
        session2.issue = issue2

        orchestrator.state.active_sessions = [session1, session2]
        orchestrator.state.completed_today = [3, 4, 5]

        status_bar = StatusBar(orchestrator)
        with patch.object(status_bar, 'update') as mock_update:
            status_bar.refresh_content()

            content = mock_update.call_args[0][0]
            assert "Active: 2/3" in content
            assert "Completed: 3" in content

    def test_refresh_content_calls_update(self):
        """Test that refresh_content updates the widget."""
        orchestrator = create_orchestrator()
        status_bar = StatusBar(orchestrator)

        with patch.object(status_bar, 'update') as mock_update:
            status_bar.refresh_content()

            mock_update.assert_called_once()
            args = mock_update.call_args[0]
            assert isinstance(args[0], str)


class TestSessionsTable:
    """Test the SessionsTable widget."""

    def test_init(self):
        """Test SessionsTable initialization."""
        orchestrator = create_orchestrator()
        table = SessionsTable(orchestrator)

        assert table.orchestrator == orchestrator

    @patch('issue_orchestrator.entrypoints.dashboard.SessionsTable.query_one')
    def test_update_table_no_sessions(self, mock_query_one):
        """Test updating table with no active sessions."""
        mock_data_table = MagicMock()
        mock_query_one.return_value = mock_data_table

        orchestrator = create_orchestrator()
        orchestrator.state.active_sessions = []

        table = SessionsTable(orchestrator)
        table.update_table()

        mock_data_table.clear.assert_called_once()
        mock_data_table.add_row.assert_called_once_with(
            "-", "No active sessions", "-", "-", "-"
        )

    @patch('issue_orchestrator.entrypoints.dashboard.SessionsTable.query_one')
    def test_update_table_with_sessions(self, mock_query_one):
        """Test updating table with active sessions."""
        mock_data_table = MagicMock()
        mock_query_one.return_value = mock_data_table

        orchestrator = create_orchestrator()
        issue1 = create_issue(1, "Test Issue 1")
        issue2 = create_issue(2, "Test Issue 2")

        # Create sessions with mocked runtime_minutes
        session1 = MagicMock()
        session1.issue = issue1
        session1.runtime_minutes = 10
        session1.agent_config.timeout_minutes = 45

        session2 = MagicMock()
        session2.issue = issue2
        session2.runtime_minutes = 50  # Over timeout
        session2.agent_config.timeout_minutes = 45

        orchestrator.state.active_sessions = [session1, session2]

        table = SessionsTable(orchestrator)
        table.update_table()

        mock_data_table.clear.assert_called_once()
        assert mock_data_table.add_row.call_count == 2

    @patch('issue_orchestrator.entrypoints.dashboard.SessionsTable.query_one')
    def test_update_table_truncates_long_titles(self, mock_query_one):
        """Test that long issue titles are truncated."""
        mock_data_table = MagicMock()
        mock_query_one.return_value = mock_data_table

        orchestrator = create_orchestrator()
        long_title = "A" * 60  # Title longer than 40 characters
        issue = create_issue(1, long_title)

        session = MagicMock()
        session.issue = issue
        session.runtime_minutes = 10
        session.agent_config.timeout_minutes = 45

        orchestrator.state.active_sessions = [session]

        table = SessionsTable(orchestrator)
        table.update_table()

        # Check that the title was truncated
        call_args = mock_data_table.add_row.call_args[0]
        title_arg = call_args[1]
        assert len(title_arg) <= 40
        assert title_arg.endswith("...")

    @patch('issue_orchestrator.entrypoints.dashboard.SessionsTable.query_one')
    def test_update_table_status_colors(self, mock_query_one):
        """Test that sessions have correct status colors."""
        from rich.text import Text

        mock_data_table = MagicMock()
        mock_query_one.return_value = mock_data_table

        orchestrator = create_orchestrator()
        issue1 = create_issue(1)

        # Session under timeout (green)
        session1 = MagicMock()
        session1.issue = issue1
        session1.runtime_minutes = 10
        session1.agent_config.timeout_minutes = 45

        orchestrator.state.active_sessions = [session1]

        table = SessionsTable(orchestrator)
        table.update_table()

        # Verify the Text object was created with green style
        call_args = mock_data_table.add_row.call_args[0]
        status_text = call_args[2]
        assert isinstance(status_text, Text)
        assert status_text.style == "green"


class TestQueueTable:
    """Test the QueueTable widget."""

    def test_init(self):
        """Test QueueTable initialization."""
        orchestrator = create_orchestrator()
        table = QueueTable(orchestrator)

        assert table.orchestrator == orchestrator

    @patch('issue_orchestrator.entrypoints.dashboard.QueueTable.query_one')
    def test_update_table_empty_queue(self, mock_query_one):
        """Test updating table with empty queue."""
        mock_data_table = MagicMock()
        mock_query_one.return_value = mock_data_table

        orchestrator = create_orchestrator()
        orchestrator.state.priority_queue = []

        table = QueueTable(orchestrator)
        table.update_table()

        mock_data_table.clear.assert_called_once()
        mock_data_table.add_row.assert_called_once_with("-", "Queue empty", "-")

    @patch('issue_orchestrator.entrypoints.dashboard.QueueTable.query_one')
    def test_update_table_with_queue(self, mock_query_one):
        """Test updating table with queued issues."""
        mock_data_table = MagicMock()
        mock_query_one.return_value = mock_data_table

        orchestrator = create_orchestrator()
        orchestrator.state.priority_queue = [1, 2, 3, 4, 5]
        orchestrator.state.active_sessions = []

        table = QueueTable(orchestrator)
        table.update_table()

        mock_data_table.clear.assert_called_once()
        assert mock_data_table.add_row.call_count == 5

    @patch('issue_orchestrator.entrypoints.dashboard.QueueTable.query_one')
    def test_update_table_excludes_active_sessions(self, mock_query_one):
        """Test that active session issue numbers are excluded from queue."""
        mock_data_table = MagicMock()
        mock_query_one.return_value = mock_data_table

        orchestrator = create_orchestrator()
        issue1 = create_issue(1)

        session1 = MagicMock()
        session1.issue = issue1

        orchestrator.state.priority_queue = [1, 2, 3]
        orchestrator.state.active_sessions = [session1]

        table = QueueTable(orchestrator)
        table.update_table()

        # Should only show issues 2 and 3 (issue 1 is active)
        assert mock_data_table.add_row.call_count == 2

    @patch('issue_orchestrator.entrypoints.dashboard.QueueTable.query_one')
    def test_update_table_limits_to_10_items(self, mock_query_one):
        """Test that queue table only shows first 10 items."""
        mock_data_table = MagicMock()
        mock_query_one.return_value = mock_data_table

        orchestrator = create_orchestrator()
        orchestrator.state.priority_queue = list(range(1, 20))  # 19 items
        orchestrator.state.active_sessions = []

        table = QueueTable(orchestrator)
        table.update_table()

        # Should only show first 10
        assert mock_data_table.add_row.call_count == 10


class TestDashboardApp:
    """Test the DashboardApp class."""

    def test_init_with_orchestrator(self):
        """Test DashboardApp initialization."""
        orchestrator = create_orchestrator()
        app = DashboardApp(orchestrator)

        assert app.orchestrator == orchestrator

    def test_init_with_callbacks(self):
        """Test DashboardApp initialization with callbacks - verifies callbacks are stored."""
        orchestrator = create_orchestrator()
        on_pause = AsyncMock()
        on_resume = AsyncMock()
        on_next = AsyncMock()
        on_attach = AsyncMock()

        app = DashboardApp(
            orchestrator,
            on_pause=on_pause,
            on_resume=on_resume,
            on_next=on_next,
            on_attach=on_attach,
        )

        # Verify callbacks are stored on the app instance
        assert app.orchestrator == orchestrator
        assert app._on_pause == on_pause  # noqa: SLF001 - verifying callback storage
        assert app._on_resume == on_resume  # noqa: SLF001 - verifying callback storage
        assert app._on_next == on_next  # noqa: SLF001 - verifying callback storage
        assert app._on_attach == on_attach  # noqa: SLF001 - verifying callback storage

    @pytest.mark.asyncio
    async def test_action_quit_exits_app(self):
        """Test that quit action exits the app and cancels refresh task."""
        orchestrator = create_orchestrator()
        app = DashboardApp(orchestrator)

        # Set up a mock refresh task
        mock_task = MagicMock()
        app._refresh_task = mock_task  # noqa: SLF001 - test setup for verifying cleanup

        with patch.object(app, 'exit') as mock_exit:
            await app.action_quit()

            mock_exit.assert_called_once()
            app._refresh_task.cancel.assert_called_once()  # noqa: SLF001 - verifying cleanup

    @pytest.mark.asyncio
    async def test_action_pause_with_callback(self):
        """Test pause action with custom callback."""
        orchestrator = create_orchestrator()
        on_pause = AsyncMock()

        app = DashboardApp(orchestrator, on_pause=on_pause)

        with patch.object(app, 'notify') as mock_notify:
            await app.action_pause()

            on_pause.assert_called_once()
            mock_notify.assert_called_once_with("Orchestrator paused")

    @pytest.mark.asyncio
    async def test_action_pause_without_callback(self):
        """Test pause action without custom callback."""
        orchestrator = create_orchestrator()
        orchestrator.state.pause_state = PauseState.running()

        app = DashboardApp(orchestrator)

        with patch.object(app, 'notify') as mock_notify:
            await app.action_pause()

            # The dashboard no longer writes ``state.paused`` behind the owner's
            # back — that path recorded no event, no log line, and no journal
            # row. It now delegates, naming itself as the actor.
            orchestrator.pause.assert_called_once_with(
                reason=PauseReason.OPERATOR, actor=PauseActor.DASHBOARD
            )
            mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_action_resume_with_callback(self):
        """Test resume action with custom callback."""
        orchestrator = create_orchestrator()
        on_resume = AsyncMock()

        app = DashboardApp(orchestrator, on_resume=on_resume)

        with patch.object(app, 'notify') as mock_notify:
            await app.action_resume()

            on_resume.assert_called_once()
            mock_notify.assert_called_once_with("Orchestrator resumed")

    @pytest.mark.asyncio
    async def test_action_resume_without_callback(self):
        """Test resume action without custom callback."""
        orchestrator = create_orchestrator()
        orchestrator.state.pause_state = operator_paused_state()

        app = DashboardApp(orchestrator)

        with patch.object(app, 'notify') as mock_notify:
            await app.action_resume()

            orchestrator.resume.assert_called_once_with(actor=PauseActor.DASHBOARD)
            mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_action_next(self):
        """Test next issue action."""
        orchestrator = create_orchestrator()
        on_next = AsyncMock()

        app = DashboardApp(orchestrator, on_next=on_next)

        with patch.object(app, 'notify') as mock_notify:
            await app.action_next()

            on_next.assert_called_once()
            mock_notify.assert_called_once_with("Next issue prioritized")

    @pytest.mark.asyncio
    async def test_action_attach_with_valid_index(self):
        """Test attach action with valid session index."""
        orchestrator = create_orchestrator()
        issue1 = create_issue(1)

        session1 = MagicMock()
        session1.issue = issue1

        orchestrator.state.active_sessions = [session1]
        orchestrator.config.ui_mode = "tmux"

        on_attach = AsyncMock()
        app = DashboardApp(orchestrator, on_attach=on_attach)

        await app.action_attach(1)

        on_attach.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_action_attach_with_invalid_index(self):
        """Test attach action with invalid session index."""
        orchestrator = create_orchestrator()
        orchestrator.state.active_sessions = []

        app = DashboardApp(orchestrator)

        with patch.object(app, 'notify') as mock_notify:
            await app.action_attach(5)

            mock_notify.assert_called_once()
            assert "No session at index 5" in mock_notify.call_args[0][0]

    @pytest.mark.asyncio
    async def test_action_attach_web_mode(self):
        """Test attach action in web mode (browser-based dashboard stays open)."""
        orchestrator = create_orchestrator()
        issue1 = create_issue(1)

        session1 = MagicMock()
        session1.issue = issue1
        session1.terminal_id = "issue-1"

        orchestrator.state.active_sessions = [session1]
        orchestrator.config.ui_mode = "web"
        # Mock the session_runner.focus_session used by the refactored code
        orchestrator.session_runner.focus_session.return_value = True

        app = DashboardApp(orchestrator)

        with patch.object(app, 'notify') as mock_notify:
            await app.action_attach(1)

            orchestrator.session_runner.focus_session.assert_called_once_with(1, "issue-1")
            mock_notify.assert_called_once()
            assert "Switched to #1" in mock_notify.call_args[0][0]

    @pytest.mark.asyncio
    async def test_action_attach_tmux_mode(self):
        """Test attach action in tmux mode."""
        orchestrator = create_orchestrator()
        issue1 = create_issue(1)

        session1 = MagicMock()
        session1.issue = issue1
        session1.terminal_id = "issue-1"

        orchestrator.state.active_sessions = [session1]
        orchestrator.config.ui_mode = "tmux"
        # Mock the session_runner.focus_session used by the refactored code
        orchestrator.session_runner.focus_session.return_value = True

        app = DashboardApp(orchestrator)

        with patch.object(app, 'exit') as mock_exit:
            await app.action_attach(1)

            orchestrator.session_runner.focus_session.assert_called_once_with(1, "issue-1")
            mock_exit.assert_called_once()

    @pytest.mark.asyncio
    async def test_action_attach_handles_exception(self):
        """Test that attach action handles exceptions gracefully."""
        orchestrator = create_orchestrator()
        issue1 = create_issue(1)

        session1 = MagicMock()
        session1.issue = issue1
        session1.terminal_id = "issue-1"

        orchestrator.state.active_sessions = [session1]
        orchestrator.config.ui_mode = "tmux"
        # Mock focus_session to raise an exception
        orchestrator.session_runner.focus_session.side_effect = Exception("Test error")

        app = DashboardApp(orchestrator)

        with patch.object(app, 'notify') as mock_notify:
            await app.action_attach(1)

            mock_notify.assert_called()
            assert "Attach failed" in mock_notify.call_args[0][0]


class TestDashboard:
    """Test the Dashboard wrapper class."""

    def test_init(self):
        """Test Dashboard initialization."""
        orchestrator = create_orchestrator()
        dashboard = Dashboard(orchestrator, ui_mode="tmux")

        assert dashboard.orchestrator == orchestrator
        assert dashboard.ui_mode == "tmux"
        assert dashboard.attach_after_exit is False

    def test_init_web_mode(self):
        """Test Dashboard initialization with web mode."""
        orchestrator = create_orchestrator()
        dashboard = Dashboard(orchestrator, ui_mode="web")

        assert dashboard.ui_mode == "web"

    @pytest.mark.asyncio
    async def test_handle_pause(self):
        """Test pause handler."""
        orchestrator = create_orchestrator()
        orchestrator.state.pause_state = PauseState.running()

        dashboard = Dashboard(orchestrator)
        # noqa: SLF001 - testing internal handler that manages orchestrator state
        await dashboard._handle_pause()  # noqa: SLF001

        orchestrator.pause.assert_called_once_with(
            reason=PauseReason.OPERATOR, actor=PauseActor.DASHBOARD
        )

    @pytest.mark.asyncio
    async def test_handle_resume(self):
        """Test resume handler."""
        orchestrator = create_orchestrator()
        orchestrator.state.pause_state = operator_paused_state()

        dashboard = Dashboard(orchestrator)
        # noqa: SLF001 - testing internal handler that manages orchestrator state
        await dashboard._handle_resume()  # noqa: SLF001

        orchestrator.resume.assert_called_once_with(actor=PauseActor.DASHBOARD)

    @pytest.mark.asyncio
    async def test_handle_attach_tmux_mode(self):
        """Test attach handler in tmux mode."""
        orchestrator = create_orchestrator()
        # Create a session for issue 42
        issue42 = create_issue(42)
        session42 = MagicMock()
        session42.issue = issue42
        session42.terminal_id = "issue-42"
        orchestrator.state.active_sessions = [session42]
        # Mock session_runner.focus_session
        orchestrator.session_runner.focus_session.return_value = True

        dashboard = Dashboard(orchestrator, ui_mode="tmux")
        # noqa: SLF001 - test setup: injecting mock app to verify exit behavior
        dashboard._app = MagicMock()  # noqa: SLF001
        dashboard._app.exit = MagicMock()  # noqa: SLF001

        # noqa: SLF001 - testing internal handler that manages tmux session attachment
        await dashboard._handle_attach(42)  # noqa: SLF001

        orchestrator.session_runner.focus_session.assert_called_once_with(42, "issue-42")
        assert dashboard.attach_after_exit is True
        dashboard._app.exit.assert_called_once()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_handle_attach_web_mode(self):
        """Test attach handler in web mode (browser-based dashboard stays open)."""
        orchestrator = create_orchestrator()
        # Create a session for issue 42
        issue42 = create_issue(42)
        session42 = MagicMock()
        session42.issue = issue42
        session42.terminal_id = "issue-42"
        orchestrator.state.active_sessions = [session42]
        # Mock session_runner.focus_session
        orchestrator.session_runner.focus_session.return_value = True

        dashboard = Dashboard(orchestrator, ui_mode="web")
        # noqa: SLF001 - test setup: injecting mock app to verify notify behavior
        dashboard._app = MagicMock()  # noqa: SLF001
        dashboard._app.notify = MagicMock()  # noqa: SLF001

        # noqa: SLF001 - testing internal handler that manages web session attachment
        await dashboard._handle_attach(42)  # noqa: SLF001

        orchestrator.session_runner.focus_session.assert_called_once_with(42, "issue-42")
        dashboard._app.notify.assert_called_once()  # noqa: SLF001
        assert "Switched to #42" in dashboard._app.notify.call_args[0][0]  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_handle_attach_session_not_found(self):
        """Test attach handler when session is not found in active sessions."""
        orchestrator = create_orchestrator()
        # No sessions in active_sessions, so lookup will fail
        orchestrator.state.active_sessions = []

        dashboard = Dashboard(orchestrator, ui_mode="web")
        # noqa: SLF001 - test setup: injecting mock app to verify notify behavior
        dashboard._app = MagicMock()  # noqa: SLF001
        dashboard._app.notify = MagicMock()  # noqa: SLF001

        # noqa: SLF001 - testing internal handler error case
        await dashboard._handle_attach(42)  # noqa: SLF001

        dashboard._app.notify.assert_called_once()  # noqa: SLF001
        assert "not found" in dashboard._app.notify.call_args[0][0]  # noqa: SLF001

    def test_stop(self):
        """Test stopping the dashboard."""
        orchestrator = create_orchestrator()
        dashboard = Dashboard(orchestrator)
        # noqa: SLF001 - Test infrastructure: injecting mock to test stop() behavior
        dashboard._app = MagicMock()  # noqa: SLF001

        dashboard.stop()

        dashboard._app.exit.assert_called_once()  # noqa: SLF001

    def test_stop_when_no_app(self):
        """Test stopping the dashboard when no app exists."""
        orchestrator = create_orchestrator()
        dashboard = Dashboard(orchestrator)
        # noqa: SLF001 - Test infrastructure: setting up scenario where app is None
        dashboard._app = None  # noqa: SLF001

        # Should not raise exception
        dashboard.stop()


class TestRunWithDashboard:
    """Test the run_with_dashboard function."""

    @pytest.mark.asyncio
    async def test_run_with_dashboard_creates_dashboard(self):
        """Test that run_with_dashboard creates a Dashboard."""
        orchestrator = create_orchestrator()
        orchestrator.run_loop = AsyncMock()

        with patch('issue_orchestrator.entrypoints.dashboard.Dashboard.run', new_callable=AsyncMock) as mock_run:
            await run_with_dashboard(orchestrator, ui_mode="tmux")

            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_with_dashboard_starts_orchestrator(self):
        """Test that run_with_dashboard starts the orchestrator."""
        orchestrator = create_orchestrator()
        run_loop_started = asyncio.Event()

        async def mock_run_loop():
            run_loop_started.set()

        orchestrator.run_loop = mock_run_loop

        async def mock_dashboard_run():
            await wait_for_async_event(run_loop_started, timeout=1.0, label="run_loop_started")

        with patch('issue_orchestrator.entrypoints.dashboard.Dashboard.run', new_callable=AsyncMock, side_effect=mock_dashboard_run):
            await run_with_dashboard(orchestrator, ui_mode="tmux")

            assert run_loop_started.is_set() is True

    @pytest.mark.asyncio
    async def test_run_with_dashboard_sets_shutdown_on_exit(self):
        """Test that run_with_dashboard sets shutdown flag when dashboard exits."""
        orchestrator = create_orchestrator()
        orchestrator.run_loop = AsyncMock()
        orchestrator.shutdown_requested = False

        with patch('issue_orchestrator.entrypoints.dashboard.Dashboard.run', new_callable=AsyncMock):
            await run_with_dashboard(orchestrator, ui_mode="tmux")

            assert orchestrator.shutdown_requested is True

    @pytest.mark.asyncio
    async def test_run_with_dashboard_returns_attach_flag(self):
        """Test that run_with_dashboard returns the attach flag."""
        orchestrator = create_orchestrator()
        orchestrator.run_loop = AsyncMock()

        with patch('issue_orchestrator.entrypoints.dashboard.Dashboard') as mock_dashboard_class:
            mock_dashboard = MagicMock()
            mock_dashboard.attach_after_exit = True
            mock_dashboard.run = AsyncMock()
            mock_dashboard_class.return_value = mock_dashboard

            result = await run_with_dashboard(orchestrator, ui_mode="tmux")

            assert result is True

    @pytest.mark.asyncio
    async def test_run_with_dashboard_cancels_orchestrator_on_exit(self):
        """Test that orchestrator task is cancelled when dashboard exits."""
        orchestrator = create_orchestrator()
        orchestrator.run_loop = AsyncMock()
        run_loop_started = asyncio.Event()

        # Mock run_loop to run indefinitely
        async def mock_run_loop():
            try:
                run_loop_started.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        orchestrator.run_loop = mock_run_loop

        with patch('issue_orchestrator.entrypoints.dashboard.Dashboard.run', new_callable=AsyncMock):
            # Should complete without hanging
            result = await run_with_dashboard(orchestrator, ui_mode="tmux")

            assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_run_with_dashboard_web_mode(self):
        """Test run_with_dashboard with web mode."""
        orchestrator = create_orchestrator()
        orchestrator.run_loop = AsyncMock()

        with patch('issue_orchestrator.entrypoints.dashboard.Dashboard') as mock_dashboard_class:
            mock_dashboard = MagicMock()
            mock_dashboard.attach_after_exit = False
            mock_dashboard.run = AsyncMock()
            mock_dashboard_class.return_value = mock_dashboard

            await run_with_dashboard(orchestrator, ui_mode="web")

            # Verify Dashboard was created with web mode
            mock_dashboard_class.assert_called_once_with(orchestrator, ui_mode="web")


class TestStatusBarRendering:
    """Test StatusBar rendering edge cases."""

    def test_render_content_max_capacity(self):
        """Test rendering when at max capacity."""
        orchestrator = create_orchestrator()
        orchestrator.config.max_concurrent_sessions = 3

        # Create 3 sessions (at max)
        sessions = []
        for i in range(3):
            issue = create_issue(i + 1)
            session = MagicMock()
            session.issue = issue
            sessions.append(session)

        orchestrator.state.active_sessions = sessions

        status_bar = StatusBar(orchestrator)
        with patch.object(status_bar, 'update') as mock_update:
            status_bar.refresh_content()

            content = mock_update.call_args[0][0]
            assert "Active: 3/3" in content

    def test_render_content_many_completed(self):
        """Test rendering with many completed issues."""
        orchestrator = create_orchestrator()
        orchestrator.state.completed_today = list(range(1, 51))  # 50 completed

        status_bar = StatusBar(orchestrator)
        with patch.object(status_bar, 'update') as mock_update:
            status_bar.refresh_content()

            content = mock_update.call_args[0][0]
            assert "Completed: 50" in content


class TestTableEdgeCases:
    """Test edge cases for table widgets."""

    @patch('issue_orchestrator.entrypoints.dashboard.SessionsTable.query_one')
    def test_sessions_table_with_exact_40_char_title(self, mock_query_one):
        """Test that 40-character titles are not truncated."""
        mock_data_table = MagicMock()
        mock_query_one.return_value = mock_data_table

        orchestrator = create_orchestrator()
        title = "A" * 40  # Exactly 40 characters
        issue = create_issue(1, title)

        session = MagicMock()
        session.issue = issue
        session.runtime_minutes = 10
        session.agent_config.timeout_minutes = 45

        orchestrator.state.active_sessions = [session]

        table = SessionsTable(orchestrator)
        table.update_table()

        call_args = mock_data_table.add_row.call_args[0]
        title_arg = call_args[1]
        # 40 chars is the threshold, should show as-is
        assert len(title_arg) == 40
        assert not title_arg.endswith("...")

    @patch('issue_orchestrator.entrypoints.dashboard.SessionsTable.query_one')
    def test_sessions_table_runtime_at_timeout_threshold(self, mock_query_one):
        """Test session exactly at timeout threshold."""
        mock_data_table = MagicMock()
        mock_query_one.return_value = mock_data_table

        orchestrator = create_orchestrator()
        issue = create_issue(1)

        session = MagicMock()
        session.issue = issue
        session.runtime_minutes = 45  # Exactly at timeout
        session.agent_config.timeout_minutes = 45

        orchestrator.state.active_sessions = [session]

        table = SessionsTable(orchestrator)
        table.update_table()

        call_args = mock_data_table.add_row.call_args[0]
        status_text = call_args[2]
        # At timeout, status should be "slow" (yellow)
        assert status_text.style == "yellow"

    @patch('issue_orchestrator.entrypoints.dashboard.QueueTable.query_one')
    def test_queue_table_with_all_sessions_active(self, mock_query_one):
        """Test queue table when all queued issues are active."""
        mock_data_table = MagicMock()
        mock_query_one.return_value = mock_data_table

        orchestrator = create_orchestrator()

        # All queued issues are active
        issue1 = create_issue(1)
        issue2 = create_issue(2)

        session1 = MagicMock()
        session1.issue = issue1
        session2 = MagicMock()
        session2.issue = issue2

        orchestrator.state.priority_queue = [1, 2]
        orchestrator.state.active_sessions = [session1, session2]

        table = QueueTable(orchestrator)
        table.update_table()

        # Should show "Queue empty" since all queued issues are active
        mock_data_table.add_row.assert_called_once_with("-", "Queue empty", "-")
