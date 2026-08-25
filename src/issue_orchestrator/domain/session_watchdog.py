"""Monotonic authority and diagnostic wall clock for session watchdogs."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SessionWatchdogClock:
    """Required clocks with monotonic time authoritative for elapsed runtime."""

    wall_now: Callable[[], datetime]
    monotonic_now: Callable[[], float]

    def __post_init__(self) -> None:
        if not callable(self.wall_now):
            raise ValueError("SessionWatchdogClock.wall_now must be callable")
        if not callable(self.monotonic_now):
            raise ValueError("SessionWatchdogClock.monotonic_now must be callable")

    def require_monotonic_now(self) -> float:
        """Return one valid monotonic observation or fail at its source."""
        observed = self.monotonic_now()
        if type(observed) is not float or not math.isfinite(observed):
            raise ValueError(
                "SessionWatchdogClock.monotonic_now must return a finite float"
            )
        return observed


SYSTEM_SESSION_WATCHDOG_CLOCK = SessionWatchdogClock(
    wall_now=datetime.now,
    monotonic_now=time.monotonic,
)
