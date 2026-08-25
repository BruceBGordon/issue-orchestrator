"""Unit tests for the SessionManager module."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.control.session_manager import (
    SessionManager,
    SessionRef,
    SessionType,
    SessionContext,
    issue_session_context,
    review_session_context,
    rework_session_context,
)
from issue_orchestrator.ports import NullEventSink, TraceEvent
from issue_orchestrator.domain.terminal_launch import TerminalLaunch, TerminalShell


class CollectingEventSink:
    """Event sink that collects events for test assertions."""

    def __init__(self):
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


class MockSessionRunner:
    """Mock SessionRunner for testing."""

    def __init__(self):
        self.sessions: dict[int, dict] = {}
        self.create_calls: list[dict] = []
        self.kill_calls: list[int] = []

    def create_session(
        self,
        session_id: int,
        launch: TerminalLaunch,
        working_dir: str,
        title: str | None = None,
        session_name: str | None = None,
    ) -> bool:
        self.create_calls.append(
            {
                "session_id": session_id,
                "session_name": session_name,
                "launch": launch,
                "working_dir": working_dir,
                "title": title,
            }
        )
        self.sessions[session_id] = {
            "launch": launch,
            "working_dir": working_dir,
            "title": title,
            "session_name": session_name,
        }
        return True

    def session_exists(self, session_id: int, session_name: str | None = None) -> bool:
        return session_id in self.sessions

    def kill_session(self, session_id: int, session_name: str | None = None) -> None:
        self.kill_calls.append(session_id)
        self.sessions.pop(session_id, None)

    def discover_running_sessions(self) -> list[dict]:
        return [
            {"tab_name": f"issue-{sid}", "issue_number": sid}
            for sid in self.sessions
        ]

    def cleanup_idle_sessions(self) -> int:
        return 0

    def get_session_output(self, session_id: int, lines: int = 50, session_name: str | None = None) -> str | None:
        if session_id in self.sessions:
            return f"Output for session {session_id}"
        return None

    def session_exists_by_name(self, session_name: str) -> bool:
        return False

    def send_to_session(self, session_id: int, text: str) -> bool:
        return False

    def send_to_session_by_name(self, session_name: str, text: str) -> bool:
        return False

    def focus_session(self, session_id: int) -> bool:
        return False

    def on_orchestrator_startup(self) -> None:
        pass

    def on_orchestrator_shutdown(self) -> None:
        pass


class TestSessionRef:
    """Test the SessionRef dataclass."""

    def test_name_property_for_issue(self):
        """Test name property for issue session."""
        ref = SessionRef(session_type=SessionType.ISSUE, number=123)
        assert ref.name == "issue-123"

    def test_name_property_for_review(self):
        """Test name property for review session."""
        ref = SessionRef(session_type=SessionType.REVIEW, number=456)
        assert ref.name == "review-456"

    def test_name_property_for_retrospective_review(self):
        """Test name property for retrospective review session."""
        ref = SessionRef(session_type=SessionType.RETROSPECTIVE_REVIEW, number=365)
        assert ref.name == "retrospective-review-365"

    def test_name_property_for_rework(self):
        """Test name property for rework session."""
        ref = SessionRef(session_type=SessionType.REWORK, number=789)
        assert ref.name == "rework-789"

    def test_from_name_parses_issue(self):
        """Test parsing issue session name."""
        ref = SessionRef.from_name("issue-123")
        assert ref.session_type == SessionType.ISSUE
        assert ref.number == 123

    def test_from_name_parses_review(self):
        """Test parsing review session name."""
        ref = SessionRef.from_name("review-456")
        assert ref.session_type == SessionType.REVIEW
        assert ref.number == 456

    def test_from_name_parses_retrospective_review(self):
        """Test parsing retrospective review session name."""
        ref = SessionRef.from_name("retrospective-review-365")
        assert ref.session_type == SessionType.RETROSPECTIVE_REVIEW
        assert ref.number == 365

    def test_from_name_parses_rework(self):
        """Test parsing rework session name."""
        ref = SessionRef.from_name("rework-789")
        assert ref.session_type == SessionType.REWORK
        assert ref.number == 789

    def test_from_name_invalid_format_raises(self):
        """Test that invalid format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid session name format"):
            SessionRef.from_name("invalid-name")

    def test_from_name_missing_number_raises(self):
        """Test that missing number raises ValueError."""
        with pytest.raises(ValueError, match="Invalid session name format"):
            SessionRef.from_name("issue-")

    def test_for_issue_factory(self):
        """Test for_issue factory method."""
        ref = SessionRef.for_issue(42)
        assert ref.session_type == SessionType.ISSUE
        assert ref.number == 42

    def test_for_review_factory(self):
        """Test for_review factory method."""
        ref = SessionRef.for_review(100)
        assert ref.session_type == SessionType.REVIEW
        assert ref.number == 100

    def test_for_retrospective_review_factory(self):
        """Test for_retrospective_review factory method."""
        ref = SessionRef.for_retrospective_review(365)
        assert ref.session_type == SessionType.RETROSPECTIVE_REVIEW
        assert ref.number == 365

    def test_for_rework_factory(self):
        """Test for_rework factory method."""
        ref = SessionRef.for_rework(200)
        assert ref.session_type == SessionType.REWORK
        assert ref.number == 200

    def test_ref_is_frozen(self):
        """Test that SessionRef is immutable."""
        ref = SessionRef(session_type=SessionType.ISSUE, number=123)
        with pytest.raises(AttributeError):
            ref.number = 456


class TestSessionManager:
    """Test the SessionManager class."""

    @pytest.fixture
    def mock_runner(self):
        return MockSessionRunner()

    @pytest.fixture
    def collecting_sink(self):
        return CollectingEventSink()

    @pytest.fixture
    def sample_config(self):
        config = MagicMock()
        config.repo_root = Path("/path/to/repo")
        return config

    @pytest.fixture
    def manager(self, mock_runner, collecting_sink, sample_config):
        return SessionManager(
            runner=mock_runner,
            events=collecting_sink,
            config=sample_config,
        )

    def test_start_creates_session(self, manager, mock_runner):
        """Test that start creates a session via runner."""
        ctx = SessionContext(
            ref=SessionRef.for_issue(123),
            launch=TerminalLaunch.classified("claude", TerminalShell.BASH),
            working_dir=Path("/path/to/worktree"),
            title="Issue #123",
        )

        result = manager.start(ctx)

        assert result is True
        assert len(mock_runner.create_calls) == 1
        call = mock_runner.create_calls[0]
        assert call["session_id"] == 123
        assert call["launch"].shell_command == "claude"
        assert call["working_dir"] == "/path/to/worktree"
        assert call["title"] == "Issue #123"

    def test_start_emits_session_launched_event(self, manager, collecting_sink):
        """Test that start emits session.launched event."""
        ctx = SessionContext(
            ref=SessionRef.for_issue(123),
            launch=TerminalLaunch.classified("claude", TerminalShell.BASH),
            working_dir=Path("/path/to/worktree"),
        )

        manager.start(ctx)

        assert len(collecting_sink.events) == 1
        event = collecting_sink.events[0]
        assert event.name == "session.launched"
        assert event.data["session_type"] == "issue"
        assert event.data["number"] == 123
        assert event.data["session_name"] == "issue-123"

    def test_stop_kills_session(self, manager, mock_runner):
        """Test that stop kills session via runner."""
        ref = SessionRef.for_issue(123)
        mock_runner.sessions[123] = {}  # Pre-create session

        manager.stop(ref)

        assert 123 in mock_runner.kill_calls
        assert 123 not in mock_runner.sessions

    def test_stop_emits_session_stopped_event(self, manager, collecting_sink):
        """Test that stop emits session.stopped event."""
        ref = SessionRef.for_issue(123)

        manager.stop(ref)

        assert len(collecting_sink.events) == 1
        event = collecting_sink.events[0]
        assert event.name == "session.stopped"
        assert event.data["session_type"] == "issue"
        assert event.data["number"] == 123

    def test_exists_returns_true_when_running(self, manager, mock_runner):
        """Test exists returns True for running session."""
        ref = SessionRef.for_issue(123)
        mock_runner.sessions[123] = {}  # Pre-create session

        assert manager.exists(ref) is True

    def test_exists_returns_false_when_not_running(self, manager, mock_runner):
        """Test exists returns False for non-existent session."""
        ref = SessionRef.for_issue(999)

        assert manager.exists(ref) is False

    def test_get_output_returns_output_for_running_session(self, manager, mock_runner):
        """Test get_output returns output for running session."""
        ref = SessionRef.for_issue(123)
        mock_runner.sessions[123] = {}  # Pre-create session

        output = manager.get_output(ref)

        assert output == "Output for session 123"

    def test_get_output_returns_none_for_non_existent(self, manager, mock_runner):
        """Test get_output returns None for non-existent session."""
        ref = SessionRef.for_issue(999)

        output = manager.get_output(ref)

        assert output is None

    def test_get_worktree_path(self, manager, sample_config):
        """Test worktree path generation."""
        path = manager.get_worktree_path(123)

        assert path == Path("/path/to/repo-123")

    def test_get_worktree_path_with_custom_base(self, manager):
        """Test worktree path with custom base directory."""
        path = manager.get_worktree_path(
            123,
            worktree_base=Path("/custom/base"),
            repo_root=Path("/path/to/myrepo"),
        )

        assert path == Path("/custom/base/myrepo-123")

    def test_discover_running_returns_refs(self, manager, mock_runner):
        """Test discover_running returns SessionRefs."""
        mock_runner.sessions[123] = {}
        mock_runner.sessions[456] = {}

        refs = manager.discover_running()

        assert len(refs) == 2
        numbers = {ref.number for ref in refs}
        assert numbers == {123, 456}


class TestSessionContextFactories:
    """Test the session context factory functions."""

    def test_issue_session_context(self):
        """Test issue session context factory."""
        ctx = issue_session_context(
            issue_number=123,
            command="claude",
            working_dir=Path("/path/to/worktree"),
        )

        assert ctx.ref.session_type == SessionType.ISSUE
        assert ctx.ref.number == 123
        assert ctx.launch.shell_command == "claude"
        assert ctx.working_dir == Path("/path/to/worktree")
        assert ctx.title == "Issue #123"

    def test_review_session_context(self):
        """Test review session context factory."""
        ctx = review_session_context(
            pr_number=456,
            command="claude --review",
            working_dir=Path("/path/to/worktree"),
        )

        assert ctx.ref.session_type == SessionType.REVIEW
        assert ctx.ref.number == 456
        assert ctx.launch.shell_command == "claude --review"
        assert ctx.title == "Review PR #456"

    def test_rework_session_context(self):
        """Test rework session context factory."""
        ctx = rework_session_context(
            issue_number=789,
            command="claude --rework",
            working_dir=Path("/path/to/worktree"),
        )

        assert ctx.ref.session_type == SessionType.REWORK
        assert ctx.ref.number == 789
        assert ctx.title == "Rework Issue #789"

    def test_custom_title_overrides_default(self):
        """Test that custom title overrides the default."""
        ctx = issue_session_context(
            issue_number=123,
            command="claude",
            working_dir=Path("/path"),
            title="Custom Title",
        )

        assert ctx.title == "Custom Title"
