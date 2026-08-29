"""Idle-detector accumulator and composer-state value objects (#7128)."""

from __future__ import annotations

from issue_orchestrator.domain.exchange_kill_evidence import (
    ComposerState,
    RoundIdleDetector,
    undetermined_composer_state,
)


def _detector(**overrides: object) -> RoundIdleDetector:
    kwargs: dict[str, object] = {
        "window_seconds": 120.0,
        "deadline_seconds": 600.0,
        "poll_interval_seconds": 0.1,
        "round_started_at": 100.0,
        "activity_since": 100.0,
        "recording_bytes": 0,
    }
    kwargs.update(overrides)
    return RoundIdleDetector(**kwargs)  # type: ignore[arg-type]


class TestRoundIdleDetectorCounters:
    def test_observe_accumulates_polls_and_drained_bytes(self) -> None:
        detector = _detector()

        detector.observe(101.0, drained=10, recording_bytes=10)
        detector.observe(102.0, drained=5, recording_bytes=15)

        assert detector.poll_iterations == 2
        assert detector.bytes_drained_total == 15
        assert detector.recording_bytes == 15

    def test_drained_bytes_reset_the_idle_clock(self) -> None:
        detector = _detector()

        detector.observe(150.0, drained=1, recording_bytes=0)

        assert detector.idle_for(160.0) == 10.0

    def test_recording_growth_alone_counts_as_activity(self) -> None:
        """A quiet drain but a growing recording still means the agent is alive."""
        detector = _detector()

        detector.observe(150.0, drained=0, recording_bytes=4096)

        assert detector.idle_for(151.0) == 1.0

    def test_silence_does_not_reset_the_idle_clock(self) -> None:
        detector = _detector()

        detector.observe(150.0, drained=0, recording_bytes=0)

        assert detector.idle_for(160.0) == 60.0

    def test_unreadable_recording_size_keeps_the_last_known_value(self) -> None:
        detector = _detector(recording_bytes=99)

        detector.observe(101.0, drained=0, recording_bytes=None)

        assert detector.recording_bytes == 99


class TestAcceptanceWindow:
    def test_window_is_exhausted_at_the_boundary(self) -> None:
        detector = _detector(window_seconds=30.0)

        assert not detector.acceptance_window_exhausted(129.9)
        assert detector.acceptance_window_exhausted(130.0)

    def test_no_window_never_exhausts(self) -> None:
        detector = _detector(window_seconds=None)

        assert not detector.acceptance_window_exhausted(1_000_000.0)


class TestIdleTrajectory:
    def test_snapshot_carries_window_config_and_final_counters(self) -> None:
        detector = _detector(window_seconds=45.0)
        detector.observe(101.0, drained=7, recording_bytes=7)

        trace = detector.snapshot(160.0)

        assert trace.window_seconds == 45.0
        assert trace.deadline_seconds == 600.0
        assert trace.poll_interval_seconds == 0.1
        assert trace.poll_iterations == 1
        assert trace.bytes_drained_total == 7
        assert trace.elapsed_seconds == 60.0
        assert trace.idle_for_seconds == 59.0
        assert trace.recording_bytes == 7

    def test_frozen_trajectory_shows_bytes_drained_stalling(self) -> None:
        """The forensic signal: drained bytes stop moving while time advances."""
        detector = _detector()
        detector.observe(101.0, drained=120, recording_bytes=120)
        detector.record_sample(130.0)
        detector.observe(160.0, drained=0, recording_bytes=120)

        trace = detector.snapshot(190.0)

        assert [sample.bytes_drained_total for sample in trace.samples] == [120, 120]
        assert [sample.idle_for_seconds for sample in trace.samples] == [29.0, 89.0]

    def test_sample_ring_drops_oldest_and_reports_the_loss(self) -> None:
        detector = _detector(max_samples=3)
        for tick in range(1, 6):
            detector.record_sample(100.0 + tick)

        trace = detector.snapshot(200.0)

        assert len(trace.samples) == 3
        assert trace.samples_dropped == 3
        # Newest points survive; the last one is the snapshot itself.
        assert trace.samples[-1].elapsed_seconds == 100.0

    def test_to_dict_is_json_shaped(self) -> None:
        detector = _detector()
        detector.observe(101.0, drained=3, recording_bytes=3)

        payload = detector.snapshot(120.0).to_dict()

        assert payload["bytes_drained_total"] == 3
        assert payload["samples"][0]["poll_iterations"] == 1


class TestUndeterminedVerdict:
    def test_carries_the_reason_it_could_not_look(self) -> None:
        verdict = undetermined_composer_state("recording is missing at /nope")

        assert verdict.state is ComposerState.UNDETERMINED
        assert verdict.matched_marker is None
        assert "/nope" in verdict.evidence_snippet
        assert verdict.prompt_marker_present is False
