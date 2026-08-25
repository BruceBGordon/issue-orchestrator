# pyright: strict
"""Strict private wire contracts for the executor guardian result pipe."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from ...domain.executor import ExecutorDeadlineReason
from ...domain.executor_guardian import (
    ExecutorGuardianBoundedBudget,
    ExecutorGuardianBudget,
    ExecutorGuardianCommandCompleted,
    ExecutorGuardianCommandStartFailed,
    ExecutorGuardianCommandTimedOut,
    ExecutorGuardianInternalFailed,
    ExecutorGuardianTerminal,
    ExecutorGuardianTerminationPolicy,
    ExecutorGuardianUnboundedBudget,
)
from ._contracts import ExecutorStrictRecord


class GuardianUnboundedBudgetRecord(ExecutorStrictRecord):
    kind: Literal["unbounded"] = "unbounded"

    def to_domain(self) -> ExecutorGuardianUnboundedBudget:
        return ExecutorGuardianUnboundedBudget()


class GuardianBoundedBudgetRecord(ExecutorStrictRecord):
    kind: Literal["bounded"] = "bounded"
    timeout_seconds: float = Field(gt=0)
    reason: ExecutorDeadlineReason

    def to_domain(self) -> ExecutorGuardianBoundedBudget:
        return ExecutorGuardianBoundedBudget(self.timeout_seconds, self.reason)


GuardianBudgetRecord = Annotated[
    GuardianUnboundedBudgetRecord | GuardianBoundedBudgetRecord,
    Field(discriminator="kind"),
]


def guardian_budget_record(budget: ExecutorGuardianBudget) -> GuardianBudgetRecord:
    if type(budget) is ExecutorGuardianUnboundedBudget:
        return GuardianUnboundedBudgetRecord()
    if type(budget) is ExecutorGuardianBoundedBudget:
        return GuardianBoundedBudgetRecord(
            timeout_seconds=budget.timeout_seconds,
            reason=budget.reason,
        )
    raise ValueError("guardian budget record requires a typed budget")


class GuardianInvocationRecord(ExecutorStrictRecord):
    schema_version: Literal[1] = 1
    arguments: tuple[str, ...]
    result_file_descriptor: int = Field(ge=0)
    budget: GuardianBudgetRecord
    graceful_shutdown_seconds: float = Field(gt=0)

    @classmethod
    def create(
        cls,
        *,
        arguments: tuple[str, ...],
        result_file_descriptor: int,
        budget: ExecutorGuardianBudget,
        termination_policy: ExecutorGuardianTerminationPolicy,
    ) -> GuardianInvocationRecord:
        return cls(
            arguments=arguments,
            result_file_descriptor=result_file_descriptor,
            budget=guardian_budget_record(budget),
            graceful_shutdown_seconds=(termination_policy.graceful_shutdown_seconds),
        )

    def domain_budget(self) -> ExecutorGuardianBudget:
        return self.budget.to_domain()

    def termination_policy(self) -> ExecutorGuardianTerminationPolicy:
        return ExecutorGuardianTerminationPolicy(self.graceful_shutdown_seconds)


class GuardianCompletedRecord(ExecutorStrictRecord):
    outcome: Literal["completed"] = "completed"
    exit_code: int

    def to_domain(self) -> ExecutorGuardianCommandCompleted:
        return ExecutorGuardianCommandCompleted(self.exit_code)


class GuardianTimedOutRecord(ExecutorStrictRecord):
    outcome: Literal["timed-out"] = "timed-out"
    reason: ExecutorDeadlineReason

    def to_domain(self) -> ExecutorGuardianCommandTimedOut:
        return ExecutorGuardianCommandTimedOut(self.reason)


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
        return GuardianCompletedRecord(exit_code=terminal.exit_code)
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
