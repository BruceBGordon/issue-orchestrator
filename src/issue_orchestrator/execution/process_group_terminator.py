"""POSIX owner for fail-fast whole-process-group termination."""

from __future__ import annotations

import os
import signal
import time

from ..domain.executor import ExecutorProcessTerminationPolicy
from ..domain.process_group import OwnedProcessGroupLeader, ProcessGroupTermination
from ..domain.process_group import (
    ProcessGroupAbsent,
    ProcessGroupExecutable,
    ProcessGroupPermissionDenied,
    ProcessGroupZombiesOnly,
)
from ..ports.process_group_observer import ProcessGroupObserver


class ProcessGroupTerminationError(RuntimeError):
    """Raised when an owned process group cannot be reaped after SIGKILL."""


class PosixProcessGroupTerminator:
    """Terminate every member of one owned POSIX process group.

    The caller must have spawned the leader as the leader of a new process
    group, either in a new session or the caller's existing terminal session.
    Its pid is therefore the process-group id. The leader is deliberately kept
    unreaped through the unconditional SIGKILL so the group id cannot be
    recycled between the courtesy signal and the containment signal.
    """

    def __init__(
        self,
        policy: ExecutorProcessTerminationPolicy,
        process_group_observer: ProcessGroupObserver,
    ) -> None:
        if type(policy) is not ExecutorProcessTerminationPolicy:
            raise ValueError(
                "PosixProcessGroupTerminator.policy must be an "
                "ExecutorProcessTerminationPolicy"
            )
        self._policy = policy
        if not isinstance(process_group_observer, ProcessGroupObserver):
            raise ValueError(
                "PosixProcessGroupTerminator.process_group_observer must "
                "implement ProcessGroupObserver"
            )
        self._process_group_observer = process_group_observer

    def terminate(
        self,
        leader: OwnedProcessGroupLeader,
    ) -> ProcessGroupTermination:
        """Send group TERM then unconditional KILL before reaping the leader."""
        if type(leader) is not OwnedProcessGroupLeader:
            raise ValueError(
                "PosixProcessGroupTerminator.terminate requires an "
                "OwnedProcessGroupLeader"
            )

        process_group_id = leader.process_id
        self._signal_group(process_group_id, signal.SIGTERM)
        self._await_leader_exit_without_reaping(
            leader,
            timeout_seconds=self._policy.graceful_shutdown_seconds,
        )

        # Never infer an empty group from the leader's exit.  A descendant can
        # ignore TERM and close inherited pipes.  SIGKILL while the unreaped
        # leader still reserves the pgid is the containment guarantee.
        self._signal_group(process_group_id, signal.SIGKILL)
        return ProcessGroupTermination(
            self._reap_leader(
                leader,
                timeout_seconds=self._policy.forceful_shutdown_seconds,
            )
        )

    def _signal_group(
        self,
        process_group_id: int,
        signal_number: signal.Signals,
    ) -> None:
        try:
            os.killpg(process_group_id, signal_number)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            observation = self._process_group_observer.observe_group(
                process_group_id
            )
            if type(observation) in (ProcessGroupAbsent, ProcessGroupZombiesOnly):
                # macOS can report EPERM for a zombie-only group.  Suppression is
                # safe only after the injected observer proves that no member can
                # execute user code.
                return
            if type(observation) is ProcessGroupExecutable:
                detail = (
                    f"{observation.member_count} executable member(s) remain"
                )
            elif type(observation) is ProcessGroupPermissionDenied:
                detail = f"membership observation denied: {observation.detail}"
            else:
                raise AssertionError("process-group observation is a closed union")
            raise ProcessGroupTerminationError(
                f"permission denied signalling process group {process_group_id} "
                f"with {signal_number.name}: {detail}"
            ) from exc

    @staticmethod
    def _await_leader_exit_without_reaping(
        leader: OwnedProcessGroupLeader,
        *,
        timeout_seconds: float,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                exit_observation = os.waitid(
                    os.P_PID,
                    leader.process_id,
                    os.WEXITED | os.WNOWAIT | os.WNOHANG,
                )
            except ChildProcessError as exc:
                raise ProcessGroupTerminationError(
                    f"owned process-group leader {leader.process_id} was reaped "
                    "outside its termination owner"
                ) from exc
            if exit_observation is not None and exit_observation.si_pid != 0:
                return
            time.sleep(0.01)

    @staticmethod
    def _reap_leader(
        leader: OwnedProcessGroupLeader,
        *,
        timeout_seconds: float,
    ) -> int:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                reaped_process_id, wait_status = os.waitpid(
                    leader.process_id,
                    os.WNOHANG,
                )
            except ChildProcessError as exc:
                raise ProcessGroupTerminationError(
                    f"owned process-group leader {leader.process_id} was reaped "
                    "outside its termination owner"
                ) from exc
            if reaped_process_id == leader.process_id:
                return os.waitstatus_to_exitcode(wait_status)
            time.sleep(0.01)
        raise ProcessGroupTerminationError(
            f"process-group leader {leader.process_id} did not reap within "
            f"{timeout_seconds:.3f}s after SIGKILL"
        )
