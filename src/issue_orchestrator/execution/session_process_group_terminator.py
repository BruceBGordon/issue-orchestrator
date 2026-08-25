# pyright: strict
"""Deep module for stopping one externally owned terminal session group."""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass

from ..domain.process_group import ProcessIdentityObservation
from ..domain.terminal_session_termination import (
    TerminalSessionContainmentError,
    TerminalSessionProcess,
    TerminalSessionStatus,
    TerminalSessionTerminationPolicy,
    TerminalSessionTerminationOutcome,
    classify_terminal_session_observation,
    terminal_session_resolution_outcome,
)
from ..ports.process_group_observer import ProcessGroupObserver
from .executor_guardian_cancellation import (
    ExecutorGuardianCancellationOutcome,
    ExecutorSessionGuardianCanceller,
)


logger = logging.getLogger(__name__)


def _require_process_group_observer(value: object) -> None:
    if not isinstance(value, ProcessGroupObserver):
        raise ValueError(
            "PosixTerminalSessionProcessGroupTerminator.process_group_observer "
            "must implement ProcessGroupObserver"
        )


@dataclass(frozen=True, slots=True)
class _TerminationPhase:
    signal_number: signal.Signals
    timeout_seconds: float
    contained_outcome: TerminalSessionTerminationOutcome

    def __post_init__(self) -> None:
        if type(self.signal_number) is not signal.Signals:
            raise ValueError("termination phase signal must be signal.Signals")
        if type(self.timeout_seconds) is not float or self.timeout_seconds <= 0:
            raise ValueError("termination phase timeout must be positive")
        if type(self.contained_outcome) is not TerminalSessionTerminationOutcome:
            raise ValueError(
                "termination phase outcome must be TerminalSessionTerminationOutcome"
            )


@dataclass(frozen=True, slots=True)
class _GuardianContainmentSucceeded:
    outcome: ExecutorGuardianCancellationOutcome

    def __post_init__(self) -> None:
        if type(self.outcome) is not ExecutorGuardianCancellationOutcome:
            raise ValueError("guardian containment outcome must be exact")


@dataclass(frozen=True, slots=True)
class _GuardianContainmentFailed:
    error: BaseException


_GuardianContainmentAttempt = (
    _GuardianContainmentSucceeded | _GuardianContainmentFailed
)


class PosixTerminalSessionProcessGroupTerminator:
    """Signal and observe a persisted PTY group without claiming child ownership."""

    def __init__(
        self,
        policy: TerminalSessionTerminationPolicy,
        guardian_canceller: ExecutorSessionGuardianCanceller,
        process_group_observer: ProcessGroupObserver,
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
        _require_process_group_observer(process_group_observer)
        self._process_group_observer = process_group_observer

    def identify(self, process_id: int) -> ProcessIdentityObservation:
        """Observe one process before the terminal registry persists it."""
        return self._process_group_observer.observe_process(process_id)

    def status(self, process: TerminalSessionProcess) -> TerminalSessionStatus:
        """Classify the exact persisted session without signalling anything."""
        self._require_process(process)
        observation = self._process_group_observer.observe_session(
            process.process_id,
            process.birth_identity,
        )
        return classify_terminal_session_observation(process, observation)

    def terminate(
        self,
        process: TerminalSessionProcess,
    ) -> TerminalSessionTerminationOutcome:
        """Return only after the complete group has no executable members."""
        self._require_process(process)
        # Contain the independently supervised command first. Besides stopping
        # opaque work, this lets a live outer executor release normally. For a
        # SIGSTOP-stalled outer group, its original leader then remains present
        # through TERM and gives the forceful phase a safe, exact PGID anchor.
        guardian_attempt = self._attempt_guardian_containment(process)
        try:
            outer_outcome = self._terminate_outer(process)
        except BaseException as outer_error:
            if type(guardian_attempt) is _GuardianContainmentFailed:
                raise BaseExceptionGroup(
                    "terminal guardian and outer-group containment failures",
                    (guardian_attempt.error, outer_error),
                )
            raise
        if type(guardian_attempt) is _GuardianContainmentFailed:
            raise guardian_attempt.error
        if type(guardian_attempt) is not _GuardianContainmentSucceeded:
            raise AssertionError("guardian containment attempt is a closed union")
        self._log_containment(process, outer_outcome, guardian_attempt.outcome)
        return outer_outcome

    def _terminate_outer(
        self,
        process: TerminalSessionProcess,
    ) -> TerminalSessionTerminationOutcome:
        """Contain the exact persisted outer process group."""
        initial_status = self.status(process)
        initial_outcome = terminal_session_resolution_outcome(
            initial_status,
            TerminalSessionTerminationOutcome.ALREADY_CONTAINED,
        )
        if initial_outcome is not None:
            return initial_outcome

        phases = (
            _TerminationPhase(
                signal.SIGTERM,
                self._policy.graceful_shutdown_seconds,
                TerminalSessionTerminationOutcome.GRACEFUL,
            ),
            _TerminationPhase(
                signal.SIGKILL,
                self._policy.forceful_shutdown_seconds,
                TerminalSessionTerminationOutcome.FORCED,
            ),
        )
        for phase in phases:
            self._signal(process.process_id, phase.signal_number)
            observed = self._await_resolution(process, phase.timeout_seconds)
            outcome = terminal_session_resolution_outcome(
                observed,
                phase.contained_outcome,
            )
            if outcome is not None:
                return outcome
        raise TerminalSessionContainmentError(
            "terminal session process group remains executable after SIGKILL: "
            f"pgid={process.process_id}"
        )

    @staticmethod
    def _require_process(process: TerminalSessionProcess) -> None:
        if type(process) is not TerminalSessionProcess:
            raise ValueError(
                "PosixTerminalSessionProcessGroupTerminator requires "
                "TerminalSessionProcess"
            )

    def _attempt_guardian_containment(
        self,
        process: TerminalSessionProcess,
    ) -> _GuardianContainmentAttempt:
        """Capture one independent failure so outer cleanup is still attempted."""
        try:
            return _GuardianContainmentSucceeded(
                self._guardian_canceller.contain_if_active(
                    process.executor_cancellation
                )
            )
        except BaseException as error:
            error.add_note("executor guardian containment failed before outer cleanup")
            return _GuardianContainmentFailed(error)

    @staticmethod
    def _log_containment(
        process: TerminalSessionProcess,
        outer_outcome: TerminalSessionTerminationOutcome,
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

    def _await_resolution(
        self,
        process: TerminalSessionProcess,
        timeout_seconds: float,
    ) -> TerminalSessionStatus:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = self.status(process)
            if status is not TerminalSessionStatus.ACTIVE:
                return status
            time.sleep(0.01)
        return self.status(process)
