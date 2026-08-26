"""Typed boundary for durable, bounded validation stream capture."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast, Protocol, runtime_checkable

from ..domain.validation_execution import (
    ValidationCommandOutput,
    ValidationCommandOutputCapture,
)


class ValidationOutputStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class ValidationOutputJournalResult:
    output: ValidationCommandOutput
    failure: BaseException | None

    def __post_init__(self) -> None:
        if type(self.output) is not ValidationCommandOutput:
            raise ValueError("validation journal output must be typed")
        if self.failure is not None and not isinstance(cast(object, self.failure), BaseException):
            raise ValueError(
                "validation journal failure must be absent or an exception"
            )


@runtime_checkable
class ValidationOutputJournal(Protocol):
    """Own complete journals and bounded in-memory tails for both streams."""

    def append(self, stream: ValidationOutputStream, payload: bytes) -> None:
        """Persist one non-empty payload before returning."""
        ...

    def finalize(self) -> ValidationOutputJournalResult:
        """Attempt to sync/close both journals and return retained tails."""
        ...


@runtime_checkable
class ValidationOutputJournalFactory(Protocol):
    def create(
        self,
        capture: ValidationCommandOutputCapture,
    ) -> ValidationOutputJournal:
        """Create and truncate both journals or raise after partial cleanup."""
        ...
