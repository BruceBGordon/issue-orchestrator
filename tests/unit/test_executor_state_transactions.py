"""Queue-record transaction proofs for host executor state."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import BinaryIO

import pytest
from pydantic import BaseModel

from issue_orchestrator.control.executor_admission import (
    ExecutorAdmissionGrant,
    ExecutorLearnedDemand,
    QueuedExecutorWork,
)
from issue_orchestrator.domain.executor import (
    ExecutorAggressiveness,
    ExecutorConcurrencyRange,
    ExecutorFairnessGroup,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.executor_monitoring import ExecutorRequestId
from issue_orchestrator.execution.atomic_record_store import (
    ExecutorAtomicRecordStore,
    OsAtomicPathReplacement,
)
from issue_orchestrator.execution.host_executor._state import (
    HostExecutorLease,
    HostExecutorState,
)


def _work() -> QueuedExecutorWork:
    return QueuedExecutorWork(
        request_id=ExecutorRequestId("publish-once"),
        sequence=1,
        work_key=ExecutorWorkKey("io:state-transaction"),
        fairness_group=ExecutorFairnessGroup("validation-state"),
        concurrency_range=ExecutorConcurrencyRange(1, 1),
        learned_demand=ExecutorLearnedDemand(1.0),
        aggressiveness=ExecutorAggressiveness(100),
        exclusive_resources=(),
    )


def test_failed_enqueue_publication_unlinks_and_unlocks_partial_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_dir = (tmp_path / "pool").resolve()
    pool_dir.mkdir()
    state = HostExecutorState(
        pool_dir,
        4,
        ExecutorAtomicRecordStore(pool_dir, OsAtomicPathReplacement()),
    )
    original_write = HostExecutorState._write_locked  # noqa: SLF001 - fault boundary
    attempt_count = 0

    def fail_once(handle: BinaryIO, record: BaseModel) -> None:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            handle.write(b"{")
            handle.flush()
            raise OSError("injected queue publication failure")
        original_write(handle, record)

    monkeypatch.setattr(HostExecutorState, "_write_locked", staticmethod(fail_once))

    with pytest.raises(OSError, match="injected queue publication failure"):
        state.enqueue(_work())

    request_path = pool_dir / "requests" / "publish-once.json"
    assert not request_path.exists()
    with state.enqueue(_work()) as owned:
        assert owned.path == request_path


def test_failed_enqueue_preserves_publication_and_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_dir = (tmp_path / "pool").resolve()
    pool_dir.mkdir()
    state = HostExecutorState(
        pool_dir,
        4,
        ExecutorAtomicRecordStore(pool_dir, OsAtomicPathReplacement()),
    )
    request_path = pool_dir / "requests" / "publish-once.json"

    def fail_write(handle: BinaryIO, record: BaseModel) -> None:
        del record
        handle.write(b"{")
        handle.flush()
        raise OSError("injected queue publication failure")

    real_unlink = Path.unlink

    def fail_request_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == request_path:
            raise PermissionError("injected queue unlink failure")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(HostExecutorState, "_write_locked", staticmethod(fail_write))
    monkeypatch.setattr(Path, "unlink", fail_request_unlink)

    with pytest.raises(BaseExceptionGroup) as raised:
        state.enqueue(_work())

    assert [type(error) for error in raised.value.exceptions] == [
        OSError,
        PermissionError,
    ]
    assert "injected queue publication failure" in str(raised.value.exceptions[0])
    assert "injected queue unlink failure" in str(raised.value.exceptions[1])
    with request_path.open("r+b") as handle:
        # Publication cleanup still released and closed its owner handle even
        # though unlinking the partial file failed.
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    monkeypatch.setattr(Path, "unlink", real_unlink)
    request_path.unlink()


def test_lease_transfer_closes_local_owner_without_unlocking_guardian_copy(
    tmp_path: Path,
) -> None:
    lease_path = tmp_path / "lease.json"
    lease_path.write_text("{}\n", encoding="utf-8")
    local_handle = lease_path.open("r+b")
    fcntl.flock(local_handle.fileno(), fcntl.LOCK_EX)
    guardian_descriptor = os.dup(local_handle.fileno())
    lease = HostExecutorLease(
        ExecutorAdmissionGrant(concurrency=1, cpu_slots=1),
        lease_path,
        [local_handle],
    )

    assert lease.inherited_file_descriptors() == (local_handle.fileno(),)
    lease.transfer_to_guardian()

    assert local_handle.closed
    with lease_path.open("r+b") as competitor:
        with pytest.raises(BlockingIOError):
            fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    os.close(guardian_descriptor)
    with lease_path.open("r+b") as competitor:
        fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    lease.release()
    assert not lease_path.exists()
