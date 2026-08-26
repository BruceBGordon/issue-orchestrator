"""POSIX supervision owner for every terminal subprocess path."""

from __future__ import annotations

import os
import time
from enum import StrEnum

from ..domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupBoundedWait,
    ProcessGroupCompleted,
    ProcessGroupInterrupted,
    ProcessGroupSupervision,
    ProcessGroupTerminalCompletionAccepted,
    ProcessGroupTerminalDecision,
    ProcessGroupTerminalInterruptionRequested,
    ProcessGroupTermination,
    ProcessGroupTimedOut,
    ProcessGroupUnboundedWait,
    ProcessGroupWait,
)
from ..ports.process_group_terminator import ProcessGroupTerminator
from ..ports.process_group_supervisor import ProcessGroupInterruption


class ProcessGroupSupervisionError(RuntimeError):
    """Raised when another owner reaps a leader during supervision."""


class _ProcessGroupWaitResult(StrEnum):
    COMPLETED = "completed"
    TIMED_OUT = "timed-out"
    INTERRUPTED = "interrupted"


class NeverInterruptProcessGroup:
    """Explicit interruption policy for callers that only wait or time out."""

    def wait_for_request(self, timeout_seconds: float) -> bool:
        if (
            type(timeout_seconds) is not float
            or not 0.0 < timeout_seconds <= 0.01
        ):
            raise ValueError(
                "NeverInterruptProcessGroup timeout must be in (0.0, 0.01]"
            )
        time.sleep(timeout_seconds)
        return False

    def decide_terminal_observation(self) -> ProcessGroupTerminalDecision:
        return ProcessGroupTerminalCompletionAccepted()


class PosixProcessGroupSupervisor:
    """Observe without reaping, contain every descendant, then reap the leader."""

    def __init__(self, terminator: ProcessGroupTerminator) -> None:
        if not isinstance(terminator, ProcessGroupTerminator):
            raise ValueError(
                "PosixProcessGroupSupervisor.terminator must implement "
                "ProcessGroupTerminator"
            )
        self._terminator = terminator

    def supervise(
        self,
        leader: OwnedProcessGroupLeader,
        wait: ProcessGroupWait,
        interruption: ProcessGroupInterruption,
    ) -> ProcessGroupSupervision:
        """Wait without reaping, then contain the group on either outcome."""
        if type(leader) is not OwnedProcessGroupLeader:
            raise ValueError(
                "PosixProcessGroupSupervisor.supervise requires OwnedProcessGroupLeader"
            )
        if not isinstance(interruption, ProcessGroupInterruption):
            raise ValueError(
                "PosixProcessGroupSupervisor.supervise requires "
                "ProcessGroupInterruption"
            )
        if type(wait) is ProcessGroupUnboundedWait:
            if not self._wait_unbounded(leader, interruption):
                return ProcessGroupInterrupted(self._terminator.terminate(leader))
            return ProcessGroupCompleted(self._terminator.terminate(leader))
        if type(wait) is ProcessGroupBoundedWait:
            wait_result = self._wait_bounded(
                leader,
                wait.timeout_seconds,
                interruption,
            )
            if wait_result is _ProcessGroupWaitResult.COMPLETED:
                return ProcessGroupCompleted(self._terminator.terminate(leader))
            if wait_result is _ProcessGroupWaitResult.INTERRUPTED:
                return ProcessGroupInterrupted(self._terminator.terminate(leader))
            return ProcessGroupTimedOut(self._terminator.terminate(leader))
        raise ValueError(
            "PosixProcessGroupSupervisor.supervise requires a typed wait policy"
        )

    def abort(self, leader: OwnedProcessGroupLeader) -> ProcessGroupTermination:
        """Contain immediately without first waiting for natural completion."""
        if type(leader) is not OwnedProcessGroupLeader:
            raise ValueError(
                "PosixProcessGroupSupervisor.abort requires OwnedProcessGroupLeader"
            )
        return self._terminator.terminate(leader)

    @classmethod
    def _wait_unbounded(
        cls,
        leader: OwnedProcessGroupLeader,
        interruption: ProcessGroupInterruption,
    ) -> bool:
        while True:
            if cls._leader_has_exited(leader):
                return cls._terminal_completion_accepted(interruption)
            if interruption.wait_for_request(0.01):
                return False

    @classmethod
    def _wait_bounded(
        cls,
        leader: OwnedProcessGroupLeader,
        timeout_seconds: float,
        interruption: ProcessGroupInterruption,
    ) -> _ProcessGroupWaitResult:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if cls._leader_has_exited(leader):
                if cls._terminal_completion_accepted(interruption):
                    return _ProcessGroupWaitResult.COMPLETED
                return _ProcessGroupWaitResult.INTERRUPTED
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                return _ProcessGroupWaitResult.TIMED_OUT
            if interruption.wait_for_request(min(0.01, remaining_seconds)):
                return _ProcessGroupWaitResult.INTERRUPTED

    @staticmethod
    def _leader_has_exited(leader: OwnedProcessGroupLeader) -> bool:
        try:
            observation = os.waitid(
                os.P_PID,
                leader.process_id,
                os.WEXITED | os.WNOWAIT | os.WNOHANG,
            )
        except InterruptedError:
            return False
        except ChildProcessError as exc:
            raise ProcessGroupSupervisionError(
                f"owned process-group leader {leader.process_id} was reaped "
                "outside its supervision owner"
            ) from exc
        return observation is not None and observation.si_pid == leader.process_id

    @staticmethod
    def _terminal_completion_accepted(
        interruption: ProcessGroupInterruption,
    ) -> bool:
        decision = interruption.decide_terminal_observation()
        if type(decision) is ProcessGroupTerminalCompletionAccepted:
            return True
        if type(decision) is ProcessGroupTerminalInterruptionRequested:
            return False
        raise AssertionError("process-group terminal decision is a closed union")
