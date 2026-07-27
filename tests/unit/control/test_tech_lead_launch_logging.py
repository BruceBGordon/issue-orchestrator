"""The tech_lead launch decision must explain itself in the log (on-change).

A queued tech_lead session (e.g. a health review) that keeps being deferred used
to be invisible: the skip was emitted only as an ephemeral event, so `trace
<issue>` showed "Queued ..." and then silence. These tests pin that every launch
outcome — launch / paused-skip / no-reserved-capacity defer — now writes an
`issue=<n>`-keyed INFO line (so `trace` surfaces it), and that a steady state
logs once, not every tick.
"""

import logging

import pytest

from issue_orchestrator.control.actions import LaunchSessionAction, SessionType
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.workflows import TechLeadWorkflow
from issue_orchestrator.domain.models import PendingTechLeadReview
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
