# pyright: strict
"""Port for the lane runtime-history learning loop.

The loop is deliberately backend-neutral: every execution backend
reports observed runtime through the lane outcome, and every backend's
submissions may consume the learned ordering. Burying it inside one
scheduler adapter would force each future backend to reinvent it;
floating it into clients would break the "logical names only" contract.

The store learns two dimensions of the same run:

- **How long** the lane executes, which orders dispatch (LPT).
- **How much CPU** it kept busy while executing, which sizes its
  admission request.

They share a run and a rolling window but not a population: every
backend reports a runtime, while only a backend whose measuring
conditions match the consumer of the number reports busy cores. A
lane can therefore have a known runtime and an unknown CPU demand,
and that asymmetry is normal, not a gap to paper over.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.lane_execution import LaneWorkKey


class LaneRuntimeHistoryError(RuntimeError):
    """The history store itself is broken — corrupt state, not absence.

    Part of the port's vocabulary rather than any one adapter's, like
    ``LaneDispatchJournalError``: every consumer of the loop has to
    handle "the store is garbage" no matter which store it is talking
    to. Raised loudly instead of degrading to naive behavior — a corrupt
    store means something wrote garbage, and hiding that behind a silent
    reset would hide the writer's bug. Absence is never an error: an
    empty store is the naive first run.
    """


@runtime_checkable
class LaneRuntimeHistory(Protocol):
    """Learn each lane's cost; answer with dispatch and sizing hints.

    The contract of the loop:

    - Only successful runs are recorded — a failed run's duration is
      the failure's, not the lane's.
    - ``learned_priority`` returns the rolling median of recorded
      runtimes in whole seconds, and 0 for a lane with no history:
      zero history is the naive first run, by design, not an error.
    - The returned value is an ordering *rank* (longer lanes sort
      first — the LPT heuristic), not a reservation: consumers must
      not treat it as a promised duration.
    - ``learned_busy_cores`` returns the rolling median of recorded
      busy-cores figures, or ``None`` when the lane has never been
      measured. ``None`` is not zero: it means "unknown", and the
      caller's declared seed answers instead.
    """

    def record_success(
        self,
        work_key: LaneWorkKey,
        runtime_seconds: float,
        busy_cores: float | None,
    ) -> None:
        """Persist one successful run's observed cost.

        ``busy_cores`` is ``None`` when this run was not measured; the
        runtime is still recorded. Passing it explicitly is required:
        a caller must decide whether it has a measurement, never
        default its way into recording a zero it did not observe.
        """
        ...

    def learned_priority(self, work_key: LaneWorkKey) -> int:
        """The lane's dispatch-order hint; 0 when nothing is known."""
        ...

    def learned_busy_cores(self, work_key: LaneWorkKey) -> float | None:
        """The lane's measured CPU demand; None when nothing is known."""
        ...
