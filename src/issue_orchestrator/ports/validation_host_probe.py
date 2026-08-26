"""Port for one authoritatively contained validation host probe."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.validation_resource_sampling import (
    ValidationHostProbeRequest,
    ValidationHostProbeResult,
)


@runtime_checkable
class ValidationHostProbe(Protocol):
    """Execute one bounded argv request without leaking a process descendant."""

    def run(self, request: ValidationHostProbeRequest) -> ValidationHostProbeResult:
        """Return exact successful output or one closed unavailable lifecycle."""
        ...
