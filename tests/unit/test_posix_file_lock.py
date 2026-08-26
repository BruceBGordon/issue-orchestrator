"""Public-contract tests for the shared POSIX file-lock lifecycle owner."""

from __future__ import annotations

import ast
import fcntl
from pathlib import Path
from typing import IO, Any, BinaryIO, cast

import pytest

from issue_orchestrator.execution.posix_file_lock import (
    PosixFileLockAcquisition,
    PosixFileLockContended,
    PosixFileLockFilePresence,
    PosixFileLockMode,
    PosixFileLockOwner,
    PosixFileLockOwnershipRetainedError,
    PosixFileLockSpecification,
)
import issue_orchestrator.execution.posix_file_lock as posix_file_lock


class _CloseFailingHandle:
    def __init__(self, delegate: BinaryIO) -> None:
        self._delegate = delegate

    def fileno(self) -> int:
        return self._delegate.fileno()

    @property
    def closed(self) -> bool:
        return self._delegate.closed

    def close(self) -> None:
        self._delegate.close()
        raise OSError("simulated POSIX file-lock close failure")


class _FailBeforeCloseOnceHandle:
    def __init__(self, delegate: BinaryIO) -> None:
        self._delegate = delegate
        self._attempts = 0

    @property
    def closed(self) -> bool:
        return self._delegate.closed

    def fileno(self) -> int:
        return self._delegate.fileno()

    def close(self) -> None:
        self._attempts += 1
        if self._attempts == 1:
            raise OSError("simulated pre-close POSIX file-lock failure")
        self._delegate.close()


@pytest.mark.parametrize(
    "mode",
    (PosixFileLockMode.SHARED, PosixFileLockMode.EXCLUSIVE),
)
def test_body_and_lock_close_failures_are_preserved_for_each_lock_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: PosixFileLockMode,
) -> None:
    lock_path = (tmp_path / f"{mode.value}.lock").resolve()
    original_fdopen = posix_file_lock.os.fdopen

    def fdopen_with_close_failure(
        descriptor: int,
        open_mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        closefd: bool = True,
        opener: Any = None,
    ) -> IO[Any]:
        opened = original_fdopen(
            descriptor,
            open_mode,
            buffering,
            encoding,
            errors,
            newline,
            closefd,
            opener,
        )
        return cast(BinaryIO, _CloseFailingHandle(cast(BinaryIO, opened)))

    monkeypatch.setattr(posix_file_lock.os, "fdopen", fdopen_with_close_failure)
    specification = PosixFileLockSpecification(
        lock_path,
        mode,
        PosixFileLockAcquisition.BLOCKING,
        PosixFileLockFilePresence.CREATE_IF_MISSING,
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        with PosixFileLockOwner().hold(specification):
            raise RuntimeError("simulated protected operation failure")

    assert [str(error) for error in raised.value.exceptions] == [
        "simulated protected operation failure",
        "simulated POSIX file-lock close failure",
    ]


def test_nonblocking_contention_retains_readable_handle_until_release(
    tmp_path: Path,
) -> None:
    record_path = (tmp_path / "live-record.json").resolve()
    record_path.write_text('{"state":"live"}\n', encoding="utf-8")
    with record_path.open("r+b") as live_owner:
        fcntl.flock(live_owner.fileno(), fcntl.LOCK_EX)
        outcome = PosixFileLockOwner().acquire(
            PosixFileLockSpecification(
                record_path,
                PosixFileLockMode.EXCLUSIVE,
                PosixFileLockAcquisition.NON_BLOCKING,
                PosixFileLockFilePresence.REQUIRE_EXISTING,
            )
        )

        assert type(outcome) is PosixFileLockContended
        assert outcome.lease.handle.read() == b'{"state":"live"}\n'
        outcome.lease.release()


def test_require_existing_never_recreates_a_disappeared_record(tmp_path: Path) -> None:
    missing_path = (tmp_path / "missing.json").resolve()

    with pytest.raises(FileNotFoundError):
        PosixFileLockOwner().acquire(
            PosixFileLockSpecification(
                missing_path,
                PosixFileLockMode.EXCLUSIVE,
                PosixFileLockAcquisition.NON_BLOCKING,
                PosixFileLockFilePresence.REQUIRE_EXISTING,
            )
        )

    assert not missing_path.exists()


def test_hold_retains_fail_before_close_owner_for_explicit_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = (tmp_path / "retained.lock").resolve()
    original_fdopen = posix_file_lock.os.fdopen

    def fdopen_with_pre_close_failure(
        descriptor: int,
        open_mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        closefd: bool = True,
        opener: Any = None,
    ) -> IO[Any]:
        opened = original_fdopen(
            descriptor,
            open_mode,
            buffering,
            encoding,
            errors,
            newline,
            closefd,
            opener,
        )
        return cast(
            BinaryIO,
            _FailBeforeCloseOnceHandle(cast(BinaryIO, opened)),
        )

    monkeypatch.setattr(posix_file_lock.os, "fdopen", fdopen_with_pre_close_failure)
    specification = PosixFileLockSpecification(
        lock_path,
        PosixFileLockMode.EXCLUSIVE,
        PosixFileLockAcquisition.BLOCKING,
        PosixFileLockFilePresence.CREATE_IF_MISSING,
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        with PosixFileLockOwner().hold(specification):
            raise RuntimeError("simulated protected operation failure")

    assert [type(error) for error in raised.value.exceptions] == [
        RuntimeError,
        OSError,
        PosixFileLockOwnershipRetainedError,
    ]
    retained = raised.value.exceptions[2]
    assert type(retained) is PosixFileLockOwnershipRetainedError

    # Unlock and close are independent. The failed close remains explicit, but
    # it cannot poison the machine-wide flock while its owner is retained.
    with lock_path.open("r+b") as competitor:
        fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    retained.lease.release()
    with pytest.raises(RuntimeError, match="released twice"):
        retained.lease.release()


def test_new_raw_flock_clients_are_rejected_by_the_owner_guardrail() -> None:
    source_root = Path(__file__).parents[2] / "src" / "issue_orchestrator"
    grandfathered = {
        Path("execution/host_executor/_state.py"),
        Path("execution/posix_file_lock.py"),
        Path("execution/process_cancellation_endpoint.py"),
        Path("infra/hooks/codex_session.py"),
        Path("infra/repo_lock.py"),
        Path("infra/repo_registry.py"),
    }
    raw_flock_clients: set[Path] = set()
    for source_path in source_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "fcntl"
            and node.func.attr == "flock"
            for node in ast.walk(tree)
        ):
            raw_flock_clients.add(source_path.relative_to(source_root))

    assert raw_flock_clients == grandfathered, (
        "new raw flock clients must use PosixFileLockOwner; remove migrated "
        "legacy clients from this #7109 grandfather list"
    )
