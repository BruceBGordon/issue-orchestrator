"""Event sink port for trace event emission.

This port defines the interface for emitting trace/lifecycle events from the
orchestrator core. The core calls `publish()` without knowing how events
are delivered (pluggy, SSE, IPC, files, metrics, etc.).

This is the key abstraction that keeps pluggy out of the core.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, cast, Literal, NotRequired, overload, Protocol, TYPE_CHECKING, TypedDict

from ..domain.review_artifacts import (
    AbstractionReviewStatus,
    NitPolicy,
    ReviewVerdict,
)

if TYPE_CHECKING:
    from issue_orchestrator.events.catalog import EventName
    RunScopedEventName = Literal[
        EventName.SESSION_STARTED,
        EventName.SESSION_COMPLETED,
        EventName.SESSION_FAILED,
        EventName.SESSION_ARTIFACT_LOOKUP,
        EventName.SESSION_PROCESSING_COMPLETED,
        EventName.SESSION_VALIDATION_PASSED,
        EventName.SESSION_VALIDATION_RETRY_NEEDED,
        EventName.SESSION_VALIDATION_FAILED,
        EventName.SESSION_INVALID_COMPLETION_RECORD,
        EventName.REVIEW_STARTED,
        EventName.REWORK_STARTED,
        EventName.REVIEW_EXCHANGE_STARTED,
        EventName.REVIEW_EXCHANGE_ROUND_STARTED,
        EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
        EventName.REVIEW_EXCHANGE_COMPLETED,
        EventName.REVIEW_EXCHANGE_FAILED,
        EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
        EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
        EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT,
        EventName.REVIEW_EXCHANGE_CHAPTER_RECORDED,
        EventName.REVIEW_REWORK_STARTED,
        EventName.REVIEW_REWORK_COMPLETED,
    ]
else:
    RunScopedEventName = Any


class RunIdentityEventPayload(TypedDict):
    """Fields every issue-scoped run event must carry."""

    issue_number: int
    run_dir: str


class RunScopedEventPayload(RunIdentityEventPayload):
    """Shared optional fields for general run-scoped lifecycle events."""

    session_name: NotRequired[str]
    session_id: NotRequired[str]
    pr_number: NotRequired[int]
    agent: NotRequired[str]
    source_agent: NotRequired[str]
    task: NotRequired[str]
    worktree_path: NotRequired[str]
    branch_name: NotRequired[str]
    run_id: NotRequired[str]
    tick_id: NotRequired[int]
    schema: NotRequired[int]
    completion_path: NotRequired[str]
    completion_path_absolute: NotRequired[str]
    session_prompt_path: NotRequired[str | None]
    lookup_kind: NotRequired[str]
    resolved_run_dir: NotRequired[str]
    run_dir_exists: NotRequired[bool]
    selected_log_path: NotRequired[str | None]
    log_path_exists: NotRequired[bool]
    reason: NotRequired[str]
    validation_source: NotRequired[str]
    validation_error_summary: NotRequired[str]
    validation_reason: NotRequired[str]
    validation_cmd: NotRequired[str | None]
    error_file: NotRequired[str | None]
    retry_count: NotRequired[int]
    max_retries: NotRequired[int]
    success: NotRequired[bool]
    message: NotRequired[str]
    actions_taken: NotRequired[list[str] | None]
    errors: NotRequired[list[str] | None]
    pr_url: NotRequired[str | None]
    prior_pr_number: NotRequired[int | None]
    prior_pr_url: NotRequired[str | None]
    trigger_label: NotRequired[str]
    rework_cycle: NotRequired[int]
    review_exchange_mode: NotRequired[str]
    reset_from_scratch: NotRequired[bool]
    review_cache_boundary_started_at: NotRequired[str]
    review_cache_summary_path: NotRequired[str]
    review_cache_validation_record_path: NotRequired[str]
    review_cache_head_sha: NotRequired[str]
    # True when a review lifecycle event (review.started / review.approved /
    # review.changes_requested) is replayed from a persisted prior-run summary
    # rather than produced by a fresh reviewer in this orchestrator run. The
    # timeline narrative enrichers branch on this flag.
    cached: NotRequired[bool]


class SessionStartedEventPayload(RunScopedEventPayload):
    """Payload for ``session.started`` events."""


class SessionCompletedEventPayload(RunScopedEventPayload):
    """Payload for ``session.completed`` events."""


class SessionFailedEventPayload(RunScopedEventPayload):
    """Payload for ``session.failed`` events."""


class SessionArtifactLookupEventPayload(RunScopedEventPayload):
    """Payload for ``session.artifact_lookup`` events."""


class SessionProcessingCompletedEventPayload(RunScopedEventPayload):
    """Payload for ``session.processing_completed`` events."""


class SessionValidationPassedEventPayload(RunScopedEventPayload):
    """Payload for ``session.validation_passed`` events."""


class SessionValidationRetryNeededEventPayload(RunScopedEventPayload):
    """Payload for ``session.validation_retry_needed`` events."""


class SessionValidationFailedEventPayload(RunScopedEventPayload):
    """Payload for ``session.validation_failed`` events."""


class ReviewExchangeDecisionEventFields(TypedDict):
    """Typed review-decision fields shared by review exchange events."""

    review_decision_verdict: NotRequired[ReviewVerdict]
    review_nit_policy: NotRequired[NitPolicy]
    review_abstraction_status: NotRequired[AbstractionReviewStatus]


class ReviewExchangeRoundCompletedEventPayload(
    RunIdentityEventPayload,
    ReviewExchangeDecisionEventFields,
):
    """Payload for ``review_exchange.round_completed`` events."""

    session_name: str
    round_index: int
    reviewer_response_type: str | None
    reviewer_response_text: str | None
    coder_response_type: str | None
    coder_response_text: NotRequired[str | None]
    artifacts: NotRequired[list[dict[str, str]]]
    detail: NotRequired[str]


class ReviewExchangeCompletedEventPayload(
    RunIdentityEventPayload,
    ReviewExchangeDecisionEventFields,
):
    """Payload for ``review_exchange.completed`` events."""

    session_name: str
    rounds: int
    status: Literal["ok", "stopped", "error"]
    reason: str
    artifacts: NotRequired[list[dict[str, str]]]
    detail: NotRequired[str]


def make_trace_event(
    event_type: "EventName",
    data: dict[str, Any],
) -> "TraceEvent":
    """Build an event while applying the central runtime run-scope policy."""
    if event_type in run_scoped_event_names():
        return make_run_scoped_event(
            cast(RunScopedEventName, event_type),
            cast(RunScopedEventPayload, data),
        )
    return TraceEvent(event_type, dict(data))


@overload
def make_run_scoped_event(
    event_type: Literal[EventName.REVIEW_EXCHANGE_ROUND_COMPLETED],
    data: ReviewExchangeRoundCompletedEventPayload,
) -> "TraceEvent": ...


@overload
def make_run_scoped_event(
    event_type: Literal[EventName.REVIEW_EXCHANGE_COMPLETED],
    data: ReviewExchangeCompletedEventPayload,
) -> "TraceEvent": ...


@overload
def make_run_scoped_event(
    event_type: RunScopedEventName,
    data: RunScopedEventPayload,
) -> "TraceEvent": ...


def make_run_scoped_event(
    event_type: RunScopedEventName,
    data: (
        RunScopedEventPayload
        | ReviewExchangeRoundCompletedEventPayload
        | ReviewExchangeCompletedEventPayload
    ),
) -> "TraceEvent":
    """Build a run-scoped event with typed payload requiring run_dir."""
    return TraceEvent(event_type, dict(data))


def make_session_started_event(data: SessionStartedEventPayload) -> "TraceEvent":
    """Build a typed ``session.started`` event."""
    from issue_orchestrator.events import EventName

    return make_run_scoped_event(EventName.SESSION_STARTED, data)


def make_session_completed_event(
    data: SessionCompletedEventPayload,
) -> "TraceEvent":
    """Build a typed ``session.completed`` event."""
    from issue_orchestrator.events import EventName

    return make_run_scoped_event(EventName.SESSION_COMPLETED, data)


def make_session_failed_event(data: SessionFailedEventPayload) -> "TraceEvent":
    """Build a typed ``session.failed`` event."""
    from issue_orchestrator.events import EventName

    return make_run_scoped_event(EventName.SESSION_FAILED, data)


def make_session_artifact_lookup_event(data: SessionArtifactLookupEventPayload) -> "TraceEvent":
    """Build a typed ``session.artifact_lookup`` event."""
    from issue_orchestrator.events import EventName

    return make_run_scoped_event(EventName.SESSION_ARTIFACT_LOOKUP, data)


def make_session_processing_completed_event(data: SessionProcessingCompletedEventPayload) -> "TraceEvent":
    """Build a typed ``session.processing_completed`` event."""
    from issue_orchestrator.events import EventName

    return make_run_scoped_event(EventName.SESSION_PROCESSING_COMPLETED, data)


def make_session_validation_passed_event(data: SessionValidationPassedEventPayload) -> "TraceEvent":
    """Build a typed ``session.validation_passed`` event."""
    from issue_orchestrator.events import EventName

    return make_run_scoped_event(EventName.SESSION_VALIDATION_PASSED, data)


def make_session_validation_retry_needed_event(data: SessionValidationRetryNeededEventPayload) -> "TraceEvent":
    """Build a typed ``session.validation_retry_needed`` event."""
    from issue_orchestrator.events import EventName

    return make_run_scoped_event(EventName.SESSION_VALIDATION_RETRY_NEEDED, data)


def make_session_validation_failed_event(data: SessionValidationFailedEventPayload) -> "TraceEvent":
    """Build a typed ``session.validation_failed`` event."""
    from issue_orchestrator.events import EventName

    return make_run_scoped_event(EventName.SESSION_VALIDATION_FAILED, data)


def make_review_exchange_round_completed_event(
    data: ReviewExchangeRoundCompletedEventPayload,
) -> "TraceEvent":
    """Build a typed ``review_exchange.round_completed`` event."""
    from issue_orchestrator.events import EventName

    return make_run_scoped_event(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, data)


@lru_cache(maxsize=1)
def run_scoped_event_names() -> frozenset["EventName"]:
    """Return the single event-name policy that requires an exact run_dir."""
    from issue_orchestrator.events import EventName

    return frozenset(
        {
            EventName.SESSION_STARTED,
            EventName.SESSION_COMPLETED,
            EventName.SESSION_FAILED,
            EventName.SESSION_ARTIFACT_LOOKUP,
            EventName.SESSION_PROCESSING_COMPLETED,
            EventName.SESSION_VALIDATION_PASSED,
            EventName.SESSION_VALIDATION_RETRY_NEEDED,
            EventName.SESSION_VALIDATION_FAILED,
            EventName.SESSION_INVALID_COMPLETION_RECORD,
            EventName.REVIEW_STARTED,
            EventName.REWORK_STARTED,
            EventName.REVIEW_EXCHANGE_STARTED,
            EventName.REVIEW_EXCHANGE_ROUND_STARTED,
            EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
            EventName.REVIEW_EXCHANGE_COMPLETED,
            EventName.REVIEW_EXCHANGE_FAILED,
            EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
            EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
            EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT,
            EventName.REVIEW_EXCHANGE_CHAPTER_RECORDED,
            EventName.REVIEW_REWORK_STARTED,
            EventName.REVIEW_REWORK_COMPLETED,
        }
    )


def make_review_exchange_completed_event(
    data: ReviewExchangeCompletedEventPayload,
) -> "TraceEvent":
    """Build a typed ``review_exchange.completed`` event."""
    from issue_orchestrator.events import EventName

    return make_run_scoped_event(EventName.REVIEW_EXCHANGE_COMPLETED, data)


@dataclass(frozen=True)
class TraceEvent:
    """A trace event emitted by the orchestrator.

    Trace events are notifications about what happened. They're fire-and-forget
    and must not influence orchestrator behavior.

    The event_type must be an EventName from the catalog - raw strings are not
    accepted. This ensures all events are documented and type-safe.

    Usage:
        from issue_orchestrator.events import EventName
        event = TraceEvent(EventName.TICK_STARTED, {"tick_id": 1})
    """

    event_type: "EventName"
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    event_id: int | None = None

    def __post_init__(self) -> None:
        """Validate strict event invariants at construction time."""
        if self.event_type not in run_scoped_event_names():
            return
        # Some non-issue-scoped helpers emit generic session events without issue_number.
        # Enforce run_dir only for issue-scoped timeline events.
        if not isinstance(self.data.get("issue_number"), int):
            return
        run_dir = self.data.get("run_dir")
        if not isinstance(run_dir, str) or not run_dir:
            raise ValueError(
                f"{self.name} requires non-empty run_dir in event data"
            )

    @property
    def name(self) -> str:
        """Get the event name string for serialization."""
        return str(self.event_type)

    def with_event_id(self, event_id: int) -> "TraceEvent":
        """Return a copy of this event with an assigned event_id."""
        return TraceEvent(
            event_type=self.event_type,
            data=dict(self.data),
            timestamp=self.timestamp,
            event_id=event_id,
        )


class EventSink(Protocol):
    """Port for emitting trace events.

    Implementations may fan out to multiple sinks (SSE, IPC, logging, metrics)
    but the orchestrator doesn't know or care about that.

    Contract:
        - publish() must not raise exceptions (fire-and-forget)
        - publish() must not block the caller
        - publish() must be thread-safe — the review-exchange background
          worker and the main tick can both emit concurrently
        - Events may be dropped if sinks are unavailable
    """

    def publish(self, event: TraceEvent) -> None:
        """Emit a trace event. Must not raise."""
        ...


class NullEventSink:
    """No-op event sink for testing or when events aren't needed."""

    def publish(self, event: TraceEvent) -> None:
        """Silently drop all events."""
        pass


class InMemoryEventSink:
    """Event sink that collects events in memory for testing.

    Provides methods to query and wait for specific events, enabling
    deterministic test synchronization without sleeps or timeouts.

    Usage:
        sink = InMemoryEventSink()
        orchestrator = Orchestrator(..., events=sink)

        # After some operation
        assert sink.has_event("tick.started")
        events = sink.get_events("session.completed")
        sink.clear()
    """

    def __init__(self) -> None:
        self._events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        """Store the event for later inspection."""
        self._events.append(event)

    @property
    def events(self) -> list[TraceEvent]:
        """Get all collected events."""
        return list(self._events)

    def get_events(self, name: str) -> list[TraceEvent]:
        """Get all events with the given name."""
        return [e for e in self._events if e.name == name]

    def has_event(self, name: str) -> bool:
        """Check if an event with the given name was published."""
        return any(e.name == name for e in self._events)

    def last_event(self, name: str) -> TraceEvent | None:
        """Get the most recent event with the given name."""
        for e in reversed(self._events):
            if e.name == name:
                return e
        return None

    def event_names(self) -> list[str]:
        """Get list of all event names in order."""
        return [e.name for e in self._events]

    def clear(self) -> None:
        """Clear all collected events."""
        self._events.clear()

    def __len__(self) -> int:
        """Return number of collected events."""
        return len(self._events)
