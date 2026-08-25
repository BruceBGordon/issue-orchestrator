"""Port for crash-resilient execution after a host lease is admitted."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from ..domain.executor_guardian import (
    ExecutorGuardianBoundedBudget,
    ExecutorGuardianBudget,
    ExecutorGuardianTerminal,
    ExecutorGuardianUnboundedBudget,
)


@dataclass(frozen=True, slots=True)
class ExecutorGuardianRequest:
    """Exact command, environment, locks, and budget transferred to a guardian."""

    arguments: tuple[str, ...]
    environment: Mapping[str, str]
    lease_file_descriptors: tuple[int, ...]
    budget: ExecutorGuardianBudget

    def __post_init__(self) -> None:
        owner = type(self).__name__
        if type(self.arguments) is not tuple or not self.arguments:
            raise ValueError(f"{owner}.arguments must be a non-empty tuple")
        if not self.arguments[0] or any(
            type(argument) is not str for argument in self.arguments
        ):
            raise ValueError(
                f"{owner}.arguments must contain strings and name an executable"
            )
        if any("\0" in argument for argument in self.arguments):
            raise ValueError(f"{owner}.arguments must not contain NUL bytes")
        environment = dict(self.environment)
        if any(
            type(key) is not str
            or not key
            or "=" in key
            or "\0" in key
            or type(value) is not str
            or "\0" in value
            for key, value in environment.items()
        ):
            raise ValueError(
                f"{owner}.environment must contain valid process environment strings"
            )
        object.__setattr__(self, "environment", MappingProxyType(environment))
        if type(self.lease_file_descriptors) is not tuple or not (
            self.lease_file_descriptors
        ):
            raise ValueError(
                f"{owner}.lease_file_descriptors must be a non-empty tuple"
            )
        if any(
            type(descriptor) is not int or descriptor <= 2
            for descriptor in self.lease_file_descriptors
        ):
            raise ValueError(
                f"{owner}.lease_file_descriptors must contain descriptors above 2"
            )
        if len(self.lease_file_descriptors) != len(set(self.lease_file_descriptors)):
            raise ValueError(
                f"{owner}.lease_file_descriptors must not contain duplicates"
            )
        if type(self.budget) not in (
            ExecutorGuardianUnboundedBudget,
            ExecutorGuardianBoundedBudget,
        ):
            raise ValueError(f"{owner}.budget must be an explicit guardian budget")


@runtime_checkable
class ExecutorCommandGuardian(Protocol):
    """Own the child guardian, typed result channel, and final group cleanup."""

    def run(self, request: ExecutorGuardianRequest) -> ExecutorGuardianTerminal:
        """Return only after the guardian process group has been contained."""
        ...
