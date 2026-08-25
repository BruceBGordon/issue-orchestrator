"""Real agent-phase command adapter for launcher boundary tests."""

from __future__ import annotations

import sys
import shlex
from pathlib import Path

from issue_orchestrator.execution.agent_phase_command_scheduler import (
    HostAgentPhaseCommandScheduler,
)
from issue_orchestrator.domain.agent_phase_execution import (
    AgentPhaseOuterWatchdogPolicy,
)
from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.domain.terminal_launch import TerminalShell


def host_agent_phase_command_scheduler() -> HostAgentPhaseCommandScheduler:
    """Return the production renderer with deterministic host executables."""
    return HostAgentPhaseCommandScheduler(
        # Preserve the virtual-environment launcher. Resolving this symlink
        # selects the base interpreter and loses the editable package context.
        python_executable=Path(sys.executable),
        application_shell=TerminalShell.BASH,
        outer_watchdog_policy=AgentPhaseOuterWatchdogPolicy(
            executor_termination=ExecutorProcessTerminationPolicy(2.0),
            observer_margin_seconds=58.0,
        ),
    )


def scheduled_agent_shell_command(rendered_command: str) -> str:
    """Extract the typed phase's application-owned shell command."""
    arguments = shlex.split(rendered_command)
    separator = arguments.index("--")
    shell_invocation = arguments[separator + 1 :]
    if shell_invocation[:2] != ["/bin/bash", "-lc"] or len(shell_invocation) != 3:
        raise AssertionError(
            f"unexpected scheduled agent shell invocation: {shell_invocation!r}"
        )
    return shell_invocation[2]
