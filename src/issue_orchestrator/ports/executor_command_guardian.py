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
from ..domain.executor import (
    ExecutorCommandCancellation,
    ExecutorCommandLifecycle,
)


@runtime_checkable
class ExecutorGuardianLeaseTransfer(Protocol):
    """Transfer admitted lock ownership to a successfully spawned guardian."""

    def inherited_file_descriptors(self) -> tuple[int, ...]:
        """Return the locally owned descriptors the guardian must inherit."""
        ...

    def transfer_to_guardian(self) -> None:
        """Close local descriptor ownership without unlocking the guardian copy."""
        ...


def _require_guardian_arguments(owner: str, arguments: tuple[str, ...]) -> None:
    if type(arguments) is not tuple or not arguments:
        raise ValueError(f"{owner}.arguments must be a non-empty tuple")
    if not arguments[0] or any(type(argument) is not str for argument in arguments):
        raise ValueError(
            f"{owner}.arguments must contain strings and name an executable"
        )
    if any("\0" in argument for argument in arguments):
        raise ValueError(f"{owner}.arguments must not contain NUL bytes")


def _validated_environment(
    owner: str,
    environment: Mapping[str, str],
) -> MappingProxyType[str, str]:
    exact_environment = dict(environment)
    if any(
        type(key) is not str
        or not key
        or "=" in key
        or "\0" in key
        or type(value) is not str
        or "\0" in value
        for key, value in exact_environment.items()
    ):
        raise ValueError(
            f"{owner}.environment must contain valid process environment strings"
        )
    return MappingProxyType(exact_environment)


def _require_guardian_lease(
    owner: str,
    lease: object,
) -> ExecutorGuardianLeaseTransfer:
    if not isinstance(lease, ExecutorGuardianLeaseTransfer):
        raise ValueError(f"{owner}.lease must implement ExecutorGuardianLeaseTransfer")
    descriptors = lease.inherited_file_descriptors()
    if type(descriptors) is not tuple or not descriptors:
        raise ValueError(f"{owner}.lease must own at least one descriptor")
    if any(type(descriptor) is not int or descriptor <= 2 for descriptor in descriptors):
        raise ValueError(f"{owner}.lease descriptors must all be above 2")
    if len(descriptors) != len(set(descriptors)):
        raise ValueError(f"{owner}.lease descriptors must not contain duplicates")
    return lease


@dataclass(frozen=True, slots=True)
class ExecutorGuardianRequest:
    """Exact command, environment, locks, and budget transferred to a guardian."""

    arguments: tuple[str, ...]
    environment: Mapping[str, str]
    lease: ExecutorGuardianLeaseTransfer
    budget: ExecutorGuardianBudget
    lifecycle: ExecutorCommandLifecycle
    cancellation: ExecutorCommandCancellation

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _require_guardian_arguments(owner, self.arguments)
        object.__setattr__(
            self,
            "environment",
            _validated_environment(owner, self.environment),
        )
        object.__setattr__(self, "lease", _require_guardian_lease(owner, self.lease))
        if type(self.budget) not in (
            ExecutorGuardianUnboundedBudget,
            ExecutorGuardianBoundedBudget,
        ):
            raise ValueError(f"{owner}.budget must be an explicit guardian budget")
        if type(self.lifecycle) is not ExecutorCommandLifecycle:
            raise ValueError(f"{owner}.lifecycle must be an ExecutorCommandLifecycle")
        self.lifecycle.require_cancellation_contract(self.cancellation, owner)


@runtime_checkable
class ExecutorCommandGuardian(Protocol):
    """Own the child guardian, typed result channel, and final group cleanup."""

    def run(self, request: ExecutorGuardianRequest) -> ExecutorGuardianTerminal:
        """Return only after the guardian process group has been contained."""
        ...
