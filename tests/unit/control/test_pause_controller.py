"""Tests for the pause owner, its provenance, and the half-open incident breaker.

These cover the two defects that motivated the module:

1. A pause that could not explain itself. Every path assigned a bare bool, so
   the reason, the actor, and the time were simply not recorded anywhere that
   survived the process.
2. A pause that never lifted. Three consecutive tick errors from a transient
   DNS drop halted a healthy engine for days, because nothing retried.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from issue_orchestrator.control.pause_controller import PauseController
from issue_orchestrator.domain.pause_state import (
    PauseActor,
    PauseReason,
    PauseState,
    PauseTransition,
)
from issue_orchestrator.events.catalog import EventName
from issue_orchestrator.events.context import EventContext
from issue_orchestrator.infra.pause_journal import JsonlPauseJournal

START = datetime(2026, 8, 17, 9, 37, 19, tzinfo=timezone.utc)


class CollectingEventSink:
    def __init__(self) -> None:
        self.events: list = []

    def publish(self, event) -> None:
        self.events.append(event)


class FakeClock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _controller(
    clock: FakeClock | None = None, journal=None
) -> tuple[PauseController, CollectingEventSink, FakeClock]:
    events = CollectingEventSink()
    the_clock = clock or FakeClock()
    controller = PauseController(
        events=events,
        event_context=EventContext(),
        journal=journal,
        clock=the_clock,
    )
    return controller, events, the_clock


class TestPauseStateInvariants:
    def test_paused_state_cannot_be_constructed_without_provenance(self) -> None:
        """The type refuses to represent the bug it exists to prevent."""
        with pytest.raises(ValueError, match="requires reason, actor, and since"):
            PauseState(paused=True)

    def test_running_state_cannot_carry_a_reason(self) -> None:
        with pytest.raises(ValueError, match="carries no reason"):
            PauseState(paused=False, reason=PauseReason.OPERATOR)

    def test_only_the_breaker_is_an_incident(self) -> None:
        assert PauseReason.LOOP_ERROR_THRESHOLD.is_incident is True
        for reason in (
            PauseReason.OPERATOR,
            PauseReason.STARTUP,
            PauseReason.TECH_LEAD_INVESTIGATION,
            PauseReason.TECH_LEAD_HEALTH_REVIEW,
        ):
            assert reason.is_incident is False

    def test_held_seconds_measures_the_pause(self) -> None:
        state = PauseState.paused_now(
            reason=PauseReason.OPERATOR, actor=PauseActor.WEB_API, now=START
        )
        assert state.held_seconds(START + timedelta(hours=2)) == pytest.approx(7200.0)
        assert PauseState.running().held_seconds(START) == 0.0


class TestPauseProvenance:
    def test_pause_records_why_who_and_when(self) -> None:
        controller, events, _ = _controller()
        state = controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD,
            actor=PauseActor.SYSTEM,
            detail="3 consecutive tick errors",
        )
        assert state.paused is True
        assert state.reason is PauseReason.LOOP_ERROR_THRESHOLD
        assert state.actor is PauseActor.SYSTEM
        assert state.since == START
        assert state.detail == "3 consecutive tick errors"

    def test_pause_event_payload_carries_the_reason(self) -> None:
        """The payload used to be ``{}`` — the UI could not say why."""
        controller, events, _ = _controller()
        controller.pause(reason=PauseReason.OPERATOR, actor=PauseActor.DASHBOARD)

        published = [e for e in events.events if e.name == EventName.ORCHESTRATOR_PAUSED]
        assert len(published) == 1
        data = published[0].data
        assert data["paused"] is True
        assert data["pause_reason"] == "operator"
        assert data["pause_actor"] == "dashboard"
        assert data["pause_is_incident"] is False
        assert data["paused_since"] == START.isoformat()

    def test_resume_event_reports_who_and_how_long(self) -> None:
        controller, events, clock = _controller()
        controller.pause(reason=PauseReason.OPERATOR, actor=PauseActor.WEB_API)
        clock.advance(90)
        controller.resume(actor=PauseActor.CONTROL_API)

        resumed = [e for e in events.events if e.name == EventName.ORCHESTRATOR_RESUMED]
        assert len(resumed) == 1
        assert resumed[0].data["resumed_by"] == "control_api"
        assert resumed[0].data["previous_pause_reason"] == "operator"
        assert resumed[0].data["paused_held_seconds"] == pytest.approx(90.0)

    def test_second_pause_preserves_the_first_cause(self) -> None:
        """An operator pause must not erase the incident that actually halted it."""
        controller, _, _ = _controller()
        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        controller.pause(reason=PauseReason.OPERATOR, actor=PauseActor.DASHBOARD)

        assert controller.state.reason is PauseReason.LOOP_ERROR_THRESHOLD
        assert controller.state.actor is PauseActor.SYSTEM

    def test_resume_while_running_is_a_no_op(self) -> None:
        controller, events, _ = _controller()
        controller.resume(actor=PauseActor.WEB_API)
        assert controller.paused is False
        assert events.events == []

    def test_describe_answers_the_question(self) -> None:
        controller, _, clock = _controller()
        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD,
            actor=PauseActor.SYSTEM,
            detail="GitHubAuthError: nodename nor servname provided",
        )
        clock.advance(3600)
        described = controller.describe()
        assert "loop_error_threshold" in described
        assert "system" in described
        assert "held=3600s" in described
        assert "nodename" in described


class TestHalfOpenIncidentBreaker:
    def test_incident_pause_expires_and_deliberate_pause_does_not(self) -> None:
        controller, _, clock = _controller()
        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        assert controller.due_for_auto_resume() is False
        clock.advance(61)
        assert controller.due_for_auto_resume() is True

        controller.resume(actor=PauseActor.SYSTEM)
        controller.pause(reason=PauseReason.OPERATOR, actor=PauseActor.WEB_API)
        clock.advance(7 * 24 * 3600)
        assert controller.due_for_auto_resume() is False

    def test_backoff_escalates_across_repeated_trips(self) -> None:
        """A genuinely broken engine backs off instead of hot-looping."""
        controller, _, clock = _controller()
        for expected in (60.0, 300.0, 900.0, 3600.0, 3600.0):
            controller.pause(
                reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
            )
            clock.advance(expected - 1)
            assert controller.due_for_auto_resume() is False, f"early at {expected}s"
            clock.advance(2)
            assert controller.due_for_auto_resume() is True, f"late at {expected}s"
            controller.resume(actor=PauseActor.SYSTEM)

    def test_healthy_tick_resets_the_ladder(self) -> None:
        controller, _, clock = _controller()
        for _ in range(3):
            controller.pause(
                reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
            )
            clock.advance(3600)
            controller.resume(actor=PauseActor.SYSTEM)

        controller.note_healthy_tick()

        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        clock.advance(61)
        assert controller.due_for_auto_resume() is True

    def test_note_healthy_tick_while_paused_does_not_reset(self) -> None:
        """A tick that runs during a pause is not evidence of recovery."""
        controller, _, clock = _controller()
        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        clock.advance(3600)
        controller.resume(actor=PauseActor.SYSTEM)
        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        controller.note_healthy_tick()
        clock.advance(61)
        # Still on the second rung (300s), not reset to the first.
        assert controller.due_for_auto_resume() is False


class TestPauseJournalDurability:
    def test_history_survives_a_new_controller(self, tmp_path: Path) -> None:
        """The gap that made past incidents unreconstructible after a restart."""
        path = tmp_path / "pause-journal.jsonl"
        controller, _, clock = _controller(journal=JsonlPauseJournal(path))
        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD,
            actor=PauseActor.SYSTEM,
            detail="DNS drop",
        )
        clock.advance(120)
        controller.resume(actor=PauseActor.WEB_API)

        # A brand-new process reading the same file.
        rows = JsonlPauseJournal(path).recent(10)
        assert [r.paused for r in rows] == [True, False]
        assert rows[0].reason is PauseReason.LOOP_ERROR_THRESHOLD
        assert rows[0].detail == "DNS drop"
        assert rows[1].previous_reason is PauseReason.LOOP_ERROR_THRESHOLD
        assert rows[1].actor is PauseActor.WEB_API
        assert rows[1].held_seconds == pytest.approx(120.0)

    def test_recent_transitions_reads_through_the_controller(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "pause-journal.jsonl"
        controller, _, _ = _controller(journal=JsonlPauseJournal(path))
        controller.pause(reason=PauseReason.OPERATOR, actor=PauseActor.CLI)
        assert [t.actor for t in controller.recent_transitions()] == [PauseActor.CLI]

    def test_unwritable_journal_never_blocks_a_pause(self, tmp_path: Path) -> None:
        """A pause is often a disk/network fault — the audit row must not add one."""
        blocked = tmp_path / "afile"
        blocked.write_text("not a directory")
        controller, _, _ = _controller(
            journal=JsonlPauseJournal(blocked / "nested" / "pause-journal.jsonl")
        )
        state = controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        assert state.paused is True

    def test_malformed_row_does_not_hide_the_rest(self, tmp_path: Path) -> None:
        path = tmp_path / "pause-journal.jsonl"
        good = PauseTransition(
            at=START, paused=True, reason=PauseReason.OPERATOR, actor=PauseActor.CLI
        )
        journal = JsonlPauseJournal(path)
        journal.record(good)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        journal.record(good)

        assert len(journal.recent(10)) == 2
