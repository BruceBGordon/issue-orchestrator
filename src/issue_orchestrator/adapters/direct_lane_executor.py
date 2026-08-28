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
    LaneResources,
    LaneTimedOut,
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
