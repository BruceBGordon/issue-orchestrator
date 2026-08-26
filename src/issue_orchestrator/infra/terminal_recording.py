"""Raw terminal recording utilities for PTY session replay.

The canonical session artifact is a newline-delimited JSON stream of terminal
events. Each event preserves the original PTY bytes losslessly via base64 so
replay tooling can feed them into a real terminal emulator later.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol, Sequence, TextIO, cast, runtime_checkable

from ..domain.independent_cleanup import (
    CleanupAction,
    IndependentCleanupPlan,
    raise_cleanup_failures,
    raise_primary_with_cleanup,
)


TERMINAL_RECORDING_FILENAME = "terminal-recording.jsonl"
TERMINAL_RECORDING_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


@runtime_checkable
class TerminalRecordingTextFileFactory(Protocol):
    """Open append-only text handles owned by terminal recordings."""

    def open_append(self, path: Path) -> TextIO: ...


class OsTerminalRecordingTextFileFactory:
    """Production append-only text-file acquisition."""

    def open_append(self, path: Path) -> TextIO:
        return path.open("a", encoding="utf-8")


_OS_TERMINAL_RECORDING_TEXT_FILES = OsTerminalRecordingTextFileFactory()


@dataclass(frozen=True)
class TerminalRecordingEvent:
    """One replayable terminal event."""

    event_type: str
    offset_ms: int
    data_b64: str | None = None
    rows: int | None = None
    cols: int | None = None
    schema_version: int = TERMINAL_RECORDING_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "offset_ms": self.offset_ms,
        }
        if self.data_b64 is not None:
            payload["data_b64"] = self.data_b64
        if self.rows is not None:
            payload["rows"] = self.rows
        if self.cols is not None:
            payload["cols"] = self.cols
        return payload


class TerminalRecordingWriter:
    """Append-only NDJSON writer for raw terminal events."""

    def __init__(
        self,
        path: Path,
        *,
        initial_rows: int | None = None,
        initial_cols: int | None = None,
        started_at: float | None = None,
        clock: Callable[[], float] | None = None,
        text_files: TerminalRecordingTextFileFactory = (
            _OS_TERMINAL_RECORDING_TEXT_FILES
        ),
    ) -> None:
        if not isinstance(
            cast(object, text_files),
            TerminalRecordingTextFileFactory,
        ):
            raise ValueError(
                "TerminalRecordingWriter.text_files must implement "
                "TerminalRecordingTextFileFactory"
            )
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._base_offset_ms = _next_recording_offset(path)
        self._file = text_files.open_append(path)
        try:
            self._clock = time.monotonic if clock is None else clock
            self._started = self._clock() if started_at is None else started_at
            if initial_rows is not None and initial_cols is not None:
                self.write_resize(
                    rows=initial_rows,
                    cols=initial_cols,
                    elapsed_ms=0,
                )
        except BaseException as primary_error:
            raise_primary_with_cleanup(
                "terminal recording setup and file cleanup failed",
                primary_error,
                IndependentCleanupPlan(
                    (
                        CleanupAction(
                            "terminal-recording-file-close",
                            self._file.close,
                        ),
                    )
                ).run(),
            )

    @property
    def name(self) -> str:
        return str(self._path)

    def write(self, data: bytes | str) -> int:
        """pexpect-compatible logfile write interface."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.write_output(data)
        return len(data)

    def write_output(self, data: bytes, *, elapsed_ms: int | None = None) -> None:
        if not data:
            return
        event = TerminalRecordingEvent(
            event_type="output",
            offset_ms=self._offset_ms(elapsed_ms=elapsed_ms),
            data_b64=base64.b64encode(data).decode("ascii"),
        )
        self._write_event(event)

    def write_resize(
        self,
        *,
        rows: int,
        cols: int,
        elapsed_ms: int | None = None,
    ) -> None:
        event = TerminalRecordingEvent(
            event_type="resize",
            offset_ms=self._offset_ms(elapsed_ms=elapsed_ms),
            rows=rows,
            cols=cols,
        )
        self._write_event(event)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def _offset_ms(self, *, elapsed_ms: int | None = None) -> int:
        elapsed = self._elapsed_ms() if elapsed_ms is None else elapsed_ms
        return self._base_offset_ms + elapsed

    def _elapsed_ms(self) -> int:
        return int((self._clock() - self._started) * 1000)

    def _write_event(self, event: TerminalRecordingEvent) -> None:
        self._file.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
        self._file.flush()


@runtime_checkable
class TerminalRecordingWriterFactory(Protocol):
    """Acquire one fully initialized structured terminal recording."""

    def create(
        self,
        path: Path,
        *,
        started_at: float,
        clock: Callable[[], float],
    ) -> TerminalRecordingWriter: ...


class StructuredTerminalRecordingWriterFactory:
    """Production factory for structured terminal recording writers."""

    def __init__(self, text_files: TerminalRecordingTextFileFactory) -> None:
        if not isinstance(
            cast(object, text_files),
            TerminalRecordingTextFileFactory,
        ):
            raise ValueError(
                "StructuredTerminalRecordingWriterFactory.text_files must "
                "implement TerminalRecordingTextFileFactory"
            )
        self._text_files = text_files

    def create(
        self,
        path: Path,
        *,
        started_at: float,
        clock: Callable[[], float],
    ) -> TerminalRecordingWriter:
        return TerminalRecordingWriter(
            path,
            started_at=started_at,
            clock=clock,
            text_files=self._text_files,
        )


_STRUCTURED_TERMINAL_RECORDINGS = StructuredTerminalRecordingWriterFactory(
    _OS_TERMINAL_RECORDING_TEXT_FILES
)


class MirroredTerminalRecordingWriter:
    """Write canonical terminal replay events and optionally mirror plain text."""

    def __init__(
        self,
        recording_path: Path,
        *,
        additional_recording_paths: Sequence[Path] | None = None,
        mirror_path: Path | None = None,
        on_output: Callable[[bytes], None] | None = None,
        initial_rows: int | None = None,
        initial_cols: int | None = None,
        clock: Callable[[], float] | None = None,
        recording_writers: TerminalRecordingWriterFactory = (
            _STRUCTURED_TERMINAL_RECORDINGS
        ),
        mirror_files: TerminalRecordingTextFileFactory = (
            _OS_TERMINAL_RECORDING_TEXT_FILES
        ),
    ) -> None:
        if not isinstance(
            cast(object, recording_writers),
            TerminalRecordingWriterFactory,
        ):
            raise ValueError(
                "MirroredTerminalRecordingWriter.recording_writers must "
                "implement TerminalRecordingWriterFactory"
            )
        if not isinstance(
            cast(object, mirror_files),
            TerminalRecordingTextFileFactory,
        ):
            raise ValueError(
                "MirroredTerminalRecordingWriter.mirror_files must implement "
                "TerminalRecordingTextFileFactory"
            )
        effective_clock = time.monotonic if clock is None else clock
        self._clock = effective_clock
        self._started = effective_clock()
        self._on_output = on_output
        self._recording_writers = recording_writers
        # Public so heartbeat/diagnostic logging in
        # ``persistent_round_runner.send_round`` can sample the
        # recording's growth without parsing the writer's name.
        self.recording_path = recording_path
        # Track the latest geometry so dynamically-added recordings can
        # seed themselves with a resize event matching the agent's
        # current PTY shape. Without this, a slice attached mid-stream
        # would have no initial geometry and the player would render
        # with a default that doesn't match the agent's actual cols/rows.
        self._latest_rows = initial_rows
        self._latest_cols = initial_cols
        recording_paths = [recording_path]
        for extra_path in additional_recording_paths or ():
            if extra_path not in recording_paths:
                recording_paths.append(extra_path)
        self._recordings: list[TerminalRecordingWriter] = []
        self._recording_by_path: dict[Path, TerminalRecordingWriter] = {}
        self._mirror: TextIO | None = None
        try:
            for path in recording_paths:
                writer = recording_writers.create(
                    path,
                    started_at=self._started,
                    clock=effective_clock,
                )
                self._recordings.append(writer)
                self._recording_by_path[path] = writer
            if initial_rows is not None and initial_cols is not None:
                self._write_resize(
                    rows=initial_rows,
                    cols=initial_cols,
                    elapsed_ms=0,
                )
            if mirror_path is not None:
                mirror_path.parent.mkdir(parents=True, exist_ok=True)
                self._mirror = mirror_files.open_append(mirror_path)
        except BaseException as primary_error:
            cleanup_actions = [
                CleanupAction(
                    f"structured-recording-{index}-close",
                    recording.close,
                )
                for index, recording in enumerate(self._recordings)
            ]
            if self._mirror is not None:
                cleanup_actions.append(
                    CleanupAction("plain-text-mirror-close", self._mirror.close)
                )
            raise_primary_with_cleanup(
                "mirrored terminal recording setup and cleanup failed",
                primary_error,
                IndependentCleanupPlan(tuple(cleanup_actions)).run(),
            )

    def add_mirror_recording(
        self,
        path: Path,
        *,
        seed_resize: bool = True,
    ) -> bool:
        """Attach an additional recording target mid-stream.

        Subsequent ``write`` calls fan out to this path alongside the
        canonical recording. The new writer shares the canonical
        writer's ``_started`` clock so its ``offset_ms`` values stay
        on the same timeline as the canonical recording — important
        for chapter-driven scrubbing where a sidecar offset must
        resolve to a coherent event sequence.

        ``seed_resize=True`` writes the latest known geometry as a
        first resize event so a viewer attaching to this slice has
        initial PTY shape (otherwise the player falls back to a
        default that doesn't match the agent's actual cols/rows).
        Skipped silently if no geometry is known.

        Returns True when a new recording was registered, False when
        ``path`` is already registered (idempotent — re-registering
        the same path during a retry must not duplicate writes).
        """
        if path in self._recording_by_path:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = self._recording_writers.create(
            path,
            started_at=self._started,
            clock=self._clock,
        )
        self._recordings.append(writer)
        self._recording_by_path[path] = writer
        if seed_resize and self._latest_rows is not None and self._latest_cols is not None:
            writer.write_resize(
                rows=self._latest_rows,
                cols=self._latest_cols,
                elapsed_ms=self._elapsed_ms(),
            )
        return True

    def remove_mirror_recording(self, path: Path) -> bool:
        """Detach a previously-added recording target.

        Closes the writer for ``path`` (flushing any pending bytes)
        and removes it from the fan-out list. Subsequent ``write``
        calls no longer touch this file. Returns True when a writer
        was removed, False when ``path`` was not registered (lets
        callers idempotently tear down without exception).

        The canonical recording (the one passed to ``__init__``)
        cannot be removed — removing it would break the
        ``recording_path`` invariant the heartbeat / diagnostics
        layers depend on. Attempting to remove it raises
        ``ValueError``.
        """
        if path == self.recording_path:
            raise ValueError(
                "cannot remove the canonical recording_path "
                f"{path}; it is the writer's primary target",
            )
        writer = self._recording_by_path.pop(path, None)
        if writer is None:
            return False
        try:
            self._recordings.remove(writer)
        except ValueError:
            # Should be impossible (map and list maintained in
            # lockstep), but tolerate to keep teardown idempotent.
            pass
        try:
            writer.close()
        except OSError:
            logger.exception(
                "Failed to close removed mirror recording at %s; "
                "subsequent writes will not target it but the file "
                "may have a half-written final event",
                path,
            )
        return True

    @property
    def name(self) -> str:
        return self._recordings[0].name

    def write(self, data: bytes | str) -> int:
        if isinstance(data, str):
            text = data
            raw = data.encode("utf-8")
        else:
            raw = data
            text = data.decode("utf-8", errors="ignore")
        elapsed_ms = self._elapsed_ms()
        written = len(raw)
        for recording in self._recordings:
            recording.write_output(raw, elapsed_ms=elapsed_ms)
        if self._mirror is not None and text:
            self._mirror.write(text)
            self._mirror.flush()
        if self._on_output is not None:
            try:
                self._on_output(raw)
            except Exception:  # noqa: BLE001
                logger.exception("Terminal output callback failed")
        return written

    def flush(self) -> None:
        for recording in self._recordings:
            recording.flush()
        if self._mirror is not None:
            self._mirror.flush()

    def close(self) -> None:
        mirror_actions = (
            ()
            if self._mirror is None
            else (CleanupAction("plain-text-mirror-close", self._mirror.close),)
        )
        raise_cleanup_failures(
            "terminal recording resource cleanup failed",
            IndependentCleanupPlan(
                (
                    *(
                        CleanupAction(
                            f"structured-recording-{index}-close",
                            recording.close,
                        )
                        for index, recording in enumerate(self._recordings)
                    ),
                    *mirror_actions,
                )
            ).run(),
        )

    def _elapsed_ms(self) -> int:
        return int((self._clock() - self._started) * 1000)

    def _write_resize(self, *, rows: int, cols: int, elapsed_ms: int) -> None:
        # Cache the geometry so dynamically-added mirror recordings
        # can seed themselves with the agent's current PTY shape.
        self._latest_rows = rows
        self._latest_cols = cols
        for recording in self._recordings:
            recording.write_resize(rows=rows, cols=cols, elapsed_ms=elapsed_ms)

def iter_terminal_recording(path: Path) -> Iterator[dict[str, Any]]:
    """Iterate over a terminal recording NDJSON file for replay or inspection."""
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            yield json.loads(raw_line)


def first_terminal_geometry(path: Path) -> tuple[int, int] | None:
    """Return the first recorded terminal geometry as (rows, cols)."""
    for event in iter_terminal_recording(path):
        if event.get("event_type") != "resize":
            continue
        rows = event.get("rows")
        cols = event.get("cols")
        if isinstance(rows, int) and isinstance(cols, int):
            return rows, cols
    return None


def append_output_event(path: Path, text: str) -> None:
    """Append a plain-text transcript snippet as one terminal output event."""
    if not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    next_offset = _next_recording_offset(path)
    payload = TerminalRecordingEvent(
        event_type="output",
        offset_ms=next_offset,
        data_b64=base64.b64encode(text.encode("utf-8")).decode("ascii"),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload.to_dict(), sort_keys=True) + "\n")


def _next_recording_offset(path: Path) -> int:
    if not path.exists():
        return 0
    last_offset = _read_last_recording_offset(path)
    if last_offset is None:
        return 0
    return last_offset + 1


def _read_last_recording_offset(path: Path) -> int | None:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        file_size = handle.tell()
        if file_size <= 0:
            return None

        buffer = b""
        position = file_size
        while position > 0:
            read_size = min(4096, position)
            position -= read_size
            handle.seek(position)
            buffer = handle.read(read_size) + buffer
            lines = buffer.splitlines()
            if position > 0 and len(lines) <= 1:
                continue
            for raw_line in reversed(lines):
                if not raw_line.strip():
                    continue
                try:
                    payload = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                offset_ms = payload.get("offset_ms")
                if isinstance(offset_ms, int):
                    return offset_ms
                return None
        return None
