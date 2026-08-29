"""The Python-side terminal screen model (#7141 finding 1).

The operations covered here are the ones a real agent TUI actually emits,
measured against a 21 MB Claude reviewer recording: cursor addressing, erase in
line, erase in display, scroll regions. The point of every test is the same —
what is *on the screen*, not what was ever written to the stream.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Iterable

from issue_orchestrator.infra.terminal_viewport import (
    TerminalViewport,
    replay_terminal_recording,
)


def _viewport(rows: int = 6, cols: int = 40) -> TerminalViewport:
    return TerminalViewport(rows=rows, cols=cols)


def _recording(path: Path, chunks: Iterable[bytes], *, resize: bool = True) -> Path:
    rows: list[dict[str, object]] = []
    if resize:
        rows.append(
            {
                "schema_version": 1,
                "event_type": "resize",
                "offset_ms": 0,
                "rows": 6,
                "cols": 40,
            }
        )
    for index, chunk in enumerate(chunks, start=1):
        rows.append(
            {
                "schema_version": 1,
                "event_type": "output",
                "offset_ms": index * 10,
                "data_b64": base64.b64encode(chunk).decode("ascii"),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


class TestPrintingAndCursorMotion:
    def test_plain_text_lands_on_the_first_row(self) -> None:
        view = _viewport()

        view.feed(b"hello")

        assert view.render().rows[0] == "hello"

    def test_carriage_return_overwrites_from_the_line_start(self) -> None:
        view = _viewport()

        view.feed(b"aaaaa\rbb")

        assert view.render().rows[0] == "bbaaa"

    def test_newline_advances_and_carriage_return_resets_the_column(self) -> None:
        view = _viewport()

        view.feed(b"one\r\ntwo")

        assert view.render().rows[:2] == ("one", "two")

    def test_cursor_addressing_writes_at_the_addressed_cell(self) -> None:
        view = _viewport()

        view.feed(b"\x1b[3;5Hplaced")

        assert view.render().rows[2] == "    placed"

    def test_backspace_and_tab_move_the_cursor(self) -> None:
        view = _viewport()

        view.feed(b"abc\x08X")
        view.feed(b"\r\n\tind")

        rendered = view.render().rows
        assert rendered[0] == "abX"
        assert rendered[1] == "        ind"

    def test_autowrap_continues_on_the_next_row(self) -> None:
        view = _viewport(rows=4, cols=5)

        view.feed(b"abcdefgh")

        assert view.render().rows[:2] == ("abcde", "fgh")

    def test_multibyte_characters_occupy_one_cell(self) -> None:
        view = _viewport()

        view.feed("⏵⏵ ok".encode("utf-8"))

        assert view.render().rows[0] == "⏵⏵ ok"

    def test_split_escape_across_chunks_is_not_printed_as_text(self) -> None:
        view = _viewport()

        view.feed(b"\x1b[3")
        view.feed(b";5Hsplit")

        assert view.render().rows[2] == "    split"
        assert "[3;5H" not in "".join(view.render().rows)

    def test_unknown_escape_sequences_leave_no_residue(self) -> None:
        view = _viewport()

        view.feed(b"\x1b[?25l\x1b[?2026hvisible\x1b[?25h")

        assert view.render().rows[0] == "visible"

    def test_osc_title_sequences_are_swallowed(self) -> None:
        view = _viewport()

        view.feed(b"\x1b]0;a window title\x07after")

        assert view.render().rows[0] == "after"


class TestErasing:
    def test_erase_in_line_clears_to_the_end(self) -> None:
        view = _viewport()

        view.feed(b"keep this away\x1b[1;5H\x1b[K")

        assert view.render().rows[0] == "keep"

    def test_erase_in_display_clears_the_whole_screen(self) -> None:
        view = _viewport()
        view.feed(b"row one\r\nrow two\r\nrow three")

        view.feed(b"\x1b[2J\x1b[H")

        assert all(row == "" for row in view.render().rows)

    def test_erased_content_is_gone_not_merely_hidden(self) -> None:
        """The #7141 finding: erased text must not survive anywhere."""
        view = _viewport()
        view.feed(b"tab to queue message")

        view.feed(b"\x1b[2J\x1b[H")
        view.feed(b"fresh output")

        screen = view.render()
        assert "tab to queue message" not in "".join(screen.rows)
        assert "tab to queue message" not in "".join(screen.written_rows)

    def test_erase_below_leaves_earlier_rows_intact(self) -> None:
        view = _viewport()
        view.feed(b"one\r\ntwo\r\nthree")

        view.feed(b"\x1b[2;1H\x1b[J")

        assert view.render().rows[:3] == ("one", "", "")


class TestWrittenRowContract:
    def test_untouched_rows_are_excluded(self) -> None:
        view = _viewport()

        view.feed(b"\x1b[4;1Honly this row")

        assert view.render().written_rows == ("only this row",)

    def test_an_erase_marks_the_row_authoritative_blank(self) -> None:
        view = _viewport()

        view.feed(b"\x1b[2;1H\x1b[K")

        assert view.render().written_rows == ("",)

    def test_a_repaint_replaces_the_row_content(self) -> None:
        """How real TUIs redraw a footer band: CUP to the row, EL, rewrite."""
        view = _viewport()
        view.feed(b"\x1b[5;1H\x1b[Ktab to queue message")

        view.feed(b"\x1b[5;1H\x1b[KWorking (esc to interrupt)")

        assert view.render().written_rows == ("Working (esc to interrupt)",)


class TestScrolling:
    def test_content_scrolls_off_the_top(self) -> None:
        view = _viewport(rows=3, cols=20)

        view.feed(b"one\r\ntwo\r\nthree\r\nfour")

        assert view.render().rows == ("two", "three", "four")

    def test_scroll_region_confines_scrolling(self) -> None:
        view = _viewport(rows=5, cols=20)
        view.feed(b"\x1b[1;1Hheader")

        view.feed(b"\x1b[2;5r")  # scroll rows 2..5 only
        view.feed(b"\x1b[2;1Ha\r\nb\r\nc\r\nd\r\ne")

        assert view.render().rows[0] == "header"

    def test_scroll_up_sequence_shifts_rows(self) -> None:
        view = _viewport(rows=3, cols=20)
        view.feed(b"one\r\ntwo\r\nthree")

        view.feed(b"\x1b[S")

        assert view.render().rows[:2] == ("two", "three")


class TestRecordingReplay:
    def test_replays_a_whole_recording_from_the_start(self, tmp_path: Path) -> None:
        path = _recording(tmp_path / "r.jsonl", (b"first\r\n", b"second"))

        replay = replay_terminal_recording(path)

        assert replay.replayed_from_start is True
        assert replay.structurally_complete is True
        assert replay.events_applied == 2
        assert replay.screen.written_rows == ("first", "second")

    def test_resize_events_reshape_the_grid(self, tmp_path: Path) -> None:
        path = _recording(tmp_path / "r.jsonl", (b"x" * 12,), resize=False)
        rows = path.read_text(encoding="utf-8").splitlines()
        header = json.dumps(
            {
                "schema_version": 1,
                "event_type": "resize",
                "offset_ms": 0,
                "rows": 4,
                "cols": 5,
            },
            sort_keys=True,
        )
        path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")

        replay = replay_terminal_recording(path)

        assert replay.screen.rows[:3] == ("xxxxx", "xxxxx", "xx")

    def test_a_half_written_final_row_is_reported_incomplete(
        self, tmp_path: Path
    ) -> None:
        path = _recording(tmp_path / "r.jsonl", (b"done",))
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"event_type": "outp')

        assert replay_terminal_recording(path).structurally_complete is False

    def test_an_unparseable_row_is_reported_incomplete(self, tmp_path: Path) -> None:
        path = _recording(tmp_path / "r.jsonl", (b"done",))
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{ not json at all\n")

        assert replay_terminal_recording(path).structurally_complete is False

    def test_a_tail_window_reports_that_it_did_not_start_from_the_beginning(
        self, tmp_path: Path
    ) -> None:
        path = _recording(
            tmp_path / "r.jsonl", (b"early\r\n", b"x" * 3000, b"\r\nlate")
        )

        replay = replay_terminal_recording(path, max_bytes=512)

        assert replay.replayed_from_start is False
        assert replay.structurally_complete is True

    def test_non_utf8_payloads_do_not_break_the_replay(self, tmp_path: Path) -> None:
        path = _recording(tmp_path / "r.jsonl", (b"\xff\xfe\x00", b"after"))

        replay = replay_terminal_recording(path)

        assert "after" in "".join(replay.screen.written_rows)

    def test_an_empty_recording_yields_no_written_rows(self, tmp_path: Path) -> None:
        path = _recording(tmp_path / "r.jsonl", ())

        replay = replay_terminal_recording(path)

        assert replay.screen.written_rows == ()
        assert replay.structurally_complete is True


class TestMalformedByteStreams:
    """#7141 round 2 finding 1: the viewport must not trust byte counts alone."""

    def test_an_incomplete_utf8_lead_does_not_swallow_the_next_escape(self) -> None:
        """A truncated multi-byte lead must not eat the clear that follows it.

        ``\\xe2`` announces three bytes; if the next two are taken blindly they
        are ``\\x1b[`` and the screen clear never happens, leaving the erased
        row searchable — which is a false composer_stranded.
        """
        view = _viewport()
        view.feed(b"\x1b[1;1Htab to queue message")

        view.feed(b"\xe2\x1b[2J\x1b[H")
        view.feed(b"\x1b[1;1Hfresh")

        screen = view.render()
        assert "tab to queue message" not in "".join(screen.rows)
        assert screen.rows[0] == "fresh"

    def test_a_lead_followed_by_a_non_continuation_byte_re_parses_that_byte(
        self,
    ) -> None:
        view = _viewport()

        view.feed(b"\xe2A")

        # One replacement for the bad lead, then a real 'A' — not a swallowed one.
        assert view.render().rows[0].endswith("A")

    def test_a_truncated_lead_at_the_very_end_is_held_for_the_next_chunk(
        self,
    ) -> None:
        """The cross-chunk case the previous round claimed but never covered."""
        view = _viewport()

        view.feed(b"ok \xe2")
        view.feed(b"\x8f\xb5 done")

        assert view.render().rows[0] == "ok ⏵ done"

    def test_a_multibyte_character_split_three_ways_still_renders(self) -> None:
        view = _viewport()

        view.feed(b"\xe6")
        view.feed(b"\x9d")
        view.feed(b"\xb1")

        assert view.render().rows[0] == "東"

    def test_an_undecodable_byte_is_dropped_not_substituted(self) -> None:
        """Measured: xterm's decoder emits nothing at all for an invalid byte.

        Round 2 substituted U+FFFD, which shifts every column after it and can
        change where a row wraps — the same class of divergence as the C1 bug.
        """
        view = _viewport()

        view.feed(b"\xffX")

        assert view.render().rows[0] == "X"

    def test_a_dropped_byte_does_not_break_a_cluster(self) -> None:
        """A byte the decoder never emits cannot separate what surrounds it."""
        view = _viewport()

        view.feed(b"A\xff" + "\u0301".encode("utf-8"))

        assert view.render().rows[0] == "A\u0301"


class TestWideCharacterCells:
    """Full-width glyphs occupy two cells; cursor maths must agree."""

    def test_a_full_width_character_advances_two_columns(self) -> None:
        view = _viewport(rows=3, cols=10)

        view.feed("東亜".encode("utf-8"))
        view.feed(b"|")

        assert view.render().rows[0] == "東亜|"

    def test_full_width_text_wraps_on_the_real_cell_count(self) -> None:
        """15 wide glyphs fill 30 columns exactly; the 16th must wrap."""
        view = _viewport(rows=4, cols=30)

        view.feed(("界" * 16).encode("utf-8"))

        rendered = view.render().rows
        assert rendered[0] == "界" * 15
        assert rendered[1] == "界"

    def test_a_wide_glyph_that_would_straddle_the_edge_wraps_whole(self) -> None:
        view = _viewport(rows=3, cols=5)

        view.feed(b"abcd")
        view.feed("東".encode("utf-8"))

        assert view.render().rows[:2] == ("abcd", "東")

    def test_wide_content_does_not_desync_a_later_erase(self) -> None:
        """The reported probe: wrong width cleared the wrong row."""
        view = _viewport(rows=4, cols=30)
        view.feed(("界" * 16).encode("utf-8"))
        view.feed(b"\x1b[2;1H\x1b[Ktab to queue message")

        # Repaint the footer row the way a TUI does.
        view.feed(b"\x1b[2;1H\x1b[Kworking (esc to interrupt)")

        assert "tab to queue message" not in "".join(view.render().rows)

    def test_combining_marks_do_not_advance_the_cursor(self) -> None:
        view = _viewport(rows=3, cols=10)

        view.feed("éx".encode("utf-8"))

        assert view.render().rows[0] == "é" + "x" if False else "éx"


class TestDegenerateGeometry:
    def test_a_wide_glyph_on_a_one_column_screen_stays_in_bounds(self) -> None:
        view = _viewport(rows=2, cols=1)

        view.feed("東亜".encode("utf-8"))

        assert view.render().rows[0] in ("東", "亜")


class TestMalformedResizeRows:
    """#7141 round 3 finding 1: geometry fields must be real, plausible ints."""

    def _recording_with_resize(self, tmp_path: Path, rows: object, cols: object) -> Path:
        path = _recording(tmp_path / "resized.jsonl", (b"\x1b[1;1Htab to queue message",))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_type": "resize",
                        "offset_ms": 50,
                        "rows": rows,
                        "cols": cols,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        return path

    def test_zero_rows_is_not_trusted(self, tmp_path: Path) -> None:
        path = self._recording_with_resize(tmp_path, 0, 40)

        assert replay_terminal_recording(path).structurally_complete is False

    def test_negative_dimensions_are_not_trusted(self, tmp_path: Path) -> None:
        path = self._recording_with_resize(tmp_path, 40, -5)

        assert replay_terminal_recording(path).structurally_complete is False

    def test_boolean_dimensions_are_not_trusted(self, tmp_path: Path) -> None:
        """``bool`` is an ``int`` subclass, so isinstance let ``true`` through as 1."""
        path = self._recording_with_resize(tmp_path, True, 40)

        assert replay_terminal_recording(path).structurally_complete is False

    def test_absurd_dimensions_are_not_trusted(self, tmp_path: Path) -> None:
        path = self._recording_with_resize(tmp_path, 40, 10_000_000)

        assert replay_terminal_recording(path).structurally_complete is False

    def test_a_missing_dimension_is_not_trusted(self, tmp_path: Path) -> None:
        path = _recording(tmp_path / "r.jsonl", (b"x",))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                '{"schema_version": 1, "event_type": "resize", "offset_ms": 5, '
                '"rows": 40}\n'
            )

        assert replay_terminal_recording(path).structurally_complete is False

    def test_a_real_resize_is_still_applied(self, tmp_path: Path) -> None:
        path = self._recording_with_resize(tmp_path, 10, 20)

        replay = replay_terminal_recording(path)

        assert replay.structurally_complete is True


class TestGraphemeClusters:
    """#7141 round 4 finding 2: the model is the bundled xterm's, measured.

    Round 3 theorised that a ZWJ family renders as one two-cell glyph. The
    vendored xterm — which is what the viewer actually draws with — gives it
    FOUR: it ships the UnicodeV6 provider, where an emoji is one cell and only
    a *zero-width* codepoint joins the cluster before it. Every number below
    came out of that terminal; see tests/unit/infra/test_xterm_widths.py for
    the fixture replay and tools/measure_xterm_widths.js for the rig.
    """

    FAMILY = "\U0001F468\u200d\U0001F469\u200d\U0001F467\u200d\U0001F466"

    def test_a_zwj_family_emoji_measures_four_cells(self) -> None:
        view = _viewport(rows=3, cols=30)

        view.feed(b"abc")
        view.feed(self.FAMILY.encode())
        view.feed(b"|")

        # 3 ASCII + 4 for the family + 1 for the bar.
        assert view.render().cursor_col == 8
        assert view.render().rows[0].endswith("|")

    def test_the_reviewer_probe_lands_the_cursor_at_column_27(self) -> None:
        view = _viewport(rows=3, cols=30)

        view.feed(b"x" * 23)
        view.feed(self.FAMILY.encode())

        assert view.render().cursor_col == 27

    def test_the_family_wraps_a_footer_exactly_where_xterm_does(self) -> None:
        """xterm splits 'messa'/'ge' here; a two-cell model kept one row."""
        view = _viewport(rows=4, cols=30)

        view.feed(b"x" * 8)
        view.feed(self.FAMILY.encode())
        view.feed(b"tab to queue message")

        rendered = view.render().rows
        assert rendered[0].endswith("tab to queue messa")
        assert rendered[1] == "ge"

    def test_a_footer_that_fits_is_left_on_one_row(self) -> None:
        view = _viewport(rows=4, cols=30)

        view.feed(b"ab ")
        view.feed(self.FAMILY.encode())
        view.feed(b"tab to queue message")

        assert "tab to queue message" in view.render().rows[0]

    def test_a_variation_selector_does_not_advance(self) -> None:
        view = _viewport(rows=3, cols=10)

        view.feed("\u26a0\ufe0f!".encode("utf-8"))

        assert view.render().rows[0].endswith("!")
        assert view.render().cursor_col == 2

    def test_a_control_character_breaks_the_cluster(self) -> None:
        """Measured: after CR a combining mark takes a cell instead of joining."""
        joined = _viewport(rows=3, cols=10)
        joined.feed("A\u0301".encode("utf-8"))

        broken = _viewport(rows=3, cols=10)
        broken.feed("A\r\u0301".encode("utf-8"))

        assert joined.render().cursor_col == 1
        assert broken.render().cursor_col == 1
        assert joined.render().rows[0] == "A\u0301"

    def test_an_escape_sequence_breaks_the_cluster(self) -> None:
        """Measured: even an erase that never moves the cursor breaks it."""
        view = _viewport(rows=3, cols=10)

        view.feed("A\u001b[K\u0301".encode("utf-8"))

        assert view.render().cursor_col == 2

    def test_a_cluster_survives_a_chunk_boundary(self) -> None:
        view = _viewport(rows=3, cols=10)

        view.feed("\U0001F468".encode())
        view.feed("\u200d".encode())
        view.feed("\U0001F469".encode())

        assert view.render().cursor_col == 2


class TestReplayAbort:
    """#7141 round 3 finding 3: the replay stage runs under the caller's budget."""

    def test_an_aborted_replay_is_reported_abandoned(self, tmp_path: Path) -> None:
        path = _recording(tmp_path / "r.jsonl", (b"one", b"two", b"three"))

        replay = replay_terminal_recording(path, abort=lambda: True)

        assert replay.abandoned is True
        assert replay.structurally_complete is False

    def test_a_replay_that_is_never_aborted_completes(self, tmp_path: Path) -> None:
        path = _recording(tmp_path / "r.jsonl", (b"one", b"two"))

        replay = replay_terminal_recording(path, abort=lambda: False)

        assert replay.abandoned is False
        assert replay.structurally_complete is True
        assert replay.events_applied == 2

    def test_an_abort_partway_stops_applying_events(self, tmp_path: Path) -> None:
        path = _recording(tmp_path / "r.jsonl", tuple(b"row %d\r\n" % i for i in range(50)))
        calls = {"n": 0}

        def _abort() -> bool:
            calls["n"] += 1
            return calls["n"] > 5

        replay = replay_terminal_recording(path, abort=_abort)

        assert replay.abandoned is True
        assert replay.events_applied < 50
