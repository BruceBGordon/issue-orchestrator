"""Closed-state tests for worktree-scoped Timeline action capabilities."""

from pathlib import Path

import pytest

from issue_orchestrator.execution.timeline_action_capabilities import (
    AvailableRunArtifacts,
    MissingRunArtifacts,
    TimelineLocalArtifactKind,
    TimelineUrlArtifactKind,
    UnscopedTimelineEvent,
    classify_timeline_run_artifacts,
    require_existing_timeline_artifact,
    review_feedback_event_name,
    timeline_local_artifact_kind,
    timeline_url_artifact_kind,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
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


def test_run_directory_with_surrounding_whitespace_is_rejected(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(RuntimeError, match="invalid run_dir"):
        classify_timeline_run_artifacts(
            raw_run_dir=f"{run_dir} ",
            issue_number=42,
            event_name=EventName.SESSION_STARTED.value,
        )


def test_non_directory_run_parent_is_malformed_not_missing(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("file\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-directory parent"):
        classify_timeline_run_artifacts(
            raw_run_dir=str(parent_file / "run"),
            issue_number=42,
            event_name=EventName.SESSION_STARTED.value,
        )


def test_local_artifact_kind_is_closed() -> None:
    assert (
        timeline_local_artifact_kind("completion_record")
        is TimelineLocalArtifactKind.COMPLETION_RECORD
    )
    assert timeline_local_artifact_kind("future_unknown_artifact") is None
    assert (
        timeline_url_artifact_kind("pull_request")
        is TimelineUrlArtifactKind.PULL_REQUEST
    )
    assert timeline_url_artifact_kind("completion_record") is None


def test_local_artifact_type_must_match_kind(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    directory_claimed_as_file = tmp_path / "completion-record"
    directory_claimed_as_file.mkdir()

    with pytest.raises(RuntimeError, match="local artifact has wrong type"):
        require_existing_timeline_artifact(
            run_artifacts=AvailableRunArtifacts(run_dir=run_dir),
            artifact_path=directory_claimed_as_file,
            artifact_kind=TimelineLocalArtifactKind.COMPLETION_RECORD,
            issue_number=42,
        )


def test_relative_local_artifact_path_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="artifact path is not absolute"):
        require_existing_timeline_artifact(
            run_artifacts=AvailableRunArtifacts(run_dir=Path("/tmp/run")),
            artifact_path=Path("relative/completion-record.json"),
            artifact_kind=TimelineLocalArtifactKind.COMPLETION_RECORD,
            issue_number=42,
        )


def test_run_directory_artifact_must_equal_selected_run(tmp_path: Path) -> None:
    selected_run = tmp_path / "selected-run"
    claimed_run = tmp_path / "claimed-run"
    selected_run.mkdir()
    claimed_run.mkdir()

    with pytest.raises(RuntimeError, match="does not belong to selected run"):
        require_existing_timeline_artifact(
            run_artifacts=AvailableRunArtifacts(run_dir=selected_run),
            artifact_path=claimed_run,
            artifact_kind=TimelineLocalArtifactKind.RUN_DIR,
            issue_number=42,
        )


def test_run_local_artifact_cannot_point_into_another_run(tmp_path: Path) -> None:
    selected_run = tmp_path / "selected-run"
    other_run = tmp_path / "other-run"
    selected_run.mkdir()
    other_run.mkdir()
    other_prompt = other_run / "prompt.txt"
    other_prompt.write_text("wrong run\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="escapes selected run"):
        require_existing_timeline_artifact(
            run_artifacts=AvailableRunArtifacts(run_dir=selected_run),
            artifact_path=other_prompt,
            artifact_kind=TimelineLocalArtifactKind.PROMPT,
            issue_number=42,
        )


def test_symlink_cannot_escape_selected_run(tmp_path: Path) -> None:
    selected_run = tmp_path / "selected-run"
    selected_run.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    escaped_link = selected_run / "prompt.txt"
    escaped_link.symlink_to(outside)

    with pytest.raises(RuntimeError, match="escapes selected run"):
        require_existing_timeline_artifact(
            run_artifacts=AvailableRunArtifacts(run_dir=selected_run),
            artifact_path=escaped_link,
            artifact_kind=TimelineLocalArtifactKind.PROMPT,
            issue_number=42,
        )


def test_run_local_artifact_must_match_selected_run(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    session_output = FileSystemSessionOutput()
    selected_run = session_output.start_run(worktree, "selected", issue_number=42)
    other_run = session_output.start_run(worktree, "other", issue_number=42)
    selected_completion = selected_run.run_dir / "completion-record.json"
    other_completion = other_run.run_dir / "completion-record.json"
    selected_completion.write_text('{"outcome":"completed"}\n', encoding="utf-8")
    other_completion.write_text('{"outcome":"completed"}\n', encoding="utf-8")
    session_output.update_manifest(
        selected_run.run_dir,
        {"completion_path": str(selected_completion)},
    )
    session_output.update_manifest(
        other_run.run_dir,
        {"completion_path": str(other_completion)},
    )

    with pytest.raises(RuntimeError, match="escapes selected run"):
        require_existing_timeline_artifact(
            run_artifacts=AvailableRunArtifacts(run_dir=selected_run.run_dir),
            artifact_path=other_completion,
            artifact_kind=TimelineLocalArtifactKind.COMPLETION_RECORD,
            issue_number=42,
        )


def test_selected_run_can_own_multiple_completion_artifacts(tmp_path: Path) -> None:
    selected_run = tmp_path / "selected-run"
    selected_run.mkdir()
    launch_completion = selected_run / "completion-agent.json"
    copied_completion = selected_run / "completion-record.json"
    launch_completion.write_text('{"outcome":"completed"}\n', encoding="utf-8")
    copied_completion.write_text('{"outcome":"completed"}\n', encoding="utf-8")

    assert require_existing_timeline_artifact(
        run_artifacts=AvailableRunArtifacts(run_dir=selected_run),
        artifact_path=launch_completion,
        artifact_kind=TimelineLocalArtifactKind.COMPLETION_RECORD,
        issue_number=42,
    ) == launch_completion
    assert require_existing_timeline_artifact(
        run_artifacts=AvailableRunArtifacts(run_dir=selected_run),
        artifact_path=copied_completion,
        artifact_kind=TimelineLocalArtifactKind.COMPLETION_RECORD,
        issue_number=42,
    ) == copied_completion


def test_review_feedback_policy_returns_canonical_event_enum() -> None:
    event = review_feedback_event_name(
        EventName.REVIEW_EXCHANGE_ROUND_COMPLETED.value,
        reviewer_response_text="Approved",
    )

    assert event is EventName.REVIEW_EXCHANGE_ROUND_COMPLETED
