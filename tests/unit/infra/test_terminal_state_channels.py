"""Every state channel the parser can reach is enumerated (#7141 round 8).

Rounds 6, 7 and 8 each found the same shape of bug: state the viewport did not
know was state. DECAWM, then the parked column and the reset scope, then the
saved cursor, the charset and the tab stops. Patching each one leaves the class
open, so the dispatch is now closed by construction: a byte the parser reaches
is either modelled, on a measured-inert allowlist, or refused — and a refusal
makes the recording untrustworthy rather than producing a verdict from a screen
this model cannot reproduce.

The exhaustiveness tests below are the induction step. They walk the whole
dispatch space rather than the sequences we happened to think of, so a new
handler cannot be added without landing in one of the three buckets.

Round 9 closed the two ways the enumeration was narrower than it claimed: the
walk covered bare CSI finals only, so a private-prefixed final that fell out of
the mode dispatcher was never exercised, and resize — the one channel that
reaches the screen from outside the parser entirely — had no measured family at
all. Both are classified here now, on the same three-way rule.

Fixtures come from ``node tools/measure_xterm_widths.js state`` and
``… resize``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.infra.terminal_replay import replay_terminal_recording
from issue_orchestrator.infra.terminal_protocol import (
    _DEVICE_QUERY_PREFIXES,
    _ESCAPE_CHARSET_DESIGNATORS,
    _IGNORED_PRIVATE_FINALS,
    _IGNORED_CSI_FINALS,
    _IGNORED_CSI_INTERMEDIATES,
    _IGNORED_ESCAPE_MARKERS,
    _IGNORED_PRIVATE_MODES,
)
from issue_orchestrator.infra.terminal_viewport import TerminalViewport
_ESCAPE_HANDLERS_KEYS = frozenset(TerminalViewport._ESCAPE_HANDLERS)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "xterm"
_FIXTURE = _FIXTURES / "measured_state.json"
_RESIZE_FIXTURE = _FIXTURES / "measured_resize.json"
_RESIZE_PROBES = json.loads(_RESIZE_FIXTURE.read_text())
#: Probes whose column shrink would reflow or discard written content. The
#: terminal rewraps there; this model refuses instead of guessing.
_REFUSED_RESIZE_PROBES = frozenset(
    {
        "resize_parked_then_shrink",
        "resize_both_dimensions",
        "resize_wide_glyph_across_shrink",
    }
)


def _replay_resize_probe(probe: dict[str, object]) -> TerminalViewport:
    view = TerminalViewport(rows=int(probe["rows"]), cols=int(probe["cols"]))
    for step in probe["steps"]:
        if "resize" in step:
            view.resize(rows=step["resize"]["rows"], cols=step["resize"]["cols"])
        else:
            view.feed(bytes(step["write"]))
    return view

#: Probes whose whole point is that the channel is refused, not reproduced.
REFUSED_PROBES = frozenset(
    {
        "state_insert_mode_shifts_the_row",
        "state_clear_all_tab_stops",
        "state_set_tab_stop",
        "state_line_drawing_charset",
        "state_shift_out_selects_g1",
    }
)


def _measured() -> dict[str, dict[str, object]]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _feed(payload: bytes, *, rows: int = 3, cols: int = 10) -> TerminalViewport:
    view = TerminalViewport(rows=rows, cols=cols)
    view.feed(payload)
    return view


class TestNoDispatchPathIsSilentlyDropped:
    """The induction: walk the dispatch space, not the sequences we thought of.

    The walk's bounds are the byte space itself. Round 9 found the previous
    version proved less than it claimed: it walked bare CSI finals only, so a
    private-prefixed final that fell through the mode dispatcher was invisible
    to it, and it started the escape walk at 0x30 so ``ESC #`` was never
    exercised. Both ranges now match what the parser can actually receive.
    """

    #: Every prefix a CSI body can open with, plus the bare case.
    CSI_PREFIXES = ("", "?", ">", "=", "<")
    #: Every final byte a CSI can end with.
    CSI_FINALS = [chr(byte) for byte in range(0x40, 0x7F)]
    #: Escape markers span the intermediates (0x20-0x2F) as well as 0x30-0x7E,
    #: which is where ``ESC #`` (DECALN fills the screen) lives.
    ESCAPE_MARKERS = [chr(byte) for byte in range(0x20, 0x7F)]

    @pytest.mark.parametrize("prefix", CSI_PREFIXES)
    @pytest.mark.parametrize("final", CSI_FINALS)
    def test_every_prefix_and_final_is_modelled_ignored_or_refused(
        self, prefix: str, final: str
    ) -> None:
        view = _feed(f"\x1b[{prefix}1{final}".encode("ascii"))

        modelled = not prefix and final in TerminalViewport._CSI_HANDLERS
        ignored = self._is_ignored(prefix, final)
        refused = bool(view.unmodelled_state)

        assert modelled or ignored or refused, (
            f"CSI {prefix}1{final} is silently dropped"
        )

    @staticmethod
    def _is_ignored(prefix: str, final: str) -> bool:
        if prefix in _DEVICE_QUERY_PREFIXES:
            return True
        if prefix == "?":
            return final in ("h", "l") or final in _IGNORED_PRIVATE_FINALS
        return final in _IGNORED_CSI_FINALS

    @pytest.mark.parametrize("final", CSI_FINALS)
    def test_a_private_final_that_is_not_a_mode_is_refused(self, final: str) -> None:
        """The hole round 9 found: only h/l and the measured queries may pass."""
        if final in ("h", "l") or final in _IGNORED_PRIVATE_FINALS:
            return
        view = _feed(f"\x1b[?2{final}".encode("ascii"))

        assert view.unmodelled_state, f"CSI ?2{final} slipped past the floor"

    @pytest.mark.parametrize("marker", ESCAPE_MARKERS)
    def test_every_escape_marker_is_modelled_ignored_or_refused(
        self, marker: str
    ) -> None:
        if marker in "[]P^_X":  # CSI and the string introducers have their own paths
            return
        view = _feed(f"\x1b{marker}B".encode("ascii"))

        modelled = (
            ord(marker) in _ESCAPE_HANDLERS_KEYS
            or ord(marker) in _ESCAPE_CHARSET_DESIGNATORS
        )
        ignored = ord(marker) in _IGNORED_ESCAPE_MARKERS
        refused = bool(view.unmodelled_state)

        assert modelled or ignored or refused, f"ESC {marker} is silently dropped"

    def test_decaln_is_refused(self) -> None:
        """``ESC # 8`` fills the screen with E's; very much grid-affecting."""
        assert _feed(b"\x1b#8").unmodelled_state == ["ESC #"]

    def test_decsed_is_refused(self) -> None:
        """``CSI ?2J`` clears the screen; it used to pass as a trusted no-op."""
        assert _feed(b"keep\x1b[?2J").unmodelled_state == ["CSI ?2J"]

    def test_decsel_is_refused(self) -> None:
        assert _feed(b"abc\x1b[?K").unmodelled_state == ["CSI ?K"]

    def test_the_keyboard_protocol_query_is_ignored(self) -> None:
        """Real recordings emit ``CSI ?u``; refusing it would cost live data."""
        assert _feed(b"ab\x1b[?ucd").unmodelled_state == []

    @pytest.mark.parametrize("mode", [1, 3, 6, 7, 25, 47, 69, 1049, 2026, 31337])
    def test_every_private_mode_is_modelled_ignored_or_refused(
        self, mode: int
    ) -> None:
        view = _feed(f"\x1b[?{mode}h".encode("ascii"))

        modelled = mode == 7
        ignored = mode in _IGNORED_PRIVATE_MODES
        refused = bool(view.unmodelled_state)

        assert modelled or ignored or refused

    @pytest.mark.parametrize("mode", [2, 4, 12, 20, 33])
    def test_setting_an_ansi_mode_is_refused(self, mode: int) -> None:
        """Insert mode shifts a row; none of these are modelled."""
        assert _feed(f"\x1b[{mode}h".encode("ascii")).unmodelled_state

    def test_resetting_insert_mode_is_allowed_as_the_default(self) -> None:
        """Measured inert: RM 4 selects the overwrite behaviour already modelled."""
        assert _feed(b"\x1b[4l").unmodelled_state == []

    @pytest.mark.parametrize("prefix", sorted(_DEVICE_QUERY_PREFIXES))
    def test_device_queries_are_ignored(self, prefix: str) -> None:
        assert _feed(f"\x1b[{prefix}0c".encode("ascii")).unmodelled_state == []

    @pytest.mark.parametrize("sequence", sorted(_IGNORED_CSI_INTERMEDIATES))
    def test_measured_inert_intermediates_are_ignored(self, sequence: str) -> None:
        assert _feed(f"\x1b[4{sequence}".encode("ascii")).unmodelled_state == []

    @pytest.mark.parametrize("intermediate", [chr(b) for b in range(0x20, 0x30)])
    def test_every_intermediate_is_modelled_ignored_or_refused(
        self, intermediate: str
    ) -> None:
        view = _feed(f"\x1b[1{intermediate}p".encode("ascii"))
        sequence = f"{intermediate}p"

        ignored = sequence in _IGNORED_CSI_INTERMEDIATES
        modelled = sequence == "!p"
        refused = bool(view.unmodelled_state)

        assert modelled or ignored or refused


class TestChannelDispositions:
    """One test per channel, naming what was decided and why."""

    def test_ascii_charset_designation_is_ignored(self) -> None:
        """The only designation real recordings emit, and it is the default."""
        assert _feed(b"\x1b(Bqqq").unmodelled_state == []
        assert _feed(b"\x1b(Bqqq").render().rows[0] == "qqq"

    def test_a_line_drawing_designation_is_refused(self) -> None:
        assert _feed(b"\x1b(0qqq").unmodelled_state == ["ESC (0"]

    def test_shift_in_is_ignored(self) -> None:
        assert _feed(b"ab\x0fcd").unmodelled_state == []

    def test_shift_out_needs_no_refusal_of_its_own(self) -> None:
        """G1 can only hold ASCII here, because designating anything else is refused."""
        assert _feed(b"ab\x0ecd").unmodelled_state == []
        assert _feed(b"\x1b)0ab\x0eqq").unmodelled_state == ["ESC )0"]

    def test_setting_a_tab_stop_is_refused(self) -> None:
        assert _feed(b"abc\x1bH").unmodelled_state == ["ESC H"]

    def test_clearing_tab_stops_is_refused(self) -> None:
        assert _feed(b"\x1b[3g").unmodelled_state == ["CSI 3g"]

    def test_keypad_modes_are_ignored(self) -> None:
        assert _feed(b"\x1b=ab\x1b>").unmodelled_state == []

    def test_a_stray_string_terminator_is_ignored(self) -> None:
        assert _feed(b"ab\x1b\\cd").unmodelled_state == []

    def test_an_unknown_escape_is_refused(self) -> None:
        assert _feed(b"\x1bZ").unmodelled_state == ["ESC Z"]


class TestResizeIsAMeasuredChannel:
    """Resize arrives outside the parser, so it gets the same treatment.

    Measured against the bundled terminal: a real change of dimensions
    discharges a pending wrap and clamps the saved cursor permanently (shrink
    then grow does not recover the original column), a same-size resize changes
    nothing at all, and a row shrink drops from the top only as far as it must
    to keep the cursor on screen. A column shrink that would lose written
    content rewraps, which this model does not reproduce — so it is refused.
    """

    @pytest.mark.parametrize("name", sorted(_RESIZE_PROBES))
    def test_replay_matches_the_measured_screen(self, name: str) -> None:
        probe = _RESIZE_PROBES[name]
        view = _replay_resize_probe(probe)

        if name in _REFUSED_RESIZE_PROBES:
            assert view.unmodelled_state, f"{name} should refuse, not guess"
            return

        rendered = view.render()
        assert view.unmodelled_state == []
        assert list(rendered.rows) == probe["screen"]
        assert (rendered.cursor_row, rendered.cursor_col) == (
            probe["cursorY"],
            probe["cursorX"],
        )

    def test_every_probe_is_classified(self) -> None:
        assert _REFUSED_RESIZE_PROBES <= set(_RESIZE_PROBES)

    def test_growing_discharges_a_pending_wrap(self) -> None:
        """Reviewer repro (a): parked at the old edge, then the screen widens."""
        view = TerminalViewport(rows=4, cols=10)
        view.feed(b"abcdefghij")
        view.resize(rows=4, cols=15)
        view.feed(b"Z")

        assert view.render().rows[0] == "abcdefghijZ"

    def test_a_same_size_resize_leaves_the_pending_wrap_standing(self) -> None:
        view = TerminalViewport(rows=4, cols=10)
        view.feed(b"abcdefghij")
        view.resize(rows=4, cols=10)
        view.feed(b"Z")

        assert list(view.render().rows[:2]) == ["abcdefghij", "Z"]

    def test_the_saved_cursor_is_clamped_at_resize_time_and_stays_clamped(
        self,
    ) -> None:
        """Reviewer repro (b): shrink then grow must not recover column 9."""
        view = TerminalViewport(rows=4, cols=10)
        view.feed(b"\x1b[10G\x1b7")
        view.resize(rows=4, cols=4)
        view.resize(rows=4, cols=10)
        view.feed(b"\x1b8Z")

        assert view.render().cursor_col == 4

    def test_the_saved_row_travels_with_the_rows_a_shrink_drops(self) -> None:
        """Clamping the saved row alone puts a later restore on the wrong line.

        Found by attacking this round's own fix: both obvious probes agreed
        with a clamp, so it took a case where shifting and clamping disagree —
        saved on row 3, cursor on row 4, shrunk to 3 rows — to show that the
        terminal shifts first and clamps second.
        """
        view = TerminalViewport(rows=5, cols=10)
        view.feed(b"a\r\nb\r\nc\r\nd\r\ne\x1b[4;1H\x1b7\x1b[5;1H")
        view.resize(rows=3, cols=10)
        view.feed(b"\x1b8Z")

        assert list(view.render().rows) == ["c", "Z", "e"]

    def test_a_row_shrink_keeps_the_cursor_visible(self) -> None:
        view = TerminalViewport(rows=4, cols=10)
        view.feed(b"a\r\nb\r\nc\r\nd")
        view.resize(rows=2, cols=10)
        view.feed(b"Z")

        assert list(view.render().rows) == ["c", "dZ"]

    def test_a_column_shrink_that_would_reflow_is_refused(self) -> None:
        view = TerminalViewport(rows=4, cols=10)
        view.feed(b"abcdefghij")
        view.resize(rows=4, cols=5)

        assert view.unmodelled_state == ["resize 10->5 with content past the edge"]

    def test_a_column_shrink_over_a_clean_screen_is_modelled(self) -> None:
        """The only shrink real recordings could produce: geometry before output."""
        view = TerminalViewport(rows=4, cols=120)
        view.resize(rows=4, cols=80)
        view.feed(b"hello")

        assert view.unmodelled_state == []
        assert view.render().rows[0] == "hello"


class TestSavedCursor:
    """DECSC/DECRC and the SCO pair, measured."""

    def test_decsc_saves_row_and_column(self) -> None:
        view = _feed(b"\x1b[3;5H\x1b7\x1b[1;1H\x1b8Z", rows=4)

        assert view.render().cursor_row == 2
        assert view.render().cursor_col == 5

    def test_decrc_without_decsc_restores_the_origin(self) -> None:
        view = _feed(b"\x1b[2;3H\x1b8Z", rows=4)

        assert view.render().rows[0] == "Z"

    def test_the_sco_pair_saves_and_restores_too(self) -> None:
        view = _feed(b"\x1b[3;5H\x1b[s\x1b[1;1H\x1b[uZ", rows=4)

        assert view.render().cursor_row == 2
        assert view.render().cursor_col == 5

    def test_a_full_reset_clears_the_saved_cursor(self) -> None:
        view = _feed(b"\x1b[3;5H\x1b7\x1bc\x1b8Z", rows=4)

        assert view.render().rows[0] == "Z"

    def test_a_soft_reset_clears_the_saved_cursor(self) -> None:
        """The round-7 reading of DECSTR missed this."""
        view = _feed(b"\x1b[3;5H\x1b7\x1b[!p\x1b8Z", rows=4)

        assert view.render().rows[0] == "Z"


class TestReverseIndex:
    """ESC M, which real recordings emit 343 times in one session."""

    def test_reverse_index_moves_up_a_row(self) -> None:
        view = _feed(b"r0\r\nr1\r\nr2\x1bMX", rows=5)

        assert view.render().rows[:3] == ("r0", "r1X", "r2")

    def test_reverse_index_at_the_top_scrolls_down(self) -> None:
        view = _feed(b"r0\r\nr1\x1b[1;1H\x1bMX", rows=4)

        assert view.render().rows[:3] == ("X", "r0", "r1")


class TestTheStateFixtureMatches:
    @pytest.mark.parametrize(
        "probe", sorted(set(_measured()) - REFUSED_PROBES)
    )
    def test_modelled_probe_matches(self, probe: str) -> None:
        expected = _measured()[probe]
        rows = expected["rows"]
        assert isinstance(rows, list)
        assert isinstance(expected["bytes"], list)
        view = TerminalViewport(rows=len(rows), cols=int(expected["cols"]))

        view.feed(bytes(expected["bytes"]))

        assert view.unmodelled_state == []
        rendered = view.render()
        assert list(rendered.rows) == rows
        assert rendered.cursor_col == expected["cursorX"]
        assert rendered.cursor_row == expected["cursorY"]

    @pytest.mark.parametrize("probe", sorted(REFUSED_PROBES))
    def test_refused_probe_is_refused(self, probe: str) -> None:
        expected = _measured()[probe]
        assert isinstance(expected["bytes"], list)
        view = TerminalViewport(rows=4, cols=int(expected["cols"]))

        view.feed(bytes(expected["bytes"]))

        assert view.unmodelled_state, f"{probe} must refuse, not guess"

    def test_a_refusal_reaches_the_replay(self, tmp_path: Path) -> None:
        import base64

        path = tmp_path / "r.jsonl"
        payload = base64.b64encode(b"\x1b[4hshifted").decode("ascii")
        path.write_text(
            json.dumps(
                {"schema_version": 1, "event_type": "output", "offset_ms": 0,
                 "data_b64": payload},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        assert replay_terminal_recording(path).unmodelled_state == ("CSI 4h",)
