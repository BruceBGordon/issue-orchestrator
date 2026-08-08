"""The planner enforces the tech-lead scope barrier it does not own (#6994).

``plan_tech_lead_launch_gate`` is unit-tested on its own; what these tests pin is
the WIRING — that the planner consults it before capacity and the provider gate,
and reports what it withheld as a scope barrier rather than as a capacity skip.
Mislabelling the two is not cosmetic: "no capacity" tells an operator to raise
``tech_lead.max_concurrent``, which would not release a single held run.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.control.actions import ActionType
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.domain.tech_lead_run import BARRIER_GLOBAL_RUN_QUEUED
from issue_orchestrator.control.workflows.tech_lead_workflow import TechLeadWorkflow
from issue_orchestrator.domain.models import (
    AgentConfig,
    DiscoveredFailure,
    PendingTechLeadReview,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.infra.config import Config
from tests.unit.test_planner import make_snapshot

TECH_LEAD_AGENT = "agent:tech-lead"


def _planner() -> Planner:
    from tests.unit.test_planner import InMemoryEventSink

    config = Config()
    config.tech_lead_review_agent = TECH_LEAD_AGENT
    config.agents[TECH_LEAD_AGENT] = AgentConfig(
        command="claude", prompt_path=Path("/tmp/tech-lead.md")
    )
    config.max_concurrent_sessions = 4
    return Planner(
        config=config,
        scheduler=Scheduler(config),
        tech_lead_workflow=TechLeadWorkflow(config, InMemoryEventSink()),
    )


def _investigation(number: int) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        issue_number=number,
        title=f"Investigate #{number}",
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        failure=DiscoveredFailure(number, f"Investigate #{number}", "timed_out"),
    )


def _health_review(anchor: int = 900) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        issue_number=anchor,
        title="Health Review",
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
    )


def _tech_lead_launches(plan) -> list[int]:
    from issue_orchestrator.control.session_manager import SessionType

    return [
        action.number
        for action in plan.actions_of_type(ActionType.LAUNCH_SESSION)
        if getattr(action, "session_type", None) is SessionType.TECH_LEAD
    ]


def test_targeted_runs_launch_together_when_no_global_run_is_queued():
    plan = _planner().plan(
        make_snapshot(pending_tech_lead=[_investigation(42), _investigation(73)])
    )

    assert sorted(_tech_lead_launches(plan)) == [42, 73]


def test_a_queued_global_run_is_the_only_launch_and_the_rest_are_barrier_skips():
    plan = _planner().plan(
        make_snapshot(pending_tech_lead=[_investigation(42), _health_review()])
    )

    assert _tech_lead_launches(plan) == [900]
    barrier_skips = [
        item
        for item in plan.skipped
        if item.item_type == "tech_lead" and item.reason == BARRIER_GLOBAL_RUN_QUEUED
    ]
    assert [item.number for item in barrier_skips] == [42]


def test_a_held_run_is_never_reported_as_a_capacity_skip():
    """The reason must name the rule that actually withheld the run."""
    plan = _planner().plan(
        make_snapshot(pending_tech_lead=[_investigation(42), _health_review()])
    )

    reasons = {item.reason for item in plan.skipped if item.item_type == "tech_lead"}
    assert reasons == {BARRIER_GLOBAL_RUN_QUEUED}
    assert not any("capacity" in reason for reason in reasons)
