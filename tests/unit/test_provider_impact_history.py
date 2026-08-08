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

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import ActionType
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.provider_availability import ProviderAvailabilityPolicy
from issue_orchestrator.control.provider_impact import (
    ProviderImpactTransition,
    ProviderReleaseKind,
)
from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.control.planner_types import PlanContext
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, PendingReview
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import InMemoryProviderCircuitStore
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import build_issue_timeline
from tests.unit.test_planner import make_snapshot

ISSUE = 5980
PROVIDER = "anthropic"
REVIEW_PROVIDER = "openai"
# Every circuit read in this module is driven from an explicit instant: the
# policy resolves `now` once per assessment and threads it into every status
# read, so nothing here depends on the wall clock (#5980 F5).
NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


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


def _multi_provider_config() -> Config:
    """An issue whose coding agent and reviewer sit on different providers."""
    config = _config()
    config.code_review_agent = "agent:reviewer"
    config.agents["agent:reviewer"] = AgentConfig(
        prompt_path=Path("/tmp/review.md"), provider=REVIEW_PROVIDER
    )
    return config


def _policy(config: Config, manager: ProviderResilienceManager) -> ProviderAvailabilityPolicy:
    return ProviderAvailabilityPolicy(config, manager, LabelManager(config))


def _snapshot(issue: Issue):
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


def _events_named(store: _RecordingTimelineStore, name: str) -> list[dict[str, object]]:
    """Projected timeline events for one event name.

    The applier also emits debug-tier ``issue.labels_changed`` records for the
    same issue, so name-filtering keeps these assertions about the record under
    test.
    """
    return [event for event in _timeline_events(store) if event["event"] == name]


def _payload_named(store: _RecordingTimelineStore, name: str) -> dict[str, object]:
    """The stored payload for the single record with ``name``.

    ``TimelineEvent.to_dict()`` projects a fixed display field set; the typed
    machine fields (provider partition, release kind) live in the record data
    that UI/automation consumers read.
    """
    payloads = [record.data for record in store.records if record.event == name]
    assert len(payloads) == 1, f"expected exactly one {name} record, got {len(payloads)}"
    return payloads[0]


def _plan_and_apply(policy, applier, issue, labels, *, now):
    """Run the real planner path for one issue at a fixed instant."""
    plan_context = PlanContext(
        issue_labels_by_number={ISSUE: tuple(sorted(labels.labels))}
    )
    actions = policy.plan_provider_impact(_snapshot(issue), plan_context, now=now)
    for action in actions:
        assert action.action_type is ActionType.APPLY_PROVIDER_IMPACT
        assert applier.apply(action).success is True
    return actions


def _reissue(labels: _LabelSetStub) -> Issue:
    return Issue(
        number=ISSUE, title="Surface provider circuit", labels=sorted(labels.labels)
    )


def test_provider_outage_lifecycle_survives_in_issue_history():
    """The outage enter/retry/exit story is readable after the label is gone."""
    config = _config()
    lm = LabelManager(config)
    blocked_label = lm.provider_unavailable

    manager = ProviderResilienceManager(
        config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=MagicMock(),
    )
    manager.record_transient_failure(
        PROVIDER, error_summary="HTTP 529 overloaded", attempts=3, now=NOW
    )
    assert manager.is_open(PROVIDER, NOW) is True

    policy = _policy(config, manager)
    timeline_store = _RecordingTimelineStore()
    events = _TimelineEventSink(timeline_store)
    labels = _LabelSetStub({"agent:web"})
    applier = _applier(labels, events)

    # --- Outage begins: the real policy owner plans the transition. ---
    blocked_actions = _plan_and_apply(
        policy, applier, _reissue(labels), labels, now=NOW
    )
    assert [a.transition for a in blocked_actions] == [ProviderImpactTransition.BLOCKED]
    assert blocked_label in labels.labels

    # --- Provider confirms recovery: a successful call deletes the row. ---
    manager.record_success(PROVIDER, now=NOW + timedelta(minutes=10))
    later = NOW + timedelta(minutes=10)
    assert manager.is_open(PROVIDER, later) is False

    cleared_actions = _plan_and_apply(
        policy, applier, _reissue(labels), labels, now=later
    )
    assert [a.transition for a in cleared_actions] == [ProviderImpactTransition.CLEARED]

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
    # Neutral narrative: the release wording lives in the summary + release_kind,
    # because a release is not always a confirmed recovery (#5980 F4).
    assert exited["narrative"] == "Provider block cleared"
    assert _payload_named(timeline_store, "provider.issue_unblocked")[
        "release_kind"
    ] == "available"


def test_blocked_record_names_only_the_open_provider():
    """F4 case 1: two relevant providers, exactly one circuit open.

    ``providers_for_snapshot`` deliberately aggregates the coding agent's
    provider AND the reviewer's. The blocked record must name only the circuit
    that is actually open — calling a healthy provider "unavailable" would make
    the operator-facing audit trail wrong.
    """
    config = _multi_provider_config()
    manager = ProviderResilienceManager(
        config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=MagicMock(),
    )
    manager.record_transient_failure(PROVIDER, error_summary="HTTP 529", now=NOW)
    policy = _policy(config, manager)

    issue = Issue(number=ISSUE, title="Two providers", labels=["agent:web"])
    review = PendingReview(
        issue_key=FakeIssueKey(name=str(ISSUE)),
        pr_number=101,
        pr_url="https://example.test/pr/101",
        branch_name="branch-101",
        _issue_number=ISSUE,
        agent_label=None,
    )
    snapshot = make_snapshot(issues=[issue], pending_reviews=[review])

    # Both providers really are in scope for this issue...
    assert policy.providers_for_snapshot(snapshot)[ISSUE] == {PROVIDER, REVIEW_PROVIDER}
    # ...but only one circuit is open.
    assessment = policy.assess({PROVIDER, REVIEW_PROVIDER}, now=NOW)
    assert assessment.open_providers == (PROVIDER,)
    assert assessment.healthy_providers == (REVIEW_PROVIDER,)

    timeline_store = _RecordingTimelineStore()
    events = _TimelineEventSink(timeline_store)
    labels = _LabelSetStub({"agent:web"})
    applier = _applier(labels, events)

    plan_context = PlanContext(issue_labels_by_number={ISSUE: ("agent:web",)})
    (action,) = policy.plan_provider_impact(snapshot, plan_context, now=NOW)
    assert action.providers == (PROVIDER,)
    assert REVIEW_PROVIDER not in action.summary()
    assert applier.apply(action).success is True

    (entered,) = _events_named(timeline_store, "provider.issue_blocked")
    assert REVIEW_PROVIDER not in str(entered["summary"]), (
        "a healthy provider must never be reported as unavailable"
    )
    payload = _payload_named(timeline_store, "provider.issue_blocked")
    assert payload["providers"] == [PROVIDER]
    # The full partition still rides along for machine consumers.
    assert payload["healthy_providers"] == [REVIEW_PROVIDER]
    assert payload["open_providers"] == [PROVIDER]


def test_release_after_cooldown_expiry_does_not_claim_recovery():
    """F4 case 2: close_expired() without record_success().

    ``close_expired`` clears ``open_until`` but keeps the row: the cooldown
    elapsed and a retry is allowed, yet no call has succeeded, so the circuit is
    "recovering", not recovered. The history must say so.
    """
    config = _config()
    manager = ProviderResilienceManager(
        config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=MagicMock(),
    )
    manager.record_transient_failure(PROVIDER, error_summary="HTTP 529", now=NOW)
    policy = _policy(config, manager)

    timeline_store = _RecordingTimelineStore()
    events = _TimelineEventSink(timeline_store)
    labels = _LabelSetStub({"agent:web"})
    applier = _applier(labels, events)

    _plan_and_apply(policy, applier, _reissue(labels), labels, now=NOW)
    assert LabelManager(config).provider_unavailable in labels.labels

    # Cooldown elapses. NOTE: no record_success() — nothing proved healthy.
    after_cooldown = NOW + timedelta(hours=1)
    closed = manager.close_expired(now=after_cooldown)
    assert [state.provider for state in closed] == [PROVIDER]

    assessment = policy.assess((PROVIDER,), now=after_cooldown)
    assert assessment.blocked is False
    assert assessment.recovering_providers == (PROVIDER,)
    assert assessment.release_kind is ProviderReleaseKind.COOLDOWN_ELAPSED

    (action,) = _plan_and_apply(
        policy, applier, _reissue(labels), labels, now=after_cooldown
    )
    assert action.transition is ProviderImpactTransition.CLEARED
    assert LabelManager(config).provider_unavailable not in labels.labels

    (released_event,) = _events_named(timeline_store, "provider.issue_unblocked")
    summary = str(released_event["summary"])
    assert "cooldown elapsed" in summary.lower()
    assert "not confirmed" in summary.lower()
    assert "recovered" not in summary.lower(), (
        "a merely-recovering circuit must not be reported as recovered"
    )
    assert released_event["narrative"] != "Provider recovered"
    payload = _payload_named(timeline_store, "provider.issue_unblocked")
    assert payload["release_kind"] == "cooldown_elapsed"
    assert payload["recovering_providers"] == [PROVIDER]


def test_release_after_confirmed_success_has_distinct_wording():
    """F4 case 3: a confirmed-healthy release reads differently."""
    config = _config()
    manager = ProviderResilienceManager(
        config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=MagicMock(),
    )
    manager.record_transient_failure(PROVIDER, error_summary="HTTP 529", now=NOW)
    policy = _policy(config, manager)

    timeline_store = _RecordingTimelineStore()
    events = _TimelineEventSink(timeline_store)
    labels = _LabelSetStub({"agent:web"})
    applier = _applier(labels, events)

    _plan_and_apply(policy, applier, _reissue(labels), labels, now=NOW)

    # A successful call deletes the row: the provider is confirmed healthy.
    later = NOW + timedelta(minutes=5)
    manager.record_success(PROVIDER, now=later)

    assessment = policy.assess((PROVIDER,), now=later)
    assert assessment.recovering_providers == ()
    assert assessment.healthy_providers == (PROVIDER,)
    assert assessment.release_kind is ProviderReleaseKind.AVAILABLE

    _plan_and_apply(policy, applier, _reissue(labels), labels, now=later)

    (released_event,) = _events_named(timeline_store, "provider.issue_unblocked")
    summary = str(released_event["summary"])
    assert "available" in summary.lower()
    assert "cooldown elapsed" not in summary.lower()
    assert _payload_named(timeline_store, "provider.issue_unblocked")[
        "release_kind"
    ] == "available"


def test_assessment_reads_every_circuit_at_one_instant():
    """The label decision and the retry metadata describe the same moment.

    Regression for #5980 F4/A2 + F5: the policy used to evaluate ``any_open``
    and the retry ETA in separate reads, each resolving its own ``now``.
    """
    config = _multi_provider_config()
    manager = ProviderResilienceManager(
        config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=MagicMock(),
    )
    manager.record_transient_failure(PROVIDER, error_summary="a", now=NOW)
    manager.record_transient_failure(REVIEW_PROVIDER, error_summary="b", now=NOW)
    policy = _policy(config, manager)

    assessment = policy.assess({PROVIDER, REVIEW_PROVIDER}, now=NOW)

    assert assessment.assessed_at == NOW
    assert assessment.open_providers == (PROVIDER, REVIEW_PROVIDER)
    assert assessment.cooldown_remaining_seconds is not None
    # The advertised retry instant is exactly one cooldown after the assessed
    # moment — derived from the same read, not a second clock sample.
    assert assessment.next_retry_at == (
        NOW + timedelta(seconds=assessment.cooldown_remaining_seconds)
    ).isoformat()


def test_blocked_transition_rejects_an_assessment_with_no_open_circuit():
    """The command refuses to record an outage that is not happening."""
    config = _config()
    manager = ProviderResilienceManager(
        config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=MagicMock(),
    )
    policy = _policy(config, manager)

    healthy = policy.assess((PROVIDER,), now=NOW)
    assert healthy.blocked is False

    with pytest.raises(ValueError, match="requires at least one open circuit"):
        policy.blocked_transition(ISSUE, healthy)


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

    manager.record_transient_failure(PROVIDER, error_summary="boom", now=NOW)

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
    manager.record_transient_failure(PROVIDER, error_summary="boom", now=NOW)
    policy = _policy(config, manager)

    class _BrokenLabels(_LabelSetStub):
        def add_label(self, _issue_number: int, label: str) -> None:
            raise RuntimeError("github write failed")

    timeline_store = _RecordingTimelineStore()
    events = _TimelineEventSink(timeline_store)
    applier = _applier(_BrokenLabels(set()), events)

    assessment = policy.assess((PROVIDER,), now=NOW)
    result = applier.apply(policy.blocked_transition(ISSUE, assessment))

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
    manager.record_transient_failure(PROVIDER, error_summary="boom", now=NOW)
    policy = _policy(config, manager)

    timeline_store = _RecordingTimelineStore()
    events = _TimelineEventSink(timeline_store)
    labels = _LabelSetStub(set())
    applier = _applier(labels, events)

    action = policy.blocked_transition(ISSUE, policy.assess((PROVIDER,), now=NOW))
    assert applier.apply(action).success is True
    assert applier.apply(action).success is True  # label already present -> no-op

    assert len(_events_named(timeline_store, "provider.issue_blocked")) == 1
