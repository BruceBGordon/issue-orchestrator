"""Public vocabulary for the machine-wide executor deep module.

Repositories describe *what* may run and the concurrency range it supports.
They do not know how the host learns demand, arbitrates fairness, persists
leases, or supervises the process.  Those details stay behind the
:class:`~issue_orchestrator.ports.executor.Executor` port.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum


MIN_EXECUTOR_AGGRESSIVENESS_PERCENT = 25
MAX_EXECUTOR_AGGRESSIVENESS_PERCENT = 400

_WORK_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_GROUP_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_RESOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


def _require_positive_integer(owner: str, field: str, value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{owner}.{field} must be a positive integer")


def _require_exact_type(owner: str, field: str, value: object, expected: type) -> None:
    if type(value) is not expected:
        raise ValueError(f"{owner}.{field} must be an {expected.__name__}")


@dataclass(frozen=True, slots=True)
class ExecutorWorkKey:
    """Human-readable, repository-local identity for one observed work kind."""

    value: str

    def __post_init__(self) -> None:
        _require_exact_type(type(self).__name__, "value", self.value, str)
        if not _WORK_KEY_PATTERN.fullmatch(self.value):
            raise ValueError(
                "ExecutorWorkKey.value must match "
                "[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ExecutorFairnessGroup:
    """One top-level validation run whose lanes share fairness service."""

    value: str

    def __post_init__(self) -> None:
        _require_exact_type(type(self).__name__, "value", self.value, str)
        if not _GROUP_PATTERN.fullmatch(self.value):
            raise ValueError(
                "ExecutorFairnessGroup.value must match "
                "[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ExecutorExclusiveResource:
    """Host resource whose use is serialized for correctness."""

    value: str

    def __post_init__(self) -> None:
        _require_exact_type(type(self).__name__, "value", self.value, str)
        if not _RESOURCE_PATTERN.fullmatch(self.value):
            raise ValueError(
                "ExecutorExclusiveResource.value must match [a-z][a-z0-9-]*"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ExecutorConcurrencyRange:
    """Closed range of concurrency grants accepted by a command."""

    minimum_concurrency: int
    maximum_concurrency: int

    def __post_init__(self) -> None:
        _require_positive_integer(
            type(self).__name__, "minimum_concurrency", self.minimum_concurrency
        )
        _require_positive_integer(
            type(self).__name__, "maximum_concurrency", self.maximum_concurrency
        )
        if self.minimum_concurrency > self.maximum_concurrency:
            raise ValueError(
                "ExecutorConcurrencyRange.minimum_concurrency must not exceed "
                "maximum_concurrency"
            )


@dataclass(frozen=True, slots=True)
class ExecutorAggressiveness:
    """Validated percentage scaling learned admission pressure."""

    percent: int

    def __post_init__(self) -> None:
        if type(self.percent) is not int:
            raise ValueError("ExecutorAggressiveness.percent must be an integer")
        if not (
            MIN_EXECUTOR_AGGRESSIVENESS_PERCENT
            <= self.percent
            <= MAX_EXECUTOR_AGGRESSIVENESS_PERCENT
        ):
            raise ValueError(
                "ExecutorAggressiveness.percent must be between "
                f"{MIN_EXECUTOR_AGGRESSIVENESS_PERCENT} and "
                f"{MAX_EXECUTOR_AGGRESSIVENESS_PERCENT}"
            )


class ExecutorPolicySource(StrEnum):
    """Authority that supplied the effective machine policy."""

    DEFAULT = "default"
    PERSISTED = "persisted"
    ENVIRONMENT = "environment"


class ExecutorDeadlineReason(StrEnum):
    """Stable reason a bounded executor command exhausted its budget."""

    ACTIVE = "active"
    ABSOLUTE = "absolute"


class ExecutorDeadlinePhase(StrEnum):
    """Stable executor phase in which a bounded deadline fired."""

    ADMISSION = "admission"
    COMMAND = "command"


class ExecutorDeadlineExceededError(RuntimeError):
    """Raised when a fixed absolute deadline expires before admission."""

    def __init__(self, reason: ExecutorDeadlineReason, detail: str) -> None:
        if type(reason) is not ExecutorDeadlineReason:
            raise ValueError(
                "ExecutorDeadlineExceededError.reason must be ExecutorDeadlineReason"
            )
        if type(detail) is not str or not detail:
            raise ValueError(
                "ExecutorDeadlineExceededError.detail must not be empty"
            )
        self.reason = reason
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ExecutorPolicy:
    """Effective host policy."""

    aggressiveness: ExecutorAggressiveness
    source: ExecutorPolicySource

    def __post_init__(self) -> None:
        _require_exact_type(
            type(self).__name__,
            "aggressiveness",
            self.aggressiveness,
            ExecutorAggressiveness,
        )
        _require_exact_type(
            type(self).__name__, "source", self.source, ExecutorPolicySource
        )


@dataclass(frozen=True, slots=True)
class ExecutorPolicyChange:
    """Persisted policy and the possibly environment-overridden effective policy."""

    saved: ExecutorPolicy
    effective: ExecutorPolicy

    def __post_init__(self) -> None:
        _require_exact_type(type(self).__name__, "saved", self.saved, ExecutorPolicy)
        _require_exact_type(
            type(self).__name__, "effective", self.effective, ExecutorPolicy
        )


@dataclass(frozen=True, slots=True)
class ExecutorUnboundedDeadline:
    """Explicit declaration that a command owns its own termination policy."""


@dataclass(frozen=True, slots=True)
class ExecutorProcessTerminationPolicy:
    """Bounds for process-group courtesy shutdown and forced leader reap."""

    graceful_shutdown_seconds: float
    forceful_shutdown_seconds: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("graceful_shutdown_seconds", self.graceful_shutdown_seconds),
            ("forceful_shutdown_seconds", self.forceful_shutdown_seconds),
        ):
            if type(value) is not float or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"ExecutorProcessTerminationPolicy.{field_name} must be "
                    "finite and positive"
                )


@dataclass(frozen=True, slots=True)
class ExecutorHistoryRetentionPolicy:
    """Hard storage bounds for machine-wide learned work profiles."""

    maximum_profiles: int
    maximum_observations_per_profile: int

    def __post_init__(self) -> None:
        _require_positive_integer(
            type(self).__name__, "maximum_profiles", self.maximum_profiles
        )
        _require_positive_integer(
            type(self).__name__,
            "maximum_observations_per_profile",
            self.maximum_observations_per_profile,
        )


@dataclass(frozen=True, slots=True)
class ExecutorCommandBudget:
    """Finite post-admission wait and the deadline that limits it."""

    timeout_seconds: float
    reason: ExecutorDeadlineReason

    def __post_init__(self) -> None:
        if (
            type(self.timeout_seconds) is not float
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError(
                "ExecutorCommandBudget.timeout_seconds must be finite and positive"
            )
        _require_exact_type(
            type(self).__name__, "reason", self.reason, ExecutorDeadlineReason
        )


@dataclass(frozen=True, slots=True)
class ExecutorBoundedDeadline:
    """Active command budget plus a fixed submission-to-exit safety bound."""

    active_timeout_seconds: float
    absolute_timeout_seconds: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("active_timeout_seconds", self.active_timeout_seconds),
            ("absolute_timeout_seconds", self.absolute_timeout_seconds),
        ):
            if type(value) is not float or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"ExecutorBoundedDeadline.{field_name} must be finite and positive"
                )
        if self.absolute_timeout_seconds < self.active_timeout_seconds:
            raise ValueError(
                "ExecutorBoundedDeadline.absolute_timeout_seconds must be at least "
                "active_timeout_seconds"
            )

    def absolute_deadline(self, submitted_at_monotonic: float) -> float:
        self._require_monotonic_instant(
            "submitted_at_monotonic", submitted_at_monotonic
        )
        return submitted_at_monotonic + self.absolute_timeout_seconds

    def require_pending_at(
        self,
        *,
        submitted_at_monotonic: float,
        observed_at_monotonic: float,
    ) -> None:
        """Fail once queueing has consumed the fixed absolute safety bound."""
        elapsed = self._elapsed_since_submission(
            submitted_at_monotonic=submitted_at_monotonic,
            observed_at_monotonic=observed_at_monotonic,
        )
        if elapsed >= self.absolute_timeout_seconds:
            raise ExecutorDeadlineExceededError(
                ExecutorDeadlineReason.ABSOLUTE,
                "executor absolute deadline expired while awaiting admission",
            )

    def command_budget(
        self,
        *,
        submitted_at_monotonic: float,
        admitted_at_monotonic: float,
    ) -> ExecutorCommandBudget:
        """Return the active budget after excluding time safely spent queued.

        Queueing does not reduce the active allowance.  The independent absolute
        bound still wins when less time remains, preventing indefinite queueing.
        """
        elapsed = self._elapsed_since_submission(
            submitted_at_monotonic=submitted_at_monotonic,
            observed_at_monotonic=admitted_at_monotonic,
        )
        absolute_remaining = self.absolute_timeout_seconds - elapsed
        if absolute_remaining <= 0:
            raise ExecutorDeadlineExceededError(
                ExecutorDeadlineReason.ABSOLUTE,
                "executor absolute deadline expired at admission",
            )
        if absolute_remaining < self.active_timeout_seconds:
            return ExecutorCommandBudget(
                timeout_seconds=absolute_remaining,
                reason=ExecutorDeadlineReason.ABSOLUTE,
            )
        return ExecutorCommandBudget(
            timeout_seconds=self.active_timeout_seconds,
            reason=ExecutorDeadlineReason.ACTIVE,
        )

    @staticmethod
    def _require_monotonic_instant(field_name: str, value: float) -> None:
        if type(value) is not float or not math.isfinite(value) or value < 0:
            raise ValueError(f"{field_name} must be finite and non-negative")

    @classmethod
    def _elapsed_since_submission(
        cls,
        *,
        submitted_at_monotonic: float,
        observed_at_monotonic: float,
    ) -> float:
        cls._require_monotonic_instant(
            "submitted_at_monotonic", submitted_at_monotonic
        )
        cls._require_monotonic_instant(
            "observed_at_monotonic", observed_at_monotonic
        )
        if observed_at_monotonic < submitted_at_monotonic:
            raise ValueError(
                "observed_at_monotonic must not precede submitted_at_monotonic"
            )
        return observed_at_monotonic - submitted_at_monotonic


ExecutorDeadline = ExecutorUnboundedDeadline | ExecutorBoundedDeadline


@dataclass(frozen=True, slots=True)
class ExecutorCommand:
    """Exact argument vector executed after admission."""

    arguments: tuple[str, ...]
    deadline: ExecutorDeadline

    def __post_init__(self) -> None:
        _require_exact_type(type(self).__name__, "arguments", self.arguments, tuple)
        if not self.arguments or not self.arguments[0]:
            raise ValueError("ExecutorCommand.arguments requires an executable")
        if any(type(argument) is not str for argument in self.arguments):
            raise ValueError("ExecutorCommand.arguments must contain only strings")
        if any("\0" in argument for argument in self.arguments):
            raise ValueError("ExecutorCommand.arguments must not contain NUL bytes")
        if type(self.deadline) not in (
            ExecutorUnboundedDeadline,
            ExecutorBoundedDeadline,
        ):
            raise ValueError(
                "ExecutorCommand.deadline must be an explicit ExecutorDeadline"
            )


@dataclass(frozen=True, slots=True)
class ExecutorRunSpecification:
    """Complete scheduling semantics for one pooled command invocation."""

    work_key: ExecutorWorkKey
    fairness_group: ExecutorFairnessGroup
    concurrency_range: ExecutorConcurrencyRange
    exclusive_resources: tuple[ExecutorExclusiveResource, ...]

    def __post_init__(self) -> None:
        if type(self.work_key) is not ExecutorWorkKey:
            raise ValueError(
                "ExecutorRunSpecification.work_key must be an ExecutorWorkKey"
            )
        if type(self.fairness_group) is not ExecutorFairnessGroup:
            raise ValueError(
                "ExecutorRunSpecification.fairness_group must be an "
                "ExecutorFairnessGroup"
            )
        if type(self.concurrency_range) is not ExecutorConcurrencyRange:
            raise ValueError(
                "ExecutorRunSpecification.concurrency_range must be an "
                "ExecutorConcurrencyRange"
            )
        _require_exact_type(
            type(self).__name__,
            "exclusive_resources",
            self.exclusive_resources,
            tuple,
        )
        if any(
            type(resource) is not ExecutorExclusiveResource
            for resource in self.exclusive_resources
        ):
            raise ValueError(
                "ExecutorRunSpecification.exclusive_resources must contain "
                "ExecutorExclusiveResource values"
            )
        values = tuple(resource.value for resource in self.exclusive_resources)
        if len(values) != len(set(values)):
            raise ValueError(
                "ExecutorRunSpecification.exclusive_resources must not contain "
                "duplicates"
            )


@dataclass(frozen=True, slots=True)
class ExecutorConcurrencyGrant:
    """Concurrency granted to one admitted command."""

    concurrency: int

    def __post_init__(self) -> None:
        _require_positive_integer(type(self).__name__, "concurrency", self.concurrency)


@dataclass(frozen=True, slots=True)
class ExecutorRunResult:
    """Public result of one admitted command."""

    exit_code: int
    grant: ExecutorConcurrencyGrant

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError("ExecutorRunResult.exit_code must be an integer")
        if type(self.grant) is not ExecutorConcurrencyGrant:
            raise ValueError(
                "ExecutorRunResult.grant must be an ExecutorConcurrencyGrant"
            )
