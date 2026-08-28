# pyright: strict
"""Port for the lane runtime-history learning loop.

The loop is deliberately backend-neutral: every execution backend
reports observed runtime through the lane outcome, and every backend's
submissions may consume the learned ordering. Burying it inside one
scheduler adapter would force each future backend to reinvent it;
floating it into clients would break the "logical names only" contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.lane_execution import LaneWorkKey


@runtime_checkable
class LaneRuntimeHistory(Protocol):
    """Learn each lane's runtime; answer with a dispatch-order hint.

    The contract of the loop:

    - Only successful runs are recorded — a failed run's duration is
      the failure's, not the lane's.
    - ``learned_priority`` returns the rolling median of recorded
      runtimes in whole seconds, and 0 for a lane with no history:
      zero history is the naive first run, by design, not an error.
    - The returned value is an ordering *rank* (longer lanes sort
      first — the LPT heuristic), not a reservation: consumers must
      not treat it as a promised duration.
    """

    def record_success(self, work_key: LaneWorkKey, runtime_seconds: float) -> None:
        """Persist one successful run's observed runtime."""
        ...

    def learned_priority(self, work_key: LaneWorkKey) -> int:
        """The lane's dispatch-order hint; 0 when nothing is known."""
        ...
