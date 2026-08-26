"""Public-boundary tests for retained POSIX process activation."""

from __future__ import annotations

import os
import json
from pathlib import Path
import sys

import pytest

from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessActivationPolicy,
    PosixProcessEnvironment,
    PosixProcessGroupMode,
    PosixProcessJoinGroup,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
)
from issue_orchestrator.entrypoints.bootstrap import build_process_group_supervisor
from issue_orchestrator.execution.posix_process import (
    MaskedPosixSpawnPrimitive,
    RetainedPosixProcessLauncher,
    run_posix_process_child,
)
from issue_orchestrator.ports.posix_process import (
    PosixProcessLaunchRecovered,
    PosixProcessLaunchRejected,
    PosixProcessLaunchStarted,
)
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
) -> RetainedPosixProcessLauncher:
    return RetainedPosixProcessLauncher(
        _CHILD_PROGRAM,
        primitive,
        build_process_group_supervisor(),
        PosixProcessActivationPolicy(2.0),
        _TERMINATION_POLICY,
    )


def _specification(
    program: PosixProcessProgram,
    working_directory: Path,
    mappings: tuple[PosixDescriptorMapping, ...] = (),
    group: PosixProcessGroupMode | PosixProcessJoinGroup = (
        PosixProcessGroupMode.NEW_SESSION
    ),
) -> PosixProcessLaunchSpec:
    return PosixProcessLaunchSpec(
        program=program,
        working_directory=working_directory,
        environment=PosixProcessEnvironment.from_mapping(os.environ),
        group_mode=group,
        descriptor_mappings=mappings,
        terminal=PosixProcessWithoutTerminal(),
    )


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
            "schema_version": 3,
            "arguments": arguments,
            "working_directory": str(tmp_path.resolve()),
            "inherited_file_descriptors": inherited_descriptors,
            "activation_gate_file_descriptor": 10,
            "exec_status_file_descriptor": 11,
            "terminal": {"kind": "without-terminal"},
        }
    )

    with pytest.raises(ValueError, match="invalid retained POSIX child invocation"):
        run_posix_process_child(request)
