# pyright: strict
"""Private typed records shared inside the host executor adapter."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from ...control.executor_admission import (
    ExecutorAdmissionGrant,
    ExecutorResourceObservation,
)
from ...domain.executor import ExecutorWorkKey


@dataclass(frozen=True, slots=True)
class ExecutorRepositoryIdentity:
    """Canonical Git common directory and a human-readable repository label."""

    common_directory: Path
    label: str

    def __post_init__(self) -> None:
        if not self.common_directory.is_absolute():
            raise ValueError(
                "ExecutorRepositoryIdentity.common_directory must be absolute"
            )
        if not self.label:
            raise ValueError("ExecutorRepositoryIdentity.label must not be empty")

    @property
    def key(self) -> str:
        """Stable key shared by every worktree of the repository."""
        return str(self.common_directory)


@dataclass(frozen=True, slots=True)
class ExecutorWorkIdentity:
    """Repository-scoped human work identity."""

    repository: ExecutorRepositoryIdentity
    work_key: ExecutorWorkKey


@dataclass(frozen=True, slots=True)
class RecordedExecutorObservation:
    """Resource observation plus command outcome and recording time."""

    resources: ExecutorResourceObservation
    exit_code: int
    recorded_at_unix: float

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError("RecordedExecutorObservation.exit_code must be an integer")
        if (
            not math.isfinite(self.recorded_at_unix)
            or self.recorded_at_unix <= 0
        ):
            raise ValueError(
                "RecordedExecutorObservation.recorded_at_unix must be finite and "
                "positive"
            )


@dataclass(frozen=True, slots=True)
class ExecutedExecutorCommand:
    """Private command outcome retained for learning and diagnostics."""

    exit_code: int
    admission_grant: ExecutorAdmissionGrant
    resources: ExecutorResourceObservation

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError("ExecutedExecutorCommand.exit_code must be an integer")
        if self.admission_grant.concurrency != self.resources.concurrency:
            raise ValueError(
                "ExecutedExecutorCommand grant and resource concurrency must match"
            )
