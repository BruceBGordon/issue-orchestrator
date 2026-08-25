"""Typed requests and policy for containing a terminal session."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .executor import ExecutorInteractiveSessionCancellation


@dataclass(frozen=True, slots=True)
class TerminalSessionProcess:
    """Persistable identity required to contain one terminal session."""

    process_id: int
    executor_cancellation: ExecutorInteractiveSessionCancellation

    def __post_init__(self) -> None:
        if type(self.process_id) is not int or self.process_id <= 1:
            raise ValueError(
                "TerminalSessionProcess.process_id must be an integer above 1"
            )
        if (
            type(self.executor_cancellation)
            is not ExecutorInteractiveSessionCancellation
        ):
            raise ValueError(
                "TerminalSessionProcess.executor_cancellation must be an "
                "ExecutorInteractiveSessionCancellation"
            )


@dataclass(frozen=True, slots=True)
class TerminalSessionTerminationPolicy:
    """Bounded courtesy and containment waits for a terminal session."""

    graceful_shutdown_seconds: float
    forceful_shutdown_seconds: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("graceful_shutdown_seconds", self.graceful_shutdown_seconds),
            ("forceful_shutdown_seconds", self.forceful_shutdown_seconds),
        ):
            if type(value) is not float or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"TerminalSessionTerminationPolicy.{field_name} must be "
                    "finite and positive"
                )
