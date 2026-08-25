# pyright: strict
"""Cross-process queue and lease adapter for the host executor."""

from __future__ import annotations

import fcntl
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Generator, TypeVar

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


_RecordType = TypeVar("_RecordType", bound=BaseModel)


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
        try:
            self.path.unlink(missing_ok=True)
        finally:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._released = True

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


class HostExecutorLease:
    """Live CPU/resource locks inherited by the admitted child process."""

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
        self._released = False

    def child_file_descriptors(self) -> tuple[int, ...]:
        if self._released:
            raise RuntimeError("cannot inherit a released host executor lease")
        return tuple(handle.fileno() for handle in self._handles)

    def release(self) -> None:
        if self._released:
            raise RuntimeError("host executor lease was released twice")
        try:
            self.path.unlink(missing_ok=True)
        finally:
            _release_handles(self._handles)
            self._released = True


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

    def __init__(self, pool_dir: Path, host_cpu_slots: int) -> None:
        if type(host_cpu_slots) is not int or host_cpu_slots < 1:
            raise ValueError("HostExecutorState capacity must be a positive integer")
        self._pool_dir = pool_dir
        self.host_cpu_slots = host_cpu_slots

    def configure_capacity(self) -> None:
        """Persist capacity, refusing to change it while leases are active."""
        self._pool_dir.mkdir(parents=True, exist_ok=True)
        capacity_path = self._pool_dir / "capacity.json"
        with self._capacity_guard():
            current = self._read_capacity(capacity_path)
            if current is not None and current != self.host_cpu_slots:
                if not self._all_cpu_slots_are_idle(current):
                    raise RuntimeError(
                        "cannot change host executor capacity while leases are active: "
                        f"current={current} requested={self.host_cpu_slots}"
                    )
            if current != self.host_cpu_slots:
                self._write_atomic(
                    capacity_path,
                    CapacityRecord(capacity_units=self.host_cpu_slots),
                )

    def enqueue(self, work: QueuedExecutorWork) -> OwnedQueuedRequest:
        """Create and exclusively own one durable queue record."""
        path = self._pool_dir / "requests" / f"{work.request_id.value}.json"
        with self._queue_guard():
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("x+b")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._write_locked(handle, QueuedWorkRecord.from_domain(work))
        return OwnedQueuedRequest(work, path, handle)

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
            except BaseException:
                _release_handles(handles)
                raise
            handles.append(lease_handle)
            try:
                self._write_group_service(
                    self._add_service(
                        service,
                        current.work.fairness_group,
                        decision.grant.cpu_slots,
                    )
                )
            except BaseException:
                try:
                    lease_path.unlink(missing_ok=True)
                finally:
                    _release_handles(handles)
                raise
            return HostAdmissionGranted(
                HostExecutorLease(decision.grant, lease_path, handles),
                decision,
            )

    @contextmanager
    def _capacity_guard(self) -> Generator[None, None, None]:
        with (self._pool_dir / "capacity.lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    @contextmanager
    def _queue_guard(self) -> Generator[None, None, None]:
        with (self._pool_dir / "queue.lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    def _live_requests(
        self,
        current: OwnedQueuedRequest,
    ) -> tuple[QueuedExecutorWork, ...]:
        requests = [current.work]
        requests_dir = self._pool_dir / "requests"
        if requests_dir.exists():
            for path in sorted(requests_dir.glob("*.json")):
                if path == current.path:
                    continue
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

    @staticmethod
    def _read_live_record(
        path: Path,
        record_type: type[_RecordType],
    ) -> _RecordType | None:
        try:
            handle = path.open("r+b")
        except FileNotFoundError:
            return None
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.seek(0)
                try:
                    return record_type.model_validate_json(handle.read())
                except ValidationError as exc:
                    raise RuntimeError(
                        f"invalid host executor state file: {path}"
                    ) from exc
            path.unlink(missing_ok=True)
            return None
        finally:
            handle.close()

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
        self._write_atomic(self._pool_dir / "group-service.json", record)

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
        except BaseException:
            try:
                path.unlink(missing_ok=True)
            finally:
                handle.close()
            raise
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

    @staticmethod
    def _write_atomic(path: Path, record: BaseModel) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}")
        temporary.write_text(record.model_dump_json() + "\n", encoding="utf-8")
        os.replace(temporary, path)


def _try_lock(path: Path) -> BinaryIO | None:
    handle = path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _release_handles(handles: list[BinaryIO]) -> None:
    for handle in reversed(handles):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
