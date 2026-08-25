# pyright: strict
"""Typed independent-attempt cleanup for multi-resource lifecycle owners."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn


@dataclass(frozen=True, slots=True)
class CleanupAction:
    """One named, no-argument cleanup operation."""

    name: str
    operation: Callable[[], object]

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("CleanupAction.name must be non-empty")
        if not callable(self.operation):
            raise ValueError("CleanupAction.operation must be callable")


@dataclass(frozen=True, slots=True)
class CleanupFailure:
    """One failed cleanup operation with its domain name preserved."""

    action_name: str
    error: BaseException


@dataclass(frozen=True, slots=True)
class CleanupSucceeded:
    """Every cleanup action completed."""


@dataclass(frozen=True, slots=True)
class CleanupFailed:
    """Every action was attempted and at least one failed."""

    failures: tuple[CleanupFailure, ...]

    def __post_init__(self) -> None:
        if not self.failures:
            raise ValueError("CleanupFailed requires at least one failure")


CleanupOutcome = CleanupSucceeded | CleanupFailed


@dataclass(frozen=True, slots=True)
class IndependentCleanupPlan:
    """Run every named action even when earlier cleanup actions fail."""

    actions: tuple[CleanupAction, ...]

    def run(self) -> CleanupOutcome:
        failures: list[CleanupFailure] = []
        for action in self.actions:
            try:
                action.operation()
            except BaseException as error:
                error.add_note(f"cleanup action failed: {action.name}")
                failures.append(CleanupFailure(action.name, error))
        if failures:
            return CleanupFailed(tuple(failures))
        return CleanupSucceeded()


def raise_cleanup_failures(
    message: str,
    outcome: CleanupOutcome,
) -> None:
    """Raise all failed cleanup operations after the complete plan ran."""
    if type(outcome) is CleanupSucceeded:
        return
    if type(outcome) is not CleanupFailed:
        raise AssertionError("cleanup outcome is a closed union")
    raise BaseExceptionGroup(
        message,
        tuple(failure.error for failure in outcome.failures),
    )


def raise_primary_with_cleanup(
    message: str,
    primary_error: BaseException,
    outcome: CleanupOutcome,
) -> NoReturn:
    """Preserve the initiating error beside every cleanup failure."""
    if type(outcome) is CleanupSucceeded:
        raise primary_error
    if type(outcome) is not CleanupFailed:
        raise AssertionError("cleanup outcome is a closed union")
    raise BaseExceptionGroup(
        message,
        (primary_error, *(failure.error for failure in outcome.failures)),
    )
