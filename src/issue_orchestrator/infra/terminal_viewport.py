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

from dataclasses import dataclass
from typing import Callable, ClassVar

from .xterm_widths import EMPTY_CLUSTER, cluster_advance
from .pending_wrap import ColumnOperation, resolve_parked_column
from .terminal_recording import MAX_TERMINAL_COLS, MAX_TERMINAL_ROWS

DEFAULT_ROWS = 40
DEFAULT_COLS = 120
_MAX_ROWS = MAX_TERMINAL_ROWS
_MAX_COLS = MAX_TERMINAL_COLS
_BLANK = " "
#: DECAWM. Modelled, because it decides whether a long line wraps.
_DECAWM = 7
#: Private modes measured to leave the character grid untouched. Anything not
#: listed is refused rather than assumed harmless — see
#: ``TerminalViewport._dispatch_private_mode``. Extend it by measuring, with
#: ``tools/measure_xterm_widths.js modes``.
_IGNORED_PRIVATE_MODES: frozenset[int] = frozenset(
    {
        1,     # DECCKM, cursor key format
        5,     # DECSCNM, reverse video
        8,     # DECARM, auto-repeat
        9,     # X10 mouse reporting
        12,    # cursor blink
        25,    # DECTCEM, cursor visibility
        40,    # allow 80/132 switching
        66,    # DECNKM, numeric keypad
        1000,  # VT200 mouse reporting
        1002,  # button-event mouse tracking
        1004,  # focus reporting
        1006,  # SGR mouse encoding
        2004,  # bracketed paste
        2026,  # synchronised output
        2031,  # colour-scheme change notification
    }
)

_C1_START = 0x80
_C1_END = 0x9F
_C1_IND = 0x84
_C1_NEL = 0x85
_C1_CSI = 0x9B
#: C1 introducers that open a string sequence: DCS, SOS, OSC, PM, APC.
_C1_STRING_INTRODUCERS = frozenset({0x90, 0x98, 0x9D, 0x9E, 0x9F})
#: The same five as two-byte escapes: ESC P, ESC X, ESC ], ESC ^, ESC _.
_ESCAPE_STRING_INTRODUCERS = frozenset({0x50, 0x58, 0x5D, 0x5E, 0x5F})
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
        # Per-cell width, the way the terminal stores it: 2 marks a wide glyph
        # that owns the following column, 0 marks that follower. Rendering
        # skips what a wide owner covers, which is why a character written into
        # a follower can sit in the buffer and never appear on screen.
        self._widths: list[list[int]] = [[1] * self._cols for _ in range(self._rows)]
        # A wide owner immediately left of the cursor is blanked once per print
        # run, not once per character — measured.
        self._run_start = True
        self._written: list[bool] = [False] * self._rows
        # Cells actually touched on each row. The terminal trims *untouched*
        # trailing cells but keeps a space someone wrote, so a plain rstrip()
        # renders a different row than the viewer shows.
        self._extent: list[int] = [0] * self._rows
        self._row = 0
        self._col = 0
        self._saved: tuple[int, int] = (0, 0)
        self._cluster = EMPTY_CLUSTER
        self._autowrap = True
        #: Grid-affecting private modes this model does not reproduce.
        self.unmodelled_modes: list[str] = []
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
        old_grid, old_written, old_extent = self._grid, self._written, self._extent
        old_widths = self._widths
        self._rows, self._cols = rows, cols
        self._grid = self._blank_grid()
        self._widths = [[1] * cols for _ in range(rows)]
        self._written = [False] * rows
        self._extent = [0] * rows
        for index in range(min(len(old_grid), rows)):
            row = old_grid[index][:cols]
            self._grid[index][: len(row)] = row
            widths = old_widths[index][:cols]
            self._widths[index][: len(widths)] = widths
            self._written[index] = old_written[index]
            self._extent[index] = min(old_extent[index], cols)
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
                # Measured: any escape sequence breaks the cluster too, even
                # one that does not move the cursor.
                self._cluster = EMPTY_CLUSTER
                self._run_start = True
                index += consumed
                continue
            if byte >= 0x80:
                char, consumed = _decode_utf8(buffer, index, size)
                if consumed == 0:
                    # Genuinely truncated at the chunk edge — hold it over.
                    self._pending = buffer[index:]
                    return
                if not char:
                    # Undecodable: xterm's UTF-8 decoder drops the byte rather
                    # than substituting anything, so nothing reaches the screen
                    # and the cluster in progress is untouched.
                    index += consumed
                    continue
                codepoint = ord(char)
                if _C1_START <= codepoint <= _C1_END:
                    self._cluster = EMPTY_CLUSTER
                    self._run_start = True
                    resumed = self._apply_c1(codepoint, buffer, index + consumed)
                    if resumed is None:
                        self._pending = buffer[index:]
                        return
                    index = resumed
                    continue
                self._print(char)
                index += consumed
                continue
            if byte < 0x20 or byte == 0x7F:
                # Measured against the bundled xterm: a control byte breaks the
                # grapheme cluster, so a combining mark after CR/LF/BS takes a
                # cell of its own instead of joining what came before.
                self._cluster = EMPTY_CLUSTER
                self._run_start = True
                self._apply_control(byte)
                index += 1
                continue
            self._print(chr(byte))
            index += 1

    def render(self) -> RenderedScreen:
        """Freeze the current screen. Non-mutating; safe to call repeatedly."""
        rows = tuple(
            _render_row(row, widths, extent)
            for row, widths, extent in zip(self._grid, self._widths, self._extent)
        )
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

    def _apply_control(self, byte: int) -> None:
        """Apply one single-byte ASCII control character."""
        if byte == 0x0D:  # CR
            self._resolve(ColumnOperation.CARRIAGE_RETURN)
            self._col = 0
            return
        if byte in (0x0A, 0x0B, 0x0C):  # LF, VT, FF all index a line
            self._resolve(ColumnOperation.LINE_FEED)
            self._line_feed()
            return
        if byte == 0x08:  # BS
            self._resolve(ColumnOperation.BACKSPACE)
            self._col = max(0, self._col - 1)
            return
        if byte == 0x09:  # HT
            self._resolve(ColumnOperation.HORIZONTAL_TAB)
            if self._col >= self._cols:
                # Measured: a parked cursor is left alone by a tab.
                return
            self._col = min(self._cols - 1, (self._col // _TAB_WIDTH + 1) * _TAB_WIDTH)
            return

    def _print(self, char: str) -> None:
        # Cell width and cluster joining come from the bundled xterm's own
        # model (``infra.xterm_widths``), not from a wcwidth-shaped guess: the
        # viewer renders emoji one cell wide and joins only zero-width
        # codepoints, so a four-emoji ZWJ family is four cells. Modelling it as
        # two put a footer on a row xterm wraps (#7141 round 4).
        cells, self._cluster = cluster_advance(ord(char), self._cluster)
        if cells <= 0:
            self._attach_combining(char)
            return
        if self._col + cells > self._cols:
            self._resolve(
                ColumnOperation.PRINT_WRAPPING
                if self._autowrap
                else ColumnOperation.PRINT_WITHOUT_WRAP
            )
            if not self._autowrap:
                # Measured with DECAWM off: a wide glyph that will not fit is
                # skipped entirely, and a narrow one overwrites the last cell
                # while the cursor stays parked past the edge.
                if cells > 1:
                    return
                self._col = self._cols - 1
            elif self._col > 0:
                # Wrap only when there is somewhere to wrap from: a glyph wider
                # than the whole row still gets drawn where it stands, and the
                # cursor is allowed past the right edge, because that is what
                # xterm does on a screen too narrow for the glyph.
                self._col = 0
                self._line_feed()
        if self._col < self._cols:
            self._break_wide_pair_at(self._col)
            self._grid[self._row][self._col] = char
            self._widths[self._row][self._col] = cells
            # A wide glyph owns the next column; the follower carries width 0
            # so rendering knows to skip it.
            if cells > 1 and self._col + 1 < self._cols:
                self._grid[self._row][self._col + 1] = _BLANK
                self._widths[self._row][self._col + 1] = 0
            self._written[self._row] = True
            self._extent[self._row] = max(
                self._extent[self._row], min(self._col + cells, self._cols)
            )
        self._col += cells

    def _break_wide_pair_at(self, column: int) -> None:
        """Split any wide glyph this write lands on, as the terminal does.

        Overwriting either half of a wide glyph leaves a blank where the other
        half was — measured mid-row. The owner immediately to the left is only
        repaired at the start of a print run, which is why a character that
        overflows into a follower mid-run stays invisible instead.
        """
        if self._run_start and column > 0 and self._widths[self._row][column - 1] == 2:
            self._grid[self._row][column - 1] = _BLANK
            self._widths[self._row][column - 1] = 1
        self._run_start = False
        if self._widths[self._row][column] == 2 and column + 1 < self._cols:
            self._grid[self._row][column + 1] = _BLANK
            self._widths[self._row][column + 1] = 1

    def _attach_combining(self, char: str) -> None:
        """Decorate the last written cell rather than consuming a new one."""
        column = min(max(0, self._col - 1), self._cols - 1)
        while column > 0 and self._grid[self._row][column] == "":
            column -= 1
        cell = self._grid[self._row][column]
        # Nothing to decorate yet: replace the blank rather than trailing the
        # mark behind a space that was never printed.
        self._grid[self._row][column] = char if cell == _BLANK else cell + char
        self._written[self._row] = True
        self._extent[self._row] = max(self._extent[self._row], column + 1)

    def _resolve(self, operation: ColumnOperation) -> None:
        """Apply this operation's parked-column resolution from the table."""
        self._col = resolve_parked_column(self._col, self._cols, operation)

    def _line_feed(self) -> None:
        if self._row == self._scroll_bottom:
            self._scroll_up(1)
            return
        self._row = min(self._rows - 1, self._row + 1)

    def _scroll_up(self, count: int) -> None:
        top, bottom = self._scroll_top, self._scroll_bottom
        for _ in range(count):
            del self._grid[top]
            del self._widths[top]
            del self._written[top]
            del self._extent[top]
            self._grid.insert(bottom, [_BLANK] * self._cols)
            self._widths.insert(bottom, [1] * self._cols)
            # A row scrolled into view is blank *because this replay scrolled
            # it*, so it is authoritative, not unknown history.
            self._written.insert(bottom, True)
            self._extent.insert(bottom, 0)

    # -- escape handling ---------------------------------------------------

    def _apply_c1(self, codepoint: int, buffer: bytes, payload: int) -> int | None:
        """Apply a C1 control, returning where parsing resumes.

        Measured against the vendored xterm (``tools/measure_xterm_widths.js
        controls``): of the 32 C1s, only IND and NEL move the cursor, six
        introduce a sequence exactly as their two-byte ESC forms do, and the
        remaining 24 — U+008D among them, despite being RI on paper — are
        consumed with no visible effect. Treating them as text is what let a
        NEL keep a footer on one row that xterm splits across two (#7141 r5).
        """
        if codepoint == _C1_IND:
            self._resolve(ColumnOperation.LINE_FEED)
            self._line_feed()
            return payload
        if codepoint == _C1_NEL:
            self._resolve(ColumnOperation.NEXT_LINE)
            self._col = 0
            self._line_feed()
            return payload
        if codepoint == _C1_CSI:
            return self._scan_csi(buffer, payload)
        if codepoint in _C1_STRING_INTRODUCERS:
            return _scan_string_sequence(buffer, payload)
        return payload

    def _apply_escape(self, buffer: bytes, start: int) -> int | None:
        """Return bytes consumed from ``start``, or None if incomplete."""
        if start + 1 >= len(buffer):
            return None
        marker = buffer[start + 1]
        if marker == 0x5B:  # '['
            end = self._scan_csi(buffer, start + 2)
            return None if end is None else end - start
        if marker in _ESCAPE_STRING_INTRODUCERS:  # OSC / DCS / SOS / PM / APC
            end = _scan_string_sequence(buffer, start + 2)
            return None if end is None else end - start
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

    def _scan_csi(self, buffer: bytes, payload: int) -> int | None:
        """Apply a CSI whose parameters start at ``payload``; return its end.

        Shared by ``ESC [`` and the single-codepoint C1 CSI (U+009B), which the
        terminal treats as the same sequence — measured identical.
        """
        index = payload
        size = len(buffer)
        while index < size and (0x30 <= buffer[index] <= 0x3F):
            index += 1
        while index < size and (0x20 <= buffer[index] <= 0x2F):
            index += 1
        if index >= size:
            return None
        final = buffer[index]
        params_raw = buffer[payload:index].decode("ascii", errors="replace")
        self._dispatch_csi(chr(final), params_raw)
        return index + 1

    def _dispatch_csi(self, final: str, params_raw: str) -> None:
        if final == "p" and params_raw == "!":
            self._soft_reset()
            return
        if params_raw.startswith("?"):
            self._dispatch_private_mode(final, params_raw[1:])
            return
        params = _numeric_params(params_raw)
        handler = self._CSI_HANDLERS.get(final)
        if handler is None:
            return
        handler(self, params)

    def _dispatch_private_mode(self, final: str, params_raw: str) -> None:
        """Apply, ignore, or refuse a private mode.

        The old blanket assumption that ``CSI ?`` never touches the grid was
        wrong: DECAWM decides whether a long line wraps, so ignoring it drew a
        footer on a row the terminal does not have (#7141 round 6). Rather than
        fix that one mode, the set is now enumerated three ways —

        modelled
            DECAWM, because real TUIs toggle it constantly and refusing on it
            would leave the discriminator useless.
        ignored
            Modes measured to leave the grid untouched (cursor visibility,
            synchronised output, mouse and paste reporting, ...).
        refused
            Everything else, including modes known to move the grid
            (DECCOLM, DECOM, the alternate buffers) and anything simply not
            measured. A refusal makes the recording untrustworthy, which
            surfaces as UNDETERMINED rather than a verdict read off a screen
            this model did not reproduce.
        """
        if final not in ("h", "l"):
            # A query such as DECRQM reports state; it never sets any.
            return
        enable = final == "h"
        for raw in params_raw.split(";"):
            stripped = raw.strip()
            if not stripped.isdigit():
                continue
            mode = int(stripped)
            if mode == _DECAWM:
                self._resolve(ColumnOperation.SET_AUTOWRAP)
                self._autowrap = enable
                continue
            if mode in _IGNORED_PRIVATE_MODES:
                continue
            self.unmodelled_modes.append(f"?{mode}{final}")

    def _soft_reset(self) -> None:
        """DECSTR. Measured: restores autowrap and the scroll region only.

        It leaves the screen, the cursor and a parked column exactly where they
        were — which is what separates it from RIS.
        """
        self._resolve(ColumnOperation.SOFT_RESET)
        self._autowrap = True
        self._scroll_top, self._scroll_bottom = 0, self._rows - 1

    def _reset(self) -> None:
        """RIS. Measured: everything back to defaults, autowrap included.

        Leaving autowrap off across a reset rendered a screen the terminal does
        not draw, with no refusal to flag it (#7141 round 7).
        """
        self._resolve(ColumnOperation.FULL_RESET)
        self._autowrap = True
        self._run_start = True
        self._grid = self._blank_grid()
        self._widths = [[1] * self._cols for _ in range(self._rows)]
        self._written = [True] * self._rows
        self._extent = [0] * self._rows
        self._row = self._col = 0
        self._cluster = EMPTY_CLUSTER
        self._scroll_top, self._scroll_bottom = 0, self._rows - 1

    # -- CSI operations ----------------------------------------------------

    def _cup(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_POSITION)
        row = (params[0] if params else 1) or 1
        col = (params[1] if len(params) > 1 else 1) or 1
        self._row = max(0, min(self._rows - 1, row - 1))
        self._col = max(0, min(self._cols - 1, col - 1))

    def _cursor_up(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_RELATIVE)
        self._row = max(0, self._row - _amount(params))

    def _cursor_down(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_RELATIVE)
        self._row = min(self._rows - 1, self._row + _amount(params))

    def _cursor_forward(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_RELATIVE)
        self._col = min(self._cols - 1, self._col + _amount(params))

    def _cursor_back(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_RELATIVE)
        self._col = max(0, self._col - _amount(params))

    def _column_absolute(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_COLUMN_ABSOLUTE)
        self._col = max(0, min(self._cols - 1, _amount(params) - 1))

    def _row_absolute(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_ROW_ABSOLUTE)
        self._row = max(0, min(self._rows - 1, _amount(params) - 1))

    def _erase_in_line(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.ERASE)
        mode = params[0] if params else 0
        start, stop = self._erase_span(mode)
        for column in range(start, stop):
            self._grid[self._row][column] = _BLANK
            self._widths[self._row][column] = 1
        self._written[self._row] = True
        if stop >= self._cols:
            # Erasing to the end of the line untouches those cells again.
            self._extent[self._row] = min(self._extent[self._row], start)

    def _erase_span(self, mode: int) -> tuple[int, int]:
        if mode == 1:
            return 0, min(self._col + 1, self._cols)
        if mode == 2:
            return 0, self._cols
        return self._col, self._cols

    def _erase_in_display(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.ERASE)
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
            self._widths[index] = [1] * self._cols
            self._written[index] = True
            self._extent[index] = 0

    def _set_scroll_region(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.SET_SCROLL_REGION)
        top = (params[0] if params else 1) or 1
        bottom = (params[1] if len(params) > 1 else self._rows) or self._rows
        top_index = max(0, min(self._rows - 1, top - 1))
        bottom_index = max(top_index, min(self._rows - 1, bottom - 1))
        self._scroll_top, self._scroll_bottom = top_index, bottom_index
        # Measured: with origin mode off — the only mode this viewport models,
        # DECOM being refused — setting the region homes to the screen origin,
        # not to the top of the region.
        self._row, self._col = 0, 0

    def _scroll_up_csi(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.SCROLL)
        self._scroll_up(_amount(params))

    def _scroll_down_csi(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.SCROLL)
        top, bottom = self._scroll_top, self._scroll_bottom
        for _ in range(_amount(params)):
            del self._grid[bottom]
            del self._widths[bottom]
            del self._written[bottom]
            del self._extent[bottom]
            self._grid.insert(top, [_BLANK] * self._cols)
            self._widths.insert(top, [1] * self._cols)
            self._written.insert(top, True)
            self._extent.insert(top, 0)

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


def _render_row(cells: list[str], widths: list[int], extent: int) -> str:
    """Render one row the way the terminal presents it.

    A wide glyph covers the following column, so whatever sits in that
    follower cell is never shown — which is exactly how a character that
    overflowed into it stays invisible.
    """
    rendered: list[str] = []
    column = 0
    while column < extent:
        width = widths[column]
        if width == 0:
            column += 1
            continue
        rendered.append(cells[column] or _BLANK)
        column += max(1, width)
    return "".join(rendered)


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
    the chunk and the caller should hold the bytes over. An empty ``char`` with
    a non-zero ``consumed`` means the bytes are undecodable and the terminal
    drops them — measured: xterm's decoder emits nothing at all for an invalid
    byte, so substituting a replacement character would shift every column
    after it.

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
        return "", 1
    end = index + width
    for offset in range(index + 1, min(end, size)):
        if not _is_continuation(buffer[offset]):
            # Corrupt, not truncated: drop the lead and re-read the next byte.
            return "", 1
    if end > size:
        return "", 0
    decoded = buffer[index:end].decode("utf-8", errors="ignore")[:1]
    return decoded, width


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


def _scan_string_sequence(buffer: bytes, payload: int) -> int | None:
    """End of an OSC/DCS/SOS/PM/APC string whose body starts at ``payload``.

    ``None`` when unterminated, which leaves the bytes pending and therefore
    unrendered — the same visible result as the terminal sitting in the string
    state waiting for a terminator that never comes.
    """
    index = payload
    size = len(buffer)
    while index < size:
        byte = buffer[index]
        if byte == _BEL:
            return index + 1
        if byte == _ESC and index + 1 < size and buffer[index + 1] == 0x5C:
            return index + 2
        if byte == _ESC and index + 1 >= size:
            return None
        index += 1
    return None
