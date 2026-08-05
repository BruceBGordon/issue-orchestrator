"""Integration tests for SessionController + CompletionProcessor flow.

These tests verify that when a session exits:
1. SessionController reads completion.json from the worktree via CompletionProcessor
2. CompletionProcessor executes requested actions (push, PR, labels)
3. Appropriate trace events are emitted
4. Correct SessionStatus is returned

This is the critical integration that connects agents to the orchestrator.
"""

import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

from issue_orchestrator.infra.config import Config, DangerousConfig
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionStatus,
    CompletionRecord,
    CompletionOutcome,
    RequestedAction,
    AgentConfig,
)
from issue_orchestrator.ports import TraceEvent, NullEventSink
from issue_orchestrator.control.session_controller import SessionController, SessionDecision
from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.observation.observation import SessionObservation, SessionObservationResult
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from tests.unit.session_run_helpers import make_session_run_assets
from tests.callback_endpoint_helpers import ready_callback_endpoint


def make_completion_record(
    outcome: CompletionOutcome,
    requested_actions: list[RequestedAction],
    session_id: str = "test-session",
    **kwargs
) -> CompletionRecord:
    """Helper to create a CompletionRecord with required fields."""
    return CompletionRecord(
        session_id=session_id,
        timestamp=datetime.now().isoformat(),
        outcome=outcome,
        summary="Test completion",
        requested_actions=requested_actions,
        **kwargs,
    )


def write_completion_to_worktree(worktree: Path, record: CompletionRecord) -> None:
    """Write completion record to worktree."""
    record_dir = worktree / ".issue-orchestrator"
    record_dir.mkdir(parents=True, exist_ok=True)
    record_path = record_dir / "completion.json"
    record_path.write_text(json.dumps(record.to_dict()))


@pytest.fixture
def test_config(tmp_path):
    """Create a real Config for testing."""
    config = Config()
    config.repo = "owner/repo"
    config.repo_root = tmp_path
    config.code_reviewed_label = "code-reviewed"
    config.code_review_label = "needs-code-review"
    config.ui_mode = "tmux"
    config.max_concurrent_sessions = 1
    config.dangerous = DangerousConfig(allow_unsupported_agents=True)
    return config


@pytest.fixture
def mock_label_adapter():
    """Mock label adapter for CompletionProcessor."""
    adapter = MagicMock()
    adapter.add_label = Mock()
    adapter.remove_label = Mock()
    return adapter


@pytest.fixture
def mock_pr_adapter():
    """Mock PR adapter for CompletionProcessor."""
    adapter = MagicMock()
    adapter.get_prs_for_issue = Mock(return_value=[])
    adapter.get_prs_for_branch = Mock(return_value=[])
    adapter.create_pr = Mock(return_value=MagicMock(number=42, url="https://github.com/owner/repo/pull/42"))
    adapter.add_comment = Mock()
    return adapter


@pytest.fixture
def mock_git_adapter():
    """Mock git adapter for CompletionProcessor."""
    adapter = MagicMock()
    adapter.get_current_branch = Mock(return_value="issue-123")
    adapter.has_uncommitted_changes = Mock(return_value=False)
    adapter.has_tracked_changes = Mock(return_value=False)
    adapter.push = Mock(return_value=MagicMock(success=True, message="Pushed"))
    adapter.rebase_on_branch = Mock(return_value=MagicMock(success=True, message="Rebased"))
    adapter.create_branch_from_current = Mock()
    adapter.list_branch_names = Mock(return_value=["issue-123"])
    return adapter


class MockEventSink:
    """Mock event sink that collects events for assertions."""

    def __init__(self):
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


@pytest.fixture
def mock_event_sink():
    """Create a mock event sink for testing."""
    return MockEventSink()


@pytest.fixture
def completion_processor(mock_label_adapter, mock_pr_adapter, mock_git_adapter):
    """Create a CompletionProcessor with mocked adapters."""
    return CompletionProcessor(
        agent_callback_endpoint=ready_callback_endpoint(),
        label_adapter=mock_label_adapter,
        pr_adapter=mock_pr_adapter,
        git_adapter=mock_git_adapter,
        session_output=FileSystemSessionOutput(),
    )


class StubWorkingCopy:
    """Stub WorkingCopy for testing."""

    def get_head_sha(self, worktree):
        return "abc1234567890"

    def get_current_branch(self, worktree):
        return "test-branch"


@pytest.fixture
def session_controller(completion_processor, mock_event_sink):
    """Create a SessionController with mocked dependencies."""
    return SessionController(
        completion_processor=completion_processor,
        events=mock_event_sink,
        session_output=FileSystemSessionOutput(),
        working_copy=StubWorkingCopy(),
    )


@pytest.fixture
def session_with_worktree(tmp_path):
    """Factory to create a Session with a real worktree."""
    def _create(issue_number: int = 123) -> Session:
        worktree = tmp_path / f"worktree-{issue_number}"
        worktree.mkdir(parents=True, exist_ok=True)

        # Initialize git repo in worktree properly
        import subprocess
        subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=worktree, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, capture_output=True)
        # Create initial commit so we can have a branch
        (worktree / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "."], cwd=worktree, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=worktree, capture_output=True)
        subprocess.run(["git", "checkout", "-b", f"issue-{issue_number}"], cwd=worktree, capture_output=True)

        terminal_id = f"issue-{issue_number}"

        # Create session output directory (required for fail-fast validation)
        session_output_dir = worktree / ".issue-orchestrator" / "sessions" / terminal_id
        session_output_dir.mkdir(parents=True, exist_ok=True)

        issue = Issue(
            number=issue_number,
            title="Test Issue",
            labels=["test"],
        )
        agent_config = AgentConfig(
            prompt_path=worktree / "prompt.md",
            timeout_minutes=30,
        )
        issue_key = FakeIssueKey(name=str(issue_number))
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
        return Session(
            key=session_key,
            issue=issue,
            terminal_id=terminal_id,
            branch_name=f"issue-{issue_number}",
            worktree_path=worktree,
            agent_config=agent_config,
            run_assets=make_session_run_assets(worktree, session_name=terminal_id),
        )
    return _create


class TestSessionControllerDecision:
    """Tests for SessionController.decide_outcome method."""

    def test_no_completion_record_returns_failed(
        self, session_controller, session_with_worktree, mock_event_sink
    ):
        """Session without completion.json should return FAILED status."""
        session = session_with_worktree()
        observation = SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=False,
        )

        decision = session_controller.decide_outcome(
            observation=observation,
            worktree_path=session.worktree_path,
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_name=session.terminal_id,
            session_run_assets=session.run_assets,
        )

        assert decision.status == SessionStatus.FAILED
        assert not decision.completion_processed
        # Should emit session.no_completion_record event
        events = mock_event_sink.events
        assert any(e.name == "session.no_completion_record" for e in events)

    def test_completed_outcome_returns_completed_status(
        self, session_controller, session_with_worktree, mock_event_sink
    ):
        """COMPLETED outcome should return COMPLETED status."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH],
            session_id=session.terminal_id,
            implementation="Added feature",
        )
        write_completion_to_worktree(session.worktree_path, record)
        observation = SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=False,
        )

        decision = session_controller.decide_outcome(
            observation=observation,
            worktree_path=session.worktree_path,
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_name=session.terminal_id,
            session_run_assets=session.run_assets,
        )

        assert decision.status == SessionStatus.COMPLETED
        assert decision.completion_processed
        # Should emit session.processing_completed event
        events = mock_event_sink.events
        event_names = [e.name for e in events]
        assert "session.processing_completed" in event_names

    def test_blocked_outcome_returns_blocked_status(
        self, session_controller, session_with_worktree, mock_label_adapter
    ):
        """BLOCKED outcome should return BLOCKED status and add label."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[RequestedAction.ADD_BLOCKED_LABEL],
            session_id=session.terminal_id,
            blocked_reason="Waiting for API access",
        )
        write_completion_to_worktree(session.worktree_path, record)
        observation = SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=False,
        )

        decision = session_controller.decide_outcome(
            observation=observation,
            worktree_path=session.worktree_path,
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_name=session.terminal_id,
            session_run_assets=session.run_assets,
        )

        assert decision.status == SessionStatus.BLOCKED
        # Should have called add_label with blocked
        mock_label_adapter.add_label.assert_called_once_with(session.issue.number, "blocked")

    def test_needs_human_outcome_returns_needs_human_status(
        self, session_controller, session_with_worktree, mock_label_adapter
    ):
        """NEEDS_HUMAN outcome should return NEEDS_HUMAN status and add label."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.NEEDS_HUMAN,
            requested_actions=[RequestedAction.ADD_NEEDS_HUMAN_LABEL],
            session_id=session.terminal_id,
            question="Which approach should I use?",
        )
        write_completion_to_worktree(session.worktree_path, record)
        observation = SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=False,
        )

        decision = session_controller.decide_outcome(
            observation=observation,
            worktree_path=session.worktree_path,
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_name=session.terminal_id,
            session_run_assets=session.run_assets,
        )

        assert decision.status == SessionStatus.NEEDS_HUMAN
        mock_label_adapter.add_label.assert_called_once_with(session.issue.number, "needs-human")

    def test_review_approved_returns_completed_status(
        self, session_controller, session_with_worktree, mock_label_adapter
    ):
        """REVIEW_APPROVED outcome should return COMPLETED status and update labels."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.REVIEW_APPROVED,
            requested_actions=[
                RequestedAction.ADD_CODE_REVIEWED_LABEL,
                RequestedAction.REMOVE_NEEDS_REWORK_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
            ],
            session_id=session.terminal_id,
            review_summary="LGTM",
        )
        write_completion_to_worktree(session.worktree_path, record)
        observation = SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=False,
        )

        decision = session_controller.decide_outcome(
            observation=observation,
            worktree_path=session.worktree_path,
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_name=session.terminal_id,
            session_run_assets=session.run_assets,
        )

        assert decision.status == SessionStatus.COMPLETED
        mock_label_adapter.add_label.assert_called_once_with(session.issue.number, "code-reviewed")
        mock_label_adapter.remove_label.assert_any_call(session.issue.number, "needs-rework")
        mock_label_adapter.remove_label.assert_any_call(session.issue.number, "code-review")

    def test_review_changes_requested_returns_completed_and_adds_rework_label(
        self, session_controller, session_with_worktree, mock_label_adapter
    ):
        """REVIEW_CHANGES_REQUESTED should return COMPLETED and add needs-rework label."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.REVIEW_CHANGES_REQUESTED,
            requested_actions=[
                RequestedAction.ADD_NEEDS_REWORK_LABEL,
                RequestedAction.REMOVE_CODE_REVIEW_LABEL,
            ],
            session_id=session.terminal_id,
            review_issues="Missing error handling",
        )
        write_completion_to_worktree(session.worktree_path, record)
        observation = SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=False,
        )

        decision = session_controller.decide_outcome(
            observation=observation,
            worktree_path=session.worktree_path,
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_name=session.terminal_id,
            session_run_assets=session.run_assets,
        )

        assert decision.status == SessionStatus.COMPLETED  # Review session completed its job
        mock_label_adapter.add_label.assert_called_once_with(session.issue.number, "needs-rework")
        mock_label_adapter.remove_label.assert_called_once_with(session.issue.number, "code-review")


class TestEventEmission:
    """Tests for trace event emission during completion processing."""

    def test_emits_processing_completed_event(
        self, session_controller, session_with_worktree, mock_event_sink
    ):
        """Should emit session.processing_completed event with action details."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
            session_id=session.terminal_id,
        )
        write_completion_to_worktree(session.worktree_path, record)
        observation = SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=False,
        )

        session_controller.decide_outcome(
            observation=observation,
            worktree_path=session.worktree_path,
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_name=session.terminal_id,
            session_run_assets=session.run_assets,
        )

        events = mock_event_sink.events
        processing_events = [e for e in events if e.name == "session.processing_completed"]
        assert len(processing_events) == 1
        assert processing_events[0].data["issue_number"] == session.issue.number
        assert processing_events[0].data["success"] is True

    def test_emits_processing_completed_on_blocked(
        self, session_controller, session_with_worktree, mock_event_sink
    ):
        """Should emit session.processing_completed event on successful processing."""
        session = session_with_worktree()
        record = make_completion_record(
            outcome=CompletionOutcome.BLOCKED,
            requested_actions=[RequestedAction.ADD_BLOCKED_LABEL],
            session_id=session.terminal_id,
        )
        write_completion_to_worktree(session.worktree_path, record)
        observation = SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=False,
        )

        session_controller.decide_outcome(
            observation=observation,
            worktree_path=session.worktree_path,
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_name=session.terminal_id,
            session_run_assets=session.run_assets,
        )

        events = mock_event_sink.events
        completed_events = [e for e in events if e.name == "session.processing_completed"]
        assert len(completed_events) == 1
        assert completed_events[0].data["success"] is True

    def test_emits_no_completion_record_when_missing(
        self, session_controller, session_with_worktree, mock_event_sink
    ):
        """Should emit session.no_completion_record event when no completion.json."""
        session = session_with_worktree()
        # Don't write completion record
        observation = SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=False,
        )

        session_controller.decide_outcome(
            observation=observation,
            worktree_path=session.worktree_path,
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_name=session.terminal_id,
            session_run_assets=session.run_assets,
        )

        events = mock_event_sink.events
        missing_events = [e for e in events if e.name == "session.no_completion_record"]
        assert len(missing_events) == 1
        assert missing_events[0].data["issue_number"] == session.issue.number
