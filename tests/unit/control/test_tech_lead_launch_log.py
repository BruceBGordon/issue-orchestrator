"""Unit tests for the TechLeadLaunchLog owner (per-issue launch-decision logging)."""

import logging

from issue_orchestrator.control.tech_lead_launch_log import TechLeadLaunchLog
from issue_orchestrator.domain.models import PendingTechLeadReview
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

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


def test_capacity_deferral_reserved_slot_full(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _owner().capacity_deferral(
            [_item(6887)],
            0,  # reserved slot exhausted
            active_count=1,
            max_concurrent_sessions=1,
            tech_lead_max_concurrent=1,
            active_tech_lead=1,
        )
    assert "issue=6887" in caplog.text
    assert "no_reserved_capacity:max_concurrent=1,active_tech_lead=1" in caplog.text


def test_capacity_deferral_shared_worker_budget_full(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _owner().capacity_deferral(
            [_item(6887)],
            None,  # tech_lead shares the worker budget
            active_count=2,
            max_concurrent_sessions=2,
            tech_lead_max_concurrent=None,
            active_tech_lead=0,
        )
    assert "no_worker_capacity:active=2,max=2" in caplog.text


def test_on_change_dedup_and_retain(caplog) -> None:
    owner = _owner()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        owner.defer_all([_item(6887)], "orchestrator_paused")
        owner.defer_all([_item(6887)], "orchestrator_paused")  # same -> silent
        owner.retain([])  # 6887 departed -> forgotten
        owner.defer_all([_item(6887)], "orchestrator_paused")  # logs fresh
    lines = [r for r in caplog.records if "issue=6887" in r.getMessage()]
    assert len(lines) == 2  # first + post-retain, not the middle repeat
