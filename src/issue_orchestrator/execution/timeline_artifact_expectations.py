"""State/event-based artifact expectations for timeline records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..events import EventName
from ..events.artifact_semantics import (
    CompletionPathSemantics,
    completion_path_semantics,
)
from ..ports.event_sink import run_scoped_event_names

_REVIEW_PHASE_LOG_EVENTS = frozenset({
    EventName.REVIEW_EXCHANGE_ROUND_STARTED,
    EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
    EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
    EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
    EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT,
    EventName.REVIEW_REWORK_STARTED,
    EventName.REVIEW_REWORK_COMPLETED,
})

_RUN_SCOPED_EVENTS = run_scoped_event_names()

# SQLite and persisted Timeline payloads use strings. Derive those wire values
# from the typed policy set rather than maintaining a second string taxonomy.
REVIEW_PHASE_LOG_TIMELINE_EVENTS: frozenset[str] = frozenset(
    event.value for event in _REVIEW_PHASE_LOG_EVENTS
)
RUN_SCOPED_TIMELINE_EVENTS: frozenset[str] = frozenset(
    event.value for event in _RUN_SCOPED_EVENTS
)

def event_requires_run_dir(event_name: str) -> bool:
    """Return True if this event must carry run_dir."""
    event = _known_event_name(event_name)
    return event in _RUN_SCOPED_EVENTS if event is not None else False


def validate_event_artifact_expectations(event_name: str, payload: dict[str, Any]) -> None:
    """Validate required artifacts for key timeline events.

    This enforces workflow-level invariants. Resolver classes (e.g. ManifestAccessor)
    should not decide policy; they only resolve artifacts.
    """
    event = _known_event_name(event_name)
    if event in _RUN_SCOPED_EVENTS:
        _require_non_empty_field(event_name, payload, "run_dir")

    if event in {
        EventName.SESSION_STARTED,
        EventName.REVIEW_STARTED,
        EventName.REWORK_STARTED,
    }:
        _require_run_dir_with_session_artifact(event_name, payload)
        return

    completion_semantics = completion_path_semantics(event_name, payload)
    if completion_semantics is CompletionPathSemantics.REQUIRED_EXISTING_ARTIFACT or (
        completion_semantics is CompletionPathSemantics.OPTIONAL_EXISTING_ARTIFACT
        and "completion_path_absolute" in payload
    ):
        missing_label = (
            "missing_path"
            if event is EventName.SESSION_COMPLETED
            else "missing_completion_record"
        )
        _require_existing_path(
            event_name,
            payload,
            "completion_path_absolute",
            missing_label,
        )
        return

    if event is EventName.REVIEW_COMMENT_ADDED:
        _require_review_feedback_reference(event_name, payload)
        return


def _known_event_name(event_name: str) -> EventName | None:
    try:
        return EventName(event_name)
    except ValueError:
        return None


def _require_run_dir_with_session_artifact(event_name: str, payload: dict[str, Any]) -> None:
    run_dir = _required_path_value(event_name, payload, "run_dir")
    if not run_dir.exists():
        raise RuntimeError(
            f"timeline artifact invariant failed: event={event_name} run_dir_missing={run_dir}"
        )
    candidates = [
        run_dir / "terminal-recording.jsonl",
        run_dir / "ui-session.log",
    ]
    if not any(path.exists() for path in candidates):
        raise RuntimeError(
            "timeline artifact invariant failed: event="
            f"{event_name} session_artifact_missing={','.join(str(path) for path in candidates)}"
        )


def _require_existing_path(
    event_name: str,
    payload: dict[str, Any],
    field: str,
    missing_label: str,
) -> None:
    path = _required_path_value(event_name, payload, field)
    if not path.exists():
        raise RuntimeError(
            "timeline artifact invariant failed: event="
            f"{event_name} {missing_label}={path}"
        )


def _required_path_value(event_name: str, payload: dict[str, Any], field: str) -> Path:
    raw = payload.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError(
            f"timeline artifact invariant failed: event={event_name} missing_field={field}"
        )
    return Path(raw)


def _require_non_empty_field(event_name: str, payload: dict[str, Any], field: str) -> None:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            f"timeline artifact invariant failed: event={event_name} missing_field={field}"
        )


def _require_review_feedback_reference(event_name: str, payload: dict[str, Any]) -> None:
    """Require either a GitHub review URL or in-app feedback text."""
    comment_url = payload.get("comment_url")
    summary = payload.get("summary")
    excerpt = payload.get("comment_excerpt")
    has_url = isinstance(comment_url, str) and bool(comment_url.strip())
    has_text = (
        (isinstance(summary, str) and bool(summary.strip()))
        or (isinstance(excerpt, str) and bool(excerpt.strip()))
    )
    if not has_url and not has_text:
        raise RuntimeError(
            "timeline artifact invariant failed: event="
            f"{event_name} missing_review_feedback_reference"
        )
