# pyright: strict
"""Port for a running lane's cooperative-yield advertisement.

A ``cooperative`` lane (see
:class:`~issue_orchestrator.domain.lane_execution.LaneSuspendability`)
may be frozen by machine-load backoff only at safe points it
advertises itself. This port is the lane-side half of that contract:
the running workload calls ``advertise(True)`` when interruption is
safe (between test items, between stages) and ``advertise(False)``
when it is not. The scheduling backend consumes the advertisement; the
direct backend has nothing to consume and an inert signal is correct.

Advertisement is a scheduling hint, not a correctness action: the
fail-safe direction is built into the consumer (an advertisement that
never arrives means never-frozen), so implementations are permitted to
degrade to inert on infrastructure failure — loudly, once — rather
than fail the lane. This is a deliberate, documented exception to the
fail-fast default, mirroring the fire-and-forget EventSink.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LaneYieldSignal(Protocol):
    """Advertise whether this moment is safe to interrupt."""

    def advertise(self, safe: bool) -> None:
        """Publish the lane's current interruptibility."""
        ...
