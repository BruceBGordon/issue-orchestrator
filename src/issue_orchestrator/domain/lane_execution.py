# pyright: strict
"""Typed contracts for validation-lane execution backends.

A *lane* is one coarse unit of validation work (a make target such as
``test-unit``). Lanes run through the :class:`~issue_orchestrator.ports.
lane_executor.LaneExecutor` port so the machinery that executes them —
direct subprocess today, an external batch scheduler when opted in — is
an adapter decision invisible to callers. These contracts are the only
vocabulary that crosses that boundary: backend-specific concepts must be
translated at the adapter (anti-corruption layer), never leaked upward.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast


_WORK_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_EXCLUSIVE_RESOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Exit code contract shared by every backend: a lane that exceeds its
# deadline reports 124, matching coreutils ``timeout`` and the direct
# path's historical behavior, so callers cannot tell backends apart.
LANE_TIMEOUT_EXIT_CODE = 124


@dataclass(frozen=True, slots=True)
class LaneWorkKey:
    """Stable identifier for one lane, used for logs and job naming."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or not _WORK_KEY_PATTERN.match(self.value):
            raise ValueError(
                "LaneWorkKey must be 1-64 chars of [a-z0-9._-] starting "
                f"alphanumeric, got {self.value!r}"
            )


@dataclass(frozen=True, slots=True)
class LaneDeadline:
    """Active-execution-time bound for one lane run.

    The budget charges time the lane actually executes: scheduler queue
    wait before execution and machine-load suspension (a backend
    freezing the lane to defer to the machine's owner) are charged to
    nothing — a lane must never time out for waiting or for being
    frozen. On the direct backend, which neither queues nor suspends,
    this is indistinguishable from a wall-clock bound. Every backend
    terminates the lane's entire process tree at the bound and reports
    :data:`LANE_TIMEOUT_EXIT_CODE`.
    """

    timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.timeout_seconds) is not float
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("LaneDeadline.timeout_seconds must be finite and positive")


@dataclass(frozen=True, slots=True)
class LaneResources:
    """Scheduling requirements for one lane.

    ``exclusive`` names machine-wide mutual-exclusion tokens (for
    example a provider account that only tolerates one concurrent
    login). Backends without a scheduler may run exclusives untracked —
    the direct path executes lanes one at a time from make's own job
    graph — but a scheduling backend must enforce them.
    """

    request_cpus: int
    exclusive: tuple[str, ...] = ()
    # Scheduling hint: expected duration in seconds. A scheduling backend
    # may start longer lanes first (the LPT makespan heuristic); the
    # direct backend ignores it. Zero means no preference.
    priority: int = 0
    # Memory the lane's whole tree may need, in MB. A scheduling backend
    # sizes the lane's slot from this; without an explicit request,
    # HTCondor derives the slot from the tiny exec wrapper's image size
    # and the real workload is OOM-killed at a ~256MB ceiling. The
    # default fits light lanes; heavy lanes must declare their budget.
    request_memory_mb: int = 1024
    # Whether the lane tolerates being frozen mid-run (machine-load
    # backoff). Only the client knows this: hermetic lanes freeze and
    # thaw safely anywhere, but a lane holding a live provider exchange
    # must never be paused mid-turn — the response window expires while
    # frozen and the thaw manufactures a provider-outage failure
    # indistinguishable from a real one. The default is the FAIL-SAFE
    # direction: a lane nobody classified is never frozen — freezing
    # requires an explicit declaration, not an author's memory.
    suspendable: bool = False

    def __post_init__(self) -> None:
        if type(self.request_cpus) is not int or self.request_cpus < 1:
            raise ValueError("LaneResources.request_cpus must be a positive integer")
        if type(self.request_memory_mb) is not int or self.request_memory_mb < 1:
            raise ValueError(
                "LaneResources.request_memory_mb must be a positive integer"
            )
        if type(self.priority) is not int or self.priority < 0:
            raise ValueError("LaneResources.priority must be a non-negative integer")
        if type(self.suspendable) is not bool:
            raise ValueError("LaneResources.suspendable must be a bool")
        if type(self.exclusive) is not tuple:
            raise ValueError("LaneResources.exclusive must be a tuple")
        for token in self.exclusive:
            if type(token) is not str or not _EXCLUSIVE_RESOURCE_PATTERN.match(token):
                raise ValueError(
                    "LaneResources.exclusive tokens must be 1-32 chars of "
                    f"[a-z0-9_] starting with a letter, got {token!r}"
                )
        if len(set(self.exclusive)) != len(self.exclusive):
            raise ValueError("LaneResources.exclusive tokens must be unique")


@dataclass(frozen=True, slots=True)
class LaneCommand:
    """One complete lane invocation."""

    work_key: LaneWorkKey
    arguments: tuple[str, ...]
    working_directory: Path
    deadline: LaneDeadline

    def __post_init__(self) -> None:
        if type(self.work_key) is not LaneWorkKey:
            raise ValueError("LaneCommand.work_key must be a LaneWorkKey")
        if (
            type(self.arguments) is not tuple
            or not self.arguments
            or any(
                type(argument) is not str or not argument or "\0" in argument
                for argument in self.arguments
            )
        ):
            raise ValueError(
                "LaneCommand.arguments must be a non-empty tuple of "
                "non-empty NUL-free strings"
            )
        if (
            not isinstance(cast(object, self.working_directory), Path)
            or not self.working_directory.is_absolute()
        ):
            raise ValueError("LaneCommand.working_directory must be an absolute Path")
        if type(self.deadline) is not LaneDeadline:
            raise ValueError("LaneCommand.deadline must be a LaneDeadline")


@dataclass(frozen=True, slots=True)
class LaneCompleted:
    """The lane's process tree ran to its own exit.

    ``observed_runtime_seconds`` is the time the lane actually
    executed — the same active-execution clock as
    :class:`LaneDeadline`: scheduler queue wait and machine-load
    suspension are both excluded. It feeds the runtime-history learning
    loop, which is why neither may leak into it: queue-inflated numbers
    would chase their own scheduling delays, and frozen time would
    teach the loop that a lane got slower when the machine got busier.

    ``queue_wait_seconds`` is the excluded scheduling wait, reported
    separately: the span from asking a backend to run the lane until
    execution first began. It is the dispatch-quality signal — a long
    pole waiting here is exactly the waste the learned-priority loop
    exists to remove — so it must be observable without pool
    archaeology. The direct backend starts immediately and reports 0.
    """

    exit_code: int
    observed_runtime_seconds: float
    queue_wait_seconds: float

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError("LaneCompleted.exit_code must be an integer")
        if (
            type(self.observed_runtime_seconds) is not float
            or not math.isfinite(self.observed_runtime_seconds)
            or self.observed_runtime_seconds < 0
        ):
            raise ValueError(
                "LaneCompleted.observed_runtime_seconds must be finite and "
                "non-negative"
            )
        if (
            type(self.queue_wait_seconds) is not float
            or not math.isfinite(self.queue_wait_seconds)
            or self.queue_wait_seconds < 0
        ):
            raise ValueError(
                "LaneCompleted.queue_wait_seconds must be finite and "
                "non-negative"
            )


@dataclass(frozen=True, slots=True)
class LaneTimedOut:
    """The backend terminated the lane's process tree at its deadline."""

    elapsed_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.elapsed_seconds) is not float
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError(
                "LaneTimedOut.elapsed_seconds must be finite and non-negative"
            )

    @property
    def exit_code(self) -> int:
        return LANE_TIMEOUT_EXIT_CODE


LaneOutcome = LaneCompleted | LaneTimedOut


class LaneExecutorError(RuntimeError):
    """The backend itself failed — distinct from the lane failing.

    Raised loudly instead of being folded into a lane exit code: a
    broken scheduler must never masquerade as a failing test suite.
    """


class LaneExecutorUnavailableError(LaneExecutorError):
    """The configured backend is not installed, running, or reachable."""
