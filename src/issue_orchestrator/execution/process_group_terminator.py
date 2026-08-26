"""POSIX owner for fail-fast whole-process-group termination."""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass

from ..domain.executor import ExecutorProcessTerminationPolicy
from ..domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCourtesyCompleted,
    ProcessGroupCourtesyFailed,
    ProcessGroupTermination,
)
from ..domain.process_group import (
    ProcessGroupAbsent,
    ProcessGroupExecutable,
    ProcessGroupPermissionDenied,
    ProcessGroupZombiesOnly,
)
from ..ports.process_group_observer import ProcessGroupObserver


class ProcessGroupTerminationError(RuntimeError):
    """Raised when an owned process group cannot be reaped after SIGKILL."""


@dataclass(frozen=True, slots=True)
class _ForceSignalCompleted:
    """The mandatory group SIGKILL did not report an error."""


@dataclass(frozen=True, slots=True)
class _ForceSignalFailed:
    """The mandatory group SIGKILL reported an error."""

    error: BaseException


_ForceSignal = _ForceSignalCompleted | _ForceSignalFailed


@dataclass(frozen=True, slots=True)
class _LeaderReaped:
    """The exact group leader was reaped after mandatory containment."""

    exit_code: int


@dataclass(frozen=True, slots=True)
class _LeaderReapFailed:
    """The exact group leader could not be reaped."""

    error: BaseException


_LeaderReap = _LeaderReaped | _LeaderReapFailed


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
        courtesy = self._attempt_courtesy_shutdown(leader)

        # Never infer an empty group from the leader's exit.  A descendant can
        # ignore TERM and close inherited pipes.  SIGKILL while the unreaped
        # leader still reserves the pgid is the containment guarantee.
        force_signal = self._attempt_force_signal(process_group_id)
        leader_reap = self._attempt_leader_reap(leader)
        self._require_forced_containment(courtesy, force_signal, leader_reap)
        if type(leader_reap) is not _LeaderReaped:
            raise AssertionError("successful containment requires typed reap evidence")
        return ProcessGroupTermination(
            leader_reap.exit_code,
            courtesy,
        )

    def _attempt_courtesy_shutdown(
        self,
        leader: OwnedProcessGroupLeader,
    ) -> ProcessGroupCourtesyCompleted | ProcessGroupCourtesyFailed:
        try:
            self._signal_group(leader.process_id, signal.SIGTERM)
            self._await_leader_exit_without_reaping(
                leader,
                timeout_seconds=self._policy.graceful_shutdown_seconds,
            )
        except BaseException as error:
            return ProcessGroupCourtesyFailed(error)
        return ProcessGroupCourtesyCompleted()

    def _attempt_force_signal(self, process_group_id: int) -> _ForceSignal:
        try:
            self._signal_group(process_group_id, signal.SIGKILL)
        except BaseException as error:
            return _ForceSignalFailed(error)
        return _ForceSignalCompleted()

    def _attempt_leader_reap(self, leader: OwnedProcessGroupLeader) -> _LeaderReap:
        try:
            return _LeaderReaped(
                self._reap_leader(
                    leader,
                    timeout_seconds=self._policy.forceful_shutdown_seconds,
                )
            )
        except BaseException as error:
            return _LeaderReapFailed(error)

    @staticmethod
    def _require_forced_containment(
        courtesy: ProcessGroupCourtesyCompleted | ProcessGroupCourtesyFailed,
        force_signal: _ForceSignal,
        leader_reap: _LeaderReap,
    ) -> None:
        errors: list[BaseException] = []
        if type(courtesy) is ProcessGroupCourtesyFailed:
            errors.append(courtesy.error)
        elif type(courtesy) is not ProcessGroupCourtesyCompleted:
            raise AssertionError("process-group courtesy result is a closed union")
        if type(force_signal) is _ForceSignalFailed:
            errors.append(force_signal.error)
        elif type(force_signal) is not _ForceSignalCompleted:
            raise AssertionError("process-group force signal is a closed union")
        if type(leader_reap) is _LeaderReapFailed:
            errors.append(leader_reap.error)
        elif type(leader_reap) is not _LeaderReaped:
            raise AssertionError("process-group leader reap is a closed union")
        if type(force_signal) is _ForceSignalCompleted and type(leader_reap) is _LeaderReaped:
            return
        if len(errors) == 1:
            raise errors[0]
        raise BaseExceptionGroup(
            "process-group courtesy observation and forced containment failed",
            errors,
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
