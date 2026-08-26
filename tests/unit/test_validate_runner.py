"""Tests for validate_runner CLI.

The validate_runner captures validation output to a known location
so agents can find failure details without re-running tests.
"""

import json
import os
import signal
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_orchestrator.domain.contained_command import (
    ContainedCommandCaptureSucceeded,
    ContainedCommandCleanupError,
    ContainedCommandCompleted,
    ContainedCommandExited,
    ContainedCommandFailure,
    ContainedCommandFinalizationFailed,
    ContainedCommandMetrics,
    ContainedCommandResult,
    ContainedCommandSupervised,
)
from issue_orchestrator.domain.retained_thread import (
    RetainedThreadActivated,
    RetainedThreadActivation,
    RetainedThreadFinalization,
    RetainedThreadFinalized,
    RetainedThreadFinalizedAfterFailure,
    RetainedThreadShutdownPolicy,
    RetainedThreadSpec,
    RetainedThreadState,
)
from issue_orchestrator.domain.validation_resource_sampling import (
    ValidationHostProbeObserved,
    ValidationHostProbeRequest,
    ValidationHostProbeResult,
)
from issue_orchestrator.domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupSupervision,
    ProcessGroupTermination,
    ProcessGroupWait,
)
from issue_orchestrator.execution.contained_command_capture import (
    OsContainedCommandOutputPipeFactory,
    PosixContainedCommandCapture,
)
from issue_orchestrator.domain.contained_command import ContainedCommandOutputPolicy
from issue_orchestrator.infra.validation_timings import (
    ValidateTimingRecorder,
)
from issue_orchestrator.entrypoints.cli_tools.validate_runner import (
    ValidationRunnerClock,
    run_validation,
)
from issue_orchestrator.entrypoints.bootstrap import (
    build_contained_command_capture,
    build_posix_process_launcher,
    build_process_group_supervisor,
)
from issue_orchestrator.entrypoints.bootstrap_executor import (
    build_retained_thread_factory,
)
from issue_orchestrator.ports.process_group_supervisor import (
    ProcessGroupInterruption,
    ProcessGroupSupervisor,
)
from issue_orchestrator.ports.contained_command import (
    ContainedCommandCapture,
    ContainedCommandLineObserver,
    ContainedCommandOutput,
    ContainedShellCommand,
)
from issue_orchestrator.ports.retained_thread import (
    RetainedThreadFactory,
    RetainedThreadLease,
)
from issue_orchestrator.ports.validation_host_probe import ValidationHostProbe
from tests.process_tree_fixture import (
    CooperativeTermResistantProcessTreeProgram,
    ExitingTermResistantProcessTreeProgram,
    ProcessTreeMember,
)
from tests.process_completion_fixture import (
    NoDescendantProcessContainment,
    PROCESS_COMPLETION_WATCHDOG,
    TextProcessInvocation,
)
from tests.unit.threading_helpers import run_in_thread


def _with_repo_on_pythonpath(env: dict[str, str]) -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[2]
    pythonpath = env.get("PYTHONPATH")
    env = dict(env)
    env["PYTHONPATH"] = str(repo_root / "src") + (
        os.pathsep + pythonpath if pythonpath else ""
    )
    return env


@dataclass(frozen=True, slots=True)
class _NoHostEvidenceProbe(ValidationHostProbe):
    """Fast port fake for validate-runner tests unrelated to host observations."""

    def run(self, request: ValidationHostProbeRequest) -> ValidationHostProbeResult:
        del request
        return ValidationHostProbeObserved("")


_NO_HOST_EVIDENCE_PROBE = _NoHostEvidenceProbe()


def _run_validation_cli(
    arguments: list[str],
    *,
    cwd: Path,
    capture_output: bool,
    text: bool,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run the validation CLI through the shared completion/containment owner."""
    if not capture_output or not text:
        raise ValueError("validation CLI fixture requires captured text output")
    return PROCESS_COMPLETION_WATCHDOG.run_text(
        TextProcessInvocation(
            operation="validate-runner CLI",
            arguments=tuple(arguments),
            working_directory=cwd.resolve(),
            environment=env,
            timeout_containment=NoDescendantProcessContainment(),
        )
    )


class _ContainThenReportCleanupFailureSupervisor(ProcessGroupSupervisor):
    """Port fake that proves evidence survives a failing cleanup report."""

    def __init__(self, delegate: ProcessGroupSupervisor, readiness_path: Path) -> None:
        if not isinstance(delegate, ProcessGroupSupervisor):
            raise ValueError("cleanup-failure supervisor requires a delegate")
        self._delegate = delegate
        if not readiness_path.is_absolute():
            raise ValueError("cleanup-failure readiness path must be absolute")
        self._readiness_path = readiness_path

    def supervise(
        self,
        leader: OwnedProcessGroupLeader,
        wait: ProcessGroupWait,
        interruption: ProcessGroupInterruption,
    ) -> ProcessGroupSupervision:
        del leader, wait, interruption
        PROCESS_COMPLETION_WATCHDOG.wait_for_path(
            self._readiness_path,
            operation="cleanup-failure child readiness",
        )
        raise RuntimeError("injected supervision failure")

    def abort(self, leader: OwnedProcessGroupLeader) -> ProcessGroupTermination:
        self._delegate.abort(leader)
        raise RuntimeError("injected cleanup failure")


@dataclass(frozen=True, slots=True)
class _StaticContainedCommandCapture(ContainedCommandCapture):
    result: ContainedCommandResult

    def capture(
        self,
        command: ContainedShellCommand,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> ContainedCommandResult:
        del command, output, line_observer
        return self.result


@dataclass(frozen=True, slots=True)
class _RaisingContainedCommandCapture(ContainedCommandCapture):
    failure: RuntimeError

    def capture(
        self,
        command: ContainedShellCommand,
        output: ContainedCommandOutput,
        line_observer: ContainedCommandLineObserver,
    ) -> ContainedCommandResult:
        del command, output, line_observer
        raise self.failure


@dataclass(slots=True)
class _FinalizationFailingRetainedThreadLease(RetainedThreadLease):
    failure: RuntimeError
    _state: RetainedThreadState = field(
        default=RetainedThreadState.CREATED,
        init=False,
    )

    @property
    def state(self) -> RetainedThreadState:
        return self._state

    def activate(self) -> RetainedThreadActivation:
        self._state = RetainedThreadState.ACTIVATED
        return RetainedThreadActivated()

    def finalize(
        self,
        policy: RetainedThreadShutdownPolicy,
    ) -> RetainedThreadFinalization:
        del policy
        return RetainedThreadFinalizedAfterFailure(self.failure)


@dataclass(frozen=True, slots=True)
class _FinalizationFailingRetainedThreadFactory(RetainedThreadFactory):
    failure: RuntimeError

    def prepare(
        self,
        spec: RetainedThreadSpec,
        target: Callable[[], None],
    ) -> RetainedThreadLease:
        del spec, target
        return _FinalizationFailingRetainedThreadLease(self.failure)


@dataclass(slots=True)
class _ActivationRaisingRetainedThreadLease(RetainedThreadLease):
    failure: RuntimeError
    finalization_action: Callable[[], None]
    _state: RetainedThreadState = field(
        default=RetainedThreadState.CREATED,
        init=False,
    )

    @property
    def state(self) -> RetainedThreadState:
        return self._state

    def activate(self) -> RetainedThreadActivation:
        self._state = RetainedThreadState.ACTIVATING
        raise self.failure

    def finalize(
        self,
        policy: RetainedThreadShutdownPolicy,
    ) -> RetainedThreadFinalization:
        del policy
        self.finalization_action()
        return RetainedThreadFinalized()


@dataclass(frozen=True, slots=True)
class _ActivationRaisingRetainedThreadFactory(RetainedThreadFactory):
    failure: RuntimeError
    finalization_action: Callable[[], None]

    def prepare(
        self,
        spec: RetainedThreadSpec,
        target: Callable[[], None],
    ) -> RetainedThreadLease:
        del spec, target
        return _ActivationRaisingRetainedThreadLease(
            self.failure,
            self.finalization_action,
        )


@dataclass(slots=True)
class _FinalizationActionRetainedThreadLease(RetainedThreadLease):
    finalization_action: Callable[[], None]
    _state: RetainedThreadState = field(
        default=RetainedThreadState.CREATED,
        init=False,
    )

    @property
    def state(self) -> RetainedThreadState:
        return self._state

    def activate(self) -> RetainedThreadActivation:
        self._state = RetainedThreadState.ACTIVATED
        return RetainedThreadActivated()

    def finalize(
        self,
        policy: RetainedThreadShutdownPolicy,
    ) -> RetainedThreadFinalization:
        del policy
        self.finalization_action()
        return RetainedThreadFinalized()


@dataclass(frozen=True, slots=True)
class _FinalizationActionRetainedThreadFactory(RetainedThreadFactory):
    finalization_action: Callable[[], None]

    def prepare(
        self,
        spec: RetainedThreadSpec,
        target: Callable[[], None],
    ) -> RetainedThreadLease:
        del spec, target
        return _FinalizationActionRetainedThreadLease(self.finalization_action)


def _contained_finalization_failure(
    failure: RuntimeError,
) -> ContainedCommandFinalizationFailed:
    return ContainedCommandFinalizationFailed(
        child=ContainedCommandExited(42_424, 0),
        capture=ContainedCommandCaptureSucceeded(),
        cleanup=ContainedCommandSupervised(),
        finalization_failure=ContainedCommandFailure(failure),
        metrics=ContainedCommandMetrics(2, 20),
    )


def _leaf_exception_types(error: BaseException) -> set[type[BaseException]]:
    if isinstance(error, BaseExceptionGroup):
        return {
            error_type
            for nested in error.exceptions
            for error_type in _leaf_exception_types(nested)
        }
    return {type(error)}


class TestValidateRunner:
    """Test the validate_runner CLI."""

    @pytest.fixture
    def fake_git_repo(self, tmp_path: Path) -> Path:
        """Create a fake git repo structure for testing."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        return repo

    def test_captures_output_to_env_var_dir(self, fake_git_repo: Path, tmp_path: Path):
        """Test that output is captured to ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command",
                "echo 'test output'",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    **os.environ,
                    "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
                }
            ),
        )

        assert result.returncode == 0
        output_file = output_dir / "validation-output.log"
        assert output_file.exists()
        assert "test output" in output_file.read_text()

    def test_falls_back_to_diagnostics_dir(self, fake_git_repo: Path):
        """Test that output falls back to .issue-orchestrator/diagnostics/."""
        result = _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command",
                "echo 'fallback test'",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    k: v
                    for k, v in os.environ.items()
                    if not k.startswith("ISSUE_ORCHESTRATOR")
                }
            ),
        )

        assert result.returncode == 0
        output_file = (
            fake_git_repo
            / ".issue-orchestrator"
            / "diagnostics"
            / "validation-output.log"
        )
        assert output_file.exists()
        assert "fallback test" in output_file.read_text()

    def test_prints_path_on_failure(self, fake_git_repo: Path, tmp_path: Path):
        """Test that failure message includes path to output file."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command",
                "exit 1",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    **os.environ,
                    "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
                }
            ),
        )

        assert result.returncode == 1
        assert "Full output saved to:" in result.stdout
        assert "validation-output.log" in result.stdout

    def test_returns_command_exit_code(self, fake_git_repo: Path, tmp_path: Path):
        """Test that exit code matches the underlying command."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        # Test exit code 0
        result = _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command",
                "exit 0",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    **os.environ,
                    "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
                }
            ),
        )
        assert result.returncode == 0

        # Test exit code 42
        result = _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command",
                "exit 42",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    **os.environ,
                    "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
                }
            ),
        )
        assert result.returncode == 42

    def test_captures_stderr(self, fake_git_repo: Path, tmp_path: Path):
        """Test that stderr is captured in the output file."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command",
                "echo 'stderr message' >&2",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    **os.environ,
                    "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
                }
            ),
        )

        output_file = output_dir / "validation-output.log"
        assert output_file.exists()
        assert "stderr message" in output_file.read_text()

    def test_fails_if_no_command_configured(self, fake_git_repo: Path, tmp_path: Path):
        """Test that it fails with clear error if no command is provided."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    **os.environ,
                    "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
                }
            ),
        )

        assert result.returncode == 2
        assert "No validation command configured" in result.stderr

    def test_streams_output_to_terminal(self, fake_git_repo: Path, tmp_path: Path):
        """Test that output is streamed to terminal while also being captured."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command",
                "echo 'visible output'",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    **os.environ,
                    "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
                }
            ),
        )

        # Output should appear in terminal (stdout)
        assert "visible output" in result.stdout

        # And also be captured to file
        output_file = output_dir / "validation-output.log"
        assert "visible output" in output_file.read_text()

    def test_orchestrated_runs_emit_concise_lifecycle_markers(
        self, fake_git_repo: Path, tmp_path: Path
    ):
        """Orchestrated validation should summarize stdout but keep full file output."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command",
                "printf 'line one\\nline two\\n'",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    **os.environ,
                    "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
                }
            ),
        )

        assert result.returncode == 0
        stdout_lines = result.stdout.splitlines()
        assert "line one" not in stdout_lines
        assert "line two" not in stdout_lines
        assert (
            "[orchestrated] full output -> file; terminal shows lifecycle markers only"
            in result.stdout
        )
        assert "[validate_runner] child_started pid=" in result.stdout
        assert "[validate_runner] stdout_eof pid=" in result.stdout
        assert "[validate_runner] child_exited pid=" in result.stdout
        assert "Full output saved to:" in result.stdout

        output_file = output_dir / "validation-output.log"
        content = output_file.read_text()
        assert "line one" in content
        assert "line two" in content
        assert "[validate_runner] child_started pid=" in content
        assert "[validate_runner] child_exited pid=" in content

    def test_appends_target_timing_records_to_shared_git_dir(self, fake_git_repo: Path):
        """Timing markers should be persisted as JSONL under the shared git dir."""
        command = (
            "printf '[validate-timing] CONFIG validate_jobs=10 unit_parallel=auto "
            "simulated_parallel=auto integration_parallel=auto static_jobs=10 "
            "test_jobs=1 web_jobs=1 live_web_jobs=2 agent_jobs=1 e2e_jobs=1\\n'"
            " && printf '[validate-timing] START target=test-unit at=2026-03-14T09:10:13-0600\\n'"
            " && printf '[validate-timing] END target=test-unit status=0 elapsed=12s "
            "at=2026-03-14T09:10:25-0600\\n'"
        )

        result = _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command",
                command,
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    k: v
                    for k, v in os.environ.items()
                    if not k.startswith("ISSUE_ORCHESTRATOR")
                }
            ),
        )

        assert result.returncode == 0
        timings_file = (
            fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
        )
        assert timings_file.exists()

        records = [json.loads(line) for line in timings_file.read_text().splitlines()]
        target_record = next(
            record for record in records if record["kind"] == "target_timing"
        )
        assert target_record["target"] == "test-unit"
        assert target_record["elapsed_seconds"] == 12
        assert target_record["validate_jobs"] == "10"
        assert target_record["unit_parallel"] == "auto"
        assert target_record["simulated_parallel"] == "auto"
        assert target_record["integration_parallel"] == "auto"
        assert target_record["static_jobs"] == "10"
        assert target_record["test_jobs"] == "1"
        assert target_record["web_jobs"] == "1"
        assert target_record["live_web_jobs"] == "2"
        assert target_record["agent_jobs"] == "1"
        assert target_record["e2e_jobs"] == "1"
        assert target_record["host_name"]
        assert target_record["host_system"]
        assert target_record["host_machine"]
        assert isinstance(target_record["host_cpu_count"], int)
        assert target_record["host_memory_bytes"] is None or isinstance(
            target_record["host_memory_bytes"], int
        )
        assert target_record["started_at"] == "2026-03-14T09:10:13-0600"
        assert target_record["ended_at"] == "2026-03-14T09:10:25-0600"

    def test_records_malformed_known_timing_marker_without_raising(
        self,
        fake_git_repo: Path,
    ) -> None:
        """Profiler corruption is explicit but cannot replace command semantics."""
        recorder = ValidateTimingRecorder(
            worktree=fake_git_repo, command="make validate"
        )

        recorder.process_line(
            "[validate-timing] CONFIG validate_jobs=10 unit_parallel=auto\n"
        )
        recorder.process_line("[validate-timing] CONFIG \n")

        records = [
            json.loads(line)
            for line in (
                fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert records[-1]["kind"] == "timing_protocol_failure"
        assert records[-1]["failure_kind"] == "malformed-marker"

    @pytest.mark.parametrize(
        ("command", "expected_failure_kind"),
        (
            (
                "printf '[validate-timing] START target=duplicate at=one\\n' && "
                "printf '[validate-timing] START target=duplicate at=two\\n'",
                "duplicate-start",
            ),
            (
                "printf '[validate-timing] END target=missing status=0 "
                "elapsed=1s at=now\\n'",
                "end-without-start",
            ),
            (
                "printf '[validate-timing] CONFIG host_cpus=18 host_cpus=19\\n'",
                "malformed-marker",
            ),
            (
                "printf '[validate-timing] START target=invalid-status at=one\\n' "
                "&& printf '[validate-timing] END target=invalid-status "
                "status=-9223372036854775809 elapsed=1s at=two\\n'",
                "malformed-marker",
            ),
            (
                "printf '[validate-timing] malformed\\n'",
                "malformed-marker",
            ),
        ),
    )
    def test_profiler_failure_preserves_child_success_and_marks_timing_partial(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
        command: str,
        expected_failure_kind: str,
    ) -> None:
        output_dir = tmp_path / "output"
        sampler_threads_before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "validate-resource-sampler"
        }

        exit_code = run_validation(
            command,
            output_dir,
            fake_git_repo,
            clock=ValidationRunnerClock(
                lambda: datetime.now(timezone.utc),
                time.monotonic,
            ),
            contained_command_capture=build_contained_command_capture(),
            retained_thread_factory=build_retained_thread_factory(),
            host_probe=_NO_HOST_EVIDENCE_PROBE,
        )

        assert exit_code == 0
        sampler_threads_after = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "validate-resource-sampler"
        }
        assert sampler_threads_after == sampler_threads_before
        records = [
            json.loads(line)
            for line in (
                fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        failure = next(
            record for record in records if record["kind"] == "timing_protocol_failure"
        )
        assert failure["failure_kind"] == expected_failure_kind
        summary = next(record for record in records if record["kind"] == "run_summary")
        assert summary["lifecycle"] == "completed"
        assert summary["process_group_cleanup"] == "supervised"
        assert summary["exit_code"] == 0
        assert summary["child_exit_code"] == 0
        assert summary["timing_protocol_status"] == "partial"
        assert summary["timing_protocol_failure_count"] == 1

    def test_oversized_profiler_marker_preserves_exact_child_success(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        source = (
            "import os; os.write(1, b'[validate-timing] ' + (b'x' * 20000) + b'\\n')"
        )

        exit_code = run_validation(
            shlex.join((sys.executable, "-c", source)),
            tmp_path / "oversized-marker-output",
            fake_git_repo,
            clock=ValidationRunnerClock(
                lambda: datetime.now(timezone.utc),
                time.monotonic,
            ),
            contained_command_capture=build_contained_command_capture(),
            retained_thread_factory=build_retained_thread_factory(),
            host_probe=_NO_HOST_EVIDENCE_PROBE,
        )

        assert exit_code == 0
        records = [
            json.loads(line)
            for line in (
                fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        failures = [
            record for record in records if record["kind"] == "timing_protocol_failure"
        ]
        assert len(failures) == 1
        assert failures[0]["failure_kind"] == "malformed-marker"
        assert failures[0]["line_truncated"] is True
        assert len(failures[0]["line"]) == 16_384
        summary = next(record for record in records if record["kind"] == "run_summary")
        assert summary["lifecycle"] == "completed"
        assert summary["child_exit_code"] == 0
        assert summary["timing_protocol_failure_count"] == 1

    @pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process cleanup")
    def test_cleanup_failure_retains_both_errors_stops_sampler_and_finalizes(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        child_pid_path = (tmp_path / "cleanup-failure-child.pid").resolve()
        cooperative_leader = CooperativeTermResistantProcessTreeProgram(
            child_pid_path,
            300,
            ("capture-ready",),
        ).python_source()
        command = (
            f"exec {shlex.quote(sys.executable)} -c {shlex.quote(cooperative_leader)}"
        )
        sampler_threads_before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "validate-resource-sampler"
        }
        supervisor = _ContainThenReportCleanupFailureSupervisor(
            build_process_group_supervisor(),
            child_pid_path,
        )

        with pytest.raises(
            ContainedCommandCleanupError,
            match="injected cleanup failure",
        ):
            run_validation(
                command,
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(
                    lambda: datetime.now(timezone.utc),
                    time.monotonic,
                ),
                contained_command_capture=PosixContainedCommandCapture(
                    build_posix_process_launcher(),
                    supervisor,
                    ContainedCommandOutputPolicy(
                        poll_interval_seconds=0.01,
                        shutdown_timeout_seconds=1.0,
                        final_drain_byte_limit=1_048_576,
                    ),
                    OsContainedCommandOutputPipeFactory(),
                ),
                retained_thread_factory=build_retained_thread_factory(),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        ProcessTreeMember(child_pid).assert_contained()
        sampler_threads_after = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "validate-resource-sampler"
        }
        assert sampler_threads_after == sampler_threads_before
        records = [
            json.loads(line)
            for line in (
                fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        summary = next(record for record in records if record["kind"] == "run_summary")
        assert summary["lifecycle"] == "cleanup-failed"
        assert summary["process_group_cleanup"] == "cleanup-failed"
        assert summary["capture_status"] == "failed"
        assert summary["capture_error_type"] == "RuntimeError"
        assert "injected supervision failure" in summary["capture_error_repr"]
        assert summary["cleanup_error_type"] == "RuntimeError"
        assert "injected cleanup failure" in summary["cleanup_error_repr"]
        assert summary["child_outcome"] == "exit-unknown"
        assert summary["child_exit_code"] is None
        assert isinstance(summary["child_process_id"], int)
        assert summary["timing_protocol_status"] == "complete"

    def test_finalization_failure_records_summary_and_terminal_marker(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        failure = RuntimeError("injected command finalization failure")
        output_dir = (tmp_path / "finalization-output").resolve()

        with pytest.raises(RuntimeError) as caught:
            run_validation(
                "true",
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(
                    lambda: datetime.now(timezone.utc),
                    time.monotonic,
                ),
                contained_command_capture=_StaticContainedCommandCapture(
                    _contained_finalization_failure(failure)
                ),
                retained_thread_factory=build_retained_thread_factory(),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        assert caught.value is failure
        records = [
            json.loads(line)
            for line in (
                fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        summary = next(record for record in records if record["kind"] == "run_summary")
        assert summary["lifecycle"] == "finalization-failed"
        assert summary["process_group_cleanup"] == "supervised"
        assert summary["capture_status"] == "succeeded"
        assert summary["cleanup_error_repr"] == repr(failure)
        output = (output_dir / "validation-output.log").read_text(encoding="utf-8")
        assert "[validate_runner] child_exited" in output
        assert "lifecycle=finalization-failed" in output
        assert "process_group_cleanup=supervised" in output

    def test_sampler_start_exception_retains_provenance_and_attempts_finalization(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        start_failure = RuntimeError("injected sampler activation exception")
        output_dir = (tmp_path / "sampler-start-output").resolve()
        sampler_finalized = False

        def record_sampler_finalization() -> None:
            nonlocal sampler_finalized
            sampler_finalized = True

        with pytest.raises(RuntimeError) as caught:
            run_validation(
                "true",
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(
                    lambda: datetime.now(timezone.utc),
                    time.monotonic,
                ),
                contained_command_capture=_StaticContainedCommandCapture(
                    ContainedCommandCompleted(
                        ContainedCommandExited(42_424, 0),
                        ContainedCommandMetrics(0, 0),
                    )
                ),
                retained_thread_factory=_ActivationRaisingRetainedThreadFactory(
                    start_failure,
                    record_sampler_finalization,
                ),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        assert caught.value is start_failure
        assert sampler_finalized
        records = [
            json.loads(line)
            for line in (
                fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        summary = next(record for record in records if record["kind"] == "run_summary")
        assert summary["lifecycle"] == "capture-failed"
        assert summary["child_outcome"] == "not-started"
        assert summary["capture_error_repr"] == repr(start_failure)
        output_file = output_dir / "validation-output.log"
        assert "lifecycle=capture-failed" in output_file.read_text(encoding="utf-8")
        output_file.unlink()

    def test_timing_finalization_failure_still_emits_exact_terminal_marker(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        output_dir = (tmp_path / "timing-finalization-output").resolve()
        timings_file = (
            fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
        )

        def poison_timing_destination() -> None:
            timings_file.unlink()
            timings_file.mkdir()

        with pytest.raises(IsADirectoryError):
            run_validation(
                "true",
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(
                    lambda: datetime.now(timezone.utc),
                    time.monotonic,
                ),
                contained_command_capture=_StaticContainedCommandCapture(
                    ContainedCommandCompleted(
                        ContainedCommandExited(42_424, 0),
                        ContainedCommandMetrics(2, 20),
                    )
                ),
                retained_thread_factory=_FinalizationActionRetainedThreadFactory(
                    poison_timing_destination
                ),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        output = (output_dir / "validation-output.log").read_text(encoding="utf-8")
        assert "[validate_runner] child_exited pid=42424" in output
        assert "child_exit_code=0" in output
        assert "lifecycle=finalization-failed" in output
        assert "lines=2 bytes=20" in output

    def test_terminal_file_failure_does_not_erase_durable_command_fact(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        output_dir = (tmp_path / "missing-marker-output").resolve()
        output_file = output_dir / "validation-output.log"

        def remove_terminal_destination() -> None:
            output_file.unlink()
            output_dir.rmdir()

        with pytest.raises(FileNotFoundError):
            run_validation(
                "true",
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(
                    lambda: datetime.now(timezone.utc),
                    time.monotonic,
                ),
                contained_command_capture=_StaticContainedCommandCapture(
                    ContainedCommandCompleted(
                        ContainedCommandExited(42_424, 0),
                        ContainedCommandMetrics(2, 20),
                    )
                ),
                retained_thread_factory=_FinalizationActionRetainedThreadFactory(
                    remove_terminal_destination
                ),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        records = [
            json.loads(line)
            for line in (
                fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        summary = next(record for record in records if record["kind"] == "run_summary")
        assert summary["lifecycle"] == "finalization-failed"
        assert summary["child_process_id"] == 42_424
        assert summary["child_exit_code"] == 0
        terminal_output = capsys.readouterr().out
        assert "[validate_runner] child_exited pid=42424" in terminal_output
        assert "lifecycle=finalization-failed" in terminal_output

    def test_terminal_marker_and_recorder_failures_retain_both_faults(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        output_dir = (tmp_path / "dual-reporting-failure").resolve()
        output_file = output_dir / "validation-output.log"
        timings_file = (
            fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
        )

        def break_file_and_recorder_sinks() -> None:
            output_file.unlink()
            output_dir.rmdir()
            timings_file.unlink(missing_ok=True)
            timings_file.mkdir()

        with pytest.raises(BaseExceptionGroup) as caught:
            run_validation(
                "true",
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(
                    lambda: datetime.now(timezone.utc),
                    time.monotonic,
                ),
                contained_command_capture=_StaticContainedCommandCapture(
                    ContainedCommandCompleted(
                        ContainedCommandExited(42_424, 0),
                        ContainedCommandMetrics(2, 20),
                    )
                ),
                retained_thread_factory=_FinalizationActionRetainedThreadFactory(
                    break_file_and_recorder_sinks
                ),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        assert _leaf_exception_types(caught.value) == {
            FileNotFoundError,
            IsADirectoryError,
        }
        terminal_output = capsys.readouterr().out
        assert "child_exited pid=42424" in terminal_output
        assert "lifecycle=finalization-failed" in terminal_output

    def test_sampler_failure_combines_with_exact_finalization_provenance(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        command_failure = RuntimeError("injected command finalization failure")
        sampler_failure = RuntimeError("injected sampler finalization failure")
        output_dir = (tmp_path / "combined-finalization-output").resolve()

        with pytest.raises(BaseExceptionGroup) as caught:
            run_validation(
                "true",
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(
                    lambda: datetime.now(timezone.utc),
                    time.monotonic,
                ),
                contained_command_capture=_StaticContainedCommandCapture(
                    _contained_finalization_failure(command_failure)
                ),
                retained_thread_factory=_FinalizationFailingRetainedThreadFactory(
                    sampler_failure
                ),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        assert caught.value.exceptions == (command_failure, sampler_failure)
        records = [
            json.loads(line)
            for line in (
                fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        summary = next(record for record in records if record["kind"] == "run_summary")
        assert summary["cleanup_error_type"] == "ExceptionGroup"
        assert "command finalization failure" in summary["cleanup_error_repr"]
        assert "sampler finalization failure" in summary["cleanup_error_repr"]
        assert "[validate_runner] child_exited" in (
            output_dir / "validation-output.log"
        ).read_text(encoding="utf-8")

    def test_raising_capture_stops_sampler_and_records_unavailable_terminal_fact(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        failure = RuntimeError("injected contained-capture port failure")
        output_dir = (tmp_path / "raising-capture-output").resolve()
        sampler_threads_before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "validate-resource-sampler"
        }

        with pytest.raises(RuntimeError) as caught:
            run_validation(
                "true",
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(
                    lambda: datetime.now(timezone.utc),
                    time.monotonic,
                ),
                contained_command_capture=_RaisingContainedCommandCapture(failure),
                retained_thread_factory=build_retained_thread_factory(),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        assert caught.value is failure
        sampler_threads_after = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "validate-resource-sampler"
        }
        assert sampler_threads_after == sampler_threads_before
        records = [
            json.loads(line)
            for line in (
                fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        summary = next(record for record in records if record["kind"] == "run_summary")
        assert summary["child_outcome"] == "unavailable"
        assert summary["process_group_cleanup"] == "unknown"
        assert summary["capture_error_repr"] == repr(failure)
        output = (output_dir / "validation-output.log").read_text(encoding="utf-8")
        assert "[validate_runner] command_terminal" in output
        assert "pid=unavailable" in output

    def test_appends_run_summary_record_to_shared_git_dir(self, fake_git_repo: Path):
        """Each validate run should append a run summary record."""
        result = _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command",
                "echo ok",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    k: v
                    for k, v in os.environ.items()
                    if not k.startswith("ISSUE_ORCHESTRATOR")
                }
            ),
        )

        assert result.returncode == 0
        timings_file = (
            fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
        )
        assert timings_file.exists()

        records = [json.loads(line) for line in timings_file.read_text().splitlines()]
        summary_record = next(
            record for record in records if record["kind"] == "run_summary"
        )
        assert summary_record["command"] == "echo ok"
        assert summary_record["worktree"] == str(fake_git_repo)
        assert summary_record["exit_code"] == 0
        assert summary_record["child_exit_code"] == 0
        assert summary_record["lifecycle"] == "completed"
        assert summary_record["process_group_cleanup"] == "supervised"
        assert isinstance(summary_record["total_elapsed_seconds"], float)
        assert isinstance(summary_record["monotonic_elapsed_seconds"], float)
        assert isinstance(summary_record["wall_elapsed_seconds"], float)
        assert summary_record["total_elapsed_seconds"] == pytest.approx(
            summary_record["monotonic_elapsed_seconds"], abs=0.01
        )
        assert isinstance(summary_record["wall_started_at"], str)
        assert isinstance(summary_record["wall_ended_at"], str)
        assert summary_record["host_name"]
        assert summary_record["host_memory_bytes"] is None or isinstance(
            summary_record["host_memory_bytes"], int
        )

    @pytest.mark.skipif(os.name != "posix", reason="asserts POSIX process cleanup")
    def test_natural_exit_contains_descendant_before_validation_returns(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        descendant_pid_path = (tmp_path / "natural-validation-descendant.pid").resolve()
        natural_leader = ExitingTermResistantProcessTreeProgram(
            descendant_pid_path,
            300,
            0,
        ).python_source()
        command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(natural_leader)}"

        validation_thread, validation_result = run_in_thread(
            run_validation,
            command,
            tmp_path / "output",
            fake_git_repo,
            clock=ValidationRunnerClock(
                lambda: datetime.now(timezone.utc),
                time.monotonic,
            ),
            contained_command_capture=build_contained_command_capture(),
            retained_thread_factory=build_retained_thread_factory(),
            host_probe=_NO_HOST_EVIDENCE_PROBE,
        )
        try:
            PROCESS_COMPLETION_WATCHDOG.join_thread(
                validation_thread,
                operation="natural-exit validation",
            )
        finally:
            if validation_thread.is_alive() and descendant_pid_path.exists():
                os.kill(
                    int(descendant_pid_path.read_text(encoding="utf-8")),
                    signal.SIGKILL,
                )
                PROCESS_COMPLETION_WATCHDOG.join_thread(
                    validation_thread,
                    operation="natural-exit validation cleanup",
                )
        descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
        assert validation_result.unwrap() == 0
        ProcessTreeMember(descendant_pid).assert_contained()

    def test_appends_resource_samples_to_shared_git_dir(self, fake_git_repo: Path):
        """Validate runs should persist periodic resource samples."""
        result = _run_validation_cli(
            [
                sys.executable,
                "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command",
                f'"{sys.executable}" -c "import time; time.sleep(0.5)"',
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env=_with_repo_on_pythonpath(
                {
                    k: v
                    for k, v in os.environ.items()
                    if not k.startswith("ISSUE_ORCHESTRATOR")
                }
            ),
        )

        assert result.returncode == 0
        timings_file = (
            fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
        )
        records = [json.loads(line) for line in timings_file.read_text().splitlines()]
        resource_records = [
            record for record in records if record["kind"] == "resource_sample"
        ]
        assert resource_records, "expected at least one resource_sample record"
        sample = resource_records[0]
        assert sample["worktree"] == str(fake_git_repo)
        assert "recorded_at" in sample
        assert sample["host_name"]
        assert isinstance(sample["host_cpu_count"], int)

    def test_wall_clock_rollback_cannot_corrupt_elapsed_duration(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        wall_start = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
        wall_rollback = datetime(2026, 8, 24, 11, tzinfo=timezone.utc)
        wall_calls = 0
        monotonic_calls = 0

        def wall_now() -> datetime:
            nonlocal wall_calls
            wall_calls += 1
            return wall_start if wall_calls == 1 else wall_rollback

        def monotonic_now() -> float:
            nonlocal monotonic_calls
            monotonic_calls += 1
            return 100.0 if monotonic_calls == 1 else 105.0

        result = run_validation(
            "true",
            tmp_path / "output",
            fake_git_repo,
            clock=ValidationRunnerClock(wall_now, monotonic_now),
            contained_command_capture=build_contained_command_capture(),
            retained_thread_factory=build_retained_thread_factory(),
            host_probe=_NO_HOST_EVIDENCE_PROBE,
        )

        assert result == 0
        records = [
            json.loads(line)
            for line in (
                fake_git_repo / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        summary = next(record for record in records if record["kind"] == "run_summary")
        assert summary["total_elapsed_seconds"] == 5.0
        assert summary["monotonic_elapsed_seconds"] == 5.0
        assert summary["wall_elapsed_seconds"] == -3600.0

    def test_end_clock_failures_do_not_skip_sampler_or_terminal_evidence(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        wall_failure = RuntimeError("injected wall-end clock failure")
        monotonic_failure = RuntimeError("injected monotonic-end clock failure")
        wall_started_at = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
        wall_calls = 0
        monotonic_calls = 0
        sampler_finalized = False

        def wall_now() -> datetime:
            nonlocal wall_calls
            wall_calls += 1
            if wall_calls == 1:
                return wall_started_at
            raise wall_failure

        def monotonic_now() -> float:
            nonlocal monotonic_calls
            monotonic_calls += 1
            if monotonic_calls == 1:
                return 100.0
            raise monotonic_failure

        def record_sampler_finalization() -> None:
            nonlocal sampler_finalized
            sampler_finalized = True

        output_dir = (tmp_path / "clock-failure-output").resolve()
        with pytest.raises(BaseExceptionGroup) as caught:
            run_validation(
                "true",
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(wall_now, monotonic_now),
                contained_command_capture=_StaticContainedCommandCapture(
                    ContainedCommandCompleted(
                        ContainedCommandExited(42_424, 0),
                        ContainedCommandMetrics(2, 20),
                    )
                ),
                retained_thread_factory=_FinalizationActionRetainedThreadFactory(
                    record_sampler_finalization
                ),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        assert caught.value.exceptions == (wall_failure, monotonic_failure)
        assert wall_calls == 2
        assert monotonic_calls == 2
        assert sampler_finalized
        output = (output_dir / "validation-output.log").read_text(encoding="utf-8")
        assert "[validate_runner] child_exited pid=42424" in output
        assert "child_exit_code=0" in output
        assert "lifecycle=finalization-failed" in output
        assert "elapsed=unavailable" in output

    @pytest.mark.parametrize(
        "invalid_end",
        (float("nan"), float("inf"), float("-inf")),
        ids=("nan", "positive-infinity", "negative-infinity"),
    )
    def test_non_finite_end_monotonic_is_typed_unavailable(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
        invalid_end: float,
    ) -> None:
        monotonic_calls = 0

        def monotonic_now() -> float:
            nonlocal monotonic_calls
            monotonic_calls += 1
            return 100.0 if monotonic_calls == 1 else invalid_end

        output_dir = (tmp_path / "non-finite-end-output").resolve()
        with pytest.raises(
            ValueError,
            match="validation monotonic end must be finite and non-negative",
        ):
            run_validation(
                "true",
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(
                    lambda: datetime.now(timezone.utc),
                    monotonic_now,
                ),
                contained_command_capture=_StaticContainedCommandCapture(
                    ContainedCommandCompleted(
                        ContainedCommandExited(42_424, 0),
                        ContainedCommandMetrics(2, 20),
                    )
                ),
                retained_thread_factory=_FinalizationActionRetainedThreadFactory(
                    lambda: None
                ),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        assert monotonic_calls == 2
        output = (output_dir / "validation-output.log").read_text(encoding="utf-8")
        assert "[validate_runner] child_exited pid=42424" in output
        assert "lifecycle=finalization-failed" in output
        assert "elapsed=unavailable" in output

    def test_naive_end_wall_clock_is_typed_unavailable(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
    ) -> None:
        wall_calls = 0

        def wall_now() -> datetime:
            nonlocal wall_calls
            wall_calls += 1
            if wall_calls == 1:
                return datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
            return datetime(2026, 8, 24, 12)

        output_dir = (tmp_path / "naive-end-output").resolve()
        with pytest.raises(
            ValueError,
            match="validation wall end must be timezone-aware",
        ):
            run_validation(
                "true",
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(wall_now, lambda: 105.0),
                contained_command_capture=_StaticContainedCommandCapture(
                    ContainedCommandCompleted(
                        ContainedCommandExited(42_424, 0),
                        ContainedCommandMetrics(2, 20),
                    )
                ),
                retained_thread_factory=_FinalizationActionRetainedThreadFactory(
                    lambda: None
                ),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        assert wall_calls == 2
        output = (output_dir / "validation-output.log").read_text(encoding="utf-8")
        assert "[validate_runner] child_exited pid=42424" in output
        assert "lifecycle=finalization-failed" in output
        assert "elapsed=unavailable" in output

    @pytest.mark.parametrize(
        ("wall_start", "monotonic_start", "expected_message"),
        (
            (
                datetime(2026, 8, 24, 12),
                100.0,
                "validation wall start must be timezone-aware",
            ),
            (
                datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
                float("nan"),
                "validation monotonic start must be finite and non-negative",
            ),
            (
                datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
                float("inf"),
                "validation monotonic start must be finite and non-negative",
            ),
            (
                datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
                -1.0,
                "validation monotonic start must be finite and non-negative",
            ),
        ),
        ids=("naive-wall", "nan-monotonic", "infinite-monotonic", "negative-monotonic"),
    )
    def test_invalid_start_clock_fails_before_sampler_or_command_activation(
        self,
        fake_git_repo: Path,
        tmp_path: Path,
        wall_start: datetime,
        monotonic_start: float,
        expected_message: str,
    ) -> None:
        activation_failure = RuntimeError("sampler must not activate")
        capture_failure = RuntimeError("command must not capture")
        output_dir = (tmp_path / "invalid-start-output").resolve()

        with pytest.raises(ValueError, match=expected_message):
            run_validation(
                "true",
                output_dir,
                fake_git_repo,
                clock=ValidationRunnerClock(
                    lambda: wall_start,
                    lambda: monotonic_start,
                ),
                contained_command_capture=_RaisingContainedCommandCapture(
                    capture_failure
                ),
                retained_thread_factory=_ActivationRaisingRetainedThreadFactory(
                    activation_failure,
                    lambda: None,
                ),
                host_probe=_NO_HOST_EVIDENCE_PROBE,
            )

        assert not (output_dir / "validation-output.log").exists()

    def test_default_run_ids_are_unique_within_the_same_second(
        self,
        fake_git_repo: Path,
    ) -> None:
        first = ValidateTimingRecorder(fake_git_repo, "first")
        second = ValidateTimingRecorder(fake_git_repo, "second")

        assert first.run_id != second.run_id
        assert "-pid" in first.run_id


class TestReadHeadSha:
    """R9 (#6824): the E2E head reader resolves HEAD via file reads, no subprocess."""

    def test_reads_sha_from_loose_ref(self, tmp_path: Path) -> None:
        from issue_orchestrator.infra.validation_timings import read_head_sha

        git = tmp_path / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "refs" / "heads" / "main").write_text("a" * 40 + "\n")

        assert read_head_sha(tmp_path) == "a" * 40

    def test_reads_detached_head_sha_directly(self, tmp_path: Path) -> None:
        from issue_orchestrator.infra.validation_timings import read_head_sha

        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("b" * 40 + "\n")

        assert read_head_sha(tmp_path) == "b" * 40

    def test_reads_sha_from_packed_refs(self, tmp_path: Path) -> None:
        from issue_orchestrator.infra.validation_timings import read_head_sha

        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            + "c" * 40
            + " refs/heads/main\n"
        )

        assert read_head_sha(tmp_path) == "c" * 40

    def test_no_subprocess_import_in_e2e_slot_policy(self) -> None:
        # The infra boundary: e2e_slot_policy must not shell out (infra/AGENTS.md).
        import issue_orchestrator.infra.e2e_slot_policy as mod

        assert not hasattr(mod, "subprocess")
