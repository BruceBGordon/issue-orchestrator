"""Readiness is read off the drawn screen, not off byte activity.

Guards the #7104 fix. Everything here runs with a fake clock and no PTY: the
gate is handed a screen and a ``pump``, so every branch — including the bounds
arithmetic and the message a caller logs — is reachable without spawning an
agent. That was the point of extracting ``ComposerGate`` from ``send_round``,
where the only way to exercise this logic was a live round.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.execution.composer_readiness import (
    ComposerGate,
    ComposerReadinessReason,
    LiveComposerScreen,
)

BUSY = "• Working (2s • esc to interrupt)"
IDLE = "  ? for shortcuts  •  84% context left"
HOLDING = "  Ctrl+J newline  •  tab to queue message  •  12% context left"


def _screen(*lines: str) -> LiveComposerScreen:
    screen = LiveComposerScreen(rows=40, cols=120)
    for line in lines:
        screen.feed(line.encode("utf-8") + b"\r\n")
    return screen


def _repaint(screen: LiveComposerScreen, line: str) -> None:
    """Clear and redraw, the way a TUI replaces its footer."""
    screen.feed(b"\x1b[2J\x1b[H" + line.encode("utf-8"))


class _Clock:
    """Moves only when the code under test spends time."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _gate(screen: LiveComposerScreen, **kwargs: float) -> ComposerGate:
    return ComposerGate(
        screen=screen,
        max_wait_seconds=kwargs.get("max_wait_seconds", 180.0),
        undrawn_grace_seconds=kwargs.get("undrawn_grace_seconds", 15.0),
        poll_interval_seconds=kwargs.get("poll_interval_seconds", 0.25),
    )


# -- the screen ---------------------------------------------------------


def test_a_working_agent_is_not_ready_to_be_typed_at() -> None:
    """The exact codex footer from the stranded recording."""
    screen = _screen(BUSY)

    assert screen.is_ready() is False
    assert screen.sample() == (True, False)


def test_a_composer_holding_unsent_text_is_not_ready() -> None:
    """`tab to queue` means the LAST prompt never went in.

    Typing a second turn on top of a stranded one is how a round ends up
    reporting a provider failure for an orchestrator mistake.
    """
    screen = _screen(HOLDING)

    assert screen.is_ready() is False
    assert screen.sample() == (False, True)


def test_an_idle_looking_agent_that_never_worked_is_not_ready() -> None:
    """The banner race, observed live.

    Codex paints its model/directory banner before starting the turn its argv
    prompt gave it. That screen is drawn and shows no busy footer, so a single
    frame calls it idle — we typed there, codex then began booting, and the
    Enter was dropped with the text left in the composer.
    """
    screen = _screen("› Ask Codex to do anything", IDLE)

    assert screen.sample() == (False, False)
    assert screen.seen_busy is False
    assert screen.is_ready() is False


def test_an_agent_that_worked_and_then_went_idle_is_ready() -> None:
    """The busy->idle transition is the thing being waited for."""
    screen = LiveComposerScreen(rows=40, cols=120)
    _repaint(screen, BUSY)
    assert screen.is_ready() is False
    assert screen.seen_busy is True

    _repaint(screen, IDLE)
    assert screen.is_ready() is True


def test_an_undrawn_screen_is_not_ready() -> None:
    screen = LiveComposerScreen(rows=40, cols=120)

    assert screen.has_drawn() is False
    assert screen.is_ready() is False


def test_an_erased_busy_footer_does_not_keep_matching() -> None:
    """The screen, not the history.

    Searching concatenated bytes instead of the rendered viewport is what
    produced a false verdict in #7141 round 1 — a footer that had already been
    erased was still findable.
    """
    screen = LiveComposerScreen(rows=40, cols=120)
    _repaint(screen, BUSY)
    assert screen.sample() == (True, False)

    _repaint(screen, IDLE)
    assert screen.sample() == (False, False)


# -- the gate -----------------------------------------------------------


def test_the_gate_returns_as_soon_as_the_agent_goes_idle() -> None:
    clock = _Clock()
    screen = LiveComposerScreen(rows=40, cols=120)
    _repaint(screen, BUSY)
    pumps = {"n": 0}

    def _pump() -> None:
        pumps["n"] += 1
        if pumps["n"] == 4:
            _repaint(screen, IDLE)

    outcome = _gate(screen).await_ready(pump=_pump, now=clock.now, sleep=clock.sleep)

    assert outcome.ready is True
    assert outcome.reason is ComposerReadinessReason.READY
    assert pumps["n"] == 4
    assert clock.now() < 1.0, "the wait outlived the event it was waiting on"


def test_a_silent_agent_is_released_by_the_grace_period() -> None:
    """Non-TUI agents must not wait out the full ceiling.

    An agent that never looks busy has no turn to finish, so there is nothing
    to strand in — but that is a different statement from "the screen shows it
    is idle", which is why it gets its own reason.
    """
    clock = _Clock()
    outcome = _gate(LiveComposerScreen(rows=40, cols=120)).await_ready(
        pump=lambda: None, now=clock.now, sleep=clock.sleep
    )

    assert outcome.ready is True
    assert outcome.reason is ComposerReadinessReason.NO_TUI_TURN
    assert 15.0 <= clock.now() < 16.0


def test_a_drawn_busy_screen_does_not_take_the_grace_shortcut() -> None:
    """Having painted a busy footer is positive evidence, so it keeps waiting.

    Otherwise the grace period becomes a 15-second bypass of the whole gate for
    exactly the agent it exists to hold.
    """
    clock = _Clock()
    screen = LiveComposerScreen(rows=40, cols=120)
    _repaint(screen, BUSY)

    outcome = _gate(screen, max_wait_seconds=40.0).await_ready(
        pump=lambda: None, now=clock.now, sleep=clock.sleep
    )

    assert outcome.ready is False
    assert outcome.reason is ComposerReadinessReason.STILL_BUSY
    assert clock.now() >= 40.0


def test_timing_out_on_unsent_text_reports_that_and_not_busy() -> None:
    """The two failures need different names: one is ours, one is the agent's.

    Unsent text means an EARLIER prompt never submitted, which is a different
    bug from an agent that is merely slow, and the log line has to say which.
    """
    clock = _Clock()
    screen = LiveComposerScreen(rows=40, cols=120)
    _repaint(screen, BUSY)
    _repaint(screen, HOLDING)

    outcome = _gate(screen, max_wait_seconds=10.0).await_ready(
        pump=lambda: None, now=clock.now, sleep=clock.sleep
    )

    assert outcome.ready is False
    assert outcome.reason is ComposerReadinessReason.HOLDING_UNSENT
    assert "earlier prompt never" in outcome.describe()
    assert "#7104" in outcome.describe()


def test_every_reason_describes_itself() -> None:
    """The caller logs `describe()` verbatim, so none may fall through blank."""
    for reason in ComposerReadinessReason:
        from issue_orchestrator.execution.composer_readiness import ComposerReadiness

        text = ComposerReadiness(
            ready=reason
            in {
                ComposerReadinessReason.READY,
                ComposerReadinessReason.NO_TUI_TURN,
            },
            reason=reason,
            waited_seconds=1.0,
            busy=False,
            holding=False,
        ).describe()
        assert text and text[0].islower(), reason


# -- the bounds ---------------------------------------------------------


def test_a_short_round_shrinks_the_wait_below_the_ceilings() -> None:
    """Getting ready must not eat the turn it is preparing for.

    A five-second round cannot spend fifteen deciding whether to start; that
    is what timed out 14 round-runner tests when the ceilings applied flat.
    """
    gate = ComposerGate.for_round(
        LiveComposerScreen(rows=40, cols=120), round_timeout_seconds=5.0
    )

    assert gate.max_wait_seconds == 2.5
    assert gate.undrawn_grace_seconds == 1.25


def test_a_long_round_is_capped_by_the_ceilings() -> None:
    """A ten-minute round does not license a five-minute readiness wait."""
    gate = ComposerGate.for_round(
        LiveComposerScreen(rows=40, cols=120), round_timeout_seconds=600.0
    )

    assert gate.max_wait_seconds == ComposerGate.DEFAULT_MAX_WAIT_SECONDS
    assert gate.undrawn_grace_seconds == ComposerGate.DEFAULT_UNDRAWN_GRACE_SECONDS


def test_the_ceiling_clears_the_measured_codex_bootstrap_turn() -> None:
    """A codex bootstrap turn was measured clearing its busy footer at 38.4s.

    Pinned so nobody trims the ceiling below the case it was sized for.
    """
    assert ComposerGate.DEFAULT_MAX_WAIT_SECONDS > 38.4


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_nonpositive_round_budget_is_refused(bad: float) -> None:
    """Fail loudly rather than compute a zero-length wait that always passes."""
    with pytest.raises(ValueError, match="round_timeout_seconds"):
        ComposerGate.for_round(
            LiveComposerScreen(rows=40, cols=120), round_timeout_seconds=bad
        )
