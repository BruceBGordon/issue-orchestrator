"""The acknowledged-transition owner, tested transition by transition.

Round-two invariant (#7134 A2): never-eligible degradation is
reachable ONLY from a confirmed-False external state. Once a True has
been published — or attempted, since a failed attempt leaves the
external value uncertain — any failure to return to confirmed False
is fatal, visibly and stickily, under every ordering."""

from __future__ import annotations

import pytest

from issue_orchestrator.execution.lane_yield import (
    AcknowledgedLaneYield,
    InertLaneYield,
)
from issue_orchestrator.ports.lane_yield_signal import LaneYieldError


class _ScriptedTransport:
    """Publishes succeed per a script; records every attempt."""

    def __init__(self, results: list[bool]) -> None:
        self._results = results
        self.published: list[bool] = []

    def publish(self, safe: bool) -> bool:
        self.published.append(safe)
        return self._results.pop(0)


# --- from UNKNOWN (construction; a predecessor may have left True) ---


def test_unknown_lower_ok_confirms_false() -> None:
    transport = _ScriptedTransport([True])
    AcknowledgedLaneYield(transport).lower()
    assert transport.published == [False]


def test_unknown_lower_fail_is_fatal() -> None:
    """fail-during-first-lower: the predecessor's possible True."""
    owner = AcknowledgedLaneYield(_ScriptedTransport([False]))
    with pytest.raises(LaneYieldError, match="predecessor may have"):
        owner.lower()


def test_unknown_raise_is_fatal_protocol_violation() -> None:
    transport = _ScriptedTransport([])
    owner = AcknowledgedLaneYield(transport)
    with pytest.raises(LaneYieldError, match="without a confirmed-False"):
        owner.raise_safe()
    assert transport.published == []


# --- from confirmed FALSE ---


def test_false_lower_ok_reconfirms() -> None:
    """double-lower: both acknowledged, state stays provable."""
    transport = _ScriptedTransport([True, True])
    owner = AcknowledgedLaneYield(transport)
    owner.lower()
    owner.lower()
    assert transport.published == [False, False]


def test_false_lower_fail_is_the_only_degradation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """External value provably False: closing costs safe-windows only.
    Closed is terminal and silent — no further transport traffic."""
    transport = _ScriptedTransport([True, False])
    owner = AcknowledgedLaneYield(transport)
    owner.lower()
    owner.lower()
    assert "never-eligible" in capsys.readouterr().err
    owner.lower()
    owner.raise_safe()
    assert transport.published == [False, False]


def test_false_raise_ok_confirms_true() -> None:
    transport = _ScriptedTransport([True, True])
    owner = AcknowledgedLaneYield(transport)
    owner.lower()
    owner.raise_safe()
    assert transport.published == [False, True]


def test_false_raise_fail_goes_uncertain_not_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """fail-during-raise: the publish may have applied, so the
    external value is uncertain — degradation is FORBIDDEN from here;
    the owner stays open and demands a confirmed False next."""
    transport = _ScriptedTransport([True, False, True])
    owner = AcknowledgedLaneYield(transport)
    owner.lower()
    owner.raise_safe()
    assert "uncertain" in capsys.readouterr().err
    # Recovery: one acknowledged False away.
    owner.lower()
    assert transport.published == [False, True, False]


def test_false_raise_fail_then_lower_fail_is_fatal() -> None:
    """Interleaved failures: uncertain external value + unlowerable =
    fatal, never a quiet close over a possible True (the round-two
    probe's exact shape, one step earlier)."""
    owner = AcknowledgedLaneYield(_ScriptedTransport([True, False, False]))
    owner.lower()
    owner.raise_safe()
    with pytest.raises(LaneYieldError, match="unknown"):
        owner.lower()


# --- from confirmed TRUE (the round-two probe) ---


def test_true_lower_ok_returns_to_false() -> None:
    transport = _ScriptedTransport([True, True, True])
    owner = AcknowledgedLaneYield(transport)
    owner.lower()
    owner.raise_safe()
    owner.lower()
    assert transport.published == [False, True, False]


def test_true_lower_fail_is_fatal_never_never_eligible() -> None:
    """THE round-two probe ([False, True, failed-lower]): a live True
    that cannot be lowered must never close the owner quietly."""
    owner = AcknowledgedLaneYield(_ScriptedTransport([True, True, False]))
    owner.lower()
    owner.raise_safe()
    with pytest.raises(LaneYieldError, match="advertised safe"):
        owner.lower()


def test_true_raise_is_fatal_protocol_violation() -> None:
    owner = AcknowledgedLaneYield(_ScriptedTransport([True, True]))
    owner.lower()
    owner.raise_safe()
    with pytest.raises(LaneYieldError, match="without a confirmed-False"):
        owner.raise_safe()


# --- latches ---


def test_fatal_is_sticky_for_both_operations() -> None:
    """raise-after-fatal and lower-after-fatal both re-raise; nothing
    proceeds past a fatal condition and nothing publishes."""
    transport = _ScriptedTransport([False])
    owner = AcknowledgedLaneYield(transport)
    with pytest.raises(LaneYieldError):
        owner.lower()
    with pytest.raises(LaneYieldError):
        owner.raise_safe()
    with pytest.raises(LaneYieldError):
        owner.lower()
    assert transport.published == [False]


def test_inert_owner_is_a_total_no_op() -> None:
    inert = InertLaneYield()
    inert.lower()
    inert.raise_safe()
