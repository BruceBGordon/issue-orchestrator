"""Unit tests for state machines."""

import pytest
from datetime import datetime, timedelta

from issue_orchestrator.domain.state_machines import (
    IssueStateMachine,
    IssueState,
    SessionStateMachine,
    SessionState,
    ReviewStateMachine,
    ReviewState,
    InvalidStateTransition,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.session_watchdog import SessionWatchdogClock


def _watchdog_clock(
    wall_now: list[datetime],
    monotonic_now: list[float],
) -> SessionWatchdogClock:
    return SessionWatchdogClock(
        wall_now=lambda: wall_now[0],
        monotonic_now=lambda: monotonic_now[0],
    )


@pytest.fixture
def sample_issue():
    """Create a sample issue for state machine tests."""
    return Issue(number=123, title="Test issue", labels=[])


class TestIssueStateMachine:
    """Test the IssueStateMachine."""

    def test_initialization_default_state(self):
        """Test state machine initializes with default AVAILABLE state."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        assert machine.issue_number == 123
        assert machine.get_state() == IssueState.AVAILABLE
        assert machine.last_transition is None

    def test_initialization_custom_state(self):
        """Test state machine initializes with custom initial state."""
        machine = IssueStateMachine(
            issue=Issue(number=123, title="Test", labels=[]),
            initial_state=IssueState.IN_PROGRESS
        )

        assert machine.get_state() == IssueState.IN_PROGRESS

    def test_happy_path_complete_flow(self):
        """Test the complete happy path: available -> claimed -> in_progress -> pr_pending -> completed."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        # Available -> Claimed
        machine.claim()
        assert machine.get_state() == IssueState.CLAIMED

        # Claimed -> In Progress
        machine.start()
        assert machine.get_state() == IssueState.IN_PROGRESS

        # In Progress -> PR Pending
        machine.pr_created()
        assert machine.get_state() == IssueState.PR_PENDING

        # PR Pending -> Completed
        machine.pr_merged()
        assert machine.get_state() == IssueState.COMPLETED

    def test_blocking_flow(self):
        """Test blocking and unblocking an issue."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        assert machine.get_state() == IssueState.IN_PROGRESS

        # Block the issue
        machine.block()
        assert machine.get_state() == IssueState.BLOCKED

        # Unblock back to in_progress
        machine.unblock()
        assert machine.get_state() == IssueState.IN_PROGRESS

    def test_needs_human_flow(self):
        """Test needs_human flow."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        assert machine.get_state() == IssueState.IN_PROGRESS

        # Mark as needs human
        machine.needs_human()
        assert machine.get_state() == IssueState.NEEDS_HUMAN

        # Unblock returns to in_progress
        machine.unblock()
        assert machine.get_state() == IssueState.IN_PROGRESS

    def test_release_from_claimed(self):
        """Test releasing an issue from claimed state."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        assert machine.get_state() == IssueState.CLAIMED

        machine.release()
        assert machine.get_state() == IssueState.AVAILABLE

    def test_release_from_in_progress(self):
        """Test releasing an issue from in_progress state."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        assert machine.get_state() == IssueState.IN_PROGRESS

        machine.release()
        assert machine.get_state() == IssueState.AVAILABLE

    def test_release_from_blocked(self):
        """Test releasing an issue from blocked state."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.block()
        assert machine.get_state() == IssueState.BLOCKED

        machine.release()
        assert machine.get_state() == IssueState.AVAILABLE

    def test_release_from_needs_human(self):
        """Test releasing an issue from needs_human state."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.needs_human()
        assert machine.get_state() == IssueState.NEEDS_HUMAN

        machine.release()
        assert machine.get_state() == IssueState.AVAILABLE

    def test_pr_closed_returns_to_in_progress(self):
        """Test that closing a PR returns to in_progress state."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.pr_created()
        assert machine.get_state() == IssueState.PR_PENDING

        machine.pr_closed()
        assert machine.get_state() == IssueState.IN_PROGRESS

    def test_invalid_transition_raises_error(self):
        """Test that invalid transitions raise InvalidStateTransition."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        # Can't start from available state
        with pytest.raises(InvalidStateTransition):
            machine.start()

    def test_invalid_claim_from_in_progress(self):
        """Test that claiming from in_progress state is invalid."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()

        # Can't claim from in_progress
        with pytest.raises(InvalidStateTransition):
            machine.claim()

    def test_cannot_release_from_completed(self):
        """Test that releasing from completed state is invalid."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.pr_created()
        machine.pr_merged()
        assert machine.get_state() == IssueState.COMPLETED

        # Can't release from completed
        with pytest.raises(InvalidStateTransition):
            machine.release()

    def test_cannot_release_from_pr_pending(self):
        """Test that releasing from pr_pending state is invalid."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.pr_created()
        assert machine.get_state() == IssueState.PR_PENDING

        # Can't release from pr_pending
        with pytest.raises(InvalidStateTransition):
            machine.release()

    def test_transition_result_on_claim(self):
        """Test that TransitionResult is stored when claiming."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim(data={"agent": "web"})

        assert machine.last_transition is not None
        assert machine.last_transition.success is True
        assert machine.last_transition.from_state == "available"
        assert machine.last_transition.to_state == "claimed"
        assert machine.last_transition.event_name == "issue.claimed"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data == {"agent": "web"}

    def test_transition_result_on_start(self):
        """Test that TransitionResult is stored when starting."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start(data={"session_id": "session-123"})

        assert machine.last_transition.event_name == "issue.started"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data == {"session_id": "session-123"}

    def test_transition_result_on_block(self):
        """Test that TransitionResult is stored when blocking."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.block(data={"reason": "waiting for dependency"})

        assert machine.last_transition.event_name == "issue.blocked"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data == {"reason": "waiting for dependency"}

    def test_transition_result_on_needs_human(self):
        """Test that TransitionResult is stored when needs_human."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.needs_human(data={"reason": "complex decision required"})

        assert machine.last_transition.event_name == "issue.needs_human"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data == {"reason": "complex decision required"}

    def test_transition_result_on_unblock(self):
        """Test that TransitionResult is stored when unblocking."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.block()
        machine.unblock(data={"resolved_by": "human"})

        assert machine.last_transition.event_name == "issue.unblocked"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data == {"resolved_by": "human"}

    def test_transition_result_on_pr_created(self):
        """Test that TransitionResult is stored when PR is created."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.pr_created(data={"pr_number": 456})

        assert machine.last_transition.event_name == "issue.pr_created"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data == {"pr_number": 456}

    def test_transition_result_on_pr_rejected(self):
        """Test that TransitionResult is stored when PR is closed."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.pr_created()
        machine.pr_closed(data={"reason": "changes requested"})

        assert machine.last_transition.event_name == "issue.pr_rejected"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data == {"reason": "changes requested"}

    def test_transition_result_on_completed(self):
        """Test that TransitionResult is stored when PR is merged."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.pr_created()
        machine.pr_merged(data={"merged_by": "bot"})

        assert machine.last_transition.event_name == "issue.completed"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data == {"merged_by": "bot"}

    def test_transition_result_on_release(self):
        """Test that TransitionResult is stored when releasing."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.release(data={"reason": "agent failed"})

        assert machine.last_transition.event_name == "issue.released"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data == {"reason": "agent failed"}

    def test_can_transition_returns_true_for_valid(self):
        """Test that can_transition returns True for valid transitions."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        assert machine.can_transition('claim') is True
        assert machine.can_transition('start') is False  # Not valid from AVAILABLE

        machine.claim()
        assert machine.can_transition('claim') is False
        assert machine.can_transition('start') is True

    def test_can_transition_returns_false_for_invalid(self):
        """Test that can_transition returns False for invalid transitions."""
        machine = IssueStateMachine(issue=Issue(number=123, title="Test", labels=[]))

        machine.claim()
        machine.start()
        machine.pr_created()

        # From PR_PENDING state, can't claim or start
        assert machine.can_transition('claim') is False
        assert machine.can_transition('start') is False
        # But can merge or close
        assert machine.can_transition('pr_merged') is True
        assert machine.can_transition('pr_closed') is True


class TestSessionStateMachine:
    """Test the SessionStateMachine."""

    def test_initialization_default_state(self):
        """Test state machine initializes with default PENDING state."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        assert machine.session_id == "session-123"
        assert machine.issue_number == 456
        assert machine.get_state() == SessionState.PENDING
        assert machine.last_transition is None

    def test_launch_to_started_to_running_to_completed(self):
        """Test happy path: pending -> starting -> running -> completed."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        # Launch
        machine.launch()
        assert machine.get_state() == SessionState.STARTING

        # Started
        machine.started()
        assert machine.get_state() == SessionState.RUNNING
        assert machine.started_at is not None

        # Complete
        machine.complete()
        assert machine.get_state() == SessionState.COMPLETED

    def test_mark_slow(self):
        """Test marking a running session as slow."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()
        assert machine.get_state() == SessionState.RUNNING

        machine.mark_slow()
        assert machine.get_state() == SessionState.SLOW

    def test_complete_from_slow(self):
        """Test completing a session from slow state."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()
        machine.mark_slow()
        assert machine.get_state() == SessionState.SLOW

        machine.complete()
        assert machine.get_state() == SessionState.COMPLETED

    def test_fail_from_starting(self):
        """Test session failing during startup."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        assert machine.get_state() == SessionState.STARTING

        machine.fail(data={"error": "failed to initialize"})
        assert machine.get_state() == SessionState.FAILED

    def test_fail_from_running(self):
        """Test session failing while running."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()
        assert machine.get_state() == SessionState.RUNNING

        machine.fail()
        assert machine.get_state() == SessionState.FAILED

    def test_fail_from_slow(self):
        """Test session failing while slow."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()
        machine.mark_slow()
        assert machine.get_state() == SessionState.SLOW

        machine.fail()
        assert machine.get_state() == SessionState.FAILED

    def test_timeout_from_running(self):
        """Test session timing out while running."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()

        machine.timeout()
        assert machine.get_state() == SessionState.TIMED_OUT

    def test_timeout_from_slow(self):
        """Test session timing out while slow."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()
        machine.mark_slow()

        machine.timeout()
        assert machine.get_state() == SessionState.TIMED_OUT

    def test_block_from_running(self):
        """Test blocking a running session."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()

        machine.block()
        assert machine.get_state() == SessionState.BLOCKED

    def test_needs_human_from_running(self):
        """Test session needs human from running."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()

        machine.needs_human()
        assert machine.get_state() == SessionState.NEEDS_HUMAN

    def test_resume_from_blocked(self):
        """Test resuming from blocked state."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()
        machine.block()
        assert machine.get_state() == SessionState.BLOCKED

        machine.resume()
        assert machine.get_state() == SessionState.RUNNING

    def test_resume_from_needs_human(self):
        """Test resuming from needs_human state."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()
        machine.needs_human()
        assert machine.get_state() == SessionState.NEEDS_HUMAN

        machine.resume()
        assert machine.get_state() == SessionState.RUNNING

    def test_check_timeout_no_timeout_configured(self):
        """Test check_timeout when no timeout is configured."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            timeout_minutes=None
        )

        machine.launch()
        machine.started()

        result = machine.check_timeout()
        assert result is False
        assert machine.get_state() == SessionState.RUNNING

    def test_check_timeout_not_exceeded(self):
        """Test check_timeout when timeout hasn't been exceeded."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            timeout_minutes=60
        )

        machine.launch()
        machine.started()
        # Just started, so shouldn't be timed out
        result = machine.check_timeout()
        assert result is False
        assert machine.get_state() == SessionState.RUNNING

    def test_check_timeout_exceeded(self):
        """Test check_timeout when timeout has been exceeded."""
        wall_now = [datetime(2026, 8, 25, 1, 0, 0)]
        monotonic_now = [100.0]
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            timeout_minutes=60,
            watchdog_clock=_watchdog_clock(wall_now, monotonic_now),
        )

        machine.launch()
        machine.started()

        # Wall time can jump in either direction without changing authority.
        wall_now[0] += timedelta(days=30)
        monotonic_now[0] += 59 * 60
        assert machine.check_timeout() is False
        wall_now[0] -= timedelta(days=60)
        monotonic_now[0] += 2 * 60

        result = machine.check_timeout()
        assert result is True
        assert machine.get_state() == SessionState.TIMED_OUT

    def test_check_timeout_only_affects_running_or_slow(self):
        """Test that check_timeout only triggers for RUNNING or SLOW states."""
        wall_now = [datetime(2026, 8, 25, 1, 0, 0)]
        monotonic_now = [100.0]
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            timeout_minutes=60,
            watchdog_clock=_watchdog_clock(wall_now, monotonic_now),
        )

        machine.launch()
        machine.started()
        machine.block()
        assert machine.get_state() == SessionState.BLOCKED

        monotonic_now[0] += 61 * 60

        # Should not timeout from BLOCKED state
        result = machine.check_timeout()
        assert result is False
        assert machine.get_state() == SessionState.BLOCKED

    def test_transition_result_on_launched(self):
        """Test that TransitionResult is stored on launch."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch(data={"worktree": "/path/to/worktree"})

        assert machine.last_transition is not None
        assert machine.last_transition.event_name == "session.launched"
        assert machine.last_transition.entity_id == 456
        assert machine.last_transition.data["session_id"] == "session-123"
        assert machine.last_transition.data["worktree"] == "/path/to/worktree"

    def test_transition_result_on_started(self):
        """Test that TransitionResult is stored on started with timestamp."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()

        assert machine.last_transition.event_name == "session.started"
        assert machine.last_transition.entity_id == 456
        assert machine.last_transition.data["session_id"] == "session-123"
        assert "started_at" in machine.last_transition.data

    def test_transition_result_on_slow(self):
        """Test that TransitionResult is stored on slow with runtime."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()
        machine.mark_slow()

        assert machine.last_transition.event_name == "session.slow"
        assert machine.last_transition.entity_id == 456
        assert machine.last_transition.data["session_id"] == "session-123"
        assert "runtime_minutes" in machine.last_transition.data

    def test_transition_result_on_completed(self):
        """Test that TransitionResult is stored on completed with runtime."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()
        machine.complete()

        assert machine.last_transition.event_name == "session.completed"
        assert machine.last_transition.entity_id == 456
        assert "runtime_minutes" in machine.last_transition.data

    def test_transition_result_on_failed(self):
        """Test that TransitionResult is stored on failed."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.fail(data={"error": "test error"})

        assert machine.last_transition.event_name == "session.failed"
        assert machine.last_transition.data["error"] == "test error"

    def test_transition_result_on_timed_out(self):
        """Test that TransitionResult is stored on timed_out."""
        wall_now = [datetime(2026, 8, 25, 1, 0, 0)]
        monotonic_now = [100.0]
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            timeout_minutes=60,
            watchdog_clock=_watchdog_clock(wall_now, monotonic_now),
        )

        machine.launch()
        machine.started()
        monotonic_now[0] += 61 * 60
        machine.check_timeout()

        assert machine.last_transition.event_name == "session.timeout"
        assert "runtime_minutes" in machine.last_transition.data
        assert "timeout_minutes" in machine.last_transition.data

    def test_transition_result_on_blocked(self):
        """Test that TransitionResult is stored on blocked."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()
        machine.block()

        assert machine.last_transition.event_name == "session.blocked"

    def test_transition_result_on_needs_human(self):
        """Test that TransitionResult is stored on needs_human."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        machine.launch()
        machine.started()
        machine.needs_human()

        assert machine.last_transition.event_name == "session.needs_human"

    def test_get_runtime_info(self):
        """Test getting runtime information."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456,
            timeout_minutes=60
        )

        # Before starting
        info = machine.get_runtime_info()
        assert info["started_at"] is None
        assert info["runtime_minutes"] is None
        assert info["timeout_minutes"] == 60
        assert info["is_timed_out"] is False

        # After starting
        machine.launch()
        machine.started()

        info = machine.get_runtime_info()
        assert info["started_at"] is not None
        assert info["runtime_minutes"] is not None
        assert info["runtime_minutes"] >= 0
        assert info["timeout_minutes"] == 60
        assert info["is_timed_out"] is False

    def test_invalid_transitions_raise_error(self):
        """Test that invalid transitions raise InvalidStateTransition."""
        machine = SessionStateMachine(
            session_id="session-123",
            issue_number=456
        )

        # Can't start from PENDING
        with pytest.raises(InvalidStateTransition):
            machine.started()

        # Can't complete from PENDING
        with pytest.raises(InvalidStateTransition):
            machine.complete()


class TestReviewStateMachine:
    """Test the ReviewStateMachine."""

    def test_initialization_default_state(self):
        """Test state machine initializes with default PENDING state."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        assert machine.pr_number == 123
        assert machine.issue_number == 456
        assert machine.get_state() == ReviewState.PENDING
        assert machine.rework_count == 0
        assert machine.last_transition is None

    def test_approve_to_merge_flow(self):
        """Test happy path: pending -> in_review -> approved -> merged."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        assert machine.get_state() == ReviewState.IN_REVIEW

        machine.approve()
        assert machine.get_state() == ReviewState.APPROVED

        machine.merge()
        assert machine.get_state() == ReviewState.MERGED

    def test_changes_requested_rework_cycle(self):
        """Test changes requested and rework cycle."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.request_changes()
        assert machine.get_state() == ReviewState.CHANGES_REQUESTED
        assert machine.rework_count == 1

        machine.queue_rework()
        assert machine.get_state() == ReviewState.REWORK_PENDING

        machine.start_rework()
        assert machine.get_state() == ReviewState.REWORK_IN_PROGRESS

        machine.complete_rework()
        assert machine.get_state() == ReviewState.IN_REVIEW

    def test_multiple_rework_cycles(self):
        """Test multiple rework cycles increment count correctly."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()

        # First rework cycle
        machine.request_changes()
        assert machine.rework_count == 1
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Second rework cycle
        machine.request_changes()
        assert machine.rework_count == 2
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Third rework cycle
        machine.request_changes()
        assert machine.rework_count == 3

    def test_cto_review_flow(self):
        """Test tech_lead review flow."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.approve()
        assert machine.get_state() == ReviewState.APPROVED

        machine.request_tech_lead_review()
        assert machine.get_state() == ReviewState.TECH_LEAD_PENDING

        machine.tech_lead_reviewed()
        assert machine.get_state() == ReviewState.TECH_LEAD_REVIEWED

        machine.merge()
        assert machine.get_state() == ReviewState.MERGED

    def test_cto_review_followed_by_changes_requested(self):
        """Test changes requested after tech_lead review."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.approve()
        machine.request_tech_lead_review()
        machine.tech_lead_reviewed()
        assert machine.get_state() == ReviewState.TECH_LEAD_REVIEWED

        # CTO can request changes
        machine.request_changes_after_tech_lead()
        assert machine.get_state() == ReviewState.CHANGES_REQUESTED
        assert machine.rework_count == 1

    def test_max_rework_cycles_unlimited(self):
        """Test that with no max_rework_cycles, rework is always allowed."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            max_rework_cycles=None
        )

        machine.start_review()

        # Do 10 rework cycles
        for i in range(10):
            machine.request_changes()
            assert machine.can_transition('queue_rework') is True
            machine.queue_rework()
            machine.start_rework()
            machine.complete_rework()

        assert machine.rework_count == 10
        assert machine.has_exceeded_rework_limit() is False

    def test_max_rework_cycles_enforced(self):
        """Test that max_rework_cycles is enforced."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            max_rework_cycles=2
        )

        machine.start_review()

        # First rework cycle
        machine.request_changes()
        assert machine.can_transition('queue_rework') is True
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Second rework cycle
        machine.request_changes()
        assert machine.can_transition('queue_rework') is True
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Third rework cycle - should be blocked
        machine.request_changes()
        assert machine.rework_count == 3
        assert machine.can_transition('queue_rework') is False
        assert machine.has_exceeded_rework_limit() is True

        # Attempting queue_rework when conditions aren't met raises InvalidStateTransition
        # The transition won't execute because _can_rework() condition returns False
        try:
            machine.queue_rework()
            # If it didn't raise, verify state didn't change
            assert machine.get_state() == ReviewState.CHANGES_REQUESTED
        except InvalidStateTransition:
            # This is also acceptable - some versions of transitions raise on failed conditions
            pass

    def test_close_from_pending(self):
        """Test closing from pending state."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.close()
        assert machine.get_state() == ReviewState.CLOSED

    def test_close_from_in_review(self):
        """Test closing from in_review state."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.close()
        assert machine.get_state() == ReviewState.CLOSED

    def test_close_from_approved(self):
        """Test closing from approved state."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.approve()
        machine.close()
        assert machine.get_state() == ReviewState.CLOSED

    def test_close_from_rework_in_progress(self):
        """Test closing from rework_in_progress state."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.close()
        assert machine.get_state() == ReviewState.CLOSED

    def test_cannot_close_from_merged(self):
        """Test that closing from merged state is invalid."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.approve()
        machine.merge()
        assert machine.get_state() == ReviewState.MERGED

        # Can't close from merged
        with pytest.raises(InvalidStateTransition):
            machine.close()

    def test_transition_result_on_review_started(self):
        """Test that TransitionResult is stored on review started."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()

        assert machine.last_transition is not None
        assert machine.last_transition.event_name == "review.started"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data["issue_number"] == 456

    def test_transition_result_on_approved(self):
        """Test that TransitionResult is stored on approved."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.approve()

        assert machine.last_transition.event_name == "review.approved"
        assert machine.last_transition.entity_id == 123

    def test_transition_result_on_changes_requested(self):
        """Test that TransitionResult is stored on changes_requested with rework count."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.request_changes()

        assert machine.last_transition.event_name == "review.changes_requested"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data["rework_count"] == 1

    def test_transition_result_on_rework_started(self):
        """Test that TransitionResult is stored on rework started."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()

        assert machine.last_transition.event_name == "review.rework_started"
        assert machine.last_transition.data["rework_count"] == 1

    def test_transition_result_on_rework_completed(self):
        """Test that TransitionResult is stored on rework completed."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        assert machine.last_transition.event_name == "review.rework_completed"

    def test_transition_result_on_tech_lead_review_started(self):
        """Test that TransitionResult is stored on tech_lead review started."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.approve()
        machine.request_tech_lead_review()

        assert machine.last_transition.event_name == "review.tech_lead_started"

    def test_transition_result_on_tech_lead_approved(self):
        """Test that TransitionResult is stored on tech_lead approved."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.approve()
        machine.request_tech_lead_review()
        machine.tech_lead_reviewed()

        assert machine.last_transition.event_name == "review.tech_lead_approved"

    def test_transition_result_on_merged(self):
        """Test that TransitionResult is stored on merged with rework count."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()
        machine.approve()
        machine.merge()

        assert machine.last_transition.event_name == "review.merged"
        assert machine.last_transition.data["rework_count"] == 1

    def test_transition_result_on_closed(self):
        """Test that TransitionResult is stored on closed."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        machine.start_review()
        machine.close()

        assert machine.last_transition.event_name == "review.closed"

    def test_get_rework_info(self):
        """Test getting rework information."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            max_rework_cycles=2
        )

        # Initially
        info = machine.get_rework_info()
        assert info["rework_count"] == 0
        assert info["max_rework_cycles"] == 2
        assert info["can_rework"] is True

        # After first rework
        machine.start_review()
        machine.request_changes()

        info = machine.get_rework_info()
        assert info["rework_count"] == 1
        assert info["can_rework"] is True

        # After second rework
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()
        machine.request_changes()

        info = machine.get_rework_info()
        assert info["rework_count"] == 2
        assert info["can_rework"] is False

    def test_can_transition_validates_rework_limit(self):
        """Test that can_transition respects rework limit."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            max_rework_cycles=1
        )

        machine.start_review()
        machine.request_changes()
        assert machine.can_transition('queue_rework') is True

        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Second rework cycle - should fail limit check
        machine.request_changes()
        assert machine.rework_count == 2
        assert machine.can_transition('queue_rework') is False

    def test_escalate_when_rework_limit_exceeded(self):
        """Test explicit escalation when rework limit is exceeded."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            max_rework_cycles=2
        )

        machine.start_review()

        # Do 2 rework cycles (max allowed)
        for _ in range(2):
            machine.request_changes()
            machine.queue_rework()
            machine.start_rework()
            machine.complete_rework()

        # Third changes_requested exceeds limit
        machine.request_changes()
        assert machine.rework_count == 3
        assert machine.has_exceeded_rework_limit() is True
        assert machine.get_state() == ReviewState.CHANGES_REQUESTED

        # Cannot queue_rework anymore
        assert machine.can_transition('queue_rework') is False

        # But CAN escalate
        assert machine.can_transition('escalate') is True
        machine.escalate()
        assert machine.get_state() == ReviewState.ESCALATED

    def test_transition_result_on_escalate(self):
        """Test that TransitionResult is stored on escalate with full context."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            max_rework_cycles=1
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()

        # Second changes_requested exceeds limit
        machine.request_changes()
        machine.escalate()

        assert machine.last_transition.event_name == "review.escalated"
        assert machine.last_transition.entity_id == 123
        assert machine.last_transition.data["issue_number"] == 456
        assert machine.last_transition.data["rework_count"] == 2
        assert machine.last_transition.data["max_rework_cycles"] == 1

    def test_cannot_close_from_escalated(self):
        """Test that escalated state cannot be closed (needs human resolution)."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            max_rework_cycles=1
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()
        machine.request_changes()
        machine.escalate()

        assert machine.get_state() == ReviewState.ESCALATED

        # Cannot close from escalated - needs human intervention
        with pytest.raises(InvalidStateTransition):
            machine.close()

    def test_escalated_state_is_terminal(self):
        """Test that ESCALATED is a terminal state requiring human intervention."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456,
            max_rework_cycles=1
        )

        machine.start_review()
        machine.request_changes()
        machine.queue_rework()
        machine.start_rework()
        machine.complete_rework()
        machine.request_changes()
        machine.escalate()

        # Cannot transition out of escalated state automatically
        assert machine.can_transition('queue_rework') is False
        assert machine.can_transition('start_review') is False
        assert machine.can_transition('approve') is False
        assert machine.can_transition('merge') is False

    def test_invalid_transitions_raise_error(self):
        """Test that invalid transitions raise InvalidStateTransition."""
        machine = ReviewStateMachine(
            pr_number=123,
            issue_number=456
        )

        # Can't approve from PENDING
        with pytest.raises(InvalidStateTransition):
            machine.approve()

        # Can't merge from PENDING
        with pytest.raises(InvalidStateTransition):
            machine.merge()
