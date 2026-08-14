"""Unit tests for the Action dataclasses and ActionApplier."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.control.actions import (
    Action,
    ActionType,
    ActionResult,
    ActionResultType,
    AddLabelAction,
    RemoveLabelAction,
    SyncLabelsAction,
    LaunchSessionAction,
    LaunchValidationRetryAction,
    StopSessionAction,
    EscalateToHumanAction,
    ReconcileHistoryEntryAction,
)
from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.review_exchange_lifecycle import (
    ReviewExchangeCancellation,
)
from issue_orchestrator.control.session_manager import SessionManager, SessionRef, SessionType
from issue_orchestrator.ports import NullEventSink, TraceEvent


class CollectingEventSink:
    """Event sink that collects events for test assertions."""

    def __init__(self):
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


def _no_review_exchange_work(
    issue_number: int, _reason: str
) -> ReviewExchangeCancellation:
    """Explicit null owner for action tests without review-exchange runtime."""
    return ReviewExchangeCancellation(issue_number, ())


class MockLabelSet:
    """Mock LabelSet for testing."""

    def __init__(self):
        self.labels: dict[int, set[str]] = {}
        self.fail_on: set[str] = set()

    def add_label(self, issue_number: int, label: str) -> None:
        if label in self.fail_on:
            raise Exception(f"Failed to add {label}")
        if issue_number not in self.labels:
            self.labels[issue_number] = set()
        self.labels[issue_number].add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        if label in self.fail_on:
            raise Exception(f"Failed to remove {label}")
        if issue_number in self.labels:
            self.labels[issue_number].discard(label)

    def has_label(self, issue_number: int, label: str) -> bool:
        return issue_number in self.labels and label in self.labels[issue_number]


class MockSessionRunner:
    """Mock SessionRunner for testing."""

    def __init__(self):
        self.sessions: dict[int, dict] = {}

    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None = None,
        session_name: str | None = None,
    ) -> bool:
        self.sessions[session_id] = {"command": command, "working_dir": working_dir}
        return True

    def session_exists(self, session_id: int, session_name: str | None = None) -> bool:
        return session_id in self.sessions

    def kill_session(self, session_id: int, session_name: str | None = None) -> None:
        self.sessions.pop(session_id, None)

    def discover_running_sessions(self) -> list[dict]:
        return []

    def cleanup_idle_sessions(self) -> int:
        return 0

    def get_session_output(self, session_id: int, lines: int = 50, session_name: str | None = None) -> str | None:
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


class TestActionDataclasses:
    """Test the Action dataclasses."""

    def test_add_label_action(self):
        """Test AddLabelAction creation."""
        action = AddLabelAction(
            issue_number=123,
            label="in-progress",
            reason="Issue claimed",
        )

        assert action.action_type == ActionType.ADD_LABEL
        assert action.issue_number == 123
        assert action.label == "in-progress"
        assert action.reason == "Issue claimed"

    def test_remove_label_action(self):
        """Test RemoveLabelAction creation."""
        action = RemoveLabelAction(
            issue_number=123,
            label="blocked",
            reason="Issue unblocked",
        )

        assert action.action_type == ActionType.REMOVE_LABEL
        assert action.issue_number == 123
        assert action.label == "blocked"

    def test_sync_labels_action(self):
        """Test SyncLabelsAction creation."""
        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress", "priority:high"),
            remove_labels=("blocked",),
        )

        assert action.action_type == ActionType.SYNC_LABELS
        assert "in-progress" in action.add_labels
        assert "blocked" in action.remove_labels

    def test_launch_session_action(self):
        """Test LaunchSessionAction creation."""
        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
            command="claude",
            working_dir="/path/to/worktree",
            title="Issue #123",
        )

        assert action.action_type == ActionType.LAUNCH_SESSION
        assert action.session_type == SessionType.ISSUE
        assert action.number == 123

    def test_launch_validation_retry_action(self):
        """Test LaunchValidationRetryAction creation."""
        action = LaunchValidationRetryAction(issue_number=123, retry_count=1)

        assert action.action_type == ActionType.LAUNCH_VALIDATION_RETRY
        assert action.issue_number == 123
        assert action.retry_count == 1

    def test_launch_validation_retry_action_rejects_invalid_issue(self):
        """Test LaunchValidationRetryAction requires a real issue number."""
        with pytest.raises(ValueError, match="positive issue_number"):
            LaunchValidationRetryAction(issue_number=0, retry_count=1)

    def test_reconcile_history_entry_action(self):
        """Test ReconcileHistoryEntryAction creation."""
        action = ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            source="pull_request",
            reason="PR merged; awaiting merge reconciled",
        )

        assert action.action_type == ActionType.RECONCILE_HISTORY_ENTRY
        assert action.issue_number == 228
        assert action.pr_number == 318
        assert action.status == "merged"
        assert action.reason == "PR merged; awaiting merge reconciled"

    def test_actions_are_frozen(self):
        """Test that actions are immutable."""
        action = AddLabelAction(issue_number=123, label="test")

        with pytest.raises(AttributeError):
            action.issue_number = 456


class TestActionResult:
    """Test the ActionResult dataclass."""

    def test_ok_creates_success_result(self):
        """Test ok factory creates success result."""
        action = AddLabelAction(issue_number=123, label="test")
        result = ActionResult.ok(action, extra="data")

        assert result.success
        assert result.result_type == ActionResultType.SUCCESS
        assert result.error is None
        assert result.details["extra"] == "data"

    def test_fail_creates_failure_result(self):
        """Test fail factory creates failure result."""
        action = AddLabelAction(issue_number=123, label="test")
        result = ActionResult.fail(action, "Something went wrong")

        assert not result.success
        assert result.result_type == ActionResultType.FAILURE
        assert result.error == "Something went wrong"

    def test_skip_creates_skipped_result(self):
        """Test skip factory creates skipped result."""
        action = AddLabelAction(issue_number=123, label="test")
        result = ActionResult.skip(action, "Already exists")

        assert not result.success
        assert result.result_type == ActionResultType.SKIPPED
        assert "Already exists" in result.details["skip_reason"]

    def test_issue_number_returns_typed_value(self):
        """#6873 N4: the typed accessor returns the stored int identity."""
        action = AddLabelAction(issue_number=123, label="test")
        assert ActionResult.ok(action, issue_number=42).issue_number == 42

    def test_issue_number_is_none_when_absent(self):
        """A result that carries no issue identity yields None, not a crash."""
        action = AddLabelAction(issue_number=123, label="test")
        assert ActionResult.ok(action, session_name="s").issue_number is None

    @pytest.mark.parametrize("bad", ["42", 42.0, True, [42]])
    def test_issue_number_fails_fast_on_non_int(self, bad):
        """A present-but-non-int identity is a producer/contract bug (bool too,
        since bool is an int subclass) — surfaced, not silently tolerated."""
        action = AddLabelAction(issue_number=123, label="test")
        with pytest.raises(TypeError, match="issue_number must be int"):
            _ = ActionResult.ok(action, issue_number=bad).issue_number


class TestActionApplier:
    """Test the ActionApplier class."""

    @pytest.fixture
    def mock_labels(self):
        return MockLabelSet()

    @pytest.fixture
    def mock_runner(self):
        return MockSessionRunner()

    @pytest.fixture
    def collecting_sink(self):
        return CollectingEventSink()

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.repo_root = Path("/path/to/repo")
        return config

    @pytest.fixture
    def session_manager(self, mock_runner, collecting_sink, mock_config):
        return SessionManager(
            runner=mock_runner,
            events=collecting_sink,
            config=mock_config,
        )

    @pytest.fixture
    def applier(self, mock_labels, session_manager, collecting_sink):
        return ActionApplier(
            labels=mock_labels,
            sessions=session_manager,
            events=collecting_sink,
            review_exchange_canceller=_no_review_exchange_work,
        )

    def test_apply_add_label(self, applier, mock_labels):
        """Test applying AddLabelAction."""
        action = AddLabelAction(issue_number=123, label="in-progress")

        result = applier.apply(action)

        assert result.success
        assert mock_labels.has_label(123, "in-progress")

    def test_apply_remove_label(self, applier, mock_labels):
        """Test applying RemoveLabelAction."""
        mock_labels.labels[123] = {"blocked", "in-progress"}
        action = RemoveLabelAction(issue_number=123, label="blocked")

        result = applier.apply(action)

        assert result.success
        assert not mock_labels.has_label(123, "blocked")
        assert mock_labels.has_label(123, "in-progress")

    def test_apply_sync_labels(self, applier, mock_labels):
        """Test applying SyncLabelsAction."""
        mock_labels.labels[123] = {"blocked"}
        action = SyncLabelsAction(
            issue_number=123,
            add_labels=("in-progress",),
            remove_labels=("blocked",),
        )

        result = applier.apply(action)

        assert result.success
        assert mock_labels.has_label(123, "in-progress")
        assert not mock_labels.has_label(123, "blocked")

    def test_apply_launch_session(self, applier, mock_runner):
        """Test applying LaunchSessionAction."""
        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
            command="claude",
            working_dir="/path/to/worktree",
        )

        result = applier.apply(action)

        assert result.success
        assert 123 in mock_runner.sessions

    def test_apply_launch_session_skips_if_exists(self, applier, mock_runner):
        """Test launch session skips if already running."""
        mock_runner.sessions[123] = {}  # Pre-create
        action = LaunchSessionAction(
            session_type=SessionType.ISSUE,
            number=123,
            command="claude",
            working_dir="/path",
        )

        result = applier.apply(action)

        assert result.result_type == ActionResultType.SKIPPED

    def test_apply_stop_session(self, applier, mock_runner):
        """Test applying StopSessionAction."""
        mock_runner.sessions[123] = {}  # Pre-create
        action = StopSessionAction(session_type=SessionType.ISSUE, number=123)

        result = applier.apply(action)

        assert result.success
        assert 123 not in mock_runner.sessions

    def test_apply_stop_session_skips_if_not_running(self, applier, mock_runner):
        """Test stop session skips if not running."""
        action = StopSessionAction(session_type=SessionType.ISSUE, number=123)

        result = applier.apply(action)

        assert result.result_type == ActionResultType.SKIPPED

    def test_apply_escalate_to_human(self, applier, mock_labels, collecting_sink):
        """Test applying EscalateToHumanAction."""
        action = EscalateToHumanAction(
            issue_number=123,
            pr_number=456,
            escalation_reason="Max rework cycles exceeded",
            rework_cycles=3,
            needs_human_label="blocked-needs-human",
            needs_rework_label="needs-rework",
            max_rework_cycles=2,
        )

        result = applier.apply(action)

        assert result.success
        # Label is added to pr_number (456), not issue_number
        assert mock_labels.has_label(456, "blocked-needs-human")
        # Check event was emitted
        escalation_events = [e for e in collecting_sink.events if e.name == "review.escalated"]
        assert len(escalation_events) == 1

    def test_apply_handles_errors(self, applier, mock_labels):
        """Test that apply handles errors gracefully."""
        mock_labels.fail_on.add("will-fail")
        action = AddLabelAction(issue_number=123, label="will-fail")

        result = applier.apply(action)

        assert not result.success
        assert result.error is not None

    def test_apply_all(self, applier, mock_labels):
        """Test applying multiple actions."""
        mock_labels.labels[123] = {"blocked"}
        actions = [
            AddLabelAction(issue_number=123, label="in-progress"),
            AddLabelAction(issue_number=123, label="priority:high"),
            RemoveLabelAction(issue_number=123, label="blocked"),
        ]

        results = applier.apply_all(actions)

        assert len(results) == 3
        assert all(r.success for r in results)
        assert mock_labels.has_label(123, "in-progress")
        assert mock_labels.has_label(123, "priority:high")

    def test_apply_emits_trace_events(self, applier, collecting_sink):
        """Test that apply emits start/end trace events."""
        action = AddLabelAction(issue_number=123, label="test")

        applier.apply(action)

        event_names = [e.name for e in collecting_sink.events]
        assert "action.start" in event_names
        assert "action.end" in event_names
