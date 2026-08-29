"""The Python width model must equal the bundled xterm's, measured (#7141 r4).

The fixtures these tests replay were produced by running the vendored
``static/vendor/xterm/xterm.js`` headlessly — see ``tools/measure_xterm_widths.js``,
which regenerates them:

    node tools/measure_xterm_widths.js widths  > tests/fixtures/xterm/measured_advances.json
    node tools/measure_xterm_widths.js screens > tests/fixtures/xterm/measured_screens.json

Nothing here asserts what the model *ought* to say. Every expected value came
out of the terminal the session viewer actually runs, which is the only thing
that makes the discriminator's screen trustworthy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.infra.terminal_viewport import TerminalViewport
from issue_orchestrator.infra.xterm_widths import (
    EMPTY_CLUSTER,
    cluster_advance,
    wcwidth,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "xterm"


def _measured_advances() -> dict[int, int]:
    raw = json.loads((_FIXTURES / "measured_advances.json").read_text(encoding="utf-8"))
    return {int(codepoint): width for codepoint, width in raw.items()}


def _measured_screens() -> dict[str, dict[str, object]]:
    return json.loads((_FIXTURES / "measured_screens.json").read_text(encoding="utf-8"))


class TestAdvancesMatchTheBundledTerminal:
    def test_every_measured_codepoint_advances_identically(self) -> None:
        """The fixture measured each codepoint written after a plain "A"."""
        measured = _measured_advances()
        assert len(measured) > 1_500, "fixture looks truncated"
        after_a = cluster_advance(ord("A"), EMPTY_CLUSTER)[1]

        mismatches = {
            codepoint: (expected, cluster_advance(codepoint, after_a)[0])
            for codepoint, expected in measured.items()
            if cluster_advance(codepoint, after_a)[0] != expected
        }

        assert mismatches == {}

    def test_emoji_are_narrow_here_unlike_east_asian_width(self) -> None:
        """The disagreement that caused the bug, pinned so it cannot drift back."""
        import unicodedata

        assert unicodedata.east_asian_width("\U0001F468") == "W"
        assert wcwidth(0x1F468) == 1

    def test_cjk_and_hangul_are_wide(self) -> None:
        assert wcwidth(0x4E00) == 2
        assert wcwidth(0xAC00) == 2
        assert wcwidth(0xFF01) == 2

    def test_the_single_narrow_hole_in_the_wide_span(self) -> None:
        assert wcwidth(0x303E) == 2
        assert wcwidth(0x303F) == 1
        assert wcwidth(0x3040) == 2

    def test_only_planes_two_and_three_are_wide_outside_the_bmp(self) -> None:
        assert wcwidth(0x20000) == 2
        assert wcwidth(0x30000) == 2
        assert wcwidth(0x1F600) == 1
        assert wcwidth(0x100000) == 1


class TestClusterJoining:
    def test_a_zero_width_codepoint_joins_what_precedes_it(self) -> None:
        after_a = cluster_advance(ord("A"), EMPTY_CLUSTER)[1]

        advance, state = cluster_advance(0x0301, after_a)

        assert advance == 0
        assert state.should_join is True

    def test_a_zero_width_codepoint_with_nothing_to_join_takes_a_cell(self) -> None:
        """Measured: a lone combining mark after a cursor move occupies a cell."""
        advance, _ = cluster_advance(0x0301, EMPTY_CLUSTER)

        assert advance == 1

    def test_a_zwj_sequence_of_four_emoji_is_four_cells(self) -> None:
        """The reviewer's measurement: xterm gives the family width FOUR."""
        family = "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466"
        state = EMPTY_CLUSTER
        total = 0
        for char in family:
            advance, state = cluster_advance(ord(char), state)
            total += advance

        assert total == 4

    def test_a_joined_codepoint_never_widens_the_run(self) -> None:
        wide = cluster_advance(0x4E00, EMPTY_CLUSTER)[1]

        advance, state = cluster_advance(0x0301, wide)

        assert advance == 0
        assert state.width == 2


class TestViewportReproducesTheMeasuredScreens:
    """The trust contract: same bytes in, same screen out."""

    @pytest.mark.parametrize("probe", sorted(_measured_screens()))
    def test_screen_matches(self, probe: str) -> None:
        expected = _measured_screens()[probe]
        rows = expected["rows"]
        assert isinstance(rows, list)
        view = TerminalViewport(rows=len(rows), cols=int(expected["cols"]))

        view.feed(str(expected["text"]).encode("utf-8"))

        rendered = view.render()
        assert list(rendered.rows) == rows
        assert rendered.cursor_col == expected["cursorX"]
        assert rendered.cursor_row == expected["cursorY"]


class TestTableInvariants:
    """The membership test assumes sorted, disjoint spans; pin that."""

    @pytest.mark.parametrize(
        "name",
        ["_WIDE_BMP_RANGES", "_ZERO_WIDTH_BMP_RANGES", "_ZERO_WIDTH_NON_BMP_RANGES",
         "_WIDE_NON_BMP_RANGES"],
    )
    def test_ranges_are_sorted_and_disjoint(self, name: str) -> None:
        from issue_orchestrator.infra import xterm_widths

        ranges = getattr(xterm_widths, name)
        assert ranges, f"{name} is empty"
        previous_end = -1
        for start, end in ranges:
            assert start <= end, f"{name} has an inverted span {(start, end)}"
            assert start > previous_end, f"{name} is unsorted or overlapping at {start}"
            previous_end = end

    def test_the_table_covers_the_whole_bmp(self) -> None:
        from issue_orchestrator.infra.xterm_widths import _BMP_WIDTHS

        assert len(_BMP_WIDTHS) == 0x10000
        assert set(_BMP_WIDTHS) <= {0, 1, 2}

    def test_c0_and_c1_controls_are_zero_width(self) -> None:
        assert wcwidth(0x00) == 0
        assert wcwidth(0x1F) == 0
        assert wcwidth(0x7F) == 0
        assert wcwidth(0x9F) == 0
        assert wcwidth(0xA0) == 1


def _measured_controls() -> dict[str, dict[str, object]]:
    return json.loads(
        (_FIXTURES / "measured_c1_controls.json").read_text(encoding="utf-8")
    )


class TestC1ControlsMatchTheBundledTerminal:
    """#7141 round 5: C1s are controls to xterm, not text.

    Regenerate with ``node tools/measure_xterm_widths.js controls``. Each probe
    is a raw BYTE sequence, so nothing here depends on a string write path
    behaving the same as the byte path a recording actually replays.
    """

    @pytest.mark.parametrize("probe", sorted(_measured_controls()))
    def test_screen_matches(self, probe: str) -> None:
        expected = _measured_controls()[probe]
        rows = expected["rows"]
        assert isinstance(rows, list)
        assert isinstance(expected["bytes"], list)
        view = TerminalViewport(rows=len(rows), cols=int(expected["cols"]))

        view.feed(bytes(expected["bytes"]))

        rendered = view.render()
        assert list(rendered.rows) == rows
        assert rendered.cursor_col == expected["cursorX"]
        assert rendered.cursor_row == expected["cursorY"]

    def test_the_fixture_covers_every_c1(self) -> None:
        """An xterm upgrade must re-measure all 32, not a convenient subset."""
        probes = _measured_controls()
        for codepoint in range(0x80, 0xA0):
            tag = f"{codepoint:X}"
            for context in ("inline", "midscreen", "lastrow"):
                assert f"c1_{tag}_{context}" in probes

    def test_nel_breaks_the_line(self) -> None:
        view = TerminalViewport(rows=4, cols=40)

        view.feed("tab to \u0085queue message".encode("utf-8"))

        assert view.render().rows[:2] == ("tab to ", "queue message")

    def test_index_keeps_the_column_while_breaking_the_line(self) -> None:
        view = TerminalViewport(rows=4, cols=20)

        view.feed("AB\u0084CD".encode("utf-8"))

        assert view.render().rows[:2] == ("AB", "  CD")

    def test_the_c1_csi_introduces_a_sequence(self) -> None:
        view = TerminalViewport(rows=3, cols=20)

        view.feed("HELLO\u009b2J".encode("utf-8"))

        assert view.render().rows == ("", "", "")

    def test_the_inert_c1s_leave_no_mark(self) -> None:
        """24 of the 32 are consumed with no visible effect — U+008D included."""
        for codepoint in (0x80, 0x88, 0x8D, 0x97, 0x9C):
            view = TerminalViewport(rows=3, cols=20)

            view.feed(f"AB{chr(codepoint)}CD".encode("utf-8"))

            assert view.render().rows[0] == "ABCD", f"U+{codepoint:04X} left a mark"

    def test_vertical_tab_and_form_feed_index_a_line(self) -> None:
        """Measured alongside the C1 sweep: VT and FF are line feeds, not text."""
        for control in (0x0B, 0x0C):
            view = TerminalViewport(rows=4, cols=10)

            view.feed(b"ab" + bytes([control]) + b"X")

            assert view.render().rows[:2] == ("ab", "  X")
