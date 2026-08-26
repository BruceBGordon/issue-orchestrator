"""Public-boundary proofs for the child-side executor command guardian."""

from __future__ import annotations

import json
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
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
    ExecutorGuardianCommandResourceUsage,
    ExecutorGuardianCommandStartFailed,
    ExecutorGuardianPostContainmentError,
    ExecutorGuardianCommandTimedOut,
    ExecutorGuardianTerminationPolicy,
    ExecutorGuardianUnboundedBudget,
)
from issue_orchestrator.domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupCourtesyFailed,
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
    PosixProcessHandle,
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
from tests.process_completion_fixture import (
    NoDescendantProcessContainment,
    PROCESS_COMPLETION_WATCHDOG,
    TextProcessInvocation,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


pytestmark = pytest.mark.timeout(180)


def _guardian(
    program: ExecutorGuardianProgram | None = None,
) -> PosixExecutorCommandGuardian:
    termination = ExecutorProcessTerminationPolicy(0.1, 1.0)
    return _guardian_with_components(
        program
        if program is not None
        else ExecutorGuardianProgram(
            (
                str(Path(sys.executable)),
                "-m",
                "issue_orchestrator.execution.host_executor.guardian",
            )
        ),
        build_posix_process_launcher(),
        PosixProcessGroupSupervisor(
            PosixProcessGroupTerminator(
                termination,
                build_test_process_group_observer(),
            )
        ),
        termination,
    )


def _guardian_with_components(
    program: ExecutorGuardianProgram,
    process_launcher: PosixProcessLauncher,
    process_group_supervisor: ProcessGroupSupervisor,
    termination: ExecutorProcessTerminationPolicy,
) -> PosixExecutorCommandGuardian:
    return PosixExecutorCommandGuardian(
        program,
        _sentinel_program(),
        ProcessGroupSentinelPolicy(0.1, 1.0),
        OsAtomicRecordStoreFactory(),
        process_launcher,
        PosixGuardianLaunchPipesFactory(OsPosixPipeFactory()),
        process_group_supervisor,
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


@dataclass(slots=True)
class _ExternalReapFailingHandle:
    """Retained-handle port fake rejecting only post-containment evidence."""

    delegate: PosixProcessHandle

    @property
    def process_id(self) -> int:
        return self.delegate.process_id

    @property
    def return_code(self) -> int | None:
        return self.delegate.return_code

    def poll(self) -> int | None:
        return self.delegate.poll()

    def wait(self, timeout_seconds: float) -> int:
        """Forward the owner's bounded wait; this fake does not initiate a wait."""
        retained_owner_wait = self.delegate.wait
        return retained_owner_wait(timeout_seconds)

    def kill(self) -> None:
        self.delegate.kill()

    def record_external_reap(self, exit_code: int) -> None:
        self.delegate.record_external_reap(exit_code)
        raise OSError("injected guardian external-reap evidence failure")


class _ExternalReapFailingLauncher:
    def __init__(self, delegate: PosixProcessLauncher) -> None:
        self._delegate = delegate

    def launch(self, specification: PosixProcessLaunchSpec) -> PosixProcessLaunch:
        outcome = self._delegate.launch(specification)
        if type(outcome) is PosixProcessLaunchStarted:
            return PosixProcessLaunchStarted(
                _ExternalReapFailingHandle(outcome.process)
            )
        return outcome


class _CourtesyFailingSupervisor:
    def __init__(
        self,
        delegate: ProcessGroupSupervisor,
        failure: BaseException,
    ) -> None:
        self._delegate = delegate
        self._failure = failure

    def supervise(
        self,
        leader: OwnedProcessGroupLeader,
        wait: ProcessGroupWait,
        interruption: ProcessGroupInterruption,
    ) -> ProcessGroupSupervision:
        supervision = self._delegate.supervise(leader, wait, interruption)
        if type(supervision) is not ProcessGroupCompleted:
            raise AssertionError("courtesy fault requires natural completion")
        return ProcessGroupCompleted(
            ProcessGroupTermination(
                supervision.termination.leader_exit_code,
                ProcessGroupCourtesyFailed(self._failure),
            )
        )

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


def test_foreign_parent_child_cannot_contaminate_guardian_resource_usage(
    tmp_path: Path,
) -> None:
    command_started = tmp_path / "command-started"

    def run_foreign_cpu_child() -> None:
        source = (
            "from pathlib import Path; import sys, time; "
            "marker=Path(sys.argv[1]); "
            "exec('while not marker.exists():\\n time.sleep(0.01)'); "
            "end=time.process_time()+0.5; "
            "exec('while time.process_time() < end:\\n pass')"
        )
        result = PROCESS_COMPLETION_WATCHDOG.run_text(
            TextProcessInvocation(
                operation="foreign resource-accounting child",
                arguments=(sys.executable, "-c", source, str(command_started)),
                working_directory=REPO_ROOT,
                environment=os.environ,
                timeout_containment=NoDescendantProcessContainment(),
            )
        )
        assert result.returncode == 0, result.stderr

    command = (
        "from pathlib import Path; import sys, time; "
        "Path(sys.argv[1]).touch(); time.sleep(1.0)"
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        foreign_child = pool.submit(run_foreign_cpu_child)
        with _lease_descriptor() as lease_fd:
            terminal = _guardian().run(
                _request(
                    lease_fd,
                    (sys.executable, "-c", command, str(command_started)),
                    budget=ExecutorGuardianUnboundedBudget(),
                )
            )
        PROCESS_COMPLETION_WATCHDOG.future_result(
            foreign_child,
            operation="foreign resource-accounting child worker",
        )

    assert type(terminal) is ExecutorGuardianCommandCompleted
    assert type(terminal.resources) is ExecutorGuardianCommandResourceUsage
    assert terminal.resources.cpu_seconds < 0.2


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


def test_valid_terminal_survives_external_reap_evidence_failure() -> None:
    termination = ExecutorProcessTerminationPolicy(0.1, 1.0)
    guardian = _guardian_with_components(
        ExecutorGuardianProgram(
            (
                str(Path(sys.executable)),
                "-m",
                "issue_orchestrator.execution.host_executor.guardian",
            )
        ),
        _ExternalReapFailingLauncher(build_posix_process_launcher()),
        PosixProcessGroupSupervisor(
            PosixProcessGroupTerminator(
                termination,
                build_test_process_group_observer(),
            )
        ),
        termination,
    )

    with _lease_descriptor() as lease_fd:
        with pytest.raises(ExecutorGuardianPostContainmentError) as raised:
            guardian.run(
                _request(
                    lease_fd,
                    (sys.executable, "-c", "raise SystemExit(19)"),
                    budget=ExecutorGuardianUnboundedBudget(),
                )
            )

    assert type(raised.value.terminal) is ExecutorGuardianCommandCompleted
    assert raised.value.terminal.exit_code == 19
    [failure] = raised.value.failures
    assert failure.attempt_name == "record guardian external reap"
    assert "injected guardian external-reap" in str(failure.error)


def test_valid_terminal_survives_courtesy_evidence_failure() -> None:
    termination = ExecutorProcessTerminationPolicy(0.1, 1.0)
    courtesy_error = OSError("injected guardian courtesy evidence failure")
    guardian = _guardian_with_components(
        ExecutorGuardianProgram(
            (
                str(Path(sys.executable)),
                "-m",
                "issue_orchestrator.execution.host_executor.guardian",
            )
        ),
        build_posix_process_launcher(),
        _CourtesyFailingSupervisor(
            PosixProcessGroupSupervisor(
                PosixProcessGroupTerminator(
                    termination,
                    build_test_process_group_observer(),
                )
            ),
            courtesy_error,
        ),
        termination,
    )

    with _lease_descriptor() as lease_fd:
        with pytest.raises(ExecutorGuardianPostContainmentError) as raised:
            guardian.run(
                _request(
                    lease_fd,
                    (sys.executable, "-c", "raise SystemExit(23)"),
                    budget=ExecutorGuardianUnboundedBudget(),
                )
            )

    assert type(raised.value.terminal) is ExecutorGuardianCommandCompleted
    assert raised.value.terminal.exit_code == 23
    [failure] = raised.value.failures
    assert failure.attempt_name == "retain guardian courtesy-wait failure"
    assert failure.error is courtesy_error


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


def test_guardian_without_owner_readiness_is_contained_and_identity_is_reusable(
    tmp_path: Path,
) -> None:
    cancellation_dir = (tmp_path / "cancellation").resolve()
    cancellation_dir.mkdir()
    descendant_path = (tmp_path / "pre-readiness-descendant.pid").resolve()
    fault = ExitingTermResistantProcessTreeProgram(
        descendant_path,
        300,
        23,
    ).python_source()
    before_descriptors = len(os.listdir("/dev/fd"))

    with _lease_descriptor() as lease_fd:
        with pytest.raises(
            ExecutorGuardianLaunchError,
            match="before publishing exact owner readiness",
        ):
            _guardian(
                ExecutorGuardianProgram((str(Path(sys.executable)), "-c", fault))
            ).run(
                _request(
                    lease_fd,
                    (sys.executable, "-c", "raise AssertionError('must not run')"),
                    budget=ExecutorGuardianUnboundedBudget(),
                    lifecycle=ExecutorCommandLifecycle.INTERACTIVE_SESSION,
                    cancellation=ExecutorInteractiveSessionCancellation.for_run_dir(
                        cancellation_dir
                    ),
                )
            )

    descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
    ProcessTreeMember(descendant_pid).assert_contained()

    with _lease_descriptor() as lease_fd:
        terminal = _guardian().run(
            _request(
                lease_fd,
                (sys.executable, "-c", "raise SystemExit(0)"),
                budget=ExecutorGuardianUnboundedBudget(),
                lifecycle=ExecutorCommandLifecycle.INTERACTIVE_SESSION,
                cancellation=ExecutorInteractiveSessionCancellation.for_run_dir(
                    cancellation_dir
                ),
            )
        )

    assert type(terminal) is ExecutorGuardianCommandCompleted
    assert terminal.exit_code == 0
    assert len(os.listdir("/dev/fd")) == before_descriptors


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
        "os.write(fd, b'{\"outcome\":\"completed\",\"exit_code\":0,"
        "\"resources\":{\"availability\":\"available\","
        "\"wall_seconds\":1.0,\"cpu_seconds\":0.0,"
        "\"max_rss_bytes\":0,\"input_blocks\":0,"
        "\"output_blocks\":0}}')"
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
