"""Closed-state tests for worktree-scoped Timeline action capabilities."""

from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.timeline_action_capabilities import (
    AvailableRunArtifacts,
    MissingRunArtifacts,
    TimelineLocalArtifactKind,
    UnscopedTimelineEvent,
    classify_timeline_run_artifacts,
    require_existing_timeline_artifact,
    review_feedback_event_name,
    timeline_local_artifact_kind,
)
from issue_orchestrator.events import EventName


def test_required_event_without_run_dir_fails_fast() -> None:
    with pytest.raises(RuntimeError, match="missing required run_dir"):
        classify_timeline_run_artifacts(
            raw_run_dir=None,
            issue_number=42,
            event_name=EventName.SESSION_STARTED.value,
        )


def test_unscoped_event_has_no_run_capability() -> None:
    state = classify_timeline_run_artifacts(
        raw_run_dir=None,
        issue_number=42,
        event_name=EventName.ISSUE_UNBLOCKED.value,
    )

    assert isinstance(state, UnscopedTimelineEvent)


def test_deleted_run_has_explicit_missing_capability(tmp_path: Path) -> None:
    deleted_run = tmp_path / "deleted-run"

    state = classify_timeline_run_artifacts(
        raw_run_dir=str(deleted_run),
        issue_number=42,
        event_name=EventName.SESSION_STARTED.value,
    )

    assert state == MissingRunArtifacts(run_dir=deleted_run)


def test_existing_run_has_available_capability(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    state = classify_timeline_run_artifacts(
        raw_run_dir=str(run_dir),
        issue_number=42,
        event_name=EventName.SESSION_STARTED.value,
    )

    assert state == AvailableRunArtifacts(run_dir=run_dir)


def test_file_cannot_masquerade_as_run_directory(tmp_path: Path) -> None:
    run_file = tmp_path / "not-a-run-directory"
    run_file.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="run_dir is not a directory"):
        classify_timeline_run_artifacts(
            raw_run_dir=str(run_file),
            issue_number=42,
            event_name=EventName.SESSION_STARTED.value,
        )


def test_relative_run_directory_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="run_dir is not absolute"):
        classify_timeline_run_artifacts(
            raw_run_dir="relative/run",
            issue_number=42,
            event_name=EventName.SESSION_STARTED.value,
        )


def test_local_artifact_kind_is_closed() -> None:
    assert (
        timeline_local_artifact_kind("completion_record")
        is TimelineLocalArtifactKind.COMPLETION_RECORD
    )
    assert timeline_local_artifact_kind("future_unknown_artifact") is None


def test_local_artifact_type_must_match_kind(tmp_path: Path) -> None:
    directory_claimed_as_file = tmp_path / "completion-record"
    directory_claimed_as_file.mkdir()

    with pytest.raises(RuntimeError, match="local artifact has wrong type"):
        require_existing_timeline_artifact(
            artifact_path=directory_claimed_as_file,
            artifact_kind=TimelineLocalArtifactKind.COMPLETION_RECORD,
            issue_number=42,
        )


def test_relative_local_artifact_path_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="artifact path is not absolute"):
        require_existing_timeline_artifact(
            artifact_path=Path("relative/completion-record.json"),
            artifact_kind=TimelineLocalArtifactKind.COMPLETION_RECORD,
            issue_number=42,
        )


def test_review_feedback_policy_returns_canonical_event_enum() -> None:
    event = review_feedback_event_name(
        EventName.REVIEW_EXCHANGE_ROUND_COMPLETED.value,
        reviewer_response_text="Approved",
    )

    assert event is EventName.REVIEW_EXCHANGE_ROUND_COMPLETED
