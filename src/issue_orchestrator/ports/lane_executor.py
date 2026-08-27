# pyright: strict
"""Port for validation-lane execution backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.lane_execution import LaneCommand, LaneOutcome, LaneResources


@runtime_checkable
class LaneExecutor(Protocol):
    """Run one validation lane to a truthful terminal outcome.

    Contract every adapter must honor, proven by the shared contract
    suite in ``tests/unit/lane_executor_contract.py``:

    - The lane runs in ``command.working_directory`` with the caller's
      environment.
    - Lane stdout/stderr reach this process's stdout/stderr while the
      lane runs (streamed, not buffered until completion).
    - A lane exceeding ``command.deadline`` has its entire process tree
      terminated and reports :class:`LaneTimedOut` (exit code 124).
    - Backend faults raise :class:`LaneExecutorError` subclasses; they
      are never encoded as lane exit codes.
    """

    def run(self, command: LaneCommand, resources: LaneResources) -> LaneOutcome:
        """Execute the lane and return its closed terminal outcome."""
        ...
