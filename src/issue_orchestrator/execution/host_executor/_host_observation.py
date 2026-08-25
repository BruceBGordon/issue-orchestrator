# pyright: strict
"""Typed host-load observation for executor diagnostics."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutorHostLoadObservation:
    """Operating-system runnable-load averages at one instant."""

    one_minute: float
    five_minutes: float
    fifteen_minutes: float

    def __post_init__(self) -> None:
        for name, value in (
            ("one_minute", self.one_minute),
            ("five_minutes", self.five_minutes),
            ("fifteen_minutes", self.fifteen_minutes),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(
                    f"ExecutorHostLoadObservation.{name} must be finite and "
                    "non-negative"
                )


def observe_host_load() -> ExecutorHostLoadObservation:
    """Read host load averages, failing if the operating system cannot."""
    try:
        one_minute, five_minutes, fifteen_minutes = os.getloadavg()
    except OSError as exc:
        raise RuntimeError("cannot observe host load for executor diagnostics") from exc
    return ExecutorHostLoadObservation(
        one_minute=one_minute,
        five_minutes=five_minutes,
        fifteen_minutes=fifteen_minutes,
    )
