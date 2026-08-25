"""Public-boundary tests for contained validation execution."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from issue_orchestrator.domain.contained_command import ContainedCommandOutputPolicy
from issue_orchestrator.domain.executor import (
    ExecutorBoundedDeadline,
    ExecutorProcessTerminationPolicy,
)
from issue_orchestrator.domain.validation_execution import (
    ContainedValidationCommand,
    ValidationCommandDeadlineExceeded,
    ValidationCommandDeadlinePending,
    ValidationCommandDeadlineTracker,
    ValidationCommandExited,
    ValidationCommandTimedOut,
    ValidationCommandTimeoutPhase,
    ValidationExecutionDeadline,
)
from issue_orchestrator.execution.contained_validation_command import (
    PosixContainedValidationCommandRunner,
)
from issue_orchestrator.execution.process_group_supervisor import (
    PosixProcessGroupSupervisor,
)
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)
from issue_orchestrator.infra.validation_executor_handshake import (
    VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT,
)
from tests.process_tree_fixture import (
    CooperativeTermResistantProcessTreeProgram,
    ProcessTreeMember,
)


def _runner() -> PosixContainedValidationCommandRunner:
    return PosixContainedValidationCommandRunner(
        PosixProcessGroupSupervisor(
            PosixProcessGroupTerminator(
                ExecutorProcessTerminationPolicy(
                    graceful_shutdown_seconds=0.05,
                    forceful_shutdown_seconds=1.0,
                )
            )
        ),
        ContainedCommandOutputPolicy(
            poll_interval_seconds=0.01,
            shutdown_timeout_seconds=1.0,
            final_drain_byte_limit=1_048_576,
        ),
    )


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.timeout(10)
def test_timeout_contains_term_resistant_descendant_before_return(
    tmp_path: Path,
) -> None:
    child_pid_path = (tmp_path / "validation-child.pid").resolve()
    program = CooperativeTermResistantProcessTreeProgram(
        child_pid_path,
        300,
        ("validation-ready",),
    ).python_source()
    command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(program)}"

    result = _runner().run(
        ContainedValidationCommand(
            command=command,
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            deadline=ValidationExecutionDeadline.for_active_timeout(1),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert type(result.cleanup) is ValidationCommandTimedOut
    assert result.cleanup.phase is ValidationCommandTimeoutPhase.ACTIVE
    assert result.timed_out is True
    assert result.exit_code == -1
    assert "validation-ready" in result.output.stdout
    descendant = ProcessTreeMember(int(child_pid_path.read_text(encoding="utf-8")))
    descendant.assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX descriptor inheritance")
@pytest.mark.timeout(10)
def test_nested_executor_handshake_yields_to_outer_deadline(tmp_path: Path) -> None:
    descriptor_variable = VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.descriptor_variable
    program = (
        "import os,time; "
        "from issue_orchestrator.infra.validation_executor_handshake import "
        "VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT as handshake; "
        f"assert {descriptor_variable!r} in os.environ; "
        "handshake.acknowledge_if_requested(os.environ); "
        "time.sleep(1.25); "
        "print('nested-executor-completed',flush=True)"
    )
    command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(program)}"

    result = _runner().run(
        ContainedValidationCommand(
            command=command,
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            deadline=ValidationExecutionDeadline(
                ExecutorBoundedDeadline(1.0, 2.0),
                outer_timeout_seconds=3,
            ),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert result.child.exit_code == 0
    assert result.timed_out is False
    assert "nested-executor-completed" in result.output.stdout


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX descriptor inheritance")
@pytest.mark.timeout(10)
def test_nested_executor_outer_deadline_starts_at_handshake(tmp_path: Path) -> None:
    descriptor_variable = VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.descriptor_variable
    program = (
        "import os,time; "
        "from issue_orchestrator.infra.validation_executor_handshake import "
        "VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT as handshake; "
        "time.sleep(1.5); "
        f"assert {descriptor_variable!r} in os.environ; "
        "handshake.acknowledge_if_requested(os.environ); "
        "time.sleep(1.75); "
        "print('delayed-nested-executor-completed',flush=True)"
    )
    command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(program)}"

    result = _runner().run(
        ContainedValidationCommand(
            command=command,
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            deadline=ValidationExecutionDeadline(
                ExecutorBoundedDeadline(2.0, 2.1),
                outer_timeout_seconds=3,
            ),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert result.child.exit_code == 0
    assert result.timed_out is False
    assert "delayed-nested-executor-completed" in result.output.stdout


@pytest.mark.parametrize(
    ("acknowledged_at", "expected_phase"),
    (
        (101.999, None),
        (102.0, ValidationCommandTimeoutPhase.ACTIVE),
        (102.001, ValidationCommandTimeoutPhase.ACTIVE),
    ),
)
def test_executor_acknowledgement_respects_exact_active_deadline_boundary(
    acknowledged_at: float,
    expected_phase: ValidationCommandTimeoutPhase | None,
) -> None:
    deadline = ValidationExecutionDeadline(
        ExecutorBoundedDeadline(2.0, 4.0),
        outer_timeout_seconds=5,
    )
    tracker = ValidationCommandDeadlineTracker(deadline, 100.0)

    tracker.acknowledge_executor(acknowledged_at)

    status = tracker.status(102.001)
    if expected_phase is None:
        assert type(status) is ValidationCommandDeadlinePending
        return
    assert type(status) is ValidationCommandDeadlineExceeded
    assert status.phase is expected_phase


def test_timely_acknowledgement_anchors_complete_outer_deadline() -> None:
    deadline = ValidationExecutionDeadline(
        ExecutorBoundedDeadline(2.0, 4.0),
        outer_timeout_seconds=5,
    )
    tracker = ValidationCommandDeadlineTracker(deadline, 100.0)
    tracker.acknowledge_executor(101.5)

    assert type(tracker.status(106.49)) is ValidationCommandDeadlinePending
    status = tracker.status(106.5)
    assert type(status) is ValidationCommandDeadlineExceeded
    assert status.phase is ValidationCommandTimeoutPhase.OUTER


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX descriptor inheritance")
@pytest.mark.timeout(10)
def test_nested_executor_remains_bounded_by_outer_deadline(tmp_path: Path) -> None:
    descriptor_variable = VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.descriptor_variable
    program = (
        "import os,time; "
        "from issue_orchestrator.infra.validation_executor_handshake import "
        "VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT as handshake; "
        f"assert {descriptor_variable!r} in os.environ; "
        "handshake.acknowledge_if_requested(os.environ); "
        "time.sleep(10)"
    )
    command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(program)}"

    result = _runner().run(
        ContainedValidationCommand(
            command=command,
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            deadline=ValidationExecutionDeadline(
                ExecutorBoundedDeadline(0.1, 0.2),
                outer_timeout_seconds=1,
            ),
        )
    )

    assert type(result.cleanup) is ValidationCommandTimedOut
    assert result.cleanup.phase is ValidationCommandTimeoutPhase.OUTER
    assert result.timed_out is True
