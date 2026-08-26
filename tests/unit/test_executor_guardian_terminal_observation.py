"""Exact boundary proofs for guardian command terminal observation."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from issue_orchestrator.domain.executor import ExecutorDeadlineReason
from issue_orchestrator.domain.executor_guardian import ExecutorGuardianBoundedBudget
from issue_orchestrator.execution.host_executor.guardian_terminal_observation import (
    ExecutorGuardianCommandDeadlineObserved,
    ExecutorGuardianCommandExitObserved,
    ExecutorGuardianTerminalObservationOwner,
)


@dataclass(slots=True)
class _SequenceGuardianClock:
    observations: tuple[float, ...]
    waits: list[float] = field(default_factory=list)
    _index: int = field(default=0, init=False)

    def monotonic(self) -> float:
        if self._index >= len(self.observations):
            raise AssertionError("guardian clock was observed too many times")
        observed = self.observations[self._index]
        self._index += 1
        return observed

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)


@dataclass(slots=True)
class _TerminalProcess:
    exit_code: int | None
    poll_count: int = field(default=0, init=False)

    @property
    def process_id(self) -> int:
        return 12345

    @property
    def return_code(self) -> int | None:
        return self.exit_code

    def poll(self) -> int | None:
        self.poll_count += 1
        return self.exit_code

    def wait(self, timeout_seconds: float) -> int:
        del timeout_seconds
        raise AssertionError("terminal observation must not perform a reaping wait")

    def kill(self) -> None:
        raise AssertionError("terminal observation must not kill the process")

    def record_external_reap(self, exit_code: int) -> None:
        del exit_code
        raise AssertionError("terminal observation must not record reaping evidence")


@dataclass(slots=True)
class _HealthyGuardianGroup:
    observation_count: int = field(default=0, init=False)

    def require_sentinel_alive(self) -> None:
        self.observation_count += 1


@pytest.mark.parametrize(
    ("clock_observations", "expected_exit", "expected_poll_count"),
    (
        ((99.998, 99.999), True, 1),
        ((99.999, 100.0), False, 1),
        ((100.001,), False, 0),
    ),
)
def test_deadline_owns_terminal_observation_at_and_after_exact_boundary(
    clock_observations: tuple[float, ...],
    expected_exit: bool,
    expected_poll_count: int,
) -> None:
    clock = _SequenceGuardianClock(clock_observations)
    process = _TerminalProcess(17)
    group = _HealthyGuardianGroup()
    owner = ExecutorGuardianTerminalObservationOwner(clock, 0.05)

    observation = owner.observe(
        process,
        ExecutorGuardianBoundedBudget(
            100.0,
            ExecutorDeadlineReason.ABSOLUTE,
        ),
        group,
    )

    if expected_exit:
        assert type(observation) is ExecutorGuardianCommandExitObserved
        assert observation.exit_code == 17
    else:
        assert type(observation) is ExecutorGuardianCommandDeadlineObserved
        assert observation.reason is ExecutorDeadlineReason.ABSOLUTE
    assert process.poll_count == expected_poll_count
    assert group.observation_count == 1
    assert clock.waits == []


def test_deadline_reached_between_pending_poll_and_wait_does_not_sleep() -> None:
    clock = _SequenceGuardianClock((99.999, 100.0))
    process = _TerminalProcess(None)
    owner = ExecutorGuardianTerminalObservationOwner(clock, 0.05)

    observation = owner.observe(
        process,
        ExecutorGuardianBoundedBudget(
            100.0,
            ExecutorDeadlineReason.ACTIVE,
        ),
        _HealthyGuardianGroup(),
    )

    assert type(observation) is ExecutorGuardianCommandDeadlineObserved
    assert observation.reason is ExecutorDeadlineReason.ACTIVE
    assert process.poll_count == 1
    assert clock.waits == []
