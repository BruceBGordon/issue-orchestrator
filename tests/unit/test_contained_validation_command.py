"""Public-boundary tests for contained validation execution."""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from issue_orchestrator.domain.contained_command import ContainedCommandOutputPolicy
from issue_orchestrator.domain.executor import (
    ExecutorBoundedDeadline,
    ExecutorProcessTerminationPolicy,
)
from issue_orchestrator.domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupSupervision,
    ProcessGroupTermination,
    ProcessGroupWait,
)
from issue_orchestrator.domain.validation_execution import (
    ContainedValidationCommand,
    ValidationCommandDeadlineExceeded,
    ValidationCommandDeadlinePending,
    ValidationCommandDeadlineTracker,
    ValidationCommandCleanupFailed,
    ValidationCommandExited,
    ValidationCommandOutput,
    ValidationCommandTimedOut,
    ValidationCommandTimeoutPhase,
    ValidationExecutionDeadline,
)
from issue_orchestrator.execution.contained_validation_command import (
    PosixContainedValidationCommandRunner,
    PosixValidationPipeCaptureFactory,
)
from issue_orchestrator.execution.process_group_supervisor import (
    PosixProcessGroupSupervisor,
)
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)
from issue_orchestrator.domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessActivationPolicy,
    PosixProcessEnvironment,
    PosixProcessProgram,
)
from issue_orchestrator.execution.posix_pipe import OsPosixPipeFactory
from issue_orchestrator.execution.posix_process import (
    MaskedPosixSpawnPrimitive,
    RetainedPosixProcessLauncher,
)
from issue_orchestrator.execution.validation_launch_pipes import (
    PosixValidationLaunchPipesFactory,
)
from issue_orchestrator.execution.validation_pipe_resources import (
    default_validation_pipe_selector,
)
from issue_orchestrator.ports.process_group_supervisor import (
    ProcessGroupInterruption,
    ProcessGroupSupervisor,
)
from issue_orchestrator.ports.posix_pipe import PosixPipeReader
from issue_orchestrator.ports.validation_pipe_capture import (
    ValidationPipeCapture,
    ValidationPipeCaptureFactory,
    ValidationPipeCaptureResult,
)
from issue_orchestrator.ports.validation_launch_pipes import (
    ValidationLaunchPipes,
    ValidationLaunchPipesClose,
    ValidationLaunchPipesClosed,
    ValidationLaunchPipesFactory,
    ValidationLaunchReaders,
)
from issue_orchestrator.infra.validation_executor_handshake import (
    VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT,
)
from tests.process_tree_fixture import (
    CooperativeTermResistantProcessTreeProgram,
    ProcessTreeMember,
)
from tests.process_completion_fixture import build_test_process_group_observer


def _runner() -> PosixContainedValidationCommandRunner:
    return _runner_with(
        _production_supervisor(),
        PosixValidationPipeCaptureFactory(default_validation_pipe_selector),
    )


def _runner_with_capture(
    capture_factory: ValidationPipeCaptureFactory,
) -> PosixContainedValidationCommandRunner:
    return _runner_with(_production_supervisor(), capture_factory)


def _runner_with(
    supervisor: ProcessGroupSupervisor,
    capture_factory: ValidationPipeCaptureFactory,
) -> PosixContainedValidationCommandRunner:
    return _runner_with_launch_pipes(
        supervisor,
        capture_factory,
        PosixValidationLaunchPipesFactory(OsPosixPipeFactory()),
    )


def _runner_with_launch_pipes(
    supervisor: ProcessGroupSupervisor,
    capture_factory: ValidationPipeCaptureFactory,
    launch_pipes_factory: ValidationLaunchPipesFactory,
) -> PosixContainedValidationCommandRunner:
    return PosixContainedValidationCommandRunner(
        RetainedPosixProcessLauncher(
            PosixProcessProgram(
                (
                    str(Path(sys.executable)),
                    "-m",
                    "issue_orchestrator.entrypoints.posix_process_child",
                )
            ),
            MaskedPosixSpawnPrimitive(),
            supervisor,
            PosixProcessActivationPolicy(2.0),
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.05,
                forceful_shutdown_seconds=1.0,
            ),
        ),
        supervisor,
        ContainedCommandOutputPolicy(
            poll_interval_seconds=0.01,
            shutdown_timeout_seconds=1.0,
            final_drain_byte_limit=1_048_576,
        ),
        capture_factory,
        launch_pipes_factory,
    )


def _production_supervisor() -> PosixProcessGroupSupervisor:
    return PosixProcessGroupSupervisor(
        PosixProcessGroupTerminator(
            ExecutorProcessTerminationPolicy(
                graceful_shutdown_seconds=0.05,
                forceful_shutdown_seconds=1.0,
            ),
            build_test_process_group_observer(),
        )
    )


def _exception_messages(error: BaseException) -> tuple[str, ...]:
    if isinstance(error, BaseExceptionGroup):
        return tuple(
            message
            for nested in error.exceptions
            for message in _exception_messages(nested)
        )
    return (str(error),)


class _FailingCapture:
    def __init__(
        self,
        streams: tuple[PosixPipeReader, PosixPipeReader, PosixPipeReader],
        failure: BaseException,
    ) -> None:
        self._streams = streams
        self._failure = failure

    def wait_for_request(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return False

    @property
    def deadline_status(self) -> ValidationCommandDeadlinePending:
        return ValidationCommandDeadlinePending()

    def finalize(self) -> ValidationPipeCaptureResult:
        for stream in self._streams:
            stream.close()
        return ValidationPipeCaptureResult(
            ValidationCommandOutput("", ""),
            self._failure,
        )


class _FailingCaptureFactory:
    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    def create(
        self,
        stdout: PosixPipeReader,
        stderr: PosixPipeReader,
        handshake_reader: PosixPipeReader,
        policy: ContainedCommandOutputPolicy,
        deadline: ValidationExecutionDeadline,
        started_at_monotonic: float,
    ) -> ValidationPipeCapture:
        del policy, deadline, started_at_monotonic
        return _FailingCapture(
            (stdout, stderr, handshake_reader),
            self._failure,
        )


class _SetupFailingCaptureFactory:
    def create(
        self,
        stdout: PosixPipeReader,
        stderr: PosixPipeReader,
        handshake_reader: PosixPipeReader,
        policy: ContainedCommandOutputPolicy,
        deadline: ValidationExecutionDeadline,
        started_at_monotonic: float,
    ) -> ValidationPipeCapture:
        del policy, deadline, started_at_monotonic
        for stream in (stdout, stderr, handshake_reader):
            stream.close()
        raise RuntimeError("injected validation capture setup failure")


@dataclass(frozen=True, slots=True)
class _TransferFailingValidationLaunchPipes(ValidationLaunchPipes):
    delegate: ValidationLaunchPipes
    failure: RuntimeError

    @property
    def descriptor_mappings(self) -> tuple[PosixDescriptorMapping, ...]:
        return self.delegate.descriptor_mappings

    def child_environment(
        self,
        base_environment: Mapping[str, str],
    ) -> PosixProcessEnvironment:
        return self.delegate.child_environment(base_environment)

    def transfer_readers_after_launch(self) -> ValidationLaunchReaders:
        outcome = self.delegate.close()
        if type(outcome) is not ValidationLaunchPipesClosed:
            raise AssertionError("injected transfer precleanup unexpectedly failed")
        raise self.failure

    def close(self) -> ValidationLaunchPipesClose:
        return self.delegate.close()


@dataclass(frozen=True, slots=True)
class _TransferFailingValidationLaunchPipesFactory(ValidationLaunchPipesFactory):
    failure: RuntimeError

    def create(self) -> ValidationLaunchPipes:
        return _TransferFailingValidationLaunchPipes(
            PosixValidationLaunchPipesFactory(OsPosixPipeFactory()).create(),
            self.failure,
        )


class _SupervisionFailingOwner(ProcessGroupSupervisor):
    def __init__(self, delegate: ProcessGroupSupervisor) -> None:
        self._delegate = delegate

    def supervise(
        self,
        leader: OwnedProcessGroupLeader,
        wait: ProcessGroupWait,
        interruption: ProcessGroupInterruption,
    ) -> ProcessGroupSupervision:
        del leader, wait, interruption
        raise RuntimeError("injected validation supervision failure")

    def abort(self, leader: OwnedProcessGroupLeader) -> ProcessGroupTermination:
        return self._delegate.abort(leader)


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
                ExecutorBoundedDeadline(1.0, 2.0),
                outer_timeout_seconds=3,
            ),
        )
    )

    assert type(result.cleanup) is ValidationCommandTimedOut
    assert result.cleanup.phase is ValidationCommandTimeoutPhase.OUTER
    assert result.timed_out is True


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.timeout(10)
def test_natural_completion_returns_typed_capture_finalization_failure(
    tmp_path: Path,
) -> None:
    result = _runner_with_capture(
        _FailingCaptureFactory(RuntimeError("injected finalization failure"))
    ).run(
        ContainedValidationCommand(
            command=f"exec {shlex.quote(sys.executable)} -c pass",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            deadline=ValidationExecutionDeadline.for_active_timeout(5),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert result.child.exit_code == 0
    assert type(result.cleanup) is ValidationCommandCleanupFailed
    assert _exception_messages(result.cleanup.error) == (
        "injected finalization failure",
    )


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.timeout(10)
def test_supervision_recovery_aggregates_typed_capture_finalization_failure(
    tmp_path: Path,
) -> None:
    result = _runner_with(
        _SupervisionFailingOwner(_production_supervisor()),
        _FailingCaptureFactory(RuntimeError("injected finalization failure")),
    ).run(
        ContainedValidationCommand(
            command=f"exec {shlex.quote(sys.executable)} -c 'import time; time.sleep(300)'",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            deadline=ValidationExecutionDeadline.for_active_timeout(5),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert type(result.cleanup) is ValidationCommandCleanupFailed
    assert _exception_messages(result.cleanup.error) == (
        "injected validation supervision failure",
        "injected finalization failure",
    )
    ProcessTreeMember(result.child.process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.timeout(10)
def test_capture_setup_failure_contains_and_reaps_started_child(
    tmp_path: Path,
) -> None:
    result = _runner_with_capture(_SetupFailingCaptureFactory()).run(
        ContainedValidationCommand(
            command=f"exec {shlex.quote(sys.executable)} -c 'import time; time.sleep(300)'",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            deadline=ValidationExecutionDeadline.for_active_timeout(5),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert type(result.cleanup) is ValidationCommandCleanupFailed
    assert _exception_messages(result.cleanup.error) == (
        "injected validation capture setup failure",
    )
    ProcessTreeMember(result.child.process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.timeout(10)
def test_post_spawn_reader_transfer_failure_contains_and_reaps_started_child(
    tmp_path: Path,
) -> None:
    transfer_failure = RuntimeError("injected validation reader transfer failure")
    result = _runner_with_launch_pipes(
        _production_supervisor(),
        PosixValidationPipeCaptureFactory(default_validation_pipe_selector),
        _TransferFailingValidationLaunchPipesFactory(transfer_failure),
    ).run(
        ContainedValidationCommand(
            command=f"exec {shlex.quote(sys.executable)} -c 'import time; time.sleep(300)'",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            deadline=ValidationExecutionDeadline.for_active_timeout(5),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert type(result.cleanup) is ValidationCommandCleanupFailed
    assert result.cleanup.error is transfer_failure
    ProcessTreeMember(result.child.process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX descriptor inheritance")
@pytest.mark.timeout(10)
def test_non_finite_executor_acknowledgement_fails_and_contains_child(
    tmp_path: Path,
) -> None:
    descriptor_variable = VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.descriptor_variable
    program = (
        "import os,struct,time; "
        f"fd=int(os.environ.pop({descriptor_variable!r})); "
        "os.write(fd,struct.pack('!Bd',1,float('nan'))); "
        "os.close(fd); "
        "time.sleep(300)"
    )
    result = _runner().run(
        ContainedValidationCommand(
            command=f"exec {shlex.quote(sys.executable)} -c {shlex.quote(program)}",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            deadline=ValidationExecutionDeadline.for_active_timeout(5),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert type(result.cleanup) is ValidationCommandCleanupFailed
    assert any(
        "positive finite float" in message
        for message in _exception_messages(result.cleanup.error)
    )
    ProcessTreeMember(result.child.process_id).assert_contained()
