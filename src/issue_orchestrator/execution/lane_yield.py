# pyright: strict
"""The acknowledged-transition owner for cooperative lane yielding.

One state machine owns every rule about when a yield publication must
succeed (A2/A3, #7134 review); transports publish, callers call
``lower``/``raise_safe``, and nobody else holds state:

- ``lower()`` — the correctness direction. Unsafe (protected) work may
  begin only after the transport ACKNOWLEDGES False. If the published
  state may be True (never confirmed, or a raise succeeded since the
  last confirmed False) and the transport cannot lower it, that is a
  hard :class:`LaneYieldError` — visible in the lane output, never a
  degradation. Only when the state is PROVABLY already False does a
  failed re-confirmation degrade: one loud line, then the lane runs
  never-eligible for the rest of this process.
- ``raise_safe()`` — the hint direction. Failure cannot endanger the
  lane (the state stays at the last acknowledged False), so it
  degrades loudly to never-eligible instead of raising.

The rest state between processes is False by design: sessions end
lowered, so a successor process inherits an unfreezable job, and a
crash that strands True is caught by the successor's own opening
``lower()`` failing hard rather than silently running exposed.
"""

from __future__ import annotations

import sys

from ..ports.lane_yield_signal import LaneYieldError, LaneYieldTransport


class InertLaneYield:
    """No consumer for yield state here; both directions are no-ops."""

    def lower(self) -> None:
        return

    def raise_safe(self) -> None:
        return


class AcknowledgedLaneYield:
    """Track the last acknowledged publication; enforce the asymmetry."""

    def __init__(self, transport: LaneYieldTransport) -> None:
        self._transport = transport
        # None = never confirmed anything (a predecessor in this job
        # may have published True); True/False = last acknowledged.
        self._confirmed: bool | None = None
        self._closed = False

    def lower(self) -> None:
        if self._closed:
            return
        if self._transport.publish(False):
            self._confirmed = False
            return
        if self._confirmed is False:
            # Provably already unfreezable: losing the transport now
            # costs only future safe-windows, not correctness.
            self._go_never_eligible("re-confirming an already-False state")
            return
        raise LaneYieldError(
            "the lane may be advertised safe to freeze and the transport "
            "cannot lower it — refusing to proceed into protected work "
            f"(last acknowledged state: {self._confirmed!r})"
        )

    def raise_safe(self) -> None:
        if self._closed:
            return
        if self._confirmed is not False:
            # Never raise without a confirmed False beneath us: an
            # unacknowledged base makes the next lower() unprovable.
            self._go_never_eligible("raising without a confirmed False base")
            return
        if self._transport.publish(True):
            self._confirmed = True
            return
        self._go_never_eligible("publishing a safe window")

    def _go_never_eligible(self, doing: str) -> None:
        self._closed = True
        print(
            f"lane-yield: transport failed while {doing}; the lane runs "
            "never-eligible for freezing from here on (state remains "
            "unfreezable)",
            file=sys.stderr,
        )
