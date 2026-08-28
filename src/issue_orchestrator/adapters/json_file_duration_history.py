# pyright: strict
"""File-backed per-file duration history — one small directory.

Storage discipline is deliberately identical to the lane runtime
history next door (``json_lane_runtime_history.py``): a flock on a
stable sibling lock file (never on a data file, which is replaced by
inode), an atomic ``os.replace`` of a fully-written temporary, and a
rolling window per key so history re-converges by itself when a file's
cost drifts or the hardware changes. Nothing is baked, so nothing needs
invalidating.

Two kinds of file live here:

``history.json``
    The rolling observations. One document rather than one file per
    key, because a reader wants *every* weight at once (it is
    partitioning the whole live file list) while writers hold the lock
    for microseconds and carry disjoint key sets, so the
    read-merge-replace under the lock keeps every writer's facts.

``pinned-<epoch>.json``
    An epoch's frozen answer. The slices of one gate ask minutes apart
    and each teaches the history as it finishes, so an unpinned read
    would hand the last slice a different partition than the first —
    and two different partitions of one file list can drop a file
    between them. The first ask of an epoch publishes the snapshot;
    every later ask, from any process, is answered from it.
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
from typing import Mapping, TextIO, cast

_ROLLING_WINDOW = 5
_RETAINED_EPOCHS = 10
_HISTORY_FILENAME = "history.json"
_LOCK_FILENAME = "history.lock"
_PINNED_PREFIX = "pinned-"
_PINNED_SUFFIX = ".json"
_DURATIONS_FIELD = "durations"
_WEIGHTS_FIELD = "weights"
_SAFE_EPOCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class FileDurationHistoryError(RuntimeError):
    """The store itself is broken — corrupt state, not absence.

    Raised loudly instead of degrading to naive behavior: a corrupt
    store means something wrote garbage, and a silent reset would hide
    the writer's bug and quietly stop the learning loop forever.
    Absence is never an error — an empty store is the naive first run.
    """


class JsonFileDurationHistory:
    """Rolling per-file duration history under one directory."""

    def __init__(self, directory: Path, window: int = _ROLLING_WINDOW) -> None:
        if not isinstance(cast(object, directory), Path) or not directory.is_absolute():
            raise ValueError(
                "JsonFileDurationHistory.directory must be an absolute Path"
            )
        if type(window) is not int or window < 1:
            raise ValueError(
                "JsonFileDurationHistory.window must be a positive integer"
            )
        self._directory = directory
        self._window = window

    def record_success(self, durations: Mapping[str, float]) -> None:
        observations = _validated(durations)
        if not observations:
            return
        with self._locked():
            recorded = self._read_history()
            for path, seconds in observations.items():
                window = recorded.get(path, [])
                window.append(seconds)
                recorded[path] = window[-self._window :]
            self._write_json(
                self._directory / _HISTORY_FILENAME, {_DURATIONS_FIELD: recorded}
            )

    def pinned_weights(self, epoch: str) -> Mapping[str, float]:
        pinned_path = self._pinned_path(epoch)
        published = self._read_pinned(pinned_path)
        if published is not None:
            return published
        with self._locked():
            # Re-read inside the lock: a sibling slice of the same gate
            # may have published between our miss and our turn, and the
            # whole point of the pin is that it wins for everyone.
            published = self._read_pinned(pinned_path)
            if published is not None:
                return published
            weights = {
                path: statistics.median(window)
                for path, window in self._read_history().items()
                if window
            }
            self._write_json(pinned_path, {_WEIGHTS_FIELD: weights})
            self._prune_pinned()
            return weights

    def _locked(self) -> "_DirectoryLock":
        self._directory.mkdir(parents=True, exist_ok=True)
        return _DirectoryLock(self._directory / _LOCK_FILENAME)

    def _pinned_path(self, epoch: str) -> Path:
        # The epoch becomes a filename, so assert the grammar rather
        # than trusting every future caller not to hand us a path.
        if type(epoch) is not str or not _SAFE_EPOCH_PATTERN.match(epoch):
            raise ValueError(f"epoch is not filesystem-safe: {epoch!r}")
        return self._directory / f"{_PINNED_PREFIX}{epoch}{_PINNED_SUFFIX}"

    def _prune_pinned(self) -> None:
        """Keep the newest few epochs; older ones can have no readers.

        Runs under the lock, and only the epochs beyond the retention
        window are removed — a gate still asking for one of those has
        been running for ten gates and has bigger problems.
        """
        try:
            snapshots = sorted(
                self._directory.glob(f"{_PINNED_PREFIX}*{_PINNED_SUFFIX}"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for stale in snapshots[_RETAINED_EPOCHS:]:
            try:
                stale.unlink()
            except OSError:
                pass

    def _read_pinned(self, path: Path) -> dict[str, float] | None:
        payload = self._read_json(path)
        if payload is None:
            return None
        weights = payload.get(_WEIGHTS_FIELD)
        if not isinstance(weights, dict):
            raise self._corrupt(path, f"has no {_WEIGHTS_FIELD!r} mapping")
        return {
            key: self._duration(path, key, value)
            for key, value in cast(dict[str, object], weights).items()
        }

    def _read_history(self) -> dict[str, list[float]]:
        path = self._directory / _HISTORY_FILENAME
        payload = self._read_json(path)
        if payload is None:
            return {}
        entries = payload.get(_DURATIONS_FIELD)
        if not isinstance(entries, dict):
            raise self._corrupt(path, f"has no {_DURATIONS_FIELD!r} mapping")
        return {
            key: self._read_window(path, key, window)
            for key, window in cast(dict[str, object], entries).items()
        }

    def _read_window(self, path: Path, key: str, window: object) -> list[float]:
        if not isinstance(window, list):
            raise self._corrupt(path, f"holds a non-list window for {key!r}")
        seconds = [
            self._duration(path, key, entry) for entry in cast(list[object], window)
        ]
        return seconds[-self._window :]

    def _duration(self, path: Path, key: str, entry: object) -> float:
        """One stored duration, or a loud failure.

        Both representations must satisfy the same invariant: a finite,
        non-negative number. A negative int is as corrupt as a NaN.
        """
        if type(entry) is int and entry >= 0:
            return float(entry)
        if type(entry) is float and math.isfinite(entry) and entry >= 0:
            return entry
        raise self._corrupt(path, f"holds a non-duration entry {entry!r} for {key!r}")

    def _read_json(self, path: Path) -> dict[str, object] | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise FileDurationHistoryError(
                f"cannot read file duration history at {path}: {error}"
            ) from error
        try:
            payload = cast(object, json.loads(raw))
        except json.JSONDecodeError as error:
            raise self._corrupt(path, f"is not JSON: {error}") from error
        if not isinstance(payload, dict):
            raise self._corrupt(path, "has an unexpected shape")
        return cast(dict[str, object], payload)

    def _corrupt(self, path: Path, detail: str) -> FileDurationHistoryError:
        return FileDurationHistoryError(
            f"file duration history at {path} {detail} "
            "(delete the file to reset the learned weights)"
        )

    def _write_json(self, path: Path, payload: Mapping[str, object]) -> None:
        temporary: str | None = None
        try:
            encoded = json.dumps(dict(payload), sort_keys=True).encode("utf-8")
            handle, temporary = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            try:
                # POSIX may write fewer bytes without raising (storage
                # exhausted, notably). Replacing on a short write would
                # install a TRUNCATED file over valid history and report
                # success, the corruption surfacing only on a later run.
                written = os.write(handle, encoded)
                if written != len(encoded):
                    raise OSError(f"short write: {written} of {len(encoded)} bytes")
            finally:
                os.close(handle)
            os.replace(temporary, path)
            temporary = None
        except OSError as error:
            raise FileDurationHistoryError(
                f"cannot persist file duration history at {path}: {error}"
            ) from error
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


class _DirectoryLock:
    """Exclusive access to one store directory.

    The lock file is a stable sibling that is never replaced: locking a
    data file would race across the inodes ``os.replace`` swaps in.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: TextIO | None = None

    def __enter__(self) -> "_DirectoryLock":
        try:
            self._handle = open(self._path, "w")
            fcntl.flock(self._handle, fcntl.LOCK_EX)
        except OSError as error:
            raise FileDurationHistoryError(
                f"cannot lock file duration history at {self._path}: {error}"
            ) from error
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class InertFileDurationHistory:
    """No-repo stand-in: always naive, never persists.

    Outside a repository there is no shared home for the history and
    nothing worth learning across invocations, so the loop degrades to
    exactly its first-run behavior rather than inventing a location.
    """

    def record_success(self, durations: Mapping[str, float]) -> None:
        del durations

    def pinned_weights(self, epoch: str) -> Mapping[str, float]:
        del epoch
        return {}


def _validated(durations: Mapping[str, float]) -> dict[str, float]:
    """Reject garbage at the boundary, before it is persisted forever."""
    observations: dict[str, float] = {}
    for path, seconds in durations.items():
        if type(path) is not str or not path:
            raise ValueError(f"file duration key must be a non-empty str: {path!r}")
        if type(seconds) is not float or not math.isfinite(seconds) or seconds < 0:
            raise ValueError(
                f"file duration for {path!r} must be finite and non-negative: "
                f"{seconds!r}"
            )
        observations[path] = round(seconds, 3)
    return observations
