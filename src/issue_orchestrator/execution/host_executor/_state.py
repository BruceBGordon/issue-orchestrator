# pyright: strict
"""Cross-process queue and lease adapter for the host executor."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Generator, NoReturn, TypeVar, cast

from pydantic import BaseModel, ValidationError

from ...control.executor_admission import (
    ActiveExecutorLease,
    ExecutorAdmissionGrant,
    ExecutorAdmissionDeferred,
    ExecutorAdmissionGranted,
    ExecutorAdmissionPolicy,
    ExecutorGroupService,
    ExecutorQueueSnapshot,
    QueuedExecutorWork,
)
from ...domain.executor import ExecutorExclusiveResource, ExecutorFairnessGroup
from ...domain.executor_host import ExecutorHostCpuUtilization
from ...domain.executor_monitoring import ExecutorWaitReason
from ._contracts import (
    ActiveLeaseRecord,
    CapacityRecord,
    GroupServiceEntryRecord,
    GroupServiceRecord,
    QueuedWorkRecord,
)
from ..atomic_record_store import AtomicRecordStore
from ...domain.independent_cleanup import (
    CleanupAction,
    CleanupOutcome,
    IndependentCleanupPlan,
    raise_cleanup_failures,
    raise_primary_with_cleanup,
)
from ...infra.posix_file_lock import (
    PosixFileLockAcquired,
    PosixFileLockAcquisition,
    PosixFileLockContended,
    PosixFileLockFilePresence,
    PosixFileLockMode,
    PosixFileLockOwner,
    PosixFileLockSpecification,
)


_RecordType = TypeVar("_RecordType", bound=BaseModel)


def _require_exception_tuple(value: object) -> tuple[BaseException, ...]:
    if type(value) is not tuple:
        raise ValueError("lease transfer failures must contain exceptions")
    entries = cast(tuple[object, ...], value)
    if any(not isinstance(failure, BaseException) for failure in entries):
        raise ValueError("lease transfer failures must contain exceptions")
    return cast(tuple[BaseException, ...], entries)


class OwnedQueuedRequest:
    """Queue record whose open handle proves this process still owns it."""

    def __init__(self, work: QueuedExecutorWork, path: Path, handle: BinaryIO) -> None:
        self.work = work
        self.path = path
        self._handle = handle
        self._released = False

    def release(self) -> None:
        if self._released:
            raise RuntimeError("queued executor request was released twice")
        outcome = _cleanup_record_and_handles(self.path, [self._handle])
        self._released = True
        raise_cleanup_failures(
            "queued executor request cleanup failures",
            outcome,
        )

    def release_after_failure(self, primary_error: BaseException) -> NoReturn:
        """Preserve a queue-body failure beside every retirement failure."""
        raise_primary_with_cleanup(
            "executor queue body and request cleanup failures",
            primary_error,
            IndependentCleanupPlan(
                (CleanupAction("release queued executor request", self.release),)
            ).run(),
        )

    def release_after_grant(self, lease: HostExecutorLease) -> None:
        """Retire the queue entry or roll back its newly admitted lease."""
        try:
            self.release()
        except BaseException as queue_cleanup_error:
            raise_primary_with_cleanup(
                "executor queue retirement and admitted lease rollback failures",
                queue_cleanup_error,
                IndependentCleanupPlan(
                    (CleanupAction("release admitted executor lease", lease.release),)
                ).run(),
            )

    def __enter__(self) -> OwnedQueuedRequest:
        if self._released:
            raise RuntimeError("cannot enter a released queued executor request")
        return self

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        self.release()


class _HostExecutorLeaseOwnership(Enum):
    LOCAL = "local"
    GUARDIAN = "guardian"
    GUARDIAN_WITH_LOCAL_RECOVERY = "guardian-with-local-recovery"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class _ExecutorLeaseDescriptorTransfer:
    """Exact failures and still-owned handles after descriptor transfer."""

    retained_handles: tuple[BinaryIO, ...]
    failures: tuple[BaseException, ...]

    def __post_init__(self) -> None:
        if type(self.retained_handles) is not tuple:
            raise ValueError("retained lease handles must be a tuple")
        _require_exception_tuple(self.failures)


@dataclass(frozen=True, slots=True)
class _GuardianLeaseRecordRetired:
    """This logical owner removed the now-unlocked lease record."""


@dataclass(frozen=True, slots=True)
class _GuardianLeaseRecordAlreadyReconciled:
    """A competing state observer already retired the unlocked record."""


_GuardianLeaseRecordRetirement = (
    _GuardianLeaseRecordRetired | _GuardianLeaseRecordAlreadyReconciled
)


def _retire_guardian_lease_record(path: Path) -> _GuardianLeaseRecordRetirement:
    """Retire one shared record after its guardian released the lock.

    Once guardian descriptors close, any queue observer may prove the record
    stale and unlink it before the originating process resumes.  Absence here
    is therefore an explicit reconciliation outcome, not suppressed cleanup.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return _GuardianLeaseRecordAlreadyReconciled()
    return _GuardianLeaseRecordRetired()


class HostExecutorLease:
    """Live CPU/resource locks transferred to the admitted command guardian."""

    def __init__(
        self,
        grant: ExecutorAdmissionGrant,
        path: Path,
        handles: list[BinaryIO],
    ) -> None:
        if not handles:
            raise ValueError("HostExecutorLease requires at least one lock handle")
        self.grant = grant
        self.path = path
        self._handles = handles
        self._ownership = _HostExecutorLeaseOwnership.LOCAL

    def inherited_file_descriptors(self) -> tuple[int, ...]:
        if self._ownership is not _HostExecutorLeaseOwnership.LOCAL:
            raise RuntimeError("only a locally owned executor lease can be inherited")
        return tuple(handle.fileno() for handle in self._handles)

    def transfer_to_guardian(self) -> None:
        """Make the spawned guardian the sole descriptor owner without unlocking."""
        if self._ownership is not _HostExecutorLeaseOwnership.LOCAL:
            raise RuntimeError("host executor lease can be transferred only once")
        # flock ownership follows the inherited open-file description. Explicit
        # LOCK_UN here would also unlock the guardian's copy.
        transfer = _transfer_lease_descriptors(tuple(reversed(self._handles)))
        self._handles = list(transfer.retained_handles)
        self._ownership = (
            _HostExecutorLeaseOwnership.GUARDIAN
            if not transfer.retained_handles
            else _HostExecutorLeaseOwnership.GUARDIAN_WITH_LOCAL_RECOVERY
        )
        if transfer.failures:
            raise BaseExceptionGroup(
                "executor lease transfer descriptor failures",
                transfer.failures,
            )

    def release(self) -> None:
        if self._ownership is _HostExecutorLeaseOwnership.RELEASED:
            raise RuntimeError("host executor lease was released twice")
        outcome = (
            _cleanup_record_and_handles(self.path, self._handles)
            if self._ownership
            in (
                _HostExecutorLeaseOwnership.LOCAL,
                _HostExecutorLeaseOwnership.GUARDIAN_WITH_LOCAL_RECOVERY,
            )
            else IndependentCleanupPlan(
                (
                    CleanupAction(
                        "retire guardian executor lease record",
                        lambda: _retire_guardian_lease_record(self.path),
                    ),
                )
            ).run()
        )
        self._handles.clear()
        self._ownership = _HostExecutorLeaseOwnership.RELEASED
        raise_cleanup_failures("executor lease cleanup failures", outcome)


@dataclass(frozen=True, slots=True)
class HostAdmissionGranted:
    lease: HostExecutorLease
    decision: ExecutorAdmissionGranted


@dataclass(frozen=True, slots=True)
class HostAdmissionDeferred:
    decision: ExecutorAdmissionDeferred


HostAdmissionOutcome = HostAdmissionGranted | HostAdmissionDeferred


class HostExecutorState:
    """Own queue transactions, file locks, leases, and fairness service state."""

    def __init__(
        self,
        pool_dir: Path,
        host_cpu_slots: int,
        atomic_records: AtomicRecordStore,
    ) -> None:
        if type(host_cpu_slots) is not int or host_cpu_slots < 1:
            raise ValueError("HostExecutorState capacity must be a positive integer")
        self._pool_dir = pool_dir
        self.host_cpu_slots = host_cpu_slots
        self._atomic_records = atomic_records
        self._file_locks = PosixFileLockOwner()
        self._capacity_lock = PosixFileLockSpecification(
            (pool_dir / "capacity.lock").resolve(),
            PosixFileLockMode.EXCLUSIVE,
            PosixFileLockAcquisition.BLOCKING,
            PosixFileLockFilePresence.CREATE_IF_MISSING,
        )
        self._queue_lock = PosixFileLockSpecification(
            (pool_dir / "queue.lock").resolve(),
            PosixFileLockMode.EXCLUSIVE,
            PosixFileLockAcquisition.BLOCKING,
            PosixFileLockFilePresence.CREATE_IF_MISSING,
        )

    def configure_capacity(self) -> None:
        """Persist capacity, refusing to change it while leases are active."""
        self._pool_dir.mkdir(parents=True, exist_ok=True)
        capacity_path = self._pool_dir / "capacity.json"
        with self._capacity_guard():
            self._atomic_records.prune_crash_remnants()
            current = self._read_capacity(capacity_path)
            if current is not None and current != self.host_cpu_slots:
                if not self._all_cpu_slots_are_idle(current):
                    raise RuntimeError(
                        "cannot change host executor capacity while leases are active: "
                        f"current={current} requested={self.host_cpu_slots}"
                    )
            if current != self.host_cpu_slots:
                self._atomic_records.write(
                    capacity_path,
                    CapacityRecord(capacity_units=self.host_cpu_slots),
                )

    def enqueue(self, work: QueuedExecutorWork) -> OwnedQueuedRequest:
        """Create and exclusively own one durable queue record."""
        path = self._pool_dir / "requests" / f"{work.request_id.value}.json"
        with self._queue_guard():
            self._reset_inactive_group_service_before_enqueue()
            path.parent.mkdir(parents=True, exist_ok=True)
            handle: BinaryIO | None = None
            created = False
            try:
                handle = path.open("x+b")
                created = True
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                self._write_locked(handle, QueuedWorkRecord.from_domain(work))
            except BaseException as publication_error:
                # A failed publication must not leave a locked, partial request
                # that poisons every peer until this process happens to exit.
                # Never unlink a record whose exclusive creation failed: that
                # path belongs to the colliding request owner.
                actions: list[CleanupAction] = []
                if created:
                    actions.append(
                        CleanupAction("unlink partial queue record", path.unlink)
                    )
                if handle is not None:
                    actions.extend(_locked_handle_cleanup_actions((handle,)))
                raise_primary_with_cleanup(
                    "host executor queue publication and cleanup failures",
                    publication_error,
                    IndependentCleanupPlan(tuple(actions)).run(),
                )
        return OwnedQueuedRequest(work, path, handle)

    def _reset_inactive_group_service_before_enqueue(self) -> None:
        """End service epochs before a new request can make a group live."""
        existing_requests = self._live_request_records()
        existing_leases = self._live_leases()
        self._write_group_service(
            self._reconciled_group_service(existing_requests, existing_leases)
        )

    def attempt_admission(
        self,
        current: OwnedQueuedRequest,
        policy: ExecutorAdmissionPolicy,
        host_cpu_utilization: ExecutorHostCpuUtilization,
    ) -> HostAdmissionOutcome:
        """Decide and commit one admission atomically under the queue lock."""
        with self._capacity_guard():
            configured = self._read_capacity(self._pool_dir / "capacity.json")
            if configured is None:
                raise RuntimeError("host executor capacity is not configured")
            if configured != self.host_cpu_slots:
                raise RuntimeError(
                    "host executor capacity changed after this command entered the "
                    f"queue: configured={configured} process={self.host_cpu_slots}"
                )
            return self._attempt_admission_with_stable_capacity(
                current,
                policy,
                host_cpu_utilization,
            )

    def _attempt_admission_with_stable_capacity(
        self,
        current: OwnedQueuedRequest,
        policy: ExecutorAdmissionPolicy,
        host_cpu_utilization: ExecutorHostCpuUtilization,
    ) -> HostAdmissionOutcome:
        """Commit one admission while capacity reconfiguration is excluded."""
        with self._queue_guard():
            requests = self._live_requests(current)
            leases = self._live_leases()
            service = self._reconciled_group_service(requests, leases)
            snapshot = ExecutorQueueSnapshot(
                host_cpu_slots=self.host_cpu_slots,
                queued=requests,
                active=leases,
                group_service=service,
                host_cpu_utilization=host_cpu_utilization,
            )
            decision = policy.decide(current.work, snapshot)
            if isinstance(decision, ExecutorAdmissionDeferred):
                return HostAdmissionDeferred(decision)

            handles = self._try_acquire_resources(
                decision.grant.cpu_slots,
                current.work.exclusive_resources,
            )
            if handles is None:
                return HostAdmissionDeferred(
                    ExecutorAdmissionDeferred(
                        reason=ExecutorWaitReason.LEASE_RACE,
                        leased_cpu_slots=(decision.leased_cpu_slots_before),
                        available_cpu_slots=(decision.available_cpu_slots_before),
                    )
                )
            try:
                lease_path, lease_handle = self._create_lease_record(
                    current.work,
                    decision.grant,
                )
            except BaseException as lease_publication_error:
                raise_primary_with_cleanup(
                    "executor lease publication and resource cleanup failures",
                    lease_publication_error,
                    _cleanup_handles(handles),
                )
            handles.append(lease_handle)
            try:
                self._write_group_service(
                    self._add_service(
                        service,
                        current.work.fairness_group,
                        decision.grant.cpu_slots,
                    )
                )
            except BaseException as service_publication_error:
                raise_primary_with_cleanup(
                    "executor service publication and lease cleanup failures",
                    service_publication_error,
                    _cleanup_record_and_handles(lease_path, handles),
                )
            return HostAdmissionGranted(
                HostExecutorLease(decision.grant, lease_path, handles),
                decision,
            )

    @contextmanager
    def _capacity_guard(self) -> Generator[None, None, None]:
        with self._file_locks.hold(self._capacity_lock):
            yield

    @contextmanager
    def _queue_guard(self) -> Generator[None, None, None]:
        with self._file_locks.hold(self._queue_lock):
            yield

    def _live_requests(
        self,
        current: OwnedQueuedRequest,
    ) -> tuple[QueuedExecutorWork, ...]:
        other_paths = tuple(
            path for path in self._request_record_paths() if path != current.path
        )
        return (current.work, *self._read_live_request_paths(other_paths))

    def _live_request_records(self) -> tuple[QueuedExecutorWork, ...]:
        return self._read_live_request_paths(self._request_record_paths())

    def _request_record_paths(self) -> tuple[Path, ...]:
        requests_dir = self._pool_dir / "requests"
        if not requests_dir.exists():
            return ()
        return tuple(sorted(requests_dir.glob("*.json")))

    def _read_live_request_paths(
        self,
        paths: tuple[Path, ...],
    ) -> tuple[QueuedExecutorWork, ...]:
        requests: list[QueuedExecutorWork] = []
        for path in paths:
            record = self._read_live_record(path, QueuedWorkRecord)
            if record is not None:
                requests.append(record.to_domain())
        return tuple(requests)

    def _live_leases(self) -> tuple[ActiveExecutorLease, ...]:
        leases: list[ActiveExecutorLease] = []
        leases_dir = self._pool_dir / "leases"
        if leases_dir.exists():
            for path in sorted(leases_dir.glob("*.json")):
                record = self._read_live_record(path, ActiveLeaseRecord)
                if record is not None:
                    leases.append(record.to_domain())
        return tuple(leases)

    def _read_live_record(
        self,
        path: Path,
        record_type: type[_RecordType],
    ) -> _RecordType | None:
        try:
            lock = self._file_locks.acquire(
                PosixFileLockSpecification(
                    path,
                    PosixFileLockMode.EXCLUSIVE,
                    PosixFileLockAcquisition.NON_BLOCKING,
                    PosixFileLockFilePresence.REQUIRE_EXISTING,
                )
            )
        except FileNotFoundError:
            return None
        try:
            if type(lock) is PosixFileLockContended:
                lock.lease.handle.seek(0)
                try:
                    result: _RecordType | None = record_type.model_validate_json(
                        lock.lease.handle.read()
                    )
                except ValidationError as exc:
                    raise RuntimeError(
                        f"invalid host executor state file: {path}"
                    ) from exc
            elif type(lock) is PosixFileLockAcquired:
                path.unlink(missing_ok=True)
                result = None
            else:
                raise AssertionError("POSIX file-lock outcome is a closed union")
        except BaseException as record_error:
            lock.lease.release_after_failure(record_error)
        lock.lease.release()
        return result

    def _reconciled_group_service(
        self,
        requests: tuple[QueuedExecutorWork, ...],
        leases: tuple[ActiveExecutorLease, ...],
    ) -> tuple[ExecutorGroupService, ...]:
        live_groups = {
            *(request.fairness_group for request in requests),
            *(lease.fairness_group for lease in leases),
        }
        stored = self._read_group_service()
        stored_by_group = {item.fairness_group: item.cpu_slots for item in stored}
        return tuple(
            ExecutorGroupService(group, stored_by_group.get(group, 0))
            for group in sorted(live_groups, key=lambda item: item.value)
        )

    def _read_group_service(self) -> tuple[ExecutorGroupService, ...]:
        path = self._pool_dir / "group-service.json"
        if not path.exists():
            return ()
        try:
            record = GroupServiceRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise RuntimeError(f"invalid host executor group state: {path}") from exc
        service = tuple(entry.to_domain() for entry in record.entries)
        groups = tuple(item.fairness_group for item in service)
        if len(groups) != len(set(groups)):
            raise RuntimeError(f"duplicate host executor group state: {path}")
        return service

    def _write_group_service(
        self,
        service: tuple[ExecutorGroupService, ...],
    ) -> None:
        record = GroupServiceRecord(
            entries=tuple(GroupServiceEntryRecord.from_domain(item) for item in service)
        )
        self._atomic_records.write(self._pool_dir / "group-service.json", record)

    @staticmethod
    def _add_service(
        service: tuple[ExecutorGroupService, ...],
        group: ExecutorFairnessGroup,
        cpu_slots: int,
    ) -> tuple[ExecutorGroupService, ...]:
        updated = tuple(
            ExecutorGroupService(
                item.fairness_group,
                item.cpu_slots + (cpu_slots if item.fairness_group == group else 0),
            )
            for item in service
        )
        if not any(item.fairness_group == group for item in service):
            raise RuntimeError("admitted group is missing from reconciled service")
        return updated

    def _create_lease_record(
        self,
        work: QueuedExecutorWork,
        grant: ExecutorAdmissionGrant,
    ) -> tuple[Path, BinaryIO]:
        leases_dir = self._pool_dir / "leases"
        leases_dir.mkdir(parents=True, exist_ok=True)
        path = leases_dir / f"{work.request_id.value}.json"
        handle = path.open("x+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._write_locked(
                handle,
                ActiveLeaseRecord.from_domain(
                    ActiveExecutorLease(
                        fairness_group=work.fairness_group,
                        grant=grant,
                        exclusive_resources=work.exclusive_resources,
                    )
                ),
            )
        except BaseException as publication_error:
            raise_primary_with_cleanup(
                "executor lease record publication and cleanup failures",
                publication_error,
                _cleanup_record_and_handles(path, [handle]),
            )
        return path, handle

    def _try_acquire_resources(
        self,
        cpu_slots: int,
        exclusive_resources: tuple[ExecutorExclusiveResource, ...],
    ) -> list[BinaryIO] | None:
        handles: list[BinaryIO] = []
        for resource in exclusive_resources:
            handle = _try_lock(self._pool_dir / f"exclusive-{resource.value}.lock")
            if handle is None:
                _release_handles(handles)
                return None
            handles.append(handle)
        for index in range(self.host_cpu_slots):
            handle = _try_lock(self._capacity_lock_path(index))
            if handle is not None:
                handles.append(handle)
                if len(handles) == len(exclusive_resources) + cpu_slots:
                    return handles
        _release_handles(handles)
        return None

    def _all_cpu_slots_are_idle(self, cpu_slots: int) -> bool:
        handles: list[BinaryIO] = []
        try:
            for index in range(cpu_slots):
                handle = _try_lock(self._capacity_lock_path(index))
                if handle is None:
                    return False
                handles.append(handle)
            return True
        finally:
            _release_handles(handles)

    def _capacity_lock_path(self, index: int) -> Path:
        return self._pool_dir / f"capacity-{index:03d}.lock"

    @staticmethod
    def _read_capacity(path: Path) -> int | None:
        if not path.exists():
            return None
        try:
            return CapacityRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            ).capacity_units
        except (OSError, ValidationError) as exc:
            raise RuntimeError(f"invalid host executor capacity: {path}") from exc

    @staticmethod
    def _write_locked(handle: BinaryIO, record: BaseModel) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write((record.model_dump_json() + "\n").encode("utf-8"))
        handle.flush()


def _try_lock(path: Path) -> BinaryIO | None:
    handle = path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as primary_error:
        close_outcome = IndependentCleanupPlan(
            (CleanupAction("unacquired-executor-lock-close", handle.close),)
        ).run()
        if isinstance(primary_error, BlockingIOError):
            raise_cleanup_failures(
                "unacquired executor lock cleanup failed",
                close_outcome,
            )
            return None
        raise_primary_with_cleanup(
            "executor lock acquisition and cleanup failed",
            primary_error,
            close_outcome,
        )
    return handle


def _release_handles(handles: list[BinaryIO]) -> None:
    raise_cleanup_failures(
        "executor lock cleanup failures",
        _cleanup_handles(handles),
    )


def _cleanup_record_and_handles(
    path: Path,
    handles: list[BinaryIO],
) -> CleanupOutcome:
    return IndependentCleanupPlan(
        (
            CleanupAction("unlink executor state record", path.unlink),
            *_locked_handle_cleanup_actions(tuple(reversed(handles))),
        )
    ).run()


def _cleanup_handles(handles: list[BinaryIO]) -> CleanupOutcome:
    return IndependentCleanupPlan(
        _locked_handle_cleanup_actions(tuple(reversed(handles)))
    ).run()


def _transfer_lease_descriptors(
    handles: tuple[BinaryIO, ...],
) -> _ExecutorLeaseDescriptorTransfer:
    retained: list[BinaryIO] = []
    failures: list[BaseException] = []
    for handle in handles:
        try:
            handle.close()
        except BaseException as close_error:
            close_error.add_note("close transferred executor descriptor")
            failures.append(close_error)
            if not handle.closed:
                retained.append(handle)
    return _ExecutorLeaseDescriptorTransfer(tuple(retained), tuple(failures))


def _locked_handle_cleanup_actions(
    handles: tuple[BinaryIO, ...],
) -> tuple[CleanupAction, ...]:
    actions: list[CleanupAction] = []
    for handle in handles:
        actions.extend(
            (
                CleanupAction(
                    "unlock executor state descriptor",
                    lambda owned_handle=handle: fcntl.flock(
                        owned_handle.fileno(),
                        fcntl.LOCK_UN,
                    ),
                ),
                CleanupAction(
                    "close executor state descriptor",
                    handle.close,
                ),
            )
        )
    return tuple(actions)
