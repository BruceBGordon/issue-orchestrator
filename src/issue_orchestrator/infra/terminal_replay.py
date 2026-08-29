"""Reconstruct a terminal screen from a recorded PTY stream.

Feeds a ``terminal-recording.jsonl`` into the screen model in
``terminal_viewport`` and reports the result together with the flags that say
how far it can be trusted: whether the whole file was replayed, whether the
stream was structurally sound, whether a deadline cut it short, and whether it
used grid-affecting terminal modes the model does not reproduce.

Split from the screen model so that module answers only "what does the screen
look like" and this one answers "can this recording be turned into one".
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .terminal_recording import (
    MAX_TERMINAL_COLS,
    MAX_TERMINAL_ROWS,
    screen_dimension,
)
from .terminal_viewport import RenderedScreen, TerminalViewport


DEFAULT_REPLAY_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_REPLAY_MAX_EVENTS = 200_000


@dataclass(frozen=True)
class RecordingReplay:
    """A viewport reconstructed from a terminal recording, plus its caveats.

    ``replayed_from_start`` and ``structurally_complete`` are the honesty
    flags. A caller that must not guess refuses to draw a conclusion unless the
    stream it replayed was structurally sound, and reads only
    ``screen.written_rows`` so a partial window cannot leak unknown history.
    """

    screen: RenderedScreen
    events_applied: int
    rows_scanned: int
    replayed_from_start: bool
    structurally_complete: bool
    abandoned: bool = False
    #: Grid-affecting state channels the recording used that the viewport does
    #: not model. Non-empty means the reconstruction cannot be trusted.
    unmodelled_state: tuple[str, ...] = ()


def replay_terminal_recording(
    path: Path,
    *,
    max_bytes: int = DEFAULT_REPLAY_MAX_BYTES,
    max_events: int = DEFAULT_REPLAY_MAX_EVENTS,
    abort: Callable[[], bool] | None = None,
) -> RecordingReplay:
    """Reconstruct the final viewport of a ``terminal-recording.jsonl``.

    Replays from the beginning when the file fits in ``max_bytes``; otherwise
    replays a trailing window of that size, which a repainting TUI refreshes
    many times over. Either way the result carries the flags a caller needs to
    decide whether the reconstruction is trustworthy enough to act on.

    ``structurally_complete`` is False when the file ends mid-row (a recording
    still open for append), when any scanned row fails to parse, or when a row
    carries an undecodable payload — all of which mean the replay saw something
    other than the exact byte stream the agent emitted.

    ``abort`` lets a caller working to a deadline stop the replay between
    events. An abandoned replay sets both ``abandoned`` and (because a
    half-applied stream is exactly the kind of hole a screen clear hides)
    ``structurally_complete=False``, so no verdict can rest on it.
    """
    blob, replayed_from_start = _read_window(path, max_bytes=max_bytes)
    complete = blob.endswith(b"\n") or not blob
    rows = blob.split(b"\n")
    if not replayed_from_start and rows:
        # The window almost certainly opens mid-row; that fragment is not a
        # parse failure, it is simply outside the window.
        rows = rows[1:]
    viewport = TerminalViewport()
    applied = 0
    scanned = 0
    abandoned = False
    for raw_row in rows:
        if not raw_row.strip():
            continue
        if abort is not None and abort():
            abandoned = True
            complete = False
            break
        scanned += 1
        if scanned > max_events:
            complete = False
            break
        event = _parse_row(raw_row)
        if event is None:
            complete = False
            continue
        outcome = _apply_event(viewport, event)
        applied += outcome.applied
        if not outcome.sound:
            complete = False
    return RecordingReplay(
        screen=viewport.render(),
        events_applied=applied,
        rows_scanned=scanned,
        replayed_from_start=replayed_from_start,
        structurally_complete=complete,
        abandoned=abandoned,
        unmodelled_state=tuple(dict.fromkeys(viewport.unmodelled_state)),
    )


def _read_window(path: Path, *, max_bytes: int) -> tuple[bytes, bool]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - max_bytes)
        handle.seek(start)
        return handle.read(size - start), start == 0


def _parse_row(raw_row: bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(raw_row)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return event if isinstance(event, dict) else None


@dataclass(frozen=True)
class _EventOutcome:
    """Whether an event reached the screen, and whether it was trustworthy.

    ``sound`` is False when an event that *should* have carried PTY bytes could
    not be decoded. Skipping it quietly would leave a hole in the reconstructed
    stream while the replay still claimed to be complete — and a hole is
    exactly where a screen clear hides (#7141 round 2).
    """

    applied: int
    sound: bool


_SOUND_NO_OP = _EventOutcome(applied=0, sound=True)


def _apply_event(viewport: TerminalViewport, event: dict[str, Any]) -> _EventOutcome:
    kind = event.get("event_type")
    if kind == "resize":
        rows = screen_dimension(event.get("rows"), limit=MAX_TERMINAL_ROWS)
        cols = screen_dimension(event.get("cols"), limit=MAX_TERMINAL_COLS)
        if rows is None or cols is None:
            return _EventOutcome(applied=0, sound=False)
        viewport.resize(rows=rows, cols=cols)
        return _SOUND_NO_OP
    if kind != "output":
        return _SOUND_NO_OP
    encoded = event.get("data_b64")
    if not isinstance(encoded, str):
        return _EventOutcome(applied=0, sound=False)
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return _EventOutcome(applied=0, sound=False)
    viewport.feed(payload)
    return _EventOutcome(applied=1, sound=True)
