# pyright: strict
"""Deep owner for one crash-resilient, in-group containment sentinel."""

from __future__ import annotations

import argparse
import os
import selectors
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from ..domain.process_group_sentinel import (
    ProcessGroupSentinelPolicy,
    ProcessGroupSentinelProgram,
)
from .strict_wire_record import StrictWireRecord
from .process_cancellation_endpoint import (
    ProcessCancellationOwner,
    ProcessCancellationOwnerControls,
    ProcessCancellationRequest,
)
from .independent_cleanup import (
    CleanupAction,
    IndependentCleanupPlan,
    raise_cleanup_failures,
    raise_primary_with_cleanup,
)


_READY = b"R"
_CONTAIN = b"K"
_RETIRE = b"X"
_CANCELLATION_MARKER = "cancellation"
_CONTROLLER_MARKER = "controller"


class ProcessGroupSentinelError(RuntimeError):
    """The in-group sentinel violated its lifecycle contract."""


@dataclass(frozen=True, slots=True)
class ProcessGroupSentinelWithoutCancellation:
    """Explicit sentinel mode with no durable external endpoint."""


ProcessGroupSentinelCancellation = (
    ProcessGroupSentinelWithoutCancellation | ProcessCancellationOwnerControls
)


class _SentinelWithoutCancellationRecord(StrictWireRecord):
    kind: Literal["without-cancellation"] = "without-cancellation"


class _SentinelCancellationEndpointRecord(StrictWireRecord):
    kind: Literal["cancellation-endpoint"] = "cancellation-endpoint"
    listener_file_descriptor: int = Field(ge=0)
    owner_lock_file_descriptor: int = Field(ge=0)


_SentinelCancellationRecord = Annotated[
    _SentinelWithoutCancellationRecord | _SentinelCancellationEndpointRecord,
    Field(discriminator="kind"),
]


class _ProcessGroupSentinelInvocation(StrictWireRecord):
    schema_version: Literal[1] = 1
    cancellation: _SentinelCancellationRecord
    controller_file_descriptor: int = Field(ge=0)
    ready_file_descriptor: int = Field(ge=0)
    process_group_id: int = Field(gt=1)
    graceful_shutdown_seconds: float = Field(gt=0)


@dataclass(slots=True)
class ProcessGroupSentinelController:
    """Parent-side authority over one exact sentinel child and control channel."""

    _child: subprocess.Popen[bytes]
    _connection: socket.socket
    _startup_timeout_seconds: float
    _transferred_to_exec: bool = False
    _closed: bool = False

    @classmethod
    def start(
        cls,
        program: ProcessGroupSentinelProgram,
        cancellation: ProcessGroupSentinelCancellation,
        policy: ProcessGroupSentinelPolicy,
        lifetime_file_descriptors: tuple[int, ...],
    ) -> ProcessGroupSentinelController:
        """Start a sentinel in the caller's current process group and await it."""
        if type(program) is not ProcessGroupSentinelProgram:
            raise ValueError("sentinel program must be ProcessGroupSentinelProgram")
        if type(cancellation) not in (
            ProcessGroupSentinelWithoutCancellation,
            ProcessCancellationOwnerControls,
        ):
            raise ValueError(
                "sentinel cancellation must be an explicit typed contract"
            )
        if type(policy) is not ProcessGroupSentinelPolicy:
            raise ValueError("sentinel policy must be ProcessGroupSentinelPolicy")
        if os.getpid() != os.getpgrp():
            raise ProcessGroupSentinelError(
                "sentinel controller requires an isolated process-group leader"
            )
        if type(lifetime_file_descriptors) is not tuple or any(
            type(descriptor) is not int or descriptor < 0
            for descriptor in lifetime_file_descriptors
        ):
            raise ValueError(
                "sentinel lifetime_file_descriptors must be non-negative integers"
            )
        if type(cancellation) is ProcessGroupSentinelWithoutCancellation:
            cancellation_record: _SentinelCancellationRecord = (
                _SentinelWithoutCancellationRecord()
            )
            cancellation_descriptors: tuple[int, ...] = ()
        elif type(cancellation) is ProcessCancellationOwnerControls:
            cancellation_record = _SentinelCancellationEndpointRecord(
                listener_file_descriptor=cancellation.listener_file_descriptor,
                owner_lock_file_descriptor=cancellation.owner_lock_file_descriptor,
            )
            cancellation_descriptors = (
                cancellation.listener_file_descriptor,
                cancellation.owner_lock_file_descriptor,
            )
        else:
            raise AssertionError("sentinel cancellation is a closed union")
        inherited_descriptors = (
            *cancellation_descriptors,
            *lifetime_file_descriptors,
        )
        if len(set(inherited_descriptors)) != len(inherited_descriptors):
            raise ValueError("sentinel inherited descriptors must be unique")

        parent_connection, child_connection = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        ready_read_fd, ready_write_fd = os.pipe()
        child: subprocess.Popen[bytes] | None = None
        child_connection_open = True
        ready_read_open = True
        ready_write_open = True
        try:
            invocation = _ProcessGroupSentinelInvocation(
                cancellation=cancellation_record,
                controller_file_descriptor=child_connection.fileno(),
                ready_file_descriptor=ready_write_fd,
                process_group_id=os.getpgrp(),
                graceful_shutdown_seconds=policy.graceful_shutdown_seconds,
            )
            child = subprocess.Popen(
                (
                    *program.arguments,
                    "--request-json",
                    invocation.model_dump_json(),
                ),
                pass_fds=(
                    *inherited_descriptors,
                    child_connection.fileno(),
                    ready_write_fd,
                ),
            )
            child_connection.close()
            child_connection_open = False
            os.close(ready_write_fd)
            ready_write_open = False
            cls._await_ready(ready_read_fd, child, policy.startup_timeout_seconds)
            os.close(ready_read_fd)
            ready_read_open = False
            return cls(child, parent_connection, policy.startup_timeout_seconds)
        except BaseException as primary_error:
            cleanup_actions: list[CleanupAction] = []
            if child is not None:
                cleanup_actions.append(
                    CleanupAction(
                        "exact-sentinel-kill-and-reap",
                        lambda: cls._kill_and_reap_exact_child(
                            child,
                            policy.startup_timeout_seconds,
                        ),
                    )
                )
            if child_connection_open:
                cleanup_actions.append(
                    CleanupAction("child-controller-close", child_connection.close)
                )
            cleanup_actions.append(
                CleanupAction("parent-controller-close", parent_connection.close)
            )
            if ready_write_open:
                cleanup_actions.append(
                    CleanupAction(
                        "readiness-writer-close",
                        lambda: os.close(ready_write_fd),
                    )
                )
            if ready_read_open:
                cleanup_actions.append(
                    CleanupAction(
                        "readiness-reader-close",
                        lambda: os.close(ready_read_fd),
                    )
                )
            raise_primary_with_cleanup(
                "sentinel startup and cleanup failed",
                primary_error,
                IndependentCleanupPlan(tuple(cleanup_actions)).run(),
            )

    def transfer_to_exec(self) -> None:
        """Keep the control lifetime open across the caller's imminent exec."""
        self._require_open()
        if self._transferred_to_exec:
            raise RuntimeError("sentinel controller was transferred twice")
        os.set_inheritable(self._connection.fileno(), True)
        self._transferred_to_exec = True

    def request_containment(self) -> None:
        """Ask the sentinel to contain the complete group, including this caller."""
        self._send_exact(_CONTAIN)

    def require_alive(self) -> None:
        """Fail if the exact sentinel child no longer protects its controller."""
        self._require_open()
        return_code = self._child.poll()
        if return_code is None:
            return
        self._connection.close()
        self._closed = True
        raise ProcessGroupSentinelError(
            "process-group sentinel exited while opaque work remained active: "
            f"pid={self._child.pid} exit_code={return_code}"
        )

    def retire_without_group(self) -> None:
        """Retire before opaque work exists, then reap the exact sentinel child."""
        try:
            self._send_exact(_RETIRE)
        except BaseException as primary_error:
            cleanup = IndependentCleanupPlan(
                (
                    CleanupAction(
                        "exact-sentinel-kill-and-reap",
                        lambda: self._kill_and_reap_exact_child(
                            self._child,
                            self._startup_timeout_seconds,
                        ),
                    ),
                    CleanupAction("controller-close", self._connection.close),
                )
            ).run()
            self._closed = True
            raise_primary_with_cleanup(
                "sentinel retirement request and cleanup failed",
                primary_error,
                cleanup,
            )
        cleanup = IndependentCleanupPlan(
            (
                CleanupAction("controller-close", self._connection.close),
                CleanupAction(
                    "sentinel-reap",
                    lambda: self._child.wait(
                        timeout=self._startup_timeout_seconds
                    ),
                ),
            )
        ).run()
        self._closed = True
        raise_cleanup_failures("sentinel retirement cleanup failed", cleanup)
        if self._child.returncode != 0:
            raise ProcessGroupSentinelError(
                "sentinel did not retire cleanly before opaque work"
            )

    def abort_before_opaque_work(self) -> None:
        """Kill only the exact sentinel child during pre-exec rollback."""
        if self._closed:
            return
        cleanup = IndependentCleanupPlan(
            (
                CleanupAction(
                    "exact-sentinel-kill-and-reap",
                    lambda: self._kill_and_reap_exact_child(
                        self._child,
                        self._startup_timeout_seconds,
                    ),
                ),
                CleanupAction("controller-close", self._connection.close),
            )
        ).run()
        self._closed = True
        raise_cleanup_failures("sentinel abort cleanup failed", cleanup)

    def _send_exact(self, payload: bytes) -> None:
        self._require_open()
        written = self._connection.send(payload)
        if written != len(payload):
            raise ProcessGroupSentinelError(
                "sentinel controller performed a short write"
            )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("sentinel controller is closed")

    @staticmethod
    def _await_ready(
        ready_read_file_descriptor: int,
        child: subprocess.Popen[bytes],
        timeout_seconds: float,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        with selectors.DefaultSelector() as selector:
            selector.register(ready_read_file_descriptor, selectors.EVENT_READ)
            remaining = deadline - time.monotonic()
            ready = selector.select(max(0.0, remaining))
        if not ready or time.monotonic() >= deadline:
            raise ProcessGroupSentinelError(
                "sentinel did not become ready before its absolute deadline"
            )
        if os.read(ready_read_file_descriptor, 1) != _READY:
            child.poll()
            raise ProcessGroupSentinelError(
                "sentinel exited before publishing exact readiness"
            )

    @staticmethod
    def _kill_and_reap_exact_child(
        child: subprocess.Popen[bytes],
        timeout_seconds: float,
    ) -> None:
        actions: list[CleanupAction] = []
        if child.poll() is None:
            actions.append(CleanupAction("exact-sentinel-kill", child.kill))
        actions.append(
            CleanupAction(
                "exact-sentinel-reap",
                lambda: child.wait(timeout=timeout_seconds),
            )
        )
        raise_cleanup_failures(
            "exact sentinel kill/reap failed",
            IndependentCleanupPlan(tuple(actions)).run(),
        )


@dataclass(slots=True)
class RedundantProcessGroupSentinelController:
    """Two independent sentinels protecting one opaque exec boundary."""

    _first: ProcessGroupSentinelController
    _second: ProcessGroupSentinelController

    @classmethod
    def start(
        cls,
        program: ProcessGroupSentinelProgram,
        cancellation: ProcessGroupSentinelCancellation,
        policy: ProcessGroupSentinelPolicy,
        lifetime_file_descriptors: tuple[int, ...],
    ) -> RedundantProcessGroupSentinelController:
        first = ProcessGroupSentinelController.start(
            program,
            cancellation,
            policy,
            lifetime_file_descriptors,
        )
        try:
            second = ProcessGroupSentinelController.start(
                program,
                cancellation,
                policy,
                lifetime_file_descriptors,
            )
        except BaseException as startup_error:
            raise_primary_with_cleanup(
                "redundant sentinel startup and rollback failed",
                startup_error,
                IndependentCleanupPlan(
                    (
                        CleanupAction(
                            "abort first redundant sentinel",
                            first.abort_before_opaque_work,
                        ),
                    )
                ).run(),
            )
        return cls(first, second)

    def transfer_to_exec(self) -> None:
        """Keep both independent control lifetimes through the caller's exec."""
        try:
            self._first.transfer_to_exec()
            self._second.transfer_to_exec()
        except BaseException as transfer_error:
            raise_primary_with_cleanup(
                "redundant sentinel transfer and rollback failed",
                transfer_error,
                self._abort_plan().run(),
            )

    def abort_before_opaque_work(self) -> None:
        """Retire both exact sentinel children during pre-exec rollback."""
        raise_cleanup_failures(
            "redundant sentinel abort failed",
            self._abort_plan().run(),
        )

    def _abort_plan(self) -> IndependentCleanupPlan:
        return IndependentCleanupPlan(
            (
                CleanupAction(
                    "abort second redundant sentinel",
                    self._second.abort_before_opaque_work,
                ),
                CleanupAction(
                    "abort first redundant sentinel",
                    self._first.abort_before_opaque_work,
                ),
            )
        )


class _NoProcessCancellationOwner:
    """Explicit no-endpoint implementation for detached guardian sentinels."""

    def register(self, selector: selectors.BaseSelector, marker: object) -> None:
        del selector, marker

    def consume_ready(
        self,
        selector: selectors.BaseSelector,
        selectable: object,
        marker: object,
    ) -> ProcessCancellationRequest | None:
        del selector, selectable, marker
        raise RuntimeError("sentinel without cancellation has no endpoint events")

    def close(self) -> None:
        pass


_SentinelCancellationOwner = ProcessCancellationOwner | _NoProcessCancellationOwner


def _cancellation_owner(
    record: _SentinelCancellationRecord,
) -> _SentinelCancellationOwner:
    if type(record) is _SentinelWithoutCancellationRecord:
        return _NoProcessCancellationOwner()
    if type(record) is _SentinelCancellationEndpointRecord:
        return ProcessCancellationOwner(
            ProcessCancellationOwnerControls(
                record.listener_file_descriptor,
                record.owner_lock_file_descriptor,
            )
        )
    raise AssertionError("sentinel cancellation record is a closed union")


class _ProcessGroupSentinelChild:
    """Minimal in-group owner that survives its controller's failure."""

    def run(self, invocation: _ProcessGroupSentinelInvocation) -> int:
        if os.getpgrp() != invocation.process_group_id:
            raise ProcessGroupSentinelError(
                "sentinel did not join its requested process group"
            )
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        controller = socket.socket(fileno=invocation.controller_file_descriptor)
        controller.setblocking(False)
        owner = _cancellation_owner(invocation.cancellation)
        try:
            self._publish_ready(invocation.ready_file_descriptor)
            return self._serve(invocation, owner, controller)
        finally:
            controller.close()
            owner.close()

    @staticmethod
    def _publish_ready(ready_file_descriptor: int) -> None:
        try:
            if os.write(ready_file_descriptor, _READY) != len(_READY):
                raise ProcessGroupSentinelError(
                    "sentinel readiness channel performed a short write"
                )
        finally:
            os.close(ready_file_descriptor)

    @classmethod
    def _serve(
        cls,
        invocation: _ProcessGroupSentinelInvocation,
        owner: _SentinelCancellationOwner,
        controller: socket.socket,
    ) -> int:
        with selectors.DefaultSelector() as selector:
            selector.register(controller, selectors.EVENT_READ, _CONTROLLER_MARKER)
            owner.register(selector, _CANCELLATION_MARKER)
            while True:
                for key, _events in selector.select():
                    if key.data == _CANCELLATION_MARKER:
                        request = owner.consume_ready(
                            selector,
                            key.fileobj,
                            _CANCELLATION_MARKER,
                        )
                        if request is not None:
                            cls._contain_group(invocation, request)
                    elif key.data == _CONTROLLER_MARKER:
                        command = controller.recv(2)
                        if command == _RETIRE:
                            return 0
                        if command in (b"", _CONTAIN):
                            cls._contain_group(invocation, None)
                        cls._contain_group(invocation, None)
                    else:
                        raise AssertionError("sentinel selector marker is closed")
        raise AssertionError("sentinel event loop unexpectedly returned")

    @staticmethod
    def _contain_group(
        invocation: _ProcessGroupSentinelInvocation,
        request: ProcessCancellationRequest | None,
    ) -> None:
        errors: list[BaseException] = []
        try:
            try:
                os.killpg(invocation.process_group_id, signal.SIGTERM)
            except BaseException as error:
                errors.append(error)
            deadline = time.monotonic() + invocation.graceful_shutdown_seconds
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
            if request is not None:
                try:
                    request.acknowledge()
                except BaseException as error:
                    errors.append(error)
        finally:
            try:
                os.killpg(invocation.process_group_id, signal.SIGKILL)
            except BaseException as force_error:
                raise BaseExceptionGroup(
                    "sentinel could not forcefully contain its process group",
                    (*errors, force_error),
                )
        if errors:
            raise BaseExceptionGroup(
                "sentinel observed failures before process-group SIGKILL",
                errors,
            )
        raise AssertionError("SIGKILL unexpectedly returned to sentinel")


def _parse_invocation(raw_request: str) -> _ProcessGroupSentinelInvocation:
    try:
        return _ProcessGroupSentinelInvocation.model_validate_json(raw_request)
    except ValidationError as error:
        raise ValueError("invalid process-group sentinel invocation") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Own one POSIX process group")
    parser.add_argument("--request-json", required=True)
    arguments = parser.parse_args()
    return _ProcessGroupSentinelChild().run(
        _parse_invocation(arguments.request_json)
    )


if __name__ == "__main__":
    raise SystemExit(main())
