"""Typed terminal-session termination test adapter."""

from __future__ import annotations

from issue_orchestrator.domain.terminal_session_termination import (
    TerminalSessionProcess,
)


class RecordingTerminalSessionTerminator:
    """Record behavior-port invocations without signalling host processes."""

    def __init__(self) -> None:
        self._processes: list[TerminalSessionProcess] = []

    @property
    def processes(self) -> tuple[TerminalSessionProcess, ...]:
        return tuple(self._processes)

    def terminate(self, process: TerminalSessionProcess) -> None:
        if type(process) is not TerminalSessionProcess:
            raise ValueError(
                "RecordingTerminalSessionTerminator requires a TerminalSessionProcess"
            )
        self._processes.append(process)
