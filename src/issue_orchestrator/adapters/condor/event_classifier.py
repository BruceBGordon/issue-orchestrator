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
from datetime import datetime

_EVENT_BANNER = re.compile(
    r"^(?P<code>\d{3}) \(\d+\.\d+\.\d+\) "
    r"(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ",
    re.MULTILINE,
)
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
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
    """The job ran to its own exit.

    ``runtime_seconds`` is the scheduler's own record: the span from
    the (last) execute event to the terminal event, read from the
    event-log timestamps — never from when this process happened to
    poll. Observation lag must not masquerade as execution time.
    """

    exit_code: int
    runtime_seconds: float


@dataclass(frozen=True, slots=True)
class LaneJobKilledBySignal:
    """The job died to a signal while running."""

    signal_number: int
    runtime_seconds: float


@dataclass(frozen=True, slots=True)
class LaneJobDeadlineRemoved:
    """The compiled runtime deadline removed the job."""

    runtime_seconds: float


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
    span = _ExecutionSpan()
    for code, occurred_at, body in events:
        if code == _EXECUTING:
            span.executing(occurred_at)
            state = LaneJobRunning()
        elif code == _SUSPENDED:
            span.suspend(occurred_at)
            state = LaneJobSuspended()
        elif code == _UNSUSPENDED:
            span.resume(occurred_at)
            state = LaneJobRunning()
        elif code == _TERMINATED:
            state = _classify_termination(body, span.runtime_at(occurred_at))
        elif code == _ABORTED:
            if _DEADLINE_REMOVAL_MARKER in body:
                state = LaneJobDeadlineRemoved(span.runtime_at(occurred_at))
            else:
                state = LaneJobRemoved(_first_body_line(body))
        elif code == _HELD:
            state = LaneJobFaulted(_first_body_line(body))
        # Submission and every other event code (image size, usage
        # updates, …) are informational and do not change the state.
    return state


class _ExecutionSpan:
    """Executing-time bookkeeping from the log's own timestamps.

    The runtime a terminal state reports is the scheduler's record —
    (last) execute → terminal, minus every suspended interval — never
    this process's poll clock, whose observation lag would masquerade
    as execution time. Second-granular, matching the log.
    """

    def __init__(self) -> None:
        self._execute_at: datetime | None = None
        self._suspended_at: datetime | None = None
        self._suspended_seconds = 0.0

    def executing(self, occurred_at: datetime) -> None:
        # The LAST execute anchors the span: a restarted job's final
        # execution is the runtime signal, not its false starts.
        self._execute_at = occurred_at
        self._suspended_at = None
        self._suspended_seconds = 0.0

    def suspend(self, occurred_at: datetime) -> None:
        self._suspended_at = occurred_at

    def resume(self, occurred_at: datetime) -> None:
        if self._suspended_at is None:
            raise ValueError(
                "job log records an unsuspend with no suspension open"
            )
        self._suspended_seconds += self._interval(
            self._suspended_at, occurred_at
        )
        self._suspended_at = None

    def runtime_at(self, terminal_at: datetime) -> float:
        if self._execute_at is None:
            raise ValueError(
                "job reached a terminal event with no execute event before it"
            )
        frozen = self._suspended_seconds
        if self._suspended_at is not None:
            # Terminal while frozen: the open suspension ends here.
            frozen += self._interval(self._suspended_at, terminal_at)
        runtime = self._interval(self._execute_at, terminal_at) - frozen
        if runtime < 0:
            raise ValueError(
                "job event log suspension intervals exceed the execution span"
            )
        return runtime

    @staticmethod
    def _interval(start: datetime, end: datetime) -> float:
        seconds = (end - start).total_seconds()
        if seconds < 0:
            raise ValueError(
                f"job event log timestamps run backwards: {start} after {end}"
            )
        return seconds


_RECORD_DELIMITER = re.compile(r"^\.\.\.\s*$", re.MULTILINE)


def _split_complete_events(log_text: str) -> list[tuple[str, datetime, str]]:
    """Yield only records the scheduler has finished writing.

    A record is complete when its terminating ``...`` line exists. The
    region after the last delimiter is a record in progress and is
    deliberately ignored.
    """
    events: list[tuple[str, datetime, str]] = []
    position = 0
    for delimiter in _RECORD_DELIMITER.finditer(log_text):
        record = log_text[position : delimiter.start()]
        position = delimiter.end()
        banner = _EVENT_BANNER.search(record)
        if banner is None:
            continue
        occurred_at = datetime.strptime(
            banner.group("timestamp"), _TIMESTAMP_FORMAT
        )
        events.append(
            (banner.group("code"), occurred_at, record[banner.start() :])
        )
    return events


def _classify_termination(body: str, runtime_seconds: float) -> LaneJobState:
    returned = _RETURN_VALUE.search(body)
    if returned is not None:
        return LaneJobExited(int(returned.group(1)), runtime_seconds)
    signaled = _TERMINATION_SIGNAL.search(body)
    if signaled is not None:
        return LaneJobKilledBySignal(int(signaled.group(1)), runtime_seconds)
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
