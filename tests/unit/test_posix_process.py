"""Public-boundary tests for retained POSIX process activation."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessEnvironment,
    PosixProcessGroupMode,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
)
from issue_orchestrator.entrypoints.bootstrap import build_process_group_supervisor
from issue_orchestrator.execution.posix_process import (
    MaskedPosixSpawnPrimitive,
    RetainedPosixProcessLauncher,
)
from issue_orchestrator.ports.posix_process import (
    PosixProcessLaunchRecovered,
    PosixProcessLaunchStarted,
)
from issue_orchestrator.ports.posix_spawn_primitive import (
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


def _launcher(
    primitive: MaskedPosixSpawnPrimitive | _PostSpawnFailurePrimitive,
) -> RetainedPosixProcessLauncher:
    return RetainedPosixProcessLauncher(
        _CHILD_PROGRAM,
        primitive,
        build_process_group_supervisor(),
        _TERMINATION_POLICY,
    )


def _specification(
    program: PosixProcessProgram,
    working_directory: Path,
    mappings: tuple[PosixDescriptorMapping, ...] = (),
) -> PosixProcessLaunchSpec:
    return PosixProcessLaunchSpec(
        program=program,
        working_directory=working_directory,
        environment=PosixProcessEnvironment.from_mapping(os.environ),
        group_mode=PosixProcessGroupMode.NEW_SESSION,
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
