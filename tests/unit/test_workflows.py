"""Unit tests for the workflow modules."""

import pytest
from unittest.mock import MagicMock

from issue_orchestrator.control.workflows.review_workflow import (
    ReviewWorkflow,
    ReviewDecision,
)
from issue_orchestrator.control.workflows.rework_workflow import (
    ReworkWorkflow,
    ReworkDecision,
    EscalationDecision,
)
from issue_orchestrator.control.workflows.tech_lead_workflow import (
    TechLeadWorkflow,
    TechLeadDecision,
)
from issue_orchestrator.domain.models import PendingReview, PendingRework, PendingTechLeadReview
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.ports import NullEventSink, TraceEvent


class CollectingEventSink:
    """Event sink that collects events for test assertions."""

    def __init__(self):
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


def make_pending_review(pr_number: int, issue_number: int) -> PendingReview:
    """Create a PendingReview for testing."""
    return PendingReview(
        pr_number=pr_number,
        issue_key=FakeIssueKey(name=str(issue_number)),
        pr_url=f"https://github.com/test/repo/pull/{pr_number}",
        branch_name=f"issue-{issue_number}",
        _issue_number=issue_number,
    )


def make_pending_rework(issue_number: int, pr_number: int = None, rework_cycle: int = 1) -> PendingRework:
    """Create a PendingRework for testing.

    Args:
        issue_number: The issue number (used as stable_id)
        pr_number: Deprecated, ignored (kept for API compat)
        rework_cycle: Which rework iteration this is
    """
    return PendingRework(
        issue_key=FakeIssueKey(name=str(issue_number)),
        agent_type="agent:test",
        rework_cycle=rework_cycle,
    )


def make_pending_tech_lead(
    issue_number: int,
    title: str = "Test",
    flavor: TechLeadSessionFlavor = TechLeadSessionFlavor.BATCH_REVIEW,
) -> PendingTechLeadReview:
    """Create a PendingTechLeadReview for testing (workflow logic is flavor-agnostic)."""
    return PendingTechLeadReview(
        issue_number=issue_number,
        title=title,
        flavor=flavor,
    )


class TestReviewWorkflow:
    """Test the ReviewWorkflow class."""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.code_review_agent = "agent:reviewer"
        config.max_concurrent_sessions = 3
        config.max_rework_cycles = 3
        return config

    @pytest.fixture
    def collecting_sink(self):
        return CollectingEventSink()

    @pytest.fixture
    def workflow(self, mock_config, collecting_sink):
        return ReviewWorkflow(config=mock_config, events=collecting_sink)

    def test_is_configured_returns_true_when_configured(self, workflow):
        """Test is_configured returns True when code_review_agent is set."""
        assert workflow.is_configured() is True

    def test_is_configured_returns_false_when_not_configured(self, collecting_sink):
        """Test is_configured returns False when not configured."""
        config = MagicMock()
        config.code_review_agent = None
        workflow = ReviewWorkflow(config=config, events=collecting_sink)
        assert workflow.is_configured() is False

    def test_should_launch_skips_when_not_configured(self, collecting_sink):
        """Test skips when not configured."""
        config = MagicMock()
        config.code_review_agent = None
        workflow = ReviewWorkflow(config=config, events=collecting_sink)

        decision = workflow.should_launch_reviews(
            pending_reviews=[make_pending_review(1, 100)],
            active_session_count=0,
            paused=False,
        )

        assert not decision.should_launch
        assert "configured" in decision.skip_reason

    def test_should_launch_skips_when_queue_empty(self, workflow):
        """Test skips when queue is empty."""
        decision = workflow.should_launch_reviews(
            pending_reviews=[],
            active_session_count=0,
            paused=False,
        )

        assert not decision.should_launch
        assert "No pending" in decision.skip_reason

    def test_should_launch_skips_when_paused(self, workflow, collecting_sink):
        """Test skips when paused."""
        decision = workflow.should_launch_reviews(
            pending_reviews=[make_pending_review(1, 100)],
            active_session_count=0,
            paused=True,
        )

        assert not decision.should_launch
        assert "paused" in decision.skip_reason
        # Event emitted
        assert any(e.name == "review.skipped" for e in collecting_sink.events)

    def test_should_launch_skips_when_no_capacity(self, workflow, mock_config):
        """Test skips when no capacity."""
        decision = workflow.should_launch_reviews(
            pending_reviews=[make_pending_review(1, 100)],
            active_session_count=3,  # Full capacity
            paused=False,
        )

        assert not decision.should_launch
        assert "capacity" in decision.skip_reason

    def test_should_launch_returns_reviews_up_to_capacity(self, workflow):
        """Test returns reviews up to capacity."""
        pending = [
            make_pending_review(1, 100),
            make_pending_review(2, 101),
            make_pending_review(3, 102),
            make_pending_review(4, 103),
        ]

        decision = workflow.should_launch_reviews(
            pending_reviews=pending,
            active_session_count=1,  # 2 slots available
            paused=False,
        )

        assert decision.should_launch
        assert len(decision.reviews_to_launch) == 2
        assert decision.available_capacity == 2

    def test_should_escalate(self, workflow):
        """Test escalation decision."""
        assert workflow.should_escalate(pr_number=1, rework_cycles=3) is True
        assert workflow.should_escalate(pr_number=1, rework_cycles=2) is False


class TestReworkWorkflow:
    """Test the ReworkWorkflow class."""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.max_concurrent_sessions = 3
        config.max_rework_cycles = 3
        config.label_prefix = None
        return config

    @pytest.fixture
    def collecting_sink(self):
        return CollectingEventSink()

    @pytest.fixture
    def workflow(self, mock_config, collecting_sink):
        return ReworkWorkflow(config=mock_config, events=collecting_sink)

    def test_should_launch_skips_when_queue_empty(self, workflow):
        """Test skips when queue is empty."""
        decision = workflow.should_launch_reworks(
            pending_reworks=[],
            active_session_count=0,
            paused=False,
        )

        assert not decision.should_launch
        assert "No pending" in decision.skip_reason

    def test_should_launch_skips_when_paused(self, workflow):
        """Test skips when paused."""
        decision = workflow.should_launch_reworks(
            pending_reworks=[make_pending_rework(100, 1)],
            active_session_count=0,
            paused=True,
        )

        assert not decision.should_launch
        assert "paused" in decision.skip_reason

    def test_should_launch_skips_when_no_capacity(self, workflow):
        """Test skips when no capacity."""
        decision = workflow.should_launch_reworks(
            pending_reworks=[make_pending_rework(100, 1)],
            active_session_count=3,
            paused=False,
        )

        assert not decision.should_launch
        assert "capacity" in decision.skip_reason

    def test_should_launch_returns_reworks_up_to_capacity(self, workflow):
        """Test returns reworks up to capacity."""
        pending = [
            make_pending_rework(100, 1),
            make_pending_rework(101, 2),
            make_pending_rework(102, 3),
        ]

        decision = workflow.should_launch_reworks(
            pending_reworks=pending,
            active_session_count=1,
            paused=False,
        )

        assert decision.should_launch
        assert len(decision.reworks_to_launch) == 2

    def test_should_escalate_above_max_cycles(self, workflow):
        """Test escalation when cycle exceeds max."""
        decision = workflow.should_escalate(rework_cycle=4)

        assert decision.should_escalate
        assert decision.rework_cycle == 4
        assert decision.max_cycles == 3

    def test_should_not_escalate_at_max_cycles(self, workflow):
        """Test no escalation at exactly max cycles (allow all configured cycles)."""
        decision = workflow.should_escalate(rework_cycle=3)

        assert not decision.should_escalate
        assert decision.rework_cycle == 3

    def test_should_not_escalate_below_max(self, workflow):
        """Test no escalation below max cycles."""
        decision = workflow.should_escalate(rework_cycle=2)

        assert not decision.should_escalate
        assert decision.rework_cycle == 2

    def test_extract_cycle_from_labels(self, workflow):
        """Test extracting cycle number from labels."""
        labels = ["bug", "rework-cycle-2", "in-progress"]
        cycle = workflow.extract_cycle_from_labels(labels)
        assert cycle == 2

    def test_extract_cycle_returns_zero_when_not_found(self, workflow):
        """Test returns 0 when no cycle label found."""
        labels = ["bug", "in-progress"]
        cycle = workflow.extract_cycle_from_labels(labels)
        assert cycle == 0

    def test_get_next_cycle_label(self, workflow):
        """Test getting next cycle label."""
        assert workflow.get_next_cycle_label(1) == "rework-cycle-2"
        assert workflow.get_next_cycle_label(0) == "rework-cycle-1"


class TestBoundedReworkEscalation:
    """Tests proving rework loops are bounded and escalate.

    Key invariant: After N failed review cycles, the issue must escalate
    to human intervention rather than looping forever.
    """

    @pytest.fixture
    def config(self):
        config = MagicMock()
        config.max_concurrent_sessions = 3
        config.max_rework_cycles = 2  # Escalate after 2 failed cycles
        return config

    @pytest.fixture
    def events(self):
        return CollectingEventSink()

    @pytest.fixture
    def workflow(self, config, events):
        return ReworkWorkflow(config=config, events=events)

    def test_bounded_rework_escalates_after_max_cycles(self, workflow):
        """CRITICAL: Rework loops must escalate after max_rework_cycles.

        This test proves the invariant that prevents infinite rework loops.
        Given max_rework_cycles=2, the system allows 2 rework cycles (1-indexed):
        - cycle 1: first rework → review → changes_requested → rework (continues)
        - cycle 2: second rework → review → changes_requested → rework (continues)
        - cycle 3: exceeds max → ESCALATE (stops)
        """
        # Cycles 0, 1, 2: should NOT escalate (up to max is allowed)
        for cycle in [0, 1, 2]:
            decision = workflow.should_escalate(rework_cycle=cycle)
            assert not decision.should_escalate, f"Cycle {cycle} should not escalate"

        # Cycle 3: MUST escalate (exceeds max_rework_cycles=2)
        decision = workflow.should_escalate(rework_cycle=3)
        assert decision.should_escalate, "Cycle 3 must escalate to prevent infinite loop"
        assert decision.reason is not None
        assert "exceeded" in decision.reason.lower() or "max" in decision.reason.lower()

    def test_escalation_emits_event_for_observability(self, workflow, events):
        """Escalation must be observable via events."""
        workflow.should_escalate(rework_cycle=3)

        # Should have emitted an escalation event
        escalation_events = [e for e in events.events if "escalat" in e.name.lower()]
        assert len(escalation_events) == 1
        assert escalation_events[0].data["rework_cycle"] == 3

    def test_different_max_cycles_respected(self):
        """Different max_rework_cycles configurations are respected."""
        for max_cycles in [1, 3, 5]:
            config = MagicMock()
            config.max_concurrent_sessions = 3
            config.max_rework_cycles = max_cycles
            workflow = ReworkWorkflow(config=config, events=CollectingEventSink())

            # At max: should not escalate (allows exactly max cycles)
            decision = workflow.should_escalate(rework_cycle=max_cycles)
            assert not decision.should_escalate

            # Above max: must escalate
            decision = workflow.should_escalate(rework_cycle=max_cycles + 1)
            assert decision.should_escalate


class TestTechLeadWorkflow:
    """Test the TechLeadWorkflow class."""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.tech_lead_review_agent = "agent:tech-lead"
        config.tech_lead_review_threshold = 3
        config.max_concurrent_sessions = 3
        return config

    @pytest.fixture
    def collecting_sink(self):
        return CollectingEventSink()

    @pytest.fixture
    def workflow(self, mock_config, collecting_sink):
        return TechLeadWorkflow(config=mock_config, events=collecting_sink)

    def test_is_configured_returns_true_when_configured(self, workflow):
        """Test is_configured returns True when tech_lead_review_agent is set."""
        assert workflow.is_configured() is True

    def test_is_configured_returns_false_when_not_configured(self, collecting_sink):
        """Test is_configured returns False when not configured."""
        config = MagicMock()
        config.tech_lead_review_agent = None
        workflow = TechLeadWorkflow(config=config, events=collecting_sink)
        assert workflow.is_configured() is False

    def test_should_launch_skips_when_not_configured(self, collecting_sink):
        """Test skips when not configured."""
        config = MagicMock()
        config.tech_lead_review_agent = None
        workflow = TechLeadWorkflow(config=config, events=collecting_sink)

        decision = workflow.should_launch_tech_lead(
            pending_tech_lead=[make_pending_tech_lead(100)],
            paused=False,
            available_slots=1,
        )

        assert not decision.should_launch

    def test_should_launch_skips_when_queue_empty(self, workflow):
        """Test skips when queue is empty."""
        decision = workflow.should_launch_tech_lead(
            pending_tech_lead=[],
            paused=False,
            available_slots=1,
        )

        assert not decision.should_launch
        assert "No pending" in decision.skip_reason

    def test_should_launch_returns_tech_lead_up_to_available_slots(self, workflow):
        """Launches up to the owner-supplied ``available_slots`` (no recompute)."""
        pending = [
            make_pending_tech_lead(100, "Test 1"),
            make_pending_tech_lead(101, "Test 2"),
            make_pending_tech_lead(102, "Test 3"),
        ]

        decision = workflow.should_launch_tech_lead(
            pending_tech_lead=pending,
            paused=False,
            available_slots=2,
        )

        assert decision.should_launch
        assert len(decision.tech_lead_to_launch) == 2

    def test_skips_when_no_available_slots(self, workflow):
        """available_slots == 0 (owner says no slot): skip with a capacity reason."""
        decision = workflow.should_launch_tech_lead(
            pending_tech_lead=[make_pending_tech_lead(100)],
            paused=False,
            available_slots=0,
        )
        assert not decision.should_launch
        assert "capacity" in decision.skip_reason.lower()

    def test_launches_up_to_available_slots_bound(self, workflow):
        """The queue is sliced by available_slots, not by max_concurrent_sessions."""
        pending = [make_pending_tech_lead(100), make_pending_tech_lead(101)]
        decision = workflow.should_launch_tech_lead(
            pending_tech_lead=pending,
            paused=False,
            available_slots=1,
        )
        assert decision.should_launch
        assert len(decision.tech_lead_to_launch) == 1

    def test_launching_event_capacity_matches_available_slots(self, workflow, collecting_sink):
        """The TECH_LEAD_LAUNCHING event reports the owner-supplied capacity, so
        the machine event can never disagree with the planned launches
        (#6892 review A2 / event contract)."""
        pending = [make_pending_tech_lead(100), make_pending_tech_lead(101)]
        workflow.should_launch_tech_lead(
            pending_tech_lead=pending, paused=False, available_slots=1
        )
        launching = [
            e for e in collecting_sink.events if e.name == "tech_lead.launching"
        ]
        assert launching and launching[-1].data["capacity"] == 1
        assert launching[-1].data["count"] == 1

    def test_still_honors_paused(self, workflow):
        """Paused is the floor: available slots do not override it."""
        decision = workflow.should_launch_tech_lead(
            pending_tech_lead=[make_pending_tech_lead(100)],
            paused=True,
            available_slots=1,
        )
        assert not decision.should_launch
        assert "paused" in decision.skip_reason.lower()
