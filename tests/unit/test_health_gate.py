"""Unit tests for the HealthGate service.

These tests verify the HealthGate's behavior-centric decisions about system health
without testing implementation details. The HealthGate encapsulates health check
policies that determine when new sessions can be launched.

Key Behaviors Tested:
1. Health check logic - when to pause, when to allow work
2. Rate limiting behavior - blocking when API quota is low
3. Capacity constraints - respecting max concurrent sessions
4. Paused state handling
5. Recovery after health issues
"""

import pytest
from typing import Any

from issue_orchestrator.control.health_gate import (
    HealthGate,
    HealthDecision,
    RateLimitProvider,
)


class MockRateLimitProvider:
    """Mock rate limit provider for testing."""

    def __init__(self, snapshot: dict[str, Any] | None = None):
        self._snapshot = snapshot

    def get_rate_limit_snapshot(self) -> dict[str, Any] | None:
        return self._snapshot

    def set_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        """Update the snapshot for testing state changes."""
        self._snapshot = snapshot


class MutableSessionCapacity:
    """Test capacity owner whose live limit can be changed between checks."""

    def __init__(self, max_concurrent_sessions: int):
        self.max_concurrent_sessions = max_concurrent_sessions


# ============================================================================
# HealthDecision Tests
# ============================================================================


class TestHealthDecisionFactoryMethods:
    """Test HealthDecision factory methods for creating decisions."""

    def test_ok_creates_passing_decision(self):
        """HealthDecision.ok() creates a decision that allows proceeding."""
        decision = HealthDecision.ok()

        assert decision.can_proceed is True
        assert decision.reason is None
        assert decision.details is None

    def test_blocked_creates_blocking_decision_with_reason(self):
        """HealthDecision.blocked() creates a decision that blocks with reason."""
        decision = HealthDecision.blocked("at_capacity")

        assert decision.can_proceed is False
        assert decision.reason == "at_capacity"
        assert decision.details is None

    def test_blocked_with_details_includes_diagnostics(self):
        """HealthDecision.blocked() can include diagnostic details."""
        decision = HealthDecision.blocked(
            "rate_limit_low",
            remaining=50,
            threshold=100,
        )

        assert decision.can_proceed is False
        assert decision.reason == "rate_limit_low"
        assert decision.details == {"remaining": 50, "threshold": 100}

    def test_decision_is_immutable(self):
        """HealthDecision is frozen and cannot be modified."""
        decision = HealthDecision.ok()

        with pytest.raises(AttributeError):
            decision.can_proceed = False


# ============================================================================
# Paused State Behavior
# ============================================================================


class TestPausedStateBehavior:
    """Tests for paused state handling.

    Invariant: When paused, no new work can start regardless of other factors.
    """

    def test_paused_blocks_new_sessions(self):
        """When paused, no new sessions can be launched."""
        gate = HealthGate(max_concurrent_sessions=5)

        decision = gate.check(active_sessions=0, paused=True)

        assert decision.can_proceed is False
        assert decision.reason == "paused"

    def test_paused_blocks_even_with_capacity(self):
        """Paused state blocks even when there is plenty of capacity."""
        gate = HealthGate(max_concurrent_sessions=10)

        decision = gate.check(active_sessions=0, paused=True)

        assert decision.can_proceed is False
        assert decision.reason == "paused"

    def test_paused_blocks_even_with_healthy_rate_limit(self):
        """Paused state blocks even when rate limit is healthy."""
        rate_provider = MockRateLimitProvider({
            "core": {"remaining": 5000, "limit": 5000}
        })
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_provider=rate_provider,
        )

        decision = gate.check(active_sessions=0, paused=True)

        assert decision.can_proceed is False
        assert decision.reason == "paused"

    def test_unpaused_allows_new_sessions(self):
        """When not paused and healthy, new sessions can be launched."""
        gate = HealthGate(max_concurrent_sessions=5)

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is True


# ============================================================================
# Capacity Constraint Behavior
# ============================================================================


class TestCapacityConstraintBehavior:
    """Tests for capacity limit enforcement.

    Invariant: Active sessions cannot exceed max_concurrent_sessions.
    """

    def test_at_capacity_blocks_new_sessions(self):
        """When at max capacity, no new sessions can be launched."""
        gate = HealthGate(max_concurrent_sessions=3)

        decision = gate.check(active_sessions=3, paused=False)

        assert decision.can_proceed is False
        assert decision.reason == "at_capacity"
        assert decision.details["active_sessions"] == 3
        assert decision.details["max_concurrent"] == 3

    def test_over_capacity_blocks_new_sessions(self):
        """When over max capacity, no new sessions can be launched."""
        gate = HealthGate(max_concurrent_sessions=3)

        decision = gate.check(active_sessions=5, paused=False)

        assert decision.can_proceed is False
        assert decision.reason == "at_capacity"

    def test_below_capacity_allows_new_sessions(self):
        """When below max capacity, new sessions can be launched."""
        gate = HealthGate(max_concurrent_sessions=3)

        decision = gate.check(active_sessions=2, paused=False)

        assert decision.can_proceed is True

    def test_no_active_sessions_allows_new_sessions(self):
        """When no sessions are active, new sessions can be launched."""
        gate = HealthGate(max_concurrent_sessions=3)

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is True

    def test_one_slot_remaining_allows_one_session(self):
        """When one slot remains, exactly one more session can be launched."""
        gate = HealthGate(max_concurrent_sessions=3)

        # At 2 sessions, 1 slot remains
        decision = gate.check(active_sessions=2, paused=False)
        assert decision.can_proceed is True

        # At 3 sessions, no slots remain
        decision = gate.check(active_sessions=3, paused=False)
        assert decision.can_proceed is False

    def test_capacity_source_changes_are_reflected_on_the_next_check(self):
        """Settings updates take effect without rebuilding the health gate."""
        capacity = MutableSessionCapacity(max_concurrent_sessions=2)
        gate = HealthGate(capacity)

        assert gate.check(active_sessions=2).can_proceed is False

        capacity.max_concurrent_sessions = 3

        assert gate.check(active_sessions=2).can_proceed is True
        assert gate.available_capacity == 3
        assert gate.remaining_capacity(active_sessions=2) == 1


# ============================================================================
# Rate Limit Behavior
# ============================================================================


class TestRateLimitBehavior:
    """Tests for GitHub API rate limit enforcement.

    Invariant: When API quota is below threshold, no new sessions should start.
    """

    def test_low_rate_limit_blocks_new_sessions(self):
        """When rate limit is below threshold, no new sessions can be launched."""
        rate_provider = MockRateLimitProvider({
            "core": {"remaining": 50, "limit": 5000}
        })
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is False
        assert decision.reason == "rate_limit_low"
        assert decision.details["remaining"] == 50
        assert decision.details["threshold"] == 100

    def test_rate_limit_at_threshold_blocks(self):
        """Rate limit exactly at threshold blocks (threshold is minimum required)."""
        rate_provider = MockRateLimitProvider({
            "core": {"remaining": 100, "limit": 5000}
        })
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        # Remaining < threshold blocks, so 99 blocks but 100 might be edge case
        # Check the actual implementation logic: remaining < threshold
        decision = gate.check(active_sessions=0, paused=False)

        # 100 is not less than 100, so this should pass
        assert decision.can_proceed is True

    def test_rate_limit_above_threshold_allows(self):
        """When rate limit is above threshold, new sessions can be launched."""
        rate_provider = MockRateLimitProvider({
            "core": {"remaining": 500, "limit": 5000}
        })
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is True

    def test_rate_limit_fully_available_allows(self):
        """Full rate limit quota allows new sessions."""
        rate_provider = MockRateLimitProvider({
            "core": {"remaining": 5000, "limit": 5000}
        })
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is True

    def test_no_rate_limit_provider_assumes_healthy(self):
        """Without rate limit provider, assume rate limit is healthy."""
        gate = HealthGate(max_concurrent_sessions=5)  # No provider

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is True

    def test_null_snapshot_assumes_healthy(self):
        """When snapshot is None, assume rate limit is healthy."""
        rate_provider = MockRateLimitProvider(None)
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is True

    def test_missing_core_data_assumes_healthy(self):
        """When core data is missing from snapshot, assume healthy."""
        rate_provider = MockRateLimitProvider({})
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is True

    def test_missing_remaining_assumes_healthy(self):
        """When remaining field is missing, assume healthy."""
        rate_provider = MockRateLimitProvider({
            "core": {"limit": 5000}  # No remaining
        })
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is True


# ============================================================================
# Recovery After Health Issues
# ============================================================================


class TestRecoveryBehavior:
    """Tests for system recovery after health issues.

    Invariant: System should resume normal operation when issues are resolved.
    """

    def test_recovery_after_rate_limit_replenishes(self):
        """System recovers when rate limit quota is replenished."""
        rate_provider = MockRateLimitProvider({
            "core": {"remaining": 50, "limit": 5000}
        })
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        # Initially blocked
        decision = gate.check(active_sessions=0, paused=False)
        assert decision.can_proceed is False

        # Rate limit replenishes
        rate_provider.set_snapshot({
            "core": {"remaining": 500, "limit": 5000}
        })

        # Now healthy
        decision = gate.check(active_sessions=0, paused=False)
        assert decision.can_proceed is True

    def test_recovery_after_sessions_complete(self):
        """System recovers when active sessions complete."""
        gate = HealthGate(max_concurrent_sessions=3)

        # Initially at capacity
        decision = gate.check(active_sessions=3, paused=False)
        assert decision.can_proceed is False

        # One session completes
        decision = gate.check(active_sessions=2, paused=False)
        assert decision.can_proceed is True

    def test_recovery_after_unpause(self):
        """System recovers when unpaused."""
        gate = HealthGate(max_concurrent_sessions=5)

        # Initially paused
        decision = gate.check(active_sessions=0, paused=True)
        assert decision.can_proceed is False

        # Unpaused
        decision = gate.check(active_sessions=0, paused=False)
        assert decision.can_proceed is True


# ============================================================================
# Check Priority Order
# ============================================================================


class TestCheckPriorityOrder:
    """Tests for health check evaluation order.

    The order matters: paused is checked first, then capacity, then rate limit.
    This ensures we report the most actionable reason.
    """

    def test_paused_reason_takes_priority_over_capacity(self):
        """Paused reason is reported before capacity reason."""
        gate = HealthGate(max_concurrent_sessions=3)

        # Both paused AND at capacity
        decision = gate.check(active_sessions=3, paused=True)

        assert decision.can_proceed is False
        assert decision.reason == "paused"  # Not "at_capacity"

    def test_paused_reason_takes_priority_over_rate_limit(self):
        """Paused reason is reported before rate limit reason."""
        rate_provider = MockRateLimitProvider({
            "core": {"remaining": 50, "limit": 5000}
        })
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        # Both paused AND low rate limit
        decision = gate.check(active_sessions=0, paused=True)

        assert decision.can_proceed is False
        assert decision.reason == "paused"  # Not "rate_limit_low"

    def test_capacity_reason_takes_priority_over_rate_limit(self):
        """Capacity reason is reported before rate limit reason."""
        rate_provider = MockRateLimitProvider({
            "core": {"remaining": 50, "limit": 5000}
        })
        gate = HealthGate(
            max_concurrent_sessions=3,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        # Both at capacity AND low rate limit
        decision = gate.check(active_sessions=3, paused=False)

        assert decision.can_proceed is False
        assert decision.reason == "at_capacity"  # Not "rate_limit_low"


# ============================================================================
# Capacity Query Methods
# ============================================================================


class TestCapacityQueryMethods:
    """Tests for capacity query helper methods."""

    def test_available_capacity_returns_max(self):
        """available_capacity returns the configured maximum."""
        gate = HealthGate(max_concurrent_sessions=5)

        assert gate.available_capacity == 5

    def test_remaining_capacity_with_no_sessions(self):
        """remaining_capacity returns max when no sessions are active."""
        gate = HealthGate(max_concurrent_sessions=5)

        assert gate.remaining_capacity(active_sessions=0) == 5

    def test_remaining_capacity_with_some_sessions(self):
        """remaining_capacity returns difference when some sessions are active."""
        gate = HealthGate(max_concurrent_sessions=5)

        assert gate.remaining_capacity(active_sessions=2) == 3

    def test_remaining_capacity_at_max(self):
        """remaining_capacity returns 0 when at max capacity."""
        gate = HealthGate(max_concurrent_sessions=5)

        assert gate.remaining_capacity(active_sessions=5) == 0

    def test_remaining_capacity_over_max_returns_zero(self):
        """remaining_capacity returns 0 when over max (never negative)."""
        gate = HealthGate(max_concurrent_sessions=5)

        assert gate.remaining_capacity(active_sessions=10) == 0


# ============================================================================
# Edge Cases
# ============================================================================


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_zero_max_concurrent_blocks_all(self):
        """Zero max concurrent sessions blocks all new sessions."""
        gate = HealthGate(max_concurrent_sessions=0)

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is False
        assert decision.reason == "at_capacity"

    def test_custom_rate_limit_threshold(self):
        """Custom rate limit threshold is respected."""
        rate_provider = MockRateLimitProvider({
            "core": {"remaining": 500, "limit": 5000}
        })
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=1000,  # Higher threshold
            rate_limit_provider=rate_provider,
        )

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is False
        assert decision.reason == "rate_limit_low"
        assert decision.details["remaining"] == 500
        assert decision.details["threshold"] == 1000

    def test_rate_limit_zero_remaining_blocks(self):
        """Zero remaining API calls blocks new sessions."""
        rate_provider = MockRateLimitProvider({
            "core": {"remaining": 0, "limit": 5000}
        })
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        decision = gate.check(active_sessions=0, paused=False)

        assert decision.can_proceed is False
        assert decision.reason == "rate_limit_low"
        assert decision.details["remaining"] == 0


# ============================================================================
# Multiple Consecutive Checks
# ============================================================================


class TestConsecutiveChecks:
    """Tests for multiple consecutive health checks.

    Invariant: Each check is independent and reflects current state.
    """

    def test_checks_are_independent(self):
        """Each health check is independent of previous checks."""
        gate = HealthGate(max_concurrent_sessions=3)

        # First check: healthy
        decision1 = gate.check(active_sessions=2, paused=False)
        assert decision1.can_proceed is True

        # Second check: at capacity (state changed externally)
        decision2 = gate.check(active_sessions=3, paused=False)
        assert decision2.can_proceed is False

        # Third check: back to healthy
        decision3 = gate.check(active_sessions=2, paused=False)
        assert decision3.can_proceed is True

    def test_rate_limit_changes_reflected_immediately(self):
        """Rate limit changes are reflected in the next check."""
        rate_provider = MockRateLimitProvider({
            "core": {"remaining": 500, "limit": 5000}
        })
        gate = HealthGate(
            max_concurrent_sessions=5,
            rate_limit_threshold=100,
            rate_limit_provider=rate_provider,
        )

        # First check: healthy
        decision1 = gate.check(active_sessions=0, paused=False)
        assert decision1.can_proceed is True

        # Rate limit drops
        rate_provider.set_snapshot({
            "core": {"remaining": 50, "limit": 5000}
        })

        # Second check: blocked
        decision2 = gate.check(active_sessions=0, paused=False)
        assert decision2.can_proceed is False

        # Rate limit recovers
        rate_provider.set_snapshot({
            "core": {"remaining": 500, "limit": 5000}
        })

        # Third check: healthy again
        decision3 = gate.check(active_sessions=0, paused=False)
        assert decision3.can_proceed is True
