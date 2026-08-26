"""Durability and provenance proofs for validation timing history."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Buffer, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from issue_orchestrator.domain.validation_timing import ValidationTimingScalar
from issue_orchestrator.domain.validation_execution import (
    ValidationCommandOutputCapture,
)
from issue_orchestrator.infra.validation_timings import (
    ValidationTimingJournal,
    ValidationTimingJournalCorruptionError,
    ValidationTimingJournalUnavailableError,
    read_branch_name,
    record_gate_timing_journals,
    require_shared_timings_file,
)


@dataclass(frozen=True, slots=True)
class _TimingEvidence:
    sequence: int

    def timing_fields(self) -> Mapping[str, ValidationTimingScalar]:
        return {"kind": "durability-proof", "sequence": self.sequence}


def test_append_completes_partial_writes_and_syncs_file_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "timings" / "validate-timings.jsonl"
    journal = ValidationTimingJournal(path)
    original_write = os.write
    original_fsync = os.fsync
    synchronized_file_types: list[int] = []

    def partial_write(descriptor: int, payload: Buffer) -> int:
        view = memoryview(payload)
        return original_write(descriptor, view[: min(7, len(view))])

    def observe_fsync(descriptor: int) -> None:
        synchronized_file_types.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
        original_fsync(descriptor)

    monkeypatch.setattr(os, "write", partial_write)
    monkeypatch.setattr(os, "fsync", observe_fsync)

    journal.append(_TimingEvidence(7))

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "kind": "durability-proof",
        "sequence": 7,
    }
    assert stat.S_IFREG in synchronized_file_types
    assert stat.S_IFDIR in synchronized_file_types


def test_detached_worktree_does_not_borrow_common_repository_branch(
    tmp_path: Path,
) -> None:
    common_git = tmp_path / "repo" / ".git"
    worktree = tmp_path / "worktree"
    worktree_git = common_git / "worktrees" / "detached"
    worktree.mkdir(parents=True)
    worktree_git.mkdir(parents=True)
    (worktree / ".git").write_text(
        f"gitdir: {worktree_git}\n",
        encoding="utf-8",
    )
    (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree_git / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    (common_git / "HEAD").write_text(
        "ref: refs/heads/main\n",
        encoding="utf-8",
    )

    assert read_branch_name(worktree) is None


def test_torn_final_record_is_truncated_before_the_next_append(
    tmp_path: Path,
) -> None:
    path = tmp_path / "validate-timings.jsonl"
    journal = ValidationTimingJournal(path)
    journal.append(_TimingEvidence(1))
    with path.open("ab") as handle:
        handle.write(b'{"kind":"durability-proof","sequence":')

    journal.append(_TimingEvidence(2))

    records = tuple(
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    )
    assert [record["sequence"] for record in records] == [1, 2]
    assert path.read_bytes().endswith(b"\n")


@pytest.mark.parametrize("invalid_record", (b"[]", b'"scalar"'))
def test_complete_non_object_tail_is_truncated_before_the_next_append(
    tmp_path: Path,
    invalid_record: bytes,
) -> None:
    path = tmp_path / "validate-timings.jsonl"
    journal = ValidationTimingJournal(path)
    journal.append(_TimingEvidence(1))
    with path.open("ab") as handle:
        handle.write(invalid_record)

    journal.append(_TimingEvidence(2))

    records = tuple(
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    )
    assert [record["sequence"] for record in records] == [1, 2]
    assert journal.audit().record_count == 2


def test_full_journal_marker_parser_preserves_state_across_read_chunks(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    stdout_path = (tmp_path / "stdout.log").resolve()
    stderr_path = (tmp_path / "stderr.log").resolve()
    marker = (
        "[validate-timing] START target=split at=2026-08-25T10:00:00-0400\n"
        "[validate-timing] END target=split status=0 elapsed=3s "
        "at=2026-08-25T10:00:03-0400\n"
    )
    stdout_path.write_text(
        ("x" * 65_529) + "\n" + marker,
        encoding="utf-8",
    )
    stderr_path.write_text("", encoding="utf-8")

    record_gate_timing_journals(
        "publish_gate",
        tmp_path,
        "make validate-pr-raw",
        ValidationCommandOutputCapture(
            stdout_path,
            stderr_path,
            retained_tail_bytes=1_024,
        ),
    )

    records = tuple(
        json.loads(line)
        for line in require_shared_timings_file(tmp_path)
        .read_text(encoding="utf-8")
        .splitlines()
    )
    target = next(record for record in records if record["kind"] == "target_timing")
    assert target["target"] == "split"
    assert target["elapsed_seconds"] == 3


def test_oversized_markers_are_bounded_once_per_stream_across_chunk_boundaries(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    stdout_path = (tmp_path / "stdout.log").resolve()
    stderr_path = (tmp_path / "stderr.log").resolve()
    oversized = "[validate-timing] " + ("x" * 20_000) + "\n"
    stdout_path.write_text(
        ("n" * 65_529) + "\n" + oversized,
        encoding="utf-8",
    )
    stderr_path.write_text(oversized, encoding="utf-8")

    record_gate_timing_journals(
        "publish_gate",
        tmp_path,
        "make validate-pr-raw",
        ValidationCommandOutputCapture(
            stdout_path,
            stderr_path,
            retained_tail_bytes=1_024,
        ),
    )

    failures = tuple(
        record
        for record in (
            json.loads(line)
            for line in require_shared_timings_file(tmp_path)
            .read_text(encoding="utf-8")
            .splitlines()
        )
        if record["kind"] == "timing_protocol_failure"
    )
    assert len(failures) == 2
    assert all(failure["failure_kind"] == "malformed-marker" for failure in failures)
    assert all(failure["line_truncated"] is True for failure in failures)
    assert all(len(failure["line"]) == 16_384 for failure in failures)


def test_hot_append_does_not_scan_prefix_and_full_audit_rejects_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "validate-timings.jsonl"
    journal = ValidationTimingJournal(path)
    journal.append(_TimingEvidence(1))
    corrupted = path.read_bytes() + b"not-json\n"
    path.write_bytes(corrupted)
    original_pread = os.pread
    inspected_offsets: list[int] = []

    def observe_pread(descriptor: int, size: int, offset: int) -> bytes:
        inspected_offsets.append(offset)
        return original_pread(descriptor, size, offset)

    monkeypatch.setattr(os, "pread", observe_pread)

    journal.append(_TimingEvidence(2))

    assert inspected_offsets == [len(corrupted) - 1]
    with pytest.raises(
        ValidationTimingJournalCorruptionError,
        match=r"invalid validation timing record at .*:2",
    ):
        journal.audit()

    assert path.read_bytes().startswith(corrupted)
    assert json.loads(path.read_bytes().splitlines()[-1])["sequence"] == 2


def test_concurrent_partial_appends_remain_complete_json_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "validate-timings.jsonl"
    journal = ValidationTimingJournal(path)
    original_write = os.write

    def partial_write(descriptor: int, payload: Buffer) -> int:
        view = memoryview(payload)
        return original_write(descriptor, view[: min(3, len(view))])

    monkeypatch.setattr(os, "write", partial_write)

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = tuple(
            workers.map(
                journal.append,
                (_TimingEvidence(sequence) for sequence in range(40)),
            )
        )

    assert results == (None,) * 40
    records = tuple(
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    )
    assert len(records) == 40
    assert {record["sequence"] for record in records} == set(range(40))
    assert journal.audit().record_count == 40


def test_append_preserves_write_and_journal_close_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "validate-timings.jsonl"
    journal = ValidationTimingJournal(path)
    original_write = os.write
    original_close = os.close
    journal_descriptor_identity: tuple[int, int] | None = None

    def fail_journal_write(descriptor: int, payload: Buffer) -> int:
        nonlocal journal_descriptor_identity
        if path.exists():
            descriptor_stat = os.fstat(descriptor)
            path_stat = path.stat()
            if (descriptor_stat.st_dev, descriptor_stat.st_ino) == (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                journal_descriptor_identity = (
                    descriptor_stat.st_dev,
                    descriptor_stat.st_ino,
                )
                raise OSError("simulated validation timing write failure")
        return original_write(descriptor, payload)

    def fail_journal_close(descriptor: int) -> None:
        descriptor_stat = os.fstat(descriptor)
        descriptor_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
        original_close(descriptor)
        if descriptor_identity == journal_descriptor_identity:
            raise OSError("simulated validation timing close failure")

    monkeypatch.setattr(os, "write", fail_journal_write)
    monkeypatch.setattr(os, "close", fail_journal_close)

    with pytest.raises(BaseExceptionGroup) as raised:
        journal.append(_TimingEvidence(1))

    messages = _leaf_exception_messages(raised.value)
    assert "simulated validation timing write failure" in messages
    assert "simulated validation timing close failure" in messages


def test_missing_git_common_directory_fails_before_timing_is_claimed(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValidationTimingJournalUnavailableError,
        match="has no Git common directory",
    ):
        require_shared_timings_file(tmp_path)


def _leaf_exception_messages(error: BaseException) -> set[str]:
    if isinstance(error, BaseExceptionGroup):
        return {
            message
            for nested in error.exceptions
            for message in _leaf_exception_messages(nested)
        }
    return {str(error)}
