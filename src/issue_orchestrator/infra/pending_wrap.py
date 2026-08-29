"""What each terminal operation does to a cursor parked past the right edge.

A terminal that defers wrapping leaves the cursor one column *past* the last
one after a row fills up. That parked position is not a cursor position — it is
a promise that the next glyph starts a new row — and every operation that
follows either resolves it, replaces it, or leaves it standing. The rule is
genuinely per-operation: a line feed pulls the cursor back onto the row while a
horizontal tab leaves it parked, and a backspace from a parked cursor therefore
lands *two* visual columns left of the character just written.

That lifecycle used to be implicit in an out-of-range integer, with each
handler clamping (or forgetting to clamp) on its own. Round 6 of #7141 shipped
a backspace bug straight out of that shape, and round 7 a reset bug. It is a
table now: one row per operation, every row measured against the bundled xterm
and dedicated-tested, so the next interaction of this kind cannot be written
without failing a row.

Regenerate the measurements behind the table with::

    node tools/measure_xterm_widths.js pending
"""

from __future__ import annotations

import enum
from typing import Mapping


class ColumnOperation(enum.Enum):
    """An operation that runs while the cursor may be parked past the edge."""

    PRINT_WRAPPING = "print_wrapping"
    PRINT_WITHOUT_WRAP = "print_without_wrap"
    CARRIAGE_RETURN = "carriage_return"
    LINE_FEED = "line_feed"
    NEXT_LINE = "next_line"
    BACKSPACE = "backspace"
    HORIZONTAL_TAB = "horizontal_tab"
    CURSOR_RELATIVE = "cursor_relative"
    CURSOR_ROW_ABSOLUTE = "cursor_row_absolute"
    CURSOR_COLUMN_ABSOLUTE = "cursor_column_absolute"
    CURSOR_POSITION = "cursor_position"
    ERASE = "erase"
    SCROLL = "scroll"
    SET_AUTOWRAP = "set_autowrap"
    SET_SCROLL_REGION = "set_scroll_region"
    SOFT_RESET = "soft_reset"
    FULL_RESET = "full_reset"
    OTHER_SEQUENCE = "other_sequence"


class PendingWrapResolution(enum.Enum):
    """What an operation does to the parked column."""

    #: The parked position survives; a later glyph still wraps.
    PRESERVE = "preserve"
    #: The cursor is pulled back onto the last column, then the operation runs.
    CLAMP = "clamp"
    #: The operation assigns the column itself, so parking is moot.
    REPLACE = "replace"
    #: The glyph starts a new row.
    WRAP = "wrap"


#: The table. Every row measured against the bundled xterm; see
#: ``MEASURED_PROBE`` for the probe each row was read from.
PENDING_WRAP_RESOLUTION: Mapping[ColumnOperation, PendingWrapResolution] = {
    ColumnOperation.PRINT_WRAPPING: PendingWrapResolution.WRAP,
    ColumnOperation.PRINT_WITHOUT_WRAP: PendingWrapResolution.CLAMP,
    ColumnOperation.CARRIAGE_RETURN: PendingWrapResolution.REPLACE,
    ColumnOperation.LINE_FEED: PendingWrapResolution.CLAMP,
    ColumnOperation.NEXT_LINE: PendingWrapResolution.REPLACE,
    ColumnOperation.BACKSPACE: PendingWrapResolution.CLAMP,
    ColumnOperation.HORIZONTAL_TAB: PendingWrapResolution.PRESERVE,
    ColumnOperation.CURSOR_RELATIVE: PendingWrapResolution.CLAMP,
    ColumnOperation.CURSOR_ROW_ABSOLUTE: PendingWrapResolution.CLAMP,
    ColumnOperation.CURSOR_COLUMN_ABSOLUTE: PendingWrapResolution.REPLACE,
    ColumnOperation.CURSOR_POSITION: PendingWrapResolution.REPLACE,
    ColumnOperation.ERASE: PendingWrapResolution.PRESERVE,
    ColumnOperation.SCROLL: PendingWrapResolution.PRESERVE,
    ColumnOperation.SET_AUTOWRAP: PendingWrapResolution.PRESERVE,
    ColumnOperation.SET_SCROLL_REGION: PendingWrapResolution.REPLACE,
    ColumnOperation.SOFT_RESET: PendingWrapResolution.PRESERVE,
    ColumnOperation.FULL_RESET: PendingWrapResolution.REPLACE,
    ColumnOperation.OTHER_SEQUENCE: PendingWrapResolution.PRESERVE,
}

#: The measured probe each row was read from, in
#: ``tests/fixtures/xterm/measured_pending_wrap.json``. Rows without a probe of
#: their own are named here anyway so the coverage test can prove no row rests
#: on reasoning alone.
MEASURED_PROBE: Mapping[ColumnOperation, str] = {
    ColumnOperation.PRINT_WRAPPING: "pending_baseline_no_operation",
    ColumnOperation.PRINT_WITHOUT_WRAP: "pending_nowrap_baseline_no_operation",
    ColumnOperation.CARRIAGE_RETURN: "pending_carriage_return",
    ColumnOperation.LINE_FEED: "pending_line_feed",
    ColumnOperation.NEXT_LINE: "pending_next_line_c1",
    ColumnOperation.BACKSPACE: "pending_backspace",
    ColumnOperation.HORIZONTAL_TAB: "pending_horizontal_tab",
    ColumnOperation.CURSOR_RELATIVE: "pending_cursor_forward",
    ColumnOperation.CURSOR_ROW_ABSOLUTE: "pending_row_absolute",
    ColumnOperation.CURSOR_COLUMN_ABSOLUTE: "pending_column_absolute",
    ColumnOperation.CURSOR_POSITION: "pending_cursor_position",
    ColumnOperation.ERASE: "pending_erase_in_line_to_end",
    ColumnOperation.SCROLL: "pending_scroll_up",
    ColumnOperation.SET_AUTOWRAP: "pending_autowrap_off",
    ColumnOperation.SET_SCROLL_REGION: "pending_set_scroll_region",
    ColumnOperation.SOFT_RESET: "pending_soft_reset",
    ColumnOperation.FULL_RESET: "pending_full_reset",
    ColumnOperation.OTHER_SEQUENCE: "pending_select_graphic_rendition",
}


def is_parked(column: int, columns: int) -> bool:
    """Whether the cursor sits past the last column, awaiting a wrap."""
    return column >= columns


def resolve_parked_column(
    column: int, columns: int, operation: ColumnOperation
) -> int:
    """Return the column ``operation`` should start from.

    Only ``CLAMP`` changes anything here. ``REPLACE`` operations overwrite the
    column themselves, ``WRAP`` is the printing path's own business, and
    ``PRESERVE`` means the parked position is meant to survive — calling this
    for those is still correct, and keeps every handler reading from the table
    instead of deciding for itself.
    """
    if PENDING_WRAP_RESOLUTION[operation] is PendingWrapResolution.CLAMP:
        return min(column, columns - 1)
    return column
