"""Tests for provider resilience manager."""

from datetime import datetime, timezone, timedelta

import pytest

from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.execution.provider_circuit_store import (
    SQLiteProviderCircuitStore,
)
from issue_orchestrator.infra.config import ProviderResilienceConfig
from issue_orchestrator.ports import InMemoryProviderCircuitStore, NullEventSink


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list = []

    def publish(self, event) -> None:
        self.events.append(event)


def _record_failure(
    manager: ProviderResilienceManager,
    cause: str,
    observed_at: datetime,
):
    if cause == "transient":
        return manager.record_transient_failure(
            "codex", error_summary="503", now=observed_at
        )
    return manager.record_quota_failure(
        "codex", error_summary="out of credits", now=observed_at
    )


def _cause_observed_at(state, cause: str):
    if cause == "transient":
        return state.transient_observed_at
    return state.quota_observed_at


def _cause_count(state, cause: str) -> int:
    if cause == "transient":
        return state.consecutive_outages
    return state.consecutive_quota_failures


def test_record_transient_failure_opens_circuit():
    store = InMemoryProviderCircuitStore()
    mgr = ProviderResilienceManager(ProviderResilienceConfig(), store=store, events=NullEventSink())
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    state = mgr.record_transient_failure("claude-code", error_summary="503", attempts=2, now=now)

    assert state is not None
    assert state.provider == "claude-code"
    assert state.open_until is not None
    assert state.open_until > now
    assert mgr.is_open("claude-code", now=now)


def test_close_expired_closes_circuit():
    store = InMemoryProviderCircuitStore()
    mgr = ProviderResilienceManager(ProviderResilienceConfig(), store=store, events=NullEventSink())
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state = mgr.record_transient_failure("codex", error_summary="timeout", attempts=1, now=now)

    assert state is not None
    later = state.open_until or (now + timedelta(seconds=1))
    mgr.close_expired(now=later)
    assert not mgr.is_open("codex", now=later)


def test_record_success_resets_state():
    store = InMemoryProviderCircuitStore()
    mgr = ProviderResilienceManager(ProviderResilienceConfig(), store=store, events=NullEventSink())
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mgr.record_transient_failure("claude-code", error_summary="503", attempts=1, now=now)

    success_at = now + timedelta(seconds=10)
    mgr.record_success(
        "claude-code",
        observed_at=success_at,
        now=success_at,
    )
    assert mgr.get_state("claude-code") is None


@pytest.mark.parametrize("cause", ["transient", "quota"])
def test_success_watermark_rejects_older_failure_and_accepts_newer(cause: str):
    """A healthy row stays absent until chronologically newer failure evidence."""
    store = InMemoryProviderCircuitStore()
    events = RecordingEvents()
    manager = ProviderResilienceManager(
        ProviderResilienceConfig(), store=store, events=events
    )
    start = datetime(2026, 8, 26, tzinfo=timezone.utc)
    success_at = start + timedelta(minutes=10)
    manager.record_success("codex", observed_at=success_at, now=success_at)

    stale = _record_failure(manager, cause, start)

    assert stale is None
    assert manager.get_state("codex") is None
    assert events.events == []
    evidence = store.get_evidence("codex")
    assert evidence is not None
    assert evidence.success_observed_at == success_at

    newer_failure_at = success_at + timedelta(minutes=1)
    accepted = _record_failure(manager, cause, newer_failure_at)

    assert accepted is not None
    assert _cause_observed_at(accepted, cause) == newer_failure_at
    assert _cause_count(accepted, cause) == 1
    assert events.events


@pytest.mark.parametrize("cause", ["transient", "quota"])
def test_failure_watermark_rejects_older_failure_and_accepts_newer(cause: str):
    """Late stale failures cannot replace a deadline or increment a counter."""
    store = InMemoryProviderCircuitStore()
    events = RecordingEvents()
    manager = ProviderResilienceManager(
        ProviderResilienceConfig(), store=store, events=events
    )
    start = datetime(2026, 8, 26, tzinfo=timezone.utc)
    first_at = start + timedelta(minutes=10)
    first = _record_failure(manager, cause, first_at)
    assert first is not None
    event_count = len(events.events)

    stale = _record_failure(manager, cause, start)

    assert stale == first
    assert manager.get_state("codex") == first
    assert len(events.events) == event_count
    evidence = store.get_evidence("codex")
    assert evidence is not None
    failure_watermark = (
        evidence.transient_failure_observed_at
        if cause == "transient"
        else evidence.quota_failure_observed_at
    )
    assert failure_watermark == first_at

    newer_at = first_at + timedelta(minutes=1)
    newer = _record_failure(manager, cause, newer_at)

    assert newer is not None
    assert _cause_observed_at(newer, cause) == newer_at
    assert _cause_count(newer, cause) == 2
    assert len(events.events) > event_count


def test_post_expiry_success_deletes_recovering_transient_row():
    """Historical escalation memory is not itself an active outage cause."""
    store = InMemoryProviderCircuitStore()
    manager = ProviderResilienceManager(
        ProviderResilienceConfig(), store=store, events=NullEventSink()
    )
    failed_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
    failed = manager.record_transient_failure("codex", now=failed_at)
    assert failed is not None
    deadline = failed.transient_open_until
    assert deadline is not None
    manager.close_expired(now=deadline)
    recovering = manager.get_state("codex")
    assert recovering is not None
    assert recovering.transient_open_until is None
    assert recovering.consecutive_outages == 1

    success_at = deadline + timedelta(seconds=1)
    manager.record_success("codex", observed_at=success_at, now=success_at)

    assert manager.get_state("codex") is None
    evidence = store.get_evidence("codex")
    assert evidence is not None
    assert evidence.success_observed_at == success_at


def test_expired_transient_escalates_again_until_success_is_confirmed():
    """Cooldown expiry preserves escalation memory; confirmed success resets it."""
    store = InMemoryProviderCircuitStore()
    manager = ProviderResilienceManager(
        ProviderResilienceConfig(), store=store, events=NullEventSink()
    )
    first_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
    first = manager.record_transient_failure("codex", now=first_at)
    assert first is not None
    deadline = first.transient_open_until
    assert deadline is not None
    manager.close_expired(now=deadline)

    second_at = deadline + timedelta(seconds=1)
    second = manager.record_transient_failure("codex", now=second_at)

    assert second is not None
    assert second.consecutive_outages == 2
    success_at = second_at + timedelta(seconds=1)
    manager.record_success("codex", observed_at=success_at, now=success_at)
    assert manager.get_state("codex") is None


def test_success_watermark_survives_restart_without_a_circuit_row(tmp_path):
    """The recovery ledger persists independently of healthy circuit state."""
    db_path = tmp_path / "provider-circuit.sqlite"
    store = SQLiteProviderCircuitStore(db_path)
    manager = ProviderResilienceManager(
        ProviderResilienceConfig(), store=store, events=NullEventSink()
    )
    success_at = datetime(2026, 8, 26, 0, 15, tzinfo=timezone.utc)
    manager.record_success("codex", observed_at=success_at, now=success_at)
    assert store.get("codex") is None

    restarted_store = SQLiteProviderCircuitStore(db_path)
    restarted = ProviderResilienceManager(
        ProviderResilienceConfig(), store=restarted_store, events=NullEventSink()
    )
    stale = restarted.record_quota_failure(
        "codex",
        error_summary="out of credits",
        now=success_at - timedelta(minutes=5),
    )

    assert stale is None
    assert restarted_store.get("codex") is None
    evidence = restarted_store.get_evidence("codex")
    assert evidence is not None
    assert evidence.success_observed_at == success_at
