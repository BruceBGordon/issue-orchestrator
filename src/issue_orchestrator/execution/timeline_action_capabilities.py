"""Typed filesystem capabilities for worktree-scoped Timeline actions."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias, assert_never
from urllib.parse import urlsplit

from ..events import EventName
from .manifest_accessor import worktree_path_from_run_dir
from .recorded_session_runs import (
    ExactRecordedRun,
    InvalidRecordedRunReference,
    RecordedRunIssueMismatch,
    RecordedRunNotFound,
    RecordedRunUntrusted,
    RecordedRunUnreadable,
    resolve_exact_recorded_run,
)
from .timeline_artifact_expectations import event_requires_run_dir


@dataclass(frozen=True, slots=True)
class AvailableRunArtifacts:
    """A manifest-validated recorded run is available."""

    recorded_run: ExactRecordedRun

    @property
    def run_dir(self) -> Path:
        return self.recorded_run.run_dir


@dataclass(frozen=True, slots=True)
class MissingRunArtifacts:
    """The recorded run directory no longer exists."""

    run_dir: Path


@dataclass(frozen=True, slots=True)
class UnavailableRunArtifacts:
    """The run exists, but its manifest cannot prove exact-run ownership."""

    run_dir: Path
    detail: str


@dataclass(frozen=True, slots=True)
class UnscopedTimelineEvent:
    """This event never required a run directory."""


TimelineRunArtifacts: TypeAlias = (
    AvailableRunArtifacts
    | MissingRunArtifacts
    | UnavailableRunArtifacts
    | UnscopedTimelineEvent
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


class TimelineUrlArtifactKind(StrEnum):
    """Artifact kinds whose value is an external URL, never a local path."""

    PULL_REQUEST = "pull_request"
    REVIEW_COMMENT = "review_comment"


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
    if (
        not isinstance(raw_run_dir, str)
        or not raw_run_dir.strip()
        or raw_run_dir != raw_run_dir.strip()
    ):
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
    except FileNotFoundError:
        return MissingRunArtifacts(run_dir=run_dir)
    except NotADirectoryError as exc:
        raise RuntimeError(
            "timeline event run_dir has a non-directory parent: "
            f"issue={issue_number} event={event_name} run_dir={run_dir}"
        ) from exc
    if not stat.S_ISDIR(mode):
        raise RuntimeError(
            "timeline event run_dir is not a directory: "
            f"issue={issue_number} event={event_name} run_dir={run_dir}"
        )
    return _validated_available_run(
        run_dir=run_dir,
        issue_number=issue_number,
        event_name=event_name,
    )


def _validated_available_run(
    *,
    run_dir: Path,
    issue_number: int,
    event_name: str,
) -> AvailableRunArtifacts | UnavailableRunArtifacts:
    """Convert exact-run lookup results into a closed capability state."""
    exact_run = resolve_exact_recorded_run(
        str(run_dir),
        issue_number=issue_number,
    )
    match exact_run:
        case ExactRecordedRun():
            return AvailableRunArtifacts(recorded_run=exact_run)
        case RecordedRunNotFound(detail=detail):
            return UnavailableRunArtifacts(
                run_dir=run_dir,
                detail=(
                    "Recorded session evidence has no readable manifest: "
                    f"issue={issue_number} event={event_name}; {detail}"
                ),
            )
        case RecordedRunIssueMismatch(actual_issue_number=actual):
            raise RuntimeError(
                "timeline event run_dir belongs to another issue: "
                f"issue={issue_number} actual_issue={actual} "
                f"event={event_name} run_dir={run_dir}"
            )
        case InvalidRecordedRunReference(detail=detail):
            raise RuntimeError(
                "timeline event has an untrusted run_dir: "
                f"issue={issue_number} event={event_name} run_dir={run_dir}; {detail}"
            )
        case RecordedRunUntrusted(detail=detail):
            raise RuntimeError(
                "timeline event has an untrusted run_dir: "
                f"issue={issue_number} event={event_name} run_dir={run_dir}; {detail}"
            )
        case RecordedRunUnreadable(detail=detail):
            return UnavailableRunArtifacts(
                run_dir=run_dir,
                detail=(
                    "Recorded session evidence is unreadable: "
                    f"issue={issue_number} event={event_name}; {detail}"
                ),
            )
        case _:
            assert_never(exact_run)


def available_run_artifacts(
    run_artifacts: TimelineRunArtifacts,
) -> AvailableRunArtifacts | None:
    match run_artifacts:
        case AvailableRunArtifacts():
            return run_artifacts
        case (
            MissingRunArtifacts() | UnavailableRunArtifacts() | UnscopedTimelineEvent()
        ):
            return None
        case _:
            assert_never(run_artifacts)


def unavailable_run_artifacts_detail(
    run_artifacts: TimelineRunArtifacts,
) -> str | None:
    """Return the explicit inspection failure for an existing unusable run."""
    match run_artifacts:
        case UnavailableRunArtifacts(detail=detail):
            return detail
        case AvailableRunArtifacts() | MissingRunArtifacts() | UnscopedTimelineEvent():
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


def timeline_url_artifact_kind(value: str) -> TimelineUrlArtifactKind | None:
    """Parse a wire artifact name into the external-URL vocabulary."""
    try:
        return TimelineUrlArtifactKind(value)
    except ValueError:
        return None


def require_external_timeline_url(
    *,
    value: str,
    artifact_kind: TimelineUrlArtifactKind,
    issue_number: int,
) -> str:
    """Return a canonical HTTP(S) URL or reject the typed URL claim."""
    if value != value.strip() or not value:
        raise RuntimeError(
            "timeline event has invalid external artifact URL: "
            f"issue={issue_number} type={artifact_kind.value}"
        )
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(
            "timeline event external artifact is not an HTTP(S) URL: "
            f"issue={issue_number} type={artifact_kind.value} value={value}"
        )
    return value


def require_existing_timeline_artifact(
    *,
    run_artifacts: AvailableRunArtifacts,
    artifact_path: Path,
    artifact_kind: TimelineLocalArtifactKind,
    issue_number: int,
) -> Path:
    """Return the canonical path only when the selected run owns the artifact."""
    resolved_artifact = _resolve_existing_artifact(
        artifact_path,
        artifact_kind=artifact_kind,
        issue_number=issue_number,
    )
    resolved_run_dir = run_artifacts.run_dir.resolve(strict=True)
    _require_artifact_ownership(
        resolved_artifact,
        run_dir=resolved_run_dir,
        artifact_kind=artifact_kind,
        issue_number=issue_number,
    )
    return resolved_artifact


def _resolve_existing_artifact(
    artifact_path: Path,
    *,
    artifact_kind: TimelineLocalArtifactKind,
    issue_number: int,
) -> Path:
    if not artifact_path.is_absolute():
        raise RuntimeError(
            "timeline event local artifact path is not absolute: "
            f"issue={issue_number} type={artifact_kind.value} path={artifact_path}"
        )
    try:
        resolved_artifact = artifact_path.resolve(strict=True)
        mode = resolved_artifact.stat().st_mode
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
    return resolved_artifact


def _require_artifact_ownership(
    artifact_path: Path,
    *,
    run_dir: Path,
    artifact_kind: TimelineLocalArtifactKind,
    issue_number: int,
) -> None:
    match artifact_kind:
        case TimelineLocalArtifactKind.RUN_DIR:
            expected_path = run_dir
        case TimelineLocalArtifactKind.WORKTREE:
            expected_path = worktree_path_from_run_dir(run_dir)
            if expected_path is None:
                raise RuntimeError(
                    "timeline run directory has no owning worktree boundary: "
                    f"issue={issue_number} run_dir={run_dir}"
                )
        case (
            TimelineLocalArtifactKind.COMPLETION_RECORD
            | TimelineLocalArtifactKind.VALIDATION
            | TimelineLocalArtifactKind.DIAGNOSTIC
            | TimelineLocalArtifactKind.CHAPTER_SIDECAR
            | TimelineLocalArtifactKind.PROMPT
            | TimelineLocalArtifactKind.REVIEW_RESPONSE
        ):
            _require_within_run(
                artifact_path,
                run_dir,
                issue_number=issue_number,
                artifact_kind=artifact_kind,
            )
            return
        case _:
            assert_never(artifact_kind)
    _require_same_path(
        artifact_path,
        expected_path,
        issue_number=issue_number,
        artifact_kind=artifact_kind,
    )


def _require_same_path(
    actual: Path,
    expected: Path,
    *,
    issue_number: int,
    artifact_kind: TimelineLocalArtifactKind,
) -> None:
    if actual != expected:
        raise RuntimeError(
            "timeline event local artifact does not belong to selected run: "
            f"issue={issue_number} type={artifact_kind.value} "
            f"expected={expected} actual={actual}"
        )


def _require_within_run(
    artifact_path: Path,
    run_dir: Path,
    *,
    issue_number: int,
    artifact_kind: TimelineLocalArtifactKind,
) -> None:
    try:
        artifact_path.relative_to(run_dir)
    except ValueError as exc:
        raise RuntimeError(
            "timeline event local artifact escapes selected run: "
            f"issue={issue_number} type={artifact_kind.value} "
            f"run_dir={run_dir} path={artifact_path}"
        ) from exc
