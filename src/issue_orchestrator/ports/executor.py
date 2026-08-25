"""Public behavior port for the machine-wide executor deep module."""

from __future__ import annotations

from typing import Protocol

from ..domain.executor import (
    ExecutorAggressiveness,
    ExecutorCommand,
    ExecutorPolicy,
    ExecutorPolicyChange,
    ExecutorRunSpecification,
    ExecutorRunResult,
)


class Executor(Protocol):
    """Run repository work under shared host policy."""

    def run(
        self,
        specification: ExecutorRunSpecification,
        command: ExecutorCommand,
    ) -> ExecutorRunResult:
        """Admit, execute, observe, and record one command."""
        ...

    def policy(self) -> ExecutorPolicy:
        """Return the effective machine-wide executor policy."""
        ...

    def configure_policy(
        self,
        aggressiveness: ExecutorAggressiveness,
    ) -> ExecutorPolicyChange:
        """Persist aggressiveness and return saved plus effective policy."""
        ...
