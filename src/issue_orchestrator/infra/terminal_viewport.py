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

from typing import Callable, ClassVar

from .xterm_widths import EMPTY_CLUSTER, cluster_advance
from .terminal_resize import ResizePlan, plan_resize
from .terminal_protocol import (
    _ASCII_CHARSET,
    _DECAWM,
    _DEVICE_QUERY_PREFIXES,
    _ESCAPE_CHARSET_DESIGNATORS,
    _IGNORED_CSI_FINALS,
    _IGNORED_CSI_INTERMEDIATES,
    _IGNORED_ESCAPE_MARKERS,
    _IGNORED_PRIVATE_FINALS,
    RenderedScreen,
    blank_cells,
    erase_span,
    render_screen,
    _IGNORED_PRIVATE_MODES,
    _INERT_ANSI_RESETS,
    _C1_CSI,
    _C1_END,
    _C1_IND,
    _C1_NEL,
    _C1_START,
    _C1_STRING_INTRODUCERS,
    _ESCAPE_STRING_INTRODUCERS,
    BLANK,
    SavedCursor,
    _ESC,
    _TAB_WIDTH,
    amount,
    decode_utf8,
    numeric_params,
    scan_string_sequence,
    split_csi,
)
from .pending_wrap import (
    ColumnOperation,
    clears_parked_state,
    resolve_parked_column,
)
from .terminal_recording import MAX_TERMINAL_COLS, MAX_TERMINAL_ROWS

DEFAULT_ROWS = 40
DEFAULT_COLS = 120
_CsiTable = dict[str, "Callable[[TerminalViewport, list[int]], None]"]


class TerminalViewport:
    """A rows x cols character grid that applies the ops a TUI actually emits.

    Unrecognised escape sequences are consumed and ignored rather than printed:
    a stray ``ESC[?25l`` must not leave ``[?25l`` sitting in the grid where a
    marker search could trip over it.
    """

    def __init__(self, *, rows: int = DEFAULT_ROWS, cols: int = DEFAULT_COLS) -> None:
        if rows < 1 or cols < 1:
            raise ValueError("viewport geometry must be positive")
        self._rows = min(rows, MAX_TERMINAL_ROWS)
        self._cols = min(cols, MAX_TERMINAL_COLS)
        # Per-cell width, the way the terminal stores it: 2 marks a wide glyph
        # that owns the following column, 0 marks that follower. Rendering
        # skips what a wide owner covers, which is why a character written into
        # a follower can sit in the buffer and never appear on screen.
        self._grid, self._widths = blank_cells(self._rows, self._cols)
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
        self._saved = SavedCursor()
        self._cluster = EMPTY_CLUSTER
        self._autowrap = True
        self._scrollback_dropped = False
        # Pending wrap is a state bit, not an out-of-range column: DECRC
        # restores a column without the promise attached, so inferring one from
        # the other reproduces a screen the terminal does not draw.
        self._parked = False
        #: Grid-affecting state channels this model does not reproduce.
        self.unmodelled_state: list[str] = []
        self._scroll_top = 0
        self._scroll_bottom = self._rows - 1
        self._fed = 0
        self._pending = b""

    # -- public -----------------------------------------------------------

    def resize(self, *, rows: int, cols: int) -> None:
        """Apply a recorded resize event, reconciling the state it touches.

        The measured semantics — and why a reflowing column shrink is refused
        rather than guessed — live in :mod:`.terminal_resize`.
        """
        if rows < 1 or cols < 1:
            return
        plan = plan_resize(
            rows=min(rows, MAX_TERMINAL_ROWS),
            cols=min(cols, MAX_TERMINAL_COLS),
            current_rows=self._rows,
            current_cols=self._cols,
            cursor_row=self._row,
            cursor_col=self._col,
            saved=self._saved,
            written_extents=self._extent,
            scrollback_dropped=self._scrollback_dropped,
        )
        if plan is None:
            return
        for refusal in plan.refusals:
            self._refuse(refusal)
        self._reshape(plan)

    def _reshape(self, plan: ResizePlan) -> None:
        """Move the grid and the cursor state onto the planned geometry."""
        keep = plan.rows_dropped_from_top
        if keep:
            self._scrollback_dropped = True
        old_grid = self._grid[keep:]
        old_written = self._written[keep:]
        old_extent = self._extent[keep:]
        old_widths = self._widths[keep:]
        self._rows, self._cols = plan.rows, plan.cols
        self._grid, self._widths = blank_cells(plan.rows, plan.cols)
        self._written = [False] * plan.rows
        self._extent = [0] * plan.rows
        for index in range(min(len(old_grid), plan.rows)):
            row = old_grid[index][: plan.cols]
            self._grid[index][: len(row)] = row
            widths = old_widths[index][: plan.cols]
            self._widths[index][: len(widths)] = widths
            self._written[index] = old_written[index]
            self._extent[index] = min(old_extent[index], plan.cols)
        self._scroll_top = 0
        self._scroll_bottom = plan.rows - 1
        self._row, self._col = plan.cursor_row, plan.cursor_col
        self._parked = False
        self._saved = plan.saved

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
                char, consumed = decode_utf8(buffer, index, size)
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
        return render_screen(
            grid=self._grid,
            widths=self._widths,
            extents=self._extent,
            written=self._written,
            cursor_row=self._row,
            cursor_col=self._col,
            fed_bytes=self._fed,
        )

    # -- character handling -----------------------------------------------

    def _apply_control(self, byte: int) -> None:
        """Apply one single-byte ASCII control character.

        SO and SI need no refusal of their own: they select G1 or G0, and
        designating either as anything but ASCII is refused where the
        designation happens, so a shift can only ever land on ASCII here.
        """
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
        if self._parked or self._col + cells > self._cols:
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
                self._parked = False
                self._line_feed()
        if self._col < self._cols:
            self._break_wide_pair_at(self._col)
            self._grid[self._row][self._col] = char
            self._widths[self._row][self._col] = cells
            # A wide glyph owns the next column; the follower carries width 0
            # so rendering knows to skip it.
            if cells > 1 and self._col + 1 < self._cols:
                self._grid[self._row][self._col + 1] = BLANK
                self._widths[self._row][self._col + 1] = 0
            self._written[self._row] = True
            self._extent[self._row] = max(
                self._extent[self._row], min(self._col + cells, self._cols)
            )
        self._col += cells
        self._parked = self._col >= self._cols

    def _break_wide_pair_at(self, column: int) -> None:
        """Split any wide glyph this write lands on, as the terminal does.

        Overwriting either half of a wide glyph leaves a blank where the other
        half was — measured mid-row. The owner immediately to the left is only
        repaired at the start of a print run, which is why a character that
        overflows into a follower mid-run stays invisible instead.
        """
        if self._run_start and column > 0 and self._widths[self._row][column - 1] == 2:
            self._grid[self._row][column - 1] = BLANK
            self._widths[self._row][column - 1] = 1
        self._run_start = False
        if self._widths[self._row][column] == 2 and column + 1 < self._cols:
            self._grid[self._row][column + 1] = BLANK
            self._widths[self._row][column + 1] = 1

    def _attach_combining(self, char: str) -> None:
        """Decorate the last written cell rather than consuming a new one."""
        column = min(max(0, self._col - 1), self._cols - 1)
        while column > 0 and self._grid[self._row][column] == "":
            column -= 1
        cell = self._grid[self._row][column]
        # Nothing to decorate yet: replace the blank rather than trailing the
        # mark behind a space that was never printed.
        self._grid[self._row][column] = char if cell == BLANK else cell + char
        self._written[self._row] = True
        self._extent[self._row] = max(self._extent[self._row], column + 1)

    def _resolve(self, operation: ColumnOperation) -> None:
        """Apply this operation's parked-state resolution from the table."""
        self._col = resolve_parked_column(self._col, self._cols, operation)
        if clears_parked_state(operation):
            self._parked = False

    def _line_feed(self) -> None:
        if self._row == self._scroll_bottom:
            self._scroll_up(1)
            return
        self._row = min(self._rows - 1, self._row + 1)

    def _scroll_up(self, count: int) -> None:
        top, bottom = self._scroll_top, self._scroll_bottom
        if top == 0 and bottom == self._rows - 1:
            # Rows leaving the top of the *whole screen* enter the scrollback,
            # which a later row growth would restore. Measured: a scroll region
            # narrower than the screen discards them instead, even when it is
            # anchored to the top row.
            self._scrollback_dropped = True
        for _ in range(count):
            del self._grid[top]
            del self._widths[top]
            del self._written[top]
            del self._extent[top]
            self._grid.insert(bottom, [BLANK] * self._cols)
            self._widths.insert(bottom, [1] * self._cols)
            # A row scrolled into view is blank *because this replay scrolled
            # it*, so it is authoritative, not unknown history.
            self._written.insert(bottom, True)
            self._extent.insert(bottom, 0)

    # -- escape handling ---------------------------------------------------

    def _refuse(self, channel: str) -> None:
        """Record a state channel this model does not reproduce."""
        self.unmodelled_state.append(channel)

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
            return scan_string_sequence(buffer, payload)
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
            end = scan_string_sequence(buffer, start + 2)
            return None if end is None else end - start
        if marker in _ESCAPE_CHARSET_DESIGNATORS:
            return self._designate_charset(buffer, start)
        handler = self._ESCAPE_HANDLERS.get(marker)
        if handler is not None:
            handler(self)
            return 2
        if marker in _IGNORED_ESCAPE_MARKERS:
            return 2
        # Refuse by default: an escape nobody enumerated may carry state this
        # model neither reproduces nor knows it is missing.
        self._refuse(f"ESC {chr(marker)}")
        return 2

    def _designate_charset(self, buffer: bytes, start: int) -> int | None:
        """``ESC ( B`` and friends. Only the ASCII set is reproduced."""
        if start + 2 >= len(buffer):
            return None
        designation = chr(buffer[start + 2])
        if designation != _ASCII_CHARSET:
            # A line-drawing or national set changes what every subsequent
            # byte renders as; refusing is cheaper than a translation table
            # real recordings never exercise.
            self._refuse(f"ESC {chr(buffer[start + 1])}{designation}")
        return 3

    def _save_cursor(self) -> None:
        """DECSC. Measured: saves row, column and charset, never the wrap.

        Restoring never brings back a pending wrap — a restored column-10
        cursor does not wrap the next glyph (#7141 round 8) — but the column it
        saves is the overflow one, which only shows once a resize widens the
        screen enough for that column to exist (#7141 round 10).
        """
        # A parked cursor sits one past the last cell, and DECSC keeps that
        # overflow column rather than clamping it away: restoring inside the
        # same width clamps it back anyway, but restoring after the screen has
        # grown lands on the column the terminal really saved (round 10).
        self._saved = SavedCursor(row=self._row, column=self._col)

    def _restore_cursor(self) -> None:
        """DECRC. Restores position without the pending-wrap promise."""
        self._resolve(ColumnOperation.RESTORE_CURSOR)
        self._row = min(self._saved.row, self._rows - 1)
        self._col = min(self._saved.column, self._cols - 1)

    def _reverse_index(self) -> None:
        """ESC M. Measured: up one row, scrolling the region down at the top.

        Real recordings emit this 343 times in a single session; ignoring it
        silently mislaid every one of them.
        """
        self._resolve(ColumnOperation.REVERSE_INDEX)
        if self._row == self._scroll_top:
            self._scroll_down_csi([1])
            return
        self._row = max(0, self._row - 1)

    def _index(self) -> None:
        """ESC D, the escape spelling of IND."""
        self._resolve(ColumnOperation.LINE_FEED)
        self._line_feed()

    def _next_line(self) -> None:
        """ESC E, the escape spelling of NEL."""
        self._resolve(ColumnOperation.NEXT_LINE)
        self._col = 0
        self._line_feed()

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
        parameters, intermediates = split_csi(params_raw)
        if intermediates:
            # An intermediate byte selects a different sequence entirely:
            # DECSCUSR is ``CSI <space> q``, not ``CSI q``, and DECSTR is
            # ``CSI ! p``, not ``CSI p``.
            self._dispatch_csi_with_intermediates(intermediates, final)
            return
        if parameters.startswith("?"):
            self._dispatch_private_mode(final, parameters[1:])
            return
        if parameters[:1] in _DEVICE_QUERY_PREFIXES:
            # ``CSI >``, ``CSI =`` and ``CSI <`` introduce device queries
            # (XTVERSION, secondary/tertiary DA). Measured inert: they report
            # state rather than changing it.
            return
        params = numeric_params(parameters)
        handler = self._CSI_HANDLERS.get(final)
        if handler is not None:
            handler(self, params)
            return
        if final in _IGNORED_CSI_FINALS:
            return
        if final in ("h", "l"):
            self._dispatch_ansi_mode(final, params)
            return
        self._refuse(f"CSI {params_raw}{final}")

    def _dispatch_csi_with_intermediates(
        self, intermediates: str, final: str
    ) -> None:
        sequence = f"{intermediates}{final}"
        if sequence == "!p":
            self._soft_reset()
            return
        if sequence in _IGNORED_CSI_INTERMEDIATES:
            return
        self._refuse(f"CSI {sequence}")

    def _dispatch_ansi_mode(self, final: str, params: list[int]) -> None:
        """SM/RM. Insert mode shifts a row's contents, so it is not ignorable.

        Real recordings contain no ANSI mode setting at all, so refusing costs
        nothing; ``RM 4`` is allowed because it selects the default this model
        already reproduces, and was measured inert.
        """
        for mode in params:
            if final == "l" and mode in _INERT_ANSI_RESETS:
                continue
            self._refuse(f"CSI {mode}{final}")

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
            if final in _IGNORED_PRIVATE_FINALS:
                # Queries and keyboard-protocol reports; measured inert.
                return
            # Anything else behind this prefix is a sequence in its own right —
            # DECSED erases the display, DECSEL erases the line — and the
            # refusal floor has to cover them too, or it is exactly one prefix
            # wide (#7141 round 9).
            self._refuse(f"CSI ?{params_raw}{final}")
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
            self._refuse(f"?{mode}{final}")

    def _soft_reset(self) -> None:
        """DECSTR. Measured: restores autowrap and the scroll region only.

        It leaves the screen, the cursor and a parked column exactly where they
        were — which is what separates it from RIS. It does *also* clear the
        saved cursor and the charset designation, which the round-7 reading of
        this reset missed; the charset half needs no code because a non-ASCII
        designation is refused before it can be reset.
        """
        self._resolve(ColumnOperation.SOFT_RESET)
        self._autowrap = True
        self._scroll_top, self._scroll_bottom = 0, self._rows - 1
        self._saved = SavedCursor()

    def _reset(self) -> None:
        """RIS. Measured: everything back to defaults, autowrap included.

        Leaving autowrap off across a reset rendered a screen the terminal does
        not draw, with no refusal to flag it (#7141 round 7). The saved cursor
        goes with it — measured in round 8, which is what the round-7 claim
        missed.
        """
        self._resolve(ColumnOperation.FULL_RESET)
        self._autowrap = True
        # Measured: RIS empties the scrollback, so growth is faithful again.
        self._scrollback_dropped = False
        self._run_start = True
        self._saved = SavedCursor()
        self._grid, self._widths = blank_cells(self._rows, self._cols)
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
        self._row = max(0, self._row - amount(params))

    def _cursor_down(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_RELATIVE)
        self._row = min(self._rows - 1, self._row + amount(params))

    def _cursor_forward(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_RELATIVE)
        self._col = min(self._cols - 1, self._col + amount(params))

    def _cursor_back(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_RELATIVE)
        self._col = max(0, self._col - amount(params))

    def _column_absolute(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_COLUMN_ABSOLUTE)
        self._col = max(0, min(self._cols - 1, amount(params) - 1))

    def _row_absolute(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.CURSOR_ROW_ABSOLUTE)
        self._row = max(0, min(self._rows - 1, amount(params) - 1))

    def _erase_in_line(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.ERASE)
        mode = params[0] if params else 0
        start, stop = erase_span(mode, column=self._col, cols=self._cols)
        self._blank_span(start, stop)
        if stop >= self._cols:
            # Erasing to the end of the line untouches those cells again.
            self._extent[self._row] = min(self._extent[self._row], start)

    def _erase_characters(self, params: list[int]) -> None:
        """ECH. Blanks cells from the cursor without moving it."""
        self._resolve(ColumnOperation.ERASE)
        self._blank_span(self._col, min(self._cols, self._col + amount(params)))

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

    def _blank_span(self, start: int, stop: int) -> None:
        """Blank cells on the cursor's row without moving it."""
        for column in range(start, stop):
            self._grid[self._row][column] = BLANK
            self._widths[self._row][column] = 1
        self._written[self._row] = True

    def _blank_rows(self, indexes: range) -> None:
        for index in indexes:
            self._grid[index] = [BLANK] * self._cols
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
        self._scroll_up(amount(params))

    def _scroll_down_csi(self, params: list[int]) -> None:
        self._resolve(ColumnOperation.SCROLL)
        top, bottom = self._scroll_top, self._scroll_bottom
        for _ in range(amount(params)):
            del self._grid[bottom]
            del self._widths[bottom]
            del self._written[bottom]
            del self._extent[bottom]
            self._grid.insert(top, [BLANK] * self._cols)
            self._widths.insert(top, [1] * self._cols)
            self._written.insert(top, True)
            self._extent.insert(top, 0)

    # Defined inside the class body so the handlers are plain names here
    # rather than private attribute access on the class from outside.
    def _save_cursor_csi(self, params: list[int]) -> None:
        del params
        self._save_cursor()

    def _restore_cursor_csi(self, params: list[int]) -> None:
        del params
        self._restore_cursor()

    # Defined inside the class body so the handlers are plain names here
    # rather than private attribute access on the class from outside.
    _ESCAPE_HANDLERS: ClassVar[dict[int, Callable[["TerminalViewport"], None]]] = {
        0x37: _save_cursor,     # ESC 7  DECSC
        0x38: _restore_cursor,  # ESC 8  DECRC
        0x44: _index,           # ESC D  IND
        0x45: _next_line,       # ESC E  NEL
        0x4D: _reverse_index,   # ESC M  RI
        0x63: _reset,           # ESC c  RIS
    }

    _CSI_HANDLERS: ClassVar[_CsiTable] = {
        "s": _save_cursor_csi,
        "u": _restore_cursor_csi,
        "H": _cup,
        "f": _cup,
        "A": _cursor_up,
        "B": _cursor_down,
        "C": _cursor_forward,
        "D": _cursor_back,
        "G": _column_absolute,
        "d": _row_absolute,
        "K": _erase_in_line,
        "X": _erase_characters,
        "J": _erase_in_display,
        "r": _set_scroll_region,
        "S": _scroll_up_csi,
        "T": _scroll_down_csi,
    }
