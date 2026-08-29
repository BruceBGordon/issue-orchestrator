"""The terminal byte protocol, and which of its channels we support.

Two views of one concern: how a recorded byte stream decomposes into
dispatches, and what this model does with each dispatch it finds. Keeping both
out of ``terminal_viewport`` leaves that module about the grid.

Rounds 6 to 8 of #7141 were all the same bug — state the model did not know was
state — so the dispatch is closed by construction. Every channel gets one of
three dispositions:

modelled
    Reproduced, measured against the bundled xterm.
ignored
    Measured to leave the grid untouched.
refused
    Everything else, including channels known to move the grid and anything
    simply not measured. A refusal makes the recording untrustworthy, which
    surfaces as UNDETERMINED rather than a verdict read off a screen this model
    cannot reproduce.

The allowlists are grounded in what real recordings emit — two sessions of
21 MB and 1.4 MB replay with zero refusals — so the refusal path cannot quietly
gut the discriminator on live data. Extend one by measuring, with
``tools/measure_xterm_widths.js state``.
"""

from __future__ import annotations

from dataclasses import dataclass

_ESC = 0x1B
_BEL = 0x07
REPLACEMENT = "\ufffd"
BLANK = " "

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

#: ``ESC ( B`` designates plain ASCII, the default this model reproduces.
_ASCII_CHARSET = "B"
#: Private parameter prefixes that introduce a device *query*, measured inert.
_DEVICE_QUERY_PREFIXES = frozenset({">", "=", "<"})
#: ``ESC (``, ``ESC )``, ``ESC *``, ``ESC +`` — the G0..G3 designators.
_ESCAPE_CHARSET_DESIGNATORS = frozenset({0x28, 0x29, 0x2A, 0x2B})
#: Escapes measured to leave the grid untouched: keypad modes and a stray ST.
_IGNORED_ESCAPE_MARKERS = frozenset({0x3D, 0x3E, 0x5C})
#: CSI finals measured inert: SGR, and the device/status queries.
_IGNORED_CSI_FINALS = frozenset({"m", "c", "n"})
#: ``<intermediate><final>`` pairs measured inert. DECSCUSR is 45k of the
#: sequences in one real recording and changes nothing on the grid.
_IGNORED_CSI_INTERMEDIATES = frozenset({" q", "$p"})
#: Private-prefixed CSI finals other than h/l that are measured inert. ``u`` is
#: the keyboard-protocol query real recordings emit; ``c`` and ``n`` are device
#: and status reports. Every other private final — DECSED ``?J`` and DECSEL
#: ``?K`` among them — moves the grid and is refused.
_IGNORED_PRIVATE_FINALS = frozenset({"u", "c", "n"})
#: ANSI modes whose *reset* selects the default this model already reproduces.
_INERT_ANSI_RESETS = frozenset({4})


def split_csi(raw: str) -> tuple[str, str]:
    """Split a CSI body into its parameter bytes and its intermediate bytes."""
    for index, char in enumerate(raw):
        if 0x20 <= ord(char) <= 0x2F:
            return raw[:index], raw[index:]
    return raw, ""


def numeric_params(raw: str) -> list[int]:
    values: list[int] = []
    for part in raw.split(";"):
        stripped = part.strip()
        values.append(int(stripped) if stripped.isdigit() else 0)
    return values


def decode_utf8(buffer: bytes, index: int, size: int) -> tuple[str, int]:
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
    width = utf8_width(lead)
    if width == 1:
        return "", 1
    end = index + width
    for offset in range(index + 1, min(end, size)):
        if not is_continuation(buffer[offset]):
            # Corrupt, not truncated: drop the lead and re-read the next byte.
            return "", 1
    if end > size:
        return "", 0
    decoded = buffer[index:end].decode("utf-8", errors="ignore")[:1]
    return decoded, width


def is_continuation(byte: int) -> bool:
    return 0x80 <= byte <= 0xBF


def utf8_width(lead: int) -> int:
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


def scan_string_sequence(buffer: bytes, payload: int) -> int | None:
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


def amount(params: list[int]) -> int:
    return max(1, params[0] if params else 1)


@dataclass
class SavedCursor:
    """What DECSC captures. Measured: position and charset, never the wrap."""

    row: int = 0
    column: int = 0


#: ``ESC <marker>`` handlers. Anything absent from here and from
#: ``_IGNORED_ESCAPE_MARKERS`` is refused rather than silently dropped.


def render_screen(
    *,
    grid: list[list[str]],
    widths: list[list[int]],
    extents: list[int],
    written: list[bool],
    cursor_row: int,
    cursor_col: int,
    fed_bytes: int,
) -> "RenderedScreen":
    """Project the cell store into the screen a reader sees.

    Pure: the caller's state is only read, so a snapshot can be taken at any
    time, repeatedly, including from another thread.
    """
    rows = tuple(
        render_row(row, row_widths, extent)
        for row, row_widths, extent in zip(grid, widths, extents)
    )
    return RenderedScreen(
        rows=rows,
        written_rows=tuple(
            text for text, was_written in zip(rows, written) if was_written
        ),
        cursor_row=cursor_row,
        cursor_col=cursor_col,
        fed_bytes=fed_bytes,
    )


def blank_cells(rows: int, cols: int) -> tuple[list[list[str]], list[list[int]]]:
    """A fresh cell store: every cell blank, every cell one column wide."""
    return (
        [[BLANK] * cols for _ in range(rows)],
        [[1] * cols for _ in range(rows)],
    )


def erase_span(mode: int, *, column: int, cols: int) -> tuple[int, int]:
    """The half-open span ``CSI <mode> K`` blanks, relative to the cursor."""
    if mode == 1:
        return 0, min(column + 1, cols)
    if mode == 2:
        return 0, cols
    return column, cols


@dataclass(frozen=True)
class RenderedScreen:
    """The reconstructed viewport plus the honesty flags that qualify it.

    Lives beside :func:`render_row`, which produces its rows.
    """

    rows: tuple[str, ...]
    written_rows: tuple[str, ...]
    cursor_row: int
    cursor_col: int
    fed_bytes: int

    @property
    def written_row_count(self) -> int:
        return len(self.written_rows)


def render_row(cells: list[str], widths: list[int], extent: int) -> str:
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
        rendered.append(cells[column] or BLANK)
        column += max(1, width)
    return "".join(rendered)
