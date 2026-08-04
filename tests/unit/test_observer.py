"""Unit tests for the observer module."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from issue_orchestrator.observation.observer import SessionObserver
from issue_orchestrator.execution.session_output_adapter import (
    session_output_dir,
    FileSystemSessionOutput,
)
from issue_orchestrator.ports.session_output import SessionOutput
from issue_orchestrator.domain.models import (
    Session,
    SessionStatus,
    Issue,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import PRInfo
from issue_orchestrator.ports.fresh_issue_reader import FreshIssueReadError
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from tests.unit.session_run_helpers import make_session_run_assets


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Config()
    config.repo = "owner/repo"
    config.max_concurrent_sessions = 3
    config.session_timeout_minutes = 45
    config.label_in_progress = "in-progress"
    config.label_blocked = "blocked"
    config.label_needs_human = "needs-human"
    config.ui_mode = "tmux"
    return config


@pytest.fixture
def mock_session_output():
    """Create a mock SessionOutput port."""
    return MagicMock(spec=SessionOutput)


@pytest.fixture
def mock_session_runner():
    """Create a mock SessionRunner port."""
    runner = MagicMock()
    runner.session_exists.return_value = False
    runner.session_exists_by_name.return_value = False
    runner.kill_session.return_value = None
    runner.send_to_session.return_value = False
    runner.send_to_session_by_name.return_value = False
    return runner


@pytest.fixture
def mock_repository_host():
    """Create a mock RepositoryHost port."""
    host = MagicMock()
    host.get_prs_for_branch.return_value = []
    host.add_label.return_value = None
    host.remove_label.return_value = None
    return host


@pytest.fixture
def mock_fresh_issue_reader():
    """Create a mock FreshIssueReader port."""
    reader = MagicMock()
    reader.read_issue_labels.return_value = []
    return reader


@pytest.fixture
def monitor(mock_config, mock_session_runner, mock_repository_host, mock_fresh_issue_reader):
    """Create a SessionObserver instance for testing."""
    return SessionObserver(
        mock_config,
        session_runner=mock_session_runner,
        repository_host=mock_repository_host,
        fresh_issue_reader=mock_fresh_issue_reader,
        session_output=FileSystemSessionOutput(),
    )


@pytest.fixture
def monitor_with_machines(mock_config, mock_session_runner, mock_repository_host, mock_fresh_issue_reader):
    """Create a SessionObserver instance with mock session machines for testing."""
    return SessionObserver(
        mock_config,
        session_machines={},
        session_runner=mock_session_runner,
        repository_host=mock_repository_host,
        fresh_issue_reader=mock_fresh_issue_reader,
        session_output=FileSystemSessionOutput(),
    )


@pytest.fixture
def sample_session(sample_agent_config, tmp_path):
    """Create a sample session for testing."""
    issue = Issue(
        number=123,
        title="Test Issue",
        labels=["agent:web"],
        body="Test body",
    )
    issue_key = FakeIssueKey(name="123")
    session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    return Session(
        key=session_key,
        issue=issue,
        agent_config=sample_agent_config,
        terminal_id="issue-123",
        worktree_path=tmp_path,
        branch_name="issue-123-test",
        run_assets=make_session_run_assets(
            tmp_path,
            session_name="issue-123",
        ),
    )


def set_session_worktree(session: Session, worktree_path) -> None:
    """Move a test session while preserving its typed run ownership."""
    session.worktree_path = worktree_path
    session.run_assets = make_session_run_assets(
        worktree_path,
        session_name=session.terminal_id,
    )


class TestSessionObserverInit:
    """Test SessionObserver initialization."""

    def test_init_with_config(self, mock_config):
        """Test initializing monitor with config."""
        monitor = SessionObserver(mock_config, mock_session_output)
        assert monitor.config == mock_config
        assert monitor.session_machines == {}

    def test_init_stores_config(self, mock_config):
        """Test that config is properly stored."""
        monitor = SessionObserver(mock_config, mock_session_output)
        assert monitor.config.repo == "owner/repo"
        assert monitor.config.max_concurrent_sessions == 3

    def test_init_with_session_machines(self, mock_config):
        """Test initializing monitor with session machines."""
        machines = {"issue-1": MagicMock(), "issue-2": MagicMock()}
        monitor = SessionObserver(mock_config, mock_session_output, session_machines=machines)
        assert monitor.config == mock_config
        assert monitor.session_machines == machines

    def test_init_session_machines_defaults_to_empty_dict(self, mock_config):
        """Test that session_machines defaults to empty dict when None."""
        monitor = SessionObserver(mock_config, mock_session_output, session_machines=None)
        assert monitor.session_machines == {}

    # Note: Tests for private port storage (_session_runner, _repository_host) removed
    # because they test implementation details. The behavior is tested through the
    # public methods like observe_session and check_session.


class TestCheckSession:
    """Test check_session method."""

    def test_check_session_timed_out(self, monitor, sample_session):
        """Test check_session returns TIMED_OUT when session times out."""
        # Set up session that has timed out
        old_time = datetime.now() - timedelta(minutes=60)
        sample_session.started_at = old_time
        sample_session.agent_config.timeout_minutes = 30

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.TIMED_OUT

    def test_check_session_timed_out_via_state_machine(self, monitor_with_machines, sample_session):
        """Test check_session returns TIMED_OUT when state machine detects timeout."""
        # Create a mock state machine that indicates timeout
        mock_machine = MagicMock()
        mock_machine.check_timeout.return_value = True
        mock_machine.get_runtime_minutes.return_value = 60.0
        mock_machine.timeout_minutes = 30

        # Add machine to monitor
        monitor_with_machines.session_machines[sample_session.terminal_id] = mock_machine

        status = monitor_with_machines.check_session(sample_session)

        assert status == SessionStatus.TIMED_OUT

    def test_check_session_not_timed_out_via_state_machine(
        self, monitor_with_machines, sample_session, mock_session_runner
    ):
        """Test check_session respects state machine when not timed out."""
        mock_machine = MagicMock()
        mock_machine.check_timeout.return_value = False
        mock_session_runner.session_exists_by_name.return_value = True

        monitor_with_machines.session_machines[sample_session.terminal_id] = mock_machine

        status = monitor_with_machines.check_session(sample_session)

        assert status == SessionStatus.RUNNING

    def test_check_session_fallback_timeout_when_no_machine(self, monitor, sample_session):
        """Test check_session uses session.is_timed_out when no state machine."""
        old_time = datetime.now() - timedelta(minutes=60)
        sample_session.started_at = old_time
        sample_session.agent_config.timeout_minutes = 30

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.TIMED_OUT

    def test_check_session_still_running(self, monitor, sample_session, mock_session_runner):
        """Test check_session returns RUNNING when session exists."""
        mock_session_runner.session_exists_by_name.return_value = True

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.RUNNING
        mock_session_runner.session_exists_by_name.assert_called_with(sample_session.terminal_id)

    def test_check_session_running_with_pr_sends_exit(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test check_session sends /exit when session has PR but still running."""
        mock_session_runner.session_exists_by_name.return_value = True
        mock_session_runner.send_to_session_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]
        sample_session.exit_sent = False

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.RUNNING
        mock_session_runner.send_to_session_by_name.assert_called_with(sample_session.terminal_id, "/exit")
        assert sample_session.exit_sent is True

    def test_check_session_running_exit_sent_only_once(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test that exit is only sent once."""
        mock_session_runner.session_exists_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]
        sample_session.exit_sent = True  # Already sent

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.RUNNING
        mock_session_runner.send_to_session_by_name.assert_not_called()

    def test_check_session_completed_with_pr(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test check_session returns COMPLETED when PR exists."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.COMPLETED

    def test_check_session_blocked_label(
        self,
        monitor,
        sample_session,
        mock_session_runner,
        mock_repository_host,
        mock_fresh_issue_reader,
    ):
        """Test check_session returns BLOCKED when issue has blocked label."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = []
        mock_fresh_issue_reader.read_issue_labels.return_value = ["blocked"]

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.BLOCKED

    def test_check_session_needs_human_label(
        self,
        monitor,
        sample_session,
        mock_session_runner,
        mock_repository_host,
        mock_fresh_issue_reader,
    ):
        """Test check_session returns NEEDS_HUMAN when issue has needs-human label."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = []
        mock_fresh_issue_reader.read_issue_labels.return_value = ["needs-human"]

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.NEEDS_HUMAN

    def test_check_session_failed_no_markers(
        self,
        monitor,
        sample_session,
        mock_session_runner,
        mock_repository_host,
        mock_fresh_issue_reader,
    ):
        """Test check_session returns FAILED when no success markers."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = []
        mock_fresh_issue_reader.read_issue_labels.return_value = []

        status = monitor.check_session(sample_session)

        assert status == SessionStatus.FAILED


class TestCheckAllSessions:
    """Test check_all_sessions method."""

    def test_check_all_sessions_empty_list(self, monitor):
        """Test check_all_sessions with empty list."""
        result = monitor.check_all_sessions([])
        assert result == {}

    def test_check_all_sessions_multiple(
        self, monitor, sample_session, sample_agent_config, tmp_path, mock_session_runner
    ):
        """Test check_all_sessions with multiple sessions."""
        # First session is running
        mock_session_runner.session_exists.side_effect = [True, False]

        session1 = sample_session
        issue2 = Issue(number=456, title="Issue 2", labels=["agent:web"])
        issue_key2 = FakeIssueKey(name="456")
        session_key2 = SessionKey(issue=issue_key2, task=TaskKind.CODE)
        session2 = Session(
            key=session_key2,
            issue=issue2,
            agent_config=sample_agent_config,
            terminal_id="issue-456",
            worktree_path=tmp_path / "worktree2",
            branch_name="issue-456-test",
            run_assets=make_session_run_assets(
                tmp_path / "worktree2",
                session_name="issue-456",
            ),
        )

        result = monitor.check_all_sessions([session1, session2])

        assert 123 in result
        assert 456 in result


class TestHandleCompletion:
    """Test handle_completion method.

    Note: The observer now only OBSERVES completion, it does NOT take actions.
    All actions (label ops, session cleanup, tab closing) are handled via
    the action system through CleanupSessionAction.
    """

    def test_handle_completion_timed_out_only_logs(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test handle_completion for TIMED_OUT only logs, doesn't kill session."""
        monitor.handle_completion(sample_session, SessionStatus.TIMED_OUT)

        # Observer only observes - cleanup handled via CleanupSessionAction
        mock_session_runner.kill_session.assert_not_called()
        mock_repository_host.add_label.assert_not_called()
        mock_repository_host.remove_label.assert_not_called()

    def test_handle_completion_failed_only_logs(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test handle_completion for FAILED only logs (handled via actions)."""
        monitor.handle_completion(sample_session, SessionStatus.FAILED)

        # Observer only observes - all actions handled via action system
        mock_session_runner.kill_session.assert_not_called()
        mock_repository_host.add_label.assert_not_called()
        mock_repository_host.remove_label.assert_not_called()

    def test_handle_completion_completed_only_logs(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test handle_completion for COMPLETED only logs, doesn't close tab."""
        monitor.handle_completion(sample_session, SessionStatus.COMPLETED)

        # Observer only observes - tab closing handled via CleanupSessionAction
        mock_session_runner.kill_session.assert_not_called()
        mock_repository_host.remove_label.assert_not_called()

    def test_handle_completion_blocked_only_logs(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test handle_completion for BLOCKED only logs."""
        monitor.handle_completion(sample_session, SessionStatus.BLOCKED)

        # Observer only observes
        mock_session_runner.kill_session.assert_not_called()
        mock_repository_host.add_label.assert_not_called()
        mock_repository_host.remove_label.assert_not_called()

    def test_handle_completion_needs_human_only_logs(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test handle_completion for NEEDS_HUMAN only logs."""
        monitor.handle_completion(sample_session, SessionStatus.NEEDS_HUMAN)

        # Labels handled via actions
        mock_repository_host.add_label.assert_not_called()
        mock_repository_host.remove_label.assert_not_called()

    # Note: Tab closing is now handled via CleanupSessionAction, not the observer.
    # Tests for config-based tab closing behavior are in test_planner.py and
    # test_action_applier.py where CleanupSessionAction is tested.


class TestSessionObserverIntegration:
    """Integration tests for SessionObserver."""

    def test_full_workflow_timed_out(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test full workflow for timed out session."""
        # Session has timed out
        old_time = datetime.now() - timedelta(minutes=60)
        sample_session.started_at = old_time
        sample_session.agent_config.timeout_minutes = 30

        # Check and handle
        status = monitor.check_session(sample_session)
        assert status == SessionStatus.TIMED_OUT

        monitor.handle_completion(sample_session, status)

        # Observer only observes - cleanup handled via CleanupSessionAction
        mock_session_runner.kill_session.assert_not_called()
        mock_repository_host.add_label.assert_not_called()

    def test_full_workflow_completed(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test full workflow for completed session."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]

        status = monitor.check_session(sample_session)
        assert status == SessionStatus.COMPLETED

        monitor.handle_completion(sample_session, status)

        # Observer only observes - all actions handled via action system
        mock_session_runner.kill_session.assert_not_called()
        mock_repository_host.remove_label.assert_not_called()


# Note: TestExtractSessionNumber class removed - it tested private method
# _extract_session_number. Session name parsing is an implementation detail.


class TestObserveSession:
    """Test observe_session method - the main observation loop."""

    def test_observe_session_returns_terminated_when_completion_json_exists(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that observe_session returns TERMINATED when valid completion.json exists."""
        import json
        from issue_orchestrator.observation.observation import SessionObservation

        # Create valid completion.json
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True, exist_ok=True)
        completion_file = completion_dir / "completion.json"
        completion_file.write_text(json.dumps({
            "session_id": "any-session-id",
            "timestamp": "2024-01-01T00:00:00Z",
            "outcome": "completed",
            "summary": "Work done",
        }))

        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.TERMINATED
        assert result.session_exists is False  # terminated() sets session_exists=False

    def test_observe_session_ignores_incomplete_completion_json(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that observe_session ignores completion.json missing required fields."""
        import json
        from issue_orchestrator.observation.observation import SessionObservation

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True, exist_ok=True)
        completion_file = completion_dir / "completion.json"
        # Missing required fields (session_id, timestamp, outcome, summary)
        completion_file.write_text(json.dumps({"partial": "data"}))

        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        # Should be RUNNING since completion.json is incomplete
        assert result.observation == SessionObservation.RUNNING

    def test_observe_session_ignores_malformed_json(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that observe_session ignores malformed completion.json."""
        from issue_orchestrator.observation.observation import SessionObservation

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True, exist_ok=True)
        completion_file = completion_dir / "completion.json"
        completion_file.write_text("{ not valid json")

        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        # Should be RUNNING since JSON is invalid
        assert result.observation == SessionObservation.RUNNING

    def test_observe_session_returns_timed_out_when_timeout_exceeded(
        self, monitor, sample_session, mock_session_runner
    ):
        """Test that observe_session returns TIMED_OUT when session exceeds timeout."""
        from datetime import timedelta
        from issue_orchestrator.observation.observation import SessionObservation

        # Set up session that has timed out
        old_time = datetime.now() - timedelta(minutes=60)
        sample_session.started_at = old_time
        sample_session.agent_config.timeout_minutes = 30

        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.TIMED_OUT
        assert result.timeout_exceeded is True

    def test_observe_session_returns_running_when_session_exists(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that observe_session returns RUNNING when session exists and not timed out."""
        from issue_orchestrator.observation.observation import SessionObservation

        set_session_worktree(sample_session, tmp_path)
        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.RUNNING
        assert result.session_exists is True

    def test_observe_session_returns_terminated_when_session_not_exists(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that observe_session returns TERMINATED when session doesn't exist.

        Note: Sessions must be older than the 60-second grace period to be marked
        as terminated. This prevents false terminations during startup when
        terminal session detection may be unreliable.
        """
        from datetime import datetime, timedelta
        from issue_orchestrator.observation.observation import SessionObservation

        set_session_worktree(sample_session, tmp_path)
        # Set session to be older than the 60-second grace period
        sample_session.started_at = datetime.now() - timedelta(seconds=120)
        mock_session_runner.session_exists_by_name.return_value = False

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.TERMINATED

    def test_observe_session_grace_period_prevents_early_termination(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that new sessions get a grace period before being marked as terminated.

        This prevents false terminations during startup when terminal session detection
        may be unreliable.
        """
        from datetime import datetime
        from issue_orchestrator.observation.observation import SessionObservation

        set_session_worktree(sample_session, tmp_path)
        # Session just started (within grace period)
        sample_session.started_at = datetime.now()
        mock_session_runner.session_exists_by_name.return_value = False

        result = monitor.observe_session(sample_session)

        # Should be treated as RUNNING during grace period, not TERMINATED
        assert result.observation == SessionObservation.RUNNING

    def test_observe_session_log_activity_prevents_termination(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test that active log file prevents termination even after grace period.

        If the session log was modified recently, the session is clearly active
        and shouldn't be terminated just because terminal session detection failed.
        """
        import os
        import time
        from datetime import datetime, timedelta
        from issue_orchestrator.observation.observation import SessionObservation

        set_session_worktree(sample_session, tmp_path)
        # Session is older than grace period
        sample_session.started_at = datetime.now() - timedelta(seconds=120)
        mock_session_runner.session_exists_by_name.return_value = False

        # Create an active session log (recently modified)
        log_dir = session_output_dir(tmp_path, sample_session.terminal_id)
        log_dir.mkdir(parents=True)
        log_file = log_dir / "ui-session.log"
        log_file.write_text("Claude is working...")
        # Touch the file to ensure it's "just modified"

        result = monitor.observe_session(sample_session)

        # Should be treated as RUNNING due to active log, not TERMINATED
        assert result.observation == SessionObservation.RUNNING

    def test_observe_session_sends_exit_when_session_has_pr(
        self, monitor, sample_session, mock_session_runner, mock_repository_host, tmp_path
    ):
        """Test that observe_session sends /exit when running session has a PR."""
        from issue_orchestrator.observation.observation import SessionObservation

        set_session_worktree(sample_session, tmp_path)
        sample_session.exit_sent = False
        mock_session_runner.session_exists_by_name.return_value = True
        mock_session_runner.send_to_session_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.RUNNING
        mock_session_runner.send_to_session_by_name.assert_called_with(sample_session.terminal_id, "/exit")
        assert sample_session.exit_sent is True

    def test_observe_session_does_not_resend_exit(
        self, monitor, sample_session, mock_session_runner, mock_repository_host, tmp_path
    ):
        """Test that observe_session doesn't resend /exit if already sent."""
        set_session_worktree(sample_session, tmp_path)
        sample_session.exit_sent = True
        mock_session_runner.session_exists_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.return_value = [
            PRInfo(number=1, url="https://...", title="PR", branch="test", labels=[], body="", state="open")
        ]

        monitor.observe_session(sample_session)

        mock_session_runner.send_to_session_by_name.assert_not_called()

    def test_observe_session_timeout_with_state_machine(
        self, monitor_with_machines, sample_session, mock_session_runner, tmp_path
    ):
        """Test timeout detection via state machine takes priority."""
        from issue_orchestrator.observation.observation import SessionObservation

        set_session_worktree(sample_session, tmp_path)

        mock_machine = MagicMock()
        mock_machine.get_runtime_minutes.return_value = 60.0
        mock_machine.timeout_minutes = 30

        monitor_with_machines.session_machines[sample_session.terminal_id] = mock_machine
        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor_with_machines.observe_session(sample_session)

        assert result.observation == SessionObservation.TIMED_OUT
        assert result.timeout_exceeded is True

    def test_completion_record_does_not_mask_timeout(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Completion detection should not downgrade an over-time session to terminated."""
        import json

        from issue_orchestrator.events import EventName
        from issue_orchestrator.observation.observation import SessionObservation

        mock_events = MagicMock()
        monitor = SessionObserver(
            mock_config,
            FileSystemSessionOutput(),
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)
        sample_session.started_at = datetime.now() - timedelta(
            minutes=sample_session.agent_config.timeout_minutes + 2
        )
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True, exist_ok=True)
        completion_file = completion_dir / "completion.json"
        completion_file.write_text(
            json.dumps({
                "session_id": "any-session-id",
                "timestamp": "2024-01-01T00:00:00Z",
                "outcome": "completed",
                "summary": "Work done",
            }),
            encoding="utf-8",
        )
        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.TIMED_OUT
        assert result.timeout_exceeded is True
        assert result.session_exists is True
        completion_events = [
            call.args[0]
            for call in mock_events.publish.call_args_list
            if call.args[0].name == EventName.OBSERVATION_COMPLETION_DETECTED
        ]
        assert len(completion_events) == 1

    def test_observe_session_emits_event_on_completion_detected(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that observe_session emits event when completion.json is detected."""
        import json
        from issue_orchestrator.events import EventName
        from issue_orchestrator.ports import TraceEvent

        mock_events = MagicMock()
        monitor = SessionObserver(
            mock_config,
            FileSystemSessionOutput(),
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True, exist_ok=True)
        completion_file = completion_dir / "completion.json"
        completion_file.write_text(json.dumps({
            "session_id": "any-session-id",
            "timestamp": "2024-01-01T00:00:00Z",
            "outcome": "completed",
            "summary": "Work done",
        }))

        mock_session_runner.session_exists_by_name.return_value = True

        monitor.observe_session(sample_session)

        # Verify event was published
        mock_events.publish.assert_called()
        call_args = mock_events.publish.call_args_list[0]
        event = call_args[0][0]
        assert event.name == EventName.OBSERVATION_COMPLETION_DETECTED

    def test_observe_session_emits_completion_event_only_once_per_session(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Regression: COMPLETION_DETECTED must not fire on every tick.

        While a session waits in a deferred state (e.g. background review
        exchange), the observer is re-invoked each tick. Without idempotency,
        the user-facing 'Agent finished coding' event spams the timeline.
        """
        import json
        from issue_orchestrator.events import EventName
        from issue_orchestrator.observation.observation import SessionObservation

        mock_events = MagicMock()
        monitor = SessionObserver(
            mock_config,
            FileSystemSessionOutput(),
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True, exist_ok=True)
        completion_file = completion_dir / "completion.json"
        completion_file.write_text(json.dumps({
            "session_id": "any-session-id",
            "timestamp": "2024-01-01T00:00:00Z",
            "outcome": "completed",
            "summary": "Work done",
        }))

        mock_session_runner.session_exists_by_name.return_value = True

        # Simulate three observation ticks while the session lingers in a
        # deferred state (e.g. background review exchange).
        results = [monitor.observe_session(sample_session) for _ in range(3)]

        # Each tick still reports termination so the controller can re-evaluate.
        assert all(r.observation == SessionObservation.TERMINATED for r in results)
        assert sample_session.completion_detected_at is not None

        # But the user-visible event fires exactly once.
        completion_events = [
            call.args[0]
            for call in mock_events.publish.call_args_list
            if call.args[0].name == EventName.OBSERVATION_COMPLETION_DETECTED
        ]
        assert len(completion_events) == 1, (
            f"Expected COMPLETION_DETECTED once, got {len(completion_events)}: "
            f"{[e.data for e in completion_events]}"
        )


# Note: TestReadLogTail class was removed as it tested private method _read_log_tail.
# Log reading is an implementation detail.


# Note: TestPortDelegation class removed - it tested private methods for handling
# None ports. The fallback behavior is implicitly tested via the public methods.


class TestCheckSessionExceptionHandling:
    """Test exception handling in check_session."""

    def test_check_session_handles_pr_lookup_exception(
        self, monitor, sample_session, mock_session_runner, mock_repository_host
    ):
        """Test check_session handles exceptions when checking for PRs."""
        mock_session_runner.session_exists_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.side_effect = Exception("Network error")
        sample_session.exit_sent = False

        # Should not raise, should return RUNNING
        status = monitor.check_session(sample_session)
        assert status == SessionStatus.RUNNING

    def test_check_session_handles_label_lookup_exception(
        self,
        monitor,
        sample_session,
        mock_session_runner,
        mock_repository_host,
        mock_fresh_issue_reader,
    ):
        """Test check_session falls back to stale labels on exception."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = []
        mock_fresh_issue_reader.read_issue_labels.side_effect = Exception("API error")

        # This session's issue carries no status labels, so the fallback and a
        # fabricated empty set are indistinguishable here — which is exactly why
        # the two tests below exist.
        status = monitor.check_session(sample_session)
        assert status == SessionStatus.FAILED

    @pytest.mark.parametrize(
        "last_known,expected",
        [
            ("blocked", SessionStatus.BLOCKED),
            ("needs-human", SessionStatus.NEEDS_HUMAN),
        ],
    )
    def test_an_unreadable_issue_preserves_its_last_known_classification(
        self,
        monitor,
        sample_agent_config,
        tmp_path,
        mock_session_runner,
        mock_repository_host,
        mock_fresh_issue_reader,
        last_known,
        expected,
    ):
        """#6957 R3 F7: the fallback must PRESERVE status, not flatten to FAILED.

        The adapter now raises instead of fabricating ``[]`` (#6957 round-2 F4),
        so this path runs in production for the first time. An empty set would
        classify a genuinely blocked or needs-human session as a plain FAILURE,
        which sends it down a different recovery lane; the last-known labels are
        the honest answer when GitHub cannot be read.
        """
        issue = Issue(
            number=321,
            title="Blocked work",
            labels=["agent:web", last_known],
            body="",
        )
        session = Session(
            key=SessionKey(issue=FakeIssueKey(name="321"), task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-321",
            worktree_path=tmp_path,
            branch_name="issue-321-test",
            run_assets=make_session_run_assets(tmp_path, session_name="issue-321"),
        )
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.return_value = []
        mock_fresh_issue_reader.read_issue_labels.side_effect = FreshIssueReadError(
            "could not read fresh labels for issue #321: rate limited"
        )

        assert monitor.check_session(session) == expected


class TestCheckAllSessionsExceptionHandling:
    """Test exception handling in check_all_sessions."""

    def test_check_all_sessions_handles_individual_session_errors(
        self, monitor, sample_session, sample_agent_config, tmp_path, mock_session_runner
    ):
        """Test check_all_sessions continues after individual session errors."""
        # Set up to raise on first session
        mock_session_runner.session_exists_by_name.side_effect = [
            Exception("Error on first"),
            False,  # Second session just doesn't exist
        ]
        mock_session_runner.session_exists.side_effect = [
            Exception("Error on first"),
            False,
        ]

        issue2 = Issue(number=456, title="Issue 2", labels=["agent:web"])
        issue_key2 = FakeIssueKey(name="456")
        session_key2 = SessionKey(issue=issue_key2, task=TaskKind.CODE)
        session2 = Session(
            key=session_key2,
            issue=issue2,
            agent_config=sample_agent_config,
            terminal_id="issue-456",
            worktree_path=tmp_path / "worktree2",
            branch_name="issue-456-test",
            run_assets=make_session_run_assets(
                tmp_path / "worktree2",
                session_name="issue-456",
            ),
        )

        result = monitor.check_all_sessions([sample_session, session2])

        # Both should be in results, first one FAILED due to error
        assert 123 in result
        assert result[123] == SessionStatus.FAILED
        assert 456 in result


class TestObserveSessionExceptionHandling:
    """Test exception handling in observe_session."""

    def test_observe_session_handles_pr_check_exception(
        self, monitor, sample_session, mock_session_runner, mock_repository_host, tmp_path
    ):
        """Test observe_session catches exceptions when checking for PRs."""
        from issue_orchestrator.observation.observation import SessionObservation

        set_session_worktree(sample_session, tmp_path)
        sample_session.exit_sent = False
        mock_session_runner.session_exists_by_name.return_value = True
        mock_repository_host.get_prs_for_branch.side_effect = Exception("Network error")

        # Should not raise, should return RUNNING
        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.RUNNING
        # exit_sent should remain False since PR check failed
        assert sample_session.exit_sent is False


# Note: TestEmitNoOutputEdgeCases class is at the end of this file with noqa: SLF001 comments
# to suppress private member access warnings, since it tests user-facing idle detection behavior.


class TestCheckSessionPRLookupOnExit:
    """Test PR lookup exception handling after session exits."""

    def test_check_session_handles_pr_lookup_exception_after_exit(
        self,
        monitor,
        sample_session,
        mock_session_runner,
        mock_repository_host,
        mock_fresh_issue_reader,
    ):
        """Test check_session handles exception when looking up PRs for exited session."""
        mock_session_runner.session_exists_by_name.return_value = False
        mock_repository_host.get_prs_for_branch.side_effect = Exception("API error")

        # Should fall through to label checking, then to FAILED
        status = monitor.check_session(sample_session)

        # PR check failed, so should check labels next
        mock_fresh_issue_reader.read_issue_labels.assert_called()
        assert status == SessionStatus.FAILED


class TestTerminalObserverIntegration:
    """Tests for TerminalObserver integration in observe_session."""

    @pytest.fixture
    def mock_terminal_observer(self):
        """Create a mock TerminalObserver."""
        from issue_orchestrator.domain import ProcessState, ProcessExitInfo
        observer = MagicMock()
        observer.get_process_state.return_value = ProcessState.UNKNOWN
        observer.get_exit_info.return_value = None
        observer.is_process_alive.return_value = False
        observer.capture_full_output.return_value = None
        return observer

    @pytest.fixture
    def monitor_with_terminal_observer(
        self, mock_config, mock_session_runner, mock_repository_host, mock_terminal_observer
    ):
        """Create a SessionObserver with terminal observer."""
        return SessionObserver(
            mock_config,
            FileSystemSessionOutput(),
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
            terminal_observer=mock_terminal_observer,
        )

    def test_observe_uses_terminal_observer_running(
        self, monitor_with_terminal_observer, sample_session, mock_terminal_observer,
        mock_session_runner, tmp_path
    ):
        """Test observe_session uses pane_dead for RUNNING detection."""
        from issue_orchestrator.domain import ProcessState
        from issue_orchestrator.observation.observation import SessionObservation

        set_session_worktree(sample_session, tmp_path)
        mock_terminal_observer.get_process_state.return_value = ProcessState.RUNNING
        # Window check returns False - but pane_dead says RUNNING should win
        mock_session_runner.session_exists_by_name.return_value = False

        result = monitor_with_terminal_observer.observe_session(sample_session)

        # TerminalObserver says process is running, so should be RUNNING
        assert result.observation == SessionObservation.RUNNING
        mock_terminal_observer.get_process_state.assert_called_with(sample_session.terminal_id)

    def test_observe_uses_terminal_observer_exited(
        self, monitor_with_terminal_observer, sample_session, mock_terminal_observer,
        mock_session_runner, tmp_path
    ):
        """Test observe_session uses pane_dead for EXITED detection."""
        from issue_orchestrator.domain import ProcessState, ProcessExitInfo
        from issue_orchestrator.observation.observation import SessionObservation
        from datetime import datetime

        set_session_worktree(sample_session, tmp_path)
        sample_session.started_at = datetime.now()
        mock_terminal_observer.get_process_state.return_value = ProcessState.EXITED
        mock_terminal_observer.get_exit_info.return_value = ProcessExitInfo(exit_code=1)
        # Window still exists (remain-on-exit), but process is dead
        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor_with_terminal_observer.observe_session(sample_session)

        # TerminalObserver says process exited, so should be TERMINATED
        assert result.observation == SessionObservation.TERMINATED
        mock_terminal_observer.get_exit_info.assert_called_with(sample_session.terminal_id)

    def test_observe_uses_terminal_observer_signaled(
        self, monitor_with_terminal_observer, sample_session, mock_terminal_observer,
        mock_session_runner, tmp_path
    ):
        """Test observe_session uses pane_dead for SIGNALED detection."""
        from issue_orchestrator.domain import ProcessState, ProcessExitInfo
        from issue_orchestrator.observation.observation import SessionObservation
        from datetime import datetime

        set_session_worktree(sample_session, tmp_path)
        sample_session.started_at = datetime.now()
        mock_terminal_observer.get_process_state.return_value = ProcessState.SIGNALED
        mock_terminal_observer.get_exit_info.return_value = ProcessExitInfo(
            exit_code=137, signal="SIGKILL"
        )
        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor_with_terminal_observer.observe_session(sample_session)

        # Process was signaled, so should be TERMINATED
        assert result.observation == SessionObservation.TERMINATED

    def test_observe_fallback_to_window_check_on_unknown(
        self, monitor_with_terminal_observer, sample_session, mock_terminal_observer,
        mock_session_runner, tmp_path
    ):
        """Test observe_session falls back to window check when process state is UNKNOWN."""
        from issue_orchestrator.domain import ProcessState
        from issue_orchestrator.observation.observation import SessionObservation

        set_session_worktree(sample_session, tmp_path)
        mock_terminal_observer.get_process_state.return_value = ProcessState.UNKNOWN
        # Window exists, so treat as running
        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor_with_terminal_observer.observe_session(sample_session)

        # UNKNOWN state falls back to window check, which returns True
        assert result.observation == SessionObservation.RUNNING

    def test_observe_without_terminal_observer_uses_window_check(
        self, monitor, sample_session, mock_session_runner, tmp_path
    ):
        """Test observe_session uses window check when no terminal observer."""
        from issue_orchestrator.observation.observation import SessionObservation

        set_session_worktree(sample_session, tmp_path)
        mock_session_runner.session_exists_by_name.return_value = True

        result = monitor.observe_session(sample_session)

        assert result.observation == SessionObservation.RUNNING
        # No terminal observer, just window check
        mock_session_runner.session_exists_by_name.assert_called_with(sample_session.terminal_id)

    def test_observe_emits_exit_info_in_event(
        self, mock_config, mock_session_runner, mock_repository_host,
        mock_terminal_observer, sample_session, tmp_path
    ):
        """Test observe_session includes exit info in observation event."""
        from issue_orchestrator.domain import ProcessState, ProcessExitInfo
        from issue_orchestrator.events import EventName
        from datetime import datetime

        mock_events = MagicMock()
        monitor = SessionObserver(
            mock_config,
            FileSystemSessionOutput(),
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
            terminal_observer=mock_terminal_observer,
        )

        set_session_worktree(sample_session, tmp_path)
        sample_session.started_at = datetime.now()
        mock_terminal_observer.get_process_state.return_value = ProcessState.EXITED
        mock_terminal_observer.get_exit_info.return_value = ProcessExitInfo(
            exit_code=1, signal=None
        )
        mock_session_runner.session_exists_by_name.return_value = True

        monitor.observe_session(sample_session)

        # Check that event was emitted with exit info
        calls = mock_events.publish.call_args_list
        observation_event = None
        for call_obj in calls:
            event = call_obj[0][0]
            if event.name == EventName.OBSERVATION_RESULT:
                observation_event = event
                break

        assert observation_event is not None
        assert observation_event.data.get("exit_code") == 1
        assert observation_event.data.get("exit_signal") is None


class TestEmitNoOutputIfStale:
    """Test _emit_no_output_if_stale method for detecting idle sessions.

    These tests verify SESSION_NO_OUTPUT event emission for idle session detection.
    Users depend on these events to identify stuck sessions. The tests access
    the private _emit_no_output_if_stale method directly because:
    1. The behavior is user-facing (SESSION_NO_OUTPUT events trigger interventions)
    2. Testing through observe_session would require complex timing manipulation
    3. The method encapsulates distinct, testable idle detection logic
    """

    def test_emit_no_output_when_log_unchanged_for_threshold(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no_output event is emitted when log hasn't changed past threshold."""
        import time
        from issue_orchestrator.events import EventName

        mock_events = MagicMock()
        mock_config.session_no_output_seconds = 1  # 1 second for test speed
        mock_config.session_no_output_tail_lines = 10
        mock_config.session_no_output_max_bytes = 1000
        mock_config.session_no_output_repeat_seconds = 0  # Allow immediate repeat for testing

        monitor = SessionObserver(
            mock_config,
            FileSystemSessionOutput(),
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)

        # Create session log
        log_dir = session_output_dir(worktree, sample_session.terminal_id)
        log_dir.mkdir(parents=True)
        log_file = log_dir / "ui-session.log"
        log_file.write_text("Some log content\n")

        # First call - initialize tracking
        # noqa: SLF001 - testing idle detection behavior that emits SESSION_NO_OUTPUT events
        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001
        assert sample_session.last_output_monotonic is not None

        # Simulate time passing (adjust the monotonic timestamp)
        sample_session.last_output_monotonic = time.monotonic() - 10  # 10 seconds ago

        # Second call - should emit event
        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001

        # Verify event was published
        mock_events.publish.assert_called()
        event = mock_events.publish.call_args[0][0]
        assert event.name == EventName.SESSION_NO_OUTPUT

    def test_no_emit_when_log_changes(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no event is emitted when log file changes."""
        import os
        import time

        mock_events = MagicMock()
        mock_config.session_no_output_seconds = 1
        mock_config.session_no_output_tail_lines = 10
        mock_config.session_no_output_max_bytes = 1000

        monitor = SessionObserver(
            mock_config,
            FileSystemSessionOutput(),
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)

        log_dir = session_output_dir(worktree, sample_session.terminal_id)
        log_dir.mkdir(parents=True)
        log_file = log_dir / "ui-session.log"
        log_file.write_text("Initial content\n")

        # First call - initialize
        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001

        # Change the log file
        now = time.time()
        log_file.write_text("Initial content\nNew content\n")
        os.utime(log_file, (now + 10, now + 10))

        # Second call - should NOT emit because log changed
        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001

        # Verify no session_no_output event (only calls would be for log updates)
        for call in mock_events.publish.call_args_list:
            event = call[0][0]
            assert event.name != "session.no_output"

    def test_no_emit_when_log_does_not_exist(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no event is emitted when log file doesn't exist."""
        mock_events = MagicMock()

        monitor = SessionObserver(
            mock_config,
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
            session_output=FileSystemSessionOutput(),
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)
        # Don't create log file

        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001

        mock_events.publish.assert_not_called()

    def test_respects_repeat_interval(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no_output event respects repeat interval."""
        import time

        mock_events = MagicMock()
        mock_config.session_no_output_seconds = 1
        mock_config.session_no_output_tail_lines = 10
        mock_config.session_no_output_max_bytes = 1000
        mock_config.session_no_output_repeat_seconds = 60  # 60 second cooldown

        monitor = SessionObserver(
            mock_config,
            FileSystemSessionOutput(),
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)

        log_dir = session_output_dir(worktree, sample_session.terminal_id)
        log_dir.mkdir(parents=True)
        log_file = log_dir / "ui-session.log"
        log_file.write_text("Some content\n")

        # Initialize
        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001
        sample_session.last_output_monotonic = time.monotonic() - 10  # 10 seconds ago

        # First emission
        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001
        first_call_count = mock_events.publish.call_count

        # Try again immediately - should be suppressed by repeat interval
        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001

        assert mock_events.publish.call_count == first_call_count  # No new calls


class TestEmitNoOutputEdgeCases:
    """Test edge cases in _emit_no_output_if_stale.

    These tests verify edge cases in idle session detection that affect
    SESSION_NO_OUTPUT event emission. Users depend on correct handling of
    OSError, threshold boundaries, and state initialization.
    """

    def test_no_emit_when_stat_raises_oserror(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no event is emitted when stat() raises OSError."""
        mock_events = MagicMock()

        # Create a mock session_output that returns a path that fails on stat()
        mock_session_output = MagicMock()
        mock_log_path = MagicMock()
        mock_log_path.exists.return_value = True
        mock_log_path.stat.side_effect = OSError("Permission denied")
        mock_session_output.get_log_path.return_value = mock_log_path

        monitor = SessionObserver(
            mock_config,
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
            session_output=mock_session_output,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)

        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001

        # No events should be emitted
        mock_events.publish.assert_not_called()

    def test_no_emit_when_idle_time_under_threshold(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no event is emitted when idle time is under threshold."""
        import time

        mock_events = MagicMock()
        mock_config.session_no_output_seconds = 600  # 10 minute threshold
        mock_config.session_no_output_tail_lines = 10
        mock_config.session_no_output_max_bytes = 1000

        monitor = SessionObserver(
            mock_config,
            FileSystemSessionOutput(),
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)

        log_dir = session_output_dir(worktree, sample_session.terminal_id)
        log_dir.mkdir(parents=True)
        log_file = log_dir / "ui-session.log"
        log_file.write_text("content")

        # Initialize tracking
        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001

        # Only 5 seconds ago - under 600 second threshold
        sample_session.last_output_monotonic = time.monotonic() - 5

        # Second call - should NOT emit because under threshold
        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001

        # No session_no_output event should be emitted
        mock_events.publish.assert_not_called()

    def test_no_emit_when_last_output_monotonic_is_none(
        self, mock_config, mock_session_runner, mock_repository_host, sample_session, tmp_path
    ):
        """Test that no event is emitted when last_output_monotonic is None but log unchanged."""
        mock_events = MagicMock()
        mock_config.session_no_output_seconds = 1
        mock_config.session_no_output_tail_lines = 10
        mock_config.session_no_output_max_bytes = 1000

        monitor = SessionObserver(
            mock_config,
            FileSystemSessionOutput(),
            events=mock_events,
            session_runner=mock_session_runner,
            repository_host=mock_repository_host,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        set_session_worktree(sample_session, worktree)

        log_dir = session_output_dir(worktree, sample_session.terminal_id)
        log_dir.mkdir(parents=True)
        log_file = log_dir / "ui-session.log"
        log_file.write_text("content")

        # Initialize - this sets the mtime/size tracking
        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001

        # Clear last_output_monotonic to simulate edge case
        sample_session.last_output_monotonic = None

        # Second call - should return early because last_output_monotonic is None
        monitor._emit_no_output_if_stale(sample_session)  # noqa: SLF001

        # No events should be emitted
        mock_events.publish.assert_not_called()
