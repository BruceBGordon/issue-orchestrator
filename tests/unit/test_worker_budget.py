"""Unit tests for the worker-slot accounting owner.

This is the single owner both the planner's ``_launch_budgets`` and the
orchestrator's E2E start-gate consult, so these tests pin the rule directly:
the tech lead's reserved additive sessions are excluded from the worker budget
when ``tech_lead.max_concurrent`` is set, otherwise every active session counts.
"""

from issue_orchestrator.control.worker_budget import (
    active_tech_lead_session_count,
    active_worker_session_count,
    worker_slot_availability,
    worker_slot_free,
)

from tests.unit.test_planner import make_config, make_issue, make_session


def _tech_lead_session(number: int, agent_label: str):
    session = make_session(make_issue(number, labels=[agent_label]))
    session.agent_label = agent_label
    return session


class TestActiveWorkerSessionCount:
    def test_shared_budget_counts_every_session(self):
        """Default (tech_lead.max_concurrent unset): all active sessions count."""
        config = make_config(tech_lead_review_agent="agent:tech-lead")
        assert config.tech_lead.max_concurrent is None
        sessions = [
            make_session(make_issue(1)),
            _tech_lead_session(2, "agent:tech-lead"),
        ]
        assert active_worker_session_count(config, sessions) == 2

    def test_reserved_budget_excludes_tech_lead_sessions(self):
        """Reserved additive budget: tech_lead sessions are NOT charged to the
        worker budget."""
        config = make_config(tech_lead_review_agent="agent:tech-lead")
        config.tech_lead.max_concurrent = 1
        sessions = [
            make_session(make_issue(1)),
            _tech_lead_session(2, "agent:tech-lead"),
        ]
        assert active_tech_lead_session_count(config, sessions) == 1
        assert active_worker_session_count(config, sessions) == 1

    def test_empty_is_zero(self):
        config = make_config()
        assert active_worker_session_count(config, []) == 0


class TestWorkerSlotFree:
    def test_free_when_below_max(self):
        config = make_config(max_concurrent_sessions=2)
        assert worker_slot_free(config, [make_session(make_issue(1))]) is True

    def test_not_free_when_workers_saturate(self):
        config = make_config(max_concurrent_sessions=1)
        assert worker_slot_free(config, [make_session(make_issue(1))]) is False

    def test_reserved_tech_lead_session_leaves_worker_slot_free(self):
        """A tech-lead session on the reserved budget does not consume the
        worker slot the E2E start-gate competes for."""
        config = make_config(
            tech_lead_review_agent="agent:tech-lead", max_concurrent_sessions=1
        )
        config.tech_lead.max_concurrent = 1
        assert worker_slot_free(config, [_tech_lead_session(9, "agent:tech-lead")]) is True

    def test_shared_tech_lead_session_consumes_worker_slot(self):
        """Default: a tech_lead session shares the worker budget, so it occupies
        the only worker slot - unchanged behavior."""
        config = make_config(
            tech_lead_review_agent="agent:tech-lead", max_concurrent_sessions=1
        )
        assert worker_slot_free(config, [_tech_lead_session(9, "agent:tech-lead")]) is False


class TestWorkerSlotAvailability:
    def test_reserved_tech_lead_reports_free_worker_capacity(self):
        config = make_config(
            tech_lead_review_agent="agent:tech-lead", max_concurrent_sessions=1
        )
        config.tech_lead.max_concurrent = 1

        slot = worker_slot_availability(
            config, [_tech_lead_session(9, "agent:tech-lead")]
        )

        assert slot.active == 0
        assert slot.maximum == 1
        assert slot.remaining == 1
        assert slot.is_free is True

    def test_over_capacity_preserves_negative_remaining_count(self):
        config = make_config(max_concurrent_sessions=1)

        slot = worker_slot_availability(
            config,
            [make_session(make_issue(1)), make_session(make_issue(2))],
        )

        assert slot.active == 2
        assert slot.remaining == -1
        assert slot.is_free is False


class TestTechLeadSlotAvailability:
    """The single owner of 'does the tech lead have a slot, and if not why'
    (#6892 review A2). Reason is derived from the SAME inputs as availability,
    with active-worker counted independently of the E2E charge (F1)."""

    def _cfg(self, *, max_sessions=1, reserved=None, agent="agent:tech-lead"):
        config = make_config(
            max_concurrent_sessions=max_sessions, tech_lead_review_agent=agent
        )
        config.tech_lead.max_concurrent = reserved
        return config

    def _avail(self, config, sessions=(), **kw):
        from issue_orchestrator.control.worker_budget import tech_lead_slot_availability

        facts = dict(
            e2e_occupies_slot=False, launched_this_tick=0, workflow_configured=True
        )
        facts.update(kw)
        return tech_lead_slot_availability(config, list(sessions), **facts)

    def test_reserved_slot_free_is_available(self):
        out = self._avail(self._cfg(reserved=1))
        assert out.available == 1 and out.reason is None

    def test_reserved_slot_occupied(self):
        cfg = self._cfg(reserved=1)
        out = self._avail(cfg, [_tech_lead_session(9, "agent:tech-lead")])
        assert out.available == 0
        assert out.reason == "reserved_slot_occupied:max_concurrent=1,active_tech_lead=1"

    def test_workflow_unavailable_wins(self):
        out = self._avail(self._cfg(reserved=1), workflow_configured=False)
        assert out.available == 0 and out.reason == "tech_lead_workflow_unavailable"

    def test_shared_worker_saturated_pre_existing(self):
        cfg = self._cfg(max_sessions=1)  # shared budget
        out = self._avail(cfg, [make_session(make_issue(1))])
        assert out.reason == "worker_slot_occupied:active=1,max=1"

    def test_shared_e2e_occupies_slot_not_misattributed_as_worker(self):
        # F1: active worker is 0; E2E holds the slot. Must NOT say worker_slot.
        out = self._avail(self._cfg(max_sessions=1), e2e_occupies_slot=True)
        assert out.reason == "e2e_occupies_worker_slot:max=1"

    def test_shared_higher_priority_launched_this_tick(self):
        out = self._avail(self._cfg(max_sessions=1), launched_this_tick=1)
        assert out.reason == "higher_priority_launched_this_tick:launched=1,max=1"

    def test_shared_slot_free(self):
        out = self._avail(self._cfg(max_sessions=2))
        assert out.available == 2 and out.reason is None


class TestTechLeadSlotAvailabilityInvariant:
    def test_reason_required_iff_zero_available(self):
        import pytest

        from issue_orchestrator.control.worker_budget import TechLeadSlotAvailability

        with pytest.raises(ValueError):
            TechLeadSlotAvailability(0, None)  # zero must carry a reason
        with pytest.raises(ValueError):
            TechLeadSlotAvailability(1, "x")  # available must not carry a reason
        # valid shapes construct fine
        assert TechLeadSlotAvailability(1, None).available == 1
        assert TechLeadSlotAvailability(0, "paused").reason == "paused"
