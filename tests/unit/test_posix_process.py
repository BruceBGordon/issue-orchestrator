"""Public-boundary tests for retained POSIX process activation."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
import sys

import pytest

from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessAbsoluteActivationDeadline,
    PosixProcessActivationDeadline,
    PosixProcessActivationDeadlineExceededError,
    PosixProcessActivationDeadlinePresent,
    PosixProcessActivationPolicy,
    PosixProcessConfiguredActivationDeadline,
    PosixProcessDetachedStandardStreams,
    PosixProcessEnvironment,
    PosixProcessGroupMode,
    PosixProcessInheritedStandardStreams,
    PosixProcessJoinedGroupContainmentRequiredError,
    PosixProcessJoinGroup,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
    classify_posix_process_activation_deadline,
)
from issue_orchestrator.entrypoints.bootstrap import build_process_group_supervisor
from issue_orchestrator.execution.posix_process import (
    MaskedPosixSpawnPrimitive,
    RetainedPosixProcessLauncher,
    SystemPosixProcessActivationClock,
    run_posix_process_child,
)
from issue_orchestrator.ports.posix_process import (
    PosixProcessActivationClock,
    PosixProcessLaunchRecovered,
    PosixProcessLaunchRejected,
    PosixProcessLaunchStarted,
)
from tests.process_completion_fixture import (
    NoDescendantProcessContainment,
    PROCESS_COMPLETION_WATCHDOG,
    TextProcessInvocation,
)
from tests.process_tree_fixture import ProcessTreeMember
from issue_orchestrator.ports.posix_spawn_primitive import (
    PosixSpawnPrimitive,
    PosixSpawnPrimitiveIndeterminate,
    PosixSpawnPrimitiveRejected,
    PosixSpawnPrimitiveRequest,
    PosixSpawnPrimitiveResult,
    PosixSpawnPrimitiveStarted,
)


_TERMINATION_POLICY = ExecutorProcessTerminationPolicy(
    graceful_shutdown_seconds=2.0,
    forceful_shutdown_seconds=2.0,
)
_CHILD_PROGRAM = PosixProcessProgram(
    (
        str(Path(sys.executable)),
        "-m",
        "issue_orchestrator.entrypoints.posix_process_child",
    )
)


class _PostSpawnFailurePrimitive:
    """Expose a real post-spawn parent failure through the public primitive port."""

    def __init__(self) -> None:
        self._production = MaskedPosixSpawnPrimitive()

    def start(self, request: PosixSpawnPrimitiveRequest) -> PosixSpawnPrimitiveResult:
        outcome = self._production.start(request)
        if type(outcome) is not PosixSpawnPrimitiveStarted:
            return outcome
        return PosixSpawnPrimitiveIndeterminate(
            outcome.process_id,
            RuntimeError("injected parent finalization failure"),
        )


class _ThrowingPrimitive:
    """Violate the primitive result contract before starting a process."""

    def start(self, request: PosixSpawnPrimitiveRequest) -> PosixSpawnPrimitiveResult:
        del request
        raise RuntimeError("injected primitive exception")


def _launcher(
    primitive: PosixSpawnPrimitive,
    clock: PosixProcessActivationClock = SystemPosixProcessActivationClock(),
) -> RetainedPosixProcessLauncher:
    return RetainedPosixProcessLauncher(
        _CHILD_PROGRAM,
        primitive,
        build_process_group_supervisor(),
        PosixProcessActivationPolicy(2.0),
        _TERMINATION_POLICY,
        clock,
    )


def _specification(
    program: PosixProcessProgram,
    working_directory: Path,
    mappings: tuple[PosixDescriptorMapping, ...] = (),
    group: PosixProcessGroupMode | PosixProcessJoinGroup = (
        PosixProcessGroupMode.NEW_SESSION
    ),
    activation_deadline: PosixProcessActivationDeadline = (
        PosixProcessConfiguredActivationDeadline()
    ),
) -> PosixProcessLaunchSpec:
    return PosixProcessLaunchSpec(
        program=program,
        working_directory=working_directory,
        environment=PosixProcessEnvironment.from_mapping(os.environ),
        group_mode=group,
        descriptor_mappings=mappings,
        terminal=PosixProcessWithoutTerminal(),
        standard_streams=PosixProcessInheritedStandardStreams(),
        activation_deadline=activation_deadline,
    )


@dataclass(slots=True)
class _SequenceActivationClock:
    observations: tuple[float, ...]
    _index: int = field(default=0, init=False)

    def monotonic(self) -> float:
        if self._index >= len(self.observations):
            raise AssertionError("activation clock was observed too many times")
        observed = self.observations[self._index]
        self._index += 1
        return observed


@pytest.mark.parametrize(
    "terminal_observation,expected_started",
    (
        (99.999, True),
        (100.0, False),
        (100.001, False),
    ),
)
def test_absolute_activation_deadline_owns_exact_exec_readiness_boundary(
    tmp_path: Path,
    terminal_observation: float,
    expected_started: bool,
) -> None:
    clock = _SequenceActivationClock((90.0, 90.0, 90.0, terminal_observation))

    outcome = _launcher(MaskedPosixSpawnPrimitive(), clock).launch(
        _specification(
            PosixProcessProgram(("/bin/sleep", "0.01")),
            tmp_path,
            activation_deadline=PosixProcessAbsoluteActivationDeadline(100.0),
        )
    )

    if expected_started:
        assert type(outcome) is PosixProcessLaunchStarted
        assert outcome.process.wait(1.0) == 0
        return
    assert type(outcome) is PosixProcessLaunchRecovered
    assert (
        type(classify_posix_process_activation_deadline(outcome.activation_error))
        is PosixProcessActivationDeadlinePresent
    )


def test_joined_post_exec_deadline_requires_and_receives_group_containment(
    tmp_path: Path,
) -> None:
    descendant_pid_path = (tmp_path / "descendant.pid").resolve()
    outcome_path = (tmp_path / "outcome.txt").resolve()
    source = (
        """
import os
from pathlib import Path
import signal
import sys
import time

from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.domain.posix_process import (
    PosixProcessAbsoluteActivationDeadline,
    PosixProcessActivationPolicy,
    PosixProcessEnvironment,
    PosixProcessInheritedStandardStreams,
    PosixProcessJoinGroup,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
)
from issue_orchestrator.entrypoints.bootstrap import build_process_group_supervisor
from issue_orchestrator.execution.posix_process import (
    MaskedPosixSpawnPrimitive,
    RetainedPosixProcessLauncher,
)
from issue_orchestrator.ports.posix_process import PosixProcessLaunchRecoveryFailed


class ForkObservedDeadlineClock:
    def __init__(self, descendant_pid_path: Path) -> None:
        self._descendant_pid_path = descendant_pid_path
        self._observations = 0

    def monotonic(self) -> float:
        self._observations += 1
        if self._observations < 4:
            return 90.0
        deadline = time.monotonic() + 5.0
        while not self._descendant_pid_path.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("opaque descendant did not fork")
            time.sleep(0.001)
        return 100.0


descendant_pid_path = Path(sys.argv[1])
outcome_path = Path(sys.argv[2])
opaque_source = """
        + repr(
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            "import time\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
            "while True:\n"
            "    time.sleep(60)\n"
        )
        + """
launcher = RetainedPosixProcessLauncher(
    PosixProcessProgram(
        (
            sys.executable,
            "-m",
            "issue_orchestrator.entrypoints.posix_process_child",
        )
    ),
    MaskedPosixSpawnPrimitive(),
    build_process_group_supervisor(),
    PosixProcessActivationPolicy(2.0),
    ExecutorProcessTerminationPolicy(0.1, 1.0),
    ForkObservedDeadlineClock(descendant_pid_path),
)
outcome = launcher.launch(
    PosixProcessLaunchSpec(
        program=PosixProcessProgram(
            (sys.executable, "-c", opaque_source, str(descendant_pid_path))
        ),
        working_directory=Path.cwd().resolve(),
        environment=PosixProcessEnvironment.from_mapping(os.environ),
        group_mode=PosixProcessJoinGroup(os.getpgrp()),
        descriptor_mappings=(),
        terminal=PosixProcessWithoutTerminal(),
        standard_streams=PosixProcessInheritedStandardStreams(),
        activation_deadline=PosixProcessAbsoluteActivationDeadline(100.0),
    )
)
if type(outcome) is not PosixProcessLaunchRecoveryFailed:
    raise AssertionError(f"unexpected launch outcome: {outcome!r}")
outcome_path.write_text(type(outcome.recovery_error).__name__, encoding="utf-8")
os.killpg(os.getpgrp(), signal.SIGKILL)
"""
    )

    result = PROCESS_COMPLETION_WATCHDOG.run_text(
        TextProcessInvocation(
            operation="joined post-exec deadline containment",
            arguments=(
                sys.executable,
                "-c",
                source,
                str(descendant_pid_path),
                str(outcome_path),
            ),
            working_directory=Path.cwd().resolve(),
            environment=os.environ,
            timeout_containment=NoDescendantProcessContainment(),
        )
    )

    assert result.returncode == -9
    assert outcome_path.read_text(encoding="utf-8") == (
        PosixProcessJoinedGroupContainmentRequiredError.__name__
    )
    ProcessTreeMember(
        int(descendant_pid_path.read_text(encoding="utf-8"))
    ).assert_contained()


def test_activation_deadline_classification_preserves_recovery_failure() -> None:
    deadline_error = PosixProcessActivationDeadlineExceededError("expired")
    cleanup_error = OSError("injected recovery evidence failure")

    evidence = classify_posix_process_activation_deadline(
        BaseExceptionGroup(
            "activation and recovery",
            (deadline_error, cleanup_error),
        )
    )

    assert type(evidence) is PosixProcessActivationDeadlinePresent
    assert evidence.recovery_failures == (cleanup_error,)


def test_launcher_retains_exact_pid_session_working_directory_and_output(
    tmp_path: Path,
) -> None:
    read_descriptor, write_descriptor = os.pipe()
    release_read_descriptor, release_write_descriptor = os.pipe()
    try:
        outcome = _launcher(MaskedPosixSpawnPrimitive()).launch(
            _specification(
                PosixProcessProgram(
                    (
                        "/bin/sh",
                        "-c",
                        'printf "%s:%s\\n" "$$" "$(pwd)"; read _ <&3',
                    )
                ),
                tmp_path,
                (
                    PosixDescriptorMapping(write_descriptor, 1),
                    PosixDescriptorMapping(release_read_descriptor, 3),
                ),
            )
        )
        assert type(outcome) is PosixProcessLaunchStarted
        os.close(write_descriptor)
        write_descriptor = -1
        output = os.read(read_descriptor, 16_384).decode().strip()
        assert output == f"{outcome.process.process_id}:{tmp_path}"
        assert os.getsid(outcome.process.process_id) == outcome.process.process_id
        os.write(release_write_descriptor, b"release\n")
        assert outcome.process.wait(2.0) == 0
    finally:
        os.close(read_descriptor)
        os.close(release_read_descriptor)
        os.close(release_write_descriptor)
        if write_descriptor >= 0:
            os.close(write_descriptor)


def test_terminal_free_child_detaches_unmapped_standard_descriptors(
    tmp_path: Path,
) -> None:
    """A WithoutTerminal child must not retain the parent's stdio: one
    leaked descendant holding a PTY slave would strand the master's
    EOF-based completion watcher forever."""
    read_descriptor, write_descriptor = os.pipe()
    try:
        outcome = _launcher(MaskedPosixSpawnPrimitive()).launch(
            PosixProcessLaunchSpec(
                program=PosixProcessProgram(
                    (
                        "/bin/sh",
                        "-c",
                        'for fd in 0 1 2; do if [ ! "/dev/fd/$fd" -ef /dev/null ];'
                        ' then echo "fd$fd:retained" >&3; exit 1; fi; done;'
                        " echo detached >&3",
                    )
                ),
                working_directory=tmp_path,
                environment=PosixProcessEnvironment.from_mapping(os.environ),
                group_mode=PosixProcessGroupMode.NEW_SESSION,
                descriptor_mappings=(PosixDescriptorMapping(write_descriptor, 3),),
                terminal=PosixProcessWithoutTerminal(),
                standard_streams=PosixProcessDetachedStandardStreams(),
                activation_deadline=PosixProcessConfiguredActivationDeadline(),
            )
        )
        assert type(outcome) is PosixProcessLaunchStarted
        os.close(write_descriptor)
        write_descriptor = -1
        assert os.read(read_descriptor, 16_384).decode().strip() == "detached"
        assert outcome.process.wait(2.0) == 0
    finally:
        os.close(read_descriptor)
        if write_descriptor >= 0:
            os.close(write_descriptor)


def test_launcher_contains_real_child_after_post_spawn_parent_failure(
    tmp_path: Path,
) -> None:
    outcome = _launcher(_PostSpawnFailurePrimitive()).launch(
        _specification(
            PosixProcessProgram(("/bin/sh", "-c", "exec /bin/sleep 300")),
            tmp_path,
        )
    )

    assert type(outcome) is PosixProcessLaunchRecovered
    assert outcome.exit_code != 0
    assert isinstance(outcome.activation_error, RuntimeError)
    try:
        os.kill(outcome.process_id, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("recovered post-spawn child remains executable")


def test_joined_group_child_cannot_exec_before_parent_activation_is_retained(
    tmp_path: Path,
) -> None:
    opaque_work_marker = tmp_path / "opaque-work-started"
    outcome = _launcher(_PostSpawnFailurePrimitive()).launch(
        _specification(
            PosixProcessProgram(
                (
                    str(Path(sys.executable)),
                    "-c",
                    "import pathlib, sys; pathlib.Path(sys.argv[1]).touch()",
                    str(opaque_work_marker),
                )
            ),
            tmp_path,
            group=PosixProcessJoinGroup(os.getpgrp()),
        )
    )

    assert type(outcome) is PosixProcessLaunchRecovered
    assert outcome.exit_code != 0
    assert not opaque_work_marker.exists()


def test_spawn_rejects_before_child_when_descriptor_acquisition_is_partial() -> None:
    read_descriptor, write_descriptor = os.pipe()
    before = len(os.listdir("/dev/fd"))
    try:
        request = PosixSpawnPrimitiveRequest(
            program=PosixProcessProgram(("/bin/true",)),
            environment=PosixProcessEnvironment.from_mapping(os.environ),
            group_mode=PosixProcessGroupMode.NEW_SESSION,
            descriptor_mappings=(
                PosixDescriptorMapping(write_descriptor, 1),
                PosixDescriptorMapping(1_000_000, 2),
            ),
        )
        outcome = MaskedPosixSpawnPrimitive().start(request)
        assert type(outcome) is PosixSpawnPrimitiveRejected
        assert len(os.listdir("/dev/fd")) == before
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)


def test_launcher_closes_activation_gate_when_primitive_raises(
    tmp_path: Path,
) -> None:
    before = len(os.listdir("/dev/fd"))

    outcome = _launcher(_ThrowingPrimitive()).launch(
        _specification(PosixProcessProgram(("/bin/true",)), tmp_path)
    )

    assert type(outcome) is PosixProcessLaunchRejected
    assert isinstance(outcome.error, RuntimeError)
    assert len(os.listdir("/dev/fd")) == before


def test_exec_handshake_deadline_contains_a_wedged_wrapper(tmp_path: Path) -> None:
    launcher = RetainedPosixProcessLauncher(
        PosixProcessProgram(
            (
                str(Path(sys.executable)),
                "-c",
                "import signal; signal.pause()",
            )
        ),
        MaskedPosixSpawnPrimitive(),
        build_process_group_supervisor(),
        PosixProcessActivationPolicy(0.1),
        ExecutorProcessTerminationPolicy(0.1, 1.0),
        SystemPosixProcessActivationClock(),
    )

    outcome = launcher.launch(
        _specification(PosixProcessProgram(("/bin/true",)), tmp_path)
    )

    assert type(outcome) is PosixProcessLaunchRecovered
    assert isinstance(outcome.activation_error, TimeoutError)
    try:
        os.kill(outcome.process_id, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("wedged POSIX wrapper remains executable")


@pytest.mark.parametrize(
    "arguments,inherited_descriptors",
    (
        ((), ()),
        (("/bin/true",), (-1,)),
        (("/bin/true",), (9, 9)),
        (("/bin/true\0hidden",), ()),
    ),
)
def test_child_entrypoint_rejects_malformed_exec_contract_before_activation(
    tmp_path: Path,
    arguments: tuple[str, ...],
    inherited_descriptors: tuple[int, ...],
) -> None:
    request = json.dumps(
        {
            "schema_version": 4,
            "arguments": arguments,
            "working_directory": str(tmp_path.resolve()),
            "inherited_file_descriptors": inherited_descriptors,
            "standard_descriptor_mappings": [],
            "detach_standard_streams": True,
            "activation_gate_file_descriptor": 10,
            "exec_status_file_descriptor": 11,
            "terminal": {"kind": "without-terminal"},
        }
    )

    with pytest.raises(ValueError, match="invalid retained POSIX child invocation"):
        run_posix_process_child(request)
