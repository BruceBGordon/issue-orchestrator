"""Typed policy for the terminal process-group ownership wrapper."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TerminalSessionOwnerPolicy:
    """Bound startup and graceful in-group containment."""

    startup_timeout_seconds: float
    graceful_shutdown_seconds: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("startup_timeout_seconds", self.startup_timeout_seconds),
            ("graceful_shutdown_seconds", self.graceful_shutdown_seconds),
        ):
            if type(value) is not float or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"TerminalSessionOwnerPolicy.{field_name} must be finite "
                    "and positive"
                )
