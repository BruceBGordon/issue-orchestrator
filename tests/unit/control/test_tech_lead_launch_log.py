"""Unit tests for the TechLeadLaunchLog owner (per-issue launch-decision logging)."""

import logging

from issue_orchestrator.control.tech_lead_launch_log import (
    TechLeadLaunchLog,
    no_slot_reason,
)
from issue_orchestrator.domain.models import PendingTechLeadReview
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor


def _reason(**overrides):
    facts = dict(
        workflow_configured=True,
        reserved_capacity=None,
        worker_active_count=0,
        launched_this_tick=0,
        e2e_occupies_slot=False,
        max_sessions=1,
        tech_lead_max_concurrent=None,
        active_tech_lead=0,
    )
    facts.update(overrides)
    return no_slot_reason(**facts)


def test_no_slot_reason_distinguishes_every_cause() -> None:
    # unavailable workflow wins over everything
    assert _reason(workflow_configured=False) == "tech_lead_workflow_unavailable"
    # reserved additive slot occupied
    assert _reason(reserved_capacity=0, tech_lead_max_concurrent=1, active_tech_lead=1) == (
        "reserved_slot_occupied:max_concurrent=1,active_tech_lead=1"
    )
    # pre-existing worker saturation
    assert _reason(worker_active_count=1) == "worker_slot_occupied:active=1,max=1"
    # E2E holding the worker slot
    assert _reason(e2e_occupies_slot=True) == "e2e_occupies_worker_slot:max=1"
    # the F1 case: higher-priority launch consumed the last slot THIS tick
    assert _reason(launched_this_tick=1) == (
        "higher_priority_launched_this_tick:launched=1,max=1"
    )
    # fallback: genuinely no capacity, active still 0
    assert _reason() == "no_worker_capacity:active=0,max=1"

LOGGER = "test.tech_lead_launch_log"


def _item(number: int) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        issue_number=number,
        title=f"tl-{number}",
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
    )


def _owner() -> TechLeadLaunchLog:
    return TechLeadLaunchLog(logging.getLogger(LOGGER))


def test_gate_skip_logs_reason_keyed_by_issue(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _owner().gate_skip([_item(6887)], "Orchestrator paused")
    assert "issue=6887" in caplog.text
    assert "decision=skip" in caplog.text and "Orchestrator paused" in caplog.text


def test_launch_outcomes_classifies_each_item(caplog) -> None:
    a, b, c = _item(1), _item(2), _item(3)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        # a launched, b hit an open provider circuit, c had no slot left.
        _owner().launch_outcomes(
            [a, b, c], launched=[a], provider_skipped=[b], reserved=True, provider="claude"
        )
    text = caplog.text
    assert "issue=1 flavor=health_review decision=launch reason=reserved_slot" in text
    assert "issue=2" in text and "provider_circuit_open:claude" in text
    assert "issue=3" in text and "decision=defer reason=no_free_slot" in text


def test_defer_all_logs_caller_supplied_reason(caplog) -> None:
    # The reason is computed by the budget/priority owner and passed in verbatim.
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _owner().defer_all([_item(6887)], "higher_priority_launched_this_tick:launched=1,max=1")
    assert "issue=6887" in caplog.text
    assert "decision=defer" in caplog.text
    assert "higher_priority_launched_this_tick:launched=1,max=1" in caplog.text


def test_note_suppressed_logs_cohort_escalation(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _owner().note_suppressed(_item(6327), pending=1)
    assert "issue=6327" in caplog.text
    assert "decision=defer" in caplog.text
    assert "suppressed_cohort_escalated" in caplog.text


def test_on_change_dedup_and_retain(caplog) -> None:
    owner = _owner()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        owner.defer_all([_item(6887)], "orchestrator_paused")
        owner.defer_all([_item(6887)], "orchestrator_paused")  # same -> silent
        owner.retain([])  # 6887 departed -> forgotten
        owner.defer_all([_item(6887)], "orchestrator_paused")  # logs fresh
    lines = [r for r in caplog.records if "issue=6887" in r.getMessage()]
    assert len(lines) == 2  # first + post-retain, not the middle repeat
