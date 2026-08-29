# pyright: strict
"""File-backed lane runtime history — one small JSON file per lane.

Per-key files keep concurrent lanes from contending: the fifteen-odd
lanes of one gate finish near-simultaneously, and each rewrites only
its own file via an atomic replace. Two gates racing the *same* lane
serialize on a per-key lock file (a stable sibling, never replaced —
locking the data file itself would race across os.replace inodes), so
every successful observation is persisted.

The store keeps only the last ``window`` runtimes per lane, so history
re-converges by itself when a lane's cost drifts or the hardware
changes underneath it — there is nothing to invalidate because nothing
is baked.
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

__all__ = ["JsonLaneRuntimeHistory", "LaneRuntimeHistoryError"]


class JsonLaneRuntimeHistory:
    """Rolling per-lane runtime history under one directory."""

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

    def record_success(self, work_key: LaneWorkKey, runtime_seconds: float) -> None:
        if type(work_key) is not LaneWorkKey:
            raise ValueError("record_success requires a LaneWorkKey")
        if (
            type(runtime_seconds) is not float
            or not math.isfinite(runtime_seconds)
            or runtime_seconds < 0
        ):
            raise ValueError(
                "record_success runtime_seconds must be finite and non-negative"
            )
        # The store is shared across worktrees by design, so two gates
        # can finish the same lane near-simultaneously. An unlocked
        # read-modify-replace loses whichever record lands first; the
        # per-key lock serializes the update so every success is
        # persisted (B2, #7117 review).
        lock_path = self._path(work_key).with_suffix(".lock")
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "w") as lock_handle:
                fcntl.flock(lock_handle, fcntl.LOCK_EX)
                runtimes = self._read(work_key)
                runtimes.append(round(runtime_seconds, 3))
                self._write(work_key, runtimes[-self._window :])
        except OSError as error:
            raise LaneRuntimeHistoryError(
                f"cannot lock lane runtime history at {lock_path}: {error}"
            ) from error

    def learned_priority(self, work_key: LaneWorkKey) -> int:
        if type(work_key) is not LaneWorkKey:
            raise ValueError("learned_priority requires a LaneWorkKey")
        runtimes = self._read(work_key)
        if not runtimes:
            return 0
        return max(0, round(statistics.median(runtimes)))

    def _path(self, work_key: LaneWorkKey) -> Path:
        # The work-key grammar is already filesystem-safe; assert it
        # anyway so a future grammar change cannot silently turn keys
        # into path traversal.
        if not _SAFE_KEY_PATTERN.match(work_key.value):
            raise LaneRuntimeHistoryError(
                f"work key is not filesystem-safe: {work_key.value!r}"
            )
        return self._directory / f"{work_key.value}{_FILE_SUFFIX}"

    def _read(self, work_key: LaneWorkKey) -> list[float]:
        path = self._path(work_key)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
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
            raise LaneRuntimeHistoryError(
                f"lane runtime history at {path} has an unexpected shape "
                "(delete the file to reset this lane)"
            )
        entries = cast(dict[str, object], payload).get("runtimes")
        if not isinstance(entries, list):
            raise LaneRuntimeHistoryError(
                f"lane runtime history at {path} has an unexpected shape "
                "(delete the file to reset this lane)"
            )
        runtimes: list[float] = []
        for entry in cast(list[object], entries):
            # Both representations must satisfy the same invariant:
            # a finite, non-negative number. A negative integer is as
            # corrupt as a NaN (B3, #7117 review).
            if type(entry) is int and entry >= 0:
                runtimes.append(float(entry))
            elif type(entry) is float and math.isfinite(entry) and entry >= 0:
                runtimes.append(entry)
            else:
                raise LaneRuntimeHistoryError(
                    f"lane runtime history at {path} holds a non-runtime "
                    f"entry {entry!r} (delete the file to reset this lane)"
                )
        return runtimes

    def _write(self, work_key: LaneWorkKey, runtimes: list[float]) -> None:
        path = self._path(work_key)
        temporary: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"runtimes": runtimes}, sort_keys=True).encode(
                "utf-8"
            )
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
