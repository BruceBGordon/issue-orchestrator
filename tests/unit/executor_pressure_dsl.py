"""Small scenario DSL for real cross-process host-executor pressure tests."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import TextIO, cast

from issue_orchestrator.domain.executor import ExecutorConcurrencyRange


POOL_DIR_ENV = "ISSUE_ORCHESTRATOR_EXECUTOR_POOL_DIR"
HOST_CPU_BUSY_FILE_ENV = "ISSUE_ORCHESTRATOR_TEST_HOST_CPU_BUSY_FILE"
ADMISSION_ATTEMPT_FD_ENV = "ISSUE_ORCHESTRATOR_TEST_ADMISSION_ATTEMPT_FD"
PROCESS_RUNNER = Path(__file__).with_name("executor_process_runner.py")
PROCESS_TRANSITION_TIMEOUT_SECONDS = 10.0
REQUIRED_POST_HOLDER_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class PressureWork:
    """One explicitly controlled command submitted by a pressure scenario."""

    label: str
    group: str
    concurrency_range: ExecutorConcurrencyRange = ExecutorConcurrencyRange(1, 1)
    exclusive_resources: tuple[str, ...] = ()

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

    def command_line(self, host_cpu_slots: int) -> list[str]:
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
        return [
            *command,
            "--",
            sys.executable,
            "-u",
            "-c",
            (f"import sys; print({self.label!r}, flush=True); sys.stdin.readline()"),
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
        host_cpu_slots: int,
        host_cpu_busy_file: Path,
    ) -> None:
        self.work = work
        attempt_read_fd, attempt_write_fd = os.pipe()
        os.set_blocking(attempt_read_fd, False)
        try:
            self._process: subprocess.Popen[str] = subprocess.Popen(
                work.command_line(host_cpu_slots),
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
        stdin = self._stdin()
        stdin.write("\n")
        stdin.flush()

    def wait_until_clean_exit(self) -> None:
        self._process.wait(timeout=PROCESS_TRANSITION_TIMEOUT_SECONDS)
        assert self._process.returncode == 0, self._unexpected_exit()

    def kill_parent(self) -> None:
        self._process.kill()
        self._process.wait(timeout=PROCESS_TRANSITION_TIMEOUT_SECONDS)

    def release_orphaned_child(self) -> None:
        if self._process.returncode is None:
            raise RuntimeError("executor parent must be killed before orphan release")
        self.signal_release()

    def cleanup(self) -> None:
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if self._process.poll() is None:
            self._process.wait(timeout=PROCESS_TRANSITION_TIMEOUT_SECONDS)
        os.close(self._admission_attempt_fd)

    def _readline(self, stream: TextIO, *, transition: str) -> str:
        selector = selectors.DefaultSelector()
        try:
            selector.register(stream, selectors.EVENT_READ)
            ready = selector.select(timeout=PROCESS_TRANSITION_TIMEOUT_SECONDS)
            assert ready, (
                f"{self.work.label} did not {transition} within "
                f"{PROCESS_TRANSITION_TIMEOUT_SECONDS:.0f}s"
            )
        finally:
            selector.close()
        return stream.readline()

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
        del exception_type, exception, traceback
        for controlled in reversed(tuple(self._processes.values())):
            controlled.cleanup()

    def submit(self, work: PressureWork) -> PressureJob:
        """Submit work without assuming its immediate admission outcome."""
        self._require_work(work)
        job = PressureJob(self._next_sequence, work)
        self._next_sequence += 1
        self._processes[job] = _ControlledPressureProcess(
            self._pool_dir,
            work,
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
        if (
            type(busy_percent) is not float
            or not 0 <= busy_percent <= 100
        ):
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
            ready = selector.select(timeout=PROCESS_TRANSITION_TIMEOUT_SECONDS)
            assert ready, (
                "no pressure job started within "
                f"{PROCESS_TRANSITION_TIMEOUT_SECONDS:.0f}s"
            )
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
                count < REQUIRED_POST_HOLDER_ATTEMPTS
                for count in attempts.values()
            ):
                ready = selector.select(timeout=PROCESS_TRANSITION_TIMEOUT_SECONDS)
                assert ready, (
                    "pressure contenders did not acknowledge post-holder "
                    "admission decisions"
                )
                signals = tuple(key.data for key, _events in ready)
                assert all(type(signal) is _PressureSignal for signal in signals)
                for signal in cast(tuple[_PressureSignal, ...], signals):
                    process = self._process(signal.job)
                    if signal.kind is _PressureSignalKind.STARTED:
                        raise AssertionError(
                            f"{signal.job.work.label} started while its holder "
                            "remained active"
                        )
                    attempts[signal.job] += (
                        process.consume_admission_attempt_signals()
                    )
            assert all(
                not self._process(job).started_signal_is_ready() for job in jobs
            )
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
        """Kill a job's executor parent while its child retains the lease."""
        self._process(job).kill_parent()

    def release_orphaned_child(self, job: PressureJob) -> None:
        """Release a child whose executor parent was explicitly killed."""
        self._process(job).release_orphaned_child()

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
