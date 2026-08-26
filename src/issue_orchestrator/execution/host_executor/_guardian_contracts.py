# pyright: strict
"""Strict private wire contracts for the executor guardian result pipe."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from ...domain.executor import ExecutorCommandLifecycle, ExecutorDeadlineReason
from ...domain.executor_guardian import (
    ExecutorGuardianActivationTimedOut,
    ExecutorGuardianBoundedBudget,
    ExecutorGuardianBudget,
    ExecutorGuardianCommandCompleted,
    ExecutorGuardianCommandInterrupted,
    ExecutorGuardianCommandResourceUsage,
    ExecutorGuardianCommandStartFailed,
    ExecutorGuardianCommandTimedOut,
    ExecutorGuardianInternalFailed,
    ExecutorGuardianResourceObservationFailed,
    ExecutorGuardianSerializedFailure,
    ExecutorGuardianTerminal,
    ExecutorGuardianTerminationPolicy,
    ExecutorGuardianUnboundedBudget,
)
from ...domain.process_group_sentinel import (
    ProcessGroupSentinelPolicy,
    ProcessGroupSentinelProgram,
)
from ._contracts import ExecutorStrictRecord


GUARDIAN_START_SIGNAL = b"S"


class GuardianUnboundedBudgetRecord(ExecutorStrictRecord):
    kind: Literal["unbounded"] = "unbounded"

    def to_domain(self) -> ExecutorGuardianUnboundedBudget:
        return ExecutorGuardianUnboundedBudget()


class GuardianBoundedBudgetRecord(ExecutorStrictRecord):
    kind: Literal["bounded"] = "bounded"
    expires_at_monotonic: float = Field(ge=0)
    reason: ExecutorDeadlineReason

    def to_domain(self) -> ExecutorGuardianBoundedBudget:
        return ExecutorGuardianBoundedBudget(
            self.expires_at_monotonic,
            self.reason,
        )


GuardianBudgetRecord = Annotated[
    GuardianUnboundedBudgetRecord | GuardianBoundedBudgetRecord,
    Field(discriminator="kind"),
]


class GuardianDetachedCancellationControlRecord(ExecutorStrictRecord):
    """Explicit absence of a cancellation listener for detached work."""

    kind: Literal["detached"] = "detached"


class GuardianInteractiveCancellationControlRecord(ExecutorStrictRecord):
    """Exact self-cancellation listener inherited by an interactive guardian."""

    kind: Literal["interactive"] = "interactive"
    listener_file_descriptor: int = Field(ge=0)
    owner_lock_file_descriptor: int = Field(ge=0)


GuardianCancellationControlRecord = Annotated[
    GuardianDetachedCancellationControlRecord
    | GuardianInteractiveCancellationControlRecord,
    Field(discriminator="kind"),
]


def guardian_budget_record(budget: ExecutorGuardianBudget) -> GuardianBudgetRecord:
    if type(budget) is ExecutorGuardianUnboundedBudget:
        return GuardianUnboundedBudgetRecord()
    if type(budget) is ExecutorGuardianBoundedBudget:
        return GuardianBoundedBudgetRecord(
            expires_at_monotonic=budget.expires_at_monotonic,
            reason=budget.reason,
        )
    raise ValueError("guardian budget record requires a typed budget")


class GuardianInvocationRecord(ExecutorStrictRecord):
    schema_version: Literal[5] = 5
    arguments: tuple[str, ...]
    result_file_descriptor: int = Field(ge=0)
    start_file_descriptor: int = Field(ge=0)
    owner_ready_file_descriptor: int = Field(ge=0)
    parent_lifetime_read_file_descriptor: int = Field(ge=0)
    lifecycle: ExecutorCommandLifecycle
    budget: GuardianBudgetRecord
    cancellation: GuardianCancellationControlRecord
    graceful_shutdown_seconds: float = Field(gt=0)
    sentinel_program: tuple[str, ...]
    sentinel_startup_timeout_seconds: float = Field(gt=0)
    lease_file_descriptors: tuple[int, ...]

    @classmethod
    def create(
        cls,
        *,
        arguments: tuple[str, ...],
        result_file_descriptor: int,
        start_file_descriptor: int,
        owner_ready_file_descriptor: int,
        parent_lifetime_read_file_descriptor: int,
        lifecycle: ExecutorCommandLifecycle,
        budget: ExecutorGuardianBudget,
        cancellation: GuardianCancellationControlRecord,
        termination_policy: ExecutorGuardianTerminationPolicy,
        sentinel_program: ProcessGroupSentinelProgram,
        sentinel_policy: ProcessGroupSentinelPolicy,
        lease_file_descriptors: tuple[int, ...],
    ) -> GuardianInvocationRecord:
        return cls(
            arguments=arguments,
            result_file_descriptor=result_file_descriptor,
            start_file_descriptor=start_file_descriptor,
            owner_ready_file_descriptor=owner_ready_file_descriptor,
            parent_lifetime_read_file_descriptor=(parent_lifetime_read_file_descriptor),
            lifecycle=lifecycle,
            budget=guardian_budget_record(budget),
            cancellation=cancellation,
            graceful_shutdown_seconds=(termination_policy.graceful_shutdown_seconds),
            sentinel_program=sentinel_program.arguments,
            sentinel_startup_timeout_seconds=(sentinel_policy.startup_timeout_seconds),
            lease_file_descriptors=lease_file_descriptors,
        )

    def domain_budget(self) -> ExecutorGuardianBudget:
        return self.budget.to_domain()

    def termination_policy(self) -> ExecutorGuardianTerminationPolicy:
        return ExecutorGuardianTerminationPolicy(self.graceful_shutdown_seconds)

    def process_group_sentinel_program(self) -> ProcessGroupSentinelProgram:
        return ProcessGroupSentinelProgram(self.sentinel_program)

    def process_group_sentinel_policy(self) -> ProcessGroupSentinelPolicy:
        return ProcessGroupSentinelPolicy(
            graceful_shutdown_seconds=self.graceful_shutdown_seconds,
            startup_timeout_seconds=self.sentinel_startup_timeout_seconds,
        )


class GuardianAvailableCommandResourceRecord(ExecutorStrictRecord):
    availability: Literal["available"] = "available"
    wall_seconds: float = Field(gt=0)
    cpu_seconds: float = Field(ge=0)
    max_rss_bytes: int = Field(ge=0)
    input_blocks: int = Field(ge=0)
    output_blocks: int = Field(ge=0)

    def to_domain(self) -> ExecutorGuardianCommandResourceUsage:
        return ExecutorGuardianCommandResourceUsage(
            wall_seconds=self.wall_seconds,
            cpu_seconds=self.cpu_seconds,
            guardian_process_lifetime_children_max_rss_bytes=self.max_rss_bytes,
            input_blocks=self.input_blocks,
            output_blocks=self.output_blocks,
        )


class GuardianUnavailableCommandResourceRecord(ExecutorStrictRecord):
    availability: Literal["unavailable"] = "unavailable"
    error_type: str = Field(min_length=1)
    error_repr: str = Field(min_length=1)

    def to_domain(self) -> ExecutorGuardianResourceObservationFailed:
        return ExecutorGuardianResourceObservationFailed(
            self.error_type,
            self.error_repr,
        )


GuardianCommandResourceRecord = Annotated[
    GuardianAvailableCommandResourceRecord | GuardianUnavailableCommandResourceRecord,
    Field(discriminator="availability"),
]


class GuardianCompletedRecord(ExecutorStrictRecord):
    outcome: Literal["completed"] = "completed"
    exit_code: int
    resources: GuardianCommandResourceRecord

    def to_domain(self) -> ExecutorGuardianCommandCompleted:
        return ExecutorGuardianCommandCompleted(
            self.exit_code,
            self.resources.to_domain(),
        )


class GuardianCommandInterruptedRecord(ExecutorStrictRecord):
    outcome: Literal["interrupted"] = "interrupted"
    signal_number: int = Field(gt=0)

    def to_domain(self) -> ExecutorGuardianCommandInterrupted:
        return ExecutorGuardianCommandInterrupted(self.signal_number)


class GuardianTimedOutRecord(ExecutorStrictRecord):
    outcome: Literal["timed-out"] = "timed-out"
    reason: ExecutorDeadlineReason

    def to_domain(self) -> ExecutorGuardianCommandTimedOut:
        return ExecutorGuardianCommandTimedOut(self.reason)


class GuardianSerializedFailureRecord(ExecutorStrictRecord):
    operation: str = Field(min_length=1)
    error_type: str = Field(min_length=1)
    error_repr: str = Field(min_length=1)

    def to_domain(self) -> ExecutorGuardianSerializedFailure:
        return ExecutorGuardianSerializedFailure(
            self.operation,
            self.error_type,
            self.error_repr,
        )


class GuardianActivationTimedOutRecord(ExecutorStrictRecord):
    outcome: Literal["activation-timed-out"] = "activation-timed-out"
    reason: ExecutorDeadlineReason
    recovery_failures: tuple[GuardianSerializedFailureRecord, ...]

    def to_domain(self) -> ExecutorGuardianActivationTimedOut:
        return ExecutorGuardianActivationTimedOut(
            self.reason,
            tuple(failure.to_domain() for failure in self.recovery_failures),
        )


class GuardianCommandStartFailedRecord(ExecutorStrictRecord):
    outcome: Literal["command-start-failed"] = "command-start-failed"
    error_type: str = Field(min_length=1)
    error_repr: str = Field(min_length=1)

    def to_domain(self) -> ExecutorGuardianCommandStartFailed:
        return ExecutorGuardianCommandStartFailed(
            self.error_type,
            self.error_repr,
        )


class GuardianInternalFailedRecord(ExecutorStrictRecord):
    outcome: Literal["guardian-internal-failed"] = "guardian-internal-failed"
    error_type: str = Field(min_length=1)
    error_repr: str = Field(min_length=1)

    def to_domain(self) -> ExecutorGuardianInternalFailed:
        return ExecutorGuardianInternalFailed(self.error_type, self.error_repr)


GuardianTerminalRecord = Annotated[
    GuardianCompletedRecord
    | GuardianCommandInterruptedRecord
    | GuardianActivationTimedOutRecord
    | GuardianTimedOutRecord
    | GuardianCommandStartFailedRecord
    | GuardianInternalFailedRecord,
    Field(discriminator="outcome"),
]

GUARDIAN_TERMINAL_ADAPTER: TypeAdapter[GuardianTerminalRecord] = TypeAdapter(
    GuardianTerminalRecord
)


def guardian_terminal_record(
    terminal: ExecutorGuardianTerminal,
) -> GuardianTerminalRecord:
    if type(terminal) is ExecutorGuardianCommandCompleted:
        resources = terminal.resources
        if type(resources) is ExecutorGuardianCommandResourceUsage:
            resource_record: GuardianCommandResourceRecord = (
                GuardianAvailableCommandResourceRecord(
                    wall_seconds=resources.wall_seconds,
                    cpu_seconds=resources.cpu_seconds,
                    max_rss_bytes=(
                        resources.guardian_process_lifetime_children_max_rss_bytes
                    ),
                    input_blocks=resources.input_blocks,
                    output_blocks=resources.output_blocks,
                )
            )
        elif type(resources) is ExecutorGuardianResourceObservationFailed:
            resource_record = GuardianUnavailableCommandResourceRecord(
                error_type=resources.error_type,
                error_repr=resources.error_repr,
            )
        else:
            raise AssertionError("guardian command resources are a closed union")
        return GuardianCompletedRecord(
            exit_code=terminal.exit_code,
            resources=resource_record,
        )
    if type(terminal) is ExecutorGuardianCommandInterrupted:
        return GuardianCommandInterruptedRecord(
            signal_number=terminal.signal_number,
        )
    if type(terminal) is ExecutorGuardianActivationTimedOut:
        return GuardianActivationTimedOutRecord(
            reason=terminal.reason,
            recovery_failures=tuple(
                GuardianSerializedFailureRecord(
                    operation=failure.operation,
                    error_type=failure.error_type,
                    error_repr=failure.error_repr,
                )
                for failure in terminal.recovery_failures
            ),
        )
    if type(terminal) is ExecutorGuardianCommandTimedOut:
        return GuardianTimedOutRecord(reason=terminal.reason)
    if type(terminal) is ExecutorGuardianCommandStartFailed:
        return GuardianCommandStartFailedRecord(
            error_type=terminal.error_type,
            error_repr=terminal.error_repr,
        )
    if type(terminal) is ExecutorGuardianInternalFailed:
        return GuardianInternalFailedRecord(
            error_type=terminal.error_type,
            error_repr=terminal.error_repr,
        )
    raise ValueError("guardian terminal record requires a typed terminal fact")
