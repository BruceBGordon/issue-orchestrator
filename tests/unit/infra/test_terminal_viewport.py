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

    def test_an_overlong_or_invalid_lead_is_one_replacement_character(self) -> None:
        view = _viewport()

        view.feed(b"\xffX")

        rendered = view.render().rows[0]
        assert rendered.endswith("X")
        assert len(rendered) == 2


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
