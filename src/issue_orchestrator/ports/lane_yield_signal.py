# pyright: strict
"""Port for publishing a cooperative lane's yield state.

A ``cooperative`` lane (see
:class:`~issue_orchestrator.domain.lane_execution.LaneSuspendability`)
may be frozen by machine-load backoff only at safe points it
advertises itself. This port is the raw publication channel; the
POLICY of when publications must succeed — the acknowledged-transition
state machine — has exactly one owner,
:class:`issue_orchestrator.execution.lane_yield.AcknowledgedLaneYield`
(A2/A3, #7134 review). Nothing else may talk to a transport directly.

The asymmetry the owner enforces: a failed raise lands in
confirmed-UNKNOWN — the publication may have applied, so degrading
is forbidden and only an acknowledged False recovers. Lowering to
unsafe is a CORRECTNESS boundary — protected work must not start
until the transport acknowledges the lane is unfreezable again; a
failed lower over a possibly-True published state is fatal
(:class:`LaneYieldError`), never a degradation. The only path to
never-eligible is an acknowledged, provably-False external state.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class LaneYieldError(RuntimeError):
    """The lane may be advertised safe and cannot be lowered.

    Raised by the owner when unsafe work is about to start (or the
    process is ending) while the published state is possibly True and
    the transport cannot confirm a False: proceeding could let the
    pool freeze a live provider turn — a hard, visible error, never a
    degradation.
    """


@runtime_checkable
class LaneYieldTransport(Protocol):
    """Publish one state; report whether it was acknowledged."""

    def publish(self, safe: bool) -> bool:
        """True only when the backend confirmed the new state."""
        ...
