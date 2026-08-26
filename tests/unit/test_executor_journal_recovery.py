"""Crash-tail recovery proofs for the machine-wide executor event journal."""

from __future__ import annotations

import os
import stat
from collections.abc import Buffer
from pathlib import Path
from typing import IO, Any, BinaryIO, cast

import pytest

from issue_orchestrator.control.executor_admission import (
    ExecutorAdmissionGrant,
    ExecutorLearnedDemand,
    QueuedExecutorWork,
)
from issue_orchestrator.domain.executor import (
    ExecutorAggressiveness,
    ExecutorConcurrencyRange,
    ExecutorFairnessGroup,
    ExecutorPolicy,
    ExecutorPolicyChange,
    ExecutorPolicySource,
    ExecutorWorkKey,
)
from issue_orchestrator.domain.executor_monitoring import (
    ExecutorCommandLifecycleFailed,
    ExecutorRecentEventsQuery,
    ExecutorRequestId,
)
from issue_orchestrator.execution.host_executor._journal import ExecutorEventStore
import issue_orchestrator.execution.posix_file_lock as posix_file_lock
from issue_orchestrator.execution.host_executor._types import (
    ExecutorRepositoryIdentity,
    ExecutorWorkIdentity,
)


def _policy_change(percent: int) -> ExecutorPolicyChange:
    policy = ExecutorPolicy(
        ExecutorAggressiveness(percent),
        ExecutorPolicySource.PERSISTED,
    )
    return ExecutorPolicyChange(saved=policy, effective=policy)


def test_torn_final_event_is_ignored_then_repaired_before_append(
    tmp_path: Path,
) -> None:
    store = ExecutorEventStore(tmp_path)
    store.policy_changed(_policy_change(100))
    with store.path.open("ab") as handle:
        handle.write(b'{"schema_version":4,"event":"policy-changed"')

    before_repair = store.recent_events(ExecutorRecentEventsQuery(10))
    assert len(before_repair.events) == 1

    store.policy_changed(_policy_change(125))

    after_repair = store.recent_events(ExecutorRecentEventsQuery(10))
    assert len(after_repair.events) == 2
    assert store.path.read_bytes().endswith(b"\n")


def test_invalid_interior_event_remains_a_hard_failure(tmp_path: Path) -> None:
    store = ExecutorEventStore(tmp_path)
    store.policy_changed(_policy_change(100))
    with store.path.open("ab") as handle:
        handle.write(b"not-json\n")
    store.policy_changed(_policy_change(125))

    with pytest.raises(RuntimeError, match="invalid executor event"):
        store.recent_events(ExecutorRecentEventsQuery(10))


def test_torn_rotated_event_is_a_hard_failure(tmp_path: Path) -> None:
    store = ExecutorEventStore(tmp_path)
    store.policy_changed(_policy_change(100))
    rotated = store.path.with_suffix(".jsonl.1")
    store.path.replace(rotated)
    with rotated.open("ab") as handle:
        handle.write(b'{"schema_version":4,"event":"policy-changed"')

    with pytest.raises(RuntimeError, match="invalid executor event"):
        store.recent_events(ExecutorRecentEventsQuery(10))


def test_empty_lifecycle_exception_message_remains_durable(tmp_path: Path) -> None:
    store = ExecutorEventStore(tmp_path)
    work = QueuedExecutorWork(
        request_id=ExecutorRequestId("empty-error-message"),
        sequence=1,
        work_key=ExecutorWorkKey("io:empty-error-message"),
        fairness_group=ExecutorFairnessGroup("validation-empty-error"),
        concurrency_range=ExecutorConcurrencyRange(1, 1),
        learned_demand=ExecutorLearnedDemand(1.0),
        aggressiveness=ExecutorAggressiveness(100),
        exclusive_resources=(),
    )
    identity = ExecutorWorkIdentity(
        ExecutorRepositoryIdentity((tmp_path / ".git").resolve(), "journal-test"),
        work.work_key,
    )

    store.command_lifecycle_failed(
        identity,
        work,
        ExecutorAdmissionGrant(1, 1),
        RuntimeError(),
    )

    [event] = store.recent_events(ExecutorRecentEventsQuery(10)).events
    assert type(event) is ExecutorCommandLifecycleFailed
    assert event.error_type == "RuntimeError"
    assert event.error_message == "RuntimeError()"


def test_first_event_create_syncs_file_and_containing_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ExecutorEventStore(tmp_path)
    original_fsync = os.fsync
    synchronized_file_types: list[int] = []

    def observe_fsync(descriptor: int) -> None:
        synchronized_file_types.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observe_fsync)

    store.policy_changed(_policy_change(100))

    assert stat.S_IFREG in synchronized_file_types
    assert stat.S_IFDIR in synchronized_file_types


def test_tail_repair_syncs_containing_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ExecutorEventStore(tmp_path)
    store.policy_changed(_policy_change(100))
    with store.path.open("ab") as handle:
        handle.write(b'{"schema_version":4,"event":"policy-changed"')
    original_fsync = os.fsync
    synchronized_directories = 0

    def observe_fsync(descriptor: int) -> None:
        nonlocal synchronized_directories
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            synchronized_directories += 1
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observe_fsync)

    store.policy_changed(_policy_change(125))

    assert synchronized_directories >= 1


def test_normal_append_inspects_only_final_framing_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ExecutorEventStore(tmp_path)
    store.policy_changed(_policy_change(100))
    original_size = store.path.stat().st_size
    original_pread = os.pread
    inspected_offsets: list[int] = []

    def observe_pread(descriptor: int, size: int, offset: int) -> bytes:
        inspected_offsets.append(offset)
        return original_pread(descriptor, size, offset)

    monkeypatch.setattr(os, "pread", observe_pread)

    store.policy_changed(_policy_change(125))

    assert inspected_offsets == [original_size - 1]


def test_rotation_syncs_containing_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ExecutorEventStore(tmp_path)
    store.path.write_bytes(b"x" * (10 * 1024 * 1024 - 1) + b"\n")
    original_fsync = os.fsync
    synchronized_directories = 0

    def observe_fsync(descriptor: int) -> None:
        nonlocal synchronized_directories
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            synchronized_directories += 1
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observe_fsync)

    store.policy_changed(_policy_change(100))

    assert synchronized_directories >= 2
    assert store.path.with_suffix(".jsonl.1").exists()
    assert store.path.exists()


def test_rotation_replace_failure_preserves_active_and_retained_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ExecutorEventStore(tmp_path)
    active_bytes = b"x" * (10 * 1024 * 1024 - 1) + b"\n"
    retained_bytes = b"previous retained executor generation\n"
    rotated = store.path.with_suffix(".jsonl.1")
    store.path.write_bytes(active_bytes)
    rotated.write_bytes(retained_bytes)

    def fail_rotation_replace(source: Path, destination: Path) -> None:
        assert source == store.path
        assert destination == rotated
        raise OSError("simulated executor event rotation replace failure")

    monkeypatch.setattr(os, "replace", fail_rotation_replace)

    with pytest.raises(
        OSError,
        match="simulated executor event rotation replace failure",
    ):
        store.policy_changed(_policy_change(100))

    assert store.path.read_bytes() == active_bytes
    assert rotated.read_bytes() == retained_bytes


def test_append_preserves_write_journal_close_and_lock_close_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ExecutorEventStore(tmp_path)
    original_write = os.write
    original_close = os.close
    original_fdopen = posix_file_lock.os.fdopen
    event_descriptor_identity: tuple[int, int] | None = None

    class _LockCloseFailingHandle:
        def __init__(self, delegate: BinaryIO) -> None:
            self._delegate = delegate

        @property
        def closed(self) -> bool:
            return self._delegate.closed

        def fileno(self) -> int:
            return self._delegate.fileno()

        def close(self) -> None:
            self._delegate.close()
            raise OSError("simulated executor event lock close failure")

    def fail_lock_stream_close(
        descriptor: int,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        closefd: bool = True,
        opener: Any = None,
    ) -> IO[Any]:
        opened = original_fdopen(
            descriptor,
            mode,
            buffering,
            encoding,
            errors,
            newline,
            closefd,
            opener,
        )
        return cast(BinaryIO, _LockCloseFailingHandle(cast(BinaryIO, opened)))

    def fail_event_write(descriptor: int, payload: Buffer) -> int:
        nonlocal event_descriptor_identity
        if store.path.exists():
            descriptor_stat = os.fstat(descriptor)
            path_stat = store.path.stat()
            if (descriptor_stat.st_dev, descriptor_stat.st_ino) == (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                event_descriptor_identity = (
                    descriptor_stat.st_dev,
                    descriptor_stat.st_ino,
                )
                raise OSError("simulated executor event write failure")
        return original_write(descriptor, payload)

    def fail_event_close(descriptor: int) -> None:
        descriptor_stat = os.fstat(descriptor)
        descriptor_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
        original_close(descriptor)
        if descriptor_identity == event_descriptor_identity:
            raise OSError("simulated executor event close failure")

    monkeypatch.setattr(os, "write", fail_event_write)
    monkeypatch.setattr(os, "close", fail_event_close)
    monkeypatch.setattr(posix_file_lock.os, "fdopen", fail_lock_stream_close)

    with pytest.raises(BaseExceptionGroup) as raised:
        store.policy_changed(_policy_change(100))

    messages = _leaf_exception_messages(raised.value)
    assert "simulated executor event write failure" in messages
    assert "simulated executor event close failure" in messages
    assert "simulated executor event lock close failure" in messages


def _leaf_exception_messages(error: BaseException) -> set[str]:
    if isinstance(error, BaseExceptionGroup):
        return {
            message
            for nested in error.exceptions
            for message in _leaf_exception_messages(nested)
        }
    return {str(error)}
