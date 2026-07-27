"""Provider outages stay legible in issue history (issue #5980, item 4 / F1).

Covers the real emitter-to-writer path, not a hand-built timeline record:

    ProviderResilienceManager (real circuit)
      -> ProviderAvailabilityPolicy.plan_provider_impact (real policy owner)
        -> ApplyProviderImpactAction
          -> ActionApplier.apply (real applier: label mutation + event)
            -> DefaultTimelineWriter (real writer, drops issue-less events)
              -> build_issue_timeline (real projection)

The regression this pins: the fleet-scoped ``provider.*`` events carry no
``issue_number``, so ``DefaultTimelineWriter`` discarded every one of them and
no provider outage could ever appear in an issue timeline. The blocked label was
the only signal, and it is shed the moment the circuit closes — so the outage
disappeared exactly when an operator would go looking for why work stalled.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import ActionType
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.provider_availability import ProviderAvailabilityPolicy
from issue_orchestrator.control.provider_impact import ProviderImpactTransition
from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.control.planner_types import PlanContext
from issue_orchestrator.domain.models import AgentConfig, Issue
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import InMemoryProviderCircuitStore
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import build_issue_timeline

ISSUE = 5980
PROVIDER = "anthropic"


class _RecordingTimelineStore:
    """Minimal TimelineStore that keeps whatever the real writer stores."""

    instance_id = "test-instance"

    def __init__(self) -> None:
        self.records: list[TimelineRecord] = []

    def append(self, _issue_number: int, record: TimelineRecord) -> None:
        self.records.append(record)

    def read(self, _issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        return list(self.records) if limit is None else self.records[-limit:]

    def delete(self, _issue_number: int) -> int:
        count = len(self.records)
        self.records.clear()
        return count


class _TimelineEventSink:
    """Event sink wired straight to the production timeline writer."""

    def __init__(self, store: _RecordingTimelineStore) -> None:
        self._writer = DefaultTimelineWriter(store)
        self.published: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.published.append(event)
        self._writer.record(event)


class _LabelSetStub:
    """In-memory LabelSet: the real applier mutates labels through this port."""

    def __init__(self, labels: set[str]) -> None:
        self.labels = labels

    def get_issue_labels(self, issue_number: int) -> list[str]:
        del issue_number
        return sorted(self.labels)

    def has_label(self, issue_number: int, label: str) -> bool:
        del issue_number
        return label in self.labels

    def add_label(self, issue_number: int, label: str) -> None:
        del issue_number
        self.labels.add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        del issue_number
        self.labels.discard(label)


def _config() -> Config:
    config = Config(repo="test/repo")
    config.repo_root = Path("/tmp/repo")
    config.agents["agent:web"] = AgentConfig(
        prompt_path=Path("/tmp/web.md"), provider=PROVIDER
    )
    return config


def _policy(config: Config, manager: ProviderResilienceManager) -> ProviderAvailabilityPolicy:
    return ProviderAvailabilityPolicy(config, manager, LabelManager(config))


def _snapshot(issue: Issue):
    from tests.unit.test_planner import make_snapshot

    return make_snapshot(issues=[issue])


def _applier(labels: _LabelSetStub, events: _TimelineEventSink) -> ActionApplier:
    return ActionApplier(
        labels=labels,
        sessions=MagicMock(),
        events=events,
        repository_host=MagicMock(),
        worktree_manager=MagicMock(),
        fresh_issue_reader=None,
        reconcile=False,
    )


def _timeline_events(store: _RecordingTimelineStore) -> list[dict[str, object]]:
    return build_issue_timeline(ISSUE, store.records)["events"]


def test_provider_outage_lifecycle_survives_in_issue_history():
    """The outage enter/retry/exit story is readable after the label is gone."""
    config = _config()
    lm = LabelManager(config)
    blocked_label = lm.provider_unavailable

    store = InMemoryProviderCircuitStore()
    manager = ProviderResilienceManager(
        config.provider_resilience, store=store, events=MagicMock()
    )
    # A real transient failure opens the real circuit. The wall clock is the
    # circuit's own clock here (the cooldown is minutes long), so the policy's
    # is_open()/retry-ETA reads see a genuinely open circuit.
    manager.record_transient_failure(
        PROVIDER, error_summary="HTTP 529 overloaded", attempts=3
    )
    assert manager.is_open(PROVIDER) is True

    policy = _policy(config, manager)
    issue = Issue(number=ISSUE, title="Surface provider circuit", labels=["agent:web"])

    timeline_store = _RecordingTimelineStore()
    events = _TimelineEventSink(timeline_store)
    labels = _LabelSetStub(set(issue.labels))
    applier = _applier(labels, events)

    # --- Outage begins: the real policy owner plans the transition. ---
    plan_context = PlanContext(issue_labels_by_number={ISSUE: tuple(issue.labels)})
    blocked_actions = policy.plan_provider_impact(_snapshot(issue), plan_context)
    assert [a.action_type for a in blocked_actions] == [ActionType.APPLY_PROVIDER_IMPACT]
    assert blocked_actions[0].transition is ProviderImpactTransition.BLOCKED

    assert applier.apply(blocked_actions[0]).success is True
    assert blocked_label in labels.labels

    # --- Provider recovers: circuit closes and the label is shed. ---
    manager.record_success(PROVIDER)
    assert manager.is_open(PROVIDER) is False

    issue_after = Issue(
        number=ISSUE, title="Surface provider circuit", labels=sorted(labels.labels)
    )
    clear_context = PlanContext(
        issue_labels_by_number={ISSUE: tuple(sorted(labels.labels))}
    )
    cleared_actions = policy.plan_provider_impact(_snapshot(issue_after), clear_context)
    assert [a.action_type for a in cleared_actions] == [ActionType.APPLY_PROVIDER_IMPACT]
    assert cleared_actions[0].transition is ProviderImpactTransition.CLEARED

    assert applier.apply(cleared_actions[0]).success is True

    # The durable context requirement: the label is GONE and the circuit is
    # closed, yet the issue's own history still explains the stall.
    assert blocked_label not in labels.labels

    timeline = _timeline_events(timeline_store)
    by_event = {event["event"]: event for event in timeline}
    assert "provider.issue_blocked" in by_event, (
        "provider outage never reached the issue timeline; "
        f"stored events: {sorted(by_event)}"
    )
    assert "provider.issue_unblocked" in by_event

    entered = by_event["provider.issue_blocked"]
    assert entered["issue_number"] == ISSUE
    assert entered["status"] == "failed"
    # Enter + retry text in one line, so the history says when it would retry.
    summary = str(entered["summary"])
    assert PROVIDER in summary
    assert "next retry in" in summary
    assert entered["narrative"] == "Blocked by provider outage"

    exited = by_event["provider.issue_unblocked"]
    assert exited["status"] == "completed"
    assert PROVIDER in str(exited["summary"])
    assert "released" in str(exited["summary"])
    assert exited["narrative"] == "Provider recovered"


def test_provider_impact_events_are_user_visible():
    """The records are in the user view, not buried in the debug tier."""
    from issue_orchestrator.events.view_registry import fan_out

    for internal in ("provider.issue_blocked", "provider.issue_unblocked"):
        (view_event,) = fan_out(internal)
        assert view_event.visible_in("user"), internal
        assert view_event.narrative


def test_fleet_scoped_provider_events_still_cannot_reach_an_issue_timeline():
    """Pins WHY the issue-scoped records exist.

    The fleet-scoped ``provider.*`` events are kept for fleet observability, but
    they carry no ``issue_number`` and the timeline writer drops them. If this
    ever changes, the issue-scoped records may be redundant — but until then
    they are the only path to issue history.
    """
    config = _config()
    timeline_store = _RecordingTimelineStore()
    manager_events = _TimelineEventSink(timeline_store)
    manager = ProviderResilienceManager(
        config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=manager_events,
    )

    manager.record_transient_failure(PROVIDER, error_summary="boom")

    assert manager_events.published, "manager must still emit fleet-scoped events"
    assert all(
        "issue_number" not in event.data for event in manager_events.published
    )
    assert _timeline_events(timeline_store) == []


def test_impact_record_is_not_written_when_the_label_mutation_fails():
    """A failed transition must not leave a misleading history entry."""
    config = _config()
    manager = ProviderResilienceManager(
        config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=MagicMock(),
    )
    manager.record_transient_failure(PROVIDER, error_summary="boom")
    policy = _policy(config, manager)

    class _BrokenLabels(_LabelSetStub):
        def add_label(self, _issue_number: int, label: str) -> None:
            raise RuntimeError("github write failed")

    timeline_store = _RecordingTimelineStore()
    events = _TimelineEventSink(timeline_store)
    applier = _applier(_BrokenLabels(set()), events)

    result = applier.apply(policy.blocked_transition(ISSUE, (PROVIDER,)))

    assert result.success is False
    assert _timeline_events(timeline_store) == []


def test_impact_record_is_not_re_emitted_while_the_outage_persists():
    """A no-op label transition records nothing, so ticks don't spam history."""
    config = _config()
    manager = ProviderResilienceManager(
        config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=MagicMock(),
    )
    manager.record_transient_failure(PROVIDER, error_summary="boom")
    policy = _policy(config, manager)

    timeline_store = _RecordingTimelineStore()
    events = _TimelineEventSink(timeline_store)
    labels = _LabelSetStub(set())
    applier = _applier(labels, events)

    action = policy.blocked_transition(ISSUE, (PROVIDER,))
    assert applier.apply(action).success is True
    assert applier.apply(action).success is True  # label already present -> no-op

    blocked = [
        event
        for event in _timeline_events(timeline_store)
        if event["event"] == "provider.issue_blocked"
    ]
    assert len(blocked) == 1
