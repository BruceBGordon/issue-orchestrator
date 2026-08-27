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
_SUSPENDED = "010"
_UNSUSPENDED = "011"
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
class LaneJobSuspended:
    """The job is frozen by machine-load backoff; it will resume.

    Not a fault and not terminal: the executor must keep waiting, and
    frozen time is charged to neither the lane's deadline nor its
    observed runtime.
    """


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
    | LaneJobSuspended
    | LaneJobExited
    | LaneJobKilledBySignal
    | LaneJobDeadlineRemoved
    | LaneJobRemoved
    | LaneJobFaulted
)


def classify_event_log(log_text: str) -> LaneJobState:
    """Return the job's current lifecycle state from its full event log.

    The scheduler writes records incrementally and this is read on a
    poll, so the tail of the text may be a record still being written.
    Only records closed by the scheduler's ``...`` delimiter are
    classified; an unfinished trailing record leaves the state at
    whatever the last complete record established. Without this, a poll
    landing between a terminal banner and its body lines would either
    raise (a termination with no verdict yet) or misclassify (an abort
    whose deadline-removal reason has not been written yet).
    """
    if type(log_text) is not str:
        raise ValueError("classify_event_log requires the log text")
    events = _split_complete_events(log_text)
    state: LaneJobState = LaneJobPending()
    for code, body in events:
        state = _transition(state, code, body)
    return state


# Events whose new state needs nothing from the body. Unsuspension
# returns to Running: suspension is a waiting interlude, not progress.
_BODYLESS_TRANSITIONS: dict[str, type[LaneJobRunning] | type[LaneJobSuspended]] = {
    _EXECUTING: LaneJobRunning,
    _SUSPENDED: LaneJobSuspended,
    _UNSUSPENDED: LaneJobRunning,
}


def _transition(state: LaneJobState, code: str, body: str) -> LaneJobState:
    bodyless = _BODYLESS_TRANSITIONS.get(code)
    if bodyless is not None:
        return bodyless()
    if code == _TERMINATED:
        return _classify_termination(body)
    if code == _ABORTED:
        if _DEADLINE_REMOVAL_MARKER in body:
            return LaneJobDeadlineRemoved()
        return LaneJobRemoved(_first_body_line(body))
    if code == _HELD:
        return LaneJobFaulted(_first_body_line(body))
    # Submission and every other event code (image size, usage
    # updates, …) are informational and do not change the state.
    return state


_RECORD_DELIMITER = re.compile(r"^\.\.\.\s*$", re.MULTILINE)


def _split_complete_events(log_text: str) -> list[tuple[str, str]]:
    """Yield only records the scheduler has finished writing.

    A record is complete when its terminating ``...`` line exists. The
    region after the last delimiter is a record in progress and is
    deliberately ignored.
    """
    events: list[tuple[str, str]] = []
    position = 0
    for delimiter in _RECORD_DELIMITER.finditer(log_text):
        record = log_text[position : delimiter.start()]
        position = delimiter.end()
        banner = _EVENT_BANNER.search(record)
        if banner is None:
            continue
        events.append((banner.group(1), record[banner.start() :]))
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
