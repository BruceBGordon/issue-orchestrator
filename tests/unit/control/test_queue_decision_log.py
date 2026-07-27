"""Unit tests for the QueueDecisionLog owner (moved verbatim from the planner)."""

import logging

from issue_orchestrator.control.queue_decision_log import QueueDecisionLog

LOGGER = "test.queue_decision_log"


def _owner() -> QueueDecisionLog:
    # Large interval so the periodic summary never fires mid-test unless forced.
    return QueueDecisionLog(logging.getLogger(LOGGER), summary_interval_seconds=1e9)


def test_launch_and_skip_lines_keyed_by_issue(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _owner().record(
            {1: "launch:scheduled", 2: "skip:blocked_by_milestone"},
            {},
        )
    text = caplog.text
    assert "issue=1 decision=launch reason=scheduled" in text
    assert "issue=2 decision=skip reason=blocked_by_milestone" in text


def test_dependency_blocked_carries_detail(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _owner().record({5: "skip:dependency_blocked"}, {5: "waiting on #4"})
    assert "issue=5 decision=skip reason=dependency_blocked detail=waiting on #4" in caplog.text


def test_on_change_only_and_prune(caplog) -> None:
    owner = _owner()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        owner.record({1: "skip:blocked_by_milestone"}, {})
        owner.record({1: "skip:blocked_by_milestone"}, {})  # unchanged -> silent
        owner.record({}, {})  # 1 pruned
        owner.record({1: "skip:blocked_by_milestone"}, {})  # logs fresh
    lines = [r for r in caplog.records if "issue=1" in r.getMessage()]
    assert len(lines) == 2


def test_periodic_summary_emitted(caplog) -> None:
    # _last_summary_at starts at 0.0; a clock past the interval fires the summary.
    owner = QueueDecisionLog(
        logging.getLogger(LOGGER), summary_interval_seconds=60.0, clock=lambda: 100.0
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        owner.record({1: "launch:scheduled", 2: "skip:blocked_by_milestone"}, {})
    assert "trace-queue-summary total=2 launch=1 skip=1" in caplog.text
