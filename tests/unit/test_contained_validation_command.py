"""Public-boundary tests for contained validation execution."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from shlex import join as shell_join
from shlex import quote as shell_quote
from typing import cast

import pytest

from issue_orchestrator.domain.contained_command import ContainedCommandOutputPolicy
from issue_orchestrator.domain.executor import (
    ExecutorBoundedDeadline,
    ExecutorProcessTerminationPolicy,
)
from issue_orchestrator.domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupSupervision,
    ProcessGroupTerminalCompletionAccepted,
    ProcessGroupTerminalDecision,
    ProcessGroupTerminalInterruptionRequested,
    ProcessGroupTermination,
    ProcessGroupWait,
)
from issue_orchestrator.domain.process_group_sentinel import (
    ProcessGroupSentinelPolicy,
    ProcessGroupSentinelProgram,
)
from issue_orchestrator.domain.validation_execution import (
    ContainedValidationCommand,
    ValidationCommandDeadlineExceeded,
    ValidationCommandDeadlinePending,
    ValidationCommandDeadlineTracker,
    ValidationCommandCleanupFailed,
    ValidationCommandExited,
    ValidationCommandOutputCapture,
    ValidationCommandNotStarted,
    ValidationCommandTimedOut,
    ValidationCommandTimeoutPhase,
    ValidationDeadlineObservationClock,
    ValidationExecutionDeadline,
    ValidationGuardianClock,
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
    PosixProcessConfiguredActivationDeadline,
    PosixProcessEnvironment,
    PosixProcessGroupMode,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessInheritedStandardStreams,
    PosixProcessWithoutTerminal,
)
from issue_orchestrator.execution.posix_pipe import OsPosixPipeFactory
from issue_orchestrator.execution.posix_process import (
    MaskedPosixSpawnPrimitive,
    RetainedPosixProcessLauncher,
    SystemPosixProcessActivationClock,
)
from issue_orchestrator.execution.validation_launch_pipes import (
    PosixValidationLaunchPipesFactory,
)
from issue_orchestrator.execution.validation_pipe_resources import (
    default_validation_pipe_selector,
)
from issue_orchestrator.execution.validation_output_journal import (
    PosixValidationOutputJournalFactory,
)
from issue_orchestrator.execution.validation_process_guardian import (
    SentinelValidationProcessGuardian,
    ValidationGuardianStartupDeadlineOwner,
    ValidationGuardianStartupPhase,
    ValidationProcessGuardianProgram,
)
from issue_orchestrator.ports.process_group_supervisor import (
    ProcessGroupInterruption,
    ProcessGroupSupervisor,
)
from issue_orchestrator.ports.posix_pipe import PosixPipeReader
from issue_orchestrator.ports.posix_process import PosixProcessLaunchStarted
from issue_orchestrator.ports.posix_process import PosixProcessLauncher
from issue_orchestrator.ports.validation_pipe_capture import (
    ValidationPipeCapture,
    ValidationPipeCaptureFactory,
    ValidationPipeCaptureResult,
)
from issue_orchestrator.ports.validation_output_journal import (
    ValidationOutputJournal,
    ValidationOutputJournalFactory,
    ValidationOutputJournalResult,
    ValidationOutputStream,
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
from issue_orchestrator.infra.executor_deadline_environment import (
    EXECUTOR_DEADLINE_ENVIRONMENT,
)
from issue_orchestrator.execution.host_executor.host_policy import (
    EXECUTOR_POOL_DIR_ENV,
)
from issue_orchestrator.entrypoints.bootstrap import build_posix_process_launcher
from tests.process_tree_fixture import (
    CooperativeTermResistantProcessTreeProgram,
    ParentCrashProcessTreeProgram,
    ProcessTreeMember,
)
from tests.process_completion_fixture import (
    NoDescendantProcessContainment,
    PROCESS_COMPLETION_WATCHDOG,
    TextProcessInvocation,
    build_test_process_group_observer,
)
from tests.unit.threading_helpers import run_in_thread, wait_for_event
from tests.posix_process_fixture import ReapEvidenceFailingProcessLauncher


def test_not_started_terminal_requires_exact_exception_evidence() -> None:
    with pytest.raises(ValueError, match="must be a BaseException"):
        ValidationCommandNotStarted(cast(BaseException, None))


def _output_capture(
    root: Path, retained_tail_bytes: int = 4096
) -> ValidationCommandOutputCapture:
    return ValidationCommandOutputCapture(
        (root / "validation-stdout.log").resolve(),
        (root / "validation-stderr.log").resolve(),
        retained_tail_bytes,
    )


def _runner() -> PosixContainedValidationCommandRunner:
    return _runner_with(
        _production_supervisor(),
        PosixValidationPipeCaptureFactory(
            default_validation_pipe_selector,
            ValidationDeadlineObservationClock(time.monotonic),
        ),
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
    process_launcher = _validation_process_launcher(supervisor)
    return PosixContainedValidationCommandRunner(
        _validation_process_guardian(process_launcher, supervisor),
        supervisor,
        ContainedCommandOutputPolicy(
            poll_interval_seconds=0.01,
            shutdown_timeout_seconds=1.0,
            final_drain_byte_limit=1_048_576,
        ),
        capture_factory,
        launch_pipes_factory,
        PosixValidationOutputJournalFactory(),
    )


def _validation_process_launcher(
    supervisor: ProcessGroupSupervisor,
) -> RetainedPosixProcessLauncher:
    return RetainedPosixProcessLauncher(
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
        SystemPosixProcessActivationClock(),
    )


def _runner_with_process_launcher(
    process_launcher: PosixProcessLauncher,
    supervisor: ProcessGroupSupervisor,
) -> PosixContainedValidationCommandRunner:
    return PosixContainedValidationCommandRunner(
        _validation_process_guardian(process_launcher, supervisor),
        supervisor,
        ContainedCommandOutputPolicy(0.01, 1.0, 1_048_576),
        PosixValidationPipeCaptureFactory(
            default_validation_pipe_selector,
            ValidationDeadlineObservationClock(time.monotonic),
        ),
        PosixValidationLaunchPipesFactory(OsPosixPipeFactory()),
        PosixValidationOutputJournalFactory(),
    )


def _runner_with_output_journal(
    output_journal_factory: ValidationOutputJournalFactory,
) -> PosixContainedValidationCommandRunner:
    supervisor = _production_supervisor()
    process_launcher = _validation_process_launcher(supervisor)
    return PosixContainedValidationCommandRunner(
        _validation_process_guardian(process_launcher, supervisor),
        supervisor,
        ContainedCommandOutputPolicy(0.01, 1.0, 1_048_576),
        PosixValidationPipeCaptureFactory(
            default_validation_pipe_selector,
            ValidationDeadlineObservationClock(time.monotonic),
        ),
        PosixValidationLaunchPipesFactory(OsPosixPipeFactory()),
        output_journal_factory,
    )


def _validation_process_guardian(
    process_launcher: PosixProcessLauncher,
    supervisor: ProcessGroupSupervisor,
) -> SentinelValidationProcessGuardian:
    return SentinelValidationProcessGuardian(
        ValidationProcessGuardianProgram(
            (
                str(Path(sys.executable)),
                "-m",
                "issue_orchestrator.execution.validation_process_guardian",
            )
        ),
        ProcessGroupSentinelProgram(
            (
                str(Path(sys.executable)),
                "-m",
                "issue_orchestrator.execution.process_group_sentinel",
            )
        ),
        ProcessGroupSentinelPolicy(0.05, 2.0),
        process_launcher,
        supervisor,
        OsPosixPipeFactory(),
        ValidationGuardianClock(time.monotonic),
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


@dataclass(slots=True)
class _SequenceValidationGuardianClock:
    observations: tuple[float, ...]
    _index: int = field(default=0, init=False)

    def monotonic_now(self) -> float:
        if self._index >= len(self.observations):
            raise AssertionError("validation guardian clock was observed too often")
        observation = self.observations[self._index]
        self._index += 1
        return observation


@pytest.mark.parametrize(
    "phase",
    (
        ValidationGuardianStartupPhase.READINESS,
        ValidationGuardianStartupPhase.EXEC_STATUS,
    ),
)
@pytest.mark.parametrize(
    ("observations", "select_succeeds", "read_succeeds"),
    (
        ((99.998, 99.999), True, True),
        ((99.999, 100.0), True, False),
        ((100.001,), False, False),
    ),
)
def test_guardian_startup_deadline_owns_select_to_read_boundary(
    phase: ValidationGuardianStartupPhase,
    observations: tuple[float, ...],
    select_succeeds: bool,
    read_succeeds: bool,
) -> None:
    clock = _SequenceValidationGuardianClock(observations)
    owner = ValidationGuardianStartupDeadlineOwner(
        ValidationGuardianClock(clock.monotonic_now),
        100.0,
    )

    if not select_succeeds:
        with pytest.raises(TimeoutError, match=phase.value):
            owner.remaining_before_select(phase)
        return

    assert owner.remaining_before_select(phase) > 0
    if read_succeeds:
        owner.accept_after_read(phase)
    else:
        with pytest.raises(TimeoutError, match=phase.value):
            owner.accept_after_read(phase)


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
        output_journal: ValidationOutputJournal,
    ) -> None:
        self._streams = streams
        self._failure = failure
        self._output_journal = output_journal

    def wait_for_request(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return False

    def decide_terminal_observation(self) -> ProcessGroupTerminalDecision:
        return ProcessGroupTerminalCompletionAccepted()

    @property
    def deadline_status(self) -> ValidationCommandDeadlinePending:
        return ValidationCommandDeadlinePending()

    def finalize(self) -> ValidationPipeCaptureResult:
        for stream in self._streams:
            stream.close()
        journal_result = self._output_journal.finalize()
        failure = self._failure
        if journal_result.failure is not None:
            failure = BaseExceptionGroup(
                "injected capture and journal finalization both failed",
                (failure, journal_result.failure),
            )
        return ValidationPipeCaptureResult(journal_result.output, failure)


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
        output_journal: ValidationOutputJournal,
    ) -> ValidationPipeCapture:
        del policy, deadline, started_at_monotonic
        return _FailingCapture(
            (stdout, stderr, handshake_reader),
            self._failure,
            output_journal,
        )


@dataclass(slots=True)
class _SetupFailingCaptureFactory:
    transferred_descriptors: list[int]

    def create(
        self,
        stdout: PosixPipeReader,
        stderr: PosixPipeReader,
        handshake_reader: PosixPipeReader,
        policy: ContainedCommandOutputPolicy,
        deadline: ValidationExecutionDeadline,
        started_at_monotonic: float,
        output_journal: ValidationOutputJournal,
    ) -> ValidationPipeCapture:
        del policy, deadline, started_at_monotonic, output_journal
        for stream in (stdout, stderr, handshake_reader):
            self.transferred_descriptors.append(stream.fileno())
        raise RuntimeError("injected validation capture setup failure")


@dataclass(frozen=True, slots=True)
class _CloseFailingPipeReader(PosixPipeReader):
    delegate: PosixPipeReader
    failure: RuntimeError

    def fileno(self) -> int:
        return self.delegate.fileno()

    def close(self) -> None:
        self.delegate.close()
        raise self.failure


@dataclass(frozen=True, slots=True)
class _ReaderCloseFailingValidationLaunchPipes(ValidationLaunchPipes):
    delegate: ValidationLaunchPipes
    failures: tuple[RuntimeError, RuntimeError, RuntimeError]
    transferred_descriptors: list[int]

    @property
    def descriptor_mappings(self) -> tuple[PosixDescriptorMapping, ...]:
        return self.delegate.descriptor_mappings

    def child_environment(
        self,
        base_environment: Mapping[str, str],
    ) -> PosixProcessEnvironment:
        return self.delegate.child_environment(base_environment)

    def transfer_readers_after_launch(self) -> ValidationLaunchReaders:
        readers = self.delegate.transfer_readers_after_launch()
        raw_readers = (
            readers.stdout,
            readers.stderr,
            readers.executor_handshake,
        )
        self.transferred_descriptors.extend(reader.fileno() for reader in raw_readers)
        wrapped = tuple(
            _CloseFailingPipeReader(reader, failure)
            for reader, failure in zip(raw_readers, self.failures, strict=True)
        )
        return ValidationLaunchReaders(*wrapped)

    def close(self) -> ValidationLaunchPipesClose:
        return self.delegate.close()


@dataclass(frozen=True, slots=True)
class _ReaderCloseFailingValidationLaunchPipesFactory(ValidationLaunchPipesFactory):
    failures: tuple[RuntimeError, RuntimeError, RuntimeError]
    transferred_descriptors: list[int]

    def create(self) -> ValidationLaunchPipes:
        return _ReaderCloseFailingValidationLaunchPipes(
            PosixValidationLaunchPipesFactory(OsPosixPipeFactory()).create(),
            self.failures,
            self.transferred_descriptors,
        )


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


@dataclass(frozen=True, slots=True)
class _ObservingValidationOutputJournal(ValidationOutputJournal):
    delegate: ValidationOutputJournal
    append_observed: threading.Event

    def append(self, stream: ValidationOutputStream, payload: bytes) -> None:
        self.delegate.append(stream, payload)
        self.append_observed.set()

    def finalize(self) -> ValidationOutputJournalResult:
        return self.delegate.finalize()


@dataclass(frozen=True, slots=True)
class _ObservingValidationOutputJournalFactory(ValidationOutputJournalFactory):
    append_observed: threading.Event

    def create(
        self,
        capture: ValidationCommandOutputCapture,
    ) -> ValidationOutputJournal:
        return _ObservingValidationOutputJournal(
            PosixValidationOutputJournalFactory().create(capture),
            self.append_observed,
        )


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX streaming output")
@pytest.mark.timeout(10)
def test_output_prefix_is_journaled_before_delayed_child_is_released(
    tmp_path: Path,
) -> None:
    child_pid_path = (tmp_path / "streaming-child.pid").resolve()
    output_capture = _output_capture(tmp_path)
    append_observed = threading.Event()
    program = (
        "import os,signal\n"
        "from pathlib import Path\n"
        "released=False\n"
        "def release(signum, frame):\n"
        "    global released\n"
        "    released=True\n"
        "signal.signal(signal.SIGUSR1, release)\n"
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()))\n"
        "os.write(1, b'prefix-before-release\\n')\n"
        "while not released:\n"
        "    signal.pause()\n"
        "os.write(1, b'suffix-after-release\\n')\n"
    )
    runner = _runner_with_output_journal(
        _ObservingValidationOutputJournalFactory(append_observed)
    )
    validation_thread, validation_result = run_in_thread(
        runner.run,
        ContainedValidationCommand(
            command=f"exec {shell_quote(sys.executable)} -c {shell_quote(program)}",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            deadline=ValidationExecutionDeadline.for_active_timeout(5),
            output_capture=output_capture,
        ),
    )
    try:
        wait_for_event(
            append_observed,
            PROCESS_COMPLETION_WATCHDOG.timeout_seconds,
            label="validation output append",
        )
        assert validation_thread.is_alive()
        assert output_capture.stdout_path.read_text(encoding="utf-8") == (
            "prefix-before-release\n"
        )
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        os.kill(child_pid, signal.SIGUSR1)
        PROCESS_COMPLETION_WATCHDOG.join_thread(
            validation_thread,
            operation="released streaming validation",
        )
    finally:
        if validation_thread.is_alive() and child_pid_path.exists():
            os.kill(int(child_pid_path.read_text(encoding="utf-8")), signal.SIGKILL)
            PROCESS_COMPLETION_WATCHDOG.join_thread(
                validation_thread,
                operation="streaming validation cleanup",
            )

    result = validation_result.unwrap()
    assert type(result.child) is ValidationCommandExited
    assert result.child.exit_code == 0
    assert result.output.stdout == "prefix-before-release\nsuffix-after-release\n"


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX streaming output")
@pytest.mark.timeout(10)
def test_noisy_output_is_complete_on_disk_and_bounded_in_memory(tmp_path: Path) -> None:
    retained_tail_bytes = 1024
    output_capture = _output_capture(tmp_path, retained_tail_bytes)
    noisy_bytes = 2_000_000
    program = (
        "import os; "
        f"os.write(1,b'a'*{noisy_bytes}); "
        "os.write(1,b'OUT-END'); "
        f"os.write(2,b'b'*{noisy_bytes}); "
        "os.write(2,b'ERR-END')"
    )

    result = _runner().run(
        ContainedValidationCommand(
            command=f"exec {shell_quote(sys.executable)} -c {shell_quote(program)}",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            deadline=ValidationExecutionDeadline.for_active_timeout(5),
            output_capture=output_capture,
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert result.child.exit_code == 0
    assert output_capture.stdout_path.stat().st_size == noisy_bytes + len(b"OUT-END")
    assert output_capture.stderr_path.stat().st_size == noisy_bytes + len(b"ERR-END")
    assert output_capture.stdout_path.read_bytes().endswith(b"OUT-END")
    assert output_capture.stderr_path.read_bytes().endswith(b"ERR-END")
    assert result.output.stdout.startswith("[VALIDATION OUTPUT TRUNCATED:")
    assert result.output.stderr.startswith("[VALIDATION OUTPUT TRUNCATED:")
    assert result.output.stdout.endswith("OUT-END")
    assert result.output.stderr.endswith("ERR-END")
    assert len(result.output.stdout.encode()) < retained_tail_bytes + 128
    assert len(result.output.stderr.encode()) < retained_tail_bytes + 128


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
    command = f"exec {shell_quote(sys.executable)} -c {shell_quote(program)}"

    result = _runner().run(
        ContainedValidationCommand(
            command=command,
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
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
    command = f"exec {shell_quote(sys.executable)} -c {shell_quote(program)}"

    result = _runner().run(
        ContainedValidationCommand(
            command=command,
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
            deadline=ValidationExecutionDeadline(
                ExecutorBoundedDeadline(1.0, 2.0),
                outer_timeout_seconds=3.0,
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
    command = f"exec {shell_quote(sys.executable)} -c {shell_quote(program)}"

    result = _runner().run(
        ContainedValidationCommand(
            command=command,
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
            deadline=ValidationExecutionDeadline(
                ExecutorBoundedDeadline(2.0, 2.1),
                outer_timeout_seconds=3.0,
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
        outer_timeout_seconds=5.0,
    )
    tracker = ValidationCommandDeadlineTracker(deadline, 100.0)

    tracker.acknowledge_executor(acknowledged_at)

    status = tracker.status(102.001)
    if expected_phase is None:
        assert type(status) is ValidationCommandDeadlinePending
        return
    assert type(status) is ValidationCommandDeadlineExceeded
    assert status.phase is expected_phase


@pytest.mark.parametrize(
    ("observed_at", "expected_decision", "expected_status"),
    (
        (
            101.999,
            ProcessGroupTerminalCompletionAccepted,
            ValidationCommandDeadlinePending,
        ),
        (
            102.0,
            ProcessGroupTerminalInterruptionRequested,
            ValidationCommandDeadlineExceeded,
        ),
        (
            102.001,
            ProcessGroupTerminalInterruptionRequested,
            ValidationCommandDeadlineExceeded,
        ),
    ),
)
def test_terminal_observation_rechecks_exact_validation_deadline_boundary(
    tmp_path: Path,
    observed_at: float,
    expected_decision: type[
        ProcessGroupTerminalCompletionAccepted
        | ProcessGroupTerminalInterruptionRequested
    ],
    expected_status: type[
        ValidationCommandDeadlinePending | ValidationCommandDeadlineExceeded
    ],
) -> None:
    launch_pipes = PosixValidationLaunchPipesFactory(OsPosixPipeFactory()).create()
    readers = launch_pipes.transfer_readers_after_launch()
    output_journal = PosixValidationOutputJournalFactory().create(
        _output_capture(tmp_path)
    )
    capture = PosixValidationPipeCaptureFactory(
        default_validation_pipe_selector,
        ValidationDeadlineObservationClock(lambda: observed_at),
    ).create(
        readers.stdout,
        readers.stderr,
        readers.executor_handshake,
        ContainedCommandOutputPolicy(0.01, 1.0, 1_048_576),
        ValidationExecutionDeadline(
            ExecutorBoundedDeadline(2.0, 4.0),
            outer_timeout_seconds=5.0,
        ),
        100.0,
        output_journal,
    )
    try:
        decision = capture.decide_terminal_observation()
        assert type(decision) is expected_decision
        assert type(capture.deadline_status) is expected_status
    finally:
        finalization = capture.finalize()
        assert finalization.failure is None


def test_timely_acknowledgement_anchors_complete_outer_deadline() -> None:
    deadline = ValidationExecutionDeadline(
        ExecutorBoundedDeadline(2.0, 4.0),
        outer_timeout_seconds=5.0,
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
    command = f"exec {shell_quote(sys.executable)} -c {shell_quote(program)}"

    result = _runner().run(
        ContainedValidationCommand(
            command=command,
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
            deadline=ValidationExecutionDeadline(
                ExecutorBoundedDeadline(1.0, 2.0),
                outer_timeout_seconds=3.0,
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
            command=f"exec {shell_quote(sys.executable)} -c pass",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
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
            command=f"exec {shell_quote(sys.executable)} -c 'import time; time.sleep(300)'",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
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
    transferred_descriptors: list[int] = []
    result = _runner_with_capture(
        _SetupFailingCaptureFactory(transferred_descriptors)
    ).run(
        ContainedValidationCommand(
            command=f"exec {shell_quote(sys.executable)} -c 'import time; time.sleep(300)'",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
            deadline=ValidationExecutionDeadline.for_active_timeout(5),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert type(result.cleanup) is ValidationCommandCleanupFailed
    assert _exception_messages(result.cleanup.error) == (
        "injected validation capture setup failure",
    )
    assert len(transferred_descriptors) == 3
    for descriptor in transferred_descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)
    ProcessTreeMember(result.child.process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.timeout(10)
def test_capture_rejection_preserves_every_reader_cleanup_failure(
    tmp_path: Path,
) -> None:
    descriptors_seen_by_capture: list[int] = []
    transferred_descriptors: list[int] = []
    reader_failures = (
        RuntimeError("injected stdout reader close failure"),
        RuntimeError("injected stderr reader close failure"),
        RuntimeError("injected handshake reader close failure"),
    )
    result = _runner_with_launch_pipes(
        _production_supervisor(),
        _SetupFailingCaptureFactory(descriptors_seen_by_capture),
        _ReaderCloseFailingValidationLaunchPipesFactory(
            reader_failures,
            transferred_descriptors,
        ),
    ).run(
        ContainedValidationCommand(
            command=f"exec {shell_quote(sys.executable)} -c 'import time; time.sleep(300)'",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
            deadline=ValidationExecutionDeadline.for_active_timeout(5),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert type(result.cleanup) is ValidationCommandCleanupFailed
    assert _exception_messages(result.cleanup.error) == (
        "injected validation capture setup failure",
        *(str(failure) for failure in reader_failures),
    )
    assert descriptors_seen_by_capture == transferred_descriptors
    assert len(transferred_descriptors) == 3
    for descriptor in transferred_descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)
    ProcessTreeMember(result.child.process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process containment")
@pytest.mark.timeout(10)
def test_post_spawn_reader_transfer_failure_contains_and_reaps_started_child(
    tmp_path: Path,
) -> None:
    transfer_failure = RuntimeError("injected validation reader transfer failure")
    result = _runner_with_launch_pipes(
        _production_supervisor(),
        PosixValidationPipeCaptureFactory(
            default_validation_pipe_selector,
            ValidationDeadlineObservationClock(time.monotonic),
        ),
        _TransferFailingValidationLaunchPipesFactory(transfer_failure),
    ).run(
        ContainedValidationCommand(
            command=f"exec {shell_quote(sys.executable)} -c 'import time; time.sleep(300)'",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
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
            command=f"exec {shell_quote(sys.executable)} -c {shell_quote(program)}",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
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


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX descriptor inheritance")
@pytest.mark.timeout(10)
def test_partial_executor_handshake_is_rejected_after_fast_child_exit(
    tmp_path: Path,
) -> None:
    descriptor_variable = VALIDATION_EXECUTOR_HANDSHAKE_ENVIRONMENT.descriptor_variable
    program = (
        "import os; "
        f"fd=int(os.environ.pop({descriptor_variable!r})); "
        "os.write(fd,b'\\x01'); "
        "os.close(fd)"
    )

    result = _runner().run(
        ContainedValidationCommand(
            command=f"exec {shell_quote(sys.executable)} -c {shell_quote(program)}",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
            deadline=ValidationExecutionDeadline.for_active_timeout(5),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert type(result.cleanup) is ValidationCommandCleanupFailed
    assert any(
        "handshake payload is malformed" in message
        for message in _exception_messages(result.cleanup.error)
    )
    ProcessTreeMember(result.child.process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process ownership")
@pytest.mark.timeout(10)
def test_reap_evidence_failure_does_not_skip_validation_output_finalization(
    tmp_path: Path,
) -> None:
    evidence_failure = RuntimeError("injected validation reap-evidence failure")
    supervisor = _production_supervisor()
    runner = _runner_with_process_launcher(
        ReapEvidenceFailingProcessLauncher(
            _validation_process_launcher(supervisor),
            evidence_failure,
        ),
        supervisor,
    )

    result = runner.run(
        ContainedValidationCommand(
            command="printf 'retained-validation-output\\n'",
            working_directory=tmp_path.resolve(),
            environment=os.environ,
            output_capture=_output_capture(tmp_path),
            deadline=ValidationExecutionDeadline.for_active_timeout(5),
        )
    )

    assert type(result.child) is ValidationCommandExited
    assert type(result.cleanup) is ValidationCommandCleanupFailed
    assert result.cleanup.error is evidence_failure
    assert result.output.stdout == "retained-validation-output\n"
    ProcessTreeMember(result.child.process_id).assert_contained()


@pytest.mark.skipif(os.name != "posix", reason="asserts POSIX crash containment")
@pytest.mark.timeout(45)
def test_outer_crash_contains_nested_executor_and_releases_exclusive_slot(
    tmp_path: Path,
) -> None:
    """A hard-killed validation parent cannot strand detached executor work."""
    repo_root = Path(__file__).resolve().parents[2]
    pool_dir = (tmp_path / "executor-pool").resolve()
    leader_pid_path = (tmp_path / "nested-leader.pid").resolve()
    descendant_pid_path = (tmp_path / "nested-descendant.pid").resolve()
    process_source = ParentCrashProcessTreeProgram(
        leader_pid_path,
        descendant_pid_path,
        300,
    ).python_source()
    base_environment = dict(os.environ)
    base_environment[EXECUTOR_POOL_DIR_ENV] = str(pool_dir)
    bounded_environment = EXECUTOR_DEADLINE_ENVIRONMENT.encode(
        base_environment,
        ExecutorBoundedDeadline(10.0, 20.0),
    )
    executor_command = shell_join(
        (
            sys.executable,
            "-m",
            "issue_orchestrator.entrypoints.cli",
            "executor-run",
            "--work-key",
            "pressure:validation-crash",
            "--group",
            "pressure-validation-crash",
            "--min-concurrency",
            "1",
            "--max-concurrency",
            "1",
            "--exclusive",
            "validation-crash-proof",
            "--",
            sys.executable,
            "-c",
            process_source,
        )
    )
    outer_source = (
        "import os\n"
        "from pathlib import Path\n"
        "from issue_orchestrator.domain.validation_execution import "
        "ContainedValidationCommand, ValidationCommandOutputCapture, "
        "ValidationExecutionDeadline\n"
        "from issue_orchestrator.entrypoints.bootstrap import "
        "build_validation_command_runner\n"
        "result = build_validation_command_runner().run(\n"
        "    ContainedValidationCommand(\n"
        f"        command={executor_command!r},\n"
        f"        working_directory=Path({str(repo_root)!r}),\n"
        "        environment=os.environ,\n"
        "        output_capture=ValidationCommandOutputCapture(\n"
        f"            Path({str((tmp_path / 'outer-stdout.log').resolve())!r}),\n"
        f"            Path({str((tmp_path / 'outer-stderr.log').resolve())!r}),\n"
        "            4096,\n"
        "        ),\n"
        "        deadline=ValidationExecutionDeadline.for_active_timeout(30),\n"
        "    )\n"
        ")\n"
        "raise SystemExit(result.exit_code)\n"
    )
    launch = build_posix_process_launcher().launch(
        PosixProcessLaunchSpec(
            PosixProcessProgram((sys.executable, "-c", outer_source)),
            repo_root,
            PosixProcessEnvironment.from_mapping(bounded_environment),
            PosixProcessGroupMode.NEW_SESSION,
            (),
            PosixProcessWithoutTerminal(),
            PosixProcessInheritedStandardStreams(),
            PosixProcessConfiguredActivationDeadline(),
        )
    )
    assert type(launch) is PosixProcessLaunchStarted
    PROCESS_COMPLETION_WATCHDOG.wait_for_path(
        leader_pid_path,
        operation="nested executor leader readiness",
    )
    PROCESS_COMPLETION_WATCHDOG.wait_for_path(
        descendant_pid_path,
        operation="nested executor descendant readiness",
    )
    nested_leader = ProcessTreeMember(int(leader_pid_path.read_text(encoding="utf-8")))
    nested_descendant = ProcessTreeMember(
        int(descendant_pid_path.read_text(encoding="utf-8"))
    )

    launch.process.kill()
    assert (
        PROCESS_COMPLETION_WATCHDOG.wait_posix_process(
            launch.process,
            operation="hard-killed outer validation parent",
        )
        == -signal.SIGKILL
    )
    nested_leader.assert_contained()
    nested_descendant.assert_contained()

    contender_environment = EXECUTOR_DEADLINE_ENVIRONMENT.encode(
        base_environment,
        ExecutorBoundedDeadline(2.0, 4.0),
    )
    contender = PROCESS_COMPLETION_WATCHDOG.run_text(
        TextProcessInvocation(
            operation="post-crash exclusive contender",
            arguments=(
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli",
                "executor-run",
                "--work-key",
                "pressure:post-crash",
                "--group",
                "pressure-post-crash",
                "--min-concurrency",
                "1",
                "--max-concurrency",
                "1",
                "--exclusive",
                "validation-crash-proof",
                "--",
                sys.executable,
                "-c",
                "print('post-crash-admitted')",
            ),
            working_directory=repo_root,
            environment=contender_environment,
            timeout_containment=NoDescendantProcessContainment(),
        )
    )

    assert contender.returncode == 0, contender.stderr
    assert "post-crash-admitted" in contender.stdout
