"""Unit tests for the system-wide orchestration health gate."""

from typing import Any

import pytest

from issue_orchestrator.control.health_gate import HealthDecision, HealthGate


class MockRateLimitProvider:
    """Controllable rate-limit provider for deterministic health checks."""

    def __init__(self, snapshot: dict[str, Any] | None = None):
        self._snapshot = snapshot

    def get_rate_limit_snapshot(self) -> dict[str, Any] | None:
        return self._snapshot

    def set_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        self._snapshot = snapshot


class TestHealthDecisionFactoryMethods:
    def test_ok_creates_passing_decision(self):
        decision = HealthDecision.ok()

        assert decision.can_proceed is True
        assert decision.reason is None
        assert decision.details is None

    def test_blocked_creates_decision_with_reason_and_details(self):
        decision = HealthDecision.blocked(
            "rate_limit_low",
            remaining=50,
            threshold=100,
        )

        assert decision.can_proceed is False
        assert decision.reason == "rate_limit_low"
        assert decision.details == {"remaining": 50, "threshold": 100}

    def test_decision_is_immutable(self):
        decision = HealthDecision.ok()

        with pytest.raises(AttributeError):
            decision.can_proceed = False


class TestPausedStateBehavior:
    """Paused is a global planning blocker, independent of launch capacity."""

    def test_paused_blocks_planning(self):
        decision = HealthGate().check(paused=True)

        assert decision.can_proceed is False
        assert decision.reason == "paused"
        assert decision.details == {"paused": True}

    def test_unpaused_allows_planning_without_rate_provider(self):
        decision = HealthGate().check(paused=False)

        assert decision.can_proceed is True

    def test_paused_takes_priority_over_low_rate_limit(self):
        provider = MockRateLimitProvider({"core": {"remaining": 50}})
        gate = HealthGate(rate_limit_threshold=100, rate_limit_provider=provider)

        decision = gate.check(paused=True)

        assert decision.reason == "paused"


class TestRateLimitBehavior:
    def test_low_rate_limit_blocks_planning(self):
        provider = MockRateLimitProvider({"core": {"remaining": 50, "limit": 5000}})
        gate = HealthGate(rate_limit_threshold=100, rate_limit_provider=provider)

        decision = gate.check()

        assert decision.can_proceed is False
        assert decision.reason == "rate_limit_low"
        assert decision.details == {"remaining": 50, "threshold": 100}

    def test_rate_limit_at_threshold_allows_planning(self):
        provider = MockRateLimitProvider({"core": {"remaining": 100, "limit": 5000}})
        gate = HealthGate(rate_limit_threshold=100, rate_limit_provider=provider)

        assert gate.check().can_proceed is True

    def test_rate_limit_above_threshold_allows_planning(self):
        provider = MockRateLimitProvider({"core": {"remaining": 500, "limit": 5000}})
        gate = HealthGate(rate_limit_threshold=100, rate_limit_provider=provider)

        assert gate.check().can_proceed is True

    @pytest.mark.parametrize(
        "snapshot",
        [None, {}, {"core": {}}, {"core": {"limit": 5000}}],
    )
    def test_missing_rate_limit_information_assumes_healthy(self, snapshot):
        provider = MockRateLimitProvider(snapshot)
        gate = HealthGate(rate_limit_threshold=100, rate_limit_provider=provider)

        assert gate.check().can_proceed is True

    def test_custom_threshold_is_respected(self):
        provider = MockRateLimitProvider({"core": {"remaining": 500, "limit": 5000}})
        gate = HealthGate(rate_limit_threshold=1000, rate_limit_provider=provider)

        decision = gate.check()

        assert decision.reason == "rate_limit_low"
        assert decision.details == {"remaining": 500, "threshold": 1000}

    def test_zero_remaining_blocks_planning(self):
        provider = MockRateLimitProvider({"core": {"remaining": 0, "limit": 5000}})
        gate = HealthGate(rate_limit_threshold=100, rate_limit_provider=provider)

        assert gate.check().reason == "rate_limit_low"


class TestRecoveryBehavior:
    def test_rate_limit_recovery_is_visible_on_next_check(self):
        provider = MockRateLimitProvider({"core": {"remaining": 50}})
        gate = HealthGate(rate_limit_threshold=100, rate_limit_provider=provider)

        assert gate.check().can_proceed is False

        provider.set_snapshot({"core": {"remaining": 500}})

        assert gate.check().can_proceed is True

    def test_unpause_is_visible_on_next_check(self):
        gate = HealthGate()

        assert gate.check(paused=True).can_proceed is False
        assert gate.check(paused=False).can_proceed is True
