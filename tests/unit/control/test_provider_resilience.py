"""Tests for provider resilience manager."""

from datetime import datetime, timezone, timedelta

from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.infra.config import ProviderResilienceConfig
from issue_orchestrator.ports import InMemoryProviderCircuitStore, NullEventSink


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
