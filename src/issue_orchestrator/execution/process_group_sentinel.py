# pyright: strict
"""Deep owner for one crash-resilient, in-group containment sentinel."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from ..domain.process_group_sentinel import (
    ProcessGroupSentinelParentLifetime,
    ProcessGroupSentinelPolicy,
    ProcessGroupSentinelProgram,
)
from ..domain.posix_process import (
    PosixDescriptorMapping,
    PosixProcessConfiguredActivationDeadline,
    PosixProcessEnvironment,
    PosixProcessJoinGroup,
    PosixProcessLaunchSpec,
    PosixProcessProgram,
    PosixProcessWithoutTerminal,
)
from ..ports.posix_process import (
    PosixProcessExecRejected,
    PosixProcessHandle,
    PosixProcessLauncher,
    PosixProcessLaunchRecovered,
    PosixProcessLaunchRecoveryFailed,
    PosixProcessLaunchRejected,
    PosixProcessLaunchStarted,
)
from .strict_wire_record import StrictWireRecord
from .process_cancellation_endpoint import (
    ProcessCancellationOwner,
    ProcessCancellationOwnerControls,
    ProcessCancellationRequest,
)
from .posix_process import descriptor_path
from ..domain.independent_cleanup import (
    CleanupAction,
    CleanupOutcome,
    IndependentCleanupPlan,
    raise_cleanup_failures,
    raise_primary_with_cleanup,
)


_READY = b"R"
_CONTAIN = b"K"
_RETIRE = b"X"
_CANCELLATION_MARKER = "cancellation"
_CONTROLLER_MARKER = "controller"
_PARENT_LIFETIME_MARKER = "parent-lifetime"


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
    record_path: str = Field(min_length=1)


_SentinelCancellationRecord = Annotated[
    _SentinelWithoutCancellationRecord | _SentinelCancellationEndpointRecord,
    Field(discriminator="kind"),
]


class _SentinelWithoutParentLifetimeRecord(StrictWireRecord):
    kind: Literal["without-parent-lifetime"] = "without-parent-lifetime"


class _SentinelParentLifetimeRecord(StrictWireRecord):
    kind: Literal["parent-lifetime"] = "parent-lifetime"
    read_file_descriptor: int = Field(ge=0)


_SentinelParentLifetimeRecordUnion = Annotated[
    _SentinelWithoutParentLifetimeRecord | _SentinelParentLifetimeRecord,
    Field(discriminator="kind"),
]


class _ProcessGroupSentinelInvocation(StrictWireRecord):
    schema_version: Literal[1] = 1
    cancellation: _SentinelCancellationRecord
    controller_file_descriptor: int = Field(ge=0)
    ready_file_descriptor: int = Field(ge=0)
    process_group_id: int = Field(gt=1)
    graceful_shutdown_seconds: float = Field(gt=0)
    parent_lifetime: _SentinelParentLifetimeRecordUnion
    lease_file_descriptors: tuple[int, ...] = ()


class _SentinelLaunchResources:
    """Incrementally own both controller sockets and the readiness pipe."""

    def __init__(self) -> None:
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            readiness_reader, readiness_writer = os.pipe()
        except BaseException as acquisition_error:
            cleanup = IndependentCleanupPlan(
                (
                    CleanupAction("parent-controller-close", parent.close),
                    CleanupAction("child-controller-close", child.close),
                )
            ).run()
            raise_primary_with_cleanup(
                "sentinel readiness acquisition and socket cleanup failed",
                acquisition_error,
                cleanup,
            )
        self._parent = parent
        self._child = child
        self._readiness_reader = readiness_reader
        self._readiness_writer = readiness_writer
        self._parent_owned = True
        self._child_owned = True
        self._readiness_reader_owned = True
        self._readiness_writer_owned = True

    @property
    def child_controller_descriptor(self) -> int:
        return self._child.fileno()

    @property
    def readiness_writer_descriptor(self) -> int:
        return self._readiness_writer

    @property
    def readiness_reader_descriptor(self) -> int:
        return self._readiness_reader

    def close_child_endpoints_after_launch(self) -> None:
        actions = (
            CleanupAction("child-controller-close", self._child.close),
            CleanupAction(
                "readiness-writer-close",
                lambda: os.close(self._readiness_writer),
            ),
        )
        self._child_owned = False
        self._readiness_writer_owned = False
        raise_cleanup_failures(
            "sentinel child endpoint transfer failed",
            IndependentCleanupPlan(actions).run(),
        )

    def transfer_parent_after_ready(self) -> socket.socket:
        if not self._parent_owned or not self._readiness_reader_owned:
            raise RuntimeError("sentinel parent resources were already transferred")
        self._readiness_reader_owned = False
        os.close(self._readiness_reader)
        self._parent_owned = False
        return self._parent

    def close(self) -> CleanupOutcome:
        actions: list[CleanupAction] = []
        if self._parent_owned:
            actions.append(CleanupAction("parent-controller-close", self._parent.close))
            self._parent_owned = False
        if self._child_owned:
            actions.append(CleanupAction("child-controller-close", self._child.close))
            self._child_owned = False
        if self._readiness_reader_owned:
            actions.append(
                CleanupAction(
                    "readiness-reader-close",
                    lambda: os.close(self._readiness_reader),
                )
            )
            self._readiness_reader_owned = False
        if self._readiness_writer_owned:
            actions.append(
                CleanupAction(
                    "readiness-writer-close",
                    lambda: os.close(self._readiness_writer),
                )
            )
            self._readiness_writer_owned = False
        return IndependentCleanupPlan(tuple(actions)).run()


@dataclass(frozen=True, slots=True)
class _SentinelActivationStarted:
    process: PosixProcessHandle


@dataclass(frozen=True, slots=True)
class _SentinelActivationContained:
    error: BaseException


@dataclass(frozen=True, slots=True)
class _SentinelActivationUncontained:
    process_id: int
    error: BaseException


_SentinelActivation = (
    _SentinelActivationStarted
    | _SentinelActivationContained
    | _SentinelActivationUncontained
)


@dataclass(slots=True)
class ProcessGroupSentinelController:
    """Parent-side authority over one exact sentinel child and control channel."""

    _child: PosixProcessHandle
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
        process_launcher: PosixProcessLauncher,
    ) -> ProcessGroupSentinelController:
        """Start a sentinel in the caller's current process group and await it."""
        return cls._start(
            program,
            cancellation,
            policy,
            lifetime_file_descriptors,
            _SentinelWithoutParentLifetimeRecord(),
            process_launcher,
        )

    @classmethod
    def start_with_parent_lifetime(
        cls,
        program: ProcessGroupSentinelProgram,
        cancellation: ProcessGroupSentinelCancellation,
        policy: ProcessGroupSentinelPolicy,
        lifetime_file_descriptors: tuple[int, ...],
        parent_lifetime: ProcessGroupSentinelParentLifetime,
        process_launcher: PosixProcessLauncher,
    ) -> ProcessGroupSentinelController:
        """Start a sentinel that also contains on exact parent-lifetime EOF."""
        if type(parent_lifetime) is not ProcessGroupSentinelParentLifetime:
            raise ValueError("sentinel parent_lifetime must be typed")
        return cls._start(
            program,
            cancellation,
            policy,
            lifetime_file_descriptors,
            _SentinelParentLifetimeRecord(
                read_file_descriptor=parent_lifetime.read_file_descriptor
            ),
            process_launcher,
        )

    @classmethod
    def _start(
        cls,
        program: ProcessGroupSentinelProgram,
        cancellation: ProcessGroupSentinelCancellation,
        policy: ProcessGroupSentinelPolicy,
        lifetime_file_descriptors: tuple[int, ...],
        parent_lifetime: _SentinelParentLifetimeRecordUnion,
        process_launcher: PosixProcessLauncher,
    ) -> ProcessGroupSentinelController:
        cls._validate_start_contract(
            program,
            cancellation,
            policy,
            lifetime_file_descriptors,
            process_launcher,
        )
        cancellation_record, cancellation_descriptors = cls._cancellation_record(
            cancellation
        )
        parent_lifetime_descriptors = cls._parent_lifetime_descriptors(parent_lifetime)
        inherited_descriptors = (
            *cancellation_descriptors,
            *lifetime_file_descriptors,
            *parent_lifetime_descriptors,
        )
        if len(set(inherited_descriptors)) != len(inherited_descriptors):
            raise ValueError("sentinel inherited descriptors must be unique")
        resources = _SentinelLaunchResources()
        child: PosixProcessHandle | None = None
        uncontained_process_id: int | None = None
        try:
            invocation = _ProcessGroupSentinelInvocation(
                cancellation=cancellation_record,
                controller_file_descriptor=(resources.child_controller_descriptor),
                ready_file_descriptor=resources.readiness_writer_descriptor,
                process_group_id=os.getpgrp(),
                graceful_shutdown_seconds=policy.graceful_shutdown_seconds,
                parent_lifetime=parent_lifetime,
                lease_file_descriptors=lifetime_file_descriptors,
            )
            activation = cls._activate(
                program,
                invocation,
                inherited_descriptors,
                resources,
                process_launcher,
            )
            if type(activation) is _SentinelActivationContained:
                raise activation.error
            if type(activation) is _SentinelActivationUncontained:
                uncontained_process_id = activation.process_id
                raise activation.error
            if type(activation) is not _SentinelActivationStarted:
                raise AssertionError("sentinel activation is a closed union")
            child = activation.process
            resources.close_child_endpoints_after_launch()
            cls._await_ready(
                resources.readiness_reader_descriptor,
                child,
                policy.startup_timeout_seconds,
            )
            connection = resources.transfer_parent_after_ready()
            return cls(child, connection, policy.startup_timeout_seconds)
        except BaseException as primary_error:
            raise_primary_with_cleanup(
                "sentinel startup and cleanup failed",
                primary_error,
                cls._startup_cleanup(
                    child,
                    uncontained_process_id,
                    resources,
                    policy.startup_timeout_seconds,
                ),
            )

    @staticmethod
    def _parent_lifetime_descriptors(
        parent_lifetime: _SentinelParentLifetimeRecordUnion,
    ) -> tuple[int, ...]:
        if type(parent_lifetime) is _SentinelWithoutParentLifetimeRecord:
            return ()
        if type(parent_lifetime) is _SentinelParentLifetimeRecord:
            return (parent_lifetime.read_file_descriptor,)
        raise AssertionError("sentinel parent lifetime is a closed union")

    @staticmethod
    def _validate_start_contract(
        program: ProcessGroupSentinelProgram,
        cancellation: ProcessGroupSentinelCancellation,
        policy: ProcessGroupSentinelPolicy,
        lifetime_file_descriptors: tuple[int, ...],
        process_launcher: PosixProcessLauncher,
    ) -> None:
        if type(program) is not ProcessGroupSentinelProgram:
            raise ValueError("sentinel program must be ProcessGroupSentinelProgram")
        if type(cancellation) not in (
            ProcessGroupSentinelWithoutCancellation,
            ProcessCancellationOwnerControls,
        ):
            raise ValueError("sentinel cancellation must be a typed contract")
        if type(policy) is not ProcessGroupSentinelPolicy:
            raise ValueError("sentinel policy must be ProcessGroupSentinelPolicy")
        if not callable(getattr(process_launcher, "launch", None)):
            raise ValueError(
                "sentinel process_launcher must implement PosixProcessLauncher"
            )
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

    @staticmethod
    def _cancellation_record(
        cancellation: ProcessGroupSentinelCancellation,
    ) -> tuple[_SentinelCancellationRecord, tuple[int, ...]]:
        if type(cancellation) is ProcessGroupSentinelWithoutCancellation:
            return _SentinelWithoutCancellationRecord(), ()
        if type(cancellation) is ProcessCancellationOwnerControls:
            return (
                _SentinelCancellationEndpointRecord(
                    listener_file_descriptor=cancellation.listener_file_descriptor,
                    owner_lock_file_descriptor=cancellation.owner_lock_file_descriptor,
                    record_path=str(cancellation.record_path),
                ),
                (
                    cancellation.listener_file_descriptor,
                    cancellation.owner_lock_file_descriptor,
                ),
            )
        raise AssertionError("sentinel cancellation is a closed union")

    @staticmethod
    def _activate(
        program: ProcessGroupSentinelProgram,
        invocation: _ProcessGroupSentinelInvocation,
        inherited_descriptors: tuple[int, ...],
        resources: _SentinelLaunchResources,
        process_launcher: PosixProcessLauncher,
    ) -> _SentinelActivation:
        descriptors = (
            *inherited_descriptors,
            resources.child_controller_descriptor,
            resources.readiness_writer_descriptor,
        )
        launch = process_launcher.launch(
            PosixProcessLaunchSpec(
                program=PosixProcessProgram(
                    (
                        *program.arguments,
                        "--request-json",
                        invocation.model_dump_json(),
                    )
                ),
                working_directory=Path.cwd().resolve(),
                environment=PosixProcessEnvironment.from_mapping(os.environ),
                group_mode=PosixProcessJoinGroup(os.getpgrp()),
                descriptor_mappings=tuple(
                    PosixDescriptorMapping(descriptor, descriptor)
                    for descriptor in descriptors
                ),
                terminal=PosixProcessWithoutTerminal(),
                activation_deadline=PosixProcessConfiguredActivationDeadline(),
            )
        )
        if type(launch) is PosixProcessLaunchStarted:
            return _SentinelActivationStarted(launch.process)
        if type(launch) is PosixProcessLaunchRejected:
            error = ProcessGroupSentinelError(
                f"sentinel activation was rejected: {launch.error!r}"
            )
            error.__cause__ = launch.error
            return _SentinelActivationContained(error)
        if type(launch) is PosixProcessExecRejected:
            return _SentinelActivationContained(launch.as_error())
        if type(launch) is PosixProcessLaunchRecovered:
            error = ProcessGroupSentinelError(
                "sentinel activation was interrupted and contained"
            )
            error.__cause__ = launch.activation_error
            return _SentinelActivationContained(error)
        if type(launch) is not PosixProcessLaunchRecoveryFailed:
            raise AssertionError("sentinel launch is a closed union")
        recovery = BaseExceptionGroup(
            "sentinel activation and recovery failed",
            (launch.activation_error, launch.recovery_error),
        )
        error = ProcessGroupSentinelError(
            "sentinel activation could not prove exact-child containment"
        )
        error.__cause__ = recovery
        return _SentinelActivationUncontained(launch.process_id, error)

    @classmethod
    def _startup_cleanup(
        cls,
        child: PosixProcessHandle | None,
        uncontained_process_id: int | None,
        resources: _SentinelLaunchResources,
        timeout_seconds: float,
    ) -> CleanupOutcome:
        actions: list[CleanupAction] = []
        if child is not None:
            started_child = child
            actions.append(
                CleanupAction(
                    "exact-sentinel-kill-and-reap",
                    lambda: cls._kill_and_reap_exact_child(
                        started_child,
                        timeout_seconds,
                    ),
                )
            )
        elif uncontained_process_id is not None:
            process_id = uncontained_process_id
            actions.append(
                CleanupAction(
                    "uncontained-exact-sentinel-kill-and-reap",
                    lambda: cls._kill_and_reap_exact_process_id(
                        process_id,
                        timeout_seconds,
                    ),
                )
            )
        actions.append(
            CleanupAction(
                "sentinel-launch-resources-close",
                lambda: cls._require_resources_closed(resources),
            )
        )
        return IndependentCleanupPlan(tuple(actions)).run()

    @staticmethod
    def _require_resources_closed(resources: _SentinelLaunchResources) -> None:
        raise_cleanup_failures(
            "sentinel launch resource cleanup failed",
            resources.close(),
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
            f"pid={self._child.process_id} exit_code={return_code}"
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
                        timeout_seconds=self._startup_timeout_seconds
                    ),
                ),
            )
        ).run()
        self._closed = True
        raise_cleanup_failures("sentinel retirement cleanup failed", cleanup)
        if self._child.return_code != 0:
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
        child: PosixProcessHandle,
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
        child: PosixProcessHandle,
        timeout_seconds: float,
    ) -> None:
        actions: list[CleanupAction] = []
        if child.poll() is None:
            actions.append(CleanupAction("exact-sentinel-kill", child.kill))
        actions.append(
            CleanupAction(
                "exact-sentinel-reap",
                lambda: child.wait(timeout_seconds=timeout_seconds),
            )
        )
        raise_cleanup_failures(
            "exact sentinel kill/reap failed",
            IndependentCleanupPlan(tuple(actions)).run(),
        )

    @staticmethod
    def _kill_and_reap_exact_process_id(
        process_id: int,
        timeout_seconds: float,
    ) -> None:
        try:
            os.kill(process_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                reaped_id, _status = os.waitpid(process_id, os.WNOHANG)
            except ChildProcessError:
                return
            if reaped_id == process_id:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"uncontained sentinel {process_id} was not reaped")
            time.sleep(min(0.01, deadline - time.monotonic()))


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
        process_launcher: PosixProcessLauncher,
    ) -> RedundantProcessGroupSentinelController:
        first = ProcessGroupSentinelController.start(
            program,
            cancellation,
            policy,
            lifetime_file_descriptors,
            process_launcher,
        )
        try:
            second = ProcessGroupSentinelController.start(
                program,
                cancellation,
                policy,
                lifetime_file_descriptors,
                process_launcher,
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
                Path(record.record_path),
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
            self._close_parent_lifetime(invocation.parent_lifetime)

    @staticmethod
    def _close_parent_lifetime(
        parent_lifetime: _SentinelParentLifetimeRecordUnion,
    ) -> None:
        if type(parent_lifetime) is _SentinelWithoutParentLifetimeRecord:
            return
        if type(parent_lifetime) is _SentinelParentLifetimeRecord:
            os.close(parent_lifetime.read_file_descriptor)
            return
        raise AssertionError("sentinel parent lifetime is a closed union")

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
            cls._register_parent_lifetime(selector, invocation.parent_lifetime)
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
                        continue
                    if cls._handle_control_event(key.data, invocation, controller):
                        return 0
        raise AssertionError("sentinel event loop unexpectedly returned")

    @staticmethod
    def _register_parent_lifetime(
        selector: selectors.BaseSelector,
        parent_lifetime: _SentinelParentLifetimeRecordUnion,
    ) -> None:
        if type(parent_lifetime) is _SentinelWithoutParentLifetimeRecord:
            return
        if type(parent_lifetime) is not _SentinelParentLifetimeRecord:
            raise AssertionError("sentinel parent lifetime is a closed union")
        selector.register(
            parent_lifetime.read_file_descriptor,
            selectors.EVENT_READ,
            _PARENT_LIFETIME_MARKER,
        )

    @classmethod
    def _handle_control_event(
        cls,
        marker: object,
        invocation: _ProcessGroupSentinelInvocation,
        controller: socket.socket,
    ) -> bool:
        if marker == _PARENT_LIFETIME_MARKER:
            cls._contain_group(invocation, None)
        if marker != _CONTROLLER_MARKER:
            raise AssertionError("sentinel selector marker is closed")
        command = controller.recv(2)
        if command == _RETIRE:
            return True
        cls._contain_group(invocation, None)
        raise AssertionError("sentinel containment unexpectedly returned")


    @staticmethod
    def _retire_endpoint_artifacts(
        invocation: _ProcessGroupSentinelInvocation,
    ) -> None:
        """Best-effort retirement of record, socket, and per-command lease.

        The lease file is deliberately untouched: capacity must stay
        charged until this group is actually dead, which only the flock
        release at process death can prove.
        """
        cancellation_record = invocation.cancellation
        if type(cancellation_record) is _SentinelCancellationEndpointRecord:
            record_file = Path(cancellation_record.record_path)
            try:
                payload = json.loads(record_file.read_text(encoding="utf-8"))
                endpoint = payload.get("endpoint")
                if isinstance(endpoint, str) and endpoint:
                    Path(endpoint).unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            try:
                record_file.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def _contain_group(
        cls,
        invocation: _ProcessGroupSentinelInvocation,
        request: ProcessCancellationRequest | None,
    ) -> None:
        errors: list[BaseException] = []
        # The group dies unconditionally from here; retiring the endpoint
        # artifacts first lets later stop requests observe this containment
        # as ABSENT instead of an unproven stale owner.
        cls._retire_endpoint_artifacts(invocation)
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
    return _ProcessGroupSentinelChild().run(_parse_invocation(arguments.request_json))


if __name__ == "__main__":
    raise SystemExit(main())
