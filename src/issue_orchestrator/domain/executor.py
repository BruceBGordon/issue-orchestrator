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
from pathlib import Path


MIN_EXECUTOR_AGGRESSIVENESS_PERCENT = 25
MAX_EXECUTOR_AGGRESSIVENESS_PERCENT = 400
EXECUTOR_SESSION_CANCELLATION_FILENAME = "executor-guardian-cancellation.json"

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
        if (
            not 1 <= len(self.value) <= 160
            or not self.value.isprintable()
            or not self.value.strip()
        ):
            raise ValueError(
                "ExecutorWorkKey.value must contain 1 through 160 printable "
                "Unicode characters and must not be whitespace-only"
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
            raise ValueError("ExecutorDeadlineExceededError.detail must not be empty")
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
        cls._require_monotonic_instant("submitted_at_monotonic", submitted_at_monotonic)
        cls._require_monotonic_instant("observed_at_monotonic", observed_at_monotonic)
        if observed_at_monotonic < submitted_at_monotonic:
            raise ValueError(
                "observed_at_monotonic must not precede submitted_at_monotonic"
            )
        return observed_at_monotonic - submitted_at_monotonic


ExecutorDeadline = ExecutorUnboundedDeadline | ExecutorBoundedDeadline


class ExecutorCommandLifecycle(StrEnum):
    """How an invocation relates to its submitting process and terminal."""

    DETACHED = "detached"
    INTERACTIVE_SESSION = "interactive-session"


    def require_cancellation_contract(self, cancellation: object, owner: str) -> None:
        """Enforce the one valid cancellation contract for this lifecycle."""
        if type(cancellation) not in (
            ExecutorNoCommandCancellation,
            ExecutorInteractiveSessionCancellation,
        ):
            raise ValueError(f"{owner}.cancellation must be an explicit typed contract")
        expected_type = (
            ExecutorNoCommandCancellation
            if self is ExecutorCommandLifecycle.DETACHED
            else ExecutorInteractiveSessionCancellation
        )
        if type(cancellation) is not expected_type:
            raise ValueError(f"{owner}.lifecycle and cancellation contract disagree")


class ExecutorSessionContainmentOutcome(StrEnum):
    """Exact durable-owner state observed while stopping a session."""

    ABSENT = "absent"
    STALE_RETIRED = "stale-retired"
    CONTAINED = "contained"


@dataclass(frozen=True, slots=True)
class ExecutorNoCommandCancellation:
    """Explicit lifecycle contract for a command with no session endpoint."""


@dataclass(frozen=True, slots=True)
class ExecutorInteractiveSessionCancellation:
    """Typed run-scoped location through which a terminal can stop a guardian."""

    record_path: Path

    def __post_init__(self) -> None:
        if not self.record_path.is_absolute():
            raise ValueError(
                "ExecutorInteractiveSessionCancellation.record_path must be an "
                "absolute Path"
            )

    @classmethod
    def for_run_dir(cls, run_dir: Path) -> ExecutorInteractiveSessionCancellation:
        if not run_dir.is_absolute():
            raise ValueError(
                "ExecutorInteractiveSessionCancellation.run_dir must be an "
                "absolute Path"
            )
        return cls(run_dir / EXECUTOR_SESSION_CANCELLATION_FILENAME)


ExecutorCommandCancellation = (
    ExecutorNoCommandCancellation | ExecutorInteractiveSessionCancellation
)


@dataclass(frozen=True, slots=True)
class ExecutorCommand:
    """Exact argument vector executed after admission."""

    arguments: tuple[str, ...]
    deadline: ExecutorDeadline
    lifecycle: ExecutorCommandLifecycle
    cancellation: ExecutorCommandCancellation

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
        _require_exact_type(
            type(self).__name__,
            "lifecycle",
            self.lifecycle,
            ExecutorCommandLifecycle,
        )
        self.lifecycle.require_cancellation_contract(
            self.cancellation,
            type(self).__name__,
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


@dataclass(frozen=True, slots=True)
class ExecutorCommandFinalizationFailure:
    """One named post-containment operation that failed."""

    attempt_name: str
    error: BaseException

    def __post_init__(self) -> None:
        if type(self.attempt_name) is not str or not self.attempt_name:
            raise ValueError(
                "ExecutorCommandFinalizationFailure.attempt_name must not be empty"
            )
        _require_base_exception(
            self.error,
            "ExecutorCommandFinalizationFailure.error",
        )


class ExecutorCommandFinalizationError(RuntimeError):
    """The command terminated, but its required finalization did not."""

    def __init__(
        self,
        command_result: ExecutorRunResult,
        failures: tuple[ExecutorCommandFinalizationFailure, ...],
    ) -> None:
        if type(command_result) is not ExecutorRunResult:
            raise ValueError(
                "ExecutorCommandFinalizationError.command_result must be an "
                "ExecutorRunResult"
            )
        if not failures or any(
            type(failure) is not ExecutorCommandFinalizationFailure
            for failure in failures
        ):
            raise ValueError(
                "ExecutorCommandFinalizationError.failures must contain "
                "ExecutorCommandFinalizationFailure values"
            )
        self.command_result = command_result
        self.failures = failures
        attempts = ", ".join(failure.attempt_name for failure in failures)
        super().__init__(
            "executor command terminated but finalization failed: "
            f"exit={command_result.exit_code} attempts={attempts}"
        )


def _require_base_exception(value: object, field_name: str) -> None:
    if not isinstance(value, BaseException):
        raise ValueError(f"{field_name} must be a BaseException")
