"""The tech_lead launch decision must explain itself in the log (on-change).

A queued tech_lead session (e.g. a health review) that keeps being deferred used
to be invisible: the skip was emitted only as an ephemeral event, so `trace
<issue>` showed "Queued ..." and then silence. These tests pin that every launch
outcome — launch / paused-skip / no-reserved-capacity defer — now writes an
`issue=<n>`-keyed INFO line (so `trace` surfaces it), and that a steady state
logs once, not every tick.
"""

import logging
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.actions import LaunchSessionAction, SessionType
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.workflows import TechLeadWorkflow
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import PendingReview, PendingTechLeadReview
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.event_sink import InMemoryEventSink
from tests.unit.test_planner import make_snapshot

PLANNER_LOGGER = "issue_orchestrator.control.planner"
ANCHOR = 6887


def _planner() -> Planner:
    config = Config(repo="test/repo", max_concurrent_sessions=1)
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead.max_concurrent = 1  # reserved additive slot
    return Planner(
        config=config,
        scheduler=Scheduler(config),
        tech_lead_workflow=TechLeadWorkflow(config, InMemoryEventSink()),
    )


def _health_review() -> PendingTechLeadReview:
    return PendingTechLeadReview(
        issue_number=ANCHOR,
        title="Health Review — walk the floor",
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
    )


def _snapshot(*, paused: bool = False) -> OrchestratorSnapshot:
    return make_snapshot(pending_tech_lead=[_health_review()], paused=paused)


def test_launch_decision_is_logged_with_issue_key(caplog) -> None:
    planner = _planner()
    with caplog.at_level(logging.INFO, logger=PLANNER_LOGGER):
        plan = planner.plan(_snapshot())
    launches = [
        a
        for a in plan.actions
        if isinstance(a, LaunchSessionAction) and a.session_type is SessionType.TECH_LEAD
    ]
    assert [a.number for a in launches] == [ANCHOR]  # it did launch
    assert f"issue={ANCHOR}" in caplog.text  # trace-visible
    assert "decision=launch" in caplog.text and "reserved_slot" in caplog.text


def test_paused_deferral_reason_is_logged(caplog) -> None:
    # The #6887-while-paused blind spot: plan() returns empty before the launch
    # path, so the queued health review must be explained at the paused exit.
    planner = _planner()
    with caplog.at_level(logging.INFO, logger=PLANNER_LOGGER):
        plan = planner.plan(_snapshot(paused=True))
    assert not any(isinstance(a, LaunchSessionAction) for a in plan.actions)
    assert f"issue={ANCHOR}" in caplog.text
    assert "decision=defer" in caplog.text
    assert "orchestrator_paused" in caplog.text


def test_steady_state_logs_once_not_every_tick(caplog) -> None:
    planner = _planner()
    with caplog.at_level(logging.INFO, logger=PLANNER_LOGGER):
        planner.plan(_snapshot(paused=True))
        planner.plan(_snapshot(paused=True))  # same decision next tick
    defer_lines = [
        r
        for r in caplog.records
        if "trace-tech-lead-decision" in r.getMessage()
        and "orchestrator_paused" in r.getMessage()
    ]
    assert len(defer_lines) == 1  # on-change: the repeat is suppressed


def test_review_taking_last_shared_slot_reports_true_reason_not_false_saturation(
    caplog,
) -> None:
    # #6892 review F1: shared budget (no tech_lead.max_concurrent), 1 slot. A
    # higher-priority review launches and consumes the slot IN this tick, while
    # snapshot.active_count is still 0. The tech-lead deferral must name the real
    # cause (higher-priority in-tick launch), not a false "no_worker_capacity:
    # active=0".
    config = Config(repo="test/repo", max_concurrent_sessions=1)
    config.tech_lead_review_agent = "agent:tech-lead"
    # shared budget: leave config.tech_lead.max_concurrent = None
    review = PendingReview(
        issue_key=FakeIssueKey(name="1"),
        pr_number=100,
        pr_url="url",
        branch_name="branch",
        _issue_number=1,
    )
    review_wf = Mock()
    review_wf.is_configured.return_value = True
    decision = Mock(should_launch=True, skip_reason=None, reviews_to_launch=[review])
    review_wf.should_launch_reviews.return_value = decision
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        review_workflow=review_wf,
        tech_lead_workflow=TechLeadWorkflow(config, InMemoryEventSink()),
    )
    snapshot = make_snapshot(
        pending_reviews=[review], pending_tech_lead=[_health_review()]
    )
    with caplog.at_level(logging.INFO, logger=PLANNER_LOGGER):
        plan = planner.plan(snapshot)
    # the review consumed the single shared slot this tick...
    assert any(
        isinstance(a, LaunchSessionAction) and a.session_type is SessionType.REVIEW
        for a in plan.actions
    )
    # ...and the tech-lead deferral names the TRUE cause, not false saturation.
    assert f"issue={ANCHOR}" in caplog.text
    assert "higher_priority_launched_this_tick" in caplog.text
    assert "no_worker_capacity:active=0" not in caplog.text
