# pyright: strict
"""Policies and executable identity for an in-group containment sentinel."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProcessGroupSentinelPolicy:
    """Bounded courtesy interval before unconditional in-group SIGKILL."""

    graceful_shutdown_seconds: float
    startup_timeout_seconds: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("graceful_shutdown_seconds", self.graceful_shutdown_seconds),
            ("startup_timeout_seconds", self.startup_timeout_seconds),
        ):
            if type(value) is not float or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"ProcessGroupSentinelPolicy.{field_name} must be finite "
                    "and positive"
                )


@dataclass(frozen=True, slots=True)
class ProcessGroupSentinelProgram:
    """Exact executable prefix for the isolated sentinel child role."""

    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.arguments) is not tuple or not self.arguments:
            raise ValueError(
                "ProcessGroupSentinelProgram.arguments must be a non-empty tuple"
            )
        if any(
            type(argument) is not str or not argument or "\0" in argument
            for argument in self.arguments
        ):
            raise ValueError(
                "ProcessGroupSentinelProgram.arguments must be non-empty, "
                "NUL-free strings"
            )
        if not Path(self.arguments[0]).is_absolute():
            raise ValueError(
                "ProcessGroupSentinelProgram executable must be absolute"
            )


@dataclass(frozen=True, slots=True)
class ProcessGroupSentinelParentLifetime:
    """Read endpoint whose closure means the owning parent has disappeared."""

    read_file_descriptor: int

    def __post_init__(self) -> None:
        if (
            type(self.read_file_descriptor) is not int
            or self.read_file_descriptor < 0
        ):
            raise ValueError(
                "ProcessGroupSentinelParentLifetime.read_file_descriptor must "
                "be non-negative"
            )
