"""Small lifecycle-owning DSL for cross-process executor-pool behavior tests."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import TextIO, cast

from tests.process_completion_fixture import (
    GuardianPidFile,
    PROCESS_COMPLETION_WATCHDOG,
    ProcessCleanupPlan,
    ProcessCleanupStep,
)
from tests.process_tree_fixture import ProcessTreeMember


POOL_DIR_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"
HOST_CPU_BUSY_FILE_ENV = "ISSUE_ORCHESTRATOR_TEST_HOST_CPU_BUSY_FILE"
PROCESS_RUNNER = Path(__file__).with_name("executor_process_runner.py")


@dataclass(frozen=True, slots=True)
class ExecutorPoolRawCommand:
    """Opaque command whose output and exit status are asserted after completion."""

    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_arguments(self.arguments)


@dataclass(frozen=True, slots=True)
class ExecutorPoolPrintCommand:
    """One-shot command with a deterministic stdout readiness line."""

    label: str

    def __post_init__(self) -> None:
        _require_label(self.label)

    def arguments(self) -> tuple[str, ...]:
        return (
            sys.executable,
            "-u",
            "-c",
            f"print({self.label!r}, flush=True)",
        )


@dataclass(frozen=True, slots=True)
class ExecutorPoolHeldCommand:
    """Command that starts explicitly and exits only after one stdin line."""

    label: str
    exit_code: int

    def __post_init__(self) -> None:
        _require_label(self.label)
        if type(self.exit_code) is not int or not 0 <= self.exit_code <= 255:
            raise ValueError("ExecutorPoolHeldCommand.exit_code must be in [0, 255]")

    def arguments(self) -> tuple[str, ...]:
        return (
            sys.executable,
            "-u",
            "-c",
            f"import sys; print({self.label!r}, flush=True); "
            f"sys.stdin.readline(); raise SystemExit({self.exit_code})",
        )


@dataclass(frozen=True, slots=True)
class ExecutorPoolHungCommand:
    """TERM-resistant command used to prove forced scenario containment."""

    label: str
    command_pid_path: Path

    def __post_init__(self) -> None:
        _require_label(self.label)
        if (
            not isinstance(self.command_pid_path, Path)
            or not self.command_pid_path.is_absolute()
        ):
            raise ValueError(
                "ExecutorPoolHungCommand.command_pid_path must be absolute"
            )

    def arguments(self) -> tuple[str, ...]:
        source = (
            "import os, pathlib, signal\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"pathlib.Path({str(self.command_pid_path)!r}).write_text("
            "str(os.getpid()), encoding='utf-8')\n"
            f"print({self.label!r}, flush=True)\n"
            "signal.pause()\n"
        )
        return (sys.executable, "-u", "-c", source)

    def require_command_contained(self) -> None:
        if not self.command_pid_path.exists():
            raise AssertionError(
                f"hung command identity was not recorded at {self.command_pid_path}"
            )
        command_pid = int(self.command_pid_path.read_text(encoding="utf-8"))
        if command_pid <= 1:
            raise AssertionError(f"invalid recorded hung command pid {command_pid}")
        ProcessTreeMember(command_pid).assert_contained()


ExecutorPoolCommand = (
    ExecutorPoolRawCommand
    | ExecutorPoolPrintCommand
    | ExecutorPoolHeldCommand
    | ExecutorPoolHungCommand
)


@dataclass(frozen=True, slots=True)
class ExecutorPoolWork:
    """One exact executor request used by a pool behavior scenario."""

    work_key: str
    fairness_group: str
    requested_concurrency: int
    host_cpu_slots: int
    exclusive_resources: tuple[str, ...]
    command: ExecutorPoolCommand

    def __post_init__(self) -> None:
        for field_name, value in (
            ("work_key", self.work_key),
            ("fairness_group", self.fairness_group),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"ExecutorPoolWork.{field_name} must not be empty")
        for field_name, value in (
            ("requested_concurrency", self.requested_concurrency),
            ("host_cpu_slots", self.host_cpu_slots),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"ExecutorPoolWork.{field_name} must be positive")
        if type(self.exclusive_resources) is not tuple or any(
            type(resource) is not str or not resource
            for resource in self.exclusive_resources
        ):
            raise ValueError(
                "ExecutorPoolWork.exclusive_resources must contain non-empty strings"
            )
        if type(self.command) not in (
            ExecutorPoolRawCommand,
            ExecutorPoolPrintCommand,
            ExecutorPoolHeldCommand,
            ExecutorPoolHungCommand,
        ):
            raise ValueError("ExecutorPoolWork.command must be a typed pool command")

    def command_line(self, guardian_pid_file: GuardianPidFile) -> tuple[str, ...]:
        if type(guardian_pid_file) is not GuardianPidFile:
            raise ValueError("ExecutorPoolWork.command_line requires a GuardianPidFile")
        command = _command_arguments(self.command)
        arguments = [
            sys.executable,
            str(PROCESS_RUNNER),
            "--host-cpu-slots",
            str(self.host_cpu_slots),
            "--min-concurrency",
            str(self.requested_concurrency),
            "--max-concurrency",
            str(self.requested_concurrency),
            "--work-key",
            self.work_key,
            "--group",
            self.fairness_group,
        ]
        for resource in self.exclusive_resources:
            arguments.extend(("--exclusive", resource))
        return (
            *arguments,
            "--",
            *guardian_pid_file.recording_arguments(command),
        )


@dataclass(frozen=True, slots=True)
class ExecutorPoolRunResult:
    """Captured terminal result from one executor-pool request."""

    exit_code: int
    stdout: str
    stderr: str

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError("ExecutorPoolRunResult.exit_code must be an integer")
        if type(self.stdout) is not str or type(self.stderr) is not str:
            raise ValueError("ExecutorPoolRunResult output must be text")


@dataclass(frozen=True, slots=True)
class ExecutorPoolJob:
    """Opaque handle naming one process owned by an ExecutorPoolRig."""

    sequence: int
    work: ExecutorPoolWork

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("ExecutorPoolJob.sequence must be positive")
        if type(self.work) is not ExecutorPoolWork:
            raise ValueError("ExecutorPoolJob.work must be ExecutorPoolWork")


class _ControlledExecutorPoolProcess:
    """Own one outer runner, its pipes, and its detached guardian identity."""

    def __init__(
        self,
        work: ExecutorPoolWork,
        guardian_pid_file: GuardianPidFile,
        process: subprocess.Popen[str],
    ) -> None:
        if type(work) is not ExecutorPoolWork:
            raise ValueError("controlled executor process requires ExecutorPoolWork")
        if type(guardian_pid_file) is not GuardianPidFile:
            raise ValueError("controlled executor process requires GuardianPidFile")
        if not isinstance(process, subprocess.Popen):
            raise ValueError("controlled executor process requires subprocess.Popen")
        if process.stdout is None or process.stderr is None:
            raise ValueError("controlled executor process requires captured output")
        if type(work.command) is ExecutorPoolHeldCommand and process.stdin is None:
            raise ValueError("held executor process requires a stdin release pipe")
        self._work = work
        self._guardian_pid_file = guardian_pid_file
        self._process = process
        self._completion_observed = False
        self._release_signalled = False

    def wait_until_queued(self, expected_reason: str) -> None:
        _require_label(expected_reason)
        line = self._readline(self._stderr(), transition="enter the executor queue")
        assert expected_reason in line, self._unexpected_line(
            transition="enter the expected executor queue",
            line=line,
        )

    def wait_until_started(self) -> None:
        command = self._work.command
        if not isinstance(
            command,
            (
                ExecutorPoolPrintCommand,
                ExecutorPoolHeldCommand,
                ExecutorPoolHungCommand,
            ),
        ):
            raise ValueError("raw executor commands have no start-line contract")
        line = self._readline(self._stdout(), transition="start")
        assert line == f"{command.label}\n", self._unexpected_line(
            transition="publish its exact start line",
            line=line,
        )

    def assert_not_started(self) -> None:
        with selectors.DefaultSelector() as selector:
            selector.register(self._stdout(), selectors.EVENT_READ)
            assert selector.select(timeout=0) == [], (
                f"{self._work.work_key} published stdout before admission"
            )

    def release_and_require_success(self) -> None:
        if type(self._work.command) is not ExecutorPoolHeldCommand:
            raise ValueError("only held executor commands accept release")
        self.signal_release()
        result = self.complete()
        assert result.exit_code == 0, self._unexpected_exit(result)

    def complete_and_require_success(self) -> ExecutorPoolRunResult:
        result = self.complete()
        assert result.exit_code == 0, self._unexpected_exit(result)
        return result

    def complete(self) -> ExecutorPoolRunResult:
        if self._completion_observed:
            raise RuntimeError("executor process completion was already observed")
        stdout, stderr = PROCESS_COMPLETION_WATCHDOG.communicate(
            self._process,
            operation=f"executor work {self._work.work_key}",
        )
        self._completion_observed = True
        return ExecutorPoolRunResult(self._process.returncode, stdout, stderr)

    def signal_release(self) -> None:
        if type(self._work.command) is not ExecutorPoolHeldCommand:
            return
        if self._release_signalled or self._process.poll() is not None:
            return
        stdin = self._stdin()
        stdin.write("\n")
        stdin.flush()
        self._release_signalled = True

    def signal_cleanup_release(self) -> None:
        if type(self._work.command) is not ExecutorPoolHeldCommand:
            return
        if self._release_signalled or self._process.poll() is not None:
            return
        try:
            self.signal_release()
        except BrokenPipeError:
            if self._process.poll() is None:
                raise

    def require_guardian_contained(self) -> None:
        self._guardian_pid_file.require_contained()

    def require_cleanup_complete(self) -> None:
        self.require_guardian_contained()
        assert self._process.poll() is not None, (
            f"executor outer process {self._process.pid} remains live"
        )
        for name, stream in (
            ("stdin", self._process.stdin),
            ("stdout", self._process.stdout),
            ("stderr", self._process.stderr),
        ):
            if stream is not None:
                assert stream.closed, f"executor {name} remained open after cleanup"

    @property
    def work_key(self) -> str:
        return self._work.work_key

    def cleanup(self, *, abort: bool) -> None:
        if type(abort) is not bool:
            raise ValueError("controlled cleanup requires an exact abort policy")
        if abort:
            self._abort_cleanup_plan().execute()
            return
        if self._completion_observed:
            self._close_cleanup_plan().execute()
            return
        try:
            result = self.complete()
        except BaseException as error:
            self._abort_cleanup_plan().execute(preceding_error=error)
            raise AssertionError("abort cleanup must raise")
        if result.exit_code != 0:
            self._close_cleanup_plan().execute(
                preceding_error=AssertionError(self._unexpected_exit(result))
            )
            raise AssertionError("failed completion cleanup must raise")
        self._close_cleanup_plan().execute()

    def _abort_cleanup_plan(self) -> ProcessCleanupPlan:
        return ProcessCleanupPlan(
            operation=f"abort executor work {self._work.work_key}",
            steps=(
                ProcessCleanupStep(
                    "contain executor guardian before outer termination",
                    self._guardian_pid_file.contain_if_recorded,
                ),
                ProcessCleanupStep(
                    "kill executor outer process group",
                    self._kill_outer_process_group_if_running,
                ),
                ProcessCleanupStep(
                    "contain executor guardian after outer termination",
                    self._guardian_pid_file.contain_if_recorded,
                ),
                ProcessCleanupStep(
                    "reap executor outer process",
                    self._reap_outer_process_if_running,
                ),
                *self._stream_cleanup_steps(),
            ),
        )

    def _close_cleanup_plan(self) -> ProcessCleanupPlan:
        return ProcessCleanupPlan(
            operation=f"close executor work {self._work.work_key}",
            steps=self._stream_cleanup_steps(),
        )

    def _kill_outer_process_group_if_running(self) -> None:
        if self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _reap_outer_process_if_running(self) -> None:
        if self._process.poll() is None:
            PROCESS_COMPLETION_WATCHDOG.wait(
                self._process,
                operation=f"reap aborted executor work {self._work.work_key}",
            )

    def _readline(self, stream: TextIO, *, transition: str) -> str:
        line = stream.readline()
        assert line, self._unexpected_line(transition=transition, line=line)
        return line

    def _unexpected_line(self, *, transition: str, line: str) -> str:
        return (
            f"{self._work.work_key} did not {transition}: line={line!r} "
            f"returncode={self._process.poll()!r}"
        )

    def _unexpected_exit(self, result: ExecutorPoolRunResult) -> str:
        return (
            f"{self._work.work_key} exited {result.exit_code}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def _stdin(self) -> TextIO:
        if self._process.stdin is None:
            raise RuntimeError("held executor process stdin was not configured")
        return cast(TextIO, self._process.stdin)

    def _stdout(self) -> TextIO:
        return cast(TextIO, self._process.stdout)

    def _stderr(self) -> TextIO:
        return cast(TextIO, self._process.stderr)

    def _stream_cleanup_steps(self) -> tuple[ProcessCleanupStep, ...]:
        return (
            ProcessCleanupStep("close executor stdin", self._close_stdin),
            ProcessCleanupStep("close executor stdout", self._close_stdout),
            ProcessCleanupStep("close executor stderr", self._close_stderr),
        )

    def _close_stdin(self) -> None:
        self._close_stream(cast(TextIO | None, self._process.stdin))

    def _close_stdout(self) -> None:
        self._close_stream(cast(TextIO | None, self._process.stdout))

    def _close_stderr(self) -> None:
        self._close_stream(cast(TextIO | None, self._process.stderr))

    @staticmethod
    def _close_stream(stream: TextIO | None) -> None:
        if stream is not None and not stream.closed:
            stream.close()


class ExecutorPoolRig:
    """Run pool requests while owning every subprocess lifecycle and cleanup."""

    def __init__(self, pool_dir: Path, *, working_directory: Path) -> None:
        for field_name, path in (
            ("pool_dir", pool_dir),
            ("working_directory", working_directory),
        ):
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError(f"ExecutorPoolRig.{field_name} must be absolute")
        self._pool_dir = pool_dir
        self._working_directory = working_directory
        self._busy_file = pool_dir / "test-host-cpu-busy-percent"
        self._busy_file.parent.mkdir(parents=True, exist_ok=True)
        self._busy_file.write_text("0\n", encoding="utf-8")
        self._processes: dict[ExecutorPoolJob, _ControlledExecutorPoolProcess] = {}
        self._next_sequence = 1

    def __enter__(self) -> ExecutorPoolRig:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, traceback
        processes = tuple(self._processes.values())
        if not processes:
            return
        ProcessCleanupPlan(
            operation="clean up executor-pool scenario",
            steps=(
                *(
                    ProcessCleanupStep(
                        f"signal cleanup release for {controlled.work_key}",
                        controlled.signal_cleanup_release,
                    )
                    for controlled in processes
                ),
                *(
                    ProcessCleanupStep(
                        f"clean up executor work {controlled.work_key}",
                        partial(controlled.cleanup, abort=exception is not None),
                    )
                    for controlled in reversed(processes)
                ),
            ),
        ).execute(preceding_error=exception)

    def run(self, work: ExecutorPoolWork) -> ExecutorPoolRunResult:
        """Run one request to completion in the rig's repository directory."""
        job = self.submit(work)
        return self._process(job).complete()

    def run_in(
        self,
        work: ExecutorPoolWork,
        *,
        working_directory: Path,
    ) -> ExecutorPoolRunResult:
        """Run one request in an explicit alternate working directory."""
        job = self._submit_in(work, working_directory=working_directory)
        return self._process(job).complete()

    def admit(self, work: ExecutorPoolWork) -> ExecutorPoolJob:
        """Submit work and require its command to publish readiness."""
        job = self.submit(work)
        self.require_started(job)
        return job

    def defer(
        self,
        work: ExecutorPoolWork,
        *,
        expected_reason: str,
    ) -> ExecutorPoolJob:
        """Submit work and require an executor-queue observation."""
        job = self.submit(work)
        self._process(job).wait_until_queued(expected_reason)
        return job

    def submit(self, work: ExecutorPoolWork) -> ExecutorPoolJob:
        """Submit work without assuming its immediate admission outcome."""
        return self._submit_in(work, working_directory=self._working_directory)

    def require_started(self, job: ExecutorPoolJob) -> None:
        self._process(job).wait_until_started()

    def require_not_started(self, job: ExecutorPoolJob) -> None:
        self._process(job).assert_not_started()

    def release(self, job: ExecutorPoolJob) -> None:
        self._process(job).release_and_require_success()

    def complete(self, job: ExecutorPoolJob) -> ExecutorPoolRunResult:
        return self._process(job).complete_and_require_success()

    def require_guardian_contained(self, job: ExecutorPoolJob) -> None:
        """Require one explicitly admitted job's guardian to be contained."""
        self._process(job).require_guardian_contained()

    def require_cleanup_complete(self, job: ExecutorPoolJob) -> None:
        """Require one admitted job's process and owned resources to be clean."""
        self._process(job).require_cleanup_complete()

    def _submit_in(
        self,
        work: ExecutorPoolWork,
        *,
        working_directory: Path,
    ) -> ExecutorPoolJob:
        if type(work) is not ExecutorPoolWork:
            raise ValueError("ExecutorPoolRig requires ExecutorPoolWork")
        if (
            not isinstance(working_directory, Path)
            or not working_directory.is_absolute()
        ):
            raise ValueError("executor-pool working directory must be absolute")
        job = ExecutorPoolJob(self._next_sequence, work)
        self._next_sequence += 1
        guardian_pid_file = GuardianPidFile(
            (self._pool_dir / "fixture-guardians" / f"{job.sequence}.pid").resolve()
        )
        guardian_pid_file.path.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            work.command_line(guardian_pid_file),
            cwd=working_directory,
            stdin=(
                subprocess.PIPE
                if type(work.command) is ExecutorPoolHeldCommand
                else subprocess.DEVNULL
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._environment(),
            start_new_session=True,
        )
        self._processes[job] = _ControlledExecutorPoolProcess(
            work,
            guardian_pid_file,
            process,
        )
        return job

    def _process(self, job: ExecutorPoolJob) -> _ControlledExecutorPoolProcess:
        if type(job) is not ExecutorPoolJob:
            raise ValueError("ExecutorPoolRig requires ExecutorPoolJob")
        try:
            return self._processes[job]
        except KeyError as error:
            raise ValueError("ExecutorPoolJob does not belong to this rig") from error

    def _environment(self) -> dict[str, str]:
        return {
            **os.environ,
            POOL_DIR_ENV: str(self._pool_dir),
            HOST_CPU_BUSY_FILE_ENV: str(self._busy_file),
        }


def _require_arguments(arguments: tuple[str, ...]) -> None:
    if type(arguments) is not tuple or not arguments:
        raise ValueError("executor-pool command arguments must not be empty")
    if any(type(argument) is not str or "\0" in argument for argument in arguments):
        raise ValueError(
            "executor-pool command arguments must contain strings without NUL bytes"
        )


def _command_arguments(command: ExecutorPoolCommand) -> tuple[str, ...]:
    if isinstance(command, ExecutorPoolRawCommand):
        return command.arguments
    if isinstance(
        command,
        (
            ExecutorPoolPrintCommand,
            ExecutorPoolHeldCommand,
            ExecutorPoolHungCommand,
        ),
    ):
        return command.arguments()
    raise AssertionError("ExecutorPoolCommand is a closed union")


def _require_label(label: str) -> None:
    if type(label) is not str or not label or "\n" in label or "\r" in label:
        raise ValueError("executor-pool labels must be non-empty single lines")
