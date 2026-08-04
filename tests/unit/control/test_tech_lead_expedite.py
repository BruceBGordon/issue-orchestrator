"""Tech-lead expedite lane (#6870): owner cap, gate inheritance, promotion.

Covers both sides of the command surface:
  * producer -> the create-issue applier reads ``action.expedite`` + gate and
    routes the follow-up through the ExpediteLane owner (execute now / propose
    deferred);
  * consumer -> RetryHistoryState performs the bounded front-queue write and the
    un-gate promotion.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest

from issue_orchestrator.domain.tech_lead_session import TechLeadCreationOrigin
from issue_orchestrator.control.actions import CreateTechLeadIssueAction
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.retry_history_state import (
    ExpediteEligibility,
    ExpediteLane,
    RetryHistoryState,
)
from issue_orchestrator.control.tech_lead_issue_creation import (
    apply_create_tech_lead_issue,
)
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL


def _lane(state, *, max_expedited=3, eligible=(), in_scope=()):
    return ExpediteLane(
        owner_factory=lambda: RetryHistoryState(state),
        eligibility_provider=lambda: ExpediteEligibility(
            eligible=frozenset(eligible), in_scope=frozenset(in_scope)
        ),
        max_expedited=max_expedited,
    )


def _apply_create(state, *, labels, expedite, issue_number=100, lane=None,
                  max_expedited=3):
    repo = Mock()
    repo.create_issue.return_value = {"number": issue_number}
    action = CreateTechLeadIssueAction(
        title="Fix it",
        body="Body",
        labels=tuple(labels),
        expedite=expedite,
        # Expedite intent only ever rides a DECISION-driven create_issue, so
        # this creation is derived from its session's anchor and carries the
        # expectations that anchor's gate checks (#6957 R2 F6).
        origin=TechLeadCreationOrigin.derived_from_anchor(77),
        expected=build_expected_for_mutation(),
    )
    result = apply_create_tech_lead_issue(
        action,
        repository_host=repo,
        events=Mock(),
        ops=None,
        add_comment=lambda number, comment: "https://example/comment",
        emit_labels_changed=lambda *args: None,
        expedite_lane=lane or _lane(state, max_expedited=max_expedited),
    )
    assert result.success
    return result


# --------------------------------------------------------------------------- #
# Owner: bounded front-queue write (decisions #3 + #4)                          #
# --------------------------------------------------------------------------- #

class TestExpediteOwner:
    def test_expedite_issue_front_inserts_and_tracks(self):
        state = OrchestratorState(priority_queue=[5])
        outcome = RetryHistoryState(state).expedite_issue_front(9, max_expedited=3)
        assert outcome.expedited is True
        assert outcome.reason == "expedited"
        assert outcome.outstanding == 1
        # Front of the lane, and recorded as a tech-lead expedite for the cap.
        assert state.priority_queue == [9, 5]
        assert state.tech_lead_expedited == [9]

    def test_already_queued_consumes_no_slot(self):
        state = OrchestratorState(priority_queue=[9])
        outcome = RetryHistoryState(state).expedite_issue_front(9, max_expedited=3)
        assert outcome.expedited is False
        assert outcome.reason == "already_queued"
        assert state.priority_queue == [9]
        assert state.tech_lead_expedited == []

    def test_cap_reached_skips_and_logs(self, caplog):
        # Two tech-lead-expedited issues already outstanding, cap of 2.
        state = OrchestratorState(
            priority_queue=[1, 2], tech_lead_expedited=[1, 2]
        )
        with caplog.at_level(logging.INFO):
            outcome = RetryHistoryState(state).expedite_issue_front(3, max_expedited=2)
        assert outcome.expedited is False
        assert outcome.reason == "cap_reached"
        assert outcome.outstanding == 2
        assert 3 not in state.priority_queue
        # No silent truncation: the skip is logged (CLAUDE.md forbids silent caps).
        assert "Not expediting issue #3" in caplog.text

    def test_max_expedited_zero_disables(self):
        state = OrchestratorState()
        outcome = RetryHistoryState(state).expedite_issue_front(9, max_expedited=0)
        assert outcome.expedited is False
        assert outcome.reason == "disabled"
        assert state.priority_queue == []

    def test_cap_counts_only_tech_lead_entries_not_operator_ones(self):
        # Operator/retry priorities (not in the tech-lead ledger) never count
        # against the expedite cap.
        state = OrchestratorState(priority_queue=[1, 2, 3], tech_lead_expedited=[])
        outcome = RetryHistoryState(state).expedite_issue_front(9, max_expedited=1)
        assert outcome.expedited is True
        assert state.priority_queue == [9, 1, 2, 3]

    def test_stale_ledger_entries_are_pruned_before_counting(self):
        # 7 was expedited then worked (left priority_queue): it must not keep
        # occupying a cap slot.
        state = OrchestratorState(priority_queue=[1], tech_lead_expedited=[1, 7])
        outcome = RetryHistoryState(state).expedite_issue_front(9, max_expedited=2)
        assert outcome.expedited is True
        assert state.tech_lead_expedited == [1, 9]

    def test_deprioritize_prunes_expedited_ledger(self):
        state = OrchestratorState(
            priority_queue=[1, 2], tech_lead_expedited=[1, 2]
        )
        RetryHistoryState(state).deprioritize_issues([1])
        assert state.priority_queue == [2]
        assert state.tech_lead_expedited == [2]

    def test_release_expedited_frees_slot_and_dequeues(self):
        state = OrchestratorState(
            priority_queue=[9, 5], tech_lead_expedited=[9]
        )
        released = RetryHistoryState(state).release_expedited(9)
        assert released is True
        # Freed from BOTH the cap ledger and the front queue.
        assert state.tech_lead_expedited == []
        assert state.priority_queue == [5]

    def test_release_is_scoped_to_expedited_issues(self):
        # An operator/retry priority_queue entry (not in the expedite ledger) is
        # never touched: releasing it is a no-op.
        state = OrchestratorState(priority_queue=[7], tech_lead_expedited=[])
        released = RetryHistoryState(state).release_expedited(7)
        assert released is False
        assert state.priority_queue == [7]

    def test_release_reopens_a_cap_slot(self):
        state = OrchestratorState()
        owner = RetryHistoryState(state)
        assert owner.expedite_issue_front(1, max_expedited=2).expedited
        assert owner.expedite_issue_front(2, max_expedited=2).expedited
        # At the cap.
        assert owner.expedite_issue_front(3, max_expedited=2).reason == "cap_reached"
        # #1 gets worked -> slot released -> a further expedite now succeeds.
        owner.release_expedited(1)
        assert owner.expedite_issue_front(3, max_expedited=2).expedited
        assert state.priority_queue[0] == 3


# --------------------------------------------------------------------------- #
# Owner: pending / promotion (decision #2 propose path)                         #
# --------------------------------------------------------------------------- #

class TestExpeditePromotion:
    def test_record_pending_is_idempotent(self):
        state = OrchestratorState()
        owner = RetryHistoryState(state)
        owner.record_expedite_pending(50)
        owner.record_expedite_pending(50)
        assert state.tech_lead_expedite_pending == [50]
        assert owner.has_expedite_pending() is True

    def test_promote_eligible_stays_gated_and_drops_gone(self):
        state = OrchestratorState(
            tech_lead_expedite_pending=[10, 20, 30]
        )
        outcomes = RetryHistoryState(state).promote_expedite_pending(
            eligible={10}, in_scope={10, 20}, max_expedited=3
        )
        # 10 un-gated -> expedited to the front.
        assert state.priority_queue == [10]
        assert [o.issue_number for o in outcomes if o.expedited] == [10]
        # 20 still gated (in scope, not eligible) -> stays pending.
        # 30 gone from scope (rejected/closed) -> dropped so pending never leaks.
        assert state.tech_lead_expedite_pending == [20]

    def test_promote_cap_blocked_keeps_pending_for_retry(self):
        state = OrchestratorState(
            priority_queue=[1], tech_lead_expedited=[1],
            tech_lead_expedite_pending=[10],
        )
        outcomes = RetryHistoryState(state).promote_expedite_pending(
            eligible={10}, in_scope={10}, max_expedited=1
        )
        assert outcomes[0].reason == "cap_reached"
        assert 10 not in state.priority_queue
        # Retried next tick once a slot frees.
        assert state.tech_lead_expedite_pending == [10]

    def test_lane_promote_short_circuits_when_nothing_pending(self):
        state = OrchestratorState()

        def _boom():
            raise AssertionError("eligibility must not be computed when idle")

        lane = ExpediteLane(
            owner_factory=lambda: RetryHistoryState(state),
            eligibility_provider=_boom,
            max_expedited=3,
        )
        assert lane.promote_ungated() == []


# --------------------------------------------------------------------------- #
# Applier boundary: gate inheritance (decision #2)                              #
# --------------------------------------------------------------------------- #

class TestExpediteApplierGate:
    def test_execute_authority_expedites_immediately(self):
        state = OrchestratorState()
        _apply_create(state, labels=("agent:web",), expedite=True)
        # Ungated (execute) create_issue jumps the lane at creation.
        assert state.priority_queue == [100]
        assert state.tech_lead_expedited == [100]
        assert state.tech_lead_expedite_pending == []

    def test_no_expedite_does_not_touch_the_lane(self):
        state = OrchestratorState()
        _apply_create(state, labels=("agent:web",), expedite=False)
        assert state.priority_queue == []
        assert state.tech_lead_expedite_pending == []

    def test_propose_authority_defers_at_creation_then_promotes_on_ungate(self):
        state = OrchestratorState()
        # Gated (propose) create_issue carries proposed-tech-lead.
        _apply_create(
            state,
            labels=(PROPOSED_TECH_LEAD_LABEL, "agent:web"),
            expedite=True,
        )
        # Does NOT enqueue at creation: the issue is still gated (decision #2).
        assert state.priority_queue == []
        assert state.tech_lead_expedite_pending == [100]

        # Operator removes the gate -> the issue becomes eligible; the per-tick
        # promotion moves it to the front of the lane.
        lane = _lane(state, eligible={100}, in_scope={100})
        lane.promote_ungated()
        assert state.priority_queue == [100]
        assert state.tech_lead_expedite_pending == []

    def test_expedite_cap_zero_disables_execute_path(self):
        state = OrchestratorState()
        _apply_create(
            state, labels=("agent:web",), expedite=True, max_expedited=0
        )
        assert state.priority_queue == []


class TestExpediteSlotReleaseIntegration:
    """B1 regression: launching an expedited issue frees its cap slot."""

    def test_launch_session_releases_the_expedite_slot(self):
        from issue_orchestrator.control.action_applier import ActionApplier
        from issue_orchestrator.control.actions import LaunchSessionAction
        from issue_orchestrator.control.session_manager import SessionType

        state = OrchestratorState()
        lane = _lane(state, max_expedited=2)
        # Fill the lane to the cap via the execute path.
        assert lane.expedite_now(101).expedited
        assert lane.expedite_now(102).expedited
        assert lane.expedite_now(103).reason == "cap_reached"

        applier = ActionApplier(labels=Mock(), sessions=Mock(), events=Mock())
        applier.expedite_lane = lane
        session = Mock()
        session.terminal_id = "issue-101"
        session.issue.number = 101
        applier.session_launcher = lambda session_type, number: session

        # Issue #101 is picked up as an active session.
        result = applier.apply(
            LaunchSessionAction(session_type=SessionType.ISSUE, number=101)
        )
        assert result.success
        # Its cap slot and front-queue entry are freed (via the queue owner).
        assert 101 not in state.tech_lead_expedited
        assert 101 not in state.priority_queue
        # A further expedite now succeeds — the slot was released.
        assert lane.expedite_now(103).expedited
        assert 103 in state.priority_queue

    def test_launch_of_non_expedited_issue_leaves_operator_queue_untouched(self):
        from issue_orchestrator.control.action_applier import ActionApplier
        from issue_orchestrator.control.actions import LaunchSessionAction
        from issue_orchestrator.control.session_manager import SessionType

        # Operator priority (not tech-lead-expedited).
        state = OrchestratorState(priority_queue=[55])
        applier = ActionApplier(labels=Mock(), sessions=Mock(), events=Mock())
        applier.expedite_lane = _lane(state, max_expedited=2)
        session = Mock()
        session.terminal_id = "issue-55"
        session.issue.number = 55
        applier.session_launcher = lambda session_type, number: session

        result = applier.apply(
            LaunchSessionAction(session_type=SessionType.ISSUE, number=55)
        )
        assert result.success
        # Operator priority_queue semantics are unchanged.
        assert state.priority_queue == [55]
