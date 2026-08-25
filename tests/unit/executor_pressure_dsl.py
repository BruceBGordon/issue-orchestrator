"""Small scenario DSL for real cross-process host-executor pressure tests."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import TextIO, cast

from issue_orchestrator.domain.executor import ExecutorConcurrencyRange
from tests.process_completion_fixture import (
    GuardianPidFile,
    PROCESS_COMPLETION_WATCHDOG,
    ProcessCleanupPlan,
    ProcessCleanupStep,
)
from tests.process_tree_fixture import (
    DirectChildProcessCohort,
    ProcessTreeMember,
    TermResistantChildProgram,
)


POOL_DIR_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"
HOST_CPU_BUSY_FILE_ENV = "ISSUE_ORCHESTRATOR_TEST_HOST_CPU_BUSY_FILE"
ADMISSION_ATTEMPT_FD_ENV = "ISSUE_ORCHESTRATOR_TEST_ADMISSION_ATTEMPT_FD"
PROCESS_RUNNER = Path(__file__).with_name("executor_process_runner.py")
REQUIRED_POST_HOLDER_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class HeldPressureCommand:
    """A coarse-grained command released through its inherited stdin pipe."""

    def arguments(self, label: str) -> tuple[str, ...]:
        return (
            sys.executable,
            "-u",
            "-c",
            f"import sys; print({label!r}, flush=True); sys.stdin.readline()",
        )

    def cleanup(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class CloseFdsTreePressureCommand:
    """A releasable leader with one TERM-resistant close-fds descendant."""

    guardian_pid_path: Path
    descendant_pid_path: Path

    def __post_init__(self) -> None:
        for field_name, value in (
            ("guardian_pid_path", self.guardian_pid_path),
            ("descendant_pid_path", self.descendant_pid_path),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(
                    f"CloseFdsTreePressureCommand.{field_name} must be absolute"
                )

    def arguments(self, label: str) -> tuple[str, ...]:
        descendant_source = TermResistantChildProgram(300).python_source()
        source = (
            "import os, pathlib, signal, subprocess, sys\n"
            "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})\n"
            "descendant = subprocess.Popen(\n"
            f"    [sys.executable, '-c', {descendant_source!r}],\n"
            "    close_fds=True,\n"
            "    stdin=subprocess.DEVNULL,\n"
            "    stdout=subprocess.PIPE,\n"
            "    stderr=subprocess.DEVNULL,\n"
            "    text=True,\n"
            ")\n"
            "if descendant.stdout is None:\n"
            "    raise RuntimeError('descendant readiness pipe was not created')\n"
            "reported_pid = int(descendant.stdout.readline())\n"
            "if reported_pid != descendant.pid:\n"
            "    raise RuntimeError('descendant readiness identity mismatch')\n"
            f"pathlib.Path({str(self.guardian_pid_path)!r}).write_text("
            "str(os.getppid()))\n"
            f"pathlib.Path({str(self.descendant_pid_path)!r}).write_text("
            "str(reported_pid))\n"
            "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})\n"
            f"print({label!r}, flush=True)\n"
            "sys.stdin.readline()\n"
        )
        return (sys.executable, "-u", "-c", source)

    def require_descendant_contained(self) -> None:
        ProcessTreeMember(
            _recorded_process_id(self.descendant_pid_path)
        ).assert_contained()

    def crash_guardian(self) -> None:
        """Hard-kill the guardian so only its independent sentinel remains."""
        os.kill(_recorded_process_id(self.guardian_pid_path), signal.SIGKILL)

    def crash_sentinel(self) -> None:
        """Hard-kill the guardian's exact sentinel child."""
        guardian_pid = _recorded_process_id(self.guardian_pid_path)
        DirectChildProcessCohort.observe_exact(
            parent_process_id=guardian_pid,
            module_name="issue_orchestrator.execution.process_group_sentinel",
            expected_count=1,
        ).crash_one()

    def require_descendant_executable(self) -> None:
        descendant = ProcessTreeMember(
            _recorded_process_id(self.descendant_pid_path)
        )
        if not descendant.is_executable():
            raise AssertionError(
                "pressure descendant stopped before containment was requested"
            )

    def cleanup(self) -> None:
        _contain_recorded_guardian(self.guardian_pid_path)


@dataclass(frozen=True, slots=True)
class HungPressureCommand:
    """A TERM-resistant leader used to prove guardian-owned deadlines."""

    guardian_pid_path: Path
    command_pid_path: Path

    def __post_init__(self) -> None:
        for field_name, value in (
            ("guardian_pid_path", self.guardian_pid_path),
            ("command_pid_path", self.command_pid_path),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"HungPressureCommand.{field_name} must be absolute")

    def arguments(self, label: str) -> tuple[str, ...]:
        source = (
            "import os, pathlib, signal\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"pathlib.Path({str(self.guardian_pid_path)!r}).write_text("
            "str(os.getppid()))\n"
            f"pathlib.Path({str(self.command_pid_path)!r}).write_text("
            "str(os.getpid()))\n"
            f"print({label!r}, flush=True)\n"
            "signal.pause()\n"
        )
        return (sys.executable, "-u", "-c", source)

    def require_command_contained(self) -> None:
        ProcessTreeMember(
            _recorded_process_id(self.command_pid_path)
        ).assert_contained()

    def cleanup(self) -> None:
        _contain_recorded_guardian(self.guardian_pid_path)


PressureCommand = (
    HeldPressureCommand | CloseFdsTreePressureCommand | HungPressureCommand
)


@dataclass(frozen=True, slots=True)
class UnboundedPressureDeadline:
    """Explicitly permit a pressure command to run until natural completion."""


@dataclass(frozen=True, slots=True)
class BoundedPressureDeadline:
    """Exact active and absolute bounds forwarded through the process runner."""

    active_timeout_seconds: float
    absolute_timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.active_timeout_seconds) is not float
            or self.active_timeout_seconds <= 0.0
        ):
            raise ValueError("pressure active timeout must be positive")
        if (
            type(self.absolute_timeout_seconds) is not float
            or self.absolute_timeout_seconds < self.active_timeout_seconds
        ):
            raise ValueError(
                "pressure absolute timeout must be at least the active timeout"
            )


PressureDeadline = UnboundedPressureDeadline | BoundedPressureDeadline


def _recorded_process_id(pid_path: Path) -> int:
    process_id = int(pid_path.read_text(encoding="utf-8"))
    if process_id <= 1:
        raise AssertionError(f"invalid recorded process id {process_id}")
    return process_id


def _contain_recorded_guardian(pid_path: Path) -> None:
    if not pid_path.exists():
        return
    guardian_pid = _recorded_process_id(pid_path)
    try:
        process_group_id = os.getpgid(guardian_pid)
    except ProcessLookupError:
        return
    if process_group_id != guardian_pid:
        raise AssertionError(
            f"recorded guardian {guardian_pid} does not own its process group"
        )
    os.killpg(process_group_id, signal.SIGKILL)


@dataclass(frozen=True, slots=True)
class PressureWork:
    """One explicitly controlled command submitted by a pressure scenario."""

    label: str
    group: str
    concurrency_range: ExecutorConcurrencyRange = ExecutorConcurrencyRange(1, 1)
    exclusive_resources: tuple[str, ...] = ()
    command: PressureCommand = HeldPressureCommand()
    deadline: PressureDeadline = UnboundedPressureDeadline()

    def __post_init__(self) -> None:
        if type(self.label) is not str or not self.label:
            raise ValueError("PressureWork.label must not be empty")
        if type(self.group) is not str or not self.group:
            raise ValueError("PressureWork.group must not be empty")
        if type(self.concurrency_range) is not ExecutorConcurrencyRange:
            raise ValueError(
                "PressureWork.concurrency_range must be an ExecutorConcurrencyRange"
            )
        if type(self.exclusive_resources) is not tuple or any(
            type(resource) is not str or not resource
            for resource in self.exclusive_resources
        ):
            raise ValueError(
                "PressureWork.exclusive_resources must contain non-empty strings"
            )
        if type(self.command) not in (
            HeldPressureCommand,
            CloseFdsTreePressureCommand,
            HungPressureCommand,
        ):
            raise ValueError("PressureWork.command must be a typed pressure command")
        if type(self.deadline) not in (
            UnboundedPressureDeadline,
            BoundedPressureDeadline,
        ):
            raise ValueError("PressureWork.deadline must be a typed pressure deadline")

    def command_line(
        self,
        host_cpu_slots: int,
        guardian_pid_file: GuardianPidFile,
    ) -> list[str]:
        if type(guardian_pid_file) is not GuardianPidFile:
            raise ValueError("PressureWork.command_line requires GuardianPidFile")
        command = [
            sys.executable,
            str(PROCESS_RUNNER),
            "--host-cpu-slots",
            str(host_cpu_slots),
            "--min-concurrency",
            str(self.concurrency_range.minimum_concurrency),
            "--max-concurrency",
            str(self.concurrency_range.maximum_concurrency),
            "--work-key",
            "pressure:shared-work",
            "--group",
            self.group,
        ]
        for resource in self.exclusive_resources:
            command.extend(("--exclusive", resource))
        if type(self.deadline) is BoundedPressureDeadline:
            command.extend(
                (
                    "--active-timeout-seconds",
                    str(self.deadline.active_timeout_seconds),
                    "--absolute-timeout-seconds",
                    str(self.deadline.absolute_timeout_seconds),
                )
            )
        return [
            *command,
            "--",
            *guardian_pid_file.recording_arguments(self.command.arguments(self.label)),
        ]


@dataclass(frozen=True, slots=True)
class PressureJob:
    """Opaque handle through which a scenario names submitted pressure work."""

    sequence: int
    work: PressureWork

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("PressureJob.sequence must be positive")
        if type(self.work) is not PressureWork:
            raise ValueError("PressureJob.work must be PressureWork")


class _PressureSignalKind(Enum):
    STARTED = "started"
    ADMISSION_ATTEMPTED = "admission_attempted"


@dataclass(frozen=True, slots=True)
class _PressureSignal:
    """One typed selector registration used by a negative handshake."""

    job: PressureJob
    kind: _PressureSignalKind


class _ControlledPressureProcess:
    """Own subprocess mechanics hidden behind the pressure scenario DSL."""

    def __init__(
        self,
        pool_dir: Path,
        work: PressureWork,
        *,
        sequence: int,
        host_cpu_slots: int,
        host_cpu_busy_file: Path,
    ) -> None:
        if type(sequence) is not int or sequence < 1:
            raise ValueError("controlled pressure process sequence must be positive")
        self.work = work
        self._guardian_pid_file = GuardianPidFile(
            (pool_dir / "fixture-guardians" / f"pressure-{sequence}.pid").resolve()
        )
        self._guardian_pid_file.path.parent.mkdir(parents=True, exist_ok=True)
        self._release_signalled = False
        self._completion_observed = False
        self._command_cleanup_attempted = False
        self._admission_attempt_fd_open = True
        attempt_read_fd, attempt_write_fd = os.pipe()
        os.set_blocking(attempt_read_fd, False)
        try:
            self._process: subprocess.Popen[str] = subprocess.Popen(
                work.command_line(host_cpu_slots, self._guardian_pid_file),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={
                    **os.environ,
                    POOL_DIR_ENV: str(pool_dir),
                    HOST_CPU_BUSY_FILE_ENV: str(host_cpu_busy_file),
                    ADMISSION_ATTEMPT_FD_ENV: str(attempt_write_fd),
                },
                pass_fds=(attempt_write_fd,),
                start_new_session=True,
            )
        except BaseException:
            os.close(attempt_read_fd)
            raise
        finally:
            os.close(attempt_write_fd)
        self._admission_attempt_fd = attempt_read_fd

    def wait_until_queued(self) -> None:
        line = self._readline(
            self._stderr(),
            transition="enter the executor queue",
        )
        assert "[executor] waiting" in line, self._unexpected_line(
            transition="enter the executor queue",
            line=line,
        )

    def wait_until_started(self) -> None:
        line = self._readline(
            self._stdout(),
            transition="start",
        )
        self._require_started_line(line)

    def consume_started_signal(self) -> None:
        self._require_started_line(self._stdout().readline())

    def register_start_signal(
        self,
        selector: selectors.BaseSelector,
        job: PressureJob,
    ) -> None:
        selector.register(self._stdout(), selectors.EVENT_READ, job)

    def register_pressure_signals(
        self,
        selector: selectors.BaseSelector,
        job: PressureJob,
    ) -> None:
        selector.register(
            self._stdout(),
            selectors.EVENT_READ,
            _PressureSignal(job, _PressureSignalKind.STARTED),
        )
        selector.register(
            self._admission_attempt_fd,
            selectors.EVENT_READ,
            _PressureSignal(job, _PressureSignalKind.ADMISSION_ATTEMPTED),
        )

    def discard_admission_attempt_signals(self) -> None:
        while True:
            try:
                signals = os.read(self._admission_attempt_fd, 4096)
            except BlockingIOError:
                return
            if not signals:
                return

    def consume_admission_attempt_signals(self) -> int:
        try:
            return len(os.read(self._admission_attempt_fd, 4096))
        except BlockingIOError:
            return 0

    def started_signal_is_ready(self) -> bool:
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._stdout(), selectors.EVENT_READ)
            return bool(selector.select(timeout=0))
        finally:
            selector.close()

    def release(self) -> None:
        self.signal_release()
        self.wait_until_clean_exit()

    def signal_release(self) -> None:
        if self._release_signalled:
            return
        stdin = self._stdin()
        stdin.write("\n")
        stdin.flush()
        self._release_signalled = True

    def wait_until_clean_exit(self) -> None:
        PROCESS_COMPLETION_WATCHDOG.wait(
            self._process,
            operation=f"pressure work {self.work.label}",
        )
        self._completion_observed = True
        assert self._process.returncode == 0, self._unexpected_exit()

    def wait_until_expected_failure(self) -> None:
        PROCESS_COMPLETION_WATCHDOG.wait(
            self._process,
            operation=f"pressure work {self.work.label} expected failure",
        )
        self._completion_observed = True
        if self._process.returncode in (None, 0):
            raise AssertionError(
                f"pressure work {self.work.label} unexpectedly succeeded"
            )

    def kill_parent(self) -> None:
        self._process.kill()
        PROCESS_COMPLETION_WATCHDOG.wait(
            self._process,
            operation=f"reap crashed pressure parent {self.work.label}",
        )
        self._completion_observed = True

    def release_orphaned_child(self) -> None:
        if self._process.returncode is None:
            raise RuntimeError("executor parent must be killed before orphan release")
        self.signal_release()

    def cleanup(self, *, abort: bool) -> None:
        if type(abort) is not bool:
            raise ValueError("pressure cleanup requires an exact abort policy")
        if abort:
            self._abort_cleanup_plan().execute()
            return
        if type(self.work.command) is HungPressureCommand:
            try:
                self._cleanup_command_once()
            except BaseException as error:
                self._abort_cleanup_plan().execute(preceding_error=error)
                raise AssertionError("abort cleanup must raise")
        if self._process.poll() is not None:
            self._cleanup_reaped_process()
            return
        try:
            PROCESS_COMPLETION_WATCHDOG.wait(
                self._process,
                operation=f"clean up pressure work {self.work.label}",
            )
        except BaseException as error:
            self._abort_cleanup_plan().execute(preceding_error=error)
            raise AssertionError("abort cleanup must raise")
        if not self._completion_observed:
            self._completion_observed = True
            if self._process.returncode != 0:
                self._abort_cleanup_plan().execute(
                    preceding_error=AssertionError(self._unexpected_exit())
                )
                raise AssertionError("failed completion cleanup must raise")
        self._close_cleanup_plan().execute()

    def _cleanup_reaped_process(self) -> None:
        if not self._completion_observed:
            self._completion_observed = True
            if self._process.returncode != 0:
                self._reaped_cleanup_plan().execute(
                    preceding_error=AssertionError(self._unexpected_exit())
                )
                raise AssertionError("failed completion cleanup must raise")
        self._reaped_cleanup_plan().execute()

    def _abort_cleanup_plan(self) -> ProcessCleanupPlan:
        return ProcessCleanupPlan(
            operation=f"abort pressure work {self.work.label}",
            steps=(
                ProcessCleanupStep(
                    "clean up pressure command containment",
                    self._cleanup_command_once,
                ),
                ProcessCleanupStep(
                    "contain pressure guardian before outer termination",
                    self._guardian_pid_file.contain_if_recorded,
                ),
                ProcessCleanupStep(
                    "kill pressure outer process group",
                    self._kill_outer_process_group_if_running,
                ),
                ProcessCleanupStep(
                    "contain pressure guardian after outer termination",
                    self._guardian_pid_file.contain_if_recorded,
                ),
                ProcessCleanupStep(
                    "reap pressure outer process",
                    self._reap_outer_process_if_running,
                ),
                *self._resource_cleanup_steps(),
            ),
        )

    def _reaped_cleanup_plan(self) -> ProcessCleanupPlan:
        return ProcessCleanupPlan(
            operation=f"contain completed pressure work {self.work.label}",
            steps=(
                ProcessCleanupStep(
                    "clean up pressure command containment",
                    self._cleanup_command_once,
                ),
                ProcessCleanupStep(
                    "contain completed pressure guardian",
                    self._guardian_pid_file.contain_if_recorded,
                ),
                *self._resource_cleanup_steps(),
            ),
        )

    def _close_cleanup_plan(self) -> ProcessCleanupPlan:
        return ProcessCleanupPlan(
            operation=f"close pressure work {self.work.label}",
            steps=self._resource_cleanup_steps(),
        )

    def _cleanup_command_once(self) -> None:
        if self._command_cleanup_attempted:
            return
        self._command_cleanup_attempted = True
        self.work.command.cleanup()

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
                operation=f"reap aborted pressure work {self.work.label}",
            )

    def _resource_cleanup_steps(self) -> tuple[ProcessCleanupStep, ...]:
        return (
            ProcessCleanupStep("close pressure stdin", self._close_stdin),
            ProcessCleanupStep("close pressure stdout", self._close_stdout),
            ProcessCleanupStep("close pressure stderr", self._close_stderr),
            ProcessCleanupStep(
                "close pressure admission descriptor",
                self._close_admission_attempt_fd,
            ),
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

    def _close_admission_attempt_fd(self) -> None:
        if not self._admission_attempt_fd_open:
            return
        os.close(self._admission_attempt_fd)
        self._admission_attempt_fd_open = False

    def signal_cleanup_release(self) -> None:
        if (
            type(self.work.command)
            in (
                HeldPressureCommand,
                CloseFdsTreePressureCommand,
            )
            and self._process.poll() is None
        ):
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
            f"pressure outer process {self._process.pid} remains live"
        )
        assert not self._admission_attempt_fd_open, (
            "pressure admission descriptor remained open after cleanup"
        )
        for name, stream in (
            ("stdin", self._process.stdin),
            ("stdout", self._process.stdout),
            ("stderr", self._process.stderr),
        ):
            if stream is not None:
                assert stream.closed, f"pressure {name} remained open after cleanup"

    def _readline(self, stream: TextIO, *, transition: str) -> str:
        line = stream.readline()
        assert line, self._unexpected_line(transition=transition, line=line)
        return line

    def _require_started_line(self, line: str) -> None:
        assert line == f"{self.work.label}\n", self._unexpected_line(
            transition="start",
            line=line,
        )

    def _unexpected_line(self, *, transition: str, line: str) -> str:
        return (
            f"{self.work.label} did not {transition}: line={line!r} "
            f"returncode={self._process.poll()!r}"
        )

    def _unexpected_exit(self) -> str:
        return (
            f"{self.work.label} exited {self._process.returncode}; "
            f"stderr={self._stderr().read()!r}"
        )

    def _stdin(self) -> TextIO:
        if self._process.stdin is None:
            raise RuntimeError("pressure process stdin was not configured")
        return cast(TextIO, self._process.stdin)

    def _stdout(self) -> TextIO:
        if self._process.stdout is None:
            raise RuntimeError("pressure process stdout was not configured")
        return cast(TextIO, self._process.stdout)

    def _stderr(self) -> TextIO:
        if self._process.stderr is None:
            raise RuntimeError("pressure process stderr was not configured")
        return cast(TextIO, self._process.stderr)


class PressureRig:
    """Run real pressure processes through a compact scheduler-scenario DSL."""

    def __init__(self, pool_dir: Path, *, host_cpu_slots: int) -> None:
        if not isinstance(pool_dir, Path):
            raise ValueError("PressureRig.pool_dir must be a Path")
        if type(host_cpu_slots) is not int or host_cpu_slots < 1:
            raise ValueError("PressureRig.host_cpu_slots must be positive")
        self._pool_dir = pool_dir
        self._host_cpu_slots = host_cpu_slots
        self._host_cpu_busy_file = pool_dir / "test-host-cpu-busy-percent"
        self.set_host_cpu_busy_percent(0.0)
        self._processes: dict[PressureJob, _ControlledPressureProcess] = {}
        self._next_sequence = 1

    def __enter__(self) -> PressureRig:
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
            operation="clean up pressure scenario",
            steps=(
                *(
                    ProcessCleanupStep(
                        f"signal cleanup release for {controlled.work.label}",
                        controlled.signal_cleanup_release,
                    )
                    for controlled in processes
                ),
                *(
                    ProcessCleanupStep(
                        f"clean up pressure work {controlled.work.label}",
                        partial(controlled.cleanup, abort=exception is not None),
                    )
                    for controlled in reversed(processes)
                ),
            ),
        ).execute(preceding_error=exception)

    def submit(self, work: PressureWork) -> PressureJob:
        """Submit work without assuming its immediate admission outcome."""
        self._require_work(work)
        job = PressureJob(self._next_sequence, work)
        self._next_sequence += 1
        self._processes[job] = _ControlledPressureProcess(
            self._pool_dir,
            work,
            sequence=job.sequence,
            host_cpu_slots=self._host_cpu_slots,
            host_cpu_busy_file=self._host_cpu_busy_file,
        )
        return job

    def submit_all(
        self,
        work: tuple[PressureWork, ...],
    ) -> tuple[PressureJob, ...]:
        """Submit a cohort before observing any member's outcome."""
        self._require_work_cohort(work)
        return tuple(self.submit(item) for item in work)

    def set_host_cpu_busy_percent(self, busy_percent: float) -> None:
        """Set the exact whole-host CPU input observed by subsequent decisions."""
        if type(busy_percent) is not float or not 0 <= busy_percent <= 100:
            raise ValueError("pressure host CPU busy percent must be in [0, 100]")
        self._host_cpu_busy_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._host_cpu_busy_file.with_suffix(".next")
        temporary.write_text(
            f"{busy_percent}\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._host_cpu_busy_file)

    def admit(self, work: PressureWork) -> PressureJob:
        """Submit work and require it to acquire a lease and start."""
        job = self.submit(work)
        self.require_started(job)
        return job

    def defer(self, work: PressureWork) -> PressureJob:
        """Submit work and require the active constraints to defer it."""
        job = self.submit(work)
        self._process(job).wait_until_queued()
        return job

    def defer_all(
        self,
        work: tuple[PressureWork, ...],
    ) -> tuple[PressureJob, ...]:
        """Submit a cohort and require every member to be deferred."""
        jobs = self.submit_all(work)
        for job in jobs:
            self._process(job).wait_until_queued()
        return jobs

    def require_started(self, job: PressureJob) -> None:
        """Require one submitted job to acquire a lease and start."""
        self._process(job).wait_until_started()

    def require_next_started(
        self,
        jobs: tuple[PressureJob, ...],
    ) -> PressureJob:
        """Require and return the next member of a submitted cohort to start."""
        self._require_job_cohort(jobs, allow_empty=False)
        selector = selectors.DefaultSelector()
        try:
            for job in jobs:
                process = self._process(job)
                process.register_start_signal(selector, job)
            ready = selector.select()
            job = ready[0][0].data
            assert type(job) is PressureJob
            self._process(job).consume_started_signal()
            return job
        finally:
            selector.close()

    def require_all_started(self, jobs: tuple[PressureJob, ...]) -> None:
        """Require every member of a submitted cohort to start in any order."""
        self._require_job_cohort(jobs, allow_empty=True)
        remaining = list(jobs)
        while remaining:
            started = self.require_next_started(tuple(remaining))
            remaining.remove(started)

    def require_none_started(self, jobs: tuple[PressureJob, ...]) -> None:
        """Prove contenders receive denials while the caller keeps a holder live."""
        self._require_job_cohort(jobs, allow_empty=True)
        if not jobs:
            return
        attempts = dict.fromkeys(jobs, 0)
        selector = selectors.DefaultSelector()
        try:
            for job in jobs:
                process = self._process(job)
                process.discard_admission_attempt_signals()
                process.register_pressure_signals(selector, job)
            while any(
                count < REQUIRED_POST_HOLDER_ATTEMPTS for count in attempts.values()
            ):
                ready = selector.select()
                signals = tuple(key.data for key, _events in ready)
                assert all(type(signal) is _PressureSignal for signal in signals)
                for signal in cast(tuple[_PressureSignal, ...], signals):
                    process = self._process(signal.job)
                    if signal.kind is _PressureSignalKind.STARTED:
                        raise AssertionError(
                            f"{signal.job.work.label} started while its holder "
                            "remained active"
                        )
                    attempts[signal.job] += process.consume_admission_attempt_signals()
            assert all(not self._process(job).started_signal_is_ready() for job in jobs)
        finally:
            selector.close()

    def release(self, job: PressureJob) -> None:
        """Release one started job and require its clean completion."""
        self._process(job).release()

    def complete_together(self, jobs: tuple[PressureJob, ...]) -> None:
        """Release a started cohort together, then require every clean exit."""
        self._require_job_cohort(jobs, allow_empty=True)
        for job in jobs:
            self._process(job).signal_release()
        for job in jobs:
            self._process(job).wait_until_clean_exit()

    def crash_parent(self, job: PressureJob) -> None:
        """Kill a job's executor parent while its guardian retains the lease."""
        self._process(job).kill_parent()

    def require_failed(self, job: PressureJob) -> None:
        """Require an intentionally faulted executor parent to fail cleanly."""
        self._process(job).wait_until_expected_failure()

    def release_orphaned_child(self, job: PressureJob) -> None:
        """Release a command whose executor parent was explicitly killed."""
        self._process(job).release_orphaned_child()

    def require_guardian_contained(self, job: PressureJob) -> None:
        """Require one explicitly admitted job's guardian to be contained."""
        self._process(job).require_guardian_contained()

    def require_cleanup_complete(self, job: PressureJob) -> None:
        """Require one admitted job's process and owned resources to be clean."""
        self._process(job).require_cleanup_complete()

    def drain(self, jobs: tuple[PressureJob, ...]) -> None:
        """Start and release a queued cohort until every member completes."""
        self._require_job_cohort(jobs, allow_empty=True)
        remaining = list(jobs)
        while remaining:
            started = self.require_next_started(tuple(remaining))
            remaining.remove(started)
            self.release(started)

    def _process(self, job: PressureJob) -> _ControlledPressureProcess:
        if type(job) is not PressureJob:
            raise ValueError("PressureRig requires a PressureJob")
        try:
            return self._processes[job]
        except KeyError as exc:
            raise ValueError("PressureJob does not belong to this PressureRig") from exc

    @staticmethod
    def _require_work(work: PressureWork) -> None:
        if type(work) is not PressureWork:
            raise ValueError("PressureRig requires PressureWork")

    @classmethod
    def _require_work_cohort(cls, work: tuple[PressureWork, ...]) -> None:
        if type(work) is not tuple:
            raise ValueError("PressureRig work cohort must be a tuple")
        for item in work:
            cls._require_work(item)

    def _require_job_cohort(
        self,
        jobs: tuple[PressureJob, ...],
        *,
        allow_empty: bool,
    ) -> None:
        if type(jobs) is not tuple:
            raise ValueError("PressureRig job cohort must be a tuple")
        if not allow_empty and not jobs:
            raise ValueError("PressureRig job cohort must not be empty")
        for job in jobs:
            self._process(job)
