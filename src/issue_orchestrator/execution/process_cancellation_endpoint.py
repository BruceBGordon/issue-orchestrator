# pyright: strict
"""Deep module for stable, owner-mediated local process cancellation."""

from __future__ import annotations

import errno
import fcntl
import math
import os
import selectors
import socket
import stat
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, cast, runtime_checkable

from pydantic import Field, ValidationError

from ..ports.atomic_record_store import (
    AtomicRecordPersistence,
    AtomicRecordStoreFactory,
)
from ..domain.independent_cleanup import (
    CleanupAction,
    CleanupFailed,
    CleanupFailure,
    CleanupOutcome,
    CleanupSucceeded,
    IndependentCleanupPlan,
    raise_cleanup_failures,
    raise_primary_with_cleanup,
)
from .strict_wire_record import StrictWireRecord


_CANCEL_REQUEST = b"C"
_CONTAINMENT_ACKNOWLEDGEMENT = b"K"
_ENDPOINT_ROOT_PREFIX = "io-process-cancellation-"


class ProcessCancellationEndpointError(RuntimeError):
    """A stable cancellation endpoint violated its ownership contract."""


class ProcessCancellationEndpointOutcome(StrEnum):
    """Exact resolution of one owner endpoint."""

    ABSENT = "absent"
    STALE_RETIRED = "stale-retired"
    CONTAINED = "contained"


class _EndpointState(StrEnum):
    SETTING_UP = "setting-up"
    ACTIVE = "active"


class _CancellationConnectionOutcome(StrEnum):
    """Exact endpoint connection result before request delivery."""

    CONNECTED = "connected"
    ABSENT = "absent"


class _EndpointRecord(StrictWireRecord):
    schema_version: Literal[1] = 1
    state: _EndpointState
    endpoint: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ProcessCancellationOwnerControls:
    """Descriptors inherited by exactly one self-containing child owner."""

    listener_file_descriptor: int
    owner_lock_file_descriptor: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("listener_file_descriptor", self.listener_file_descriptor),
            ("owner_lock_file_descriptor", self.owner_lock_file_descriptor),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"ProcessCancellationOwnerControls.{field_name} must be "
                    "a non-negative integer"
                )


@runtime_checkable
class ProcessCancellationEndpointLeaseContract(Protocol):
    """Parent-side cancellation endpoint ownership used by launch transactions."""

    def controls(self) -> ProcessCancellationOwnerControls: ...

    def transfer_after_inherited_activation(self) -> None: ...

    def release_parent_after_spawn_uncertainty(self) -> None: ...

    def retire(self) -> None: ...


@runtime_checkable
class ProcessCancellationEndpointLeaseFactory(Protocol):
    """Acquire one typed cancellation endpoint lease for a launch."""

    def create(
        self,
        record_path: Path,
        record_stores: AtomicRecordStoreFactory,
    ) -> ProcessCancellationEndpointLeaseContract:
        """Return complete ownership or raise only after retiring partial state."""
        ...


class ProcessCancellationOwnerLifetime:
    """Explicit child-side owner of an inherited shared lock reference."""

    def __init__(self, file_descriptor: int) -> None:
        if type(file_descriptor) is not int or file_descriptor < 0:
            raise ValueError(
                "ProcessCancellationOwnerLifetime.file_descriptor must be "
                "a non-negative integer"
            )
        try:
            fcntl.fcntl(file_descriptor, fcntl.F_GETFD)
        except OSError as error:
            raise ValueError(
                "ProcessCancellationOwnerLifetime.file_descriptor must be open"
            ) from error
        self._file_descriptor = file_descriptor
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        os.close(self._file_descriptor)
        self._closed = True


@dataclass(slots=True)
class ProcessCancellationRequest:
    """Accepted cancellation request retained through acknowledgement."""

    connection: socket.socket
    _closed: bool = False

    def acknowledge(self) -> None:
        if self._closed:
            raise RuntimeError("process cancellation request is already closed")
        try:
            self.connection.send(containment_acknowledgement_byte())
        except (BlockingIOError, BrokenPipeError):
            # The shared owner lock, not the courtesy byte, is the final proof.
            pass
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self.connection.close()
        self._closed = True


class ProcessCancellationAcceptedConnection:
    """Own one accepted socket until nonblocking setup succeeds."""

    def __init__(self, connection: socket.socket) -> None:
        if not isinstance(cast(object, connection), socket.socket):
            raise ValueError(
                "ProcessCancellationAcceptedConnection.connection must be a socket"
            )
        try:
            connection.setblocking(False)
        except BaseException as primary_error:
            raise_primary_with_cleanup(
                "accepted cancellation connection setup and cleanup failed",
                primary_error,
                IndependentCleanupPlan(
                    (
                        CleanupAction(
                            "accepted-connection-close",
                            connection.close,
                        ),
                    )
                ).run(),
            )
        self._connection = connection
        self._transferred = False

    @property
    def connection(self) -> socket.socket:
        if self._transferred:
            raise RuntimeError("accepted cancellation connection was transferred")
        return self._connection

    def transfer_to_owner(self) -> socket.socket:
        if self._transferred:
            raise RuntimeError("accepted cancellation connection was transferred twice")
        self._transferred = True
        return self._connection

    def close_before_transfer(self) -> None:
        if self._transferred:
            raise RuntimeError("transferred cancellation connection is owner-managed")
        self._connection.close()


class ProcessCancellationOwner:
    """Child-side listener/lifetime owner integrated into any selector loop."""

    def __init__(self, controls: ProcessCancellationOwnerControls) -> None:
        if type(controls) is not ProcessCancellationOwnerControls:
            raise ValueError(
                "ProcessCancellationOwner requires ProcessCancellationOwnerControls"
            )
        listener = socket.socket(fileno=controls.listener_file_descriptor)
        try:
            listener.setblocking(False)
            lifetime = ProcessCancellationOwnerLifetime(
                controls.owner_lock_file_descriptor
            )
        except BaseException as primary_error:
            raise_primary_with_cleanup(
                "process cancellation owner setup and cleanup failed",
                primary_error,
                IndependentCleanupPlan(
                    (
                        CleanupAction("inherited-listener-close", listener.close),
                        CleanupAction(
                            "inherited-owner-lock-close",
                            lambda: os.close(controls.owner_lock_file_descriptor),
                        ),
                    )
                ).run(),
            )
        self._listener = listener
        self._lifetime = lifetime
        self._connections: set[socket.socket] = set()
        self._closed = False

    def register(self, selector: selectors.BaseSelector, marker: object) -> None:
        if self._closed:
            raise RuntimeError("process cancellation owner is closed")
        selector.register(self._listener, selectors.EVENT_READ, marker)
        for connection in self._connections:
            selector.register(connection, selectors.EVENT_READ, marker)

    def consume_ready(
        self,
        selector: selectors.BaseSelector,
        selectable: object,
        marker: object,
    ) -> ProcessCancellationRequest | None:
        if selectable is self._listener:
            connection, _address = self._listener.accept()
            accepted = ProcessCancellationAcceptedConnection(connection)
            try:
                selector.register(
                    accepted.connection,
                    selectors.EVENT_READ,
                    marker,
                )
            except BaseException as primary_error:
                raise_primary_with_cleanup(
                    "accepted cancellation connection registration and cleanup failed",
                    primary_error,
                    IndependentCleanupPlan(
                        (
                            CleanupAction(
                                "accepted-connection-close",
                                accepted.close_before_transfer,
                            ),
                        )
                    ).run(),
                )
            try:
                self._connections.add(accepted.connection)
            except BaseException as primary_error:
                raise_primary_with_cleanup(
                    "accepted cancellation connection retention and cleanup failed",
                    primary_error,
                    IndependentCleanupPlan(
                        (
                            CleanupAction(
                                "accepted-connection-unregister",
                                lambda: selector.unregister(accepted.connection),
                            ),
                            CleanupAction(
                                "accepted-connection-close",
                                accepted.close_before_transfer,
                            ),
                        )
                    ).run(),
                )
            accepted.transfer_to_owner()
            return None
        if not isinstance(selectable, socket.socket):
            raise ProcessCancellationEndpointError(
                "cancellation selector returned a non-socket owner event"
            )
        if selectable not in self._connections:
            raise ProcessCancellationEndpointError(
                "cancellation selector returned an unknown connection"
            )
        payload = selectable.recv(2)
        selector.unregister(selectable)
        self._connections.remove(selectable)
        if not payload:
            selectable.close()
            return None
        if payload != cancellation_request_byte():
            selectable.close()
            return None
        return ProcessCancellationRequest(selectable)

    def close(self) -> None:
        if self._closed:
            return
        connections = tuple(self._connections)
        self._connections.clear()
        self._closed = True
        raise_cleanup_failures(
            "process cancellation owner resource cleanup failed",
            IndependentCleanupPlan(
                (
                    *(
                        CleanupAction(
                            f"accepted-connection-{index}-close",
                            connection.close,
                        )
                        for index, connection in enumerate(connections)
                    ),
                    CleanupAction("listener-close", self._listener.close),
                    CleanupAction("owner-lifetime-close", self._lifetime.close),
                )
            ).run(),
        )


class ProcessCancellationEndpointLease:
    """Publish setup before spawn and transfer one stable endpoint to its owner."""

    def __init__(
        self,
        record_path: Path,
        record_stores: AtomicRecordStoreFactory,
    ) -> None:
        _require_absolute_record_path(record_path)
        self._record_path = record_path
        self._record_store = record_stores.create(record_path.parent)
        self._lock_path = _lock_path(record_path)
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_handle = self._lock_path.open("a+b")
        self._listener: socket.socket | None = None
        self._endpoint: Path | None = None
        self._active = False
        self._owner_started = False
        self._transferred = False
        self._retired = False
        try:
            fcntl.flock(
                self._lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BaseException as lock_error:
            if isinstance(lock_error, BlockingIOError):
                primary_error: BaseException = ProcessCancellationEndpointError(
                    f"a cancellation owner already owns this record: {record_path}"
                )
                primary_error.__cause__ = lock_error
            else:
                primary_error = lock_error
            raise_primary_with_cleanup(
                "cancellation owner-lock acquisition and cleanup failed",
                primary_error,
                IndependentCleanupPlan(
                    (CleanupAction("owner-lock-close", self._lock_handle.close),)
                ).run(),
            )
        try:
            self._retire_stale_record()
            self._prepare_endpoint()
        except BaseException as primary_error:
            raise_primary_with_cleanup(
                "cancellation endpoint setup and cleanup failed",
                primary_error,
                self._close_parent_resources(),
            )

    def controls(self) -> ProcessCancellationOwnerControls:
        if self._transferred or self._retired:
            raise RuntimeError("cancellation endpoint is no longer parent-owned")
        listener = self._require_listener()
        return ProcessCancellationOwnerControls(
            listener.fileno(),
            self._lock_handle.fileno(),
        )

    def activate(self) -> None:
        if self._active:
            raise RuntimeError("cancellation endpoint was activated twice")
        if self._transferred or self._retired:
            raise RuntimeError("cancellation endpoint is no longer activatable")
        self._record_store.write(
            self._record_path,
            _EndpointRecord(
                state=_EndpointState.ACTIVE,
                endpoint=str(self._require_endpoint()),
            ),
        )
        self._active = True

    def transfer_to_owner(self) -> None:
        if not self._active:
            raise RuntimeError("cancellation endpoint must be active before transfer")
        self._transfer_active_endpoint_to_owner()

    def transfer_after_inherited_activation(self) -> None:
        """Transfer after proving the child published this endpoint ACTIVE."""
        if self._active:
            raise RuntimeError(
                "parent-activated endpoint cannot use inherited activation transfer"
            )
        if self._transferred or self._retired:
            raise RuntimeError("cancellation endpoint was transferred twice")
        record = _read_record(self._record_path)
        if record.state is not _EndpointState.ACTIVE:
            raise ProcessCancellationEndpointError(
                "inherited cancellation endpoint did not publish ACTIVE: "
                f"{self._record_path}"
            )
        if _validated_endpoint(record) != self._require_endpoint():
            raise ProcessCancellationEndpointError(
                "inherited cancellation endpoint changed identity before transfer: "
                f"{self._record_path}"
            )
        self._active = True
        self._transfer_active_endpoint_to_owner()

    def release_parent_after_spawn_uncertainty(self) -> None:
        """Preserve durable recovery when a fork may have inherited ownership."""
        if self._owner_started or self._transferred or self._retired:
            raise RuntimeError(
                "cancellation endpoint spawn ownership was already resolved"
            )
        # Once a fork may have occurred, only the shared owner lock can prove
        # whether a child exists.  Mark that fact before closing parent copies
        # so retire() can never take the unproven no-owner branch.
        self._owner_started = True
        cleanup = self._close_parent_resources()
        if type(cleanup) is CleanupFailed:
            raise_cleanup_failures(
                "could not release parent cancellation resources after uncertain spawn",
                cleanup,
            )
        if type(cleanup) is not CleanupSucceeded:
            raise AssertionError("cleanup outcome is a closed union")
        self._transferred = True

    def _transfer_active_endpoint_to_owner(self) -> None:
        if self._transferred or self._retired:
            raise RuntimeError("cancellation endpoint was transferred twice")
        # From this point onward a child may retain both descriptors.  Record
        # retirement must therefore prove shared-lock release even if closing
        # either parent copy fails.
        self._owner_started = True
        cleanup = self._close_parent_resources()
        if type(cleanup) is CleanupFailed:
            raise_cleanup_failures(
                "cancellation ownership transferred but parent cleanup failed",
                cleanup,
            )
        if type(cleanup) is not CleanupSucceeded:
            raise AssertionError("cleanup outcome is a closed union")
        self._transferred = True

    def retire(self) -> None:
        if self._retired:
            return
        if self._owner_started:
            parent_cleanup = self._close_parent_resources()
            if type(parent_cleanup) is CleanupFailed:
                raise_cleanup_failures(
                    "cannot prove cancellation owner release after parent "
                    "descriptor cleanup failed",
                    parent_cleanup,
                )
            if type(parent_cleanup) is not CleanupSucceeded:
                raise AssertionError("cleanup outcome is a closed union")
            lock_handle = self._open_released_owner_lock()
            listener_cleanup: CleanupOutcome = CleanupSucceeded()
        else:
            lock_handle = self._lock_handle
            listener = self._listener
            listener_cleanup = IndependentCleanupPlan(
                ()
                if listener is None
                else (CleanupAction("listener-close", listener.close),)
            ).run()
        retirement_cleanup: CleanupOutcome = CleanupSucceeded()
        if type(listener_cleanup) is CleanupSucceeded:
            try:
                if self._record_path.exists():
                    _retire_record(
                        self._record_path,
                        _read_record(self._record_path),
                        self._record_store,
                    )
            except BaseException as error:
                retirement_cleanup = CleanupFailed(
                    (CleanupFailure("endpoint-record-retirement", error),)
                )
        lock_cleanup = IndependentCleanupPlan(
            (CleanupAction("owner-lock-close", lock_handle.close),)
        ).run()
        failures = _cleanup_errors(
            listener_cleanup,
            retirement_cleanup,
            lock_cleanup,
        )
        if failures:
            raise BaseExceptionGroup(
                "cancellation endpoint retirement failed",
                failures,
            )
        self._retired = True

    def _open_released_owner_lock(self) -> BinaryIO:
        lock_handle = self._lock_path.open("r+b")
        try:
            acquired = _try_acquire_owner_lock(lock_handle)
        except BaseException as primary_error:
            raise_primary_with_cleanup(
                "cancellation retirement lock acquisition and cleanup failed",
                primary_error,
                IndependentCleanupPlan(
                    (CleanupAction("retirement-owner-lock-close", lock_handle.close),)
                ).run(),
            )
        if acquired:
            return lock_handle
        primary_error = ProcessCancellationEndpointError(
            "cannot retire a cancellation endpoint while its owner "
            f"is live: {self._record_path}"
        )
        raise_primary_with_cleanup(
            "live cancellation owner observation and cleanup failed",
            primary_error,
            IndependentCleanupPlan(
                (CleanupAction("retirement-owner-lock-close", lock_handle.close),)
            ).run(),
        )

    def _prepare_endpoint(self) -> None:
        _require_endpoint_root()
        file_status = os.fstat(self._lock_handle.fileno())
        endpoint = _endpoint_root() / (
            f"{file_status.st_dev:x}-{file_status.st_ino:x}.sock"
        )
        if endpoint.exists():
            raise ProcessCancellationEndpointError(
                f"managed cancellation endpoint already exists: {endpoint}"
            )
        self._record_store.write(
            self._record_path,
            _EndpointRecord(
                state=_EndpointState.SETTING_UP,
                endpoint=str(endpoint),
            ),
        )
        listener: socket.socket | None = None
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(endpoint))
            listener.listen(4)
        except BaseException as primary_error:
            listener_cleanup = IndependentCleanupPlan(
                ()
                if listener is None
                else (CleanupAction("listener-close", listener.close),)
            ).run()
            retirement_cleanup: CleanupOutcome = CleanupSucceeded()
            if type(listener_cleanup) is CleanupSucceeded:
                try:
                    _retire_record(
                        self._record_path,
                        _read_record(self._record_path),
                        self._record_store,
                    )
                except BaseException as cleanup_error:
                    retirement_cleanup = CleanupFailed(
                        (
                            CleanupFailure(
                                "failed-endpoint-record-retirement",
                                cleanup_error,
                            ),
                        )
                    )
            cleanup_errors = _cleanup_errors(
                listener_cleanup,
                retirement_cleanup,
            )
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "cancellation endpoint bind and cleanup failed",
                    (primary_error, *cleanup_errors),
                )
            raise
        self._endpoint = endpoint
        self._listener = listener

    def _retire_stale_record(self) -> None:
        if self._record_path.exists():
            _retire_record(
                self._record_path,
                _read_record(self._record_path),
                self._record_store,
            )

    def _close_parent_resources(self) -> CleanupOutcome:
        actions: list[CleanupAction] = []
        if self._listener is not None:
            actions.append(CleanupAction("listener-close", self._listener.close))
        if not self._lock_handle.closed:
            actions.append(CleanupAction("owner-lock-close", self._lock_handle.close))
        return IndependentCleanupPlan(tuple(actions)).run()

    def _require_listener(self) -> socket.socket:
        if self._listener is None:
            raise RuntimeError("cancellation listener was not created")
        return self._listener

    def _require_endpoint(self) -> Path:
        if self._endpoint is None:
            raise RuntimeError("cancellation endpoint was not created")
        return self._endpoint


class PosixProcessCancellationEndpointLeaseFactory:
    """Production factory for local POSIX cancellation endpoint leases."""

    def create(
        self,
        record_path: Path,
        record_stores: AtomicRecordStoreFactory,
    ) -> ProcessCancellationEndpointLeaseContract:
        return ProcessCancellationEndpointLease(record_path, record_stores)


class ProcessCancellationInheritedEndpointActivator:
    """Child-side activation after its in-group sentinel is ready."""

    def __init__(
        self,
        record_path: Path,
        controls: ProcessCancellationOwnerControls,
        record_stores: AtomicRecordStoreFactory,
    ) -> None:
        _require_absolute_record_path(record_path)
        if type(controls) is not ProcessCancellationOwnerControls:
            raise ValueError(
                "inherited endpoint activator requires typed owner controls"
            )
        self._record_path = record_path
        self._controls = controls
        self._record_store = record_stores.create(record_path.parent)
        self._activated = False

    def activate_for_owner(self) -> None:
        if self._activated:
            raise RuntimeError("inherited cancellation endpoint activated twice")
        record = _read_record(self._record_path)
        if record.state is not _EndpointState.SETTING_UP:
            raise ProcessCancellationEndpointError(
                "inherited cancellation endpoint was not in SETTING_UP: "
                f"{self._record_path}"
            )
        self._record_store.write(
            self._record_path,
            _EndpointRecord(
                state=_EndpointState.ACTIVE,
                endpoint=record.endpoint,
            ),
        )
        self._activated = True
        raise_cleanup_failures(
            "inherited cancellation descriptor release failed",
            IndependentCleanupPlan(
                (
                    CleanupAction(
                        "inherited-listener-close",
                        lambda: os.close(self._controls.listener_file_descriptor),
                    ),
                    CleanupAction(
                        "inherited-owner-lock-close",
                        lambda: os.close(self._controls.owner_lock_file_descriptor),
                    ),
                )
            ).run(),
        )


class ProcessCancellationEndpointRequester:
    """Request self-containment and await the exact owner-lock release."""

    def __init__(
        self,
        timeout_seconds: float,
        record_stores: AtomicRecordStoreFactory,
    ) -> None:
        if (
            type(timeout_seconds) is not float
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError(
                "ProcessCancellationEndpointRequester.timeout_seconds must be "
                "finite and positive"
            )
        self._timeout_seconds = timeout_seconds
        self._record_stores = record_stores

    def contain(self, record_path: Path) -> ProcessCancellationEndpointOutcome:
        return self.contain_before(
            record_path,
            time.monotonic() + self._timeout_seconds,
        )

    def contain_before(
        self,
        record_path: Path,
        deadline: float,
    ) -> ProcessCancellationEndpointOutcome:
        if type(deadline) is not float or not math.isfinite(deadline):
            raise ValueError("cancellation deadline must be a finite float")
        _require_absolute_record_path(record_path)
        try:
            lock_handle = _lock_path(record_path).open("r+b")
        except FileNotFoundError:
            if record_path.exists():
                raise ProcessCancellationEndpointError(
                    f"cancellation record exists without its owner lock: {record_path}"
                )
            return ProcessCancellationEndpointOutcome.ABSENT
        try:
            outcome = self._contain_with_owner_lock(
                lock_handle,
                record_path,
                deadline,
            )
        except BaseException as primary_error:
            raise_primary_with_cleanup(
                "cancellation request and owner-lock cleanup failed",
                primary_error,
                IndependentCleanupPlan(
                    (CleanupAction("request-owner-lock-close", lock_handle.close),)
                ).run(),
            )
        raise_cleanup_failures(
            "cancellation request owner-lock cleanup failed",
            IndependentCleanupPlan(
                (CleanupAction("request-owner-lock-close", lock_handle.close),)
            ).run(),
        )
        return outcome

    def _contain_with_owner_lock(
        self,
        lock_handle: BinaryIO,
        record_path: Path,
        deadline: float,
    ) -> ProcessCancellationEndpointOutcome:
        request_delivered = False
        while True:
            if _try_acquire_owner_lock(lock_handle):
                stale = _retire_without_owner(
                    record_path,
                    self._record_stores.create(record_path.parent),
                )
                if request_delivered:
                    return ProcessCancellationEndpointOutcome.CONTAINED
                return stale
            record = _read_record(record_path)
            if record.state is _EndpointState.SETTING_UP:
                self._await_next_setup_state(lock_handle, record_path, deadline)
                continue
            if record.state is not _EndpointState.ACTIVE:
                raise AssertionError("endpoint state is a closed enum")
            if not request_delivered:
                request_delivered = _deliver_request(
                    _validated_endpoint(record), deadline
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessCancellationEndpointError(
                    "cancellation owner retained its lock past the absolute "
                    f"deadline: {record_path}"
                )
            time.sleep(min(0.01, remaining))

    @staticmethod
    def _await_next_setup_state(
        lock_handle: BinaryIO,
        record_path: Path,
        deadline: float,
    ) -> None:
        while True:
            if _try_acquire_owner_lock(lock_handle):
                return
            record = _read_record(record_path)
            if record.state is _EndpointState.ACTIVE:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessCancellationEndpointError(
                    "cancellation endpoint remained in setup past the absolute "
                    f"deadline: {record_path}"
                )
            time.sleep(min(0.01, remaining))


class ProcessCancellationEndpointReadiness:
    """Await ACTIVE publication or fail when the setup owner disappears."""

    def __init__(
        self,
        timeout_seconds: float,
        record_stores: AtomicRecordStoreFactory,
    ) -> None:
        if (
            type(timeout_seconds) is not float
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError(
                "ProcessCancellationEndpointReadiness.timeout_seconds must be "
                "finite and positive"
            )
        self._timeout_seconds = timeout_seconds
        self._record_stores = record_stores

    def await_active(self, record_path: Path) -> None:
        _require_absolute_record_path(record_path)
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                lock_handle = _lock_path(record_path).open("r+b")
            except FileNotFoundError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProcessCancellationEndpointError(
                        "cancellation setup did not create its owner lock: "
                        f"{record_path}"
                    ) from None
                time.sleep(min(0.01, remaining))
                continue
            try:
                if _try_acquire_owner_lock(lock_handle):
                    _retire_without_owner(
                        record_path,
                        self._record_stores.create(record_path.parent),
                    )
                    raise ProcessCancellationEndpointError(
                        f"cancellation setup owner exited before ACTIVE: {record_path}"
                    )
                try:
                    record = _read_record(record_path)
                except FileNotFoundError:
                    record = None
                if record is not None and record.state is _EndpointState.ACTIVE:
                    return
            finally:
                lock_handle.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessCancellationEndpointError(
                    "cancellation setup did not publish ACTIVE before its "
                    f"deadline: {record_path}"
                )
            time.sleep(min(0.01, remaining))


def cancellation_request_byte() -> bytes:
    return _CANCEL_REQUEST


def containment_acknowledgement_byte() -> bytes:
    return _CONTAINMENT_ACKNOWLEDGEMENT


def _deliver_request(endpoint: Path, deadline: float) -> bool:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.setblocking(False)
    try:
        selector = selectors.DefaultSelector()
    except BaseException as primary_error:
        raise_primary_with_cleanup(
            "cancellation selector setup and connection cleanup failed",
            primary_error,
            IndependentCleanupPlan(
                (CleanupAction("request-connection-close", connection.close),)
            ).run(),
        )
    try:
        connection_outcome = _connect_request_endpoint(
            connection,
            selector,
            endpoint,
            deadline,
        )
        delivered = (
            False
            if connection_outcome is _CancellationConnectionOutcome.ABSENT
            else _send_request_and_await_acknowledgement(
                connection,
                selector,
                endpoint,
                deadline,
            )
        )
    except BaseException as primary_error:
        raise_primary_with_cleanup(
            "cancellation request and transport cleanup failed",
            primary_error,
            IndependentCleanupPlan(
                (
                    CleanupAction("request-selector-close", selector.close),
                    CleanupAction("request-connection-close", connection.close),
                )
            ).run(),
        )
    raise_cleanup_failures(
        "cancellation request transport cleanup failed",
        IndependentCleanupPlan(
            (
                CleanupAction("request-selector-close", selector.close),
                CleanupAction("request-connection-close", connection.close),
            )
        ).run(),
    )
    return delivered


def _connect_request_endpoint(
    connection: socket.socket,
    selector: selectors.BaseSelector,
    endpoint: Path,
    deadline: float,
) -> _CancellationConnectionOutcome:
    connect_error = connection.connect_ex(str(endpoint))
    if connect_error in (errno.ENOENT, errno.ECONNREFUSED):
        return _CancellationConnectionOutcome.ABSENT
    if connect_error not in (
        0,
        errno.EINPROGRESS,
        errno.EALREADY,
        errno.EWOULDBLOCK,
    ):
        raise OSError(connect_error, os.strerror(connect_error))
    if connect_error == 0:
        return _CancellationConnectionOutcome.CONNECTED
    selector.register(connection, selectors.EVENT_WRITE)
    _select_before_deadline(selector, deadline, endpoint, "connect")
    socket_error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    selector.unregister(connection)
    if socket_error in (errno.ENOENT, errno.ECONNREFUSED):
        return _CancellationConnectionOutcome.ABSENT
    if socket_error != 0:
        raise OSError(socket_error, os.strerror(socket_error))
    return _CancellationConnectionOutcome.CONNECTED


def _send_request_and_await_acknowledgement(
    connection: socket.socket,
    selector: selectors.BaseSelector,
    endpoint: Path,
    deadline: float,
) -> bool:
    selector.register(connection, selectors.EVENT_WRITE)
    _send_cancellation_request(connection, selector, endpoint, deadline)
    selector.modify(connection, selectors.EVENT_READ)
    return _await_containment_acknowledgement(
        connection, selector, endpoint, deadline
    )


def _send_cancellation_request(
    connection: socket.socket,
    selector: selectors.BaseSelector,
    endpoint: Path,
    deadline: float,
) -> None:
    while True:
        try:
            written = connection.send(cancellation_request_byte())
        except BlockingIOError:
            _select_before_deadline(selector, deadline, endpoint, "request")
            continue
        if written != len(cancellation_request_byte()):
            raise ProcessCancellationEndpointError(
                f"cancellation request performed a short write: {endpoint}"
            )
        return


def _await_containment_acknowledgement(
    connection: socket.socket,
    selector: selectors.BaseSelector,
    endpoint: Path,
    deadline: float,
) -> bool:
    acknowledged = False
    while True:
        _select_before_deadline(selector, deadline, endpoint, "response")
        payload = connection.recv(2)
        if not payload:
            # An owner completing naturally can accept the connection and exit
            # before processing the request.  The owner lock, not this socket,
            # proves containment: report the request undelivered so the caller
            # keeps polling for lock release under its absolute deadline.
            return acknowledged
        if acknowledged or payload != containment_acknowledgement_byte():
            raise ProcessCancellationEndpointError(
                f"cancellation owner returned an invalid response: {endpoint}"
            )
        acknowledged = True


def _select_before_deadline(
    selector: selectors.BaseSelector,
    deadline: float,
    endpoint: Path,
    phase: str,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not selector.select(remaining):
        raise ProcessCancellationEndpointError(
            f"cancellation {phase} exceeded its absolute deadline: {endpoint}"
        )
    if time.monotonic() >= deadline:
        raise ProcessCancellationEndpointError(
            f"cancellation {phase} exceeded its absolute deadline: {endpoint}"
        )


def _try_acquire_owner_lock(lock_handle: BinaryIO) -> bool:
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _require_absolute_record_path(record_path: Path) -> None:
    if not record_path.is_absolute():
        raise ValueError("cancellation record path must be absolute")


def _lock_path(record_path: Path) -> Path:
    return record_path.with_suffix(f"{record_path.suffix}.lock")


def _require_endpoint_root() -> None:
    endpoint_root = _endpoint_root()
    endpoint_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_status = endpoint_root.stat()
    if stat.S_IMODE(root_status.st_mode) != 0o700:
        raise ProcessCancellationEndpointError(
            f"managed cancellation directory must have mode 0700: {endpoint_root}"
        )
    if root_status.st_uid != os.getuid():
        raise ProcessCancellationEndpointError(
            f"managed cancellation directory has a different owner: {endpoint_root}"
        )


def _read_record(record_path: Path) -> _EndpointRecord:
    try:
        record = _EndpointRecord.model_validate_json(
            record_path.read_text(encoding="utf-8")
        )
        _validated_endpoint(record)
        return record
    except FileNotFoundError:
        raise
    except (OSError, ValidationError, ValueError) as error:
        raise ProcessCancellationEndpointError(
            f"invalid process cancellation record: {record_path}"
        ) from error


def _validated_endpoint(record: _EndpointRecord) -> Path:
    endpoint = Path(record.endpoint)
    if (
        not endpoint.is_absolute()
        or endpoint.parent != _endpoint_root()
        or endpoint.suffix != ".sock"
    ):
        raise ValueError("cancellation endpoint is outside its managed directory")
    return endpoint


def _endpoint_root() -> Path:
    return Path("/tmp") / f"{_ENDPOINT_ROOT_PREFIX}{os.getuid()}"


def _retire_without_owner(
    record_path: Path,
    record_store: AtomicRecordPersistence,
) -> ProcessCancellationEndpointOutcome:
    if not record_path.exists():
        return ProcessCancellationEndpointOutcome.ABSENT
    _retire_record(record_path, _read_record(record_path), record_store)
    return ProcessCancellationEndpointOutcome.STALE_RETIRED


def _retire_record(
    record_path: Path,
    record: _EndpointRecord,
    record_store: AtomicRecordPersistence,
) -> None:
    endpoint = _validated_endpoint(record)
    try:
        endpoint_status = endpoint.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(endpoint_status.st_mode):
            raise ProcessCancellationEndpointError(
                f"managed cancellation endpoint is not a socket: {endpoint}"
            )
        endpoint.unlink()
    record_store.delete(record_path)


def _cleanup_errors(*outcomes: CleanupOutcome) -> tuple[BaseException, ...]:
    errors: list[BaseException] = []
    for outcome in outcomes:
        if type(outcome) is CleanupSucceeded:
            continue
        if type(outcome) is not CleanupFailed:
            raise AssertionError("cleanup outcome is a closed union")
        errors.extend(failure.error for failure in outcome.failures)
    return tuple(errors)
