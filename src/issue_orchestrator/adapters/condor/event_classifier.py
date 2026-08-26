# pyright: strict
"""Inbound half of the anti-corruption layer: job event log → typed state.

The scheduler's user-log format (numeric event codes, banner lines,
free-text bodies) is parsed here and nowhere else. The classifier is
pure: it reads the complete log text each observation and returns one
closed lifecycle state, so it is unit-testable against captured logs
without any scheduler installed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_EVENT_BANNER = re.compile(r"^(\d{3}) \(\d+\.\d+\.\d+\) ", re.MULTILINE)
_RETURN_VALUE = re.compile(r"Normal termination \(return value (-?\d+)\)")
_TERMINATION_SIGNAL = re.compile(r"Abnormal termination \(signal (\d+)\)")

_SUBMITTED = "000"
_EXECUTING = "001"
_TERMINATED = "005"
_ABORTED = "009"
_HELD = "012"

# The body of an abort event names the expression that removed the job;
# this marker distinguishes our compiled deadline from any other removal.
_DEADLINE_REMOVAL_MARKER = "PeriodicRemove"


@dataclass(frozen=True, slots=True)
class LaneJobPending:
    """Submitted (or not yet observably submitted); not running yet."""


@dataclass(frozen=True, slots=True)
class LaneJobRunning:
    """The job has started executing."""


@dataclass(frozen=True, slots=True)
class LaneJobExited:
    """The job ran to its own exit."""

    exit_code: int


@dataclass(frozen=True, slots=True)
class LaneJobKilledBySignal:
    """The job died to a signal while running."""

    signal_number: int


@dataclass(frozen=True, slots=True)
class LaneJobDeadlineRemoved:
    """The compiled runtime deadline removed the job."""


@dataclass(frozen=True, slots=True)
class LaneJobRemoved:
    """The job was removed for a reason other than its deadline."""

    detail: str


@dataclass(frozen=True, slots=True)
class LaneJobFaulted:
    """The scheduler put the job in a non-running fault state (held)."""

    detail: str


LaneJobState = (
    LaneJobPending
    | LaneJobRunning
    | LaneJobExited
    | LaneJobKilledBySignal
    | LaneJobDeadlineRemoved
    | LaneJobRemoved
    | LaneJobFaulted
)


def classify_event_log(log_text: str) -> LaneJobState:
    """Return the job's current lifecycle state from its full event log."""
    if type(log_text) is not str:
        raise ValueError("classify_event_log requires the log text")
    events = _split_events(log_text)
    state: LaneJobState = LaneJobPending()
    for code, body in events:
        if code == _EXECUTING:
            state = LaneJobRunning()
        elif code == _TERMINATED:
            state = _classify_termination(body)
        elif code == _ABORTED:
            if _DEADLINE_REMOVAL_MARKER in body:
                state = LaneJobDeadlineRemoved()
            else:
                state = LaneJobRemoved(_first_body_line(body))
        elif code == _HELD:
            state = LaneJobFaulted(_first_body_line(body))
        elif code == _SUBMITTED:
            continue
        # Every other event code (image size, usage updates, …) is
        # informational and does not change the lifecycle state.
    return state


def _split_events(log_text: str) -> list[tuple[str, str]]:
    banners = list(_EVENT_BANNER.finditer(log_text))
    events: list[tuple[str, str]] = []
    for index, banner in enumerate(banners):
        end = banners[index + 1].start() if index + 1 < len(banners) else len(log_text)
        events.append((banner.group(1), log_text[banner.start() : end]))
    return events


def _classify_termination(body: str) -> LaneJobState:
    returned = _RETURN_VALUE.search(body)
    if returned is not None:
        return LaneJobExited(int(returned.group(1)))
    signaled = _TERMINATION_SIGNAL.search(body)
    if signaled is not None:
        return LaneJobKilledBySignal(int(signaled.group(1)))
    raise ValueError(
        "job termination event carried neither a return value nor a signal: "
        f"{_first_body_line(body)!r}"
    )


def _first_body_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not _EVENT_BANNER.match(stripped):
            return stripped
    return body.splitlines()[0].strip() if body.splitlines() else ""
