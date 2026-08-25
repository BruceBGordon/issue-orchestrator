"""Port for whole-process-tree-contained validation commands."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.validation_execution import (
    ContainedValidationCommand,
    ValidationCommandExecution,
)


@runtime_checkable
class ValidationCommandRunner(Protocol):
    """Return only after a validation process tree has a closed lifecycle."""

    def run(self, command: ContainedValidationCommand) -> ValidationCommandExecution:
        """Execute one typed validation request."""
        ...
