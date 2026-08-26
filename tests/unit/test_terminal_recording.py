from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import TextIO, cast

import pytest

from issue_orchestrator.infra.terminal_recording import (
    MirroredTerminalRecordingWriter,
    StructuredTerminalRecordingWriterFactory,
    TerminalRecordingWriter,
    TerminalRecordingWriterFactory,
    append_output_event,
    first_terminal_geometry,
    iter_terminal_recording,
)


class _TrackingTextFile:
    def __init__(self, delegate: TextIO) -> None:
        self._delegate = delegate
        self.close_calls = 0

    @property
    def closed(self) -> bool:
        return self._delegate.closed

    def write(self, value: str) -> int:
        return self._delegate.write(value)

    def flush(self) -> None:
        self._delegate.flush()

    def close(self) -> None:
        self.close_calls += 1
        self._delegate.close()


class _TrackingTextFileFactory:
    def __init__(self) -> None:
        self.opened: list[_TrackingTextFile] = []

    def open_append(self, path: Path) -> TextIO:
        tracked = _TrackingTextFile(path.open("a", encoding="utf-8"))
        self.opened.append(tracked)
        return cast(TextIO, tracked)


class _FlushFailureTextFile(_TrackingTextFile):
    def __init__(self, delegate: TextIO, flush_error: BaseException) -> None:
        super().__init__(delegate)
        self._flush_error = flush_error

    def flush(self) -> None:
        raise self._flush_error


class _FlushFailureTextFileFactory:
    def __init__(self, flush_error: BaseException) -> None:
        self._flush_error = flush_error
        self.opened: list[_FlushFailureTextFile] = []

    def open_append(self, path: Path) -> TextIO:
        tracked = _FlushFailureTextFile(
            path.open("a", encoding="utf-8"),
            self._flush_error,
        )
        self.opened.append(tracked)
        return cast(TextIO, tracked)


class _FailSecondRecordingWriterFactory:
    def __init__(
        self,
        delegate: TerminalRecordingWriterFactory,
        second_error: BaseException,
    ) -> None:
        self._delegate = delegate
        self._second_error = second_error
        self._calls = 0

    def create(
        self,
        path: Path,
        *,
        started_at: float,
        clock: Callable[[], float],
    ) -> TerminalRecordingWriter:
        self._calls += 1
        if self._calls == 2:
            raise self._second_error
        return self._delegate.create(
            path,
            started_at=started_at,
            clock=clock,
        )


class _MirrorOpenFailureFactory:
    def __init__(self, open_error: BaseException) -> None:
        self._open_error = open_error

    def open_append(self, path: Path) -> TextIO:
        del path
        raise self._open_error


def test_terminal_recording_writer_flushes_events_immediately(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"

    writer = TerminalRecordingWriter(recording_path)
    writer.write_output(b"live output\n")

    raw = recording_path.read_text(encoding="utf-8")
    writer.close()

    event = json.loads(raw.strip())
    payload = base64.b64decode(event["data_b64"]).decode("utf-8", errors="replace")
    assert payload == "live output\n"


def test_terminal_recording_writer_records_initial_geometry_first(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    clock = IncrementingClock(now=100.0, step_seconds=0.002)

    writer = TerminalRecordingWriter(recording_path, initial_rows=40, initial_cols=120, clock=clock)
    writer.write_output(b"hello\n")
    writer.close()

    events = list(iter_terminal_recording(recording_path))
    assert events[0]["event_type"] == "resize"
    assert events[0]["rows"] == 40
    assert events[0]["cols"] == 120
    assert events[0]["offset_ms"] == 0
    assert first_terminal_geometry(recording_path) == (40, 120)
    assert events[1]["event_type"] == "output"


def test_append_output_event_uses_next_offset_after_existing_events(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    recording_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_type": "output",
                "offset_ms": 41,
                "data_b64": base64.b64encode(b"first").decode("ascii"),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    append_output_event(recording_path, "second")

    events = list(iter_terminal_recording(recording_path))
    assert [event["offset_ms"] for event in events] == [41, 42]


def test_append_output_event_ignores_trailing_invalid_lines_when_finding_offset(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    recording_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_type": "output",
                        "offset_ms": 3,
                        "data_b64": base64.b64encode(b"ok").decode("ascii"),
                    }
                ),
                "{not-json",
                "",
            ]
        ),
        encoding="utf-8",
    )

    append_output_event(recording_path, "next")

    raw_lines = [line for line in recording_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert json.loads(raw_lines[0])["offset_ms"] == 3
    assert json.loads(raw_lines[-1])["offset_ms"] == 4


def test_first_terminal_geometry_returns_none_without_resize_events(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    recording_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_type": "output",
                "offset_ms": 0,
                "data_b64": base64.b64encode(b"plain").decode("ascii"),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert first_terminal_geometry(recording_path) is None


def test_terminal_recording_writer_reopens_with_monotonic_offsets(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"

    first = TerminalRecordingWriter(recording_path, initial_rows=24, initial_cols=80)
    first.write_output(b"first\n")
    first.close()

    second = TerminalRecordingWriter(recording_path, initial_rows=24, initial_cols=80)
    second.write_output(b"second\n")
    second.close()

    offsets = [event["offset_ms"] for event in iter_terminal_recording(recording_path)]
    assert offsets == sorted(offsets)
    assert max(offsets[:2]) < min(offsets[2:])


def test_mirrored_terminal_recording_writer_keeps_plain_text_mirror(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    mirror_path = tmp_path / "agent-output.log"

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        mirror_path=mirror_path,
        initial_rows=30,
        initial_cols=100,
    )
    writer.write("hello\n")
    writer.write(b"world\n")
    writer.close()

    events = list(iter_terminal_recording(recording_path))
    payload = "".join(
        base64.b64decode(event["data_b64"]).decode("utf-8", errors="replace")
        for event in events
        if event.get("event_type") == "output"
    )
    assert payload == "hello\nworld\n"
    assert mirror_path.read_text(encoding="utf-8") == "hello\nworld\n"


def test_mirrored_terminal_recording_writer_can_mirror_to_additional_recordings(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    secondary_path = tmp_path / "secondary-terminal-recording.jsonl"
    clock = IncrementingClock(now=100.0, step_seconds=0.001)

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        additional_recording_paths=[secondary_path],
        initial_rows=24,
        initial_cols=80,
        clock=clock,
    )
    writer.write("hello\n")
    writer.close()

    primary_events = list(iter_terminal_recording(recording_path))
    secondary_events = list(iter_terminal_recording(secondary_path))
    assert primary_events == secondary_events


def test_mirrored_terminal_recording_writer_invokes_output_callback(tmp_path) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    seen: list[bytes] = []

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        on_output=seen.append,
        initial_rows=24,
        initial_cols=80,
    )
    writer.write("hello\n")
    writer.close()

    assert seen == [b"hello\n"]


class ManualClock:
    def __init__(self, now: float) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class IncrementingClock:
    def __init__(self, *, now: float, step_seconds: float) -> None:
        self._now = now
        self._step_seconds = step_seconds
        self._first = True

    def __call__(self) -> float:
        if self._first:
            self._first = False
            return self._now
        self._now += self._step_seconds
        return self._now


def test_recording_clock_failure_closes_the_open_file(tmp_path: Path) -> None:
    files = _TrackingTextFileFactory()
    primary_error = RuntimeError("injected recording clock failure")

    def failing_clock() -> float:
        raise primary_error

    with pytest.raises(RuntimeError) as raised:
        TerminalRecordingWriter(
            tmp_path / "clock-failure.jsonl",
            clock=failing_clock,
            text_files=files,
        )

    assert raised.value is primary_error
    assert len(files.opened) == 1
    assert files.opened[0].closed
    assert files.opened[0].close_calls == 1


def test_initial_resize_failure_closes_the_open_file(tmp_path: Path) -> None:
    primary_error = RuntimeError("injected initial resize failure")
    files = _FlushFailureTextFileFactory(primary_error)

    with pytest.raises(RuntimeError) as raised:
        TerminalRecordingWriter(
            tmp_path / "resize-failure.jsonl",
            initial_rows=40,
            initial_cols=120,
            text_files=files,
        )

    assert raised.value is primary_error
    assert len(files.opened) == 1
    assert files.opened[0].closed
    assert files.opened[0].close_calls == 1


def test_later_recording_failure_closes_every_earlier_writer(
    tmp_path: Path,
) -> None:
    files = _TrackingTextFileFactory()
    primary_error = RuntimeError("injected second recording failure")
    writers = _FailSecondRecordingWriterFactory(
        StructuredTerminalRecordingWriterFactory(files),
        primary_error,
    )

    with pytest.raises(RuntimeError) as raised:
        MirroredTerminalRecordingWriter(
            tmp_path / "primary.jsonl",
            additional_recording_paths=(tmp_path / "secondary.jsonl",),
            recording_writers=writers,
        )

    assert raised.value is primary_error
    assert len(files.opened) == 1
    assert files.opened[0].closed
    assert files.opened[0].close_calls == 1


def test_mirrored_initial_resize_failure_closes_every_writer(
    tmp_path: Path,
) -> None:
    primary_error = RuntimeError("injected mirrored resize failure")
    files = _FlushFailureTextFileFactory(primary_error)

    with pytest.raises(RuntimeError) as raised:
        MirroredTerminalRecordingWriter(
            tmp_path / "primary.jsonl",
            additional_recording_paths=(tmp_path / "secondary.jsonl",),
            initial_rows=40,
            initial_cols=120,
            recording_writers=StructuredTerminalRecordingWriterFactory(files),
        )

    assert raised.value is primary_error
    assert len(files.opened) == 2
    assert all(file.closed for file in files.opened)
    assert [file.close_calls for file in files.opened] == [1, 1]


def test_plain_text_mirror_open_failure_closes_every_structured_writer(
    tmp_path: Path,
) -> None:
    files = _TrackingTextFileFactory()
    primary_error = RuntimeError("injected plain mirror open failure")

    with pytest.raises(RuntimeError) as raised:
        MirroredTerminalRecordingWriter(
            tmp_path / "primary.jsonl",
            additional_recording_paths=(tmp_path / "secondary.jsonl",),
            mirror_path=tmp_path / "mirror.log",
            recording_writers=StructuredTerminalRecordingWriterFactory(files),
            mirror_files=_MirrorOpenFailureFactory(primary_error),
        )

    assert raised.value is primary_error
    assert len(files.opened) == 2
    assert all(file.closed for file in files.opened)
    assert [file.close_calls for file in files.opened] == [1, 1]


def test_mirrored_terminal_recording_writer_preserves_per_path_base_offsets(
    tmp_path,
) -> None:
    recording_path = tmp_path / "terminal-recording.jsonl"
    aggregate_path = tmp_path / "aggregate-terminal-recording.jsonl"
    append_output_event(aggregate_path, "existing")
    clock = ManualClock(100.0)

    writer = MirroredTerminalRecordingWriter(
        recording_path,
        additional_recording_paths=[aggregate_path],
        initial_rows=24,
        initial_cols=80,
        clock=clock,
    )
    clock.advance(0.001)
    writer.write("hello\n")
    writer.close()

    primary_events = list(iter_terminal_recording(recording_path))
    aggregate_events = list(iter_terminal_recording(aggregate_path))

    primary_offsets = [event["offset_ms"] for event in primary_events]
    aggregate_offsets = [event["offset_ms"] for event in aggregate_events[-2:]]
    assert primary_offsets == [0, 1]
    assert aggregate_offsets == [1, 2]


def test_add_mirror_recording_fans_subsequent_writes_only(tmp_path) -> None:
    """``add_mirror_recording`` attached mid-stream must NOT backfill
    earlier events into the new mirror — it sees only writes that
    happen after registration. Without this guarantee a slice attached
    to a cached pair would inherit the previous exchange's content."""
    recording_path = tmp_path / "pair.jsonl"
    slice_path = tmp_path / "slice.jsonl"
    clock = ManualClock(100.0)
    writer = MirroredTerminalRecordingWriter(
        recording_path,
        initial_rows=24,
        initial_cols=80,
        clock=clock,
    )
    try:
        clock.advance(0.001)
        writer.write("BEFORE\n")
        # Attach the slice mid-stream.
        registered = writer.add_mirror_recording(slice_path, seed_resize=False)
        assert registered is True
        # Re-registering the same path is a no-op (returns False).
        assert writer.add_mirror_recording(slice_path, seed_resize=False) is False
        clock.advance(0.001)
        writer.write("AFTER-1\n")
        clock.advance(0.001)
        writer.write("AFTER-2\n")
    finally:
        writer.close()

    pair_events = list(iter_terminal_recording(recording_path))
    slice_events = list(iter_terminal_recording(slice_path))

    # Pair has resize + BEFORE + AFTER-1 + AFTER-2 = 4 events.
    assert len(pair_events) == 4
    # Slice has only AFTER-1 + AFTER-2 = 2 events (no resize because
    # seed_resize=False; no BEFORE because it was written pre-attach).
    assert len(slice_events) == 2
    # Decode payloads to confirm content alignment.
    import base64
    payloads = [
        base64.b64decode(e["data_b64"]).decode("utf-8")
        for e in slice_events
    ]
    assert payloads == ["AFTER-1\n", "AFTER-2\n"]


def test_add_mirror_recording_seed_resize_emits_initial_geometry(tmp_path) -> None:
    """When ``seed_resize=True``, the new mirror gets a synthetic resize
    event so a viewer attaching to the slice has initial PTY geometry.
    The geometry matches the writer's most recent shape."""
    recording_path = tmp_path / "pair.jsonl"
    slice_path = tmp_path / "slice.jsonl"
    writer = MirroredTerminalRecordingWriter(
        recording_path,
        initial_rows=40,
        initial_cols=120,
    )
    try:
        writer.write("first\n")
        registered = writer.add_mirror_recording(slice_path, seed_resize=True)
        assert registered is True
    finally:
        writer.close()

    slice_events = list(iter_terminal_recording(slice_path))
    # First event in the slice is the synthetic resize matching the
    # writer's current geometry. Subsequent agent writes (none here)
    # would follow.
    assert len(slice_events) == 1
    assert slice_events[0]["event_type"] == "resize"
    assert slice_events[0]["rows"] == 40
    assert slice_events[0]["cols"] == 120


def test_remove_mirror_recording_stops_fan_out(tmp_path) -> None:
    """``remove_mirror_recording`` detaches a mirror so subsequent
    writes no longer touch it. Idempotent — calling twice returns
    False the second time. Removing the canonical recording_path
    raises ``ValueError``."""
    recording_path = tmp_path / "pair.jsonl"
    slice_path = tmp_path / "slice.jsonl"
    writer = MirroredTerminalRecordingWriter(
        recording_path,
        initial_rows=24,
        initial_cols=80,
    )
    try:
        writer.add_mirror_recording(slice_path, seed_resize=False)
        writer.write("attached\n")
        slice_events_before_detach = list(iter_terminal_recording(slice_path))

        removed = writer.remove_mirror_recording(slice_path)
        assert removed is True
        # Idempotent: second remove returns False.
        assert writer.remove_mirror_recording(slice_path) is False

        writer.write("after-detach\n")
        slice_events_after_detach = list(iter_terminal_recording(slice_path))
        assert slice_events_after_detach == slice_events_before_detach, (
            "writer continued to mirror after remove_mirror_recording"
        )

        # Cannot remove the canonical recording.
        import pytest as _pt
        with _pt.raises(ValueError, match="cannot remove the canonical"):
            writer.remove_mirror_recording(recording_path)
    finally:
        writer.close()
