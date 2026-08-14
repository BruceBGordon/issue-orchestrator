"""Agent journey: when a PR is merged, the user-facing timeline must
show a "PR merged" event.

Background
----------
Before this test landed, ``EventName.REVIEW_MERGED`` was defined in
the catalog, had an ``EventSpec`` (phase=reviewing/step=merged), was in
the view registry mapped to the user view with narrative "PR merged",
was in the issue-detail view models — yet **nothing in production
actually published it**. The closest emitter was
``_apply_reconcile_history_entry`` in ``control/action_applier.py``,
which emitted ``HISTORY_RECONCILED`` (an ops/debug-only record) when
the awaiting-merge reconciler discovered ``pr_state == "merged"``.

The result: a real session that successfully merged its PR ended with
the user-facing timeline silent on the merge — the entire downstream
rendering pipeline was prepared for the event, but the event never
arrived. Goldens didn't catch this because no scenario emitted the
event in the first place.

What this test pins
-------------------
The journey from "awaiting-merge reconciler detects a merged PR" to
"user reads `PR merged` on their dashboard timeline":

  - Driving ``ReconcileHistoryEntryAction(status="merged")`` through
    the action applier publishes both ``HISTORY_RECONCILED`` (record
    of work) AND ``REVIEW_MERGED`` (user-facing event).
  - Flowing those events through the production write path
    (``DefaultTimelineWriter`` → ``produce_external_records`` fan-out
    → ``TimelineStore``) and projecting via ``project_timeline``
    yields a ``review.merged`` event in the user view.
  - That event carries the canonical "PR merged" narrative, so a
    human or agent skimming the dashboard sees the merge at a glance.
  - A close-without-merge reconciliation does NOT produce a
    ``review.merged`` event in the user view (regression guard
    against false positives).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import ReconcileHistoryEntryAction
from issue_orchestrator.control.review_exchange_lifecycle import (
    ReviewExchangeCancellation,
)
from issue_orchestrator.control.session_history import SessionHistoryOwner
from issue_orchestrator.domain.models import SessionHistoryEntry
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.timeline_event_sink import TimelineEventSink
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.ports.event_sink import EventSink, TraceEvent
from issue_orchestrator.ports.timeline_store import TimelineRecord, TimelineStore
from issue_orchestrator.timeline import project_timeline


class _RecordingStore(TimelineStore):
    def __init__(self) -> None:
        self.records: list[TimelineRecord] = []

    def append(self, issue_number: int, record: TimelineRecord) -> None:  # noqa: ARG002
        self.records.append(record)

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:  # noqa: ARG002
        return list(self.records)

    def delete(self, issue_number: int) -> int:  # noqa: ARG002
        self.records.clear()
        return 0


class _FanInSink(EventSink):
    """Forward published events to BOTH a list (for direct-event
    assertions) and the timeline writer (for projection assertions).
    """

    def __init__(self, sinks: list[EventSink]) -> None:
        self._sinks = sinks

    def publish(self, event: TraceEvent) -> None:
        for sink in self._sinks:
            sink.publish(event)


class _ListSink(EventSink):
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


def _no_review_exchange_work(
    issue_number: int, _reason: str
) -> ReviewExchangeCancellation:
    return ReviewExchangeCancellation(issue_number, ())


def _build_applier_with_dual_sink(
    history_entries: list[SessionHistoryEntry],
    store: TimelineStore,
) -> tuple[ActionApplier, _ListSink]:
    """Wire an ActionApplier whose event sink fans out to both a
    capturing list and the production TimelineWriter pipeline."""
    list_sink = _ListSink()
    timeline_sink = TimelineEventSink(DefaultTimelineWriter(store))
    fan_in = _FanInSink([list_sink, timeline_sink])

    labels = MagicMock()
    labels.has_label.return_value = False
    sessions = MagicMock()
    sessions.exists.return_value = False
    fresh_issue_reader = MagicMock()
    fresh_issue_reader.read_issue_labels.return_value = []

    applier = ActionApplier(
        labels=labels,
        sessions=sessions,
        events=fan_in,
        repository_host=MagicMock(),
        worktree_manager=MagicMock(),
        fresh_issue_reader=fresh_issue_reader,
        reconcile=False,
        review_exchange_canceller=_no_review_exchange_work,
    )
    applier.history_owner = SessionHistoryOwner(history_entries)
    return applier, list_sink


class TestPrMergedTimelineJourney:
    """Agent journey: PR merged → user sees `PR merged` on the timeline."""

    def test_merged_status_surfaces_pr_merged_in_user_view(
        self, tmp_path: Path
    ) -> None:
        """The full journey from reconciliation to projection."""
        issue_number = 7070
        pr_number = 9090
        pr_url = "https://github.com/test/repo/pull/9090"

        entry = SessionHistoryEntry(
            issue_number=issue_number,
            title="Add feature X",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=12,
            pr_url=pr_url,
            status_reason="Recovered awaiting merge state on startup",
        )
        store = _RecordingStore()
        applier, list_sink = _build_applier_with_dual_sink([entry], store)

        action = ReconcileHistoryEntryAction(
            issue_number=issue_number,
            pr_number=pr_number,
            pr_url=pr_url,
            status="merged",
            source="pull_request",
            issue_key=str(issue_number),
            reason="PR merged; awaiting merge reconciled",
        )

        result = applier.apply(action)
        assert result.success

        # Direct-event assertion: REVIEW_MERGED was published with the
        # right canonical payload.
        merged_events = [
            evt for evt in list_sink.events
            if evt.name == EventName.REVIEW_MERGED.value
        ]
        assert len(merged_events) == 1, (
            "Expected exactly one REVIEW_MERGED event from a merged-status "
            f"reconciliation; saw {[e.name for e in list_sink.events]}"
        )

        # Projection-level assertion: the event lands in the projected
        # timeline as a `review.merged` event in the user view, with
        # narrative the user actually reads.
        records = store.read(issue_number)
        projected = project_timeline(records, issue_number=issue_number)

        merged_projected = [e for e in projected if e.event == "review.merged"]
        assert merged_projected, (
            "Expected a review.merged event in the projected timeline. "
            "The fan-out pipeline didn't produce one — the journey from "
            "REVIEW_MERGED publication to user-visible timeline event is "
            "broken. Projected events: "
            f"{[e.event for e in projected]}"
        )
        assert len(merged_projected) == 1
        merged_event = merged_projected[0]

        # User must actually see this event on the dashboard.
        assert merged_event.views and "user" in merged_event.views, (
            f"review.merged is not in the 'user' view (views={merged_event.views})"
        )

        # The narrative is what the user skim-reads. Without a populated
        # narrative, the dashboard renders a generic "review.merged" string
        # that doesn't tell the user anything actionable.
        assert merged_event.narrative, (
            "review.merged narrative is empty; the user sees a bare event "
            "name on the dashboard instead of 'PR merged'."
        )
        assert "merged" in merged_event.narrative.lower()

        # Phase / step / status come from the spec — they drive the
        # dashboard's grouping and "completed checkmark" rendering.
        assert merged_event.phase == "reviewing"
        assert merged_event.step == "merged"
        assert merged_event.status == "completed"

    def test_closed_without_merge_does_not_show_pr_merged(
        self, tmp_path: Path
    ) -> None:
        """Regression guard: PR closed without merge must NOT surface
        a `review.merged` event. The user expects "PR merged" to mean
        a real merge happened.
        """
        issue_number = 7071
        pr_number = 9091
        pr_url = "https://github.com/test/repo/pull/9091"

        entry = SessionHistoryEntry(
            issue_number=issue_number,
            title="Abandoned feature Y",
            agent_type="agent:backend",
            status="completed",
            runtime_minutes=8,
            pr_url=pr_url,
            status_reason="Recovered awaiting merge state on startup",
        )
        store = _RecordingStore()
        applier, list_sink = _build_applier_with_dual_sink([entry], store)

        action = ReconcileHistoryEntryAction(
            issue_number=issue_number,
            pr_number=pr_number,
            pr_url=pr_url,
            status="closed",
            source="pull_request",
            issue_key=str(issue_number),
            reason="PR closed without merge; awaiting merge reconciled",
        )

        result = applier.apply(action)
        assert result.success

        names = [evt.name for evt in list_sink.events]
        assert EventName.REVIEW_MERGED.value not in names, (
            "REVIEW_MERGED leaked from a closed-without-merge reconciliation; "
            f"events: {names}"
        )

        records = store.read(issue_number)
        projected = project_timeline(records, issue_number=issue_number)
        assert not [e for e in projected if e.event == "review.merged"], (
            "review.merged appeared in projected timeline despite a closed "
            "(not merged) reconciliation — false-positive merge surfaced "
            "to user."
        )
