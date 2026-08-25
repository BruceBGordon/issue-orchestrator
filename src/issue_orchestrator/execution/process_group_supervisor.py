"""POSIX supervision owner for every terminal subprocess path."""

from __future__ import annotations

import os
import time

from ..domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupBoundedWait,
    ProcessGroupCompleted,
    ProcessGroupSupervision,
    ProcessGroupTermination,
    ProcessGroupTimedOut,
    ProcessGroupUnboundedWait,
    ProcessGroupWait,
)
from ..ports.process_group_terminator import ProcessGroupTerminator


class ProcessGroupSupervisionError(RuntimeError):
    """Raised when another owner reaps a leader during supervision."""


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
    ) -> ProcessGroupSupervision:
        """Wait without reaping, then contain the group on either outcome."""
        if type(leader) is not OwnedProcessGroupLeader:
            raise ValueError(
                "PosixProcessGroupSupervisor.supervise requires OwnedProcessGroupLeader"
            )
        if type(wait) is ProcessGroupUnboundedWait:
            self._wait_unbounded(leader)
            return ProcessGroupCompleted(self._terminator.terminate(leader))
        if type(wait) is ProcessGroupBoundedWait:
            if self._wait_bounded(leader, wait.timeout_seconds):
                return ProcessGroupCompleted(self._terminator.terminate(leader))
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

    @staticmethod
    def _wait_unbounded(leader: OwnedProcessGroupLeader) -> None:
        while True:
            try:
                observation = os.waitid(
                    os.P_PID,
                    leader.process_id,
                    os.WEXITED | os.WNOWAIT,
                )
            except InterruptedError:
                continue
            except ChildProcessError as exc:
                raise ProcessGroupSupervisionError(
                    f"owned process-group leader {leader.process_id} was reaped "
                    "outside its supervision owner"
                ) from exc
            if observation is not None and observation.si_pid == leader.process_id:
                return

    @classmethod
    def _wait_bounded(
        cls,
        leader: OwnedProcessGroupLeader,
        timeout_seconds: float,
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                observation = os.waitid(
                    os.P_PID,
                    leader.process_id,
                    os.WEXITED | os.WNOWAIT | os.WNOHANG,
                )
            except InterruptedError:
                continue
            except ChildProcessError as exc:
                raise ProcessGroupSupervisionError(
                    f"owned process-group leader {leader.process_id} was reaped "
                    "outside its supervision owner"
                ) from exc
            if observation is not None and observation.si_pid == leader.process_id:
                return True
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                return False
            time.sleep(min(0.01, remaining_seconds))
