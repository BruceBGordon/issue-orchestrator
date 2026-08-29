# pyright: strict
"""Direct-subprocess lane executor — the default backend.

Preserves the pre-port behavior exactly: the lane runs as a child
process group with inherited stdio, and a deadline overrun terminates
the whole group (TERM, a graceful window, then KILL).
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import time
from dataclasses import dataclass

from ..domain.lane_execution import (
    LaneCommand,
    LaneCompleted,
    LaneOutcome,
    LanePolicyReport,
    LaneResources,
    LaneTimedOut,
)
from ..ports.executor_pool import PoolOffline, PoolState

_NO_POOL_REASON = (
    "the direct backend runs each lane as a child process of the gate "
    "that asked for it; there is no machine-wide pool, no queue, and no "
    "admission control to inspect (see docs/user/condor_lanes.md to opt "
    "into a scheduling backend that has one)"
)


@dataclass(frozen=True, slots=True)
class DirectLaneTerminationPolicy:
    """Bound the TERM-to-KILL escalation for a deadline overrun."""

    graceful_shutdown_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.graceful_shutdown_seconds) is not float
            or not math.isfinite(self.graceful_shutdown_seconds)
            or self.graceful_shutdown_seconds <= 0
        ):
            raise ValueError(
                "DirectLaneTerminationPolicy.graceful_shutdown_seconds must be "
                "finite and positive"
            )


class DirectLaneExecutor:
    """Run the lane as a directly supervised child process group."""

    def __init__(self, termination: DirectLaneTerminationPolicy) -> None:
        if type(termination) is not DirectLaneTerminationPolicy:
            raise ValueError(
                "DirectLaneExecutor.termination must be DirectLaneTerminationPolicy"
            )
        self._termination = termination

    def run(self, command: LaneCommand, resources: LaneResources) -> LaneOutcome:
        if type(command) is not LaneCommand:
            raise ValueError("DirectLaneExecutor.run requires a LaneCommand")
        if type(resources) is not LaneResources:
            raise ValueError("DirectLaneExecutor.run requires LaneResources")
        # The direct backend has no scheduler: cpu requests and exclusive
        # tokens are honored by the caller's own job graph (make -j), so
        # they are accepted and intentionally untracked here.
        started_at = time.monotonic()
        process = subprocess.Popen(
            command.arguments,
            cwd=command.working_directory,
            start_new_session=True,
        )
        try:
            exit_code = process.wait(timeout=command.deadline.timeout_seconds)
        except subprocess.TimeoutExpired:
            self._contain_group(process)
            return LaneTimedOut(time.monotonic() - started_at)
        except BaseException:
            # The caller is dying (KeyboardInterrupt included): the lane's
            # tree must not outlive its supervisor.
            self._contain_group(process)
            raise
        observed_runtime = time.monotonic() - started_at
        # No scheduler, no queue: the lane started the moment it was
        # asked to, so the queue wait is identically zero.
        #
        # CPU demand is deliberately NOT measured here, and this
        # backend reports no busy-cores figure at all. It could — the
        # lane is a direct child, so getrusage(RUSAGE_CHILDREN) around
        # the wait would give an exact number — but the number would
        # be wrong for its only consumer. Busy cores is CPU-seconds
        # over WALL time, and the direct path runs lanes concurrently
        # out of make's own job graph: contention leaves the numerator
        # unchanged while inflating the denominator, so every
        # measurement comes out systematically LOW. Learned evidence
        # may only lower a request, so feeding deflated numbers into
        # the loop would quietly shrink every lane's request and
        # oversubscribe the scheduler that consumes it — a backend
        # this one never even talks to. The rule is: only a backend
        # whose measuring conditions match the consumer of the number
        # reports one, so the scheduler learns from itself.
        if exit_code < 0:
            # Signal death reports as 128+N in every backend.
            return LaneCompleted(128 - exit_code, observed_runtime, 0.0)
        return LaneCompleted(exit_code, observed_runtime, 0.0)

    def _contain_group(self, process: subprocess.Popen[bytes]) -> None:
        try:
            group_id = os.getpgid(process.pid) if process.poll() is None else None
        except ProcessLookupError:
            group_id = None
        if group_id is None:
            process.wait()
            return
        _signal_group(group_id, signal.SIGTERM)
        try:
            process.wait(timeout=self._termination.graceful_shutdown_seconds)
        except subprocess.TimeoutExpired:
            _signal_group(group_id, signal.SIGKILL)
            process.wait()


def _signal_group(group_id: int, signal_number: signal.Signals) -> None:
    try:
        os.killpg(group_id, signal_number)
    except ProcessLookupError:
        return


class DirectLanePolicyCheck:
    """The direct backend's policy self-check: nothing to assert.

    Lanes run as children of the caller in the caller's own
    environment. There is no external configuration that could have
    drifted since the lane contracts were written, so the report is
    empty BY CONSTRUCTION — a truthful "no invariants" answer rather
    than a skip. That is what lets the gate's preflight step be
    unconditional and mode-agnostic instead of branching on the
    backend.
    """

    def inspect(self) -> LanePolicyReport:
        return LanePolicyReport(
            source="direct subprocess execution (this process's environment)",
            remedy="not applicable: this backend has no external policy",
            invariants=(),
        )


class DirectLanePoolInspector:
    """The direct backend's answer to "show me the pool": there isn't one.

    A real adapter rather than a ``None`` the caller must remember to
    handle: the absence of a pool is a fact this backend can state
    precisely, and stating it is more useful than an empty listing that
    looks like an idle pool.
    """

    def inspect(self) -> PoolState:
        return PoolOffline(_NO_POOL_REASON)
