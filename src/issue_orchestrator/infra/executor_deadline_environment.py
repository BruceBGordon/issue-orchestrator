"""Typed environment transport for nested executor deadlines."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..domain.executor import (
    ExecutorBoundedDeadline,
    ExecutorDeadline,
    ExecutorUnboundedDeadline,
)
from .env import ENV_PREFIX


@dataclass(frozen=True, slots=True)
class ExecutorDeadlineEnvironment:
    """Encode and decode the complete nested-executor deadline contract."""

    active_timeout_variable: str
    absolute_timeout_variable: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("active_timeout_variable", self.active_timeout_variable),
            ("absolute_timeout_variable", self.absolute_timeout_variable),
        ):
            if type(value) is not str or not value:
                raise ValueError(
                    f"ExecutorDeadlineEnvironment.{field_name} must not be empty"
                )
        if self.active_timeout_variable == self.absolute_timeout_variable:
            raise ValueError(
                "ExecutorDeadlineEnvironment variables must be distinct"
            )

    def encode(
        self,
        environment: Mapping[str, str],
        deadline: ExecutorBoundedDeadline,
    ) -> dict[str, str]:
        """Return a copy carrying both required bounded-deadline values."""
        if type(deadline) is not ExecutorBoundedDeadline:
            raise ValueError(
                "ExecutorDeadlineEnvironment.encode requires ExecutorBoundedDeadline"
            )
        encoded = dict(environment)
        encoded[self.active_timeout_variable] = str(
            deadline.active_timeout_seconds
        )
        encoded[self.absolute_timeout_variable] = str(
            deadline.absolute_timeout_seconds
        )
        return encoded

    def decode(self, environment: Mapping[str, str]) -> ExecutorDeadline:
        """Decode either an explicit bounded pair or an explicit absence."""
        active_present = self.active_timeout_variable in environment
        absolute_present = self.absolute_timeout_variable in environment
        if not active_present and not absolute_present:
            return ExecutorUnboundedDeadline()
        if active_present != absolute_present:
            raise ValueError(
                "nested executor deadline requires both environment variables: "
                f"{self.active_timeout_variable}, {self.absolute_timeout_variable}"
            )
        try:
            active = float(environment[self.active_timeout_variable])
            absolute = float(environment[self.absolute_timeout_variable])
        except ValueError as exc:
            raise ValueError(
                "nested executor deadline environment values must be numbers"
            ) from exc
        return ExecutorBoundedDeadline(active, absolute)


EXECUTOR_DEADLINE_ENVIRONMENT = ExecutorDeadlineEnvironment(
    active_timeout_variable=f"{ENV_PREFIX}EXECUTOR_ACTIVE_TIMEOUT_SECONDS",
    absolute_timeout_variable=f"{ENV_PREFIX}EXECUTOR_ABSOLUTE_TIMEOUT_SECONDS",
)
