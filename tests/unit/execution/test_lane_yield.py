"""The acknowledged-transition owner: lowering is correctness, raising
is a hint, and every ambiguous state lands on the not-frozen side."""

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


def test_acknowledged_lifecycle_tracks_state() -> None:
    transport = _ScriptedTransport([True, True, True])
    owner = AcknowledgedLaneYield(transport)
    owner.lower()
    owner.raise_safe()
    owner.lower()
    assert transport.published == [False, True, False]


def test_lower_failure_with_no_confirmed_state_is_a_hard_error() -> None:
    """A predecessor process may have left True; an unacknowledged
    lower must refuse to let protected work start (A2, #7134)."""
    owner = AcknowledgedLaneYield(_ScriptedTransport([False]))
    with pytest.raises(LaneYieldError, match="cannot lower"):
        owner.lower()


def test_lower_failure_after_a_confirmed_true_is_a_hard_error() -> None:
    owner = AcknowledgedLaneYield(_ScriptedTransport([True, True, False]))
    owner.lower()
    owner.raise_safe()
    with pytest.raises(LaneYieldError, match="cannot lower"):
        owner.lower()


def test_lower_failure_on_a_provably_false_state_degrades_loudly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Re-confirming an already-False state costs only future safe
    windows — one loud line, then never-eligible for the rest of the
    process, with no further transport traffic."""
    transport = _ScriptedTransport([True, False])
    owner = AcknowledgedLaneYield(transport)
    owner.lower()
    owner.lower()
    assert "never-eligible" in capsys.readouterr().err
    owner.lower()
    owner.raise_safe()
    assert transport.published == [False, False]


def test_raise_failure_degrades_loudly_and_keeps_the_false_base(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = _ScriptedTransport([True, False])
    owner = AcknowledgedLaneYield(transport)
    owner.lower()
    owner.raise_safe()
    assert "never-eligible" in capsys.readouterr().err
    # Closed: no more traffic, and the last acknowledged state is the
    # safe-to-proceed False, so later lowers are silent no-ops.
    owner.lower()
    assert transport.published == [False, True]


def test_raise_without_a_confirmed_false_base_goes_never_eligible(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Raising over an unacknowledged base would make the next lower
    unprovable — the owner refuses the publication entirely."""
    transport = _ScriptedTransport([])
    owner = AcknowledgedLaneYield(transport)
    owner.raise_safe()
    assert transport.published == []
    assert "never-eligible" in capsys.readouterr().err


def test_inert_owner_is_a_total_no_op() -> None:
    inert = InertLaneYield()
    inert.lower()
    inert.raise_safe()
