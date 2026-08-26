# pyright: strict
"""One owner for command-terminal versus deadline observation precedence."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ...domain.executor import ExecutorDeadlineReason
from ...domain.executor_guardian import (
    ExecutorGuardianBoundedBudget,
    ExecutorGuardianBudget,
    ExecutorGuardianUnboundedBudget,
)
from ...ports.posix_process import PosixProcessHandle


@dataclass(frozen=True, slots=True)
class ExecutorGuardianCommandExitObserved:
    """The command exit was observed strictly before its deadline."""

    exit_code: int

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError("observed command exit_code must be an int")


@dataclass(frozen=True, slots=True)
class ExecutorGuardianCommandDeadlineObserved:
    """The deadline owned an at-or-after-boundary terminal observation."""

    reason: ExecutorDeadlineReason

    def __post_init__(self) -> None:
        if type(self.reason) is not ExecutorDeadlineReason:
            raise ValueError("observed command deadline reason must be typed")


@dataclass(frozen=True, slots=True)
class ExecutorGuardianCommandObservationFailed:
    """Terminal observation failed before an outcome could be established."""

    error_type: str
    error_repr: str

    def __post_init__(self) -> None:
        if type(self.error_type) is not str or not self.error_type:
            raise ValueError("command observation error_type must be non-empty")
        if type(self.error_repr) is not str or not self.error_repr:
            raise ValueError("command observation error_repr must be non-empty")


ExecutorGuardianCommandObservation = (
    ExecutorGuardianCommandExitObserved
    | ExecutorGuardianCommandDeadlineObserved
    | ExecutorGuardianCommandObservationFailed
)


@dataclass(frozen=True, slots=True)
class _ExecutorGuardianObservationPending:
    """No terminal fact was established during this poll cycle."""


_ExecutorGuardianObservationCycle = (
    _ExecutorGuardianObservationPending
    | ExecutorGuardianCommandExitObserved
    | ExecutorGuardianCommandDeadlineObserved
)


@dataclass(frozen=True, slots=True)
class _ExecutorGuardianDeadlinePending:
    """The bounded or unbounded command may continue."""


_ExecutorGuardianDeadlineObservation = (
    _ExecutorGuardianDeadlinePending | ExecutorGuardianCommandDeadlineObserved
)


@dataclass(frozen=True, slots=True)
class _ExecutorGuardianWaitPlanned:
    seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.seconds) is not float
            or not math.isfinite(self.seconds)
            or self.seconds <= 0.0
        ):
            raise ValueError("guardian wait must be finite and positive")


_ExecutorGuardianWaitDecision = (
    _ExecutorGuardianWaitPlanned | ExecutorGuardianCommandDeadlineObserved
)


@runtime_checkable
class ExecutorGuardianObservationClock(Protocol):
    """Clock and cooperative wait seam for deterministic terminal decisions."""

    def monotonic(self) -> float: ...

    def wait(self, seconds: float) -> None: ...


@runtime_checkable
class ExecutorGuardianGroupLiveness(Protocol):
    """Sentinel health authority consulted before every terminal observation."""

    def require_sentinel_alive(self) -> None: ...


def _require_observation_clock(value: object) -> ExecutorGuardianObservationClock:
    if not isinstance(value, ExecutorGuardianObservationClock):
        raise ValueError("guardian observation clock must implement its port")
    return value


def _require_process(value: object) -> PosixProcessHandle:
    if not isinstance(value, PosixProcessHandle):
        raise ValueError("guardian observation process must implement its port")
    return value


def _require_budget(value: object) -> ExecutorGuardianBudget:
    if type(value) is ExecutorGuardianBoundedBudget:
        return value
    if type(value) is ExecutorGuardianUnboundedBudget:
        return value
    raise ValueError("guardian observation budget must be typed")


def _require_group_liveness(value: object) -> ExecutorGuardianGroupLiveness:
    if not isinstance(value, ExecutorGuardianGroupLiveness):
        raise ValueError("guardian group liveness must implement its port")
    return value


class SystemExecutorGuardianObservationClock:
    """Production monotonic clock and cooperative wait adapter."""

    def monotonic(self) -> float:
        return time.monotonic()

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)


class ExecutorGuardianTerminalObservationOwner:
    """Decide exit versus deadline from one authoritative observation boundary.

    A bounded deadline wins whenever it is reached before the initial poll or
    at the clock observation immediately after a newly observed command exit.
    """

    def __init__(
        self,
        clock: ExecutorGuardianObservationClock,
        poll_interval_seconds: float,
    ) -> None:
        if (
            type(poll_interval_seconds) is not float
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0.0
        ):
            raise ValueError(
                "guardian observation poll interval must be finite and positive"
            )
        self._clock = _require_observation_clock(clock)
        self._poll_interval_seconds = poll_interval_seconds

    def observe(
        self,
        process: PosixProcessHandle,
        budget: ExecutorGuardianBudget,
        group_liveness: ExecutorGuardianGroupLiveness,
    ) -> ExecutorGuardianCommandObservation:
        process = _require_process(process)
        budget = _require_budget(budget)
        group_liveness = _require_group_liveness(group_liveness)
        try:
            while True:
                cycle = self._observe_cycle(process, budget, group_liveness)
                if type(cycle) is _ExecutorGuardianObservationPending:
                    continue
                if type(cycle) is ExecutorGuardianCommandExitObserved:
                    return cycle
                if type(cycle) is ExecutorGuardianCommandDeadlineObserved:
                    return cycle
                raise AssertionError("guardian observation cycle is closed")
        except BaseException as error:
            return ExecutorGuardianCommandObservationFailed(
                type(error).__name__,
                repr(error),
            )

    def _observe_cycle(
        self,
        process: PosixProcessHandle,
        budget: ExecutorGuardianBudget,
        group_liveness: ExecutorGuardianGroupLiveness,
    ) -> _ExecutorGuardianObservationCycle:
        group_liveness.require_sentinel_alive()
        before_poll = self._deadline_observation(budget)
        if type(before_poll) is ExecutorGuardianCommandDeadlineObserved:
            return before_poll
        self._require_pending_deadline(before_poll)
        return_code = process.poll()
        if return_code is not None:
            return self._terminal_observation(return_code, budget)
        wait = self._wait_decision(budget)
        if type(wait) is ExecutorGuardianCommandDeadlineObserved:
            return wait
        if type(wait) is not _ExecutorGuardianWaitPlanned:
            raise AssertionError("guardian wait decision is closed")
        self._clock.wait(wait.seconds)
        return _ExecutorGuardianObservationPending()

    def _terminal_observation(
        self,
        exit_code: int,
        budget: ExecutorGuardianBudget,
    ) -> ExecutorGuardianCommandExitObserved | ExecutorGuardianCommandDeadlineObserved:
        after_poll = self._deadline_observation(budget)
        if type(after_poll) is ExecutorGuardianCommandDeadlineObserved:
            return after_poll
        self._require_pending_deadline(after_poll)
        return ExecutorGuardianCommandExitObserved(exit_code)

    @staticmethod
    def _require_pending_deadline(
        observation: _ExecutorGuardianDeadlineObservation,
    ) -> None:
        if type(observation) is not _ExecutorGuardianDeadlinePending:
            raise AssertionError("guardian deadline observation is closed")

    def _deadline_observation(
        self,
        budget: ExecutorGuardianBudget,
    ) -> _ExecutorGuardianDeadlineObservation:
        if type(budget) is ExecutorGuardianUnboundedBudget:
            return _ExecutorGuardianDeadlinePending()
        if type(budget) is not ExecutorGuardianBoundedBudget:
            raise AssertionError("guardian budget is a closed union")
        if budget.is_expired_at(self._monotonic()):
            return ExecutorGuardianCommandDeadlineObserved(budget.reason)
        return _ExecutorGuardianDeadlinePending()

    def _wait_decision(
        self,
        budget: ExecutorGuardianBudget,
    ) -> _ExecutorGuardianWaitDecision:
        if type(budget) is ExecutorGuardianUnboundedBudget:
            return _ExecutorGuardianWaitPlanned(self._poll_interval_seconds)
        if type(budget) is not ExecutorGuardianBoundedBudget:
            raise AssertionError("guardian budget is a closed union")
        remaining_seconds = budget.expires_at_monotonic - self._monotonic()
        if remaining_seconds <= 0.0:
            return ExecutorGuardianCommandDeadlineObserved(budget.reason)
        return _ExecutorGuardianWaitPlanned(
            min(self._poll_interval_seconds, remaining_seconds)
        )

    def _monotonic(self) -> float:
        observed = self._clock.monotonic()
        if type(observed) is not float or not math.isfinite(observed) or observed < 0:
            raise ValueError(
                "guardian observation clock must return a finite, non-negative float"
            )
        return observed
