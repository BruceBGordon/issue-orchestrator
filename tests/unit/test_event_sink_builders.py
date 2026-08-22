"""Typed event builder coverage."""

from __future__ import annotations

import pytest

from issue_orchestrator.events import EventName
from issue_orchestrator.ports.event_sink import (
    TraceEvent,
    make_review_exchange_completed_event,
    make_review_exchange_round_completed_event,
)


def test_round_completed_builder_preserves_review_decision_fields() -> None:
    event = make_review_exchange_round_completed_event(
        {
            "issue_number": 42,
            "run_dir": "/tmp/run-42",
            "session_name": "review-exchange-42",
            "round_index": 1,
            "reviewer_response_type": "ok",
            "reviewer_response_text": "Approved.",
            "coder_response_type": None,
            "review_decision_verdict": "approved",
            "review_nit_policy": "address",
            "review_abstraction_status": "no_issues",
            "artifacts": [
                {
                    "type": "review_report",
                    "label": "Review report",
                    "value": "/tmp/review-report.md",
                    "render_mode": "markdown",
                },
            ],
        }
    )

    assert event.event_type is EventName.REVIEW_EXCHANGE_ROUND_COMPLETED
    assert event.data["review_decision_verdict"] == "approved"
    assert event.data["review_nit_policy"] == "address"
    assert event.data["review_abstraction_status"] == "no_issues"


def test_round_completed_builder_rejects_missing_run_dir() -> None:
    with pytest.raises(ValueError, match="requires non-empty run_dir"):
        make_review_exchange_round_completed_event(  # type: ignore[typeddict-item]
            {
                "issue_number": 42,
                "session_name": "review-exchange-42",
                "round_index": 1,
                "reviewer_response_type": "ok",
                "reviewer_response_text": "Approved.",
                "coder_response_type": None,
            }
        )


@pytest.mark.parametrize(
    "event_name",
    (
        EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
        EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
        EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT,
    ),
)
def test_review_exchange_role_events_reject_missing_run_dir(
    event_name: EventName,
) -> None:
    with pytest.raises(ValueError, match="requires non-empty run_dir"):
        TraceEvent(event_name, {"issue_number": 42})


def test_review_exchange_completed_builder_preserves_review_decision_fields() -> None:
    event = make_review_exchange_completed_event(
        {
            "issue_number": 42,
            "session_name": "review-exchange-42",
            "rounds": 1,
            "status": "ok",
            "reason": "reviewer_ok",
            "review_decision_verdict": "approved",
            "review_nit_policy": "address",
            "review_abstraction_status": "no_issues",
        }
    )

    assert event.event_type is EventName.REVIEW_EXCHANGE_COMPLETED
    assert event.data["review_decision_verdict"] == "approved"
    assert event.data["review_nit_policy"] == "address"
    assert event.data["review_abstraction_status"] == "no_issues"
