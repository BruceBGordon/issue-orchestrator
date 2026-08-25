"""Typed terminal-session termination test adapter."""

from __future__ import annotations

from issue_orchestrator.domain.terminal_session_termination import (
    TerminalSessionContainmentReport,
    TerminalSessionOwnerContainmentOutcome,
    TerminalSessionProcess,
    TerminalSessionStatus,
    TerminalSessionTerminationOutcome,
    UnregisteredTerminalSessionOwnership,
)
from issue_orchestrator.domain.process_group import (
    ProcessBirthIdentity,
    ProcessIdentityObservation,
    ProcessIdentityPresent,
)


class RecordingTerminalSessionTerminator:
    """Record behavior-port invocations without signalling host processes."""

    def __init__(
        self,
        status: TerminalSessionStatus = TerminalSessionStatus.CONTAINED,
        termination_outcome: TerminalSessionTerminationOutcome = (
            TerminalSessionTerminationOutcome.ALREADY_CONTAINED
        ),
    ) -> None:
        if type(status) is not TerminalSessionStatus:
            raise ValueError("status must be TerminalSessionStatus")
        if type(termination_outcome) is not TerminalSessionTerminationOutcome:
            raise ValueError(
                "termination_outcome must be TerminalSessionTerminationOutcome"
            )
        self._status = status
        self._termination_outcome = termination_outcome
        self._processes: list[TerminalSessionProcess] = []
        self.recovered_ownership: list[UnregisteredTerminalSessionOwnership] = []
        self.identified_process_ids: list[int] = []

    @property
    def processes(self) -> tuple[TerminalSessionProcess, ...]:
        return tuple(self._processes)

    def identify(self, process_id: int) -> ProcessIdentityObservation:
        self.identified_process_ids.append(process_id)
        return ProcessIdentityPresent(
            ProcessBirthIdentity("darwin-timeval:1700000000:100"),
            process_id,
        )

    def status(self, process: TerminalSessionProcess) -> TerminalSessionStatus:
        if type(process) is not TerminalSessionProcess:
            raise ValueError(
                "RecordingTerminalSessionTerminator requires a TerminalSessionProcess"
            )
        return self._status

    def terminate(
        self,
        process: TerminalSessionProcess,
    ) -> TerminalSessionTerminationOutcome:
        if type(process) is not TerminalSessionProcess:
            raise ValueError(
                "RecordingTerminalSessionTerminator requires a TerminalSessionProcess"
            )
        self._processes.append(process)
        return self._termination_outcome

    def recover_unregistered(
        self,
        ownership: UnregisteredTerminalSessionOwnership,
    ) -> TerminalSessionContainmentReport:
        if type(ownership) is not UnregisteredTerminalSessionOwnership:
            raise ValueError(
                "RecordingTerminalSessionTerminator requires typed ownership"
            )
        self.recovered_ownership.append(ownership)
        return TerminalSessionContainmentReport(
            terminal_owner=TerminalSessionOwnerContainmentOutcome.ABSENT,
            guardian_owner=TerminalSessionOwnerContainmentOutcome.ABSENT,
        )
