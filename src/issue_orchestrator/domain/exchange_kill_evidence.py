"""Value objects for review-exchange round-failure (kill) evidence.

Two independent facts describe *why* a persistent-PTY round died, and both
were previously trapped inside log strings:

1. **The idle-detector trace** — the window the detector was configured with,
   how many poll iterations it ran, and the ``bytes_drained`` trajectory it
   sampled at each heartbeat. ``RoundIdleDetector`` is the accumulator the
   round runner drives; ``RoundIdleTrace`` is the frozen snapshot it hands to
   the diagnostics recorder at declaration time.

2. **The composer state** — after the prompt was injected, did the agent's
   composer *empty* (submit registered, provider went silent) or is the
   injected text still *stranded* in it (injection/settle race, PR #6484
   family)? ``ComposerStateVerdict`` carries the classification plus the
   evidence snippet that produced it, so a human can check the machine's work.

This module is pure data and arithmetic — no filesystem, no decoding. The
recording decode and artifact writing live in
``execution/exchange_kill_evidence.py``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

# Heartbeat samples are a trajectory, not a full time series: enough points to
# see "bytes_drained froze at N and never moved" without letting a multi-hour
# round accumulate an unbounded list in a live orchestrator process.
DEFAULT_MAX_IDLE_SAMPLES = 64


class ComposerState(str, enum.Enum):
    """Where the injected prompt ended up when the round was declared dead."""

    #: The prompt text is still sitting in the agent's composer — the TUI is
    #: showing a "holds unsent input" affordance. The submit never registered.
    COMPOSER_STRANDED = "composer_stranded"
    #: The composer accepted the submit and emptied; the silence that followed
    #: is provider-side, not an injection race.
    COMPOSER_EMPTIED = "composer_emptied"
    #: Neither family of evidence was found in the scanned recording tail.
    UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class ComposerStateVerdict:
    """One composer-state classification plus the evidence behind it."""

    state: ComposerState
    matched_marker: str | None
    evidence_snippet: str
    scanned_events: int
    scanned_chars: int
    prompt_marker_present: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "matched_marker": self.matched_marker,
            "evidence_snippet": self.evidence_snippet,
            "scanned_events": self.scanned_events,
            "scanned_chars": self.scanned_chars,
            "prompt_marker_present": self.prompt_marker_present,
        }


def undetermined_composer_state(detail: str) -> ComposerStateVerdict:
    """Return the verdict for "we could not look" / "nothing matched"."""
    return ComposerStateVerdict(
        state=ComposerState.UNDETERMINED,
        matched_marker=None,
        evidence_snippet=detail,
        scanned_events=0,
        scanned_chars=0,
        prompt_marker_present=False,
    )


@dataclass(frozen=True)
class IdleSample:
    """One heartbeat observation of the round's liveness counters."""

    elapsed_seconds: float
    poll_iterations: int
    bytes_drained_total: int
    idle_for_seconds: float
    recording_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "elapsed_seconds": self.elapsed_seconds,
            "poll_iterations": self.poll_iterations,
            "bytes_drained_total": self.bytes_drained_total,
            "idle_for_seconds": self.idle_for_seconds,
            "recording_bytes": self.recording_bytes,
        }


@dataclass(frozen=True)
class RoundIdleTrace:
    """Frozen snapshot of the idle detector at round-failure declaration."""

    window_seconds: float | None
    deadline_seconds: float
    poll_interval_seconds: float
    elapsed_seconds: float
    idle_for_seconds: float
    poll_iterations: int
    bytes_drained_total: int
    recording_bytes: int | None
    samples: tuple[IdleSample, ...]
    samples_dropped: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_seconds": self.window_seconds,
            "deadline_seconds": self.deadline_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "elapsed_seconds": self.elapsed_seconds,
            "idle_for_seconds": self.idle_for_seconds,
            "poll_iterations": self.poll_iterations,
            "bytes_drained_total": self.bytes_drained_total,
            "recording_bytes": self.recording_bytes,
            "samples_dropped": self.samples_dropped,
            "samples": [sample.to_dict() for sample in self.samples],
        }


def _rounded(value: float) -> float:
    return round(value, 3)


@dataclass
class RoundIdleDetector:
    """Accumulates the round runner's liveness counters and idle verdict.

    Owns what used to be five loose locals in ``_wait_for_round_response``
    (poll counter, drained-byte total, last-activity clock, last recording
    size, heartbeat clock) plus the "has the agent been silent past the
    acceptance window" rule. The runner drives it once per poll; the
    diagnostics recorder reads :meth:`snapshot` at declaration time.

    ``deadline_seconds`` is the round's total budget and ``window_seconds`` the
    post-injection acceptance window (``None`` disables that check). Times are
    caller-supplied monotonic values so the runner's injected clock stays the
    single source of time.
    """

    window_seconds: float | None
    deadline_seconds: float
    poll_interval_seconds: float
    round_started_at: float
    activity_since: float
    recording_bytes: int | None = None
    max_samples: int = DEFAULT_MAX_IDLE_SAMPLES
    poll_iterations: int = field(default=0, init=False)
    bytes_drained_total: int = field(default=0, init=False)
    samples_dropped: int = field(default=0, init=False)
    _last_activity_at: float = field(init=False)
    _samples: list[IdleSample] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.max_samples < 1:
            raise ValueError("max_samples must be positive")
        self._last_activity_at = self.activity_since

    def observe(
        self,
        current: float,
        *,
        drained: int,
        recording_bytes: int | None,
    ) -> None:
        """Fold one poll iteration's observations into the detector."""
        self.poll_iterations += 1
        self.bytes_drained_total += drained
        grew = (
            self.recording_bytes is not None
            and recording_bytes is not None
            and recording_bytes > self.recording_bytes
        )
        if drained or grew:
            self._last_activity_at = current
        if recording_bytes is not None:
            self.recording_bytes = recording_bytes

    def idle_for(self, current: float) -> float:
        """Seconds since the last observed PTY/recording activity."""
        return current - self._last_activity_at

    def elapsed(self, current: float) -> float:
        """Seconds since the round started (prompt write included)."""
        return current - self.round_started_at

    def acceptance_window_exhausted(self, current: float) -> bool:
        """Whether silence has outlasted the prompt-acceptance window."""
        window = self.window_seconds
        return window is not None and self.idle_for(current) >= window

    def record_sample(self, current: float) -> IdleSample:
        """Append one trajectory point, evicting the oldest past the cap.

        The newest points matter most for forensics, so the ring drops from
        the front. ``samples_dropped`` keeps the snapshot honest about it.
        """
        sample = IdleSample(
            elapsed_seconds=_rounded(self.elapsed(current)),
            poll_iterations=self.poll_iterations,
            bytes_drained_total=self.bytes_drained_total,
            idle_for_seconds=_rounded(self.idle_for(current)),
            recording_bytes=self.recording_bytes,
        )
        self._samples.append(sample)
        while len(self._samples) > self.max_samples:
            self._samples.pop(0)
            self.samples_dropped += 1
        return sample

    def snapshot(self, current: float) -> RoundIdleTrace:
        """Freeze the detector's state, adding a final sample at ``current``."""
        self.record_sample(current)
        return RoundIdleTrace(
            window_seconds=self.window_seconds,
            deadline_seconds=self.deadline_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
            elapsed_seconds=_rounded(self.elapsed(current)),
            idle_for_seconds=_rounded(self.idle_for(current)),
            poll_iterations=self.poll_iterations,
            bytes_drained_total=self.bytes_drained_total,
            recording_bytes=self.recording_bytes,
            samples=tuple(self._samples),
            samples_dropped=self.samples_dropped,
        )
