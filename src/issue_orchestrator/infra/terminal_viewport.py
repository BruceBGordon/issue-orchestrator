"""Minimal VT screen model: what is actually on the terminal right now.

Every other Python path in this repo that touches PTY output either
concatenates it (``exchange_kill_evidence.read_recording_tail_text``) or
*strips* the escape sequences (``terminal_cleaning.strip_ansi_codes``,
``session_interactions.normalize_terminal_text``). Both answer "what bytes were
ever emitted", which is not the same question as "what does the screen say" —
and for a TUI that repaints in place, they are wildly different answers. The
only real emulation in the repository is browser-side ``xterm.js`` in the
session-replay viewer, which no server-side caller can reach.

That gap produced a false diagnosis (#7141 round 1): a composer footer that had
already been erased was still findable in the concatenated stream, so the
kill-evidence discriminator asserted a stranded prompt from dead history.

This module is the Python-side owner of that question. It is deliberately a
*screen model*, not a terminal: no styling, no colours, no scrollback, no
response generation. It implements exactly the operations a repainting agent
TUI uses, measured against a real 21 MB Claude reviewer recording:

    567,061 x CUP (``ESC[r;cH``)     cursor addressing
    497,583 x EL  (``ESC[K``)        erase in line
        460 x DECSTBM (``ESC[r``)    scroll region
        124 x ED  (``ESC[J``)        erase in display
         74 x SU  (``ESC[S``)        scroll up
  1,229,026 x SGR (``ESC[m``)        styling — ignored on purpose

**The written-row contract.** Reconstructing a screen from a *window* of a
stream cannot know what the untouched rows held before the window opened. So
the viewport tracks which rows this replay actually wrote (printed to, or
erased — an erase is authoritative knowledge that the row is now blank) and
exposes only those through :meth:`written_rows`. Callers that must not guess
read that, never :meth:`rows`. This is what makes a verdict from a tail window
sound: a repainting TUI redraws its footer band every frame, so the rows a
discriminator cares about are always written, while stale scrollback above is
excluded by construction.
"""

from __future__ import annotations

import base64
import binascii
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar

from .terminal_recording import (
    MAX_TERMINAL_COLS,
    MAX_TERMINAL_ROWS,
    screen_dimension,
)

DEFAULT_ROWS = 40
DEFAULT_COLS = 120
_MAX_ROWS = MAX_TERMINAL_ROWS
_MAX_COLS = MAX_TERMINAL_COLS
_BLANK = " "
_REPLACEMENT = "\ufffd"
_ZWJ = "\u200d"
# Marks, format characters and variation selectors never claim a cell.
_ZERO_WIDTH_CATEGORIES = frozenset({"Mn", "Me", "Cf"})
_TAB_WIDTH = 8

_ESC = 0x1B
_BEL = 0x07
_CsiTable = dict[str, "Callable[[TerminalViewport, list[int]], None]"]


@dataclass(frozen=True)
class RenderedScreen:
    """The reconstructed viewport plus the honesty flags that qualify it."""

    rows: tuple[str, ...]
    written_rows: tuple[str, ...]
    cursor_row: int
    cursor_col: int
    fed_bytes: int

    @property
    def written_row_count(self) -> int:
        return len(self.written_rows)


class TerminalViewport:
    """A rows x cols character grid that applies the ops a TUI actually emits.

    Unrecognised escape sequences are consumed and ignored rather than printed:
    a stray ``ESC[?25l`` must not leave ``[?25l`` sitting in the grid where a
    marker search could trip over it.
    """

    def __init__(self, *, rows: int = DEFAULT_ROWS, cols: int = DEFAULT_COLS) -> None:
        if rows < 1 or cols < 1:
            raise ValueError("viewport geometry must be positive")
        self._rows = min(rows, _MAX_ROWS)
        self._cols = min(cols, _MAX_COLS)
        self._grid: list[list[str]] = self._blank_grid()
        self._written: list[bool] = [False] * self._rows
        self._row = 0
        self._col = 0
        self._saved: tuple[int, int] = (0, 0)
        self._join_pending = False
        self._scroll_top = 0
        self._scroll_bottom = self._rows - 1
        self._fed = 0
        self._pending = b""

    # -- public -----------------------------------------------------------

    def resize(self, *, rows: int, cols: int) -> None:
        """Apply a recorded resize event, preserving what is already on screen."""
        if rows < 1 or cols < 1:
            return
        rows = min(rows, _MAX_ROWS)
        cols = min(cols, _MAX_COLS)
        old_grid, old_written = self._grid, self._written
        self._rows, self._cols = rows, cols
        self._grid = self._blank_grid()
        self._written = [False] * rows
        for index in range(min(len(old_grid), rows)):
            row = old_grid[index][:cols]
            self._grid[index][: len(row)] = row
            self._written[index] = old_written[index]
        self._scroll_top = 0
        self._scroll_bottom = rows - 1
        self._row = min(self._row, rows - 1)
        self._col = min(self._col, cols - 1)

    def feed(self, data: bytes) -> None:
        """Apply a chunk of raw PTY bytes.

        Chunk boundaries may split an escape sequence, so an unterminated tail
        is held over to the next call instead of being printed as text.
        """
        self._fed += len(data)
        buffer = self._pending + data
        self._pending = b""
        index = 0
        size = len(buffer)
        while index < size:
            byte = buffer[index]
            if byte == _ESC:
                consumed = self._apply_escape(buffer, index)
                if consumed is None:
                    # Incomplete sequence at the end of the chunk.
                    self._pending = buffer[index:]
                    return
                index += consumed
                continue
            if byte >= 0x80:
                char, consumed = _decode_utf8(buffer, index, size)
                if consumed == 0:
                    # Genuinely truncated at the chunk edge — hold it over.
                    self._pending = buffer[index:]
                    return
                self._print(char)
                index += consumed
                continue
            self._apply_control_byte(byte)
            index += 1

    def render(self) -> RenderedScreen:
        """Freeze the current screen. Non-mutating; safe to call repeatedly."""
        rows = tuple("".join(row).rstrip() for row in self._grid)
        written = tuple(
            text for text, was_written in zip(rows, self._written) if was_written
        )
        return RenderedScreen(
            rows=rows,
            written_rows=written,
            cursor_row=self._row,
            cursor_col=self._col,
            fed_bytes=self._fed,
        )

    # -- character handling -----------------------------------------------

    def _blank_grid(self) -> list[list[str]]:
        return [[_BLANK] * self._cols for _ in range(self._rows)]

    def _apply_control_byte(self, byte: int) -> None:
        """Apply one single-byte ASCII control character or printable."""
        if byte == 0x0D:  # CR
            self._col = 0
            return
        if byte == 0x0A:  # LF
            self._line_feed()
            return
        if byte == 0x08:  # BS
            self._col = max(0, self._col - 1)
            return
        if byte == 0x09:  # HT
            self._col = min(self._cols - 1, (self._col // _TAB_WIDTH + 1) * _TAB_WIDTH)
            return
        if byte < 0x20 or byte == 0x7F:
            return
        self._print(chr(byte))

    def _print(self, char: str) -> None:
        # A ZWJ binds what follows into the grapheme already on screen, so the
        # joined character claims no cells of its own. The bundled xterm the
        # session viewer runs draws 👨‍👩‍👧‍👦 as ONE glyph; counting its parts
        # separately pushed everything after it along the row and wrapped a
        # footer mid-word, costing a real stranded-composer catch
        # (#7141 round 3).
        joined = self._join_pending
        self._join_pending = char == _ZWJ
        # A grid narrower than the glyph cannot represent it; clamping keeps
        # the write in bounds instead of running off the end of the row.
        cells = 0 if joined else min(_char_cells(char), self._cols)
        if cells == 0:
            self._attach_combining(char)
            return
        if self._col + cells > self._cols:
            self._col = 0
            self._line_feed()
        self._grid[self._row][self._col] = char
        # A wide glyph owns its trailing cell too; the empty string keeps the
        # rendered row the right length without printing a second glyph.
        for offset in range(1, cells):
            self._grid[self._row][self._col + offset] = ""
        self._written[self._row] = True
        self._col += cells

    def _attach_combining(self, char: str) -> None:
        """Decorate the last written cell rather than consuming a new one."""
        column = max(0, self._col - 1)
        while column > 0 and self._grid[self._row][column] == "":
            column -= 1
        cell = self._grid[self._row][column]
        # Nothing to decorate yet: replace the blank rather than trailing the
        # mark behind a space that was never printed.
        self._grid[self._row][column] = char if cell == _BLANK else cell + char
        self._written[self._row] = True

    def _line_feed(self) -> None:
        if self._row == self._scroll_bottom:
            self._scroll_up(1)
            return
        self._row = min(self._rows - 1, self._row + 1)

    def _scroll_up(self, count: int) -> None:
        top, bottom = self._scroll_top, self._scroll_bottom
        for _ in range(count):
            del self._grid[top]
            del self._written[top]
            self._grid.insert(bottom, [_BLANK] * self._cols)
            # A row scrolled into view is blank *because this replay scrolled
            # it*, so it is authoritative, not unknown history.
            self._written.insert(bottom, True)

    # -- escape handling ---------------------------------------------------

    def _apply_escape(self, buffer: bytes, start: int) -> int | None:
        """Return bytes consumed from ``start``, or None if incomplete."""
        if start + 1 >= len(buffer):
            return None
        marker = buffer[start + 1]
        if marker == 0x5B:  # '['
            return self._apply_csi(buffer, start)
        if marker in (0x5D, 0x50, 0x5E, 0x5F):  # OSC / DCS / PM / APC
            return _string_sequence_length(buffer, start)
        if marker == 0x37:  # ESC 7 save cursor
            self._saved = (self._row, self._col)
            return 2
        if marker == 0x38:  # ESC 8 restore cursor
            self._row, self._col = self._saved
            return 2
        if marker == 0x63:  # ESC c full reset
            self._reset()
            return 2
        return 2

    def _apply_csi(self, buffer: bytes, start: int) -> int | None:
        index = start + 2
        size = len(buffer)
        while index < size and (0x30 <= buffer[index] <= 0x3F):
            index += 1
        while index < size and (0x20 <= buffer[index] <= 0x2F):
            index += 1
        if index >= size:
            return None
        final = buffer[index]
        params_raw = buffer[start + 2 : index].decode("ascii", errors="replace")
        consumed = index + 1 - start
        self._dispatch_csi(chr(final), params_raw)
        return consumed

    def _dispatch_csi(self, final: str, params_raw: str) -> None:
        if params_raw.startswith("?"):
            # Private modes (cursor visibility, bracketed paste, synchronised
            # output). None of them change the character grid.
            return
        params = _numeric_params(params_raw)
        handler = self._CSI_HANDLERS.get(final)
        if handler is None:
            return
        handler(self, params)

    def _reset(self) -> None:
        self._grid = self._blank_grid()
        self._written = [True] * self._rows
        self._row = self._col = 0
        self._join_pending = False
        self._scroll_top, self._scroll_bottom = 0, self._rows - 1

    # -- CSI operations ----------------------------------------------------

    def _cup(self, params: list[int]) -> None:
        row = (params[0] if params else 1) or 1
        col = (params[1] if len(params) > 1 else 1) or 1
        self._row = max(0, min(self._rows - 1, row - 1))
        self._col = max(0, min(self._cols - 1, col - 1))

    def _cursor_up(self, params: list[int]) -> None:
        self._row = max(0, self._row - _amount(params))

    def _cursor_down(self, params: list[int]) -> None:
        self._row = min(self._rows - 1, self._row + _amount(params))

    def _cursor_forward(self, params: list[int]) -> None:
        self._col = min(self._cols - 1, self._col + _amount(params))

    def _cursor_back(self, params: list[int]) -> None:
        self._col = max(0, self._col - _amount(params))

    def _column_absolute(self, params: list[int]) -> None:
        self._col = max(0, min(self._cols - 1, _amount(params) - 1))

    def _row_absolute(self, params: list[int]) -> None:
        self._row = max(0, min(self._rows - 1, _amount(params) - 1))

    def _erase_in_line(self, params: list[int]) -> None:
        mode = params[0] if params else 0
        start, stop = self._erase_span(mode)
        for column in range(start, stop):
            self._grid[self._row][column] = _BLANK
        self._written[self._row] = True

    def _erase_span(self, mode: int) -> tuple[int, int]:
        if mode == 1:
            return 0, min(self._col + 1, self._cols)
        if mode == 2:
            return 0, self._cols
        return self._col, self._cols

    def _erase_in_display(self, params: list[int]) -> None:
        mode = params[0] if params else 0
        if mode in (2, 3):
            self._blank_rows(range(self._rows))
            return
        if mode == 1:
            self._blank_rows(range(0, self._row))
            self._erase_in_line([1])
            return
        self._blank_rows(range(self._row + 1, self._rows))
        self._erase_in_line([0])

    def _blank_rows(self, indexes: range) -> None:
        for index in indexes:
            self._grid[index] = [_BLANK] * self._cols
            self._written[index] = True

    def _set_scroll_region(self, params: list[int]) -> None:
        top = (params[0] if params else 1) or 1
        bottom = (params[1] if len(params) > 1 else self._rows) or self._rows
        top_index = max(0, min(self._rows - 1, top - 1))
        bottom_index = max(top_index, min(self._rows - 1, bottom - 1))
        self._scroll_top, self._scroll_bottom = top_index, bottom_index
        self._row, self._col = top_index, 0

    def _scroll_up_csi(self, params: list[int]) -> None:
        self._scroll_up(_amount(params))

    def _scroll_down_csi(self, params: list[int]) -> None:
        top, bottom = self._scroll_top, self._scroll_bottom
        for _ in range(_amount(params)):
            del self._grid[bottom]
            del self._written[bottom]
            self._grid.insert(top, [_BLANK] * self._cols)
            self._written.insert(top, True)

    # Defined inside the class body so the handlers are plain names here
    # rather than private attribute access on the class from outside.
    _CSI_HANDLERS: ClassVar[_CsiTable] = {
        "H": _cup,
        "f": _cup,
        "A": _cursor_up,
        "B": _cursor_down,
        "C": _cursor_forward,
        "D": _cursor_back,
        "G": _column_absolute,
        "d": _row_absolute,
        "K": _erase_in_line,
        "J": _erase_in_display,
        "r": _set_scroll_region,
        "S": _scroll_up_csi,
        "T": _scroll_down_csi,
    }


def _amount(params: list[int]) -> int:
    return max(1, params[0] if params else 1)


def _numeric_params(raw: str) -> list[int]:
    values: list[int] = []
    for part in raw.split(";"):
        stripped = part.strip()
        values.append(int(stripped) if stripped.isdigit() else 0)
    return values


def _decode_utf8(buffer: bytes, index: int, size: int) -> tuple[str, int]:
    """Decode one UTF-8 character at ``index``; return ``(char, consumed)``.

    ``consumed == 0`` means the sequence is genuinely truncated at the end of
    the chunk and the caller should hold the bytes over.

    A declared byte count is not enough on its own: every byte after the lead
    must be a continuation (0x80-0xBF). A lead that announces three bytes and
    is followed by ``\x1b[`` is *corrupt*, not incomplete — consuming those two
    bytes on its word swallows the escape sequence, and a swallowed screen
    clear leaves an erased footer searchable, which is a false
    ``composer_stranded`` verdict (#7141 round 2).
    """
    lead = buffer[index]
    width = _utf8_width(lead)
    if width == 1:
        return _REPLACEMENT, 1
    end = index + width
    for offset in range(index + 1, min(end, size)):
        if not _is_continuation(buffer[offset]):
            # Corrupt, not truncated: the lead alone is the bad byte.
            return _REPLACEMENT, 1
    if end > size:
        return "", 0
    return buffer[index:end].decode("utf-8", errors="replace")[:1] or _REPLACEMENT, width


def _is_continuation(byte: int) -> bool:
    return 0x80 <= byte <= 0xBF


def _utf8_width(lead: int) -> int:
    """Bytes in the UTF-8 sequence this lead byte starts.

    Invalid leads (0xC0/0xC1 overlongs, 0xF5-0xFF, and stray continuation
    bytes) are width 1 so one bad byte becomes one replacement character
    instead of swallowing the bytes that follow it.
    """
    if 0xF0 <= lead <= 0xF4:
        return 4
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xC2 <= lead <= 0xDF:
        return 2
    return 1


def _char_cells(char: str) -> int:
    """Screen cells this character occupies, ignoring cluster joining.

    wcwidth semantics, narrowed to what a grid needs: East Asian Wide and
    Fullwidth forms take two cells; combining marks, variation selectors and
    format characters (including ZWJ) take none and decorate the cell already
    written. Getting this wrong desynchronises every subsequent cursor address
    on the row, so a later erase clears the wrong columns and leaves a footer
    behind (#7141 round 2).

    Cluster joining is the caller's job — see ``TerminalViewport._print``.
    """
    if char == _ZWJ or unicodedata.category(char) in _ZERO_WIDTH_CATEGORIES:
        return 0
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _string_sequence_length(buffer: bytes, start: int) -> int | None:
    """Length of an OSC/DCS/PM/APC string sequence, or None if unterminated."""
    index = start + 2
    size = len(buffer)
    while index < size:
        byte = buffer[index]
        if byte == _BEL:
            return index + 1 - start
        if byte == _ESC and index + 1 < size and buffer[index + 1] == 0x5C:
            return index + 2 - start
        if byte == _ESC and index + 1 >= size:
            return None
        index += 1
    return None


# ---------------------------------------------------------------------------
# Recording replay
# ---------------------------------------------------------------------------


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
        rows = screen_dimension(event.get("rows"), limit=_MAX_ROWS)
        cols = screen_dimension(event.get("cols"), limit=_MAX_COLS)
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
