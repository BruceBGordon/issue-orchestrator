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

The two dimensions (runtime, busy cores) roll independently. They are
NOT parallel arrays: every run reports a runtime, but only measured
runs report busy cores, so a lane can hold five runtimes and two
measurements. Pairing them positionally would silently attribute one
run's CPU figure to another run's duration.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..domain.lane_execution import LaneWorkKey
from ..ports.lane_runtime_history import LaneRuntimeHistoryError

_ROLLING_WINDOW = 5
_FILE_SUFFIX = ".json"
_SAFE_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RUNTIMES_FIELD = "runtimes"
_BUSY_CORES_FIELD = "busy_cores"

__all__ = ["JsonLaneRuntimeHistory", "LaneRuntimeHistoryError"]


@dataclass(frozen=True, slots=True)
class _LaneObservations:
    """One lane's persisted history, both dimensions."""

    runtimes: tuple[float, ...]
    busy_cores: tuple[float, ...]


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
        # persisted (B2, #7117 review). Both dimensions are written
        # under the SAME lock acquisition: two locks would let a
        # concurrent writer interleave between them.
        lock_path = self._path(work_key).with_suffix(".lock")
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "w") as lock_handle:
                fcntl.flock(lock_handle, fcntl.LOCK_EX)
                observations = self._read(work_key)
                runtimes = [*observations.runtimes, round(runtime_seconds, 3)]
                cores = list(observations.busy_cores)
                if busy_cores is not None:
                    cores.append(round(busy_cores, 3))
                self._write(
                    work_key,
                    _LaneObservations(
                        tuple(runtimes[-self._window :]),
                        tuple(cores[-self._window :]),
                    ),
                )
        except OSError as error:
            raise LaneRuntimeHistoryError(
                f"cannot lock lane runtime history at {lock_path}: {error}"
            ) from error

    def learned_priority(self, work_key: LaneWorkKey) -> int:
        if type(work_key) is not LaneWorkKey:
            raise ValueError("learned_priority requires a LaneWorkKey")
        runtimes = self._read(work_key).runtimes
        if not runtimes:
            return 0
        return max(0, round(statistics.median(runtimes)))

    def learned_busy_cores(self, work_key: LaneWorkKey) -> float | None:
        if type(work_key) is not LaneWorkKey:
            raise ValueError("learned_busy_cores requires a LaneWorkKey")
        cores = self._read(work_key).busy_cores
        if not cores:
            # Never-measured is not zero-CPU: the caller's declared
            # seed answers, and a zero here would silently floor every
            # lane's request at one core.
            return None
        return float(statistics.median(cores))

    def _path(self, work_key: LaneWorkKey) -> Path:
        # The work-key grammar is already filesystem-safe; assert it
        # anyway so a future grammar change cannot silently turn keys
        # into path traversal.
        if not _SAFE_KEY_PATTERN.match(work_key.value):
            raise LaneRuntimeHistoryError(
                f"work key is not filesystem-safe: {work_key.value!r}"
            )
        return self._directory / f"{work_key.value}{_FILE_SUFFIX}"

    def _read(self, work_key: LaneWorkKey) -> _LaneObservations:
        path = self._path(work_key)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _LaneObservations((), ())
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
        fields = cast(dict[str, object], payload)
        return _LaneObservations(
            _measurements(path, _RUNTIMES_FIELD, fields.get(_RUNTIMES_FIELD)),
            # A file written before the CPU dimension existed has no
            # busy_cores key at all. Absence of a dimension is the
            # naive state — the same "nothing known yet" as an absent
            # file — so it reads as empty. A key that IS present but
            # holds a non-list is a writer bug and stays corrupt.
            _measurements(
                path, _BUSY_CORES_FIELD, fields.get(_BUSY_CORES_FIELD, [])
            ),
        )

    def _write(self, work_key: LaneWorkKey, observations: _LaneObservations) -> None:
        path = self._path(work_key)
        temporary: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {
                    _RUNTIMES_FIELD: list(observations.runtimes),
                    _BUSY_CORES_FIELD: list(observations.busy_cores),
                },
                sort_keys=True,
            ).encode("utf-8")
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
                    raise OSError(
                        f"short write: {written} of {len(payload)} bytes"
                    )
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
