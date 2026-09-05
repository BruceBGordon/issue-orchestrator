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
from dataclasses import dataclass
from enum import Enum

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


class ComposerReadinessReason(str, Enum):
    """Why the gate stopped waiting. Every outcome names its own cause."""

    #: The agent worked, finished, and left an empty composer.
    READY = "ready"
    #: Nothing ever looked busy within the grace period, so there is no turn
    #: to wait out — a plain stdin agent, or a TUI that never started one.
    NO_TUI_TURN = "no_tui_turn"
    #: Gave up while the agent was still working.
    STILL_BUSY = "still_busy"
    #: Gave up with unsent text sitting in the composer, which means an
    #: EARLIER prompt never went in.
    HOLDING_UNSENT = "holding_unsent"


@dataclass(frozen=True)
class ComposerReadiness:
    """What the gate decided, and the evidence it decided on."""

    ready: bool
    reason: ComposerReadinessReason
    waited_seconds: float
    busy: bool
    holding: bool

    def describe(self) -> str:
        """One log-ready sentence. The caller adds role and pid."""
        if self.reason is ComposerReadinessReason.READY:
            return f"composer ready after {self.waited_seconds:.1f}s"
        if self.reason is ComposerReadinessReason.NO_TUI_TURN:
            return (
                f"no TUI turn seen in {self.waited_seconds:.1f}s; treating the "
                "agent as having nothing to finish"
            )
        if self.reason is ComposerReadinessReason.HOLDING_UNSENT:
            return (
                f"composer still holds unsent text after "
                f"{self.waited_seconds:.1f}s — an earlier prompt never "
                "submitted; writing anyway and it may strand (#7104)"
            )
        return (
            f"agent still working after {self.waited_seconds:.1f}s; writing "
            "anyway and the prompt may strand in the composer (#7104)"
        )


@dataclass(frozen=True)
class ComposerGate:
    """Wait until a TUI will accept typing. The whole of that decision.

    Deliberately knows nothing about PTYs: it is handed a screen and a
    ``pump``, so every branch is reachable in a unit test with a fake clock
    and no subprocess. The bounds arithmetic lives in :meth:`for_round` for
    the same reason — it used to sit inline in ``send_round``, where the only
    way to exercise it was to spawn an agent.
    """

    screen: LiveComposerScreen
    max_wait_seconds: float
    undrawn_grace_seconds: float
    poll_interval_seconds: float = 0.25

    #: Ceilings for a real agent. A codex bootstrap turn was measured clearing
    #: its busy footer at 38.4s, so the wait must comfortably exceed that.
    DEFAULT_MAX_WAIT_SECONDS = 180.0
    DEFAULT_UNDRAWN_GRACE_SECONDS = 15.0

    @classmethod
    def for_round(
        cls,
        screen: LiveComposerScreen,
        *,
        round_timeout_seconds: float,
        poll_interval_seconds: float = 0.25,
    ) -> "ComposerGate":
        """Bound the wait by the round's budget as well as the ceilings.

        Getting ready must never eat the turn it is preparing for: a round
        given five seconds cannot spend fifteen deciding whether to start.
        Without this, the gate timed out 14 round-runner tests whose rounds
        are shorter than the grace period.
        """
        if round_timeout_seconds <= 0:
            raise ValueError("round_timeout_seconds must be positive")
        return cls(
            screen=screen,
            max_wait_seconds=min(
                cls.DEFAULT_MAX_WAIT_SECONDS, round_timeout_seconds * 0.5
            ),
            undrawn_grace_seconds=min(
                cls.DEFAULT_UNDRAWN_GRACE_SECONDS, round_timeout_seconds * 0.25
            ),
            poll_interval_seconds=poll_interval_seconds,
        )

    def await_ready(
        self,
        *,
        pump: Callable[[], None],
        now: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> ComposerReadiness:
        """Pump output until the screen says typing will be accepted.

        Never raises and never refuses to return: a caller that cannot type is
        usually still better off writing than failing the round outright, so
        the outcome is advice plus evidence rather than a veto.
        """
        started = now()
        deadline = started + self.max_wait_seconds
        grace_ends = started + self.undrawn_grace_seconds
        while True:
            pump()
            busy, holding = self.screen.sample()
            if self.screen.is_ready():
                return ComposerReadiness(
                    True, ComposerReadinessReason.READY, now() - started, busy, holding
                )
            if not self.screen.seen_busy and now() >= grace_ends:
                return ComposerReadiness(
                    True,
                    ComposerReadinessReason.NO_TUI_TURN,
                    now() - started,
                    busy,
                    holding,
                )
            if now() >= deadline:
                reason = (
                    ComposerReadinessReason.HOLDING_UNSENT
                    if holding
                    else ComposerReadinessReason.STILL_BUSY
                )
                return ComposerReadiness(
                    False, reason, now() - started, busy, holding
                )
            sleep(self.poll_interval_seconds)
