"""Public-boundary proofs for the child-side executor command guardian."""

from __future__ import annotations

import json
import os
import signal
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

import pytest

from issue_orchestrator.domain.executor import (
    ExecutorCommandCancellation,
    ExecutorCommandLifecycle,
    ExecutorDeadlineReason,
    ExecutorInteractiveSessionCancellation,
    ExecutorNoCommandCancellation,
)
from issue_orchestrator.domain.executor_guardian import (
    ExecutorGuardianBoundedBudget,
    ExecutorGuardianCommandCompleted,
    ExecutorGuardianCommandStartFailed,
    ExecutorGuardianCommandTimedOut,
    ExecutorGuardianTerminationPolicy,
    ExecutorGuardianUnboundedBudget,
)
from issue_orchestrator.domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupSupervision,
    ProcessGroupTermination,
    ProcessGroupWait,
)
from issue_orchestrator.domain.process_group_sentinel import (
    ProcessGroupSentinelPolicy,
    ProcessGroupSentinelProgram,
)
from issue_orchestrator.execution.host_executor.guardian_launcher import (
    ExecutorGuardianLaunchError,
    ExecutorGuardianProgram,
    ExecutorGuardianProtocolError,
    PosixExecutorCommandGuardian,
)
from issue_orchestrator.execution.atomic_record_store import (
    OsAtomicRecordStoreFactory,
)
from issue_orchestrator.entrypoints.bootstrap import build_posix_process_launcher
from issue_orchestrator.execution.guardian_launch_pipes import (
    PosixGuardianLaunchPipesFactory,
)
from issue_orchestrator.execution.posix_pipe import OsPosixPipeFactory
from issue_orchestrator.execution.process_group_supervisor import (
    PosixProcessGroupSupervisor,
)
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)
from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.ports.executor_command_guardian import (
    ExecutorGuardianRequest,
    ExecutorGuardianLeaseTransfer,
)
from issue_orchestrator.ports.process_group_supervisor import (
    ProcessGroupInterruption,
    ProcessGroupSupervisor,
)
from issue_orchestrator.ports.posix_process import (
    PosixProcessLaunch,
    PosixProcessLauncher,
    PosixProcessLaunchRecoveryFailed,
    PosixProcessLaunchStarted,
)
from issue_orchestrator.domain.posix_process import PosixProcessLaunchSpec
from tests.process_tree_fixture import (
    ExitingTermResistantProcessTreeProgram,
    ProcessTreeMember,
)
from tests.process_completion_fixture import build_test_process_group_observer


def _guardian(
    program: ExecutorGuardianProgram | None = None,
) -> PosixExecutorCommandGuardian:
    termination = ExecutorProcessTerminationPolicy(0.1, 1.0)
    return PosixExecutorCommandGuardian(
        program
        if program is not None
        else ExecutorGuardianProgram(
            (
                str(Path(sys.executable)),
                "-m",
                "issue_orchestrator.execution.host_executor.guardian",
            )
        ),
        _sentinel_program(),
        ProcessGroupSentinelPolicy(0.1, 1.0),
        OsAtomicRecordStoreFactory(),
        build_posix_process_launcher(),
        PosixGuardianLaunchPipesFactory(OsPosixPipeFactory()),
        PosixProcessGroupSupervisor(
            PosixProcessGroupTerminator(
                termination,
                build_test_process_group_observer(),
            )
        ),
        ExecutorGuardianTerminationPolicy(termination.graceful_shutdown_seconds),
    )


def _sentinel_program() -> ProcessGroupSentinelProgram:
    return ProcessGroupSentinelProgram(
        (
            str(Path(sys.executable)),
            "-m",
            "issue_orchestrator.execution.process_group_sentinel",
        )
    )


def _fault_guardian_protocol_prelude() -> str:
    return (
        "import json, os, sys\n"
        "raw = sys.argv[sys.argv.index('--request-json') + 1]\n"
        "request = json.loads(raw)\n"
        "os.write(request['owner_ready_file_descriptor'], b'R')\n"
        "os.close(request['owner_ready_file_descriptor'])\n"
        "os.read(request['start_file_descriptor'], 1)\n"
        "os.close(request['start_file_descriptor'])\n"
    )


@dataclass(slots=True)
class _TestGuardianLease:
    descriptor: int
    _transferred: bool = field(default=False, init=False)

    def inherited_file_descriptors(self) -> tuple[int, ...]:
        if self._transferred:
            raise RuntimeError("test lease was already transferred")
        return (self.descriptor,)

    def transfer_to_guardian(self) -> None:
        if self._transferred:
            raise RuntimeError("test lease was already transferred")
        os.close(self.descriptor)
        self._transferred = True

    def close_local(self) -> None:
        if not self._transferred:
            os.close(self.descriptor)
            self._transferred = True


@contextmanager
def _lease_descriptor() -> Generator[_TestGuardianLease, None, None]:
    read_fd, write_fd = os.pipe()
    lease = _TestGuardianLease(write_fd)
    try:
        yield lease
    finally:
        os.close(read_fd)
        lease.close_local()


def _request(
    lease: ExecutorGuardianLeaseTransfer,
    arguments: tuple[str, ...],
    *,
    budget: ExecutorGuardianUnboundedBudget | ExecutorGuardianBoundedBudget,
    lifecycle: ExecutorCommandLifecycle = ExecutorCommandLifecycle.DETACHED,
    cancellation: ExecutorCommandCancellation = ExecutorNoCommandCancellation(),
) -> ExecutorGuardianRequest:
    return ExecutorGuardianRequest(
        arguments=arguments,
        environment=os.environ.copy(),
        lease=lease,
        budget=budget,
        lifecycle=lifecycle,
        cancellation=cancellation,
    )


class _LateInterruptionAfterCompletionSupervisor:
    """Inject SIGTERM only after natural guardian completion is established."""

    def __init__(self, delegate: ProcessGroupSupervisor) -> None:
        if not isinstance(delegate, ProcessGroupSupervisor):
            raise ValueError("delegate must implement ProcessGroupSupervisor")
        self._delegate = delegate

    def supervise(
        self,
        leader: OwnedProcessGroupLeader,
        wait: ProcessGroupWait,
        interruption: ProcessGroupInterruption,
    ) -> ProcessGroupSupervision:
        supervision = self._delegate.supervise(leader, wait, interruption)
        if type(supervision) is not ProcessGroupCompleted:
            raise AssertionError("test requires natural guardian completion")
        os.kill(os.getpid(), signal.SIGTERM)
        return supervision

    def abort(self, leader: OwnedProcessGroupLeader) -> ProcessGroupTermination:
        return self._delegate.abort(leader)


class _IndeterminateGuardianLauncher:
    """Report a real started guardian as uncontained after parent finalization."""

    def __init__(self, delegate: PosixProcessLauncher) -> None:
        if not isinstance(delegate, PosixProcessLauncher):
            raise ValueError("delegate must implement PosixProcessLauncher")
        self._delegate = delegate
        self.started_process_id: int | None = None

    def launch(self, specification: PosixProcessLaunchSpec) -> PosixProcessLaunch:
        outcome = self._delegate.launch(specification)
        if type(outcome) is not PosixProcessLaunchStarted:
            return outcome
        self.started_process_id = outcome.process.process_id
        return PosixProcessLaunchRecoveryFailed(
            outcome.process.process_id,
            RuntimeError("injected guardian parent finalization failure"),
            RuntimeError("injected first guardian recovery failure"),
        )


@pytest.mark.parametrize(
    ("command", "expected_exit_code"),
    (
        ("raise SystemExit(0)", 0),
        ("raise SystemExit(7)", 7),
        ("import os, signal; os.kill(os.getpid(), signal.SIGTERM)", -signal.SIGTERM),
    ),
)
def test_guardian_preserves_exact_command_exit_status(
    command: str,
    expected_exit_code: int,
) -> None:
    with _lease_descriptor() as lease_fd:
        terminal = _guardian().run(
            _request(
                lease_fd,
                (sys.executable, "-c", command),
                budget=ExecutorGuardianUnboundedBudget(),
            )
        )

    assert type(terminal) is ExecutorGuardianCommandCompleted
    assert terminal.exit_code == expected_exit_code


def test_completed_terminal_record_wins_over_late_parent_sigterm(
    tmp_path: Path,
) -> None:
    termination = ExecutorProcessTerminationPolicy(0.1, 1.0)
    supervisor = _LateInterruptionAfterCompletionSupervisor(
        PosixProcessGroupSupervisor(
            PosixProcessGroupTerminator(
                termination,
                build_test_process_group_observer(),
            )
        )
    )
    guardian = PosixExecutorCommandGuardian(
        ExecutorGuardianProgram(
            (
                str(Path(sys.executable)),
                "-m",
                "issue_orchestrator.execution.host_executor.guardian",
            )
        ),
        _sentinel_program(),
        ProcessGroupSentinelPolicy(0.1, 1.0),
        OsAtomicRecordStoreFactory(),
        build_posix_process_launcher(),
        PosixGuardianLaunchPipesFactory(OsPosixPipeFactory()),
        supervisor,
        ExecutorGuardianTerminationPolicy(termination.graceful_shutdown_seconds),
    )

    with _lease_descriptor() as lease_fd:
        terminal = guardian.run(
            _request(
                lease_fd,
                (sys.executable, "-c", "raise SystemExit(17)"),
                budget=ExecutorGuardianUnboundedBudget(),
                lifecycle=ExecutorCommandLifecycle.INTERACTIVE_SESSION,
                cancellation=ExecutorInteractiveSessionCancellation.for_run_dir(
                    tmp_path.resolve()
                ),
            )
        )

    assert type(terminal) is ExecutorGuardianCommandCompleted
    assert terminal.exit_code == 17


def test_guardian_timeout_wins_over_cooperative_term_exit() -> None:
    command = (
        "import signal, sys; "
        "signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0)); "
        "signal.pause()"
    )
    with _lease_descriptor() as lease_fd:
        terminal = _guardian().run(
            _request(
                lease_fd,
                (sys.executable, "-c", command),
                budget=ExecutorGuardianBoundedBudget(
                    0.5,
                    ExecutorDeadlineReason.ACTIVE,
                ),
            )
        )

    assert type(terminal) is ExecutorGuardianCommandTimedOut
    assert terminal.reason is ExecutorDeadlineReason.ACTIVE


def test_opaque_command_cannot_observe_guardian_lease_descriptors(
    tmp_path: Path,
) -> None:
    observed_path = tmp_path / "opaque-fds.json"
    command = (
        "import json, os, pathlib, sys\n"
        "observed = []\n"
        "for fd in range(3, 256):\n"
        "    try:\n"
        "        os.fstat(fd)\n"
        "    except OSError:\n"
        "        continue\n"
        "    observed.append(fd)\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(observed))\n"
    )
    with _lease_descriptor() as lease_fd:
        terminal = _guardian().run(
            _request(
                lease_fd,
                (sys.executable, "-c", command, str(observed_path)),
                budget=ExecutorGuardianUnboundedBudget(),
            )
        )

    assert type(terminal) is ExecutorGuardianCommandCompleted
    assert terminal.exit_code == 0
    assert json.loads(observed_path.read_text(encoding="utf-8")) == []


def test_guardian_reports_command_start_failure_without_exit_fabrication() -> None:
    with _lease_descriptor() as lease_fd:
        terminal = _guardian().run(
            _request(
                lease_fd,
                ("/definitely/missing/executor-command",),
                budget=ExecutorGuardianUnboundedBudget(),
            )
        )

    assert type(terminal) is ExecutorGuardianCommandStartFailed
    assert terminal.error_type == "FileNotFoundError"
    assert "/definitely/missing/executor-command" in terminal.error_repr


def test_indeterminate_guardian_activation_gets_second_containment_attempt() -> None:
    termination = ExecutorProcessTerminationPolicy(0.1, 1.0)
    launcher = _IndeterminateGuardianLauncher(build_posix_process_launcher())
    guardian = PosixExecutorCommandGuardian(
        ExecutorGuardianProgram(
            (
                str(Path(sys.executable)),
                "-m",
                "issue_orchestrator.execution.host_executor.guardian",
            )
        ),
        _sentinel_program(),
        ProcessGroupSentinelPolicy(0.1, 1.0),
        OsAtomicRecordStoreFactory(),
        launcher,
        PosixGuardianLaunchPipesFactory(OsPosixPipeFactory()),
        PosixProcessGroupSupervisor(
            PosixProcessGroupTerminator(
                termination,
                build_test_process_group_observer(),
            )
        ),
        ExecutorGuardianTerminationPolicy(termination.graceful_shutdown_seconds),
    )

    with _lease_descriptor() as lease_fd:
        with pytest.raises(
            ExecutorGuardianLaunchError,
            match="guardian activation could not prove containment",
        ):
            guardian.run(
                _request(
                    lease_fd,
                    (
                        sys.executable,
                        "-c",
                        "raise AssertionError('command start gate must stay closed')",
                    ),
                    budget=ExecutorGuardianUnboundedBudget(),
                )
            )

    assert launcher.started_process_id is not None
    try:
        os.kill(launcher.started_process_id, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("second-chance guardian containment left a live process")


def test_missing_guardian_result_is_explicit_and_outer_contains_group(
    tmp_path: Path,
) -> None:
    descendant_path = (tmp_path / "guardian-crash-descendant.pid").resolve()
    fault = (
        _fault_guardian_protocol_prelude()
        + ExitingTermResistantProcessTreeProgram(
            descendant_path,
            300,
            23,
        ).python_source()
    )
    with _lease_descriptor() as lease_fd:
        with pytest.raises(
            ExecutorGuardianProtocolError,
            match="without a terminal record",
        ):
            _guardian(
                ExecutorGuardianProgram((str(Path(sys.executable)), "-c", fault))
            ).run(
                _request(
                    lease_fd,
                    (sys.executable, "-c", "raise AssertionError('must not run')"),
                    budget=ExecutorGuardianUnboundedBudget(),
                )
            )

    descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
    ProcessTreeMember(descendant_pid).assert_contained()


def test_malformed_guardian_result_is_never_treated_as_command_success() -> None:
    fault = _fault_guardian_protocol_prelude() + (
        "raw = sys.argv[sys.argv.index('--request-json') + 1]; "
        "fd = json.loads(raw)['result_file_descriptor']; "
        "os.write(fd, b'{}')"
    )
    with _lease_descriptor() as lease_fd:
        with pytest.raises(
            ExecutorGuardianProtocolError,
            match="malformed terminal record",
        ):
            _guardian(
                ExecutorGuardianProgram((str(Path(sys.executable)), "-c", fault))
            ).run(
                _request(
                    lease_fd,
                    (sys.executable, "-c", "raise AssertionError('must not run')"),
                    budget=ExecutorGuardianUnboundedBudget(),
                )
            )


def test_valid_terminal_record_cannot_fabricate_containment() -> None:
    fault = _fault_guardian_protocol_prelude() + (
        "raw = sys.argv[sys.argv.index('--request-json') + 1]; "
        "fd = json.loads(raw)['result_file_descriptor']; "
        'os.write(fd, b\'{"outcome":"completed","exit_code":0}\')'
    )
    with _lease_descriptor() as lease_fd:
        with pytest.raises(
            ExecutorGuardianProtocolError,
            match="requires a contained SIGKILL exit",
        ):
            _guardian(
                ExecutorGuardianProgram((str(Path(sys.executable)), "-c", fault))
            ).run(
                _request(
                    lease_fd,
                    (sys.executable, "-c", "raise AssertionError('must not run')"),
                    budget=ExecutorGuardianUnboundedBudget(),
                )
            )
