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

from issue_orchestrator.control.actions import (
    AddLabelAction,
    LaunchSessionAction,
    LaunchValidationRetryAction,
    SessionType,
)
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.planner_types import OrchestratorSnapshot
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.workflows import TechLeadWorkflow
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    PendingReview,
    PendingTechLeadReview,
    PendingValidationRetry,
)
from issue_orchestrator.domain.session_key import TaskKind
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


def _health_review(number: int = ANCHOR) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        issue_number=number,
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


def test_e2e_occupancy_is_not_misclassified_as_worker_saturation(caplog) -> None:
    # #6892 review F1: shared budget, no active sessions, but a first-class E2E
    # run holds the single worker slot. The tech-lead deferral must name E2E, not
    # a false "worker_slot_occupied:active=1".
    config = Config(repo="test/repo", max_concurrent_sessions=1)
    config.tech_lead_review_agent = "agent:tech-lead"  # shared budget (no max_concurrent)
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        tech_lead_workflow=TechLeadWorkflow(config, InMemoryEventSink()),
    )
    snapshot = make_snapshot(
        pending_tech_lead=[_health_review()], e2e_occupies_slot=True
    )
    with caplog.at_level(logging.INFO, logger=PLANNER_LOGGER):
        planner.plan(snapshot)
    assert f"issue={ANCHOR}" in caplog.text
    assert "e2e_occupies_worker_slot" in caplog.text
    assert "worker_slot_occupied:active=1" not in caplog.text


def test_provider_skipped_review_does_not_steal_the_tech_lead_slot(caplog) -> None:
    # #6892 review F2: a review whose provider circuit is open produces an
    # AddLabelAction, NOT a session launch. It must not consume worker capacity
    # nor be counted as a higher-priority launch — the tech lead (on an available
    # provider) still gets the shared slot.
    config = Config(repo="test/repo", max_concurrent_sessions=1)
    config.tech_lead_review_agent = "agent:tech-lead"  # shared budget
    review = PendingReview(
        issue_key=FakeIssueKey(name="10"),
        pr_number=100,
        pr_url="url",
        branch_name="branch",
        _issue_number=10,
    )
    review_wf = Mock()
    review_wf.is_configured.return_value = True
    review_wf.should_launch_reviews.return_value = Mock(
        should_launch=True, skip_reason=None, reviews_to_launch=[review]
    )
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        review_workflow=review_wf,
        tech_lead_workflow=TechLeadWorkflow(config, InMemoryEventSink()),
    )
    # review provider open, tech-lead provider available.
    policy = Mock()
    policy.provider_for_agent_label = (
        lambda label: "prov-tl" if label == "agent:tech-lead" else "prov-review"
    )
    policy.is_open = lambda prov: prov == "prov-review"
    policy.should_add_blocked_label = lambda *a, **k: True
    planner.provider_policy = policy
    snapshot = make_snapshot(
        pending_reviews=[review], pending_tech_lead=[_health_review()]
    )  # review issue 10 absent from snapshot.issues
    with caplog.at_level(logging.INFO, logger=PLANNER_LOGGER):
        plan = planner.plan(snapshot)
    # the review was provider-skipped (label, not launch)...
    assert any(isinstance(a, AddLabelAction) and a.issue_number == 10 for a in plan.actions)
    assert not any(
        isinstance(a, LaunchSessionAction) and a.session_type is SessionType.REVIEW
        for a in plan.actions
    )
    # ...and the tech lead STILL got the shared slot (not stolen by the skip).
    tl = [
        a
        for a in plan.actions
        if isinstance(a, LaunchSessionAction) and a.session_type is SessionType.TECH_LEAD
    ]
    assert [a.number for a in tl] == [ANCHOR]
    assert "higher_priority_launched_this_tick" not in caplog.text


def _validation_retry(issue_number: int = 1) -> PendingValidationRetry:
    return PendingValidationRetry(
        issue_number=issue_number,
        issue_title="Retry me",
        agent_label="agent:developer",
        worktree_path=f"/tmp/repo-{issue_number}",
        branch_name=f"{issue_number}-retry",
        original_prompt="original task",
        validation_error="dirty worktree",
        validation_error_file=None,
        retry_count=1,
        source_task=TaskKind.CODE,
        validation_cmd="make test",
    )


def test_validation_retry_consumes_a_slot_and_does_not_oversubscribe_tech_lead(
    caplog,
) -> None:
    # #6892 review F1: LaunchValidationRetryAction is a capacity-consuming launch
    # too. With one shared slot, a pending validation retry AND a pending
    # tech-lead item must NOT both launch; the retry takes the slot, the tech
    # lead is deferred (no oversubscription of max_concurrent_sessions=1).
    config = Config(repo="test/repo", max_concurrent_sessions=1)
    config.tech_lead_review_agent = "agent:tech-lead"  # shared budget
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        tech_lead_workflow=TechLeadWorkflow(config, InMemoryEventSink()),
    )
    snapshot = make_snapshot(
        pending_validation_retries=[_validation_retry()],
        pending_tech_lead=[_health_review()],
    )
    with caplog.at_level(logging.INFO, logger=PLANNER_LOGGER):
        plan = planner.plan(snapshot)
    consuming = [
        a
        for a in plan.actions
        if isinstance(a, (LaunchSessionAction, LaunchValidationRetryAction))
    ]
    assert len(consuming) == 1  # only the retry — the single slot is not oversubscribed
    assert any(isinstance(a, LaunchValidationRetryAction) for a in plan.actions)
    assert not any(
        isinstance(a, LaunchSessionAction) and a.session_type is SessionType.TECH_LEAD
        for a in plan.actions
    )
    assert f"issue={ANCHOR}" in caplog.text
    assert "higher_priority_launched_this_tick" in caplog.text


def test_launching_event_count_matches_planned_launches_under_e2e() -> None:
    # #6892 review F2/A2 (event contract): max=2, E2E holds one worker slot, two
    # pending tech leads. Availability is 1, so the TECH_LEAD_LAUNCHING event must
    # report count=1/capacity=1 (matching the single planned launch), not the
    # pre-owner false count=2.
    config = Config(repo="test/repo", max_concurrent_sessions=2)
    config.tech_lead_review_agent = "agent:tech-lead"  # shared budget
    events = InMemoryEventSink()
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        tech_lead_workflow=TechLeadWorkflow(config, events),
    )
    snapshot = make_snapshot(
        pending_tech_lead=[_health_review(6887), _health_review(6888)],
        e2e_occupies_slot=True,
    )
    plan = planner.plan(snapshot)
    tl_launches = [
        a
        for a in plan.actions
        if isinstance(a, LaunchSessionAction) and a.session_type is SessionType.TECH_LEAD
    ]
    launching = [e for e in events.events if e.name == "tech_lead.launching"]
    assert len(tl_launches) == 1
    assert launching and launching[-1].data["count"] == len(tl_launches)
    assert launching[-1].data["capacity"] == 1


def test_provider_open_tech_lead_no_launch_and_no_launching_event() -> None:
    # #6892 review: the TECH_LEAD_LAUNCHING event must not claim a launch the
    # provider gate then suppresses. With the tech-lead provider circuit OPEN, the
    # plan carries only the provider-skip label — no LaunchSessionAction — and NO
    # TECH_LEAD_LAUNCHING event fires (provider eligibility is applied before the
    # workflow decides/publishes).
    config = Config(repo="test/repo", max_concurrent_sessions=1)
    config.tech_lead_review_agent = "agent:tech-lead"
    events = InMemoryEventSink()
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        tech_lead_workflow=TechLeadWorkflow(config, events),
    )
    policy = Mock()
    policy.provider_for_agent_label = lambda label: "prov-tl"
    policy.is_open = lambda prov: True  # tech-lead provider circuit OPEN
    policy.should_add_blocked_label = lambda *a, **k: True
    planner.provider_policy = policy
    plan = planner.plan(make_snapshot(pending_tech_lead=[_health_review()]))
    assert not any(
        isinstance(a, LaunchSessionAction) and a.session_type is SessionType.TECH_LEAD
        for a in plan.actions
    )
    assert any(isinstance(a, AddLabelAction) for a in plan.actions)  # provider-skip label
    assert [e for e in events.events if e.name == "tech_lead.launching"] == []
