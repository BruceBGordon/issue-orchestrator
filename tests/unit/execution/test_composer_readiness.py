"""Readiness is read off the drawn screen, not off byte activity.

Guards the #7104 fix. The decisions asserted here are the ones that decide
whether a turn is typed into a TUI that will take it, so they are stated as
screen contents in and a verdict out — no PTY, no real clock.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.execution.composer_readiness import (
    LiveComposerScreen,
    wait_for_ready_composer,
)


def _screen(*lines: str) -> LiveComposerScreen:
    screen = LiveComposerScreen(rows=40, cols=120)
    for line in lines:
        screen.feed(line.encode("utf-8") + b"\r\n")
    return screen


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_a_working_agent_is_not_ready_to_be_typed_at() -> None:
    """The exact codex footer from the stranded recording."""
    screen = _screen("• Working (8s • esc to interrupt)")

    assert screen.is_ready() is False
    assert screen.state() == (True, False)


def test_a_composer_holding_unsent_text_is_not_ready() -> None:
    """`tab to queue` means the LAST prompt never went in.

    Typing a second turn on top of a stranded one is how a round ends up
    reporting a provider failure for an orchestrator mistake.
    """
    screen = _screen("  Ctrl+J newline  •  tab to queue message  •  12% context left")

    assert screen.is_ready() is False
    assert screen.state() == (False, True)


def test_an_idle_looking_agent_that_never_worked_is_not_ready() -> None:
    """The banner race, observed live.

    Codex paints its model/directory banner before starting the turn its argv
    prompt gave it. That screen is drawn and shows no busy footer, so a
    snapshot calls it idle — we typed there, codex then began booting, and the
    Enter was dropped with the text left in the composer.
    """
    screen = _screen("›", "  ? for shortcuts  •  84% context left")

    assert screen.state() == (False, False)
    assert screen.seen_busy is False
    assert screen.is_ready() is False


def test_an_agent_that_worked_and_went_idle_is_ready() -> None:
    """The busy→idle transition is the thing being waited for."""
    screen = LiveComposerScreen(rows=40, cols=120)
    screen.feed("\x1b[2J\x1b[H\u2022 Working (2s \u2022 esc to interrupt)".encode("utf-8"))
    assert screen.is_ready() is False
    assert screen.seen_busy is True

    screen.feed(b"\x1b[2J\x1b[H  ? for shortcuts")
    assert screen.is_ready() is True


def test_an_undrawn_screen_is_not_ready() -> None:
    """The regression that made the first version of this gate a no-op.

    The bootstrap prompt is delivered by argv, so codex starts working the
    instant it launches and the first round is injected before the TUI has
    painted anything. Reporting ready there waved through every single spawn
    and the prompt stranded exactly as before the gate existed.
    """
    assert LiveComposerScreen(rows=40, cols=120).is_ready() is False
    assert LiveComposerScreen(rows=40, cols=120).has_drawn() is False


def test_a_silent_agent_is_released_by_the_grace_period_not_by_is_ready() -> None:
    """Non-TUI agents must not wait out the full backstop.

    The escape hatch is elapsed time, and it belongs to the waiter: an agent
    that draws nothing has no composer to strand in, but that is a different
    statement from "the screen shows it is idle".
    """
    clock = _Clock()
    screen = LiveComposerScreen(rows=40, cols=120)

    assert (
        wait_for_ready_composer(
            screen,
            pump=lambda: None,
            timeout_seconds=180.0,
            poll_interval_seconds=0.25,
            undrawn_grace_seconds=15.0,
            now=clock.now,
            sleep=clock.sleep,
        )
        is True
    )
    assert 15.0 <= clock.now() < 16.0, "grace period moved"


def test_a_drawn_busy_screen_does_not_get_the_grace_shortcut() -> None:
    """Having painted a busy footer is positive evidence, so it keeps waiting.

    Otherwise the grace period would become a 15-second bypass of the whole
    gate for exactly the agent it is meant to hold.
    """
    clock = _Clock()
    screen = LiveComposerScreen(rows=40, cols=120)
    screen.feed("\u2022 Working (1s \u2022 esc to interrupt)".encode("utf-8"))

    assert (
        wait_for_ready_composer(
            screen,
            pump=lambda: None,
            timeout_seconds=40.0,
            poll_interval_seconds=0.25,
            undrawn_grace_seconds=15.0,
            now=clock.now,
            sleep=clock.sleep,
        )
        is False
    )
    assert clock.now() >= 40.0, "a drawn busy screen took the grace shortcut"


def test_an_erased_busy_footer_does_not_keep_matching() -> None:
    """The screen, not the history.

    A footer that has been overwritten is not on screen and must not hold the
    agent busy forever — searching concatenated bytes instead of the rendered
    viewport is what produced a false verdict in #7141 round 1.
    """
    screen = LiveComposerScreen(rows=40, cols=120)
    screen.feed("\x1b[2J\x1b[H\u2022 Working (8s \u2022 esc to interrupt)".encode("utf-8"))
    assert screen.is_ready() is False

    screen.feed(b"\x1b[2J\x1b[H  ? for shortcuts")
    assert screen.is_ready() is True


def test_the_wait_returns_as_soon_as_the_agent_goes_idle() -> None:
    """Pumped output flips the verdict; the wait ends there, not at a timeout."""
    clock = _Clock()
    screen = LiveComposerScreen(rows=40, cols=120)
    screen.feed("\x1b[2J\x1b[H\u2022 Working (1s \u2022 esc to interrupt)".encode("utf-8"))
    pumps = {"n": 0}

    def _pump() -> None:
        pumps["n"] += 1
        if pumps["n"] == 4:
            screen.feed(b"\x1b[2J\x1b[H  ? for shortcuts")

    assert (
        wait_for_ready_composer(
            screen,
            pump=_pump,
            timeout_seconds=180.0,
            poll_interval_seconds=0.25,
            undrawn_grace_seconds=15.0,
            now=clock.now,
            sleep=clock.sleep,
        )
        is True
    )
    assert pumps["n"] == 4
    assert clock.now() < 1.0, "the wait outlived the event it was waiting on"


def test_the_wait_reports_failure_rather_than_blocking_forever() -> None:
    """An agent that never goes idle must end the wait with a verdict.

    False is not "do not send" — send_round still writes — it is what lets the
    warning name the cause at submission time instead of leaving a ten-minute
    timeout to be explained from a screen replay.
    """
    clock = _Clock()
    screen = LiveComposerScreen(rows=40, cols=120)
    screen.feed("\u2022 Working (1s \u2022 esc to interrupt)".encode("utf-8"))

    assert (
        wait_for_ready_composer(
            screen,
            pump=lambda: None,
            timeout_seconds=5.0,
            poll_interval_seconds=0.25,
            undrawn_grace_seconds=15.0,
            now=clock.now,
            sleep=clock.sleep,
        )
        is False
    )
    assert clock.now() >= 5.0


@pytest.mark.parametrize("marker", ["tab to queue message", "Enter to send"])
def test_holding_markers_are_matched_case_insensitively(marker: str) -> None:
    """Both surfaces normalise the same way, so casing cannot split them."""
    assert _screen(f"  {marker.upper()}  ").is_ready() is False
