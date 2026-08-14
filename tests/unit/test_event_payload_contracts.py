"""Contract tests for event payload shapes used by the UI."""

from __future__ import annotations

from unittest.mock import MagicMock

from issue_orchestrator.control.github_workflow import GitHubWorkflow
from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import ReconcileHistoryEntryAction
from issue_orchestrator.control.review_exchange_lifecycle import (
    ReviewExchangeCancellation,
)
from issue_orchestrator.control.orchestrator_support import (
    emit_queue_changes,
    track_stale_ticks,
)
from issue_orchestrator.control.stale_detection import detect_stale_in_progress
from issue_orchestrator.control.session_history import SessionHistoryOwner
from issue_orchestrator.contracts.public import (
    DependencyBlockedPayload,
    DependencyUnblockedPayload,
    HistoryReconciledPayload,
    PersistentStalePayload,
    QueueChangedPayload,
    StaleClearedPayload,
    StaleDetectedPayload,
)
from issue_orchestrator.domain.models import Issue, OrchestratorState, SessionHistoryEntry
from issue_orchestrator.events import EventContext, EventName
from issue_orchestrator.infra.config import Config
from tests.conftest import MockEventSink


def _no_review_exchange_work(
    issue_number: int, _reason: str
) -> ReviewExchangeCancellation:
    return ReviewExchangeCancellation(issue_number, ())


def test_queue_changed_event_payload_shape():
    events = MockEventSink()
    state = OrchestratorState(
        cached_queue_issues=[Issue(number=1, title="Old", labels=[])],
    )
    new_queue = [
        Issue(number=1, title="Old", labels=[]),
        Issue(number=2, title="New", labels=[]),
    ]

    emit_queue_changes(events, state, new_queue)

    matches = events.get_events_by_name(EventName.QUEUE_CHANGED)
    assert len(matches) == 1
    payload = matches[0].data
    assert {"added", "removed", "total"}.issubset(payload.keys())
    assert payload["total"] == 2
    assert payload["added"] == [{"number": 2, "title": "New", "issue_key": "2"}]
    assert payload["removed"] == []
    QueueChangedPayload.model_validate(payload)


def test_dependency_blocked_event_payload_shape():
    events = MockEventSink()
    workflow = GitHubWorkflow(
        config=Config(),
        events=events,
        repository_host=MagicMock(),
        fact_gatherer=MagicMock(),
        pr_scanner=MagicMock(),
        label_sync=None,
        event_context=EventContext(tick_id=7),
    )
    state = OrchestratorState()

    workflow.update_dependency_problems(
        state,
        [(Issue(number=5, title="Blocked", labels=[]), "Depends on #1")],
    )

    matches = events.get_events_by_name(EventName.DEPENDENCY_BLOCKED)
    assert len(matches) == 1
    payload = matches[0].data
    assert payload["issue_number"] == 5
    assert payload["summary"] == "Depends on #1"
    assert {"schema", "run_id", "tick_id"}.issubset(payload.keys())
    DependencyBlockedPayload.model_validate(payload)


def test_dependency_unblocked_event_payload_shape():
    events = MockEventSink()
    workflow = GitHubWorkflow(
        config=Config(),
        events=events,
        repository_host=MagicMock(),
        fact_gatherer=MagicMock(),
        pr_scanner=MagicMock(),
        label_sync=None,
        event_context=EventContext(tick_id=3),
    )
    state = OrchestratorState()
    workflow.update_dependency_problems(
        state,
        [(Issue(number=6, title="Blocked", labels=[]), "Depends on #2")],
    )
    events.clear()

    workflow.update_dependency_problems(state, [])

    matches = events.get_events_by_name(EventName.DEPENDENCY_UNBLOCKED)
    assert len(matches) == 1
    payload = matches[0].data
    assert payload["issue_number"] == 6
    assert {"schema", "run_id", "tick_id"}.issubset(payload.keys())
    DependencyUnblockedPayload.model_validate(payload)


def test_stale_in_progress_events_payload_shape():
    events = MockEventSink()
    context = EventContext(tick_id=2)
    state = OrchestratorState(
        cached_queue_issues=[Issue(number=7, title="Stale", labels=["in-progress"])],
    )

    class _Observer:
        def detect_stale_in_progress(self, cached_queue_issues, active_sessions):
            return cached_queue_issues

    stale = detect_stale_in_progress(_Observer(), state, events, context)
    assert stale
    detected = events.get_events_by_name(EventName.STALE_IN_PROGRESS_DETECTED)
    assert len(detected) == 1
    payload = detected[0].data
    assert payload["issue_number"] == 7
    assert payload["labels"] == ["in-progress"]
    assert {"schema", "run_id", "tick_id"}.issubset(payload.keys())
    StaleDetectedPayload.model_validate(payload)

    events.clear()
    state.stale_issue_ticks = {7: 2}
    config = Config()
    track_stale_ticks(config, events, context, state, stale_issues=[])

    cleared = events.get_events_by_name(EventName.STALE_IN_PROGRESS_CLEARED)
    assert len(cleared) == 1
    cleared_payload = cleared[0].data
    assert cleared_payload["issue_number"] == 7
    assert {"schema", "run_id", "tick_id"}.issubset(cleared_payload.keys())
    StaleClearedPayload.model_validate(cleared_payload)


def test_stale_persistent_detected_payload_shape():
    events = MockEventSink()
    context = EventContext(tick_id=5)
    state = OrchestratorState(
        stale_issue_ticks={7: 1},
    )
    config = Config()
    config.stale_escalation_ticks = 2
    stale_issues = [Issue(number=7, title="Stale", labels=["in-progress"])]

    track_stale_ticks(config, events, context, state, stale_issues=stale_issues)

    persistent = events.get_events_by_name(EventName.PERSISTENT_STALE_DETECTED)
    assert len(persistent) == 1
    payload = persistent[0].data
    assert payload["issue_number"] == 7
    assert payload["consecutive_ticks"] == 2
    assert payload["threshold"] == 2
    assert {"schema", "run_id", "tick_id"}.issubset(payload.keys())
    PersistentStalePayload.model_validate(payload)


def test_history_reconciled_event_payload_shape():
    events = MockEventSink()
    entry = SessionHistoryEntry(
        issue_number=228,
        title="Shared cache read misses",
        agent_type="agent:backend",
        status="completed",
        runtime_minutes=0,
        pr_url="https://github.com/owner/repo/pull/318",
        status_reason="Recovered awaiting merge state on startup",
    )
    applier = ActionApplier(
        labels=MagicMock(),
        sessions=MagicMock(),
        events=events,
        history_owner=SessionHistoryOwner([entry]),
        review_exchange_canceller=_no_review_exchange_work,
    )

    result = applier.apply(
        ReconcileHistoryEntryAction(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/owner/repo/pull/318",
            status="merged",
            source="pull_request",
            issue_key="M1-228",
            reason="PR merged; awaiting merge reconciled",
        )
    )
    assert result.success

    matches = events.get_events_by_name(EventName.HISTORY_RECONCILED)
    assert len(matches) == 1
    payload = matches[0].data
    assert payload["issue_number"] == 228
    assert payload["issue_key"] == "M1-228"
    assert payload["pr_number"] == 318
    assert payload["previous_status"] == "completed"
    assert payload["status"] == "merged"
    assert payload["source"] == "pull_request"
    HistoryReconciledPayload.model_validate(payload)
