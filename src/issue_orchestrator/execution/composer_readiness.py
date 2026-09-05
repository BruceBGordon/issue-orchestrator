"""Is the agent ready to be typed at, right now?

The question ``send_round`` has to answer before it writes a turn into a live
TUI, and the one thing the old settle could not: a prompt typed while the agent
is mid-turn is not submitted by the Enter that follows, and strands in the
composer until the round times out ten minutes later (#7104).

Byte activity cannot answer it. Measured from a stranded codex 0.153.4 reviewer
recording — 6074 output frames over 600s, ``p50=0.104s max=0.42s``, not one gap
over a second — the TUI repaints at ~10Hz continuously, including long after
the agent had said it was idle and waiting. Any quiet window below that worst
case is satisfied by chance gaps; any window above it is never satisfied at
all. The screen knows what the byte stream cannot say: in that same recording
the busy footer stopped appearing 21 seconds before output did.

So readiness is read off the rendered viewport, using the vocabulary
``composer_state`` already established for the post-mortem verdict. The two
surfaces must agree on what a footer looks like, which is why both normalise
through ``normalize_terminal_text`` and match the same marker text.

This is the LIVE counterpart to ``composer_state.classify_composer_state``, and
it is a separate function on purpose: that one replays a recording FILE and
returns UNDETERMINED whenever the file is still being appended to — which,
during a live session, is always.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..infra.terminal_viewport import TerminalViewport
from .session_interactions import normalize_terminal_text

logger = logging.getLogger(__name__)

# The agent is mid-turn. Codex renders "Working (8s • esc to interrupt)";
# claude-shaped TUIs render their own "esc to interrupt". Matching the shared
# tail keeps one marker for both, exactly as COMPOSER_MARKERS does.
BUSY_MARKER = "to interrupt"

# The composer is holding text nobody has taken yet. Codex renders this while
# an injected prompt sits unsent — it is the #7104 signature itself, and seeing
# it BEFORE writing means the previous turn never went in.
HOLDING_MARKERS: tuple[str, ...] = ("tab to queue", "enter to send")


class LiveComposerScreen:
    """A viewport kept current by the session's own output stream.

    Fed from ``PersistentSession.output_observer``, so it costs one grid
    update per chunk the runner already reads — no second reader, no replay of
    a file that is still being written, and no history: a footer that has been
    erased is not on the screen and cannot be matched.
    """

    def __init__(self, *, rows: int, cols: int) -> None:
        self._viewport = TerminalViewport(rows=rows, cols=cols)
        self._fed = False
        self._seen_busy = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._fed = True
        try:
            self._viewport.feed(chunk)
        except Exception:  # noqa: BLE001
            # A readiness probe must never be able to kill a round. An
            # unmodelled sequence costs accuracy here, not the exchange.
            logger.debug("[composer] viewport rejected a chunk", exc_info=True)

    def rows(self) -> tuple[str, ...]:
        return self._viewport.render().written_rows

    def state(self) -> tuple[bool, bool]:
        """Return ``(busy, holding)`` as the current screen shows them."""
        busy = False
        holding = False
        for row in self.rows():
            text = normalize_terminal_text(row)
            if not text:
                continue
            if BUSY_MARKER in text:
                busy = True
            if any(marker in text for marker in HOLDING_MARKERS):
                holding = True
        return busy, holding

    def has_drawn(self) -> bool:
        """Whether the agent has painted anything at all yet."""
        return self._fed and bool(self.rows())

    @property
    def seen_busy(self) -> bool:
        """Whether this session was ever observed working.

        The bootstrap prompt is delivered by argv, so an agent that has drawn
        its banner but not yet started that turn looks exactly like one that
        has finished it. Only the busy→idle TRANSITION separates them, and a
        single snapshot cannot see a transition.
        """
        return self._seen_busy

    def sample(self) -> tuple[bool, bool]:
        """Read ``(busy, holding)`` and remember having seen busy."""
        busy, holding = self.state()
        if busy:
            self._seen_busy = True
        return busy, holding

    def is_ready(self) -> bool:
        """True once the agent has worked and then gone idle at an empty composer.

        Three conditions, and dropping any one of them reintroduces a failure
        that was observed live:

        - **drawn** — typing at a terminal that has painted nothing is the
          spawn race; reporting ready there made the first version of this
          gate a no-op.
        - **seen_busy** — an agent that has drawn its banner but not yet begun
          its argv turn reads as idle. Codex accepted the typed text into its
          composer and then started booting; the Enter was dropped and the
          turn stranded. This is the condition that catches it.
        - **not busy and not holding** — the agent has finished, and nothing
          is left unsent in the composer from a previous round.
        """
        if not self.has_drawn():
            return False
        busy, holding = self.sample()
        if not self._seen_busy:
            return False
        return not busy and not holding


def wait_for_ready_composer(
    screen: LiveComposerScreen,
    *,
    pump: Callable[[], None],
    timeout_seconds: float,
    poll_interval_seconds: float,
    undrawn_grace_seconds: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """Pump output until the screen says the agent will accept typing.

    ``pump`` drains pending PTY output into the screen; this function owns only
    the decision, so tests drive it with no PTY at all.

    ``undrawn_grace_seconds`` is the one concession to agents that are not
    TUIs at all: if nothing has been painted by then, there is no composer for
    a prompt to strand in and waiting longer would be a stall this probe
    invented. It is deliberately NOT the same as being ready — an agent that
    has drawn a busy footer keeps the full ``timeout_seconds``.

    Returns True once ready (or once the grace period settles an undrawn
    screen), False if ``timeout_seconds`` passes first. False does not mean
    "do not send" — the caller may still prefer a doomed write to a failed
    round — it means "say so", which is what turns a ten-minute mystery
    timeout into a line in the log at the moment it happens.
    """
    started = now()
    deadline = started + timeout_seconds
    grace_ends = started + undrawn_grace_seconds
    while True:
        pump()
        if screen.is_ready():
            return True
        if not screen.seen_busy and now() >= grace_ends:
            logger.debug(
                "[composer] agent never looked busy within %.1fs (drawn=%s); "
                "treating as an agent with no turn to finish and proceeding",
                undrawn_grace_seconds, screen.has_drawn(),
            )
            return True
        if now() >= deadline:
            return False
        sleep(poll_interval_seconds)
