"""Typed deadlock-watchdog and process-containment fixtures for real tests."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TextIO, TypeVar

from issue_orchestrator.domain.executor import ExecutorInteractiveSessionCancellation
from issue_orchestrator.adapters.ps_process_group_observer import (
    PsProcessGroupObserver,
    PsProcessObservationPolicy,
)
from issue_orchestrator.adapters.kernel_process_identity import (
    build_kernel_process_identity_observer,
)
from issue_orchestrator.execution.executor_guardian_cancellation import (
    ExecutorSessionGuardianCanceller,
)
from issue_orchestrator.execution.atomic_record_store import (
    OsAtomicRecordStoreFactory,
)
from tests.process_tree_fixture import (
    PROCESS_CONTAINMENT_WATCHDOG_SECONDS,
    ProcessTreeMember,
)


_Output = TypeVar("_Output", str, bytes)
_Result = TypeVar("_Result")


def build_test_process_group_observer() -> PsProcessGroupObserver:
    """Construct the real host observer used by process-containment proofs."""
    return PsProcessGroupObserver(
        Path("/bin/ps"),
        PsProcessObservationPolicy(command_timeout_seconds=2.0),
        build_kernel_process_identity_observer(),
    )


class ProcessCompletionTimeout(AssertionError):
    """A real fixture failed to complete before its deadlock watchdog."""


@dataclass(frozen=True, slots=True)
class ProcessCleanupStep:
    """One independently attempted action in a process-cleanup plan."""

    operation: str
    action: Callable[[], None]

    def __post_init__(self) -> None:
        _require_operation(self.operation)
        if not callable(self.action):
            raise ValueError("ProcessCleanupStep.action must be callable")


@dataclass(frozen=True, slots=True)
class ProcessCleanupPlan:
    """Attempt every cleanup step before reporting any combined failures."""

    operation: str
    steps: tuple[ProcessCleanupStep, ...]

    def __post_init__(self) -> None:
        _require_operation(self.operation)
        if type(self.steps) is not tuple or not self.steps:
            raise ValueError("ProcessCleanupPlan.steps must not be empty")
        if any(type(step) is not ProcessCleanupStep for step in self.steps):
            raise ValueError(
                "ProcessCleanupPlan.steps must contain ProcessCleanupStep values"
            )

    def execute(self, *, preceding_error: BaseException | None = None) -> None:
        """Run all steps, preserving the triggering error alongside cleanup errors."""
        failures: list[BaseException] = []
        if preceding_error is not None:
            failures.append(preceding_error)
        for step in self.steps:
            try:
                step.action()
            except BaseException as error:
                error.add_note(f"process cleanup step: {step.operation}")
                failures.append(error)
        if not failures:
            return
        if len(failures) == 1 and preceding_error is not None:
            raise preceding_error
        raise BaseExceptionGroup(self.operation, failures)


@dataclass(frozen=True, slots=True)
class NoDescendantProcessContainment:
    """Explicit contract for a command that cannot leave detached descendants."""

    def contain_after_timeout(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class ExecutorGuardianCancellationContainment:
    """Contain an interactive executor guardian through its ownership record."""

    cancellation_record_path: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cancellation_record_path, Path)
            or not self.cancellation_record_path.is_absolute()
        ):
            raise ValueError(
                "ExecutorGuardianCancellationContainment.cancellation_record_path "
                "must be absolute"
            )

    def contain_after_timeout(self) -> None:
        ExecutorSessionGuardianCanceller(
            PROCESS_CONTAINMENT_WATCHDOG_SECONDS,
            OsAtomicRecordStoreFactory(),
        ).contain_if_active(
            ExecutorInteractiveSessionCancellation(self.cancellation_record_path)
        )


ProcessTimeoutContainment = (
    NoDescendantProcessContainment | ExecutorGuardianCancellationContainment
)


@dataclass(frozen=True, slots=True)
class TextProcessInvocation:
    """One exact, captured text subprocess invocation."""

    operation: str
    arguments: tuple[str, ...]
    working_directory: Path
    environment: Mapping[str, str]
    timeout_containment: ProcessTimeoutContainment

    def __post_init__(self) -> None:
        if type(self.operation) is not str or not self.operation:
            raise ValueError("TextProcessInvocation.operation must not be empty")
        if type(self.arguments) is not tuple or not self.arguments:
            raise ValueError("TextProcessInvocation.arguments must not be empty")
        if any(
            type(argument) is not str or "\0" in argument for argument in self.arguments
        ):
            raise ValueError(
                "TextProcessInvocation.arguments must contain strings without NUL bytes"
            )
        if (
            not isinstance(self.working_directory, Path)
            or not self.working_directory.is_absolute()
        ):
            raise ValueError(
                "TextProcessInvocation.working_directory must be an absolute Path"
            )
        environment = dict(self.environment)
        if any(
            type(key) is not str
            or not key
            or "=" in key
            or "\0" in key
            or type(value) is not str
            or "\0" in value
            for key, value in environment.items()
        ):
            raise ValueError(
                "TextProcessInvocation.environment must contain valid process strings"
            )
        object.__setattr__(self, "environment", MappingProxyType(environment))
        if type(self.timeout_containment) not in (
            NoDescendantProcessContainment,
            ExecutorGuardianCancellationContainment,
        ):
            raise ValueError(
                "TextProcessInvocation.timeout_containment must be explicit"
            )


@dataclass(frozen=True, slots=True)
class ProcessCompletionWatchdog:
    """Bound only deadlocks; explicit fixture signals own transition ordering."""

    timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.timeout_seconds) is not float
            or self.timeout_seconds <= PROCESS_CONTAINMENT_WATCHDOG_SECONDS
        ):
            raise ValueError(
                "ProcessCompletionWatchdog.timeout_seconds must exceed the "
                "process-containment watchdog"
            )

    def run_text(
        self,
        invocation: TextProcessInvocation,
    ) -> subprocess.CompletedProcess[str]:
        """Run one captured text process under this deadlock watchdog."""
        if type(invocation) is not TextProcessInvocation:
            raise ValueError(
                "ProcessCompletionWatchdog.run_text requires TextProcessInvocation"
            )
        process = subprocess.Popen(
            invocation.arguments,
            cwd=invocation.working_directory,
            env=dict(invocation.environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            return subprocess.CompletedProcess(
                invocation.arguments,
                process.returncode,
                stdout,
                stderr,
            )
        except subprocess.TimeoutExpired as error:
            completion_error = ProcessCompletionTimeout(
                f"{invocation.operation} did not complete within the "
                f"{self.timeout_seconds:.0f}-second deadlock watchdog"
            )
            completion_error.__cause__ = error
            ProcessCleanupPlan(
                operation=f"contain timed-out {invocation.operation}",
                steps=(
                    ProcessCleanupStep(
                        operation="contain outer process group",
                        action=lambda: _kill_process_group(process.pid),
                    ),
                    ProcessCleanupStep(
                        operation="contain descendants after process timeout",
                        action=invocation.timeout_containment.contain_after_timeout,
                    ),
                    ProcessCleanupStep(
                        operation="reap outer process",
                        action=lambda: process.wait(
                            timeout=PROCESS_CONTAINMENT_WATCHDOG_SECONDS
                        ),
                    ),
                    ProcessCleanupStep(
                        operation="close outer stdout",
                        action=lambda: _close_process_stream(process.stdout),
                    ),
                    ProcessCleanupStep(
                        operation="close outer stderr",
                        action=lambda: _close_process_stream(process.stderr),
                    ),
                ),
            ).execute(preceding_error=completion_error)
            raise AssertionError("timeout cleanup must raise")

    def communicate(
        self,
        process: subprocess.Popen[_Output],
        *,
        operation: str,
    ) -> tuple[_Output, _Output]:
        """Collect one process after the test's own protocol permits completion."""
        _require_operation(operation)
        try:
            return process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise ProcessCompletionTimeout(
                f"{operation} did not complete within the "
                f"{self.timeout_seconds:.0f}-second deadlock watchdog; "
                f"pid={process.pid} returncode={process.poll()!r}"
            ) from error

    def wait(
        self,
        process: subprocess.Popen[_Output],
        *,
        operation: str,
    ) -> int:
        """Reap one process after the test's own protocol permits completion."""
        _require_operation(operation)
        try:
            return process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise ProcessCompletionTimeout(
                f"{operation} did not complete within the "
                f"{self.timeout_seconds:.0f}-second deadlock watchdog; "
                f"pid={process.pid} returncode={process.poll()!r}"
            ) from error

    def wait_for_event(self, event: threading.Event, *, operation: str) -> None:
        """Require an explicit thread handoff under this deadlock watchdog."""
        if not isinstance(event, threading.Event):
            raise ValueError(
                "ProcessCompletionWatchdog.wait_for_event requires threading.Event"
            )
        _require_operation(operation)
        if not event.wait(timeout=self.timeout_seconds):
            raise ProcessCompletionTimeout(
                f"{operation} did not occur within the "
                f"{self.timeout_seconds:.0f}-second deadlock watchdog"
            )

    def wait_for_path(self, path: Path, *, operation: str) -> None:
        """Require one external process to publish a filesystem handshake."""
        if not path.is_absolute():
            raise ValueError(
                "ProcessCompletionWatchdog.wait_for_path requires an absolute Path"
            )
        _require_operation(operation)
        deadline = time.monotonic() + self.timeout_seconds
        pause = threading.Event()
        while not path.exists():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessCompletionTimeout(
                    f"{operation} did not occur within the "
                    f"{self.timeout_seconds:.0f}-second deadlock watchdog; "
                    f"path={path}"
                )
            pause.wait(timeout=min(0.01, remaining))

    def join_thread(self, thread: threading.Thread, *, operation: str) -> None:
        """Require one thread to complete under this deadlock watchdog."""
        if not isinstance(thread, threading.Thread):
            raise ValueError(
                "ProcessCompletionWatchdog.join_thread requires threading.Thread"
            )
        _require_operation(operation)
        thread.join(timeout=self.timeout_seconds)
        if thread.is_alive():
            raise ProcessCompletionTimeout(
                f"{operation} did not complete within the "
                f"{self.timeout_seconds:.0f}-second deadlock watchdog; "
                f"thread={thread.name!r}"
            )

    def future_result(
        self,
        future: Future[_Result],
        *,
        operation: str,
    ) -> _Result:
        """Read a signalled future under this deadlock watchdog."""
        if not isinstance(future, Future):
            raise ValueError(
                "ProcessCompletionWatchdog.future_result requires concurrent Future"
            )
        _require_operation(operation)
        try:
            return future.result(timeout=self.timeout_seconds)
        except FutureTimeoutError as error:
            if future.done():
                return future.result()
            raise ProcessCompletionTimeout(
                f"{operation} did not complete within the "
                f"{self.timeout_seconds:.0f}-second deadlock watchdog"
            ) from error


PROCESS_COMPLETION_WATCHDOG = ProcessCompletionWatchdog(120.0)


@dataclass(frozen=True, slots=True)
class GuardianPidFile:
    """Explicit guardian-identity handshake for one admitted fixture command."""

    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("GuardianPidFile.path must be an absolute Path")

    def recording_arguments(self, arguments: tuple[str, ...]) -> tuple[str, ...]:
        """Wrap a command so it records its guardian before replacing itself."""
        if type(arguments) is not tuple or not arguments:
            raise ValueError("guardian-recorded arguments must not be empty")
        if any(type(argument) is not str or "\0" in argument for argument in arguments):
            raise ValueError(
                "guardian-recorded arguments must contain strings without NUL bytes"
            )
        source = (
            "import os, pathlib, sys\n"
            f"pathlib.Path({str(self.path)!r}).write_text("
            "str(os.getppid()), encoding='utf-8')\n"
            "os.execvpe(sys.argv[1], sys.argv[1:], os.environ)\n"
        )
        return (sys.executable, "-c", source, *arguments)

    def contain_if_recorded(self) -> None:
        """Force-contain a guardian when admission published its exact identity."""
        if not self.path.exists():
            return
        guardian_pid = self._recorded_process_id()
        try:
            process_group_id = os.getpgid(guardian_pid)
        except ProcessLookupError:
            return
        if process_group_id != guardian_pid:
            raise AssertionError(
                f"recorded guardian {guardian_pid} does not own its process group"
            )
        os.killpg(process_group_id, signal.SIGKILL)
        ProcessTreeMember(guardian_pid).assert_contained()

    def require_contained(self) -> None:
        """Require an admitted guardian to be unable to execute user code."""
        if not self.path.exists():
            raise AssertionError(f"guardian identity was not recorded at {self.path}")
        ProcessTreeMember(self._recorded_process_id()).assert_contained()

    def _recorded_process_id(self) -> int:
        guardian_pid = int(self.path.read_text(encoding="utf-8"))
        if guardian_pid <= 1:
            raise AssertionError(f"invalid recorded guardian pid {guardian_pid}")
        return guardian_pid


def _kill_process_group(process_group_id: int) -> None:
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return


def _close_process_stream(stream: TextIO | None) -> None:
    if stream is not None:
        stream.close()


def _require_operation(operation: str) -> None:
    if type(operation) is not str or not operation:
        raise ValueError("process completion operation must not be empty")
