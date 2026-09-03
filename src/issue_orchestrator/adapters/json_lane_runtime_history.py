# pyright: strict
"""File-backed lane runtime history — one small JSON file per lane.

Per-key files keep concurrent lanes from contending: the fifteen-odd
lanes of one gate finish near-simultaneously, and each rewrites only
its own file via an atomic replace. Two gates racing the *same* lane
serialize on a per-key lock file (a stable sibling, never replaced —
locking the data file itself would race across os.replace inodes), so
every successful observation is persisted.

The store keeps only the last ``window`` observations per lane and per
dimension, so history re-converges by itself when a lane's cost drifts
or the hardware changes underneath it — there is nothing to invalidate
because nothing is baked.

Two dimensions, two files
-------------------------

Runtimes live in ``<key>.json`` and busy cores in
``busy-cores/<key>.json``. They are separated because this store is
shared by every worktree of the repository, and a worktree checked out
at a commit that predates the CPU dimension still runs gates: its
writer rewrites ``<key>.json`` wholesale with runtimes only, silently
erasing any sibling key inside that file (A, #7136 review — reproduced
against the real writer). A file older code does not know about is a
file older code cannot destroy, so ``<key>.json`` keeps EXACTLY the
legacy shape and the new dimension lives beside it.

Both dimensions are still written under the one legacy lock file, so a
new writer and an old writer serialize against each other rather than
racing. Writes to the two files are individually atomic but not atomic
as a pair: a crash between them loses at most one dimension of one
run, which the rolling window absorbs.

A subdirectory rather than a ``<key>.cpu.json`` suffix, because work
keys may legitimately contain dots (``execenv.memory-ok``): a lane
named ``test-unit.cpu`` would otherwise own the same path as
``test-unit``'s CPU file.

The dimensions also roll INDEPENDENTLY. Every backend reports a
runtime, but only a backend whose measuring conditions match the
consumer of the number reports busy cores, so a lane can hold five
runtimes and two measurements. Pairing them positionally would
silently attribute one run's CPU figure to another run's duration.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import statistics
import tempfile
from pathlib import Path
from typing import cast

from ..domain.lane_execution import LaneWorkKey
from ..ports.lane_runtime_history import LaneRuntimeHistoryError

_ROLLING_WINDOW = 5
_FILE_SUFFIX = ".json"
_SAFE_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RUNTIMES_FIELD = "runtimes"
_BUSY_CORES_FIELD = "busy_cores"
_BUSY_CORES_SUBDIRECTORY = "busy-cores"

__all__ = ["JsonLaneRuntimeHistory", "LaneRuntimeHistoryError"]


class JsonLaneRuntimeHistory:
    """Rolling per-lane cost history under one directory."""

    def __init__(self, directory: Path, window: int = _ROLLING_WINDOW) -> None:
        if not isinstance(cast(object, directory), Path) or not directory.is_absolute():
            raise ValueError(
                "JsonLaneRuntimeHistory.directory must be an absolute Path"
            )
        if type(window) is not int or window < 1:
            raise ValueError(
                "JsonLaneRuntimeHistory.window must be a positive integer"
            )
        self._directory = directory
        self._window = window

    def record_success(
        self,
        work_key: LaneWorkKey,
        runtime_seconds: float,
        busy_cores: float | None,
    ) -> None:
        if type(work_key) is not LaneWorkKey:
            raise ValueError("record_success requires a LaneWorkKey")
        _require_measurement("runtime_seconds", runtime_seconds)
        if busy_cores is not None:
            _require_measurement("busy_cores", busy_cores)
        # The store is shared across worktrees by design, so two gates
        # can finish the same lane near-simultaneously. An unlocked
        # read-modify-replace loses whichever record lands first; the
        # per-key lock serializes the update so every success is
        # persisted (B2, #7117 review). The lock path is deliberately
        # the LEGACY one: a worktree running older code takes the same
        # lock, so new and old writers serialize instead of racing.
        lock_path = self._runtime_path(work_key).with_suffix(".lock")
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "w") as lock_handle:
                fcntl.flock(lock_handle, fcntl.LOCK_EX)
                self._append(
                    self._runtime_path(work_key), _RUNTIMES_FIELD, runtime_seconds
                )
                if busy_cores is not None:
                    self._append(
                        self._busy_cores_path(work_key),
                        _BUSY_CORES_FIELD,
                        busy_cores,
                    )
        except OSError as error:
            raise LaneRuntimeHistoryError(
                f"cannot lock lane runtime history at {lock_path}: {error}"
            ) from error

    def learned_priority(self, work_key: LaneWorkKey) -> int:
        if type(work_key) is not LaneWorkKey:
            raise ValueError("learned_priority requires a LaneWorkKey")
        runtimes = _read(self._runtime_path(work_key), _RUNTIMES_FIELD)
        if not runtimes:
            return 0
        return max(0, round(statistics.median(runtimes)))

    def learned_busy_cores(self, work_key: LaneWorkKey) -> float | None:
        if type(work_key) is not LaneWorkKey:
            raise ValueError("learned_busy_cores requires a LaneWorkKey")
        cores = _read(self._busy_cores_path(work_key), _BUSY_CORES_FIELD)
        if not cores:
            # Never-measured is not zero-CPU: the caller's declared
            # seed answers, and a zero here would silently floor every
            # lane's request at one core.
            return None
        return float(statistics.median(cores))

    def _append(self, path: Path, field: str, value: float) -> None:
        """Read-modify-write one dimension's file; caller holds the lock.

        The runtime file is written with EXACTLY the legacy payload —
        one key, sorted — so a worktree on older code reads it without
        knowing anything changed, and so a diff of the two writers'
        output is empty.
        """
        values = [*_read(path, field), round(value, 3)]
        _write(path, field, values[-self._window :])

    def _runtime_path(self, work_key: LaneWorkKey) -> Path:
        return self._directory / f"{self._safe_name(work_key)}{_FILE_SUFFIX}"

    def _busy_cores_path(self, work_key: LaneWorkKey) -> Path:
        return (
            self._directory
            / _BUSY_CORES_SUBDIRECTORY
            / f"{self._safe_name(work_key)}{_FILE_SUFFIX}"
        )

    @staticmethod
    def _safe_name(work_key: LaneWorkKey) -> str:
        # The work-key grammar is already filesystem-safe; assert it
        # anyway so a future grammar change cannot silently turn keys
        # into path traversal.
        if not _SAFE_KEY_PATTERN.match(work_key.value):
            raise LaneRuntimeHistoryError(
                f"work key is not filesystem-safe: {work_key.value!r}"
            )
        return work_key.value


def _require_measurement(field_name: str, value: float) -> None:
    """Both dimensions satisfy one invariant: finite and non-negative."""
    if type(value) is not float or not math.isfinite(value) or value < 0:
        raise ValueError(
            f"record_success {field_name} must be finite and non-negative"
        )


def _shape_error(path: Path) -> LaneRuntimeHistoryError:
    return LaneRuntimeHistoryError(
        f"lane runtime history at {path} has an unexpected shape "
        "(delete the file to reset this lane)"
    )


def _read(path: Path, field: str) -> tuple[float, ...]:
    """One dimension's persisted values; absence is the naive state."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise LaneRuntimeHistoryError(
            f"cannot read lane runtime history at {path}: {error}"
        ) from error
    try:
        payload = cast(object, json.loads(raw))
    except json.JSONDecodeError as error:
        raise LaneRuntimeHistoryError(
            f"lane runtime history at {path} is corrupt "
            f"(delete the file to reset this lane): {error}"
        ) from error
    if not isinstance(payload, dict):
        raise _shape_error(path)
    return _measurements(path, field, cast(dict[str, object], payload).get(field))


def _measurements(path: Path, dimension: str, entries: object) -> tuple[float, ...]:
    """Validate one persisted dimension; both obey the same invariant.

    ``dimension`` only names the offending list in the error, so a
    reader is told which of the two is corrupt instead of guessing.
    """
    if not isinstance(entries, list):
        raise _shape_error(path)
    values: list[float] = []
    for entry in cast(list[object], entries):
        # Both representations must satisfy the same invariant:
        # a finite, non-negative number. A negative integer is as
        # corrupt as a NaN (B3, #7117 review).
        if type(entry) is int and entry >= 0:
            values.append(float(entry))
        elif type(entry) is float and math.isfinite(entry) and entry >= 0:
            values.append(entry)
        else:
            raise LaneRuntimeHistoryError(
                f"lane runtime history at {path} holds a non-measurement "
                f"{dimension} entry {entry!r} "
                "(delete the file to reset this lane)"
            )
    return tuple(values)


def _write(path: Path, field: str, values: list[float]) -> None:
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({field: values}, sort_keys=True).encode("utf-8")
        handle, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            # POSIX may write fewer bytes without raising (storage
            # exhausted, notably). Replacing on a short write would
            # install a TRUNCATED file over valid history and report
            # success — the corruption surfacing only on a later
            # run. Verify the full payload landed before the swap
            # (B5, #7117 review; same check as infra append_jsonl).
            written = os.write(handle, payload)
            if written != len(payload):
                raise OSError(f"short write: {written} of {len(payload)} bytes")
        finally:
            os.close(handle)
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        raise LaneRuntimeHistoryError(
            f"cannot persist lane runtime history at {path}: {error}"
        ) from error
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
