"""Nested executor deadline transport contracts."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.executor import (
    ExecutorBoundedDeadline,
    ExecutorUnboundedDeadline,
)
from issue_orchestrator.infra.executor_deadline_environment import (
    EXECUTOR_DEADLINE_ENVIRONMENT,
)


def test_absent_deadline_environment_is_explicitly_unbounded() -> None:
    assert type(EXECUTOR_DEADLINE_ENVIRONMENT.decode({})) is (
        ExecutorUnboundedDeadline
    )


def test_bounded_deadline_round_trips_as_a_required_pair() -> None:
    deadline = ExecutorBoundedDeadline(30.0, 60.0)

    encoded = EXECUTOR_DEADLINE_ENVIRONMENT.encode({"BASE": "preserved"}, deadline)

    assert encoded["BASE"] == "preserved"
    assert EXECUTOR_DEADLINE_ENVIRONMENT.decode(encoded) == deadline


@pytest.mark.parametrize(
    "environment",
    (
        {"ISSUE_ORCHESTRATOR_EXECUTOR_ACTIVE_TIMEOUT_SECONDS": "30"},
        {"ISSUE_ORCHESTRATOR_EXECUTOR_ABSOLUTE_TIMEOUT_SECONDS": "60"},
    ),
)
def test_partial_deadline_environment_fails_fast(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="requires both"):
        EXECUTOR_DEADLINE_ENVIRONMENT.decode(environment)


def test_malformed_deadline_environment_fails_fast() -> None:
    environment = EXECUTOR_DEADLINE_ENVIRONMENT.encode(
        {},
        ExecutorBoundedDeadline(30.0, 60.0),
    )
    environment[
        EXECUTOR_DEADLINE_ENVIRONMENT.active_timeout_variable
    ] = "not-a-number"

    with pytest.raises(ValueError, match="must be numbers"):
        EXECUTOR_DEADLINE_ENVIRONMENT.decode(environment)
