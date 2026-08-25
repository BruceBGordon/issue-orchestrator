# pyright: strict
"""Child-side command worker protected by an independent group sentinel."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from types import FrameType

from pydantic import ValidationError

from ...domain.executor import ExecutorCommandLifecycle
from ...domain.executor_guardian import (
    ExecutorGuardianBoundedBudget,
    ExecutorGuardianBudget,
    ExecutorGuardianCommandCompleted,
    ExecutorGuardianCommandStartFailed,
    ExecutorGuardianCommandTimedOut,
    ExecutorGuardianInternalFailed,
    ExecutorGuardianTerminal,
    ExecutorGuardianTerminationPolicy,
    ExecutorGuardianUnboundedBudget,
)
from ..process_cancellation_endpoint import ProcessCancellationOwnerControls
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


_MAX_TERMINAL_RECORD_BYTES = 4096
_COMMAND_POLL_SECONDS = 0.05
_OWNER_READY_SIGNAL = b"R"


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


@dataclass(slots=True)
class _SentinelGuardianGroupOwner:
    """Dual guardian/sentinel owner that survives either single failure."""

    controller: ProcessGroupSentinelController

    def retire_before_opaque_work(self) -> None:
        self.controller.retire_without_group()

    def require_sentinel_alive(self) -> None:
        self.controller.require_alive()

    def contain(
        self,
        process: subprocess.Popen[bytes],
        terminal: ExecutorGuardianTerminal,
        termination_policy: ExecutorGuardianTerminationPolicy,
    ) -> None:
        del process, terminal
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
        deadline = (
            time.monotonic()
            + termination_policy.graceful_shutdown_seconds
        )
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
    ) -> None:
        if type(termination_policy) is not ExecutorGuardianTerminationPolicy:
            raise ValueError(
                "PosixExecutorGuardianChild.termination_policy must be an "
                "ExecutorGuardianTerminationPolicy"
            )
        self._termination_policy = termination_policy

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

    @staticmethod
    def _build_group_owner(
        invocation: GuardianInvocationRecord,
    ) -> _GuardianGroupOwner:
        cancellation = invocation.cancellation
        if type(cancellation) is GuardianDetachedCancellationControlRecord:
            sentinel_cancellation = ProcessGroupSentinelWithoutCancellation()
            cancellation_descriptors: tuple[int, ...] = ()
        elif type(cancellation) is GuardianInteractiveCancellationControlRecord:
            controls = ProcessCancellationOwnerControls(
                cancellation.listener_file_descriptor,
                cancellation.owner_lock_file_descriptor,
            )
            sentinel_cancellation = controls
            cancellation_descriptors = (
                controls.listener_file_descriptor,
                controls.owner_lock_file_descriptor,
            )
        else:
            raise AssertionError("guardian cancellation is a closed union")
        controller = ProcessGroupSentinelController.start(
            invocation.process_group_sentinel_program(),
            sentinel_cancellation,
            invocation.process_group_sentinel_policy(),
            invocation.lease_file_descriptors,
        )
        cleanup_errors: list[BaseException] = []
        for descriptor in cancellation_descriptors:
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
        return _SentinelGuardianGroupOwner(controller)

    def _run_started_command(
        self,
        invocation: GuardianInvocationRecord,
        result_writer: _GuardianResultWriter,
        group_owner: _GuardianGroupOwner,
    ) -> int:
        arguments = invocation.arguments
        self._require_arguments(arguments)
        try:
            process = subprocess.Popen(list(arguments), close_fds=True)
        except OSError as error:
            try:
                result_writer.write(
                    ExecutorGuardianCommandStartFailed(
                        type(error).__name__,
                        f"{error!r}; executable={arguments[0]!r}",
                    )
                )
            finally:
                group_owner.retire_before_opaque_work()
            return 0
        except BaseException as error:
            try:
                result_writer.write(
                    ExecutorGuardianInternalFailed(type(error).__name__, repr(error))
                )
            finally:
                group_owner.retire_before_opaque_work()
            return 1

        outcome = self._wait_for_outcome(
            process,
            invocation.domain_budget(),
            group_owner,
        )
        try:
            result_writer.write(outcome.terminal)
        finally:
            group_owner.contain(
                process,
                outcome.terminal,
                self._termination_policy,
            )
        raise AssertionError("group containment unexpectedly returned to guardian")

    @staticmethod
    def _require_arguments(arguments: tuple[str, ...]) -> None:
        if type(arguments) is not tuple or not arguments:
            raise ValueError("executor guardian arguments must be a non-empty tuple")
        if any(type(argument) is not str for argument in arguments):
            raise ValueError("executor guardian arguments must contain strings")

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

    @classmethod
    def _wait_for_outcome(
        cls,
        process: subprocess.Popen[bytes],
        budget: ExecutorGuardianBudget,
        group_owner: _GuardianGroupOwner,
    ) -> _GuardianCommandTerminal:
        if type(budget) is ExecutorGuardianUnboundedBudget:
            deadline: float | None = None
        elif type(budget) is ExecutorGuardianBoundedBudget:
            deadline = time.monotonic() + budget.timeout_seconds
        else:
            raise AssertionError("guardian budget is a closed union")
        try:
            while True:
                group_owner.require_sentinel_alive()
                return_code = process.poll()
                if return_code is not None:
                    return _GuardianCommandTerminal(
                        ExecutorGuardianCommandCompleted(return_code)
                    )
                sleep_seconds = _COMMAND_POLL_SECONDS
                if deadline is not None:
                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        if type(budget) is not ExecutorGuardianBoundedBudget:
                            raise AssertionError(
                                "only a bounded budget can reach its deadline"
                            )
                        return _GuardianCommandTerminal(
                            ExecutorGuardianCommandTimedOut(budget.reason)
                        )
                    sleep_seconds = min(sleep_seconds, remaining_seconds)
                time.sleep(sleep_seconds)
        except BaseException as error:
            return _GuardianCommandTerminal(
                ExecutorGuardianInternalFailed(type(error).__name__, repr(error))
            )


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
    return PosixExecutorGuardianChild(invocation.termination_policy()).run(
        invocation,
        _GuardianResultWriter(invocation.result_file_descriptor),
    )


if __name__ == "__main__":
    raise SystemExit(main())
