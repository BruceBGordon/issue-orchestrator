# pyright: strict
"""Outer owner for launching and observing one crash-resilient guardian."""

from __future__ import annotations

import os
import selectors
import signal
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from pydantic import ValidationError

from ...domain.executor_guardian import (
    ExecutorGuardianActivationTimedOut,
    ExecutorGuardianBoundedBudget,
    ExecutorGuardianBudget,
    ExecutorGuardianCommandCompleted,
    ExecutorGuardianCommandInterrupted,
    ExecutorGuardianCommandStartFailed,
    ExecutorGuardianCommandTimedOut,
    ExecutorGuardianInternalFailed,
    ExecutorGuardianPostContainmentError,
    ExecutorGuardianPostContainmentFailure,
    ExecutorGuardianTerminal,
    ExecutorGuardianTerminationPolicy,
    ExecutorGuardianUnboundedBudget,
)
from ...domain.executor import ExecutorCommandLifecycle, ExecutorDeadlineReason
from ...domain.process_group_sentinel import (
    ProcessGroupSentinelPolicy,
    ProcessGroupSentinelProgram,
)
from ...domain.process_group import (
    OwnedProcessGroupLeader,
    ProcessGroupCompleted,
    ProcessGroupInterrupted,
    ProcessGroupSupervision,
    ProcessGroupTerminalCompletionAccepted,
    ProcessGroupTerminalDecision,
    ProcessGroupTerminalInterruptionRequested,
    ProcessGroupTermination,
    ProcessGroupUnboundedWait,
)
from ...domain.posix_process import (
    PosixProcessActivationDeadlineAbsent,
    PosixProcessActivationDeadlinePresent,
    PosixDescriptorMapping,
    PosixProcessAbsoluteActivationDeadline,
    PosixProcessConfiguredActivationDeadline,
    PosixProcessEnvironment,
    PosixProcessGroupMode,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
    classify_posix_process_activation_deadline,
)
from ...ports.executor_command_guardian import ExecutorGuardianRequest
from ...ports.atomic_record_store import AtomicRecordStoreFactory
from ...ports.process_group_supervisor import ProcessGroupSupervisor
from ...ports.guardian_launch_pipes import (
    GuardianLaunchPipes,
    GuardianLaunchPipesClosed,
    GuardianLaunchPipesCloseFailed,
    GuardianLaunchPipesFactory,
)
from ...ports.posix_process import (
    PosixProcessExecRejected,
    PosixProcessHandle,
    PosixProcessLaunch,
    PosixProcessLauncher,
    PosixProcessLaunchRecovered,
    PosixProcessLaunchRecoveryFailed,
    PosixProcessLaunchRejected,
    PosixProcessLaunchStarted,
)
from ..process_group_supervisor import NeverInterruptProcessGroup
from ...domain.independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupFailure,
    CleanupOutcome,
    CleanupSucceeded,
    IndependentCleanupPlan,
)
from ..executor_guardian_cancellation import (
    ExecutorGuardianCancellationControls,
    ExecutorGuardianCancellationLease,
    NoExecutorGuardianCancellationControls,
    prepare_executor_guardian_cancellation,
)
from ..process_cancellation_endpoint import ProcessCancellationOwnerControls
from ._guardian_contracts import (
    GUARDIAN_TERMINAL_ADAPTER,
    GUARDIAN_START_SIGNAL,
    GuardianCancellationControlRecord,
    GuardianDetachedCancellationControlRecord,
    GuardianInteractiveCancellationControlRecord,
    GuardianInvocationRecord,
)


_MAX_RESULT_BYTES = 65536
_OWNER_READY_SIGNAL = b"R"


def _require_process_group_supervisor(value: object) -> None:
    if not isinstance(value, ProcessGroupSupervisor):
        raise ValueError(
            "PosixExecutorCommandGuardian.process_group_supervisor must implement "
            "ProcessGroupSupervisor"
        )


class ExecutorGuardianLaunchError(RuntimeError):
    """Raised when the outer process cannot start its guardian."""


class ExecutorGuardianProtocolError(RuntimeError):
    """Raised when a guardian exits without one exact terminal record."""


@dataclass(frozen=True, slots=True)
class _GuardianActivationStarted:
    process: PosixProcessHandle


@dataclass(frozen=True, slots=True)
class _GuardianActivationContained:
    error: BaseException


@dataclass(frozen=True, slots=True)
class _GuardianActivationUncontained:
    process_id: int
    error: BaseException


@dataclass(frozen=True, slots=True)
class _GuardianActivationTimedOut:
    reason: ExecutorDeadlineReason
    recovery_failures: tuple[ExecutorGuardianPostContainmentFailure, ...]

    def __post_init__(self) -> None:
        if type(self.reason) is not ExecutorDeadlineReason:
            raise ValueError("guardian activation timeout reason must be typed")
        if type(self.recovery_failures) is not tuple or any(
            type(failure) is not ExecutorGuardianPostContainmentFailure
            for failure in self.recovery_failures
        ):
            raise ValueError("guardian activation recovery failures must be typed")


_GuardianActivation = (
    _GuardianActivationStarted
    | _GuardianActivationContained
    | _GuardianActivationUncontained
    | _GuardianActivationTimedOut
)


class _NoParentInterruption:
    """Explicit detached lifecycle that does not reinterpret parent signals."""

    def __init__(self) -> None:
        self._waiter = NeverInterruptProcessGroup()

    @property
    def requested(self) -> bool:
        return False

    def wait_for_request(self, timeout_seconds: float) -> bool:
        return self._waiter.wait_for_request(timeout_seconds)

    def decide_terminal_observation(self) -> ProcessGroupTerminalDecision:
        return ProcessGroupTerminalCompletionAccepted()


class _SigtermParentInterruption:
    """Translate one deliberate parent SIGTERM into group interruption."""

    def __init__(self) -> None:
        self._requested = threading.Event()

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def request(
        self,
        signal_number: int,
        frame: FrameType | None,
    ) -> None:
        del frame
        if signal_number != signal.SIGTERM:
            raise AssertionError("parent interruption only accepts SIGTERM")
        self._requested.set()

    def wait_for_request(self, timeout_seconds: float) -> bool:
        return self._requested.wait(timeout_seconds)

    def decide_terminal_observation(self) -> ProcessGroupTerminalDecision:
        if self._requested.is_set():
            return ProcessGroupTerminalInterruptionRequested()
        return ProcessGroupTerminalCompletionAccepted()


_ParentInterruption = _NoParentInterruption | _SigtermParentInterruption


@contextmanager
def _installed_parent_interruption(
    lifecycle: ExecutorCommandLifecycle,
) -> Generator[_ParentInterruption]:
    if lifecycle is ExecutorCommandLifecycle.DETACHED:
        yield _NoParentInterruption()
        return
    if lifecycle is not ExecutorCommandLifecycle.INTERACTIVE_SESSION:
        raise AssertionError("ExecutorCommandLifecycle is a closed enum")
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "interactive executor sessions must run on the process main thread"
        )
    interruption = _SigtermParentInterruption()
    previous_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, interruption.request)
    try:
        yield interruption
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


class _NoControllingTerminal:
    def grant(self, process_group_id: int) -> None:
        del process_group_id

    def restore(self) -> None:
        pass


@dataclass(slots=True)
class _InheritedControllingTerminal:
    """Transfer one existing controlling terminal between process groups."""

    file_descriptor: int
    original_process_group_id: int
    _granted: bool = False

    def grant(self, process_group_id: int) -> None:
        if self._granted:
            raise RuntimeError("controlling terminal foreground is already granted")
        self._set_foreground_process_group(process_group_id)
        self._granted = True

    def restore(self) -> None:
        if not self._granted:
            return
        self._set_foreground_process_group(self.original_process_group_id)
        self._granted = False

    def _set_foreground_process_group(self, process_group_id: int) -> None:
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            {signal.SIGTTOU},
        )
        try:
            os.tcsetpgrp(self.file_descriptor, process_group_id)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


_ControllingTerminal = _NoControllingTerminal | _InheritedControllingTerminal


def _controlling_terminal(
    lifecycle: ExecutorCommandLifecycle,
) -> _ControllingTerminal:
    if lifecycle is ExecutorCommandLifecycle.DETACHED or not os.isatty(0):
        return _NoControllingTerminal()
    if lifecycle is not ExecutorCommandLifecycle.INTERACTIVE_SESSION:
        raise AssertionError("ExecutorCommandLifecycle is a closed enum")
    foreground_process_group = os.tcgetpgrp(0)
    process_group = os.getpgrp()
    if foreground_process_group != process_group:
        raise RuntimeError(
            "interactive executor parent must own its controlling terminal foreground"
        )
    return _InheritedControllingTerminal(0, process_group)


@dataclass(frozen=True, slots=True)
class ExecutorGuardianProgram:
    """Exact executable argument prefix for the guardian child."""

    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.arguments) is not tuple or not self.arguments:
            raise ValueError("guardian program arguments must be a non-empty tuple")
        if not self.arguments[0] or any(
            type(argument) is not str for argument in self.arguments
        ):
            raise ValueError(
                "guardian program arguments must contain strings and an executable"
            )
        if any("\0" in argument for argument in self.arguments):
            raise ValueError("guardian program arguments must not contain NUL bytes")
        executable = Path(self.arguments[0])
        if not executable.is_absolute():
            raise ValueError("guardian program executable must be absolute")


class PosixExecutorCommandGuardian:
    """Transfer lease FDs to a child owner and validate its terminal channel."""

    def __init__(
        self,
        program: ExecutorGuardianProgram,
        sentinel_program: ProcessGroupSentinelProgram,
        sentinel_policy: ProcessGroupSentinelPolicy,
        record_stores: AtomicRecordStoreFactory,
        process_launcher: PosixProcessLauncher,
        launch_pipes_factory: GuardianLaunchPipesFactory,
        process_group_supervisor: ProcessGroupSupervisor,
        termination_policy: ExecutorGuardianTerminationPolicy,
    ) -> None:
        if type(program) is not ExecutorGuardianProgram:
            raise ValueError(
                "PosixExecutorCommandGuardian.program must be ExecutorGuardianProgram"
            )
        _require_process_group_supervisor(process_group_supervisor)
        if type(termination_policy) is not ExecutorGuardianTerminationPolicy:
            raise ValueError(
                "PosixExecutorCommandGuardian.termination_policy must be an "
                "ExecutorGuardianTerminationPolicy"
            )
        self._program = program
        if type(sentinel_program) is not ProcessGroupSentinelProgram:
            raise ValueError(
                "PosixExecutorCommandGuardian.sentinel_program must be a "
                "ProcessGroupSentinelProgram"
            )
        if type(sentinel_policy) is not ProcessGroupSentinelPolicy:
            raise ValueError(
                "PosixExecutorCommandGuardian.sentinel_policy must be a "
                "ProcessGroupSentinelPolicy"
            )
        self._sentinel_program = sentinel_program
        self._sentinel_policy = sentinel_policy
        self._record_stores = record_stores
        if not callable(getattr(process_launcher, "launch", None)):
            raise ValueError(
                "PosixExecutorCommandGuardian.process_launcher must implement "
                "PosixProcessLauncher"
            )
        self._process_launcher = process_launcher
        if not callable(getattr(launch_pipes_factory, "create", None)):
            raise ValueError(
                "PosixExecutorCommandGuardian.launch_pipes_factory must implement "
                "GuardianLaunchPipesFactory"
            )
        self._launch_pipes_factory = launch_pipes_factory
        self._process_group_supervisor = process_group_supervisor
        self._termination_policy = termination_policy

    def run(self, request: ExecutorGuardianRequest) -> ExecutorGuardianTerminal:
        self._require_request(request)

        terminal_foreground = _controlling_terminal(request.lifecycle)
        return self._run_validated(request, terminal_foreground)

    @staticmethod
    def _require_request(request: object) -> None:
        if type(request) is not ExecutorGuardianRequest:
            raise ValueError(
                "PosixExecutorCommandGuardian.run requires ExecutorGuardianRequest"
            )

    def _run_validated(
        self,
        request: ExecutorGuardianRequest,
        terminal_foreground: _ControllingTerminal,
    ) -> ExecutorGuardianTerminal:
        pipes = self._launch_pipes_factory.create()
        cancellation_lease = self._prepare_cancellation(request, pipes)
        cancellation_controls = cancellation_lease.controls()
        guardian: PosixProcessHandle | None = None
        uncontained_process_id: int | None = None
        group_contained = False
        try:
            lease_file_descriptors = request.lease.inherited_file_descriptors()
            child_descriptors = pipes.child_descriptors
            invocation = GuardianInvocationRecord.create(
                arguments=request.arguments,
                result_file_descriptor=child_descriptors.result_writer,
                start_file_descriptor=child_descriptors.start_reader,
                owner_ready_file_descriptor=child_descriptors.owner_ready_writer,
                parent_lifetime_read_file_descriptor=(
                    child_descriptors.parent_lifetime_reader
                ),
                lifecycle=request.lifecycle,
                budget=request.budget,
                cancellation=self._guardian_cancellation_record(cancellation_controls),
                termination_policy=self._termination_policy,
                sentinel_program=self._sentinel_program,
                sentinel_policy=self._sentinel_policy,
                lease_file_descriptors=lease_file_descriptors,
            )
            guardian_arguments = (
                *self._program.arguments,
                "--request-json",
                invocation.model_dump_json(),
            )
            inherited_descriptors = (
                *lease_file_descriptors,
                *self._guardian_cancellation_descriptors(cancellation_controls),
            )
            with _installed_parent_interruption(request.lifecycle) as interruption:
                activation = self._activate_guardian(
                    guardian_arguments,
                    request,
                    pipes.descriptor_mappings(inherited_descriptors),
                )
                if type(activation) is _GuardianActivationContained:
                    raise activation.error
                if type(activation) is _GuardianActivationUncontained:
                    uncontained_process_id = activation.process_id
                    raise activation.error
                if type(activation) is _GuardianActivationTimedOut:
                    return self._activation_timeout_terminal(activation)
                if type(activation) is not _GuardianActivationStarted:
                    raise AssertionError("guardian activation is a closed union")
                guardian = activation.process
                # The guardian now owns inherited copies. Close the outer
                # executor's references before publishing or starting work so
                # a stopped outer process cannot retain machine capacity.
                endpoints = pipes.transfer_parent_endpoints_after_launch()
                owner_ready = self._await_guardian_owner_ready(
                    endpoints.owner_ready_reader.fileno(),
                    request.budget,
                )
                if not owner_ready:
                    if type(request.budget) is not ExecutorGuardianBoundedBudget:
                        raise AssertionError(
                            "only a bounded guardian can expire before readiness"
                        )
                    return ExecutorGuardianActivationTimedOut(
                        request.budget.reason,
                        (),
                    )
                request.lease.transfer_to_guardian()
                cancellation_lease.activate()
                cancellation_lease.transfer_to_owner()
                if interruption.requested:
                    return ExecutorGuardianCommandInterrupted(int(signal.SIGTERM))
                if type(
                    request.budget
                ) is ExecutorGuardianBoundedBudget and request.budget.is_expired_at(
                    time.monotonic()
                ):
                    return ExecutorGuardianActivationTimedOut(
                        request.budget.reason,
                        (),
                    )
                terminal_foreground.grant(guardian.process_id)
                if os.write(
                    endpoints.start_writer.fileno(),
                    GUARDIAN_START_SIGNAL,
                ) != len(GUARDIAN_START_SIGNAL):
                    raise ExecutorGuardianLaunchError(
                        "executor guardian start gate performed a short write"
                    )
                endpoints.start_writer.close()

                supervision = self._process_group_supervisor.supervise(
                    OwnedProcessGroupLeader(guardian.process_id),
                    ProcessGroupUnboundedWait(),
                    interruption,
                )
                group_contained = True
                return self._terminal_after_supervision(
                    supervision,
                    interruption,
                    guardian,
                    endpoints.result_reader.fileno(),
                )
        finally:
            primary_error = sys.exception()
            self._finalize_run(
                guardian,
                uncontained_process_id,
                group_contained,
                cancellation_lease,
                terminal_foreground,
                pipes,
                primary_error,
            )

    @staticmethod
    def _activation_timeout_terminal(
        activation: _GuardianActivationTimedOut,
    ) -> ExecutorGuardianActivationTimedOut:
        terminal = ExecutorGuardianActivationTimedOut(activation.reason, ())
        if activation.recovery_failures:
            raise ExecutorGuardianPostContainmentError(
                terminal,
                activation.recovery_failures,
            )
        return terminal

    def _activate_guardian(
        self,
        guardian_arguments: tuple[str, ...],
        request: ExecutorGuardianRequest,
        descriptor_mappings: tuple[PosixDescriptorMapping, ...],
    ) -> _GuardianActivation:
        launch = self._spawn_guardian(
            guardian_arguments,
            request,
            descriptor_mappings,
        )
        if type(launch) is PosixProcessLaunchStarted:
            return _GuardianActivationStarted(launch.process)
        if type(launch) is PosixProcessLaunchRejected:
            deadline_timeout = self._activation_timeout(
                launch.error,
                request.budget,
            )
            if deadline_timeout is not None:
                return deadline_timeout
            return _GuardianActivationContained(
                self._launch_error(
                    f"could not start executor guardian: {launch.error!r}",
                    launch.error,
                )
            )
        if type(launch) is PosixProcessExecRejected:
            error = launch.as_error()
            return _GuardianActivationContained(
                self._launch_error("executor guardian exec was rejected", error)
            )
        if type(launch) is PosixProcessLaunchRecovered:
            deadline_timeout = self._activation_timeout(
                launch.activation_error,
                request.budget,
            )
            if deadline_timeout is not None:
                return deadline_timeout
            return _GuardianActivationContained(
                self._launch_error(
                    "executor guardian activation was interrupted and contained",
                    launch.activation_error,
                )
            )
        if type(launch) is PosixProcessLaunchRecoveryFailed:
            recovery = BaseExceptionGroup(
                "guardian activation and recovery both failed",
                (launch.activation_error, launch.recovery_error),
            )
            return _GuardianActivationUncontained(
                launch.process_id,
                self._launch_error(
                    "executor guardian activation could not prove containment",
                    recovery,
                ),
            )
        raise AssertionError("POSIX process launch is a closed union")

    @staticmethod
    def _activation_timeout(
        error: BaseException,
        budget: ExecutorGuardianBudget,
    ) -> _GuardianActivationTimedOut | None:
        if type(budget) is ExecutorGuardianUnboundedBudget:
            return None
        if type(budget) is not ExecutorGuardianBoundedBudget:
            raise AssertionError("guardian budget is a closed union")
        evidence = classify_posix_process_activation_deadline(error)
        if type(evidence) is PosixProcessActivationDeadlineAbsent:
            return None
        if type(evidence) is not PosixProcessActivationDeadlinePresent:
            raise AssertionError("activation deadline evidence is a closed union")
        return _GuardianActivationTimedOut(
            budget.reason,
            tuple(
                ExecutorGuardianPostContainmentFailure(
                    "retain guardian activation recovery failure",
                    failure,
                )
                for failure in evidence.recovery_failures
            ),
        )

    @staticmethod
    def _launch_error(
        message: str,
        cause: BaseException,
    ) -> ExecutorGuardianLaunchError:
        error = ExecutorGuardianLaunchError(message)
        error.__cause__ = cause
        return error

    def _finalize_run(
        self,
        guardian: PosixProcessHandle | None,
        uncontained_process_id: int | None,
        group_contained: bool,
        cancellation_lease: ExecutorGuardianCancellationLease,
        terminal_foreground: _ControllingTerminal,
        pipes: GuardianLaunchPipes,
        primary_error: BaseException | None,
    ) -> None:
        containment_cleanup, safe_to_retire = self._contain_after_run(
            guardian,
            uncontained_process_id,
            group_contained,
        )
        cleanup_actions: list[CleanupAction] = []
        if safe_to_retire:
            cleanup_actions.append(
                CleanupAction(
                    "cancellation-endpoint-retirement",
                    cancellation_lease.retire,
                )
            )
        cleanup_actions.extend(
            (
                CleanupAction(
                    "terminal-foreground-restore",
                    terminal_foreground.restore,
                ),
                CleanupAction(
                    "guardian-launch-pipes-close",
                    lambda: self._require_pipes_closed(pipes),
                ),
            )
        )
        resource_cleanup = IndependentCleanupPlan(tuple(cleanup_actions)).run()
        cleanup_errors = self._cleanup_errors(
            containment_cleanup,
            resource_cleanup,
        )
        if not cleanup_errors:
            return
        if primary_error is not None:
            raise BaseExceptionGroup(
                "guardian execution and cleanup failed",
                (primary_error, *cleanup_errors),
            )
        raise BaseExceptionGroup("guardian cleanup failed", cleanup_errors)

    def _contain_after_run(
        self,
        guardian: PosixProcessHandle | None,
        uncontained_process_id: int | None,
        group_contained: bool,
    ) -> tuple[CleanupOutcome, bool]:
        if group_contained:
            return CleanupSucceeded(), True
        if guardian is not None:
            try:
                termination = self._process_group_supervisor.abort(
                    OwnedProcessGroupLeader(guardian.process_id)
                )
            except BaseException as cleanup_error:
                cleanup_error.add_note(
                    "guardian group abort failed; cancellation identity retained"
                )
                return (
                    CleanupFailed(
                        (CleanupFailure("guardian-group-abort", cleanup_error),)
                    ),
                    False,
                )
            try:
                guardian.record_external_reap(termination.leader_exit_code)
            except BaseException as evidence_error:
                evidence_error.add_note(
                    "guardian group was contained before process-handle evidence "
                    "recording failed"
                )
                return (
                    CleanupFailed(
                        (
                            CleanupFailure(
                                "guardian-reap-evidence-record",
                                evidence_error,
                            ),
                        )
                    ),
                    True,
                )
            return (
                self._courtesy_cleanup(
                    termination,
                    "guardian-courtesy-shutdown",
                ),
                True,
            )
        if uncontained_process_id is None:
            return CleanupSucceeded(), True
        try:
            termination = self._process_group_supervisor.abort(
                OwnedProcessGroupLeader(uncontained_process_id)
            )
        except BaseException as cleanup_error:
            cleanup_error.add_note(
                "indeterminate guardian group recovery failed; "
                "cancellation identity retained"
            )
            return (
                CleanupFailed(
                    (
                        CleanupFailure(
                            "indeterminate-guardian-group-abort",
                            cleanup_error,
                        ),
                    )
                ),
                False,
            )
        return (
            self._courtesy_cleanup(
                termination,
                "indeterminate-guardian-courtesy-shutdown",
            ),
            True,
        )

    @staticmethod
    def _courtesy_cleanup(
        termination: ProcessGroupTermination,
        action: str,
    ) -> CleanupOutcome:
        courtesy_failure = termination.courtesy_failure()
        if courtesy_failure is None:
            return CleanupSucceeded()
        return CleanupFailed((CleanupFailure(action, courtesy_failure.error),))

    @staticmethod
    def _cleanup_errors(
        *outcomes: CleanupOutcome,
    ) -> tuple[BaseException, ...]:
        errors: list[BaseException] = []
        for outcome in outcomes:
            if type(outcome) is CleanupSucceeded:
                continue
            if type(outcome) is not CleanupFailed:
                raise AssertionError("cleanup outcome is a closed union")
            errors.extend(failure.error for failure in outcome.failures)
        return tuple(errors)

    def _prepare_cancellation(
        self,
        request: ExecutorGuardianRequest,
        pipes: GuardianLaunchPipes,
    ) -> ExecutorGuardianCancellationLease:
        """Acquire cancellation ownership or roll back all pre-spawn pipes."""
        try:
            return prepare_executor_guardian_cancellation(
                request.cancellation,
                self._record_stores,
            )
        except BaseException as cancellation_error:
            pipe_cleanup_error = self._pipes_close_error(pipes)
            if pipe_cleanup_error is None:
                raise
            raise BaseExceptionGroup(
                "guardian cancellation and pipe cleanup both failed",
                (cancellation_error, pipe_cleanup_error),
            )

    @staticmethod
    def _pipes_close_error(pipes: GuardianLaunchPipes) -> BaseException | None:
        try:
            outcome = pipes.close()
        except BaseException as error:
            return error
        if type(outcome) is GuardianLaunchPipesClosed:
            return None
        if type(outcome) is not GuardianLaunchPipesCloseFailed:
            raise AssertionError("guardian launch pipe close is a closed union")
        return outcome.error

    @classmethod
    def _require_pipes_closed(cls, pipes: GuardianLaunchPipes) -> None:
        error = cls._pipes_close_error(pipes)
        if error is not None:
            raise error

    def _await_guardian_owner_ready(
        self,
        ready_read_file_descriptor: int,
        budget: ExecutorGuardianBudget,
    ) -> bool:
        deadline = time.monotonic() + self._sentinel_policy.startup_timeout_seconds
        if type(budget) is ExecutorGuardianBoundedBudget:
            deadline = min(deadline, budget.expires_at_monotonic)
        elif type(budget) is not ExecutorGuardianUnboundedBudget:
            raise AssertionError("guardian budget is a closed union")
        with selectors.DefaultSelector() as selector:
            selector.register(ready_read_file_descriptor, selectors.EVENT_READ)
            remaining = deadline - time.monotonic()
            ready = selector.select(max(0.0, remaining))
        observed_at_monotonic = time.monotonic()
        if not ready or observed_at_monotonic >= deadline:
            if type(budget) is ExecutorGuardianBoundedBudget and budget.is_expired_at(
                observed_at_monotonic
            ):
                return False
            raise ExecutorGuardianLaunchError(
                "guardian owner did not become ready before its absolute deadline"
            )
        if os.read(ready_read_file_descriptor, 1) != _OWNER_READY_SIGNAL:
            raise ExecutorGuardianLaunchError(
                "guardian exited before publishing exact owner readiness"
            )
        return True

    def _spawn_guardian(
        self,
        guardian_arguments: tuple[str, ...],
        request: ExecutorGuardianRequest,
        descriptor_mappings: tuple[PosixDescriptorMapping, ...],
    ) -> PosixProcessLaunch:
        if request.lifecycle is ExecutorCommandLifecycle.DETACHED:
            group_mode = PosixProcessGroupMode.NEW_SESSION
        elif request.lifecycle is ExecutorCommandLifecycle.INTERACTIVE_SESSION:
            group_mode = PosixProcessGroupMode.NEW_PROCESS_GROUP
        else:
            raise AssertionError("ExecutorCommandLifecycle is a closed enum")
        if type(request.budget) is ExecutorGuardianUnboundedBudget:
            activation_deadline = PosixProcessConfiguredActivationDeadline()
        elif type(request.budget) is ExecutorGuardianBoundedBudget:
            activation_deadline = PosixProcessAbsoluteActivationDeadline(
                request.budget.expires_at_monotonic
            )
        else:
            raise AssertionError("guardian budget is a closed union")
        return self._process_launcher.launch(
            PosixProcessLaunchSpec(
                program=PosixProcessProgram(guardian_arguments),
                working_directory=Path.cwd().resolve(),
                environment=PosixProcessEnvironment.from_mapping(request.environment),
                group_mode=group_mode,
                descriptor_mappings=descriptor_mappings,
                terminal=PosixProcessWithoutTerminal(),
                activation_deadline=activation_deadline,
            )
        )

    @staticmethod
    def _guardian_cancellation_record(
        controls: ExecutorGuardianCancellationControls,
    ) -> GuardianCancellationControlRecord:
        if type(controls) is NoExecutorGuardianCancellationControls:
            return GuardianDetachedCancellationControlRecord()
        if type(controls) is ProcessCancellationOwnerControls:
            return GuardianInteractiveCancellationControlRecord(
                listener_file_descriptor=controls.listener_file_descriptor,
                owner_lock_file_descriptor=controls.owner_lock_file_descriptor,
            )
        raise AssertionError("guardian cancellation controls are a closed union")

    @staticmethod
    def _guardian_cancellation_descriptors(
        controls: ExecutorGuardianCancellationControls,
    ) -> tuple[int, ...]:
        if type(controls) is NoExecutorGuardianCancellationControls:
            return ()
        if type(controls) is ProcessCancellationOwnerControls:
            return (
                controls.listener_file_descriptor,
                controls.owner_lock_file_descriptor,
            )
        raise AssertionError("guardian cancellation controls are a closed union")

    @classmethod
    def _terminal_after_supervision(
        cls,
        supervision: ProcessGroupSupervision,
        interruption: _ParentInterruption,
        guardian: PosixProcessHandle,
        result_read_fd: int,
    ) -> ExecutorGuardianTerminal:
        """Interpret one fully contained guardian supervision outcome."""
        terminal: ExecutorGuardianTerminal | None = None
        terminal_error: BaseException | None = None
        try:
            terminal = cls._terminal_observed_after_supervision(
                supervision,
                result_read_fd,
            )
        except BaseException as error:
            terminal_error = error

        evidence_failures: list[ExecutorGuardianPostContainmentFailure] = []
        try:
            guardian.record_external_reap(supervision.termination.leader_exit_code)
        except BaseException as error:
            evidence_failures.append(
                ExecutorGuardianPostContainmentFailure(
                    "record guardian external reap",
                    error,
                )
            )
        courtesy_failure = supervision.termination.courtesy_failure()
        if courtesy_failure is not None:
            evidence_failures.append(
                ExecutorGuardianPostContainmentFailure(
                    "retain guardian courtesy-wait failure",
                    courtesy_failure.error,
                )
            )
        if terminal_error is not None:
            if not evidence_failures:
                raise terminal_error
            raise BaseExceptionGroup(
                "guardian terminal and post-containment evidence failed",
                (
                    terminal_error,
                    *(failure.error for failure in evidence_failures),
                ),
            )
        if terminal is None:
            raise AssertionError("guardian terminal attempt must publish one outcome")
        if evidence_failures:
            raise ExecutorGuardianPostContainmentError(
                terminal,
                tuple(evidence_failures),
            )
        return terminal

    @classmethod
    def _terminal_observed_after_supervision(
        cls,
        supervision: ProcessGroupSupervision,
        result_read_fd: int,
    ) -> ExecutorGuardianTerminal:
        """Prefer a durable child fact over a racing parent interruption."""
        if type(supervision) is ProcessGroupInterrupted:
            terminal = cls._read_terminal_if_published(result_read_fd)
            if terminal is None:
                return ExecutorGuardianCommandInterrupted(int(signal.SIGTERM))
            cls._require_expected_guardian_exit(
                supervision.termination.leader_exit_code,
                terminal,
                interrupted=True,
            )
            return terminal
        if type(supervision) is not ProcessGroupCompleted:
            raise AssertionError("an unbounded guardian wait cannot time out")
        terminal = cls._read_terminal_if_published(result_read_fd)
        if terminal is None:
            raise ExecutorGuardianProtocolError(
                "executor guardian exited without a terminal record"
            )
        cls._require_expected_guardian_exit(
            supervision.termination.leader_exit_code,
            terminal,
            interrupted=False,
        )
        return terminal

    @classmethod
    def _require_expected_guardian_exit(
        cls,
        guardian_exit_code: int,
        terminal: ExecutorGuardianTerminal,
        *,
        interrupted: bool,
    ) -> None:
        if interrupted and guardian_exit_code == -signal.SIGKILL:
            if type(terminal) is ExecutorGuardianCommandInterrupted:
                raise ExecutorGuardianProtocolError(
                    "executor guardian cannot publish a synthesized interruption record"
                )
            return
        if type(terminal) is ExecutorGuardianCommandInterrupted:
            raise ExecutorGuardianProtocolError(
                "executor guardian cannot publish a synthesized interruption record"
            )
        expected_exit_codes = cls._expected_guardian_exit_codes(terminal)
        if guardian_exit_code not in expected_exit_codes:
            raise ExecutorGuardianProtocolError(
                "executor guardian exit code contradicts its terminal record: "
                f"exit={guardian_exit_code} expected={expected_exit_codes!r}"
            )

    @staticmethod
    def _expected_guardian_exit_codes(
        terminal: ExecutorGuardianTerminal,
    ) -> tuple[int, ...]:
        if type(terminal) in (
            ExecutorGuardianActivationTimedOut,
            ExecutorGuardianCommandStartFailed,
        ):
            return (0,)
        if type(terminal) is ExecutorGuardianInternalFailed:
            return (1, -signal.SIGKILL)
        if type(terminal) in (
            ExecutorGuardianCommandCompleted,
            ExecutorGuardianCommandTimedOut,
        ):
            return (-signal.SIGKILL,)
        raise AssertionError("guardian terminal is a closed union")

    @staticmethod
    def _read_terminal_if_published(
        result_read_fd: int,
    ) -> ExecutorGuardianTerminal | None:
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            chunk = os.read(result_read_fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
            total_bytes += len(chunk)
            if total_bytes > _MAX_RESULT_BYTES:
                raise ExecutorGuardianProtocolError(
                    "executor guardian terminal record exceeds size limit"
                )
        payload = b"".join(chunks)
        if not payload:
            return None
        try:
            record = GUARDIAN_TERMINAL_ADAPTER.validate_json(payload, strict=True)
        except ValidationError as error:
            raise ExecutorGuardianProtocolError(
                "executor guardian emitted a malformed terminal record"
            ) from error
        return record.to_domain()
