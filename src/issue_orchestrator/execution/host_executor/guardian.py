# pyright: strict
"""Child-side lease guardian for one opaque executor command group."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
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
from ._guardian_contracts import (
    GUARDIAN_START_SIGNAL,
    GuardianInvocationRecord,
    guardian_terminal_record,
)


_MAX_TERMINAL_RECORD_BYTES = 4096


def _retain_lease_until_contained(
    signal_number: int,
    frame: FrameType | None,
) -> None:
    """Keep TERM from destroying the sole lease owner before containment."""
    del signal_number, frame


def _await_start(start_file_descriptor: int) -> None:
    """Do not spawn opaque work before the launcher establishes ownership."""
    if type(start_file_descriptor) is not int or start_file_descriptor < 0:
        raise ValueError("guardian start descriptor must be non-negative")
    try:
        signal_byte = os.read(start_file_descriptor, 1)
    finally:
        os.close(start_file_descriptor)
    if signal_byte != GUARDIAN_START_SIGNAL:
        raise RuntimeError("executor guardian start gate closed without a grant")


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


class PosixExecutorGuardianChild:
    """Hold lease FDs while enforcing deadline and containing the opaque group."""

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
        arguments: tuple[str, ...],
        budget: ExecutorGuardianBudget,
        result_writer: _GuardianResultWriter,
        lifecycle: ExecutorCommandLifecycle,
    ) -> int:
        """Run the command; started-command paths end by killing this group."""
        self._require_request(arguments, budget, result_writer, lifecycle)
        # A caught disposition resets to default across exec, unlike SIG_IGN.
        # The guardian therefore survives TERM while its opaque child retains
        # the normal TERM behavior expected by command cleanup.
        signal.signal(signal.SIGTERM, _retain_lease_until_contained)
        if lifecycle is ExecutorCommandLifecycle.INTERACTIVE_SESSION:
            # Exiting a PTY session leader sends SIGHUP to its foreground group.
            # Interactive guardians and their opaque commands must outlive an
            # accidental outer-wrapper crash; deliberate stop uses SIGTERM.
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        elif lifecycle is not ExecutorCommandLifecycle.DETACHED:
            raise AssertionError("ExecutorCommandLifecycle is a closed enum")
        try:
            process = subprocess.Popen(list(arguments), close_fds=True)
        except OSError as error:
            result_writer.write(
                ExecutorGuardianCommandStartFailed(
                    type(error).__name__,
                    f"{error!r}; executable={arguments[0]!r}",
                )
            )
            return 0
        except BaseException as error:
            result_writer.write(
                ExecutorGuardianInternalFailed(type(error).__name__, repr(error))
            )
            return 1

        terminal = self._wait_for_terminal(process, budget)
        try:
            result_writer.write(terminal)
        finally:
            self._contain_own_process_group(process, terminal)
        raise AssertionError("SIGKILL unexpectedly returned to executor guardian")

    @staticmethod
    def _require_request(
        arguments: tuple[str, ...],
        budget: ExecutorGuardianBudget,
        result_writer: _GuardianResultWriter,
        lifecycle: ExecutorCommandLifecycle,
    ) -> None:
        if type(arguments) is not tuple or not arguments:
            raise ValueError("executor guardian arguments must be a non-empty tuple")
        if any(type(argument) is not str for argument in arguments):
            raise ValueError("executor guardian arguments must contain strings")
        if type(budget) not in (
            ExecutorGuardianUnboundedBudget,
            ExecutorGuardianBoundedBudget,
        ):
            raise ValueError("executor guardian requires an explicit budget")
        if type(result_writer) is not _GuardianResultWriter:
            raise ValueError("executor guardian requires its typed result writer")
        if type(lifecycle) is not ExecutorCommandLifecycle:
            raise ValueError("executor guardian requires a typed command lifecycle")

    @staticmethod
    def _wait_for_terminal(
        process: subprocess.Popen[bytes],
        budget: ExecutorGuardianBudget,
    ) -> ExecutorGuardianTerminal:
        try:
            if type(budget) is ExecutorGuardianUnboundedBudget:
                return ExecutorGuardianCommandCompleted(process.wait())
            if type(budget) is ExecutorGuardianBoundedBudget:
                try:
                    return ExecutorGuardianCommandCompleted(
                        process.wait(timeout=budget.timeout_seconds)
                    )
                except subprocess.TimeoutExpired:
                    return ExecutorGuardianCommandTimedOut(budget.reason)
            raise AssertionError("guardian budget is a closed union")
        except BaseException as error:
            return ExecutorGuardianInternalFailed(type(error).__name__, repr(error))

    def _contain_own_process_group(
        self,
        process: subprocess.Popen[bytes],
        terminal: ExecutorGuardianTerminal,
    ) -> None:
        # The opaque child inherited the default TERM disposition. Install the
        # guardian's immunity only after spawn, then retain this live group
        # leader as the PGID reservation through the unconditional group KILL.
        process_group_id = os.getpgrp()
        try:
            os.killpg(process_group_id, signal.SIGTERM)
            if type(terminal) is ExecutorGuardianCommandTimedOut:
                try:
                    process.wait(
                        timeout=self._termination_policy.graceful_shutdown_seconds
                    )
                except subprocess.TimeoutExpired:
                    pass
        finally:
            os.killpg(process_group_id, signal.SIGKILL)


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
    result_writer = _GuardianResultWriter(invocation.result_file_descriptor)
    try:
        _await_start(invocation.start_file_descriptor)
    except BaseException as error:
        result_writer.write(
            ExecutorGuardianInternalFailed(type(error).__name__, repr(error))
        )
        return 1
    return PosixExecutorGuardianChild(invocation.termination_policy()).run(
        invocation.arguments,
        invocation.domain_budget(),
        result_writer,
        invocation.lifecycle,
    )


if __name__ == "__main__":
    raise SystemExit(main())
