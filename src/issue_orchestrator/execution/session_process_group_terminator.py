# pyright: strict
"""Deep module for stopping one externally owned terminal session group."""

from __future__ import annotations

import logging
import os
import signal
import time

from ..domain.process_group import (
    ProcessGroupAbsent,
    ProcessGroupZombiesOnly,
    ProcessIdentityObservation,
)
from ..domain.terminal_session_termination import (
    TerminalSessionContainmentError,
    TerminalSessionContainmentReport,
    TerminalSessionOwnerContainmentOutcome,
    TerminalSessionProcess,
    TerminalSessionStatus,
    TerminalSessionTerminationPolicy,
    TerminalSessionTerminationOutcome,
    UnregisteredTerminalSessionOwnership,
    classify_terminal_session_observation,
    terminal_session_resolution_outcome,
)
from ..ports.process_group_observer import ProcessGroupObserver
from ..ports.terminal_session_containment_owner import (
    TerminalSessionContainmentOwner,
)


logger = logging.getLogger(__name__)


def _require_process_group_observer(value: object) -> None:
    if not isinstance(value, ProcessGroupObserver):
        raise ValueError(
            "PosixTerminalSessionProcessGroupTerminator.process_group_observer "
            "must implement ProcessGroupObserver"
        )

class PosixTerminalSessionProcessGroupTerminator:
    """Signal and observe a persisted PTY group without claiming child ownership."""

    def __init__(
        self,
        policy: TerminalSessionTerminationPolicy,
        containment_owner: TerminalSessionContainmentOwner,
        process_group_observer: ProcessGroupObserver,
    ) -> None:
        if type(policy) is not TerminalSessionTerminationPolicy:
            raise ValueError(
                "PosixTerminalSessionProcessGroupTerminator.policy must be a "
                "TerminalSessionTerminationPolicy"
            )
        self._policy = policy
        self._containment_owner = containment_owner
        _require_process_group_observer(process_group_observer)
        self._process_group_observer = process_group_observer

    def identify(self, process_id: int) -> ProcessIdentityObservation:
        """Observe one process before the terminal registry persists it."""
        return self._process_group_observer.observe_process(process_id)

    def recover_unregistered(
        self,
        ownership: UnregisteredTerminalSessionOwnership,
    ) -> TerminalSessionContainmentReport:
        """Contain launch-intent owners without consulting a numeric PID."""
        if type(ownership) is not UnregisteredTerminalSessionOwnership:
            raise ValueError(
                "recover_unregistered requires UnregisteredTerminalSessionOwnership"
            )
        return self._containment_owner.contain_unregistered(ownership)

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
        # The cancellation owner contains both groups from inside those groups.
        # Numeric process observation below is status-only and never grants
        # signal authority to this recovered caller.
        initial_status = self.status(process)
        report = self._containment_owner.contain(process)
        outer_outcome = self._resolve_after_owner_containment(
            process,
            initial_status,
            report,
        )
        self._log_containment(process, outer_outcome, report)
        return outer_outcome

    def _resolve_after_owner_containment(
        self,
        process: TerminalSessionProcess,
        initial_status: TerminalSessionStatus,
        report: TerminalSessionContainmentReport,
    ) -> TerminalSessionTerminationOutcome:
        """Observe the result of owner-mediated containment without signalling."""
        initial_outcome = terminal_session_resolution_outcome(
            initial_status,
            TerminalSessionTerminationOutcome.ALREADY_CONTAINED,
        )
        if initial_outcome is not None:
            return initial_outcome
        if (
            report.terminal_owner
            is TerminalSessionOwnerContainmentOutcome.UNRESPONSIVE
        ):
            # A live owner that held its endpoint but never acknowledged is
            # stalled (for example SIGSTOP).  Signal authority here is granted
            # only through the verified birth identity of the exact persisted
            # leader, never a bare numeric PID.
            self._force_contain_unresponsive_outer(process)
        elif (
            report.terminal_owner
            is not TerminalSessionOwnerContainmentOutcome.CONTAINED
        ):
            raise TerminalSessionContainmentError(
                "active terminal session has no live self-containment owner: "
                f"pid={process.process_id} "
                f"owner={report.terminal_owner.value}"
            )
        # A stale or unresponsive guardian owner is not trusted; the
        # session-wide observation below is the proof.  Every group in the
        # leader's session, including a guardian's descendants, must stop
        # being executable before the deadline or this raises.
        observed = self._await_resolution(
            process,
            self._policy.forceful_shutdown_seconds,
        )
        outcome = terminal_session_resolution_outcome(
            observed,
            TerminalSessionTerminationOutcome.FORCED,
        )
        if outcome is not None:
            return outcome
        raise TerminalSessionContainmentError(
            "terminal session remains executable after its owner acknowledged "
            f"containment: pid={process.process_id}"
        )

    def _force_contain_unresponsive_outer(
        self,
        process: TerminalSessionProcess,
    ) -> None:
        """Forcefully contain every group of a stalled session after proof.

        Group ids are enumerated while the verified leader is still alive:
        after leader death the platform stops attributing members to this
        session, so each swept group is then verified by its own id.
        """
        if self.status(process) is not TerminalSessionStatus.ACTIVE:
            return
        group_ids = self._process_group_observer.observe_session_group_ids(
            process.process_id
        )
        for group_id in (process.process_id, *group_ids):
            try:
                os.killpg(group_id, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except PermissionError as error:
                raise TerminalSessionContainmentError(
                    "stalled terminal session group refused forced "
                    f"containment: pgid={group_id}"
                ) from error
        self._await_group_ids_contained(group_ids)

    def _await_group_ids_contained(self, group_ids: tuple[int, ...]) -> None:
        deadline = time.monotonic() + self._policy.forceful_shutdown_seconds
        pending = list(group_ids)
        while pending:
            observation = self._process_group_observer.observe_group(pending[0])
            if type(observation) in (ProcessGroupAbsent, ProcessGroupZombiesOnly):
                pending.pop(0)
                continue
            if time.monotonic() >= deadline:
                raise TerminalSessionContainmentError(
                    "stalled terminal session group remains executable after "
                    f"forced containment: pgid={pending[0]}"
                )
            time.sleep(0.01)

    @staticmethod
    def _require_process(process: TerminalSessionProcess) -> None:
        if type(process) is not TerminalSessionProcess:
            raise ValueError(
                "PosixTerminalSessionProcessGroupTerminator requires "
                "TerminalSessionProcess"
            )

    @staticmethod
    def _log_containment(
        process: TerminalSessionProcess,
        outer_outcome: TerminalSessionTerminationOutcome,
        report: TerminalSessionContainmentReport,
    ) -> None:
        logger.info(
            "[terminal-containment] session stopped: pid=%s outer=%s "
            "terminal_owner=%s guardian_owner=%s cancellation_record=%s",
            process.process_id,
            outer_outcome.value,
            report.terminal_owner.value,
            report.guardian_owner.value,
            process.executor_cancellation.record_path,
        )

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
