"""Tests for the pause owner, its provenance, and the half-open incident breaker.

These cover the two defects that motivated the module:

1. A pause that could not explain itself. Every path assigned a bare bool, so
   the reason, the actor, and the time were simply not recorded anywhere that
   survived the process.
2. A pause that never lifted. Three consecutive tick errors from a transient
   DNS drop halted a healthy engine for days, because nothing retried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class FakeStore:
    """The one place the pause state lives — ``OrchestratorState``'s shape."""

    pause_state: PauseState = field(default_factory=PauseState.running)


def _controller(
    clock: FakeClock | None = None, journal=None, store: FakeStore | None = None
) -> tuple[PauseController, CollectingEventSink, FakeClock]:
    events = CollectingEventSink()
    the_clock = clock or FakeClock()
    controller = PauseController(
        events=events,
        event_context=EventContext(),
        store=store or FakeStore(),
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
        with pytest.raises(ValueError, match="carries no pause provenance"):
            PauseState(paused=False, reason=PauseReason.OPERATOR)

    @pytest.mark.parametrize(
        "field",
        [
            {"since": START},
            {"detail": "left over from an earlier pause"},
            {"actor": PauseActor.SYSTEM},
        ],
    )
    def test_running_state_rejects_every_paused_only_field(self, field) -> None:
        """The invariant is symmetric.

        Checking only reason/actor let ``PauseState(paused=False, since=...)``
        through, which serialized a running engine with ``paused_since``
        populated — a state that reads as "resumed, but paused since 09:37".
        """
        with pytest.raises(ValueError, match="carries no pause provenance"):
            PauseState(paused=False, **field)

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
        outcome = controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD,
            actor=PauseActor.SYSTEM,
            detail="3 consecutive tick errors",
        )
        assert outcome.committed is True
        state = outcome.state
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
        outcome = controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        assert outcome.state.paused is True

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


class TestTransitionOutcomeReportsWhatWasCommitted:
    """Review finding 6: a response must not claim a transition that never happened."""

    def test_duplicate_pause_reports_the_stored_actor_not_the_requester(self) -> None:
        controller, events, _ = _controller()
        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        outcome = controller.pause(
            reason=PauseReason.OPERATOR, actor=PauseActor.MCP
        )

        assert outcome.committed is False
        assert outcome.requested_actor is PauseActor.MCP
        # What is actually on record — the breaker, not the MCP caller.
        assert outcome.recorded_actor is PauseActor.SYSTEM
        assert outcome.recorded_reason is PauseReason.LOOP_ERROR_THRESHOLD
        # And no second event/journal row was produced.
        assert len([e for e in events.events
                    if e.name == EventName.ORCHESTRATOR_PAUSED]) == 1

    def test_resume_while_running_is_not_committed(self) -> None:
        controller, _, _ = _controller()
        outcome = controller.resume(actor=PauseActor.WEB_API)
        assert outcome.committed is False
        assert outcome.recorded_actor is None

    def test_committed_pause_reports_its_own_actor(self) -> None:
        controller, _, _ = _controller()
        outcome = controller.pause(
            reason=PauseReason.OPERATOR, actor=PauseActor.DASHBOARD
        )
        assert outcome.committed is True
        assert outcome.recorded_actor is PauseActor.DASHBOARD


class TestAutoResumeIsAtomic:
    """Review finding 5: the due-check and the transition must not be separable."""

    def test_resume_if_due_will_not_lift_a_deliberate_pause(self) -> None:
        """The exact interleaving: tick sees due -> operator re-pauses -> tick acts.

        With a split check/act the tick's stale decision resumed an engine a
        human had just deliberately stopped.
        """
        controller, _, clock = _controller()
        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        clock.advance(61)
        assert controller.due_for_auto_resume() is True  # the tick's observation

        # Another thread resumes, then deliberately pauses.
        controller.resume(actor=PauseActor.WEB_API)
        controller.pause(reason=PauseReason.OPERATOR, actor=PauseActor.WEB_API)

        # The tick now acts on its stale observation.
        outcome = controller.resume_if_due(actor=PauseActor.SYSTEM)

        assert outcome.committed is False
        assert controller.paused is True
        assert controller.state.reason is PauseReason.OPERATOR
        assert controller.state.actor is PauseActor.WEB_API

    def test_resume_if_due_commits_when_still_due(self) -> None:
        controller, _, clock = _controller()
        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        clock.advance(61)
        outcome = controller.resume_if_due(actor=PauseActor.SYSTEM)
        assert outcome.committed is True
        assert controller.paused is False

    def test_resume_if_due_is_a_no_op_before_the_backoff(self) -> None:
        controller, _, clock = _controller()
        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        clock.advance(30)
        assert controller.resume_if_due(actor=PauseActor.SYSTEM).committed is False
        assert controller.paused is True

    def test_concurrent_resume_if_due_commits_exactly_once(self) -> None:
        """Many threads racing the same due deadline must produce one transition."""
        import threading

        controller, events, clock = _controller()
        controller.pause(
            reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
        )
        clock.advance(61)

        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def attempt() -> None:
            barrier.wait()
            committed = controller.resume_if_due(actor=PauseActor.SYSTEM).committed
            with lock:
                results.append(committed)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1, results
        assert len([e for e in events.events
                    if e.name == EventName.ORCHESTRATOR_RESUMED]) == 1


class TestBreakerCountingIsOwnedPolicy:
    """The consecutive-error count decides WHEN a pause happens, so the pause
    owner holds it — exactly as the backoff ladder beside it decides when one
    lifts. It used to be loop bookkeeping on the engine, split from the policy
    it drives.
    """

    def test_breaker_trips_only_on_the_third_consecutive_failure(self) -> None:
        controller, _, _ = _controller()
        boom = RuntimeError("dns gone")

        assert controller.note_tick_failure(boom) is None
        assert controller.note_tick_failure(boom) is None
        assert controller.paused is False

        outcome = controller.note_tick_failure(boom)
        assert outcome is not None and outcome.committed is True
        assert controller.state.reason is PauseReason.LOOP_ERROR_THRESHOLD
        assert "dns gone" in controller.state.detail

    def test_a_healthy_tick_clears_the_budget(self) -> None:
        """Two failures then a success must not leave the engine one away."""
        controller, _, _ = _controller()
        boom = RuntimeError("blip")
        controller.note_tick_failure(boom)
        controller.note_tick_failure(boom)

        controller.note_healthy_tick()
        assert controller.consecutive_tick_errors == 0

        assert controller.note_tick_failure(boom) is None
        assert controller.paused is False

    def test_resume_restarts_the_error_budget(self) -> None:
        """After a resume the next trip must earn its own three failures."""
        controller, _, _ = _controller()
        boom = RuntimeError("blip")
        for _ in range(3):
            controller.note_tick_failure(boom)
        assert controller.paused is True

        controller.resume(actor=PauseActor.WEB_API)
        assert controller.consecutive_tick_errors == 0
        assert controller.note_tick_failure(boom) is None
        assert controller.paused is False

    def test_failures_while_paused_do_not_re_pause(self) -> None:
        controller, events, _ = _controller()
        boom = RuntimeError("blip")
        for _ in range(6):
            controller.note_tick_failure(boom)

        assert len([e for e in events.events
                    if e.name == EventName.ORCHESTRATOR_PAUSED]) == 1


class TestTransitionFanOutIsOrdered:
    """Findings 5 and 6: what a transition REPORTS and RECORDS must match it."""

    def test_a_committed_resume_never_reports_a_foreign_state(self) -> None:
        """The resume outcome used to re-read shared state after the lock.

        A pause landing in that window made the reply say "resumed,
        committed: true" while reporting the new pauser's actor and reason —
        a response claiming a transition that never happened, which is exactly
        what the outcome type exists to prevent.
        """
        import threading

        store = FakeStore()
        clock = FakeClock()
        journal_entered = threading.Event()
        release_journal = threading.Event()

        class SlowJournal:
            def record(self, transition) -> None:  # noqa: ANN001
                if not transition.paused:
                    journal_entered.set()
                    release_journal.wait(timeout=2.0)

            def recent(self, limit: int = 20) -> list:
                return []

        controller = PauseController(
            events=CollectingEventSink(),
            event_context=EventContext(),
            store=store,
            journal=SlowJournal(),
            clock=clock,
        )
        controller.pause(reason=PauseReason.OPERATOR, actor=PauseActor.WEB_API)

        outcome: list = []
        resumer = threading.Thread(
            target=lambda: outcome.append(controller.resume(actor=PauseActor.WEB_API))
        )
        resumer.start()
        assert journal_entered.wait(timeout=2.0)

        # A foreign pause lands while the resume is still announcing.
        intruder = threading.Thread(
            target=lambda: controller.pause(
                reason=PauseReason.OPERATOR, actor=PauseActor.DASHBOARD
            )
        )
        intruder.start()
        release_journal.set()
        resumer.join(timeout=3.0)
        intruder.join(timeout=3.0)

        assert outcome[0].committed is True
        # It reports the state IT committed, not whatever is current now.
        assert outcome[0].state.paused is False
        assert outcome[0].recorded_actor is None
        assert outcome[0].recorded_reason is None

    def test_journal_and_events_are_ordered_with_the_transitions(self) -> None:
        """A durable journal that records the transitions backwards is useless.

        The swap was serialized but the fan-out was not, so a resume landing
        inside a pause's journal write produced rows in reverse order — and an
        SSE stream whose last event said `paused` on a running engine.
        """
        import threading

        store = FakeStore()
        clock = FakeClock()
        rows: list = []
        pause_entered = threading.Event()
        release_pause = threading.Event()

        class BlockingJournal:
            def record(self, transition) -> None:  # noqa: ANN001
                if transition.paused:
                    pause_entered.set()
                    release_pause.wait(timeout=2.0)
                rows.append("paused" if transition.paused else "resumed")

            def recent(self, limit: int = 20) -> list:
                return []

        events = CollectingEventSink()
        controller = PauseController(
            events=events,
            event_context=EventContext(),
            store=store,
            journal=BlockingJournal(),
            clock=clock,
        )

        pauser = threading.Thread(
            target=lambda: controller.pause(
                reason=PauseReason.OPERATOR, actor=PauseActor.WEB_API
            )
        )
        pauser.start()
        assert pause_entered.wait(timeout=2.0)

        resumer = threading.Thread(
            target=lambda: controller.resume(actor=PauseActor.DASHBOARD)
        )
        resumer.start()
        release_pause.set()
        pauser.join(timeout=3.0)
        resumer.join(timeout=3.0)

        assert rows == ["paused", "resumed"], rows
        names = [str(e.name) for e in events.events]
        assert names == ["orchestrator.paused", "orchestrator.resumed"], names
        # And the last thing recorded agrees with reality.
        assert store.pause_state.paused is False
