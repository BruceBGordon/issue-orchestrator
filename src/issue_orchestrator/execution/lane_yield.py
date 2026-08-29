# pyright: strict
"""The acknowledged-transition owner for cooperative lane yielding.

One state machine owns every rule about when a yield publication must
succeed (A2/A3, #7134 rounds one and two). The structural invariant
that gates EVERY path (round two's sharpening): **degrading to
never-eligible is reachable only while the external state is
confirmed False.** Once a True has been published — or even attempted,
since a failed attempt leaves the external value uncertain — any
failure to return to a confirmed False is fatal, visibly and stickily,
from that point forward. There is no ordering of events that closes
the owner quietly over a possible True.

The full transition table (state = last acknowledged external value ∈
{unknown, False, True}, plus the closed and fatal latches):

    state      event        outcome
    unknown    lower ok     confirmed False
    unknown    lower fail   FATAL (a predecessor's True may stand)
    unknown    raise *      FATAL (no confirmed False base)
    False      lower ok     confirmed False (re-confirmation)
    False      lower fail   CLOSED, loud (the only degradation: the
                            external value is provably False, so a
                            dead transport costs safe-windows only)
    False      raise ok     confirmed True
    False      raise fail   confirmed UNKNOWN, loud (the publish may
                            have applied; the next lower must confirm
                            False or die — recovery is one
                            acknowledged False away)
    True       lower ok     confirmed False
    True       lower fail   FATAL (a live True cannot be lowered)
    True       raise *      FATAL (raise without a False base is a
                            protocol violation)
    CLOSED     lower/raise  no-op (closed implies external False and
                            no further publications, forever)
    FATAL      lower/raise  re-raise (sticky: nothing proceeds past a
                            fatal condition)

Rest state between processes is False by design: sessions end lowered,
and a crash that strands True is caught by the successor's opening
``lower()`` failing hard rather than the successor running exposed.
"""

from __future__ import annotations

import sys

from ..ports.lane_yield_signal import LaneYieldError, LaneYieldTransport

_UNKNOWN = "unknown"
_FALSE = "false"
_TRUE = "true"


class InertLaneYield:
    """No consumer for yield state here; both directions are no-ops.

    Legitimate ONLY outside a scheduler job (A3, #7134 round two): a
    developer's local pytest has no job ad to protect. Inside a job,
    an unresolvable transport must be fatal instead — the plugin owns
    that distinction at composition.
    """

    def lower(self) -> None:
        return

    def raise_safe(self) -> None:
        return


class AcknowledgedLaneYield:
    """Track the last acknowledged publication; enforce the table."""

    def __init__(self, transport: LaneYieldTransport) -> None:
        self._transport = transport
        self._confirmed = _UNKNOWN
        self._closed = False
        self._fatal: LaneYieldError | None = None

    def lower(self) -> None:
        self._reraise_if_fatal()
        if self._closed:
            return
        if self._transport.publish(False):
            self._confirmed = _FALSE
            return
        if self._confirmed == _FALSE:
            # The only degradation in the table: external value is
            # provably False, so losing the transport costs only
            # future safe-windows, never correctness.
            self._close_over_confirmed_false()
            return
        raise self._become_fatal(
            "the lane's external state is "
            f"{'unknown - a predecessor may have advertised safe' if self._confirmed == _UNKNOWN else 'advertised safe'}"
            " and the transport cannot lower it; refusing to proceed "
            "into protected work"
        )

    def raise_safe(self) -> None:
        self._reraise_if_fatal()
        if self._closed:
            return
        if self._confirmed != _FALSE:
            # Publishing True over anything but a confirmed False base
            # would make the next lower unprovable by construction —
            # the caller protocol (lower before raise) was violated.
            raise self._become_fatal(
                "raise_safe called without a confirmed-False base "
                f"(state: {self._confirmed})"
            )
        if self._transport.publish(True):
            self._confirmed = _TRUE
            return
        # The attempt may have applied: the external value is now
        # uncertain, which forbids degradation. Recovery is exactly one
        # acknowledged False away; failing that, the next lower is
        # fatal per the table.
        self._confirmed = _UNKNOWN
        print(
            "lane-yield: publishing a safe window failed and the external "
            "state is now uncertain; the next unsafe boundary must "
            "confirm False or the lane fails",
            file=sys.stderr,
        )

    def _close_over_confirmed_false(self) -> None:
        # Structural guard for the round-two invariant: closing is
        # only expressible here, and only from confirmed False.
        assert self._confirmed == _FALSE
        self._closed = True
        print(
            "lane-yield: transport failed while re-confirming an "
            "already-False state; the lane runs never-eligible for "
            "freezing from here on (external state remains unfreezable)",
            file=sys.stderr,
        )

    def _become_fatal(self, detail: str) -> LaneYieldError:
        error = LaneYieldError(f"lane-yield: {detail}")
        self._fatal = error
        return error

    def _reraise_if_fatal(self) -> None:
        if self._fatal is not None:
            raise self._fatal
