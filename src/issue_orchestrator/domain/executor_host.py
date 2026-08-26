"""Strongly typed host observations used by executor admission policy."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutorHostCpuUtilization:
    """Whole-host CPU utilization measured over one explicit interval."""

    busy_percent: float
    observation_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.busy_percent) is not float
            or not math.isfinite(self.busy_percent)
            or not 0 <= self.busy_percent <= 100
        ):
            raise ValueError(
                "ExecutorHostCpuUtilization.busy_percent must be finite and in "
                "[0, 100]"
            )
        if (
            type(self.observation_seconds) is not float
            or not math.isfinite(self.observation_seconds)
            or self.observation_seconds <= 0
        ):
            raise ValueError(
                "ExecutorHostCpuUtilization.observation_seconds must be finite "
                "and positive"
            )
