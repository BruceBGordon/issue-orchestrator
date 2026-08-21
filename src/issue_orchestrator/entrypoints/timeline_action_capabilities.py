"""Typed capability policy for worktree-scoped Timeline actions."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias, assert_never

from ..events import EventName
from ..execution.timeline_artifact_expectations import event_requires_run_dir


@dataclass(frozen=True, slots=True)
class AvailableRunArtifacts:
    """The recorded run directory still exists."""

    run_dir: Path


@dataclass(frozen=True, slots=True)
class MissingRunArtifacts:
    """The recorded run directory no longer exists."""

    run_dir: Path


@dataclass(frozen=True, slots=True)
class UnscopedTimelineEvent:
    """This event never required a run directory."""


TimelineRunArtifacts: TypeAlias = (
    AvailableRunArtifacts | MissingRunArtifacts | UnscopedTimelineEvent
)


class TimelineLocalArtifactKind(StrEnum):
    CHAPTER_SIDECAR = "chapter_sidecar"
    COMPLETION_RECORD = "completion_record"
    DIAGNOSTIC = "diagnostic"
    PROMPT = "prompt"
    REVIEW_RESPONSE = "review_response"
    RUN_DIR = "run_dir"
    VALIDATION = "validation"
    WORKTREE = "worktree"


_REVIEW_FEEDBACK_EVENTS = frozenset(
    {
        EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
        EventName.REVIEW_APPROVED,
        EventName.REVIEW_CHANGES_REQUESTED,
        EventName.REVIEW_COMMENT_ADDED,
    }
)

_DIRECTORY_ARTIFACTS = frozenset(
    {
        TimelineLocalArtifactKind.RUN_DIR,
        TimelineLocalArtifactKind.WORKTREE,
    }
)


def classify_timeline_run_artifacts(
    *,
    raw_run_dir: object,
    issue_number: int,
    event_name: str,
) -> TimelineRunArtifacts:
    """Parse a raw Timeline run reference into a closed capability state."""
    if raw_run_dir is None:
        if event_requires_run_dir(event_name):
            raise RuntimeError(
                "timeline event missing required run_dir: "
                f"issue={issue_number} event={event_name}"
            )
        return UnscopedTimelineEvent()
    if not isinstance(raw_run_dir, str) or not raw_run_dir.strip():
        raise RuntimeError(
            "timeline event has invalid run_dir: "
            f"issue={issue_number} event={event_name}"
        )

    run_dir = Path(raw_run_dir)
    if not run_dir.is_absolute():
        raise RuntimeError(
            "timeline event run_dir is not absolute: "
            f"issue={issue_number} event={event_name} run_dir={run_dir}"
        )
    try:
        mode = run_dir.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return MissingRunArtifacts(run_dir=run_dir)
    if not stat.S_ISDIR(mode):
        raise RuntimeError(
            "timeline event run_dir is not a directory: "
            f"issue={issue_number} event={event_name} run_dir={run_dir}"
        )
    return AvailableRunArtifacts(run_dir=run_dir)


def available_run_artifacts(
    run_artifacts: TimelineRunArtifacts,
) -> AvailableRunArtifacts | None:
    match run_artifacts:
        case AvailableRunArtifacts():
            return run_artifacts
        case MissingRunArtifacts() | UnscopedTimelineEvent():
            return None
        case _:
            assert_never(run_artifacts)


def review_feedback_event_name(
    event_name: str,
    *,
    reviewer_response_text: object,
) -> EventName | None:
    """Classify rows that own durable review feedback."""
    try:
        feedback_event = EventName(event_name)
    except ValueError:
        return None
    if feedback_event not in _REVIEW_FEEDBACK_EVENTS:
        return None
    if feedback_event is EventName.REVIEW_EXCHANGE_ROUND_COMPLETED:
        if not isinstance(reviewer_response_text, str):
            return None
        return feedback_event if reviewer_response_text.strip() else None
    return feedback_event


def timeline_local_artifact_kind(value: str) -> TimelineLocalArtifactKind | None:
    """Parse a wire artifact name into the local-path action vocabulary."""
    try:
        return TimelineLocalArtifactKind(value)
    except ValueError:
        return None


def require_existing_timeline_artifact(
    *,
    artifact_path: Path,
    artifact_kind: TimelineLocalArtifactKind,
    issue_number: int,
) -> None:
    """Reject a local artifact claim that contradicts an available run."""
    if not artifact_path.is_absolute():
        raise RuntimeError(
            "timeline event local artifact path is not absolute: "
            f"issue={issue_number} type={artifact_kind.value} path={artifact_path}"
        )
    try:
        mode = artifact_path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise RuntimeError(
            "timeline event references missing local artifact: "
            f"issue={issue_number} type={artifact_kind.value} path={artifact_path}"
        ) from exc

    is_expected_type = (
        stat.S_ISDIR(mode)
        if artifact_kind in _DIRECTORY_ARTIFACTS
        else stat.S_ISREG(mode)
    )
    if not is_expected_type:
        expected = "directory" if artifact_kind in _DIRECTORY_ARTIFACTS else "file"
        raise RuntimeError(
            "timeline event local artifact has wrong type: "
            f"issue={issue_number} type={artifact_kind.value} "
            f"expected={expected} path={artifact_path}"
        )
