"""Public-boundary proofs for the child-side executor command guardian."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import pytest

from issue_orchestrator.domain.executor import (
    ExecutorCommandLifecycle,
    ExecutorDeadlineReason,
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
from issue_orchestrator.execution.host_executor.guardian_launcher import (
    ExecutorGuardianProgram,
    ExecutorGuardianProtocolError,
    PosixExecutorCommandGuardian,
)
from issue_orchestrator.execution.process_group_supervisor import (
    PosixProcessGroupSupervisor,
)
from issue_orchestrator.execution.process_group_terminator import (
    PosixProcessGroupTerminator,
)
from issue_orchestrator.domain.executor import ExecutorProcessTerminationPolicy
from issue_orchestrator.ports.executor_command_guardian import (
    ExecutorGuardianRequest,
)
from tests.process_tree_fixture import ExitingTermResistantProcessTreeProgram


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
        PosixProcessGroupSupervisor(PosixProcessGroupTerminator(termination)),
        ExecutorGuardianTerminationPolicy(termination.graceful_shutdown_seconds),
    )


@contextmanager
def _lease_descriptor() -> Generator[int, None, None]:
    read_fd, write_fd = os.pipe()
    try:
        yield write_fd
    finally:
        os.close(read_fd)
        os.close(write_fd)


def _request(
    lease_fd: int,
    arguments: tuple[str, ...],
    *,
    budget: ExecutorGuardianUnboundedBudget | ExecutorGuardianBoundedBudget,
) -> ExecutorGuardianRequest:
    return ExecutorGuardianRequest(
        arguments=arguments,
        environment=os.environ.copy(),
        lease_file_descriptors=(lease_fd,),
        budget=budget,
        lifecycle=ExecutorCommandLifecycle.DETACHED,
        cancellation=ExecutorNoCommandCancellation(),
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


def _pid_has_exited(pid: int) -> bool:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False


def test_missing_guardian_result_is_explicit_and_outer_contains_group(
    tmp_path: Path,
) -> None:
    descendant_path = (tmp_path / "guardian-crash-descendant.pid").resolve()
    fault = ExitingTermResistantProcessTreeProgram(
        descendant_path,
        30,
        23,
    ).python_source()
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
    assert _pid_has_exited(descendant_pid)


def test_malformed_guardian_result_is_never_treated_as_command_success() -> None:
    fault = (
        "import json, os, sys; "
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
    fault = (
        "import json, os, sys; "
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
