"""The parked-cursor resolution table, one dedicated test per row (#7141 r7).

A cursor parked past the right edge is a promise that the next glyph starts a
new row, and each operation resolves, replaces or preserves that promise
differently. The lifecycle used to be implicit in an out-of-range integer with
each handler clamping on its own, which is where the round-6 backspace bug and
the round-7 reset bug both came from.

Every row below is measured against the bundled xterm — regenerate with
``node tools/measure_xterm_widths.js pending`` — and every row is exercised
twice: once as the table entry it declares, and once as the screen that entry
produces. Adding an operation without a row, or changing a row without changing
the terminal, fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.infra.pending_wrap import (
    MEASURED_PROBE,
    PENDING_WRAP_RESOLUTION,
    ColumnOperation,
    PendingWrapResolution,
    clears_parked_state,
    resolve_parked_column,
)
from issue_orchestrator.infra.terminal_viewport import TerminalViewport

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "xterm"
    / "measured_pending_wrap.json"
)


def _measured() -> dict[str, dict[str, object]]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


class TestTheTableIsComplete:
    """No operation may reach the viewport without a row, or without a measurement."""

    @pytest.mark.parametrize("operation", list(ColumnOperation))
    def test_every_operation_has_a_resolution(
        self, operation: ColumnOperation
    ) -> None:
        assert operation in PENDING_WRAP_RESOLUTION

    @pytest.mark.parametrize("operation", list(ColumnOperation))
    def test_every_operation_names_the_probe_it_was_measured_from(
        self, operation: ColumnOperation
    ) -> None:
        assert MEASURED_PROBE[operation] in _measured()

    def test_the_table_has_no_rows_for_operations_that_do_not_exist(self) -> None:
        assert set(PENDING_WRAP_RESOLUTION) == set(ColumnOperation)
        assert set(MEASURED_PROBE) == set(ColumnOperation)


class TestEachRowResolvesAsMeasured:
    """One dedicated assertion per row of the table."""

    @pytest.mark.parametrize(
        ("operation", "expected"),
        [
            (ColumnOperation.PRINT_WRAPPING, PendingWrapResolution.WRAP),
            (ColumnOperation.PRINT_WITHOUT_WRAP, PendingWrapResolution.CLAMP),
            (ColumnOperation.CARRIAGE_RETURN, PendingWrapResolution.REPLACE),
            (ColumnOperation.LINE_FEED, PendingWrapResolution.CLAMP),
            (ColumnOperation.NEXT_LINE, PendingWrapResolution.REPLACE),
            (ColumnOperation.BACKSPACE, PendingWrapResolution.CLAMP),
            (ColumnOperation.HORIZONTAL_TAB, PendingWrapResolution.PRESERVE),
            (ColumnOperation.CURSOR_RELATIVE, PendingWrapResolution.CLAMP),
            (ColumnOperation.CURSOR_ROW_ABSOLUTE, PendingWrapResolution.CLAMP),
            (ColumnOperation.CURSOR_COLUMN_ABSOLUTE, PendingWrapResolution.REPLACE),
            (ColumnOperation.CURSOR_POSITION, PendingWrapResolution.REPLACE),
            (ColumnOperation.ERASE, PendingWrapResolution.PRESERVE),
            (ColumnOperation.SCROLL, PendingWrapResolution.PRESERVE),
            (ColumnOperation.SET_AUTOWRAP, PendingWrapResolution.PRESERVE),
            (ColumnOperation.SET_SCROLL_REGION, PendingWrapResolution.REPLACE),
            (ColumnOperation.SOFT_RESET, PendingWrapResolution.PRESERVE),
            (ColumnOperation.FULL_RESET, PendingWrapResolution.REPLACE),
            (ColumnOperation.OTHER_SEQUENCE, PendingWrapResolution.PRESERVE),
        ],
    )
    def test_row(
        self, operation: ColumnOperation, expected: PendingWrapResolution
    ) -> None:
        assert PENDING_WRAP_RESOLUTION[operation] is expected

    @pytest.mark.parametrize("operation", list(ColumnOperation))
    def test_only_clamp_rows_move_a_parked_column(
        self, operation: ColumnOperation
    ) -> None:
        resolved = resolve_parked_column(10, 10, operation)
        clamps = PENDING_WRAP_RESOLUTION[operation] is PendingWrapResolution.CLAMP

        assert resolved == (9 if clamps else 10)

    @pytest.mark.parametrize("operation", list(ColumnOperation))
    def test_a_column_on_the_row_is_never_moved(
        self, operation: ColumnOperation
    ) -> None:
        assert resolve_parked_column(4, 10, operation) == 4


class TestParkedStateIsABit:
    """Parked-ness is state, not an out-of-range column (#7141 round 8)."""

    @pytest.mark.parametrize("operation", list(ColumnOperation))
    def test_clamp_and_replace_discharge_the_promise(
        self, operation: ColumnOperation
    ) -> None:
        resolution = PENDING_WRAP_RESOLUTION[operation]
        expected = resolution in (
            PendingWrapResolution.CLAMP,
            PendingWrapResolution.REPLACE,
        )

        assert clears_parked_state(operation) is expected

    def test_a_restored_column_is_not_parked(self) -> None:
        """DECRC puts the cursor back without the pending wrap."""
        view = TerminalViewport(rows=3, cols=10)

        view.feed(b"abcdefghij\x1b7\rX\x1b8Z")

        assert view.render().rows[0] == "XbcdefghiZ"
        assert view.render().cursor_col == 10


class TestEachRowMatchesTheBundledTerminal:
    """The screen each row produces, replayed from the measurement."""

    @pytest.mark.parametrize("probe", sorted(_measured()))
    def test_screen_matches(self, probe: str) -> None:
        expected = _measured()[probe]
        rows = expected["rows"]
        assert isinstance(rows, list)
        assert isinstance(expected["bytes"], list)
        view = TerminalViewport(rows=len(rows), cols=int(expected["cols"]))

        view.feed(bytes(expected["bytes"]))

        rendered = view.render()
        assert list(rendered.rows) == rows
        assert rendered.cursor_col == expected["cursorX"]
        assert rendered.cursor_row == expected["cursorY"]


class TestTheRowsThatBitUs:
    """Named regressions, so the reasons stay attached to the behaviour."""

    def test_backspace_from_parked_lands_two_columns_left(self) -> None:
        """Round 6: clamping and decrementing are two steps, not one."""
        view = TerminalViewport(rows=2, cols=10)

        view.feed(b"abcdefghij\bZ")

        assert view.render().rows[0] == "abcdefghZj"
        assert view.render().cursor_col == 9

    def test_a_tab_leaves_a_parked_cursor_alone(self) -> None:
        """The row that disagreed with the obvious implementation."""
        view = TerminalViewport(rows=3, cols=10)

        view.feed(b"abcdefghij\tZ")

        assert view.render().rows[:2] == ("abcdefghij", "Z")

    def test_a_full_reset_restores_autowrap(self) -> None:
        """Round 7: RIS put the screen back but left autowrap off."""
        view = TerminalViewport(rows=4, cols=10)

        view.feed(b"\x1b[?7l\x1bcabcdefghijXY")

        assert view.render().rows[:2] == ("abcdefghij", "XY")
        assert view.render().cursor_row == 1

    def test_a_soft_reset_restores_autowrap_without_clearing(self) -> None:
        view = TerminalViewport(rows=4, cols=10)

        view.feed(b"keep\x1b[?7l\x1b[!pZ")

        assert view.render().rows[0] == "keepZ"

    def test_a_soft_reset_leaves_a_parked_cursor_parked(self) -> None:
        view = TerminalViewport(rows=4, cols=10)

        view.feed(b"abcdefghij\x1b[!pZ")

        assert view.render().rows[:2] == ("abcdefghij", "Z")

    def test_a_full_reset_clears_the_parked_cursor(self) -> None:
        view = TerminalViewport(rows=4, cols=10)

        view.feed(b"abcdefghij\x1bcZ")

        assert view.render().rows[0] == "Z"
        assert view.render().cursor_col == 1

    def test_a_full_reset_restores_the_scroll_region(self) -> None:
        view = TerminalViewport(rows=4, cols=10)

        view.feed(b"\x1b[2;3r\x1bca\nb\nc\nd")

        assert view.render().rows == ("a", " b", "  c", "   d")


class TestResetDoesNotLaunderARefusal:
    """A reset restores the terminal, not the trustworthiness of the replay."""

    def test_a_full_reset_keeps_an_earlier_refusal(self) -> None:
        """Once a recording used a mode we cannot model, the screen is suspect.

        RIS puts the terminal back to defaults, but it cannot undo whatever the
        unmodelled mode already did to the rows this replay reconstructed.
        """
        view = TerminalViewport(rows=3, cols=10)

        view.feed(b"\x1b[?1049h\x1bctext")

        assert view.unmodelled_state == ["?1049h"]

    def test_a_soft_reset_keeps_an_earlier_refusal(self) -> None:
        view = TerminalViewport(rows=3, cols=10)

        view.feed(b"\x1b[?6h\x1b[!ptext")

        assert view.unmodelled_state == ["?6h"]
