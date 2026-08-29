"""Did the injected prompt ever leave the composer?

The composer-state discriminator for review-exchange kill evidence. After the
orchestrator writes a turn prompt into an agent's PTY, exactly one of two things
happened by the time the round died:

``composer_emptied``
    The composer took the text and the turn was submitted. The silence that
    followed is provider-side.

``composer_stranded``
    The text is still sitting in the composer, unsent — the injection/settle
    race of the PR #6484 family, and the signature behind #7104.

Telling them apart is a question about the **screen**, so this module reads the
rendered final viewport (``infra.terminal_viewport``) rather than the
concatenated byte history. Searching the history is what produced a false
stranded verdict in #7141 round 1: a footer that had already been erased was
still findable in the stream. It refuses to answer — ``UNDETERMINED``, never a
guess — whenever the recording cannot be reconstructed faithfully.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..domain.exchange_kill_evidence import (
    ComposerState,
    ComposerStateVerdict,
    undetermined_composer_state,
)
from ..infra.terminal_viewport import RecordingReplay, replay_terminal_recording
from .session_interactions import normalize_terminal_text

# Replay budget. Measured against a real 21 MB Claude reviewer recording: a
# 4 MiB trailing window replays in ~0.4s and converges to the same screen an
# 8 MiB window produces, because the TUI repaints its whole footer band every
# frame.
DEFAULT_REPLAY_BYTES = 4 * 1024 * 1024
_EVIDENCE_ROW_CHARS = 240


@dataclass(frozen=True)
class ComposerMarker:
    """One TUI affordance whose presence on the *current screen* implies state.

    Matched against :func:`normalize_terminal_text` output of a single rendered
    viewport row, so entries are lowercase with collapsed whitespace and no
    escape sequences.
    """

    name: str
    text: str
    state: ComposerState


# Heuristics, keyed to observed evidence and deliberately small.
#
# Precedence is **semantic, not positional**: a holding marker outranks an
# emptied marker when both are on screen. "tab to queue message" is direct
# evidence that the composer is holding unsent text, whereas "esc to interrupt"
# only says the agent is busy — and a stranded prompt sitting in the composer
# while the agent is busy shows *both*. Ranking them the other way is exactly
# how you would miss the #7104 signature.
COMPOSER_MARKERS: tuple[ComposerMarker, ...] = (
    # Claude-shaped TUIs show this only while the composer holds text the agent
    # has not taken. This is the footer visible in the 2026-08-28 recording
    # where the injected prompt never submitted.
    ComposerMarker(
        name="queue_message_footer",
        text="tab to queue message",
        state=ComposerState.COMPOSER_STRANDED,
    ),
    # Generic "you have typed something, press Enter" affordance.
    ComposerMarker(
        name="send_hint_footer",
        text="enter to send",
        state=ComposerState.COMPOSER_STRANDED,
    ),
    # The agent is working on a submitted turn.
    ComposerMarker(
        name="interrupt_footer",
        text="to interrupt",
        state=ComposerState.COMPOSER_EMPTIED,
    ),
    # Idle footer shown with an empty composer.
    ComposerMarker(
        name="shortcuts_footer",
        text="? for shortcuts",
        state=ComposerState.COMPOSER_EMPTIED,
    ),
)

_MARKER_PRECEDENCE: tuple[ComposerState, ...] = (
    ComposerState.COMPOSER_STRANDED,
    ComposerState.COMPOSER_EMPTIED,
)


def classify_composer_state(
    recording_path: Path,
    *,
    prompt_marker: str = "",
    replay_bytes: int = DEFAULT_REPLAY_BYTES,
    abort: Callable[[], bool] | None = None,
) -> ComposerStateVerdict:
    """Classify whether the injected prompt is still stranded in the composer.

    Reads the **rendered final viewport** (see ``infra.terminal_viewport``),
    never the concatenated byte history: an erased footer is not on the screen
    and must not be findable. Only rows the replay actually wrote are searched,
    so a trailing window cannot leak unknown history into a verdict.

    Returns ``UNDETERMINED`` — never a guess — when the recording is missing,
    structurally incomplete (still being appended to, or holding an unparseable
    row), renders no written rows, or shows no marker at all.

    ``abort`` lets a caller on a deadline stop the replay between events; an
    abandoned replay yields ``UNDETERMINED`` rather than a verdict read off a
    half-applied stream.

    ``prompt_marker`` is a short per-turn token (the round/attempt tag).
    Whether it is visible is recorded as supporting evidence only, never as the
    decision: a submitted prompt is also rendered into the transcript, and long
    text wraps.
    """
    if not recording_path.exists():
        return undetermined_composer_state(f"recording is missing at {recording_path}")
    replay = replay_terminal_recording(
        recording_path, max_bytes=replay_bytes, abort=abort
    )
    if replay.abandoned:
        return undetermined_composer_state(
            "the replay was abandoned before the recording was fully applied; "
            "refusing to classify a half-reconstructed screen"
        )
    if not replay.structurally_complete:
        return undetermined_composer_state(
            "recording is incomplete (still being appended to, or holding an "
            "unparseable row); refusing to classify a partial stream"
        )
    rows = tuple(
        normalized
        for normalized in (
            normalize_terminal_text(row) for row in replay.screen.written_rows
        )
        if normalized
    )
    if not rows:
        return undetermined_composer_state("viewport replay produced no written rows")
    return _verdict_from_rows(rows, replay=replay, prompt_marker=prompt_marker)


def _verdict_from_rows(
    rows: tuple[str, ...],
    *,
    replay: RecordingReplay,
    prompt_marker: str,
) -> ComposerStateVerdict:
    normalized_prompt = normalize_terminal_text(prompt_marker)
    echoed = bool(normalized_prompt) and any(normalized_prompt in row for row in rows)
    found = _highest_precedence_marker(rows)
    if found is None:
        return _verdict(
            ComposerState.UNDETERMINED,
            marker_name=None,
            snippet=rows[-1][:_EVIDENCE_ROW_CHARS],
            replay=replay,
            rows=rows,
            echoed=echoed,
        )
    marker, row = found
    return _verdict(
        marker.state,
        marker_name=marker.name,
        snippet=row[:_EVIDENCE_ROW_CHARS],
        replay=replay,
        rows=rows,
        echoed=echoed,
    )


def _highest_precedence_marker(
    rows: tuple[str, ...],
) -> tuple[ComposerMarker, str] | None:
    for state in _MARKER_PRECEDENCE:
        for marker in COMPOSER_MARKERS:
            if marker.state is not state:
                continue
            for row in rows:
                if marker.text in row:
                    return marker, row
    return None


def _verdict(
    state: ComposerState,
    *,
    marker_name: str | None,
    snippet: str,
    replay: RecordingReplay,
    rows: tuple[str, ...],
    echoed: bool,
) -> ComposerStateVerdict:
    return ComposerStateVerdict(
        state=state,
        matched_marker=marker_name,
        evidence_snippet=snippet,
        scanned_events=replay.events_applied,
        scanned_rows=len(rows),
        replayed_from_start=replay.replayed_from_start,
        prompt_marker_present=echoed,
    )


