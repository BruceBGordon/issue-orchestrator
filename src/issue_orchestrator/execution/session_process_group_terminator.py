# pyright: strict
"""Deep module for stopping one externally owned terminal session group."""

from __future__ import annotations

import logging
import os
import signal
import time
from enum import StrEnum

from ..domain.terminal_session_termination import (
    TerminalSessionProcess,
    TerminalSessionTerminationPolicy,
)
from .executor_guardian_cancellation import (
    ExecutorGuardianCancellationOutcome,
    ExecutorSessionGuardianCanceller,
)


logger = logging.getLogger(__name__)


class TerminalSessionContainmentError(RuntimeError):
    """Raised rather than reporting a session stopped while its group is live."""


class TerminalSessionContainmentMode(StrEnum):
    """How the registered outer process group reached containment."""

    OUTER_ABSENT = "outer-absent"
    GRACEFUL = "graceful"
    FORCED = "forced"


class PosixTerminalSessionProcessGroupTerminator:
    """Signal and observe a persisted PTY group without claiming child ownership."""

    def __init__(
        self,
        policy: TerminalSessionTerminationPolicy,
        guardian_canceller: ExecutorSessionGuardianCanceller,
    ) -> None:
        if type(policy) is not TerminalSessionTerminationPolicy:
            raise ValueError(
                "PosixTerminalSessionProcessGroupTerminator.policy must be a "
                "TerminalSessionTerminationPolicy"
            )
        self._policy = policy
        if type(guardian_canceller) is not ExecutorSessionGuardianCanceller:
            raise ValueError(
                "PosixTerminalSessionProcessGroupTerminator.guardian_canceller "
                "must be an ExecutorSessionGuardianCanceller"
            )
        self._guardian_canceller = guardian_canceller

    def terminate(self, process: TerminalSessionProcess) -> None:
        """Return only after the complete group has no executable members."""
        if type(process) is not TerminalSessionProcess:
            raise ValueError(
                "PosixTerminalSessionProcessGroupTerminator.terminate requires "
                "TerminalSessionProcess"
            )
        try:
            process_group_id = os.getpgid(process.process_id)
        except ProcessLookupError:
            guardian_outcome = self._guardian_canceller.contain_if_active(
                process.executor_cancellation
            )
            self._log_containment(
                process,
                TerminalSessionContainmentMode.OUTER_ABSENT,
                guardian_outcome,
            )
            return
        if process_group_id != process.process_id:
            raise TerminalSessionContainmentError(
                "terminal session registry pid is not its process-group leader: "
                f"pid={process.process_id} pgid={process_group_id}"
            )

        self._signal(process_group_id, signal.SIGTERM)
        if self._await_empty(
            process_group_id,
            self._policy.graceful_shutdown_seconds,
        ):
            guardian_outcome = self._guardian_canceller.contain_if_active(
                process.executor_cancellation
            )
            self._log_containment(
                process,
                TerminalSessionContainmentMode.GRACEFUL,
                guardian_outcome,
            )
            return

        self._signal(process_group_id, signal.SIGKILL)
        guardian_outcome = self._guardian_canceller.contain_if_active(
            process.executor_cancellation
        )
        if self._await_empty(
            process_group_id,
            self._policy.forceful_shutdown_seconds,
        ):
            self._guardian_canceller.contain_if_active(process.executor_cancellation)
            self._log_containment(
                process,
                TerminalSessionContainmentMode.FORCED,
                guardian_outcome,
            )
            return
        raise TerminalSessionContainmentError(
            "terminal session process group remains executable after SIGKILL: "
            f"pgid={process_group_id}"
        )

    @staticmethod
    def _log_containment(
        process: TerminalSessionProcess,
        outer_outcome: TerminalSessionContainmentMode,
        guardian_outcome: ExecutorGuardianCancellationOutcome,
    ) -> None:
        logger.info(
            "[terminal-containment] session stopped: pid=%s outer=%s "
            "guardian=%s cancellation_record=%s",
            process.process_id,
            outer_outcome.value,
            guardian_outcome.value,
            process.executor_cancellation.record_path,
        )

    @staticmethod
    def _signal(process_group_id: int, signal_number: signal.Signals) -> None:
        try:
            os.killpg(process_group_id, signal_number)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise TerminalSessionContainmentError(
                "permission denied while signalling terminal session process "
                f"group: pgid={process_group_id} signal={signal_number.name}"
            ) from exc

    @classmethod
    def _await_empty(cls, process_group_id: int, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not cls._has_executable_members(process_group_id):
                return True
            time.sleep(0.01)
        return not cls._has_executable_members(process_group_id)

    @staticmethod
    def _has_executable_members(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # macOS reports EPERM once this caller-owned group's only remaining
            # member is an unreaped zombie. It has no executable code left.
            return False
        return True
