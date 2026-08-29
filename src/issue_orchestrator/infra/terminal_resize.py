"""What a resize does to everything that is not the grid (#7141 round 9).

Resize is the one channel that reaches the screen from outside the parser, so
it never passed through the dispatch enumeration that classifies every other
state channel. It also moves more than the dimensions: measured against the
bundled terminal, a real change of size discharges a pending wrap, clamps the
saved cursor, and chooses *which* rows survive a row shrink.

The measured semantics, from ``node tools/measure_xterm_widths.js resize``:

===========================  =================================================
Same size                    Nothing happens at all — even a pending wrap
                             stays parked. Only a real change reconciles.
Pending wrap                 Discharged. The cursor keeps its column, so the
                             next glyph lands at the old edge rather than
                             wrapping to a fresh row.
Saved cursor                 Reconciled **at resize time**, permanently: the
                             row travels with any rows the shrink drops and is
                             then clamped, the column is clamped. Shrinking and
                             growing again does not recover the original
                             column.
Row shrink                   Rows are dropped from the top, but only as far as
                             it takes to keep the cursor visible.
Row grow                     Content stays at the top; the cursor does not
                             move.
Column shrink losing content The terminal rewraps the overflow onto new lines.
                             Refused — see below.
===========================  =================================================

Row growth is history-sensitive, which is the sharper half of the same
problem: growing a screen does not append blank rows, it *un-drops* whatever
fell off the top. A replay that appends blanks instead puts every live row on
the wrong line, so a later erase clears the wrong one — which is how a marker
the terminal had wiped survived into a trusted verdict (#7141 round 10).
Measuring it showed the class is wider than the report: ordinary scrolling
fills the scrollback too, not just a row shrink. So the owner carries one bit
of "the screen has already lost rows", and any later row growth while it is set
is refused. A full reset clears it, measured.

Column reflow is the other behaviour here that is not modelled. Reproducing it
needs line-continuation state this viewport does not carry, and the measured
probes disagree in ways a guess would get wrong. Real recordings make that
cheap to refuse: they carry at most one resize event, emitted before any output
exists, so nothing is ever reflowed in practice. A shrink that *would* lose
content therefore joins the refusal floor and makes the recording undetermined
rather than producing a verdict from a screen this model cannot reproduce.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .terminal_protocol import SavedCursor

__all__ = ["ResizePlan", "plan_resize"]


@dataclass(frozen=True, slots=True)
class ResizePlan:
    """The reconciliation a resize implies, decided before anything is moved.

    ``rows_dropped_from_top`` is how much of the old screen the row shrink
    discards to keep the cursor visible; ``refusals`` names every channel the
    resize would need a model this viewport does not have. Both can apply to a
    single event, so it is a tuple rather than one reason.
    """

    rows: int
    cols: int
    rows_dropped_from_top: int
    cursor_row: int
    cursor_col: int
    saved: SavedCursor
    refusals: tuple[str, ...]


def plan_resize(
    *,
    rows: int,
    cols: int,
    current_rows: int,
    current_cols: int,
    cursor_row: int,
    cursor_col: int,
    saved: SavedCursor,
    written_extents: Sequence[int],
    scrollback_dropped: bool,
) -> ResizePlan | None:
    """Decide the reconciliation, or ``None`` when the resize is a no-op.

    A same-size resize returns ``None`` because the terminal treats it as one:
    it does not even discharge a pending wrap.
    """
    if rows == current_rows and cols == current_cols:
        return None

    refusals: list[str] = []
    if cols < current_cols and any(extent > cols for extent in written_extents):
        refusals.append(f"resize {current_cols}->{cols} with content past the edge")
    if rows > current_rows and scrollback_dropped:
        refusals.append(f"resize {current_rows}->{rows} rows over dropped history")

    dropped = max(0, cursor_row - (rows - 1)) if rows < current_rows else 0
    return ResizePlan(
        rows=rows,
        cols=cols,
        rows_dropped_from_top=dropped,
        cursor_row=min(max(0, cursor_row - dropped), rows - 1),
        cursor_col=min(cursor_col, cols - 1),
        saved=SavedCursor(
            # The saved row rides along with the rows the shrink drops, and is
            # only then clamped — measured; clamping alone puts a later restore
            # on the wrong line.
            row=min(max(0, saved.row - dropped), rows - 1),
            column=min(saved.column, cols - 1),
        ),
        refusals=tuple(refusals),
    )
