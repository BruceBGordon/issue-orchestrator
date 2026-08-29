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
    every later ask, from any process, is answered from it. Which pins
    may be deleted is :class:`_PinRetention`'s decision alone, taken
    over the typed :data:`PinDate` states — never a guess about which
    pins are still being read.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import re
import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, TextIO, cast

_logger = logging.getLogger(__name__)

_ROLLING_WINDOW = 5
# Eviction needs age AND depth to agree — see _PinRetention. Gates take
# minutes, so a day is three orders of magnitude of headroom...
_PIN_RETENTION_SECONDS = 24 * 60 * 60
# ...and the newest pins are protected outright however old the clock
# says they are, so a wall-clock correction cannot age a live pin out.
# Fifty is far more than the handful of gates that can overlap.
_PIN_RETENTION_DEPTH = 50
# Pins written before the payload carried its own timestamp fall back
# to mtime, which is not the file's own account of itself, so they are
# held far longer before anything is assumed about their age.
_UNSTAMPED_PIN_RETENTION_SECONDS = 7 * _PIN_RETENTION_SECONDS
# A recorded stamp outside these bounds is not a date, it is damage.
# 2020-01-01T00:00:00Z: no pin predates the feature by years.
_EARLIEST_SANE_PUBLISH_TIME = 1577836800.0
_FUTURE_TOLERANCE_SECONDS = 48 * 60 * 60
_HISTORY_FILENAME = "history.json"
_LOCK_FILENAME = "history.lock"
_PINNED_PREFIX = "pinned-"
_PINNED_SUFFIX = ".json"
_DURATIONS_FIELD = "durations"
_WEIGHTS_FIELD = "weights"
_PUBLISHED_AT_FIELD = "published_at"
_SAFE_EPOCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class StampedPin:
    """The pin recorded its own publication time, and it makes sense."""

    published_at: float


@dataclass(frozen=True)
class LegacyUnstampedPin:
    """A well-formed pin from before pins dated themselves.

    All that is known about it is mtime — metadata *about* the file
    rather than the file's own account of itself.
    """

    modified_at: float


@dataclass(frozen=True)
class UndatablePin:
    """Nothing trustworthy could be learned about this pin's age.

    Unreadable, not JSON, not a pin document, or carrying a stamp that
    is not a date (non-numeric, NaN, infinite, absurd magnitude). This
    state exists because collapsing it into "no timestamp" was the
    round-2 defect: a garbled pin then took the legacy path, and the
    legacy path deletes.
    """

    reason: str


# Closed union: every pin is in exactly one of these states, and each
# state has its own retention policy. Keeping them distinct is the
# whole point — a `float | None` cannot tell the owner whether absence
# means "old format" (evictable when very old) or "unreadable" (never
# evictable).
PinDate = StampedPin | LegacyUnstampedPin | UndatablePin


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
            self._write_json(
                pinned_path,
                {
                    _WEIGHTS_FIELD: weights,
                    # The pin's own account of its age, so retention
                    # never has to trust a filesystem timestamp.
                    _PUBLISHED_AT_FIELD: round(time.time(), 3),
                },
            )
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
        """Drop only pins no live reader can possibly still want.

        Runs under the lock on the publish path only — a reader never
        deletes — and never raises: an unreadable neighbour must not
        fail a publish that is otherwise fine. What may be dropped is
        decided by :class:`_PinRetention`; this method only gathers the
        neighbourhood and carries out the verdict.
        """
        now = time.time()
        try:
            pins = list(self._directory.glob(f"{_PINNED_PREFIX}*{_PINNED_SUFFIX}"))
        except OSError:
            return
        dated = {pin: self._dated(pin, now) for pin in pins}
        for stale in _PinRetention(now).evictable(dated):
            try:
                stale.unlink()
            except OSError:
                pass

    def _dated(self, pin: Path, now: float) -> PinDate:
        """Classify one neighbour, contained.

        One pathological neighbour must never fail an otherwise-good
        publish — a pin carrying 10**400 raised OverflowError straight
        out of pruning (round 2, finding 3). Containment is honest
        here rather than a swallowed error, because "we could not
        classify it" IS a modelled state, and that state is always
        retained.
        """
        try:
            return classify_pin_date(pin, now)
        except Exception as error:
            return UndatablePin(f"{pin.name} could not be classified: {error!r}")

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


class _PinRetention:
    """Which pins may be deleted — and, far more importantly, which may not.

    Every earlier version of this policy evicted on a single signal,
    and each single signal was wrong on its own:

    - **Count alone** (round 1): a busy day publishes enough newer
      epochs to evict a pin whose gate is still running.
    - **Age alone** (round 2): a forward wall-clock correction past the
      bound ages every live pin out at once.

    Both produce the same silent failure — the delayed slice
    republishes from newer history, its partition disagrees with the
    one its siblings already ran, and the combined gate omits some
    files and runs others twice. So eviction of a dated pin requires
    the two to **agree**: older than the age bound *and* outside the
    newest ``_PIN_RETENTION_DEPTH`` pins by recorded time. A clock jump
    alone cannot evict, because a live gate's pin is among the newest.
    Count pressure alone cannot evict, because recent pins are young.
    The store still stays bounded: a quiet store cannot exceed the
    depth plus one day of gates.

    The undated states keep their own policies, which is why they are
    modelled separately (round 2, finding 2).
    """

    def __init__(self, now: float) -> None:
        self._now = now

    def evictable(self, dated: Mapping[Path, PinDate]) -> list[Path]:
        protected = self._protected(dated)
        evictable: list[Path] = []
        for path, date in sorted(dated.items()):
            if type(date) is UndatablePin:
                # Never delete what could not be read. Said out loud,
                # because a pin nobody can date is also a pin nobody
                # will ever clean up.
                _logger.warning(
                    "[file-durations] retaining undatable pin: %s", date.reason
                )
                continue
            if self._expired(date) and path not in protected:
                evictable.append(path)
        return evictable

    def _protected(self, dated: Mapping[Path, PinDate]) -> frozenset[Path]:
        """The newest dated pins, kept whatever the clock now says."""
        stamped = {
            path: date.published_at
            for path, date in dated.items()
            if type(date) is StampedPin
        }
        ranked = sorted(stamped, key=lambda path: (-stamped[path], path.name))
        return frozenset(ranked[:_PIN_RETENTION_DEPTH])

    def _expired(self, date: PinDate) -> bool:
        if type(date) is StampedPin:
            return self._now - date.published_at > _PIN_RETENTION_SECONDS
        if type(date) is LegacyUnstampedPin:
            return self._now - date.modified_at > _UNSTAMPED_PIN_RETENTION_SECONDS
        return False


def classify_pin_date(path: Path, now: float) -> PinDate:
    """Date one pin file, keeping the three outcomes distinct.

    Collapsing them into ``float | None`` was the round-2 defect: a
    garbled pin and a legacy pin then took the same path, and the
    legacy path deletes.
    """
    payload = _load_pin_document(path)
    if payload is None:
        return UndatablePin(f"{path.name} is not a readable pin document")
    if _PUBLISHED_AT_FIELD not in payload:
        return _legacy_date(path, payload)
    return _recorded_date(path, payload[_PUBLISHED_AT_FIELD], now)


def _load_pin_document(path: Path) -> dict[str, object] | None:
    try:
        payload = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        # OSError covers a pin this process may not read; ValueError
        # covers both malformed JSON and undecodable bytes.
        return None
    if not isinstance(payload, dict):
        return None
    return cast(dict[str, object], payload)


def _legacy_date(path: Path, payload: Mapping[str, object]) -> PinDate:
    """A pin with no recorded date — but only if it is really a pin.

    A document carrying no weights is not "a pin missing its date", it
    is a file nobody can interpret, and an absent date there is
    indistinguishable from corruption.
    """
    if not isinstance(payload.get(_WEIGHTS_FIELD), dict):
        return UndatablePin(f"{path.name} has no date and no weights")
    try:
        return LegacyUnstampedPin(path.stat().st_mtime)
    except OSError:
        return UndatablePin(f"{path.name} has no recorded date and no readable mtime")


def _recorded_date(path: Path, stamp: object, now: float) -> PinDate:
    """A recorded stamp, accepted only if it is actually a date."""
    # A bool is an int to Python but never a timestamp to anyone else.
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return UndatablePin(f"{path.name} has a non-numeric date {stamp!r}")
    if isinstance(stamp, float) and not math.isfinite(stamp):
        return UndatablePin(f"{path.name} has a non-finite date {stamp!r}")
    # Bounds are compared BEFORE any float() conversion: a Python int
    # compares exactly at any magnitude, while float(10**400) raises
    # OverflowError — which escaped pruning and failed an otherwise-good
    # publish (round 2, finding 3).
    if not _EARLIEST_SANE_PUBLISH_TIME <= stamp <= now + _FUTURE_TOLERANCE_SECONDS:
        return UndatablePin(f"{path.name} has an out-of-range date {stamp!r}")
    return StampedPin(float(stamp))


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
