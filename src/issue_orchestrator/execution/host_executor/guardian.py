# pyright: strict
"""Child-side command worker protected by an independent group sentinel."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

from pydantic import ValidationError

from ...domain.executor import ExecutorCommandLifecycle
from ...domain.executor_child_resources import ExecutorChildResourceSnapshot
from ...domain.posix_process import (
    PosixProcessActivationDeadlineExceededError,
    PosixProcessAbsoluteActivationDeadline,
    PosixProcessActivationDeadlineAbsent,
    PosixProcessActivationDeadlinePresent,
    PosixProcessConfiguredActivationDeadline,
    PosixProcessEnvironment,
    PosixProcessJoinGroup,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
    classify_posix_process_activation_deadline,
)
from ...domain.executor_guardian import (
    ExecutorGuardianActivationTimedOut,
    ExecutorGuardianBoundedBudget,
    ExecutorGuardianBudget,
    ExecutorGuardianCommandCompleted,
    ExecutorGuardianCommandResourceUsage,
    ExecutorGuardianCommandStartFailed,
    ExecutorGuardianCommandTimedOut,
    ExecutorGuardianInternalFailed,
    ExecutorGuardianResourceObservationFailed,
    ExecutorGuardianSerializedFailure,
    ExecutorGuardianTerminal,
    ExecutorGuardianTerminationPolicy,
    ExecutorGuardianUnboundedBudget,
)
from ...domain.process_group_sentinel import ProcessGroupSentinelParentLifetime
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
from ...ports.executor_child_resources import ExecutorChildResourceObserver
from ..process_cancellation_endpoint import ProcessCancellationOwnerControls
from ..posix_process import descriptor_path
from ..process_group_sentinel import (
    ProcessGroupSentinelController,
    ProcessGroupSentinelWithoutCancellation,
)
from ._guardian_contracts import (
    GUARDIAN_START_SIGNAL,
    GuardianDetachedCancellationControlRecord,
    GuardianInteractiveCancellationControlRecord,
    GuardianInvocationRecord,
    guardian_terminal_record,
)
from .guardian_terminal_observation import (
    ExecutorGuardianCommandDeadlineObserved,
    ExecutorGuardianCommandExitObserved,
    ExecutorGuardianCommandObservationFailed,
    ExecutorGuardianTerminalObservationOwner,
    SystemExecutorGuardianObservationClock,
)


_MAX_TERMINAL_RECORD_BYTES = 4096
_COMMAND_POLL_SECONDS = 0.05
_OWNER_READY_SIGNAL = b"R"


def _require_child_resource_observer(
    value: object,
) -> ExecutorChildResourceObserver:
    if not isinstance(value, ExecutorChildResourceObserver):
        raise ValueError(
            "PosixExecutorGuardianChild.child_resource_observer must implement "
            "ExecutorChildResourceObserver"
        )
    return value


def _retain_lease_until_contained(
    signal_number: int,
    frame: FrameType | None,
) -> None:
    """Keep TERM from destroying the worker before its sentinel contains all."""
    del signal_number, frame


@dataclass(slots=True)
class _GuardianResultWriter:
    """Write one bounded terminal record and close the private result channel."""

    file_descriptor: int
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if type(self.file_descriptor) is not int or self.file_descriptor < 0:
            raise ValueError("guardian result descriptor must be non-negative")

    def write(self, terminal: ExecutorGuardianTerminal) -> None:
        if self._closed:
            raise RuntimeError("guardian result channel is already closed")
        record = guardian_terminal_record(terminal)
        payload = record.model_dump_json().encode("utf-8") + b"\n"
        if len(payload) > _MAX_TERMINAL_RECORD_BYTES:
            raise RuntimeError("guardian terminal record exceeds size limit")
        try:
            written = os.write(self.file_descriptor, payload)
            if written != len(payload):
                raise RuntimeError("guardian result channel performed a short write")
        finally:
            os.close(self.file_descriptor)
            self._closed = True


@dataclass(frozen=True, slots=True)
class _GuardianCommandTerminal:
    terminal: ExecutorGuardianTerminal

    def __post_init__(self) -> None:
        if type(self.terminal) not in (
            ExecutorGuardianCommandCompleted,
            ExecutorGuardianCommandTimedOut,
            ExecutorGuardianInternalFailed,
        ):
            raise ValueError("_GuardianCommandTerminal requires a terminal fact")


@dataclass(frozen=True, slots=True)
class _ResolvedCommandArguments:
    arguments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _RejectedCommandResolution:
    guardian_exit_code: int


_CommandResolution = _ResolvedCommandArguments | _RejectedCommandResolution


@dataclass(frozen=True, slots=True)
class _StartedCommandProcess:
    process: PosixProcessHandle


_CommandActivation = _StartedCommandProcess | _RejectedCommandResolution


@dataclass(frozen=True, slots=True)
class _GuardianResourceMeasurementStarted:
    started_at_monotonic: float
    child_resources_before: ExecutorChildResourceSnapshot


@dataclass(frozen=True, slots=True)
class _GuardianResourceMeasurementUnavailable:
    error_type: str
    error_repr: str


_GuardianResourceMeasurement = (
    _GuardianResourceMeasurementStarted | _GuardianResourceMeasurementUnavailable
)


@dataclass(slots=True)
class _SentinelGuardianGroupOwner:
    """Dual guardian/sentinel owner that survives either single failure."""

    controller: ProcessGroupSentinelController
    cancellation_record_path: Path | None = None
    lease_file_descriptors: tuple[int, ...] = ()

    def retire_before_opaque_work(self) -> None:
        self.controller.retire_without_group()

    def require_sentinel_alive(self) -> None:
        self.controller.require_alive()

    def contain(
        self,
        terminal: ExecutorGuardianTerminal,
        termination_policy: ExecutorGuardianTerminationPolicy,
    ) -> None:
        del terminal
        # This group is now unconditionally dying by its own hand.  Retiring
        # the cancellation record first lets later stop requests observe a
        # completed owner as ABSENT; a record that outlives its guardian then
        # always means the guardian died without containing its work.
        if self.cancellation_record_path is not None:
            try:
                payload = json.loads(
                    self.cancellation_record_path.read_text(encoding="utf-8")
                )
                endpoint = payload.get("endpoint")
                if isinstance(endpoint, str) and endpoint:
                    Path(endpoint).unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            try:
                self.cancellation_record_path.unlink(missing_ok=True)
            except OSError:
                pass
        # The guardian is the crash-safe lease owner: with the group dying,
        # the charge ends here, so the lease record must not outlive it.
        # Only per-command records under leases/ are retired; shared capacity
        # locks travel on the same descriptors and must survive.
        try:
            self.controller.request_containment()
        except BaseException:
            # The guardian deliberately retains the lease descriptors, so a
            # dead sentinel cannot uncharge this work. The guardian becomes
            # the containment owner for the same exact process group.
            pass
        self._contain_from_guardian(termination_policy)

    @staticmethod
    def _contain_from_guardian(
        termination_policy: ExecutorGuardianTerminationPolicy,
    ) -> None:
        errors: list[BaseException] = []
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except BaseException as error:
            errors.append(error)
        deadline = time.monotonic() + termination_policy.graceful_shutdown_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                time.sleep(remaining)
            except InterruptedError:
                continue
            except BaseException as error:
                errors.append(error)
                break
        try:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        except BaseException as force_error:
            raise BaseExceptionGroup(
                "guardian could not forcefully contain its process group",
                (*errors, force_error),
            )
        raise AssertionError("guardian SIGKILL unexpectedly returned")


_GuardianGroupOwner = _SentinelGuardianGroupOwner


class PosixExecutorGuardianChild:
    """Run one opaque command while a minimal sibling owns its group identity."""

    def __init__(
        self,
        termination_policy: ExecutorGuardianTerminationPolicy,
        process_launcher: PosixProcessLauncher,
        child_resource_observer: ExecutorChildResourceObserver,
        terminal_observation_owner: ExecutorGuardianTerminalObservationOwner,
    ) -> None:
        if type(termination_policy) is not ExecutorGuardianTerminationPolicy:
            raise ValueError(
                "PosixExecutorGuardianChild.termination_policy must be an "
                "ExecutorGuardianTerminationPolicy"
            )
        self._termination_policy = termination_policy
        if not callable(getattr(process_launcher, "launch", None)):
            raise ValueError(
                "PosixExecutorGuardianChild.process_launcher must implement "
                "PosixProcessLauncher"
            )
        self._process_launcher = process_launcher
        self._child_resource_observer = _require_child_resource_observer(
            child_resource_observer
        )
        if type(terminal_observation_owner) is not (
            ExecutorGuardianTerminalObservationOwner
        ):
            raise ValueError(
                "PosixExecutorGuardianChild.terminal_observation_owner must be an "
                "ExecutorGuardianTerminalObservationOwner"
            )
        self._terminal_observation_owner = terminal_observation_owner

    def run(
        self,
        invocation: GuardianInvocationRecord,
        result_writer: _GuardianResultWriter,
    ) -> int:
        """Run the command; every started path ends in whole-group containment."""
        if type(invocation) is not GuardianInvocationRecord:
            raise ValueError("guardian requires its typed invocation")
        if type(result_writer) is not _GuardianResultWriter:
            raise ValueError("guardian requires its typed result writer")
        signal.signal(signal.SIGTERM, _retain_lease_until_contained)
        if invocation.lifecycle is ExecutorCommandLifecycle.INTERACTIVE_SESSION:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        elif invocation.lifecycle is not ExecutorCommandLifecycle.DETACHED:
            raise AssertionError("ExecutorCommandLifecycle is a closed enum")

        try:
            group_owner = self._build_group_owner(invocation)
        except BaseException as error:
            try:
                result_writer.write(
                    ExecutorGuardianInternalFailed(type(error).__name__, repr(error))
                )
            finally:
                os.close(invocation.owner_ready_file_descriptor)
            return 1
        self._publish_owner_ready(invocation.owner_ready_file_descriptor)
        try:
            self._await_start(invocation.start_file_descriptor)
        except BaseException as error:
            try:
                result_writer.write(
                    ExecutorGuardianInternalFailed(type(error).__name__, repr(error))
                )
            finally:
                group_owner.retire_before_opaque_work()
            return 1
        budget = invocation.domain_budget()
        if type(budget) is ExecutorGuardianBoundedBudget and budget.is_expired_at(
            time.monotonic()
        ):
            terminal = ExecutorGuardianActivationTimedOut(budget.reason, ())
            try:
                result_writer.write(terminal)
            finally:
                group_owner.retire_before_opaque_work()
            return 0
        return self._run_started_command(invocation, result_writer, group_owner)

    @staticmethod
    def _publish_owner_ready(file_descriptor: int) -> None:
        try:
            if os.write(file_descriptor, _OWNER_READY_SIGNAL) != len(
                _OWNER_READY_SIGNAL
            ):
                raise RuntimeError(
                    "guardian owner readiness channel performed a short write"
                )
        finally:
            os.close(file_descriptor)

    def _build_group_owner(
        self,
        invocation: GuardianInvocationRecord,
    ) -> _GuardianGroupOwner:
        cancellation = invocation.cancellation
        cancellation_record_path: Path | None = None
        if type(cancellation) is GuardianDetachedCancellationControlRecord:
            sentinel_cancellation = ProcessGroupSentinelWithoutCancellation()
            cancellation_descriptors: tuple[int, ...] = ()
        elif type(cancellation) is GuardianInteractiveCancellationControlRecord:
            cancellation_record_path = Path(cancellation.record_path)
            controls = ProcessCancellationOwnerControls(
                cancellation.listener_file_descriptor,
                cancellation.owner_lock_file_descriptor,
                cancellation_record_path,
            )
            sentinel_cancellation = controls
            cancellation_descriptors = (
                controls.listener_file_descriptor,
                controls.owner_lock_file_descriptor,
            )
        else:
            raise AssertionError("guardian cancellation is a closed union")
        controller = ProcessGroupSentinelController.start_with_parent_lifetime(
            invocation.process_group_sentinel_program(),
            sentinel_cancellation,
            invocation.process_group_sentinel_policy(),
            invocation.lease_file_descriptors,
            ProcessGroupSentinelParentLifetime(
                invocation.parent_lifetime_read_file_descriptor
            ),
            self._process_launcher,
        )
        cleanup_errors: list[BaseException] = []
        for descriptor in (
            *cancellation_descriptors,
            invocation.parent_lifetime_read_file_descriptor,
        ):
            try:
                os.close(descriptor)
            except BaseException as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            try:
                controller.abort_before_opaque_work()
            except BaseException as error:
                cleanup_errors.append(error)
            raise BaseExceptionGroup(
                "guardian could not transfer cancellation ownership to sentinel",
                cleanup_errors,
            )
        return _SentinelGuardianGroupOwner(
            controller,
            cancellation_record_path,
            invocation.lease_file_descriptors,
        )

    def _run_started_command(
        self,
        invocation: GuardianInvocationRecord,
        result_writer: _GuardianResultWriter,
        group_owner: _GuardianGroupOwner,
    ) -> int:
        budget = invocation.domain_budget()
        resolution = self._resolve_command(
            invocation.arguments,
            result_writer,
            group_owner,
        )
        if type(resolution) is _RejectedCommandResolution:
            return resolution.guardian_exit_code
        if type(resolution) is not _ResolvedCommandArguments:
            raise AssertionError("command resolution is a closed union")
        arguments = resolution.arguments
        if type(budget) is ExecutorGuardianBoundedBudget and budget.is_expired_at(
            time.monotonic()
        ):
            terminal = ExecutorGuardianActivationTimedOut(budget.reason, ())
            try:
                result_writer.write(terminal)
            finally:
                group_owner.retire_before_opaque_work()
            return 0
        resource_measurement = self._start_resource_measurement()
        activation = self._activate_resolved_command(
            arguments,
            budget,
            result_writer,
            group_owner,
        )
        if type(activation) is _RejectedCommandResolution:
            return activation.guardian_exit_code
        if type(activation) is not _StartedCommandProcess:
            raise AssertionError("command activation is a closed union")
        process = activation.process

        outcome = self._wait_for_outcome(
            process,
            budget,
            group_owner,
            resource_measurement,
        )
        try:
            result_writer.write(outcome.terminal)
        finally:
            group_owner.contain(
                outcome.terminal,
                self._termination_policy,
            )
        raise AssertionError("group containment unexpectedly returned to guardian")

    def _activate_resolved_command(
        self,
        arguments: tuple[str, ...],
        budget: ExecutorGuardianBudget,
        result_writer: _GuardianResultWriter,
        group_owner: _GuardianGroupOwner,
    ) -> _CommandActivation:
        if type(budget) is ExecutorGuardianUnboundedBudget:
            activation_deadline = PosixProcessConfiguredActivationDeadline()
        elif type(budget) is ExecutorGuardianBoundedBudget:
            activation_deadline = PosixProcessAbsoluteActivationDeadline(
                budget.expires_at_monotonic
            )
        else:
            raise AssertionError("guardian budget is a closed union")
        try:
            launch = self._process_launcher.launch(
                PosixProcessLaunchSpec(
                    program=PosixProcessProgram(arguments),
                    working_directory=Path.cwd().resolve(),
                    environment=PosixProcessEnvironment.from_mapping(os.environ),
                    group_mode=PosixProcessJoinGroup(os.getpgrp()),
                    descriptor_mappings=(),
                    terminal=PosixProcessWithoutTerminal(),
                    activation_deadline=activation_deadline,
                )
            )
        except BaseException as error:
            try:
                result_writer.write(
                    ExecutorGuardianInternalFailed(type(error).__name__, repr(error))
                )
            finally:
                group_owner.contain(
                    ExecutorGuardianInternalFailed(type(error).__name__, repr(error)),
                    self._termination_policy,
                )
            raise AssertionError("guardian containment unexpectedly returned")
        if type(launch) is PosixProcessLaunchRecoveryFailed:
            if type(budget) is ExecutorGuardianBoundedBudget and isinstance(
                launch.activation_error,
                PosixProcessActivationDeadlineExceededError,
            ):
                # The budget legitimately expired mid-activation; the joined
                # child needs full-group containment, but the terminal is a
                # deadline outcome, not a guardian defect.
                terminal = ExecutorGuardianActivationTimedOut(
                    budget.reason,
                    (
                        ExecutorGuardianSerializedFailure(
                            "recover activation-expired opaque command",
                            type(launch.recovery_error).__name__,
                            repr(launch.recovery_error),
                        ),
                    ),
                )
            else:
                recovery = BaseExceptionGroup(
                    "opaque command activation and recovery failed",
                    (launch.activation_error, launch.recovery_error),
                )
                terminal = ExecutorGuardianInternalFailed(
                    type(recovery).__name__,
                    repr(recovery),
                )
            try:
                result_writer.write(terminal)
            finally:
                group_owner.contain(terminal, self._termination_policy)
            raise AssertionError("guardian containment unexpectedly returned")
        if type(launch) in (
            PosixProcessLaunchRejected,
            PosixProcessExecRejected,
            PosixProcessLaunchRecovered,
        ):
            terminal = self._contained_activation_terminal(
                launch,
                arguments[0],
                budget,
            )
            try:
                result_writer.write(terminal)
            finally:
                group_owner.retire_before_opaque_work()
            return _RejectedCommandResolution(
                self._activation_terminal_exit_code(terminal)
            )
        if type(launch) is PosixProcessLaunchStarted:
            return _StartedCommandProcess(launch.process)
        raise AssertionError("opaque command launch is a closed union")

    @classmethod
    def _contained_activation_terminal(
        cls,
        launch: PosixProcessLaunch,
        executable: str,
        budget: ExecutorGuardianBudget,
    ) -> (
        ExecutorGuardianActivationTimedOut
        | ExecutorGuardianCommandStartFailed
        | ExecutorGuardianInternalFailed
    ):
        if type(launch) is PosixProcessLaunchRejected:
            return cls._activation_terminal(launch.error, executable, budget)
        if type(launch) is PosixProcessExecRejected:
            return ExecutorGuardianCommandStartFailed(
                launch.error_type,
                launch.error_repr,
            )
        if type(launch) is PosixProcessLaunchRecovered:
            return cls._activation_terminal(
                launch.activation_error,
                executable,
                budget,
            )
        raise AssertionError("contained command activation is a closed union")

    @staticmethod
    def _activation_terminal_exit_code(
        terminal: (
            ExecutorGuardianActivationTimedOut
            | ExecutorGuardianCommandStartFailed
            | ExecutorGuardianInternalFailed
        ),
    ) -> int:
        if type(terminal) in (
            ExecutorGuardianActivationTimedOut,
            ExecutorGuardianCommandStartFailed,
        ):
            return 0
        if type(terminal) is ExecutorGuardianInternalFailed:
            return 1
        raise AssertionError("activation terminal is a closed union")

    @classmethod
    def _activation_terminal(
        cls,
        error: BaseException,
        executable: str,
        budget: ExecutorGuardianBudget,
    ) -> (
        ExecutorGuardianActivationTimedOut
        | ExecutorGuardianCommandStartFailed
        | ExecutorGuardianInternalFailed
    ):
        if type(budget) is ExecutorGuardianBoundedBudget:
            evidence = classify_posix_process_activation_deadline(error)
            if type(evidence) is PosixProcessActivationDeadlinePresent:
                return ExecutorGuardianActivationTimedOut(
                    budget.reason,
                    tuple(
                        ExecutorGuardianSerializedFailure(
                            "opaque command activation recovery",
                            type(failure).__name__,
                            repr(failure),
                        )
                        for failure in evidence.recovery_failures
                    ),
                )
            if type(evidence) is not PosixProcessActivationDeadlineAbsent:
                raise AssertionError("activation deadline evidence is a closed union")
        elif type(budget) is not ExecutorGuardianUnboundedBudget:
            raise AssertionError("guardian budget is a closed union")
        return cls._rejected_launch_terminal(error, executable)

    @staticmethod
    def _rejected_launch_terminal(
        error: BaseException,
        executable: str,
    ) -> ExecutorGuardianCommandStartFailed | ExecutorGuardianInternalFailed:
        if isinstance(error, OSError):
            return ExecutorGuardianCommandStartFailed(
                type(error).__name__,
                f"{error!r}; executable={executable!r}",
            )
        return ExecutorGuardianInternalFailed(type(error).__name__, repr(error))

    @staticmethod
    def _require_arguments(arguments: tuple[str, ...]) -> None:
        if type(arguments) is not tuple or not arguments:
            raise ValueError("executor guardian arguments must be a non-empty tuple")
        if any(type(argument) is not str for argument in arguments):
            raise ValueError("executor guardian arguments must contain strings")

    @staticmethod
    def _resolved_arguments(arguments: tuple[str, ...]) -> tuple[str, ...]:
        """Resolve standard command names against the exact inherited PATH."""
        executable = arguments[0]
        executable_path = Path(executable)
        if executable_path.is_absolute():
            resolved_executable = executable
        elif "/" in executable:
            resolved_executable = os.path.abspath(executable)
        else:
            search_path = os.environ.get("PATH")
            if search_path is None:
                raise FileNotFoundError(
                    f"cannot resolve executor command without PATH: {executable!r}"
                )
            match = shutil.which(executable, path=search_path)
            if match is None:
                raise FileNotFoundError(
                    f"executor command is not present on PATH: {executable!r}"
                )
            resolved_executable = os.path.abspath(match)
        return (resolved_executable, *arguments[1:])

    def _resolve_command(
        self,
        requested_arguments: tuple[str, ...],
        result_writer: _GuardianResultWriter,
        group_owner: _GuardianGroupOwner,
    ) -> _CommandResolution:
        self._require_arguments(requested_arguments)
        try:
            return _ResolvedCommandArguments(
                self._resolved_arguments(requested_arguments)
            )
        except OSError as error:
            terminal = self._rejected_launch_terminal(
                error,
                requested_arguments[0],
            )
            try:
                result_writer.write(terminal)
            finally:
                group_owner.retire_before_opaque_work()
            return _RejectedCommandResolution(
                0 if type(terminal) is ExecutorGuardianCommandStartFailed else 1
            )

    @staticmethod
    def _await_start(start_file_descriptor: int) -> None:
        if type(start_file_descriptor) is not int or start_file_descriptor < 0:
            raise ValueError("guardian start descriptor must be non-negative")
        try:
            signal_byte = os.read(start_file_descriptor, 1)
        finally:
            os.close(start_file_descriptor)
        if signal_byte != GUARDIAN_START_SIGNAL:
            raise RuntimeError("executor guardian start gate closed without a grant")

    def _start_resource_measurement(self) -> _GuardianResourceMeasurement:
        try:
            return _GuardianResourceMeasurementStarted(
                time.monotonic(),
                self._child_resource_observer.observe(),
            )
        except BaseException as error:
            return _GuardianResourceMeasurementUnavailable(
                type(error).__name__,
                repr(error),
            )

    def _completed_terminal(
        self,
        exit_code: int,
        measurement: _GuardianResourceMeasurement,
    ) -> ExecutorGuardianCommandCompleted:
        if type(measurement) is _GuardianResourceMeasurementUnavailable:
            resources = ExecutorGuardianResourceObservationFailed(
                measurement.error_type,
                measurement.error_repr,
            )
            return ExecutorGuardianCommandCompleted(exit_code, resources)
        if type(measurement) is not _GuardianResourceMeasurementStarted:
            raise AssertionError("guardian resource measurement is a closed union")
        try:
            usage_after = self._child_resource_observer.observe()
            resources = ExecutorGuardianCommandResourceUsage(
                wall_seconds=time.monotonic() - measurement.started_at_monotonic,
                cpu_seconds=(
                    usage_after.user_cpu_seconds
                    - measurement.child_resources_before.user_cpu_seconds
                )
                + (
                    usage_after.system_cpu_seconds
                    - measurement.child_resources_before.system_cpu_seconds
                ),
                guardian_process_lifetime_children_max_rss_bytes=(
                    usage_after.process_lifetime_children_max_rss_bytes
                ),
                input_blocks=max(
                    0,
                    usage_after.input_blocks
                    - measurement.child_resources_before.input_blocks,
                ),
                output_blocks=max(
                    0,
                    usage_after.output_blocks
                    - measurement.child_resources_before.output_blocks,
                ),
            )
        except BaseException as error:
            resources = ExecutorGuardianResourceObservationFailed(
                type(error).__name__,
                repr(error),
            )
        return ExecutorGuardianCommandCompleted(exit_code, resources)

    def _wait_for_outcome(
        self,
        process: PosixProcessHandle,
        budget: ExecutorGuardianBudget,
        group_owner: _GuardianGroupOwner,
        resource_measurement: _GuardianResourceMeasurement,
    ) -> _GuardianCommandTerminal:
        observation = self._terminal_observation_owner.observe(
            process,
            budget,
            group_owner,
        )
        if type(observation) is ExecutorGuardianCommandExitObserved:
            return _GuardianCommandTerminal(
                self._completed_terminal(
                    observation.exit_code,
                    resource_measurement,
                )
            )
        if type(observation) is ExecutorGuardianCommandDeadlineObserved:
            return _GuardianCommandTerminal(
                ExecutorGuardianCommandTimedOut(observation.reason)
            )
        if type(observation) is ExecutorGuardianCommandObservationFailed:
            return _GuardianCommandTerminal(
                ExecutorGuardianInternalFailed(
                    observation.error_type,
                    observation.error_repr,
                )
            )
        raise AssertionError("guardian command observation is a closed union")


def _parse_invocation(raw_request: str) -> GuardianInvocationRecord:
    try:
        return GuardianInvocationRecord.model_validate_json(raw_request)
    except ValidationError as error:
        raise ValueError("invalid executor guardian invocation") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one executor guardian")
    parser.add_argument("--request-json", required=True)
    arguments = parser.parse_args()
    invocation = _parse_invocation(arguments.request_json)
    from ...entrypoints.bootstrap import (
        build_executor_child_resource_observer,
        build_posix_process_launcher,
    )

    return PosixExecutorGuardianChild(
        invocation.termination_policy(),
        build_posix_process_launcher(),
        build_executor_child_resource_observer(),
        ExecutorGuardianTerminalObservationOwner(
            SystemExecutorGuardianObservationClock(),
            _COMMAND_POLL_SECONDS,
        ),
    ).run(
        invocation,
        _GuardianResultWriter(invocation.result_file_descriptor),
    )


if __name__ == "__main__":
    raise SystemExit(main())
