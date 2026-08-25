# pyright: strict
"""Executor-specific facade over stable owner-mediated cancellation."""

from __future__ import annotations

from dataclasses import dataclass
import math

from ..domain.executor import (
    ExecutorCommandCancellation,
    ExecutorInteractiveSessionCancellation,
    ExecutorNoCommandCancellation,
    ExecutorSessionContainmentOutcome,
)
from ..ports.atomic_record_store import AtomicRecordStoreFactory
from .process_cancellation_endpoint import (
    ProcessCancellationEndpointLease,
    ProcessCancellationEndpointOutcome,
    ProcessCancellationEndpointRequester,
    ProcessCancellationOwnerControls,
)


class ExecutorGuardianCancellationError(RuntimeError):
    """Compatibility-free domain error for invalid guardian cancellation."""


@dataclass(frozen=True, slots=True)
class NoExecutorGuardianCancellationControls:
    """Explicit absence of cancellation controls for detached work."""


ExecutorGuardianCancellationControls = (
    NoExecutorGuardianCancellationControls | ProcessCancellationOwnerControls
)


class NoExecutorGuardianCancellationLease:
    """Explicit no-channel implementation for detached executor commands."""

    def controls(self) -> NoExecutorGuardianCancellationControls:
        return NoExecutorGuardianCancellationControls()

    def activate(self) -> None:
        pass

    def transfer_to_owner(self) -> None:
        pass

    def retire(self) -> None:
        pass


class InteractiveExecutorGuardianCancellationLease:
    """Executor facade for one guardian-owned cancellation endpoint."""

    def __init__(
        self,
        cancellation: ExecutorInteractiveSessionCancellation,
        record_stores: AtomicRecordStoreFactory,
    ) -> None:
        if type(cancellation) is not ExecutorInteractiveSessionCancellation:
            raise ValueError(
                "InteractiveExecutorGuardianCancellationLease requires an "
                "ExecutorInteractiveSessionCancellation"
            )
        self._lease = ProcessCancellationEndpointLease(
            cancellation.record_path,
            record_stores,
        )

    def controls(self) -> ProcessCancellationOwnerControls:
        return self._lease.controls()

    def activate(self) -> None:
        self._lease.activate()

    def transfer_to_owner(self) -> None:
        self._lease.transfer_to_owner()

    def retire(self) -> None:
        self._lease.retire()


ExecutorGuardianCancellationLease = (
    NoExecutorGuardianCancellationLease | InteractiveExecutorGuardianCancellationLease
)


def prepare_executor_guardian_cancellation(
    cancellation: ExecutorCommandCancellation,
    record_stores: AtomicRecordStoreFactory,
) -> ExecutorGuardianCancellationLease:
    if type(cancellation) is ExecutorNoCommandCancellation:
        return NoExecutorGuardianCancellationLease()
    if type(cancellation) is ExecutorInteractiveSessionCancellation:
        return InteractiveExecutorGuardianCancellationLease(
            cancellation,
            record_stores,
        )
    raise ValueError("executor guardian cancellation requires a typed contract")


class ExecutorSessionGuardianCanceller:
    """Contain an active guardian through its stable owner endpoint."""

    def __init__(
        self,
        containment_timeout_seconds: float,
        record_stores: AtomicRecordStoreFactory,
    ) -> None:
        self._requester = ProcessCancellationEndpointRequester(
            containment_timeout_seconds,
            record_stores,
        )

    def contain_if_active(
        self,
        cancellation: ExecutorInteractiveSessionCancellation,
    ) -> ExecutorSessionContainmentOutcome:
        if type(cancellation) is not ExecutorInteractiveSessionCancellation:
            raise ValueError(
                "ExecutorSessionGuardianCanceller requires an "
                "ExecutorInteractiveSessionCancellation"
            )
        outcome = self._requester.contain(cancellation.record_path)
        if outcome is ProcessCancellationEndpointOutcome.ABSENT:
            return ExecutorSessionContainmentOutcome.ABSENT
        if outcome is ProcessCancellationEndpointOutcome.STALE_RETIRED:
            return ExecutorSessionContainmentOutcome.STALE_RETIRED
        if outcome is ProcessCancellationEndpointOutcome.CONTAINED:
            return ExecutorSessionContainmentOutcome.CONTAINED
        raise AssertionError("process cancellation outcome is a closed enum")

    def contain_if_active_before(
        self,
        cancellation: ExecutorInteractiveSessionCancellation,
        deadline: float,
    ) -> ExecutorSessionContainmentOutcome:
        if type(cancellation) is not ExecutorInteractiveSessionCancellation:
            raise ValueError(
                "ExecutorSessionGuardianCanceller requires an "
                "ExecutorInteractiveSessionCancellation"
            )
        if type(deadline) is not float or not math.isfinite(deadline):
            raise ValueError("guardian containment deadline must be finite")
        outcome = self._requester.contain_before(
            cancellation.record_path,
            deadline,
        )
        if outcome is ProcessCancellationEndpointOutcome.ABSENT:
            return ExecutorSessionContainmentOutcome.ABSENT
        if outcome is ProcessCancellationEndpointOutcome.STALE_RETIRED:
            return ExecutorSessionContainmentOutcome.STALE_RETIRED
        if outcome is ProcessCancellationEndpointOutcome.CONTAINED:
            return ExecutorSessionContainmentOutcome.CONTAINED
        raise AssertionError("process cancellation outcome is a closed enum")
