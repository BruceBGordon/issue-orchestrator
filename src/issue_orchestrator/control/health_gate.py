"""HealthGate - System-wide checks that gate orchestration planning.

This module provides a HealthGate service for conditions that make the whole
planning cycle unsafe. Session-capacity policy deliberately does not live here:
the planner's ``worker_budget`` owner understands worker, reserved tech-lead,
and E2E budgets and is the only authority that may suppress launch actions.

Health Checks:
- Rate Limit: Do we have sufficient GitHub API quota remaining?
- Paused: Is the orchestrator paused?

Usage:
    gate = HealthGate()
    decision = gate.check(paused=False)
    if decision.can_proceed:
        # Gather facts and ask the planner for actions
    else:
        # Skip this planning cycle and log decision.reason
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


class HealthGate:
    """Service for checking system health before orchestration planning.

    This service encapsulates global checks that must pass before the
    orchestrator may plan or apply actions. Launch capacity remains in the
    planner because capacity does not block non-launch actions and the planner
    owns multiple independent session budgets.

    By extracting global health checks into a separate service:

    1. The orchestrator becomes thinner (just a mediator)
    2. Health checks are testable in isolation
    3. Health policy is documented in one place
    """

    def __init__(
        self,
        rate_limit_threshold: int = 100,
        rate_limit_provider: RateLimitProvider | None = None,
    ) -> None:
        """Initialize the health gate.

        Args:
            rate_limit_threshold: Minimum remaining API calls before blocking.
            rate_limit_provider: Provider for rate limit info (e.g., gh_audit).
        """
        self._rate_limit_threshold = rate_limit_threshold
        self._rate_limit_provider = rate_limit_provider

    def check(
        self,
        *,
        paused: bool = False,
    ) -> HealthDecision:
        """Check if the system is healthy enough to run a planning cycle.

        Args:
            paused: Whether the orchestrator is paused.

        Returns:
            HealthDecision indicating whether to proceed.
        """
        # Check 1: Paused state
        if paused:
            return HealthDecision.blocked("paused", paused=True)

        # Check 2: Rate limit (optional)
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


def create_health_gate_from_config(config: Config) -> HealthGate:
    """Create a HealthGate from config.

    Args:
        config: Runtime config containing the GitHub rate-limit threshold.

    Returns:
        Configured HealthGate instance.
    """
    from ..infra import gh_audit

    # gh_audit implements RateLimitProvider protocol
    return HealthGate(
        rate_limit_threshold=getattr(config, "rate_limit_warn_remaining", 100),
        rate_limit_provider=gh_audit,
    )
