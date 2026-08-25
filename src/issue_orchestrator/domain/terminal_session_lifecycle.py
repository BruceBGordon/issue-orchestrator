"""Typed lifecycle facts for subprocess-backed terminal sessions."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TerminalSessionWatcherPolicy:
    """Deadlock watchdog for the thread that owns PTY reaping and finalization."""

    shutdown_timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.shutdown_timeout_seconds) is not float
            or not math.isfinite(self.shutdown_timeout_seconds)
            or self.shutdown_timeout_seconds <= 0
        ):
            raise ValueError(
                "TerminalSessionWatcherPolicy.shutdown_timeout_seconds must be "
                "finite and positive"
            )


@dataclass(frozen=True, slots=True)
class TerminalSessionWatcherCompleted:
    """The watcher proved that PTY reaping and output finalization completed."""

    session_name: str
    process_id: int

    def __post_init__(self) -> None:
        _require_watcher_identity(self.session_name, self.process_id)


@dataclass(frozen=True, slots=True)
class TerminalSessionWatcherTimedOut:
    """The watcher remained alive after its isolated shutdown watchdog."""

    session_name: str
    process_id: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        _require_watcher_identity(self.session_name, self.process_id)
        if (
            type(self.timeout_seconds) is not float
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError(
                "TerminalSessionWatcherTimedOut.timeout_seconds must be finite "
                "and positive"
            )


@dataclass(frozen=True, slots=True)
class TerminalSessionWatcherFailed:
    """The watcher stopped after PTY reaping or finalization raised."""

    session_name: str
    process_id: int
    error: BaseException

    def __post_init__(self) -> None:
        _require_watcher_identity(self.session_name, self.process_id)


TerminalSessionWatcherOutcome = (
    TerminalSessionWatcherCompleted
    | TerminalSessionWatcherTimedOut
    | TerminalSessionWatcherFailed
)


def _require_watcher_identity(session_name: str, process_id: int) -> None:
    if type(session_name) is not str or not session_name:
        raise ValueError("terminal watcher session_name must not be empty")
    if type(process_id) is not int or process_id <= 1:
        raise ValueError("terminal watcher process_id must be an integer above 1")
