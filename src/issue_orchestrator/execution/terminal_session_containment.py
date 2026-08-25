# pyright: strict
"""Coordinate terminal and executor self-containment endpoint owners."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time

from ..domain.executor import (
    ExecutorInteractiveSessionCancellation,
    ExecutorSessionContainmentOutcome,
)
from ..domain.terminal_session_termination import (
    TerminalSessionContainmentReport,
    TerminalSessionOwnerContainmentOutcome,
    TerminalSessionProcess,
    TerminalSessionOwnerCancellation,
    UnregisteredTerminalSessionOwnership,
)
from .executor_guardian_cancellation import ExecutorSessionGuardianCanceller
from .process_cancellation_endpoint import (
    ProcessCancellationEndpointOutcome,
    ProcessCancellationEndpointRequester,
)


@dataclass(frozen=True, slots=True)
class _ContainmentSucceeded:
    outcome: TerminalSessionOwnerContainmentOutcome

    def __post_init__(self) -> None:
        if type(self.outcome) is not TerminalSessionOwnerContainmentOutcome:
            raise ValueError("containment success requires an exact outcome")


@dataclass(frozen=True, slots=True)
class _ContainmentFailed:
    error: BaseException


_ContainmentAttempt = _ContainmentSucceeded | _ContainmentFailed


class OwnerMediatedTerminalSessionContainment:
    """Contain the outer owner first, then any independently live guardian."""

    def __init__(
        self,
        outer_requester: ProcessCancellationEndpointRequester,
        guardian_canceller: ExecutorSessionGuardianCanceller,
        containment_timeout_seconds: float,
    ) -> None:
        if type(outer_requester) is not ProcessCancellationEndpointRequester:
            raise ValueError(
                "outer_requester must be ProcessCancellationEndpointRequester"
            )
        if type(guardian_canceller) is not ExecutorSessionGuardianCanceller:
            raise ValueError(
                "guardian_canceller must be ExecutorSessionGuardianCanceller"
            )
        if (
            type(containment_timeout_seconds) is not float
            or not math.isfinite(containment_timeout_seconds)
            or containment_timeout_seconds <= 0
        ):
            raise ValueError(
                "containment_timeout_seconds must be finite and positive"
            )
        self._outer_requester = outer_requester
        self._guardian_canceller = guardian_canceller
        self._containment_timeout_seconds = containment_timeout_seconds

    def contain(
        self,
        process: TerminalSessionProcess,
    ) -> TerminalSessionContainmentReport:
        if type(process) is not TerminalSessionProcess:
            raise ValueError(
                "OwnerMediatedTerminalSessionContainment requires "
                "TerminalSessionProcess"
            )
        return self._contain_endpoints(
            process.terminal_cancellation,
            process.executor_cancellation,
        )

    def contain_unregistered(
        self,
        ownership: UnregisteredTerminalSessionOwnership,
    ) -> TerminalSessionContainmentReport:
        if type(ownership) is not UnregisteredTerminalSessionOwnership:
            raise ValueError(
                "contain_unregistered requires UnregisteredTerminalSessionOwnership"
            )
        return self._contain_endpoints(
            ownership.terminal_cancellation,
            ownership.executor_cancellation,
        )

    def _contain_endpoints(
        self,
        terminal_cancellation: TerminalSessionOwnerCancellation,
        executor_cancellation: ExecutorInteractiveSessionCancellation,
    ) -> TerminalSessionContainmentReport:
        deadline = time.monotonic() + self._containment_timeout_seconds
        outer = self._attempt_outer(terminal_cancellation, deadline)
        guardian = self._attempt_guardian(executor_cancellation, deadline)
        failures = tuple(
            attempt.error
            for attempt in (outer, guardian)
            if type(attempt) is _ContainmentFailed
        )
        if failures:
            raise BaseExceptionGroup(
                "terminal session owner containment failed",
                failures,
            )
        if type(outer) is not _ContainmentSucceeded or type(
            guardian
        ) is not _ContainmentSucceeded:
            raise AssertionError("containment attempt is a closed union")
        return TerminalSessionContainmentReport(
            terminal_owner=outer.outcome,
            guardian_owner=guardian.outcome,
        )

    def _attempt_outer(
        self,
        cancellation: TerminalSessionOwnerCancellation,
        deadline: float,
    ) -> _ContainmentAttempt:
        try:
            outcome = self._outer_requester.contain_before(
                cancellation.record_path,
                deadline,
            )
            if outcome is ProcessCancellationEndpointOutcome.ABSENT:
                mapped = TerminalSessionOwnerContainmentOutcome.ABSENT
            elif outcome is ProcessCancellationEndpointOutcome.STALE_RETIRED:
                mapped = TerminalSessionOwnerContainmentOutcome.STALE_RETIRED
            elif outcome is ProcessCancellationEndpointOutcome.CONTAINED:
                mapped = TerminalSessionOwnerContainmentOutcome.CONTAINED
            else:
                raise AssertionError("outer endpoint outcome is a closed enum")
            return _ContainmentSucceeded(mapped)
        except BaseException as error:
            error.add_note("outer terminal owner containment failed")
            return _ContainmentFailed(error)

    def _attempt_guardian(
        self,
        cancellation: ExecutorInteractiveSessionCancellation,
        deadline: float,
    ) -> _ContainmentAttempt:
        try:
            outcome = self._guardian_canceller.contain_if_active_before(
                cancellation,
                deadline,
            )
            if outcome is ExecutorSessionContainmentOutcome.ABSENT:
                mapped = TerminalSessionOwnerContainmentOutcome.ABSENT
            elif outcome is ExecutorSessionContainmentOutcome.STALE_RETIRED:
                mapped = TerminalSessionOwnerContainmentOutcome.STALE_RETIRED
            elif outcome is ExecutorSessionContainmentOutcome.CONTAINED:
                mapped = TerminalSessionOwnerContainmentOutcome.CONTAINED
            else:
                raise AssertionError("guardian endpoint outcome is a closed enum")
            return _ContainmentSucceeded(mapped)
        except BaseException as error:
            error.add_note("executor guardian containment failed")
            return _ContainmentFailed(error)
