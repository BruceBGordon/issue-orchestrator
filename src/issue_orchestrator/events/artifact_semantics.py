"""Artifact existence semantics owned by the event vocabulary."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum, auto
from typing import Any

from .catalog import EventName


class CompletionPathSemantics(Enum):
    """What an event asserts about ``completion_path_absolute``."""

    NONE = auto()
    EXPECTED_DESTINATION = auto()
    OPTIONAL_EXISTING_ARTIFACT = auto()
    REQUIRED_EXISTING_ARTIFACT = auto()


_EXPECTED_COMPLETION_DESTINATION_EVENTS = frozenset(
    {
        EventName.SESSION_STARTED,
        EventName.REVIEW_STARTED,
        EventName.REWORK_STARTED,
    }
)

_EXISTING_COMPLETION_ARTIFACT_EVENTS = frozenset(
    {
        EventName.SESSION_COMPLETED,
        EventName.SESSION_INVALID_COMPLETION_RECORD,
    }
)


def completion_path_semantics(
    event_name: str,
    payload: Mapping[str, Any],
) -> CompletionPathSemantics:
    """Classify completion-path existence without duplicating event policy."""
    try:
        event = EventName(event_name)
    except ValueError:
        return CompletionPathSemantics.NONE
    if event in _EXPECTED_COMPLETION_DESTINATION_EVENTS:
        return CompletionPathSemantics.EXPECTED_DESTINATION
    if event in _EXISTING_COMPLETION_ARTIFACT_EVENTS:
        return CompletionPathSemantics.REQUIRED_EXISTING_ARTIFACT
    if (
        event is EventName.SESSION_FAILED
        and payload.get("failure_kind") == "invalid_completion_record"
    ):
        return CompletionPathSemantics.OPTIONAL_EXISTING_ARTIFACT
    return CompletionPathSemantics.NONE
