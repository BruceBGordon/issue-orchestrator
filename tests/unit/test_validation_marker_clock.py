"""Public behavior tests for monotonic Make marker timing."""

from __future__ import annotations

import pytest

from issue_orchestrator.entrypoints.cli_tools.validation_marker_clock import (
    ValidationMarkerClock,
)


def test_elapsed_uses_monotonic_samples() -> None:
    clock = ValidationMarkerClock(lambda: 8_500_000_000)

    assert clock.now_nanoseconds() == 8_500_000_000
    assert clock.elapsed_seconds(1_000_000_000, 8_500_000_000) == 7


def test_elapsed_fails_if_injected_monotonic_clock_rolls_back() -> None:
    with pytest.raises(RuntimeError, match="moved backwards"):
        ValidationMarkerClock.elapsed_seconds(8_500_000_000, 1_000_000_000)
