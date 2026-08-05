"""HealthGate - System health checks for session launching.

This module provides a HealthGate service that encapsulates all health
checks that must pass before launching new sessions. Per architecture
objective: "move policies into small, testable services."

Health Checks:
- Capacity: Are we under the max concurrent sessions limit?
- Rate Limit: Do we have sufficient GitHub API quota remaining?
- Paused: Is the orchestrator paused?

Usage:
    gate = HealthGate(config)
    decision = gate.check(active_sessions=5, paused=False)
    if decision.can_proceed:
        # Launch new session
    else:
        # Skip, log decision.reason
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..infra.config import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthDecision:
    """Result of a health gate check.

    Attributes:
        can_proceed: True if all health checks pass.
        reason: Explanation if can_proceed is False.
        details: Additional diagnostic info.
    """

    can_proceed: bool
    reason: str | None = None
    details: dict[str, Any] | None = None

    @classmethod
    def ok(cls) -> "HealthDecision":
        """Create a passing decision."""
        return cls(can_proceed=True)

    @classmethod
    def blocked(cls, reason: str, **details: Any) -> "HealthDecision":
        """Create a blocking decision."""
        return cls(can_proceed=False, reason=reason, details=details or None)


class RateLimitProvider(Protocol):
    """Protocol for rate limit information."""

    def get_rate_limit_snapshot(self) -> dict[str, Any] | None:
        """Get the latest rate limit snapshot."""
        ...


class SessionCapacitySource(Protocol):
    """Live source of the Repository Engine's worker-session limit."""

    max_concurrent_sessions: int


@dataclass(frozen=True)
class _FixedSessionCapacity:
    """Value owner for standalone callers that supply a fixed limit."""

    max_concurrent_sessions: int


class HealthGate:
    """Service for checking system health before launching sessions.

    This service encapsulates all the health checks that must pass
    before the orchestrator can launch new sessions. By extracting
    this into a separate service:

    1. The orchestrator becomes thinner (just a mediator)
    2. Health checks are testable in isolation
    3. Health policy is documented in one place
    """

    def __init__(
        self,
        max_concurrent_sessions: int | SessionCapacitySource,
        rate_limit_threshold: int = 100,
        rate_limit_provider: RateLimitProvider | None = None,
    ) -> None:
        """Initialize the health gate.

        Args:
            max_concurrent_sessions: A fixed limit, or the live configuration
                owner whose current limit should be read on every check.
            rate_limit_threshold: Minimum remaining API calls before blocking.
            rate_limit_provider: Provider for rate limit info (e.g., gh_audit).
        """
        self._capacity = (
            _FixedSessionCapacity(max_concurrent_sessions)
            if isinstance(max_concurrent_sessions, int)
            else max_concurrent_sessions
        )
        self._rate_limit_threshold = rate_limit_threshold
        self._rate_limit_provider = rate_limit_provider

    def check(
        self,
        *,
        active_sessions: int,
        paused: bool = False,
    ) -> HealthDecision:
        """Check if the system is healthy enough to launch new sessions.

        Args:
            active_sessions: Number of currently active sessions.
            paused: Whether the orchestrator is paused.

        Returns:
            HealthDecision indicating whether to proceed.
        """
        # Check 1: Paused state
        if paused:
            return HealthDecision.blocked("paused", paused=True)

        # Check 2: Capacity. Read once so one decision is internally
        # consistent even if the settings owner changes between ticks.
        max_concurrent = self._capacity.max_concurrent_sessions
        if active_sessions >= max_concurrent:
            return HealthDecision.blocked(
                "at_capacity",
                active_sessions=active_sessions,
                max_concurrent=max_concurrent,
            )

        # Check 3: Rate limit (optional)
        if self._rate_limit_provider is not None:
            rate_decision = self._check_rate_limit()
            if not rate_decision.can_proceed:
                return rate_decision

        # All checks passed
        return HealthDecision.ok()

    def _check_rate_limit(self) -> HealthDecision:
        """Check GitHub API rate limit.

        Returns:
            HealthDecision for rate limit check.
        """
        if self._rate_limit_provider is None:
            return HealthDecision.ok()

        snapshot = self._rate_limit_provider.get_rate_limit_snapshot()
        if snapshot is None:
            # No rate limit info - assume OK
            logger.debug("No rate limit snapshot available, assuming healthy")
            return HealthDecision.ok()

        core = snapshot.get("core", {})
        remaining = core.get("remaining")

        if remaining is None:
            return HealthDecision.ok()

        if remaining < self._rate_limit_threshold:
            return HealthDecision.blocked(
                "rate_limit_low",
                remaining=remaining,
                threshold=self._rate_limit_threshold,
            )

        return HealthDecision.ok()

    @property
    def available_capacity(self) -> int:
        """Get the total capacity (for planning)."""
        return self._capacity.max_concurrent_sessions

    def remaining_capacity(self, active_sessions: int) -> int:
        """Get remaining capacity given current active sessions.

        Args:
            active_sessions: Number of currently active sessions.

        Returns:
            Number of additional sessions that can be launched.
        """
        return max(0, self._capacity.max_concurrent_sessions - active_sessions)


def create_health_gate_from_config(config: Config) -> HealthGate:
    """Create a HealthGate from config.

    Args:
        config: Config object with max_concurrent_sessions, etc.

    Returns:
        Configured HealthGate instance.
    """
    from ..infra import gh_audit

    # gh_audit implements RateLimitProvider protocol
    return HealthGate(
        max_concurrent_sessions=config,
        rate_limit_threshold=getattr(config, "rate_limit_warn_remaining", 100),
        rate_limit_provider=gh_audit,
    )
