"""Tests for timeline write-side artifact invariants."""

from __future__ import annotations

import pytest

from issue_orchestrator.execution.timeline_artifact_expectations import (
    REVIEW_PHASE_LOG_TIMELINE_EVENTS,
    event_requires_run_dir,
    validate_event_artifact_expectations,
)


@pytest.mark.parametrize("event_name", sorted(REVIEW_PHASE_LOG_TIMELINE_EVENTS))
def test_review_phase_log_events_require_run_dir_at_write_boundary(
    event_name: str,
) -> None:
    assert event_requires_run_dir(event_name)

    with pytest.raises(RuntimeError, match=f"event={event_name} missing_field=run_dir"):
        validate_event_artifact_expectations(event_name, {"issue_number": 1})


def test_invalid_completion_record_requires_run_and_existing_completion_file(
    tmp_path,
) -> None:
    event_name = "session.invalid_completion_record"
    completion_path = tmp_path / "completion-agent_backend.json"
    completion_path.write_text('{"outcome":"completed"}\n', encoding="utf-8")

    assert event_requires_run_dir(event_name)

    with pytest.raises(RuntimeError, match="missing_field=run_dir"):
        validate_event_artifact_expectations(
            event_name,
            {
                "issue_number": 1,
                "completion_path_absolute": str(completion_path),
            },
        )

    with pytest.raises(RuntimeError, match="missing_field=completion_path_absolute"):
        validate_event_artifact_expectations(
            event_name,
            {
                "issue_number": 1,
                "run_dir": str(tmp_path),
            },
        )

    validate_event_artifact_expectations(
        event_name,
        {
            "issue_number": 1,
            "run_dir": str(tmp_path),
            "completion_path_absolute": str(completion_path),
        },
    )


def test_invalid_record_session_failure_uses_same_existing_completion_policy(
    tmp_path,
) -> None:
    payload = {
        "issue_number": 1,
        "run_dir": str(tmp_path),
        "failure_kind": "invalid_completion_record",
    }

    validate_event_artifact_expectations("session.failed", payload)

    payload["completion_path_absolute"] = str(tmp_path / "completion.json")

    with pytest.raises(RuntimeError, match="missing_completion_record"):
        validate_event_artifact_expectations("session.failed", payload)

    (tmp_path / "completion.json").write_text("{}\n", encoding="utf-8")
    validate_event_artifact_expectations("session.failed", payload)


def test_start_completion_path_is_an_expected_destination_not_existing_artifact(
    tmp_path,
) -> None:
    (tmp_path / "ui-session.log").write_text("session output\n", encoding="utf-8")
    future_completion = tmp_path / "completion-future.json"

    validate_event_artifact_expectations(
        "session.started",
        {
            "issue_number": 1,
            "run_dir": str(tmp_path),
            "completion_path_absolute": str(future_completion),
        },
    )

    assert not future_completion.exists()
