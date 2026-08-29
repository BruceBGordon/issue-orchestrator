# pyright: strict
"""Port for lane-backend policy self-checks."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.lane_execution import LanePolicyReport


@runtime_checkable
class LanePolicyCheck(Protocol):
    """Report whether a backend still carries the policy lanes depend on.

    The gate proves that lanes *complete*; nothing else proves the live
    backend still enforces the scheduling contract those lanes were
    written against. A backend whose policy has been edited by hand,
    reinstalled, or reverted keeps accepting work and quietly degrades
    every lane that follows.

    Contract every adapter must honor:

    - ``inspect`` is read-only interrogation of the backend's live
      configuration. It never repairs and never mutates.
    - Drift is DATA, not an exception: the returned report names every
      required setting with expected and observed values, so one run
      names every drifted knob instead of stopping at the first.
    - Backend faults (unreachable, unreadable configuration) raise
      :class:`~issue_orchestrator.domain.lane_execution.LaneExecutorError`
      subclasses, exactly as :class:`LaneExecutor` does. A backend that
      cannot be read must never be reported as satisfying its policy.
    - It is cheap enough to run unconditionally at the head of a gate,
      and is run ONCE there — never once per lane.
    """

    def inspect(self) -> LanePolicyReport:
        """Read the backend's live policy and report what it found."""
        ...
