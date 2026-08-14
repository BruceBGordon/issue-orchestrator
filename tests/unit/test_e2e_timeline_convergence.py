"""Tests for E2E timeline convergence — E2E events flowing through shared timeline infrastructure."""

import pytest

from issue_orchestrator.domain.timeline_key import TimelineKey
from issue_orchestrator.infra.e2e_db import nest_orchestrator_events
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.timeline import (
    TimelineStream,
    _e2e_phase,
    _e2e_status,
    _e2e_level,
    _e2e_summary,
    _phase_for_event,
    _step_for_event,
    _status_for_event,
    _level_for_event,
    _parent_key,
)


def _patch_closing_timeline_store(monkeypatch, reads):
    from issue_orchestrator.execution import timeline_store as timeline_store_module

    planned_reads = list(reads)
    instances = []

    class ClosingTimelineStore:
        def __init__(self, db_path, config=None):
            self.db_path = db_path
            self.closed = False
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.closed = True

        def close(self):
            self.closed = True

        def read(self, issue_number, limit=None):
            assert planned_reads, "Unexpected timeline read"
            result = planned_reads.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

    monkeypatch.setattr(timeline_store_module, "SqliteTimelineStore", ClosingTimelineStore)
    return instances


class TestE2EPhaseDerivation:
    """E2E events map to domain-specific phases through the shared pipeline."""

    def test_run_started_is_setup(self):
        assert _phase_for_event("e2e.run_started") == "setup"

    def test_tests_collected_is_setup(self):
        assert _phase_for_event("e2e.tests_collected") == "setup"

    def test_test_started_is_execution(self):
        assert _phase_for_event("e2e.test_started") == "execution"

    def test_test_completed_is_execution(self):
        assert _phase_for_event("e2e.test_completed") == "execution"

    def test_retry_started_is_retry(self):
        assert _phase_for_event("e2e.retry_started") == "retry"

    def test_run_finished_is_teardown(self):
        assert _phase_for_event("e2e.run_finished") == "teardown"

    def test_run_canceled_is_teardown(self):
        assert _phase_for_event("e2e.run_canceled") == "teardown"

    def test_run_error_is_teardown(self):
        assert _phase_for_event("e2e.run_error") == "teardown"


class TestE2EStepDerivation:
    def test_strips_e2e_prefix(self):
        assert _step_for_event("e2e.run_started") == "run_started"
        assert _step_for_event("e2e.test_completed") == "test_completed"


class TestE2EStatusDerivation:
    def test_run_error_is_error(self):
        assert _status_for_event("e2e.run_error") == "error"

    def test_run_canceled_is_error(self):
        assert _status_for_event("e2e.run_canceled") == "error"

    def test_test_completed_passed(self):
        assert _status_for_event("e2e.test_completed", {"outcome": "passed"}) == "completed"

    def test_test_completed_failed(self):
        assert _status_for_event("e2e.test_completed", {"outcome": "failed"}) == "error"

    def test_test_completed_skipped(self):
        assert _status_for_event("e2e.test_completed", {"outcome": "skipped"}) == "skipped"

    def test_run_finished_is_completed(self):
        assert _status_for_event("e2e.run_finished") == "completed"

    def test_test_started_is_active(self):
        assert _status_for_event("e2e.test_started") == "active"


class TestE2ELevelDerivation:
    def test_run_error_is_error_level(self):
        assert _level_for_event("e2e.run_error") == "error"

    def test_run_canceled_is_warning(self):
        assert _level_for_event("e2e.run_canceled") == "warning"

    def test_test_completed_is_detail(self):
        assert _level_for_event("e2e.test_completed") == "detail"

    def test_run_started_is_info(self):
        assert _level_for_event("e2e.run_started") == "info"


class TestE2EParentKey:
    def test_negative_issue_number_produces_e2e_parent_key(self):
        assert _parent_key(-42, {}) == "e2e-run-42"

    def test_positive_issue_number_unchanged(self):
        assert _parent_key(123, {}) == "issue:123"


class TestE2ETimelineStreamRoundtrip:
    """E2E events stored as TimelineRecords can be read back via TimelineStream."""

    def _make_record(self, event_name: str, data: dict | None = None) -> TimelineRecord:
        return TimelineRecord(
            event_id="evt1",
            timestamp="2026-01-01T00:00:00Z",
            event=event_name,
            data=data or {},
            source_event=event_name,
        )

    def test_e2e_run_started_roundtrip(self):
        record = self._make_record("e2e.run_started", {"branch": "main"})
        stream = TimelineStream.from_records(-42, [record])

        assert len(stream.events) == 1
        event = stream.events[0]
        assert event.phase == "setup"
        assert event.step == "run_started"
        assert event.status == "active"
        assert event.level == "info"
        assert event.parent_key == "e2e-run-42"
        assert "main" in (event.summary or "")

    def test_e2e_test_completed_passed(self):
        record = self._make_record("e2e.test_completed", {
            "nodeid": "tests/e2e/test_foo.py::test_bar",
            "outcome": "passed",
            "duration_seconds": 2.5,
        })
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.phase == "execution"
        assert event.status == "completed"
        assert "passed" in (event.summary or "")
        assert "2.5s" in (event.summary or "")

    def test_e2e_test_completed_failed(self):
        record = self._make_record("e2e.test_completed", {
            "nodeid": "tests/e2e/test_foo.py::test_bar",
            "outcome": "failed",
        })
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.status == "error"

    def test_e2e_run_finished(self):
        record = self._make_record("e2e.run_finished", {
            "status": "passed",
            "duration_seconds": 120.5,
        })
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.phase == "teardown"
        assert event.status == "completed"

    def test_e2e_run_error(self):
        record = self._make_record("e2e.run_error", {
            "error": "Something went wrong",
        })
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.status == "error"
        assert event.level == "error"
        assert "Something went wrong" in (event.summary or "")


class TestSourceEventSemantics:
    """Verify completion events derive from their own name, not the paired started event.

    In the legacy e2e_run_events table, source_event is a *pairing* concept:
    e2e.test_completed stores source_event=e2e.test_started.  In timeline.sqlite,
    source_event is the *canonical name* used for derivation.  If we wrote the
    pairing value, completions would render as started events (bug).
    """

    def test_test_completed_with_pairing_source_event_renders_wrong(self):
        """Demonstrates the bug: pairing source_event causes wrong derivation."""
        record = TimelineRecord(
            event_id="evt1",
            timestamp="2026-01-01T00:00:00Z",
            event="e2e.test_completed",
            data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 1.0},
            source_event="e2e.test_started",  # Pairing value — wrong for timeline
        )
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        # With pairing source_event, derivation uses "e2e.test_started" -> wrong
        assert event.step == "test_started"  # Bug: should be "test_completed"
        assert event.status == "active"  # Bug: should be "completed"

    def test_test_completed_with_correct_source_event(self):
        """With source_event=event_name, derivation is correct."""
        record = TimelineRecord(
            event_id="evt1",
            timestamp="2026-01-01T00:00:00Z",
            event="e2e.test_completed",
            data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 1.0},
            source_event="e2e.test_completed",  # Own name — correct for timeline
        )
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.step == "test_completed"
        assert event.status == "completed"

    def test_run_finished_with_pairing_source_event_renders_wrong(self):
        """Demonstrates the bug: pairing source_event on run_finished."""
        record = TimelineRecord(
            event_id="evt1",
            timestamp="2026-01-01T00:00:00Z",
            event="e2e.run_finished",
            data={"status": "passed", "duration_seconds": 60.0},
            source_event="e2e.run_started",  # Pairing value — wrong
        )
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.step == "run_started"  # Bug
        assert event.status == "active"  # Bug

    def test_run_finished_with_correct_source_event(self):
        """With source_event=event_name, run_finished derives correctly."""
        record = TimelineRecord(
            event_id="evt1",
            timestamp="2026-01-01T00:00:00Z",
            event="e2e.run_finished",
            data={"status": "passed", "duration_seconds": 60.0},
            source_event="e2e.run_finished",
        )
        stream = TimelineStream.from_records(-1, [record])
        event = stream.events[0]
        assert event.step == "run_finished"
        assert event.status == "completed"
        assert event.phase == "teardown"


class TestE2ETimelineStoreIntegration:
    """E2E events can be written to and read from SqliteTimelineStore."""

    @pytest.fixture
    def store(self, tmp_path):
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        return SqliteTimelineStore(db_path=tmp_path / "timeline.sqlite")

    def test_write_and_read_e2e_events(self, store):
        key = TimelineKey.for_e2e_run(42)
        store_key = key.to_store_key()

        records = [
            TimelineRecord(
                event_id="evt1",
                timestamp="2026-01-01T00:00:00Z",
                event="e2e.run_started",
                data={"branch": "main", "e2e_run_id": 42},
                source_event="e2e.run_started",
            ),
            TimelineRecord(
                event_id="evt2",
                timestamp="2026-01-01T00:00:10Z",
                event="e2e.test_completed",
                data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 5.0, "e2e_run_id": 42},
                source_event="e2e.test_completed",
            ),
            TimelineRecord(
                event_id="evt3",
                timestamp="2026-01-01T00:01:00Z",
                event="e2e.run_finished",
                data={"status": "passed", "duration_seconds": 60.0, "e2e_run_id": 42},
                source_event="e2e.run_finished",
            ),
        ]
        for record in records:
            store.append(store_key, record)

        read_back = store.read(store_key)
        assert len(read_back) == 3
        assert read_back[0].event == "e2e.run_started"
        assert read_back[2].event == "e2e.run_finished"

    def test_e2e_and_issue_events_isolated(self, store):
        """E2E events don't appear in issue timelines and vice versa."""
        issue_key = TimelineKey.for_issue(123).to_store_key()
        e2e_key = TimelineKey.for_e2e_run(42).to_store_key()

        store.append(issue_key, TimelineRecord(
            event_id="issue-evt", timestamp="2026-01-01T00:00:00Z",
            event="session.started", data={"run_dir": "/tmp/fake-run"},
            source_event="session.started",
        ))
        store.append(e2e_key, TimelineRecord(
            event_id="e2e-evt", timestamp="2026-01-01T00:00:00Z",
            event="e2e.run_started", data={"branch": "main"},
            source_event="e2e.run_started",
        ))

        issue_records = store.read(issue_key)
        e2e_records = store.read(e2e_key)

        assert len(issue_records) == 1
        assert issue_records[0].event == "session.started"
        assert len(e2e_records) == 1
        assert e2e_records[0].event == "e2e.run_started"

    def test_full_pipeline_through_timeline_stream(self, store):
        """E2E events written to store flow through TimelineStream correctly."""
        key = TimelineKey.for_e2e_run(7)
        store_key = key.to_store_key()

        store.append(store_key, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="e2e.run_started", data={"branch": "feature-x"},
            source_event="e2e.run_started",
        ))
        store.append(store_key, TimelineRecord(
            event_id="e2", timestamp="2026-01-01T00:00:30Z",
            event="e2e.test_completed",
            data={"nodeid": "test_a", "outcome": "failed", "duration_seconds": 25.0},
            source_event="e2e.test_completed",
        ))

        records = store.read(store_key)
        stream = TimelineStream.from_records(store_key, records)

        assert len(stream.events) == 2
        assert stream.events[0].phase == "setup"
        assert stream.events[0].parent_key == "e2e-run-7"
        assert stream.events[1].phase == "execution"
        assert stream.events[1].status == "error"

        # Verify dict serialization works
        timeline_dict = stream.to_dict()
        assert len(timeline_dict["events"]) == 2
        assert timeline_dict["events"][0]["phase"] == "setup"


class TestNestOrchestratorEvents:
    """Orchestrator events are nested as children under E2E test time windows."""

    def test_orch_event_nested_under_matching_test_window(self):
        """An orchestrator event within a test window becomes a child of that test."""
        pytest_events = [
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:00:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:00:30Z", "nodeid": "test_a",
             "status": "completed"},
        ]
        orch_events = [
            {"event": "session.started", "timestamp": "2026-01-01T00:00:10Z",
             "step": "started", "status": "active", "summary": "Agent launched"},
        ]
        nest_orchestrator_events(pytest_events, orch_events)

        # The test_started event gets the child (it's the window opener)
        assert len(pytest_events[0]["children"]) == 1
        assert pytest_events[0]["children"][0]["event"] == "session.started"

    def test_orch_events_outside_window_attach_to_nearest(self):
        """Orchestrator events outside test windows attach to nearest preceding event."""
        pytest_events = [
            {"event": "e2e.run_started", "timestamp": "2026-01-01T00:00:00Z"},
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:01:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:02:00Z", "nodeid": "test_a"},
        ]
        orch_events = [
            {"event": "tick.started", "timestamp": "2026-01-01T00:00:30Z",
             "step": "tick_started", "status": "active"},
        ]
        nest_orchestrator_events(pytest_events, orch_events)

        # Event at :30 is before the test window (:01:00-:02:00), so attaches to run_started
        assert len(pytest_events[0]["children"]) == 1
        assert pytest_events[0]["children"][0]["event"] == "tick.started"

    def test_no_orch_events_means_no_children(self):
        """When there are no orchestrator events, no children are added."""
        pytest_events = [
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:00:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:00:30Z", "nodeid": "test_a"},
        ]
        nest_orchestrator_events(pytest_events, [])
        # children lists are initialized but empty
        assert pytest_events[0].get("children", []) == []

    def test_multiple_tests_get_correct_children(self):
        """Each test window gets its own orchestrator events."""
        pytest_events = [
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:00:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:01:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:02:00Z", "nodeid": "test_b"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:03:00Z", "nodeid": "test_b"},
        ]
        orch_events = [
            {"event": "session.started", "timestamp": "2026-01-01T00:00:30Z",
             "step": "started", "status": "active", "summary": "Agent A"},
            {"event": "review.approved", "timestamp": "2026-01-01T00:02:30Z",
             "step": "approved", "status": "completed", "summary": "Review OK"},
        ]
        nest_orchestrator_events(pytest_events, orch_events)

        # test_a's window gets session.started
        assert len(pytest_events[0]["children"]) == 1
        assert pytest_events[0]["children"][0]["event"] == "session.started"
        # test_b's window gets review.approved
        assert len(pytest_events[2]["children"]) == 1
        assert pytest_events[2]["children"][0]["event"] == "review.approved"

    def test_shared_pipeline_events_nest_correctly(self):
        """Events from TimelineStream.to_dict() can be nested with orchestrator events."""
        # Simulate what the shared endpoint does: read E2E events from store,
        # convert to dicts, then nest orchestrator events
        records = [
            TimelineRecord(
                event_id="e1", timestamp="2026-01-01T00:00:00Z",
                event="e2e.test_started", data={"nodeid": "test_a"},
                source_event="e2e.test_started",
            ),
            TimelineRecord(
                event_id="e2", timestamp="2026-01-01T00:01:00Z",
                event="e2e.test_completed",
                data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 55.0},
                source_event="e2e.test_completed",
            ),
        ]
        stream = TimelineStream.from_records(-1, records)
        events = [evt.to_dict() for evt in stream.events]

        # Orchestrator events that happened during the test
        orch_events = [
            {"event": "session.completed", "timestamp": "2026-01-01T00:00:45Z",
             "step": "completed", "status": "completed", "summary": "Code written"},
        ]
        nest_orchestrator_events(events, orch_events)

        # The test_started event dict should have the orchestrator event as a child
        assert len(events[0].get("children", [])) == 1
        assert events[0]["children"][0]["event"] == "session.completed"


class TestCompactBranchLabel:
    """Pin the derivation of compact affordance labels from branch names.

    The matcher turns raw GitHub branch names like
    ``5713-m0-800-e2e-concurrent-1-concurrent-pipeline-test`` into
    short human-readable labels like ``concurrent-1-pipeline`` that
    fit on a single affordance row in the drawer. The extraction
    applies several opinionated rules (strip number prefix, strip
    milestone prefix, strip ``e2e-`` anywhere, strip trailing
    decorative suffixes, dedupe tokens, cap at 24 chars). If any of
    those rules regress, users lose readable labels silently and fall
    back to bare ``#N`` — so we pin each rule explicitly.
    """

    def _extract(self, branch_name: str, issue_number: int):
        from issue_orchestrator.entrypoints.e2e_affordances import (
            _compact_branch_label,
        )
        return _compact_branch_label(branch_name, issue_number)

    def test_strips_issue_number_prefix(self) -> None:
        assert self._extract("5712-foo-bar", 5712) == "foo-bar"

    def test_strips_leading_milestone_prefix(self) -> None:
        assert self._extract("5723-m0-067-inflight-discovery-test", 5723) == (
            "inflight-discovery"
        )

    def test_strips_e2e_token_at_start_and_middle(self) -> None:
        assert self._extract("5712-e2e-claim-coordination-test-issue", 5712) == (
            "claim-coordination"
        )

    def test_strips_trailing_decorative_suffixes(self) -> None:
        assert self._extract("5717-m0-851-pr-creation-checkpoint", 5717) == (
            "pr-creation"
        )
        assert self._extract("5500-foo-test", 5500) == "foo"
        assert self._extract("5500-foo-test-issue", 5500) == "foo"
        assert self._extract("5500-foo-test-data", 5500) == "foo"
        assert self._extract("5500-foo-status", 5500) == "foo"

    def test_dedupes_duplicate_tokens_non_adjacent(self) -> None:
        """``concurrent-1-concurrent-pipeline`` → ``concurrent-1-pipeline``."""
        assert self._extract(
            "5713-m0-800-e2e-concurrent-1-concurrent-pipeline-test", 5713,
        ) == "concurrent-1-pipeline"

    def test_dedupes_duplicate_tokens_adjacent(self) -> None:
        """``pr-pr-creation`` → ``pr-creation``."""
        assert self._extract(
            "5717-m0-851-e2e-pr-pr-creation-checkpoint", 5717,
        ) == "pr-creation"

    def test_caps_at_24_chars_with_ellipsis(self) -> None:
        label = self._extract(
            "5724-m4-057-ui-surface-provider-circuit-breaker-status", 5724,
        )
        assert label is not None
        assert len(label) <= 24
        assert label.endswith("\u2026")
        assert label.startswith("ui-surface-provider")

    def test_returns_none_for_empty_or_missing(self) -> None:
        assert self._extract("", 5000) is None
        assert self._extract("   ", 5000) is None

    def test_handles_branch_without_milestone_or_e2e_prefix(self) -> None:
        assert self._extract("5400-feature-branch", 5400) == "feature-branch"


class TestOrchestratorWindowExcludesE2EEvents:
    """read_orchestrator_events_by_window must not return E2E run events."""

    def test_excludes_negative_key_events(self, tmp_path):
        """E2E events (negative issue_number) are excluded from orchestrator results."""
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.infra.e2e_timeline import read_orchestrator_events_by_window

        store = SqliteTimelineStore(db_path=tmp_path / "timeline.sqlite")

        e2e_key = TimelineKey.for_e2e_run(1).to_store_key()
        issue_key = TimelineKey.for_issue(42).to_store_key()

        store.append(e2e_key, TimelineRecord(
            event_id="e2e-evt", timestamp="2026-01-01T00:00:10Z",
            event="e2e.test_started", data={"nodeid": "test_a"},
            source_event="e2e.test_started",
        ))
        store.append(issue_key, TimelineRecord(
            event_id="orch-evt", timestamp="2026-01-01T00:00:15Z",
            event="session.started", data={"run_dir": "/tmp/fake"},
            source_event="session.started",
        ))

        results = read_orchestrator_events_by_window(
            tmp_path / "timeline.sqlite",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
        )

        assert len(results) == 1
        assert results[0].get("event") == "session.started"

    def test_returns_empty_for_nonexistent_db(self, tmp_path):
        """Returns empty list when the timeline DB does not exist."""
        from issue_orchestrator.infra.e2e_timeline import read_orchestrator_events_by_window

        results = read_orchestrator_events_by_window(
            tmp_path / "nonexistent.sqlite",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
        )
        assert results == []

    def test_events_carry_real_issue_number_per_record(self, tmp_path):
        """Each returned event must carry the issue_number it was stored under.

        Regression: the reader previously passed a single placeholder
        ``issue_number=0`` to ``TimelineStream.from_records`` for every row,
        so all returned events lost their identity. Downstream window
        matching against test events then matched zero issues, leaving
        the E2E timeline with no navigation affordances.
        """
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.infra.e2e_timeline import read_orchestrator_events_by_window

        store = SqliteTimelineStore(db_path=tmp_path / "timeline.sqlite")

        # Three different issues each emit one orchestrator event in the window.
        for issue_num, ts in [
            (5677, "2026-01-01T00:00:10Z"),
            (5678, "2026-01-01T00:00:20Z"),
            (5679, "2026-01-01T00:00:30Z"),
        ]:
            store.append(
                TimelineKey.for_issue(issue_num).to_store_key(),
                TimelineRecord(
                    event_id=f"evt-{issue_num}", timestamp=ts,
                    event="session.started",
                    data={"run_dir": f"/tmp/run-{issue_num}"},
                    source_event="session.started",
                ),
            )

        results = read_orchestrator_events_by_window(
            tmp_path / "timeline.sqlite",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:01:00Z",
        )

        # Each event must report its real issue_number — never 0.
        assert len(results) == 3
        returned_issues = sorted(evt.get("issue_number") for evt in results)
        assert returned_issues == [5677, 5678, 5679], (
            f"Reader collapsed identity to {returned_issues}; expected real issue numbers."
        )
        # And events must remain in chronological order across issues.
        timestamps = [evt.get("timestamp") for evt in results]
        assert timestamps == sorted(timestamps)

    def test_full_pipeline_matches_issue_numbers_to_test_windows(self, tmp_path):
        """End-to-end check: reader output feeds the matcher correctly.

        Combines ``read_orchestrator_events_by_window`` with
        ``_attach_issue_numbers_to_test_windows`` to pin the full path
        used by the live ``/api/e2e-run-detail/{id}`` and
        ``/control/e2e/run/{id}/timeline`` endpoints when no
        ``e2e.agent_snapshot`` rows are available (the common case for
        runs whose snapshot has not been written yet).
        """
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.infra.e2e_timeline import read_orchestrator_events_by_window
        from issue_orchestrator.entrypoints.e2e_affordances import (
            _attach_issue_numbers_to_test_windows,
        )

        store = SqliteTimelineStore(db_path=tmp_path / "timeline.sqlite")

        # Two ephemeral issues active during the test window.
        store.append(
            TimelineKey.for_issue(5677).to_store_key(),
            TimelineRecord(
                event_id="s1", timestamp="2026-01-01T00:00:10Z",
                event="session.started", data={"run_dir": "/tmp/r1"},
                source_event="session.started",
            ),
        )
        store.append(
            TimelineKey.for_issue(5678).to_store_key(),
            TimelineRecord(
                event_id="s2", timestamp="2026-01-01T00:00:20Z",
                event="session.started", data={"run_dir": "/tmp/r2"},
                source_event="session.started",
            ),
        )
        # An event for an issue OUTSIDE the test window must not attach.
        store.append(
            TimelineKey.for_issue(5679).to_store_key(),
            TimelineRecord(
                event_id="s3", timestamp="2026-01-01T00:02:00Z",
                event="session.started", data={"run_dir": "/tmp/r3"},
                source_event="session.started",
            ),
        )

        agent_events = read_orchestrator_events_by_window(
            tmp_path / "timeline.sqlite",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:03:00Z",
        )
        assert len(agent_events) == 3, "Reader must surface all in-window events"

        # Build pytest test events the way the production endpoints do.
        e2e_events = [
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:00:05Z",
             "nodeid": "tests/e2e/test_one.py::test_a"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:00:30Z",
             "nodeid": "tests/e2e/test_one.py::test_a"},
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:01:00Z",
             "nodeid": "tests/e2e/test_two.py::test_b"},
            # test_b is still in progress (no completed event yet) but
            # still must accumulate issue numbers from agents that started
            # during its window.
        ]

        result = _attach_issue_numbers_to_test_windows(
            e2e_events, agent_events, run_id=42,
        )

        def _nums(evt):
            return sorted(a["issue_number"] for a in evt.get("issue_affordances") or [])

        test_a_started = result[0]
        test_a_completed = result[1]
        test_b_started = result[2]
        # test_a window covers issues 5677 and 5678 — both started during it.
        assert _nums(test_a_started) == [5677, 5678]
        assert _nums(test_a_completed) == [5677, 5678]
        # test_b window is open-ended; the out-of-window 5679 (00:02:00)
        # falls inside test_b's start (00:01:00) so it attaches to test_b.
        assert _nums(test_b_started) == [5679]
        # Every affordance carries the run_id passed to the matcher.
        for evt in result:
            for a in evt.get("issue_affordances") or []:
                assert a["run_id"] == 42


class TestE2ERunDetailEndpoint:
    """Endpoint-level tests for GET /api/e2e-run-detail/{run_id}."""

    def _setup_orchestrator_with_timeline(self, store_key, records):
        """Set up a mock orchestrator whose timeline_reader returns the given records."""
        import tempfile
        from unittest.mock import MagicMock
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from fastapi.testclient import TestClient
        from pathlib import Path
        from issue_orchestrator.infra.e2e_db import E2EDB

        run_id = int(str(store_key).rsplit("-", 1)[-1])
        temp_dir = tempfile.TemporaryDirectory(prefix="e2e-run-detail-")
        repo_root = Path(temp_dir.name)
        db_dir = repo_root / ".issue-orchestrator"
        db_dir.mkdir(parents=True)
        db = E2EDB(db_dir / "e2e.db")
        for _ in range(run_id):
            created_run_id = db.start_run(
                repo_root=str(repo_root),
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e"],
                command=["pytest", "tests/e2e"],
                runner_kind="pytest",
            )
            db.finish_run(created_run_id, status="passed", exit_code=0, duration_seconds=60.0)

        mock_orch = MagicMock()
        setattr(mock_orch, "_e2e_temp_dir", temp_dir)
        mock_orch.config.repo_root = repo_root
        mock_orch.config.e2e.flake_threshold = 15.0
        mock_orch.deps.timeline_store.read.return_value = records

        return mock_orch, TestClient(app)

    def test_returns_404_when_no_events(self):
        """Endpoint returns 404 for runs with no shared timeline events."""
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from unittest.mock import MagicMock
        from fastapi.testclient import TestClient
        from pathlib import Path

        mock_orch = MagicMock()
        mock_orch.config.repo_root = Path("/tmp/nonexistent")
        mock_orch.deps.timeline_store.read.return_value = []
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/e2e-run-detail/99")
            assert response.status_code == 404
            assert response.json()["error"] == "not_found"
        finally:
            set_orchestrator(None)

    def test_returns_404_when_e2e_database_missing(self, tmp_path):
        """Endpoint does not synthesize unknown run data when the DB is missing."""
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from unittest.mock import MagicMock
        from fastapi.testclient import TestClient

        mock_orch = MagicMock()
        mock_orch.config.repo_root = tmp_path
        mock_orch.deps.timeline_store.read.return_value = [
            TimelineRecord(
                event_id="e1",
                timestamp="2026-01-01T00:00:00Z",
                event="e2e.run_started",
                data={"branch": "main"},
                source_event="e2e.run_started",
            )
        ]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/e2e-run-detail/10")
            assert response.status_code == 404
            assert response.json() == {
                "error": "not_found",
                "detail": "E2E database not found for run 10",
            }
        finally:
            set_orchestrator(None)

    def test_returns_404_when_e2e_run_row_missing(self, tmp_path):
        """Endpoint distinguishes a missing DB row from a present timeline."""
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from unittest.mock import MagicMock
        from fastapi.testclient import TestClient
        from issue_orchestrator.infra.e2e_db import E2EDB

        repo_root = tmp_path
        db_dir = repo_root / ".issue-orchestrator"
        db_dir.mkdir(parents=True)
        E2EDB(db_dir / "e2e.db")

        mock_orch = MagicMock()
        mock_orch.config.repo_root = repo_root
        mock_orch.config.e2e.flake_threshold = 15.0
        mock_orch.deps.timeline_store.read.return_value = [
            TimelineRecord(
                event_id="e1",
                timestamp="2026-01-01T00:00:00Z",
                event="e2e.run_started",
                data={"branch": "main"},
                source_event="e2e.run_started",
            )
        ]

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/e2e-run-detail/10")
            assert response.status_code == 404
            assert response.json() == {
                "error": "not_found",
                "detail": "E2E run 10 not found",
            }
        finally:
            set_orchestrator(None)

    def test_e2e_run_artifacts_rejects_malformed_db_rows(self):
        """Malformed artifact rows fail loudly instead of disappearing from the payload."""
        from issue_orchestrator.entrypoints.web_issue_detail_routes import (
            _e2e_run_artifacts,
        )

        with pytest.raises(ValueError, match="Malformed E2E artifact row for run 10"):
            _e2e_run_artifacts(
                {"id": 10, "log_path": None},
                [{"kind": "html_report", "label": "HTML Report", "path": 17}],
            )

    def test_log_excerpt_tails_run_log(self, tmp_path):
        """``_public_e2e_run_payload`` tails the worker log into ``log_excerpt``."""
        from issue_orchestrator.entrypoints.web_issue_detail_routes import (
            _public_e2e_run_payload,
        )

        log_path = tmp_path / "run.log"
        log_path.write_text(
            "first line\nsecond line\nthird line\n",
            encoding="utf-8",
        )
        payload = _public_e2e_run_payload({"log_path": str(log_path)}, run_id=42)
        assert payload["log_excerpt"] == ["first line", "second line", "third line"]

    def test_log_excerpt_truncates_to_byte_cap(self, tmp_path):
        """The tail is capped so a chatty pytest run cannot bloat the JSON payload."""
        from issue_orchestrator.entrypoints.web_issue_detail_routes import (
            _E2E_LOG_EXCERPT_BYTE_CAP,
            _public_e2e_run_payload,
        )

        log_path = tmp_path / "run.log"
        line = "x" * 200 + "\n"
        line_count = (_E2E_LOG_EXCERPT_BYTE_CAP // len(line)) + 50
        log_path.write_text(line * line_count, encoding="utf-8")
        payload = _public_e2e_run_payload({"log_path": str(log_path)}, run_id=42)
        joined = "\n".join(payload["log_excerpt"])
        assert len(joined) <= _E2E_LOG_EXCERPT_BYTE_CAP
        # Tail (not head): later lines, not earlier ones.
        assert payload["log_excerpt"][-1] == "x" * 200

    def test_log_excerpt_caps_line_count(self, tmp_path):
        """Even when each line is tiny, the excerpt is line-capped."""
        from issue_orchestrator.entrypoints.web_issue_detail_routes import (
            _E2E_LOG_EXCERPT_LINE_CAP,
            _public_e2e_run_payload,
        )

        log_path = tmp_path / "run.log"
        log_path.write_text(
            "\n".join(f"line {i}" for i in range(_E2E_LOG_EXCERPT_LINE_CAP * 3))
            + "\n",
            encoding="utf-8",
        )
        payload = _public_e2e_run_payload({"log_path": str(log_path)}, run_id=42)
        assert len(payload["log_excerpt"]) == _E2E_LOG_EXCERPT_LINE_CAP
        # Tail wins over head.
        assert payload["log_excerpt"][-1] == f"line {_E2E_LOG_EXCERPT_LINE_CAP * 3 - 1}"

    def test_log_excerpt_collapses_runs_of_blank_lines(self, tmp_path):
        """Pytest separates phases with blank lines; preserve structure (one
        blank between non-blank lines) without letting a chatty step
        degenerate into an all-blank tail."""
        from issue_orchestrator.entrypoints.web_issue_detail_routes import (
            _public_e2e_run_payload,
        )

        log_path = tmp_path / "run.log"
        # Three sections separated by varying-length blank runs, plus
        # leading and trailing blank padding.
        log_path.write_text(
            "\n\n"
            "collected 5 items\n"
            "\n\n   \n\n"
            "test_a PASSED\n"
            "test_b PASSED\n"
            "\n"
            "===== 2 passed in 0.5s =====\n"
            "\n\n",
            encoding="utf-8",
        )
        payload = _public_e2e_run_payload({"log_path": str(log_path)}, run_id=42)
        assert payload["log_excerpt"] == [
            "collected 5 items",
            "",
            "test_a PASSED",
            "test_b PASSED",
            "",
            "===== 2 passed in 0.5s =====",
        ]

    def test_log_excerpt_all_blank_file_returns_empty(self, tmp_path):
        from issue_orchestrator.entrypoints.web_issue_detail_routes import (
            _public_e2e_run_payload,
        )

        log_path = tmp_path / "run.log"
        log_path.write_text("\n\n   \n\n\t\n", encoding="utf-8")
        payload = _public_e2e_run_payload({"log_path": str(log_path)}, run_id=42)
        assert payload["log_excerpt"] == []

    def test_log_excerpt_empty_when_log_path_missing_or_unreadable(self, tmp_path):
        """No ``log_path`` or a missing file degrades to an empty excerpt rather than 500ing."""
        from issue_orchestrator.entrypoints.web_issue_detail_routes import (
            _public_e2e_run_payload,
        )

        for log_path in (None, "", "   ", str(tmp_path / "does-not-exist.log")):
            payload = _public_e2e_run_payload({"log_path": log_path}, run_id=42)
            assert payload["log_excerpt"] == []

    def test_returns_500_without_leaking_malformed_artifact_details(self):
        """Endpoint returns a generic 500 when stored artifact rows are malformed."""
        from unittest.mock import patch

        from issue_orchestrator.entrypoints.web import set_orchestrator

        store_key = TimelineKey.for_e2e_run(10).to_store_key()
        records = [
            TimelineRecord(
                event_id="e1",
                timestamp="2026-01-01T00:00:00Z",
                event="e2e.run_started",
                data={"branch": "main"},
                source_event="e2e.run_started",
            )
        ]
        mock_orch, client = self._setup_orchestrator_with_timeline(store_key, records)
        set_orchestrator(mock_orch)
        try:
            with patch(
                "issue_orchestrator.entrypoints.web_issue_detail_routes._e2e_run_artifacts",
                side_effect=ValueError("secret /tmp/path should not leak"),
            ):
                response = client.get("/api/e2e-run-detail/10")
            assert response.status_code == 500
            assert response.json() == {
                "error": "internal_error",
                "detail": "Malformed E2E run artifacts",
            }
        finally:
            set_orchestrator(None)

    def test_returns_200_with_e2e_events(self):
        """Endpoint returns 200 with events from the shared timeline store."""
        from issue_orchestrator.entrypoints.web import set_orchestrator

        store_key = TimelineKey.for_e2e_run(42).to_store_key()
        records = [
            TimelineRecord(
                event_id="e1", timestamp="2026-01-01T00:00:00Z",
                event="e2e.run_started", data={"branch": "main"},
                source_event="e2e.run_started",
            ),
            TimelineRecord(
                event_id="e2", timestamp="2026-01-01T00:01:00Z",
                event="e2e.run_finished", data={"status": "passed", "duration_seconds": 60.0},
                source_event="e2e.run_finished",
            ),
        ]
        mock_orch, client = self._setup_orchestrator_with_timeline(store_key, records)
        set_orchestrator(mock_orch)
        try:
            response = client.get("/api/e2e-run-detail/42")
            assert response.status_code == 200
            payload = response.json()
            assert payload["title"] == "E2E Run #42"
            events = payload.get("events", [])
            assert len(events) >= 2
            phases = {e["phase"] for e in events}
            assert "setup" in phases
            assert "teardown" in phases
        finally:
            set_orchestrator(None)

    def test_e2e_run_detail_sanitizes_result_cases_for_public_contract(self):
        """Endpoint strips internal DB identifiers from categorized test rows."""
        from issue_orchestrator.contracts.ui_openapi_models import TestCaseResultPayload
        from issue_orchestrator.entrypoints.web import set_orchestrator
        from issue_orchestrator.infra.e2e_db import E2EDB
        from issue_orchestrator.infra.e2e_reports import E2ERunArtifactRecord

        store_key = TimelineKey.for_e2e_run(6).to_store_key()
        records = [
            TimelineRecord(
                event_id="e1",
                timestamp="2026-01-01T00:00:00Z",
                event="e2e.run_started",
                data={"branch": "main"},
                source_event="e2e.run_started",
            ),
            TimelineRecord(
                event_id="e2",
                timestamp="2026-01-01T00:01:00Z",
                event="e2e.test_completed",
                data={
                    "nodeid": "tixmeup.e2e.smoke::runtime.verify_primary_search",
                    "outcome": "passed",
                    "duration_seconds": 0.0,
                },
                source_event="e2e.test_completed",
            ),
            TimelineRecord(
                event_id="e3",
                timestamp="2026-01-01T00:01:30Z",
                event="e2e.test_completed",
                data={
                    "nodeid": "tixmeup.e2e.smoke::runtime.verify_checkout",
                    "outcome": "failed",
                    "duration_seconds": 1.0,
                },
                source_event="e2e.test_completed",
            ),
            TimelineRecord(
                event_id="e4",
                timestamp="2026-01-01T00:02:00Z",
                event="e2e.run_finished",
                data={"status": "passed", "duration_seconds": 120.0},
                source_event="e2e.run_finished",
            ),
        ]
        mock_orch, client = self._setup_orchestrator_with_timeline(store_key, records)
        db = E2EDB(mock_orch.config.repo_root / ".issue-orchestrator" / "e2e.db")
        db.upsert_result_case(
            run_id=6,
            case_id="tixmeup.e2e.smoke::runtime.verify_primary_search",
            outcome="passed",
            duration_seconds=0.0,
            display_name="runtime.verify_primary_search",
            suite_name="tixmeup.e2e.smoke",
            result_source="junit_xml",
            stdout_available=True,
        )
        db.upsert_result_case(
            run_id=6,
            case_id="tixmeup.e2e.smoke::runtime.verify_checkout",
            outcome="failed",
            duration_seconds=1.0,
            failure_details="checkout failed",
            display_name="runtime.verify_checkout",
            suite_name="tixmeup.e2e.smoke",
            result_source="junit_xml",
            stderr_available=True,
        )
        db.record_failure_issue(
            nodeid="tixmeup.e2e.smoke::runtime.verify_checkout",
            github_issue_number=1234,
            parent_issue_number=99,
            first_failing_run_id=6,
            first_failing_sha="abc123",
        )
        junit_path = mock_orch.config.repo_root / "junit.xml"
        # Run-detail should read captured-output availability from SQLite, not
        # parse JUnit XML on the request path.
        junit_path.write_text(
            "<testsuite><not-well-formed",
            encoding="utf-8",
        )
        db.replace_run_artifacts(
            6,
            [
                E2ERunArtifactRecord(
                    kind="junit_xml",
                    label="JUnit XML: junit.xml",
                    path=str(junit_path),
                )
            ],
        )

        set_orchestrator(mock_orch)
        try:
            response = client.get("/api/e2e-run-detail/6")
            assert response.status_code == 200
            results_by_category = response.json()["results_by_category"]
            assert set(results_by_category) == {
                "fixed",
                "flaky",
                "has_issue",
                "passed",
                "quarantined",
                "skipped",
                "untriaged",
            }
            passed = results_by_category["passed"]
            assert len(passed) == 1
            assert passed[0]["nodeid"] == "tixmeup.e2e.smoke::runtime.verify_primary_search"
            assert passed[0]["result_category"] == "passed"
            assert set(passed[0]) == set(TestCaseResultPayload.model_fields)
            assert passed[0]["captured_output"] == {
                "stdout_available": True,
                "stderr_available": False,
            }
            has_issue = results_by_category["has_issue"]
            assert len(has_issue) == 1
            assert has_issue[0]["result_category"] == "has_issue"
            assert set(has_issue[0]) == set(TestCaseResultPayload.model_fields)
            assert has_issue[0]["captured_output"] == {
                "stdout_available": False,
                "stderr_available": True,
            }
            assert has_issue[0]["existing_issue"] == {
                "number": 1234,
                "status": "open",
                "resolution": None,
            }
        finally:
            set_orchestrator(None)

    def test_test_events_carry_issue_affordances_for_navigation(self):
        """Test events expose issue_affordances for the frontend to render as links.

        Each affordance is a ``{"issue_number", "run_id"}`` dict; the
        frontend uses the run_id to route the click directly to the
        explicit e2e issue-detail endpoint.
        """
        from issue_orchestrator.entrypoints.web import set_orchestrator

        store_key = TimelineKey.for_e2e_run(10).to_store_key()
        records = [
            TimelineRecord(
                event_id="e1", timestamp="2026-01-01T00:00:00Z",
                event="e2e.test_started", data={"nodeid": "test_a"},
                source_event="e2e.test_started",
            ),
            TimelineRecord(
                event_id="e2", timestamp="2026-01-01T00:01:00Z",
                event="e2e.test_completed",
                data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 55},
                source_event="e2e.test_completed",
            ),
            TimelineRecord(
                event_id="snap-s1", timestamp="2026-01-01T00:00:30Z",
                event="e2e.agent_snapshot",
                data={"event": "session.started", "timestamp": "2026-01-01T00:00:30Z",
                      "issue_number": 42, "phase": "in_progress", "step": "started",
                      "status": "started", "summary": "Agent launched",
                      "views": ["user", "ops", "debug"]},
                source_event="e2e.agent_snapshot",
            ),
        ]
        mock_orch, client = self._setup_orchestrator_with_timeline(store_key, records)
        set_orchestrator(mock_orch)
        try:
            response = client.get("/api/e2e-run-detail/10")
            assert response.status_code == 200
            payload = response.json()
            events = payload.get("events", [])
            test_started = next((e for e in events if e.get("event") == "e2e.test_started"), None)
            assert test_started is not None
            assert test_started.get("issue_affordances") == [
                {"issue_number": 42, "run_id": 10},
            ]
            test_completed = next((e for e in events if e.get("event") == "e2e.test_completed"), None)
            assert test_completed is not None
            assert test_completed.get("issue_affordances") == [
                {"issue_number": 42, "run_id": 10},
            ]
        finally:
            set_orchestrator(None)

    def test_debug_only_issue_attaches_in_debug_view_not_user_view(self, tmp_path):
        """View-aware affordance attachment.

        Pins the per-issue view filter contract: an issue gets an
        affordance only when the requested view would actually show its
        events. An issue whose only in-window events are ``views=["debug"]``
        should:

        - attach in ``view=debug`` (the user can see those events)
        - NOT attach in ``view=user`` (clicking would open an empty drawer)

        This avoids two pathologies that earlier iterations of the
        pipeline both hit:

        1. Per-event view filtering — drops events with debug-only tags
           even when other events for the SAME issue are user-tagged,
           silently losing window matches for legitimate issues.
        2. No view filtering — attaches affordances for issues whose
           only in-window event is debug-tagged, leaving the user with
           a clickable link that opens an empty drawer in story view.

        The current contract is per-issue: filter at the issue level so
        full-run issues with mixed debug/user events still match all
        windows, but pure-debug-touch issues do not pollute the user
        view.
        """
        import sqlite3
        from unittest.mock import MagicMock
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.infra.e2e_db import E2EDB

        repo_root = tmp_path / "repo"
        (repo_root / ".issue-orchestrator").mkdir(parents=True)
        wt_state = tmp_path / "repo-e2e-worktree" / ".issue-orchestrator" / "state"
        wt_state.mkdir(parents=True)

        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_id = db.start_run(
            repo_root=str(repo_root),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.finish_run(run_id=run_id, status="failed", duration_seconds=120.0)
        run_started = "2026-01-01T00:00:00Z"
        run_finished = "2026-01-01T00:05:00Z"
        with sqlite3.connect(str(repo_root / ".issue-orchestrator" / "e2e.db")) as raw:
            raw.execute(
                "UPDATE e2e_runs SET started_at = ?, finished_at = ? WHERE id = ?",
                (run_started, run_finished, run_id),
            )

        e2e_records = [
            TimelineRecord(
                event_id="ts1", timestamp="2026-01-01T00:00:30Z",
                event="e2e.test_started",
                data={"nodeid": "tests/e2e/test_a.py::test_one"},
                source_event="e2e.test_started",
            ),
            TimelineRecord(
                event_id="tc1", timestamp="2026-01-01T00:02:00Z",
                event="e2e.test_completed",
                data={"nodeid": "tests/e2e/test_a.py::test_one", "outcome": "failed"},
                source_event="e2e.test_completed",
            ),
        ]

        # Issue 5707's only in-window event is debug-tagged. Issue 5705
        # has a fully-tagged event so it should attach in every view.
        wt_store = SqliteTimelineStore(db_path=wt_state / "timeline.sqlite")
        wt_store.append(
            TimelineKey.for_issue(5707).to_store_key(),
            TimelineRecord(
                event_id="d1", timestamp="2026-01-01T00:01:00Z",
                event="claim.acquired",
                data={"run_dir": "/tmp/r1", "views": ["debug"]},
                source_event="claim.acquired",
            ),
        )
        wt_store.append(
            TimelineKey.for_issue(5705).to_store_key(),
            TimelineRecord(
                event_id="u1", timestamp="2026-01-01T00:01:30Z",
                event="session.started",
                data={"run_dir": "/tmp/r2", "views": ["debug", "ops", "user"]},
                source_event="session.started",
            ),
        )

        mock_orch = MagicMock()
        mock_orch.config.repo_root = repo_root
        mock_orch.deps.timeline_store.read.return_value = e2e_records

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)

            def _nums(evt):
                return sorted(
                    a["issue_number"] for a in evt.get("issue_affordances") or []
                )

            # User view: 5705 attaches (has user-tagged event), 5707 does not.
            user_response = client.get(
                f"/api/e2e-run-detail/{run_id}", params={"view": "user"},
            )
            assert user_response.status_code == 200
            user_test_started = next(
                e for e in user_response.json()["events"]
                if e.get("event") == "e2e.test_started"
            )
            assert _nums(user_test_started) == [5705], (
                "User view must show 5705 (user-tagged) and hide 5707 "
                f"(debug-only) — got {_nums(user_test_started)}"
            )

            # Debug view: BOTH issues attach.
            debug_response = client.get(
                f"/api/e2e-run-detail/{run_id}", params={"view": "debug"},
            )
            assert debug_response.status_code == 200
            debug_test_started = next(
                e for e in debug_response.json()["events"]
                if e.get("event") == "e2e.test_started"
            )
            assert _nums(debug_test_started) == [5705, 5707], (
                "Debug view must show both issues — got "
                f"{_nums(debug_test_started)}"
            )

            # Raw view: the issue affordance owner treats raw as unfiltered
            # event access, so all issues with in-window events attach.
            raw_response = client.get(
                f"/api/e2e-run-detail/{run_id}", params={"view": "raw"},
            )
            assert raw_response.status_code == 200
            raw_test_started = next(
                e for e in raw_response.json()["events"]
                if e.get("event") == "e2e.test_started"
            )
            assert _nums(raw_test_started) == [5705, 5707], (
                "Raw view must show both issues — got "
                f"{_nums(raw_test_started)}"
            )
        finally:
            set_orchestrator(None)

    def test_e2e_issue_detail_scopes_events_to_run_window(self, tmp_path):
        """Cross-run leakage regression.

        The e2e-worktree timeline is intentionally preserved across runs
        for history. If the same issue number is used in two different
        runs, the click-through drawer for run 1 must NOT show events
        from run 2. The endpoint must scope ``wt_store.read(issue_number)``
        output to the run's ``started_at`` / ``finished_at`` window.

        Stages two e2e runs and writes two agent events for issue 42 —
        one inside run 1's window, one inside run 2's window — then
        asserts the click-through for each run returns only its own
        events.
        """
        import sqlite3
        from unittest.mock import MagicMock
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.infra.e2e_db import E2EDB

        repo_root = tmp_path / "repo"
        (repo_root / ".issue-orchestrator").mkdir(parents=True)
        wt_state = tmp_path / "repo-e2e-worktree" / ".issue-orchestrator" / "state"
        wt_state.mkdir(parents=True)

        # Two runs with deterministic, non-overlapping windows.
        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_1 = db.start_run(
            repo_root=str(repo_root),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.finish_run(run_id=run_1, status="failed", duration_seconds=60.0)
        run_2 = db.start_run(
            repo_root=str(repo_root),
            orchestrator_id="test-orch-2",
            pytest_args=["tests/e2e"],
        )
        db.finish_run(run_id=run_2, status="failed", duration_seconds=60.0)
        with sqlite3.connect(str(repo_root / ".issue-orchestrator" / "e2e.db")) as raw:
            raw.execute(
                "UPDATE e2e_runs SET started_at = ?, finished_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", run_1),
            )
            raw.execute(
                "UPDATE e2e_runs SET started_at = ?, finished_at = ? WHERE id = ?",
                ("2026-01-01T01:00:00Z", "2026-01-01T01:05:00Z", run_2),
            )

        # Two events for issue 42, one in each run's window. Both have
        # the semantic fields required by _retain_semantic_timeline_events.
        wt_store = SqliteTimelineStore(db_path=wt_state / "timeline.sqlite")
        wt_store.append(
            TimelineKey.for_issue(42).to_store_key(),
            TimelineRecord(
                event_id="run1-evt", timestamp="2026-01-01T00:02:30Z",
                event="session.started",
                data={
                    "run_dir": "/tmp/run1",
                    "views": ["user", "ops", "debug"],
                    "logical_run": 1,
                    "logical_cycle": 1,
                    "logical_phase": "coding",
                    "timeline_schema_version": 4,
                    "narrative": "Run 1 session",
                },
                source_event="session.started",
            ),
        )
        wt_store.append(
            TimelineKey.for_issue(42).to_store_key(),
            TimelineRecord(
                event_id="run2-evt", timestamp="2026-01-01T01:02:30Z",
                event="session.started",
                data={
                    "run_dir": "/tmp/run2",
                    "views": ["user", "ops", "debug"],
                    "logical_run": 1,
                    "logical_cycle": 1,
                    "logical_phase": "coding",
                    "timeline_schema_version": 4,
                    "narrative": "Run 2 session",
                },
                source_event="session.started",
            ),
        )

        mock_orch = MagicMock()
        mock_orch.config.repo_root = repo_root
        mock_orch.deps.timeline_store = SqliteTimelineStore(
            db_path=repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)

            # Run 1 click-through must see ONLY run 1's event.
            r1 = client.get(f"/api/e2e-run/{run_1}/issue-detail/42?view=debug")
            assert r1.status_code == 200, r1.text
            r1_payload = r1.json()
            assert r1_payload["e2e_run_id"] == run_1
            assert r1_payload["lifecycle"]["kind"] == "dashboard"
            r1_ids = {e.get("event_id") for e in (r1_payload.get("events") or [])}
            assert "run1-evt" in r1_ids, (
                f"run 1 event missing from run 1 drawer: ids={r1_ids}"
            )
            assert "run2-evt" not in r1_ids, (
                f"run 1 drawer LEAKED run 2 event (cross-run contamination): "
                f"ids={r1_ids}"
            )

            # Run 2 click-through must see ONLY run 2's event.
            r2 = client.get(f"/api/e2e-run/{run_2}/issue-detail/42?view=debug")
            assert r2.status_code == 200, r2.text
            r2_payload = r2.json()
            assert r2_payload["e2e_run_id"] == run_2
            assert r2_payload["lifecycle"]["kind"] == "dashboard"
            r2_ids = {e.get("event_id") for e in (r2_payload.get("events") or [])}
            assert "run2-evt" in r2_ids, (
                f"run 2 event missing from run 2 drawer: ids={r2_ids}"
            )
            assert "run1-evt" not in r2_ids, (
                f"run 2 drawer LEAKED run 1 event (cross-run contamination): "
                f"ids={r2_ids}"
            )
        finally:
            set_orchestrator(None)

    def test_e2e_issue_detail_never_leaks_production_stack_dependency(self, tmp_path):
        """Production stack gate state must not bleed into an E2E run drawer.

        The stack dependency payload is resolved from the live orchestrator's
        ``dependency_gate_snapshot`` by numeric issue number. The E2E run
        drawer is scoped to an ephemeral run timeline, so if an E2E scenario
        issue reuses a real dashboard issue number, the run drawer must NOT
        render that unrelated production stack gate (#6597/#6680).

        Stages one run with an issue-42 event in its window, seeds a real
        production stack snapshot for issue 42 on the orchestrator state, then
        asserts the E2E drawer returns ``stack_dependency: null``.
        """
        import sqlite3
        from unittest.mock import MagicMock
        from fastapi.testclient import TestClient
        from issue_orchestrator.domain.dependencies import DependencyMode
        from issue_orchestrator.domain.dependency_gates import (
            DependencyGateSnapshot,
            SuccessorEdge,
        )
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.infra.e2e_db import E2EDB

        repo_root = tmp_path / "repo"
        (repo_root / ".issue-orchestrator").mkdir(parents=True)
        wt_state = tmp_path / "repo-e2e-worktree" / ".issue-orchestrator" / "state"
        wt_state.mkdir(parents=True)

        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_id = db.start_run(
            repo_root=str(repo_root),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.finish_run(run_id=run_id, status="failed", duration_seconds=60.0)
        with sqlite3.connect(str(repo_root / ".issue-orchestrator" / "e2e.db")) as raw:
            raw.execute(
                "UPDATE e2e_runs SET started_at = ?, finished_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", run_id),
            )

        wt_store = SqliteTimelineStore(db_path=wt_state / "timeline.sqlite")
        wt_store.append(
            TimelineKey.for_issue(42).to_store_key(),
            TimelineRecord(
                event_id="run-evt", timestamp="2026-01-01T00:02:30Z",
                event="session.started",
                data={
                    "run_dir": "/tmp/run",
                    "views": ["user", "ops", "debug"],
                    "logical_run": 1,
                    "logical_cycle": 1,
                    "logical_phase": "coding",
                    "timeline_schema_version": 4,
                    "narrative": "Run session",
                },
                source_event="session.started",
            ),
        )

        mock_orch = MagicMock()
        mock_orch.config.repo_root = repo_root
        mock_orch.deps.timeline_store = SqliteTimelineStore(
            db_path=repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
        )
        # Real production stack gate for issue 42 living in orchestrator state.
        mock_orch.state.dependency_gate_snapshot = DependencyGateSnapshot(
            reports={},
            successors={
                42: (
                    SuccessorEdge(issue_number=99, ref="#99", mode=DependencyMode.STACK),
                )
            },
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/e2e-run/{run_id}/issue-detail/42?view=debug")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["e2e_run_id"] == run_id
            # The run drawer is ephemeral: it must NOT surface the live
            # dashboard's production stack gate for this reused issue number.
            assert payload["stack_dependency"] is None, (
                "E2E run drawer leaked production stack dependency payload: "
                f"{payload['stack_dependency']!r}"
            )
        finally:
            set_orchestrator(None)

    def test_e2e_issue_detail_surfaces_review_artifact_actions(self, tmp_path):
        """E2E issue drill-in exposes the same review report/JSON actions."""
        import sqlite3
        from unittest.mock import MagicMock
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.infra.e2e_db import E2EDB

        repo_root = tmp_path / "repo"
        (repo_root / ".issue-orchestrator").mkdir(parents=True)
        wt_state = tmp_path / "repo-e2e-worktree" / ".issue-orchestrator" / "state"
        wt_state.mkdir(parents=True)
        run_dir = tmp_path / "agent-run"
        run_dir.mkdir()
        (run_dir / "terminal-recording.jsonl").write_text("{}\n", encoding="utf-8")
        turns = run_dir / "review-exchange" / "turns"
        turns.mkdir(parents=True)
        report = turns / "round-001.reviewer.attempt-001.review-report.md"
        decision = turns / "round-001.reviewer.attempt-001.review-decision.json"
        report.write_text("# Review Report\n\nApproved.\n", encoding="utf-8")
        decision.write_text('{"schema_version": 1, "verdict": "approved"}\n', encoding="utf-8")

        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_id = db.start_run(
            repo_root=str(repo_root),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.finish_run(run_id=run_id, status="failed", duration_seconds=60.0)
        with sqlite3.connect(str(repo_root / ".issue-orchestrator" / "e2e.db")) as raw:
            raw.execute(
                "UPDATE e2e_runs SET started_at = ?, finished_at = ? WHERE id = ?",
                ("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", run_id),
            )

        wt_store = SqliteTimelineStore(db_path=wt_state / "timeline.sqlite")
        wt_store.append(
            TimelineKey.for_issue(42).to_store_key(),
            TimelineRecord(
                event_id="review-artifact-evt",
                timestamp="2026-01-01T00:02:30Z",
                event="review.approved",
                data={
                    "run_dir": str(run_dir),
                    "views": ["user", "ops", "debug"],
                    "logical_run": 1,
                    "logical_cycle": 1,
                    "logical_phase": "review",
                    "timeline_schema_version": 4,
                    "event_intent": "review",
                    "review_oriented": True,
                    "reviewer_agent": "agent:reviewer",
                    "artifacts": [
                        {"type": "review_report", "label": "Review report", "value": str(report), "render_mode": "markdown"},
                        {"type": "review_decision", "label": "Decision JSON", "value": str(decision), "render_mode": "json"},
                    ],
                },
                source_event="review.approved",
            ),
        )

        mock_orch = MagicMock()
        mock_orch.config.repo_root = repo_root
        mock_orch.deps.timeline_store = SqliteTimelineStore(
            db_path=repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
        )

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/e2e-run/{run_id}/issue-detail/42?view=debug")
            assert response.status_code == 200, response.text
            event = response.json()["events"][0]
            report_action = next(
                action for action in event["actions"]
                if action.get("artifact_type") == "review_report"
            )
            decision_action = next(
                action for action in event["actions"]
                if action.get("artifact_type") == "review_decision"
            )
            assert report_action["primary"] is True
            assert report_action["run_dir"] == str(run_dir)
            assert decision_action["type"] == "open_review_artifact"
            assert "primary" not in decision_action
        finally:
            set_orchestrator(None)

    def test_web_endpoint_worktree_fallback_attaches_issue_numbers_end_to_end(self, tmp_path):
        """Full /api/e2e-run-detail/{id} repro through the worktree-fallback route.

        Mirror of the control-endpoint integration test, but exercises the
        web-endpoint path:

            /api/e2e-run-detail/{run_id}
              -> _load_orchestrator_events_for_run        (web.py)
              -> read_orchestrator_events_by_window       (worktree timeline)
              -> _attach_issue_numbers_to_test_windows
              -> JSON response.events[*].issue_numbers

        Without per-issue identity restoration in the reader, this path also
        returns issue_numbers=[] for every test row even when the worktree
        timeline contains real agent activity for many issues. The control
        endpoint and web endpoint share the matcher but reach the reader
        through different code paths, so each needs its own pin.
        """
        import sqlite3
        from unittest.mock import MagicMock
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.infra.e2e_db import E2EDB

        # Layout — repo_root must be a named subdir so its sibling
        # "<name>-e2e-worktree" path is also under tmp_path:
        #   tmp_path/repo/.issue-orchestrator/e2e.db
        #   tmp_path/repo-e2e-worktree/.issue-orchestrator/state/timeline.sqlite
        repo_root = tmp_path / "repo"
        (repo_root / ".issue-orchestrator").mkdir(parents=True)
        wt_state = tmp_path / "repo-e2e-worktree" / ".issue-orchestrator" / "state"
        wt_state.mkdir(parents=True)

        # E2E run record with a controlled time window.
        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_id = db.start_run(
            repo_root=str(repo_root),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.finish_run(run_id=run_id, status="failed", duration_seconds=120.0)
        run_started = "2026-01-01T00:00:00Z"
        run_finished = "2026-01-01T00:05:00Z"
        with sqlite3.connect(str(repo_root / ".issue-orchestrator" / "e2e.db")) as raw:
            raw.execute(
                "UPDATE e2e_runs SET started_at = ?, finished_at = ? WHERE id = ?",
                (run_started, run_finished, run_id),
            )

        # E2E run events live under the negative store key. The web endpoint
        # reads them via _orchestrator.deps.timeline_store.read(store_key).
        run_key = TimelineKey.for_e2e_run(run_id).to_store_key()
        e2e_records = [
            TimelineRecord(
                event_id="rs", timestamp=run_started,
                event="e2e.run_started", data={},
                source_event="e2e.run_started",
            ),
            TimelineRecord(
                event_id="ts1", timestamp="2026-01-01T00:00:30Z",
                event="e2e.test_started",
                data={"nodeid": "tests/e2e/test_a.py::test_one"},
                source_event="e2e.test_started",
            ),
            TimelineRecord(
                event_id="tc1", timestamp="2026-01-01T00:02:00Z",
                event="e2e.test_completed",
                data={"nodeid": "tests/e2e/test_a.py::test_one", "outcome": "failed"},
                source_event="e2e.test_completed",
            ),
            TimelineRecord(
                event_id="rf", timestamp=run_finished,
                event="e2e.run_finished", data={"status": "failed"},
                source_event="e2e.run_finished",
            ),
        ]

        # Agent activity for two ephemeral issues — written into the WORKTREE
        # timeline. No e2e.agent_snapshot rows are present, so the endpoint
        # falls back to _load_orchestrator_events_for_run -> the reader.
        wt_store = SqliteTimelineStore(db_path=wt_state / "timeline.sqlite")
        wt_store.append(
            TimelineKey.for_issue(5677).to_store_key(),
            TimelineRecord(
                event_id="a1", timestamp="2026-01-01T00:00:45Z",
                event="session.started", data={"run_dir": "/tmp/r1"},
                source_event="session.started",
            ),
        )
        wt_store.append(
            TimelineKey.for_issue(5678).to_store_key(),
            TimelineRecord(
                event_id="a2", timestamp="2026-01-01T00:01:30Z",
                event="session.started", data={"run_dir": "/tmp/r2"},
                source_event="session.started",
            ),
        )

        # Mock orchestrator with the real repo_root and an in-memory store
        # that returns the E2E run records for the negative key.
        mock_orch = MagicMock()
        mock_orch.config.repo_root = repo_root
        mock_orch.deps.timeline_store.read.return_value = e2e_records

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get(f"/api/e2e-run-detail/{run_id}")
            assert response.status_code == 200
            events = response.json()["events"]

            def _nums(evt):
                return sorted(
                    a["issue_number"] for a in evt.get("issue_affordances") or []
                )

            test_started = next(e for e in events if e.get("event") == "e2e.test_started")
            test_completed = next(e for e in events if e.get("event") == "e2e.test_completed")
            assert _nums(test_started) == [5677, 5678], (
                f"web endpoint test_started lost identity: {_nums(test_started)}"
            )
            assert _nums(test_completed) == [5677, 5678], (
                f"web endpoint test_completed lost identity: {_nums(test_completed)}"
            )
            # And the run_id stamp is correct for every affordance.
            for a in (test_started.get("issue_affordances") or []):
                assert a["run_id"] == run_id
        finally:
            set_orchestrator(None)

    def test_in_progress_test_window_attaches_issue_affordances(self):
        """Active e2e.test_started (no test_completed yet) must still attach
        issue_affordances from agent activity during the live window.

        Regression: the window-builder previously only emitted windows for
        completed test pairs, so clicking through to the issue detail or
        session log from the E2E timeline was impossible while a run was
        still in progress.
        """
        from issue_orchestrator.entrypoints.web import set_orchestrator

        store_key = TimelineKey.for_e2e_run(10).to_store_key()
        records = [
            TimelineRecord(
                event_id="e1", timestamp="2026-01-01T00:00:00Z",
                event="e2e.test_started", data={"nodeid": "test_live"},
                source_event="e2e.test_started",
            ),
            # No e2e.test_completed — test is still running.
            TimelineRecord(
                event_id="snap-s1", timestamp="2026-01-01T00:00:30Z",
                event="e2e.agent_snapshot",
                data={"event": "session.started", "timestamp": "2026-01-01T00:00:30Z",
                      "issue_number": 42, "phase": "in_progress", "step": "started",
                      "status": "started", "summary": "Agent launched",
                      "views": ["user", "ops", "debug"]},
                source_event="e2e.agent_snapshot",
            ),
        ]
        mock_orch, client = self._setup_orchestrator_with_timeline(store_key, records)
        set_orchestrator(mock_orch)
        try:
            response = client.get("/api/e2e-run-detail/10")
            assert response.status_code == 200
            payload = response.json()
            events = payload.get("events", [])
            test_started = next((e for e in events if e.get("event") == "e2e.test_started"), None)
            assert test_started is not None
            assert test_started.get("issue_affordances") == [
                {"issue_number": 42, "run_id": 10},
            ], (
                "In-progress test window must carry live issue_affordances so "
                "the timeline can navigate to issue detail before completion."
            )
        finally:
            set_orchestrator(None)

    def test_returns_503_when_orchestrator_not_running(self):
        """Endpoint returns 503 when orchestrator is not set."""
        from issue_orchestrator.entrypoints.web import app, set_orchestrator
        from fastapi.testclient import TestClient

        set_orchestrator(None)
        client = TestClient(app)
        response = client.get("/api/e2e-run-detail/1")
        assert response.status_code == 503

    def test_failed_test_event_carries_longrepr_only_on_completed_row(self):
        """e2e.test_completed carries ``longrepr``; test_started does not.

        Both rows share the same nodeid, so the e2e.db backfill would
        naively attach the pytest ``longrepr`` to both — which causes
        the run drawer to render the failure block twice (once under
        "test started Active", once under "test completed Error").
        The contract is that the failure is a property of the TERMINAL
        row only, so we assert:

        - ``test_completed`` carries ``longrepr`` and ``outcome``
        - ``test_started`` does NOT carry either, even though it shares
          the nodeid that the backfill keys on
        """
        from issue_orchestrator.entrypoints.web import set_orchestrator

        store_key = TimelineKey.for_e2e_run(10).to_store_key()
        failure_text = (
            "tests/e2e/test_example.py:42: in test_thing\n"
            "    assert False, 'boom'\n"
            "E   AssertionError: boom"
        )
        records = [
            TimelineRecord(
                event_id="s", timestamp="2026-01-01T00:00:00Z",
                event="e2e.test_started",
                data={"nodeid": "tests/e2e/test_example.py::test_thing"},
                source_event="e2e.test_started",
            ),
            TimelineRecord(
                event_id="c", timestamp="2026-01-01T00:00:05Z",
                event="e2e.test_completed",
                data={
                    "nodeid": "tests/e2e/test_example.py::test_thing",
                    "outcome": "failed",
                    "duration_seconds": 5.0,
                    "is_quarantined": False,
                    "longrepr": failure_text,
                },
                source_event="e2e.test_completed",
            ),
        ]
        mock_orch, client = self._setup_orchestrator_with_timeline(store_key, records)
        set_orchestrator(mock_orch)
        try:
            response = client.get("/api/e2e-run-detail/10")
            assert response.status_code == 200
            events = response.json().get("events", [])
            completed = next(
                (e for e in events if e.get("event") == "e2e.test_completed"),
                None,
            )
            started = next(
                (e for e in events if e.get("event") == "e2e.test_started"),
                None,
            )
            assert completed is not None, "test_completed event missing"
            assert started is not None, "test_started event missing"

            # test_completed — failure metadata present.
            assert completed.get("longrepr") == failure_text, (
                f"longrepr was not promoted onto test_completed; "
                f"got {completed.get('longrepr')!r}"
            )
            assert completed.get("outcome") == "failed"

            # test_started — failure metadata absent (would otherwise
            # cause the run drawer to render the failure block twice).
            assert started.get("longrepr") is None, (
                f"longrepr leaked onto test_started — the failure block "
                f"will render twice in the run drawer: "
                f"{started.get('longrepr')!r}"
            )
            assert started.get("outcome") is None, (
                f"outcome leaked onto test_started: {started.get('outcome')!r}"
            )
        finally:
            set_orchestrator(None)


class TestE2EAgentEventFiltering:
    """Agent events are filtered to story view before nesting."""

    def test_debug_only_events_filtered_out(self):
        """Events with views=['debug'] are excluded from user view."""
        from issue_orchestrator.view_models.issue_detail import _filter_events_by_view

        agent_events = [
            {"event": "claim.acquired", "views": ["debug"], "timestamp": "2026-01-01T00:00:10Z"},
            {"event": "session.started", "views": ["user", "ops", "debug"], "timestamp": "2026-01-01T00:00:20Z"},
            {"event": "apply.step_applied", "views": ["ops", "debug"], "timestamp": "2026-01-01T00:00:30Z"},
            {"event": "session.completed", "views": ["user", "ops", "debug"], "timestamp": "2026-01-01T00:00:40Z"},
        ]
        filtered = _filter_events_by_view(agent_events, "user")

        events = [e["event"] for e in filtered]
        assert "claim.acquired" not in events
        assert "apply.step_applied" not in events
        assert "session.started" in events
        assert "session.completed" in events

    def test_legacy_events_without_views_pass_through(self):
        """Events without views tag are included in all views."""
        from issue_orchestrator.view_models.issue_detail import _filter_events_by_view

        agent_events = [
            {"event": "session.started", "timestamp": "2026-01-01T00:00:10Z"},
            {"event": "review.approved", "timestamp": "2026-01-01T00:00:20Z"},
        ]
        filtered = _filter_events_by_view(agent_events, "user")
        assert len(filtered) == 2


    def test_story_projection_per_window_preserves_both_reviews(self):
        """Story projection runs per test window, not globally.

        Two tests each have a review.started event. If projection ran
        globally, consecutive review.started events could be collapsed
        across windows, dropping one test's review activity.
        """
        from issue_orchestrator.infra.e2e_db import nest_orchestrator_events
        from issue_orchestrator.view_models.issue_detail import _story_projection_events

        pytest_events = [
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:00:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:01:00Z", "nodeid": "test_a"},
            {"event": "e2e.test_started", "timestamp": "2026-01-01T00:02:00Z", "nodeid": "test_b"},
            {"event": "e2e.test_completed", "timestamp": "2026-01-01T00:03:00Z", "nodeid": "test_b"},
        ]
        agent_events = [
            {"event": "review.started", "timestamp": "2026-01-01T00:00:30Z",
             "phase": "reviewing", "step": "started", "status": "started"},
            {"event": "review.started", "timestamp": "2026-01-01T00:02:30Z",
             "phase": "reviewing", "step": "started", "status": "started"},
        ]
        nest_orchestrator_events(pytest_events, agent_events)

        # Apply story projection per window (not globally)
        for evt in pytest_events:
            children = evt.get("children")
            if children:
                evt["children"] = _story_projection_events(children, "user")

        # Both test windows should retain their review.started child
        test_a_children = pytest_events[0].get("children", [])
        test_b_children = pytest_events[2].get("children", [])
        assert len(test_a_children) == 1, f"test_a lost its review child: {test_a_children}"
        assert len(test_b_children) == 1, f"test_b lost its review child: {test_b_children}"
        assert test_a_children[0]["event"] == "review.started"
        assert test_b_children[0]["event"] == "review.started"


class TestE2ETimelineControlEndpoint:
    """Test /control/e2e/run/{run_id}/timeline returns phase_toc and cycles."""

    def test_returns_phase_toc_and_cycles(self, tmp_path):
        """Timeline endpoint includes phase_toc and cycles alongside events."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app

        # Write E2E events to timeline store
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        store = SqliteTimelineStore(db_path=state_dir / "timeline.sqlite", instance_id="inst-1")

        e2e_key = TimelineKey.for_e2e_run(1).to_store_key()
        store.append(e2e_key, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="e2e.run_started", data={"branch": "main"},
            source_event="e2e.run_started",
        ))
        store.append(e2e_key, TimelineRecord(
            event_id="e2", timestamp="2026-01-01T00:01:00Z",
            event="e2e.run_finished", data={"status": "passed", "duration_seconds": 60},
            source_event="e2e.run_finished",
        ))

        client = TestClient(control_app)
        response = client.get(
            "/control/e2e/run/1/timeline",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        payload = response.json()

        assert "events" in payload
        assert len(payload["events"]) == 2
        assert "phase_toc" in payload
        assert "cycles" in payload
        assert payload["lifecycle"]["kind"] == "e2e_suite"
        test_model = payload["lifecycle"]["runs"][0]["e2e_run"]["tests"][0]
        assert test_model["kind"] == "missing_e2e_test_evidence"
        assert test_model["diagnostics"][0]["code"] == "e2e.tests_missing"
        assert isinstance(payload["phase_toc"], list)
        assert isinstance(payload["cycles"], list)
        # phase_toc should have setup and teardown phases
        toc_phases = {item.get("phase") for item in payload["phase_toc"]}
        assert "setup" in toc_phases
        assert "teardown" in toc_phases

    def test_snapshotted_agent_events_attach_issue_affordances(self, tmp_path):
        """Snapshotted agent events annotate test events with issue_affordances
        for the navigation-based architecture (not nested as children)."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app

        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        store = SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")

        e2e_key = TimelineKey.for_e2e_run(1).to_store_key()

        # Pytest events
        store.append(e2e_key, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="e2e.test_started", data={"nodeid": "test_a"},
            source_event="e2e.test_started",
        ))
        store.append(e2e_key, TimelineRecord(
            event_id="e2", timestamp="2026-01-01T00:01:00Z",
            event="e2e.test_completed",
            data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 55},
            source_event="e2e.test_completed",
        ))

        # Snapshotted agent events for issue 42 (within the test window)
        store.append(e2e_key, TimelineRecord(
            event_id="snap-s1", timestamp="2026-01-01T00:00:30Z",
            event="e2e.agent_snapshot",
            data={"event": "session.started", "timestamp": "2026-01-01T00:00:30Z",
                  "issue_number": 42, "phase": "in_progress", "step": "started",
                  "status": "started", "summary": "Agent launched",
                  "views": ["user", "ops", "debug"]},
            source_event="e2e.agent_snapshot",
        ))
        store.append(e2e_key, TimelineRecord(
            event_id="snap-s2", timestamp="2026-01-01T00:00:50Z",
            event="e2e.agent_snapshot",
            data={"event": "session.completed", "timestamp": "2026-01-01T00:00:50Z",
                  "issue_number": 42, "phase": "in_progress", "step": "completed",
                  "status": "completed", "summary": "Code written",
                  "views": ["user", "ops", "debug"]},
            source_event="e2e.agent_snapshot",
        ))

        client = TestClient(control_app)
        response = client.get(
            "/control/e2e/run/1/timeline",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        payload = response.json()

        events = payload["events"]
        assert len(events) == 2
        test_started = events[0]
        assert test_started["event"] == "e2e.test_started"
        # Test event has issue_affordances annotation, not nested children
        assert test_started.get("issue_affordances") == [
            {"issue_number": 42, "run_id": 1},
        ]
        assert payload["lifecycle"]["kind"] == "e2e_suite"
        linked_lifecycles = payload["lifecycle"]["runs"][0]["e2e_run"]["linked_issue_lifecycles"]
        assert [item["issue_number"] for item in linked_lifecycles] == [42]
        # No nested children — frontend opens issue detail via openIssueDetail
        assert test_started.get("children", []) == []

    def test_run_level_issue_affordances_survive_when_no_test_window_matches(
        self,
        tmp_path,
    ):
        """Control timeline response exposes run-level issue timeline links.

        Per-event issue affordances are still window-scoped to pytest
        rows. The run drawer also needs a top-level issue list so a user
        can open cycle-aware issue timelines even when agent activity does
        not align with an individual test_started/test_completed window.
        """
        from fastapi.testclient import TestClient
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app

        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        store = SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")

        e2e_key = TimelineKey.for_e2e_run(1).to_store_key()
        store.append(e2e_key, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="e2e.test_started", data={"nodeid": "test_a"},
            source_event="e2e.test_started",
        ))
        store.append(e2e_key, TimelineRecord(
            event_id="e2", timestamp="2026-01-01T00:01:00Z",
            event="e2e.test_completed",
            data={"nodeid": "test_a", "outcome": "passed", "duration_seconds": 55},
            source_event="e2e.test_completed",
        ))
        store.append(e2e_key, TimelineRecord(
            event_id="snap-late", timestamp="2026-01-01T00:03:00Z",
            event="e2e.agent_snapshot",
            data={"event": "agent.coding_started", "timestamp": "2026-01-01T00:03:00Z",
                  "issue_number": 43, "branch_name": "43-m1-001-late-agent-test-issue",
                  "views": ["user", "ops", "debug"]},
            source_event="e2e.agent_snapshot",
        ))
        store.append(e2e_key, TimelineRecord(
            event_id="snap-debug", timestamp="2026-01-01T00:03:30Z",
            event="e2e.agent_snapshot",
            data={"event": "agent.debug_probe", "timestamp": "2026-01-01T00:03:30Z",
                  "issue_number": 44, "branch_name": "44-m1-002-debug-only-test-issue",
                  "views": ["debug"]},
            source_event="e2e.agent_snapshot",
        ))

        client = TestClient(control_app)
        response = client.get(
            "/control/e2e/run/1/timeline",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        payload = response.json()

        test_started = next(e for e in payload["events"] if e.get("event") == "e2e.test_started")
        assert test_started.get("issue_affordances") == []
        assert payload["issue_affordances"] == [
            {
                "issue_number": 43,
                "run_id": 1,
                "label": "late-agent",
                "branch_name": "43-m1-001-late-agent-test-issue",
            },
        ]

        debug_response = client.get(
            "/control/e2e/run/1/timeline",
            params={"repo_root": str(tmp_path), "view": "debug"},
        )
        assert debug_response.status_code == 200
        debug_issues = [
            a["issue_number"] for a in debug_response.json()["issue_affordances"]
        ]
        assert debug_issues == [43, 44]

    def test_in_progress_test_window_attaches_issue_affordances(self, tmp_path):
        """Control endpoint also pins live in-progress issue_affordances.

        Mirrors the web-endpoint regression: while a test is still running
        (e2e.test_started without a matching e2e.test_completed), agent
        activity during that live window must still annotate the started
        event so the control-center timeline can navigate to issue detail.
        """
        from fastapi.testclient import TestClient
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app

        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        store = SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")

        e2e_key = TimelineKey.for_e2e_run(1).to_store_key()
        store.append(e2e_key, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="e2e.test_started", data={"nodeid": "test_live"},
            source_event="e2e.test_started",
        ))
        # No e2e.test_completed — test is still in progress.
        store.append(e2e_key, TimelineRecord(
            event_id="snap-s1", timestamp="2026-01-01T00:00:30Z",
            event="e2e.agent_snapshot",
            data={"event": "session.started", "timestamp": "2026-01-01T00:00:30Z",
                  "issue_number": 42, "phase": "in_progress", "step": "started",
                  "status": "started", "summary": "Agent launched",
                  "views": ["user", "ops", "debug"]},
            source_event="e2e.agent_snapshot",
        ))

        client = TestClient(control_app)
        response = client.get(
            "/control/e2e/run/1/timeline",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        events = response.json()["events"]

        test_started = next((e for e in events if e.get("event") == "e2e.test_started"), None)
        assert test_started is not None
        assert test_started.get("issue_affordances") == [
            {"issue_number": 42, "run_id": 1},
        ], (
            "Control endpoint must attach issue_affordances to in-progress test "
            "windows so live runs remain navigable."
        )

    def test_returns_empty_events_when_no_timeline(self, tmp_path):
        """Returns empty events list when no timeline DB exists."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.control_api import control_app

        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        # Create empty timeline DB
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")

        client = TestClient(control_app)
        response = client.get(
            "/control/e2e/run/999/timeline",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["events"] == []
        assert payload["phase_toc"] == []
        assert payload["cycles"] == []
        assert payload["issue_affordances"] == []
        assert payload["lifecycle"]["kind"] == "e2e_suite"
        assert payload["lifecycle"]["runs"][0]["e2e_run"]["tests"][0]["kind"] == "missing_e2e_test_evidence"

    def test_e2e_run_timeline_closes_transient_store_on_success(
        self,
        tmp_path,
        monkeypatch,
    ):
        """E2E timeline endpoint closes its short-lived timeline store."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.control_api import control_app

        timeline_db = tmp_path / ".issue-orchestrator" / "state" / "timeline.sqlite"
        timeline_db.parent.mkdir(parents=True)
        timeline_db.touch()
        instances = _patch_closing_timeline_store(
            monkeypatch,
            [[
                TimelineRecord(
                    event_id="run-started",
                    timestamp="2026-01-01T00:00:00Z",
                    event="e2e.run_started",
                    data={},
                    source_event="e2e.run_started",
                )
            ]],
        )

        client = TestClient(control_app)
        response = client.get(
            "/control/e2e/run/1/timeline",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        assert [instance.closed for instance in instances] == [True]

    def test_e2e_run_timeline_closes_transient_store_on_empty(
        self,
        tmp_path,
        monkeypatch,
    ):
        """E2E timeline endpoint closes its store before returning empty payloads."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.control_api import control_app

        timeline_db = tmp_path / ".issue-orchestrator" / "state" / "timeline.sqlite"
        timeline_db.parent.mkdir(parents=True)
        timeline_db.touch()
        instances = _patch_closing_timeline_store(monkeypatch, [[]])

        client = TestClient(control_app)
        response = client.get(
            "/control/e2e/run/1/timeline",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        assert response.json()["events"] == []
        assert [instance.closed for instance in instances] == [True]

    def test_worktree_fallback_attaches_issue_numbers_end_to_end(self, tmp_path):
        """Full endpoint repro through the worktree-fallback agent-events route.

        Pins the route exercised in production when no e2e.agent_snapshot rows
        have been written yet (the common case for live runs):

            /control/e2e/run/{id}/timeline
              -> _load_worktree_agent_events
              -> read_orchestrator_events_by_window  (worktree timeline)
              -> _attach_issue_numbers_to_test_windows
              -> JSON response.events[*].issue_numbers

        Without the per-issue identity restoration in the reader, this path
        returns issue_numbers=[] for every test row even when the worktree
        timeline contains real agent activity for many issues.
        """
        import sqlite3
        from fastapi.testclient import TestClient
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app
        from issue_orchestrator.infra.e2e_db import E2EDB

        # Layout:
        #   tmp_path/repo/.issue-orchestrator/state/timeline.sqlite (E2E run events)
        #   tmp_path/repo/.issue-orchestrator/e2e.db                (run record)
        #   tmp_path/repo-e2e-worktree/.issue-orchestrator/state/timeline.sqlite (agent events)
        repo_root = tmp_path / "repo"
        (repo_root / ".issue-orchestrator" / "state").mkdir(parents=True)
        wt_state = tmp_path / "repo-e2e-worktree" / ".issue-orchestrator" / "state"
        wt_state.mkdir(parents=True)

        # E2E run record with a controlled time window.
        db = E2EDB(repo_root / ".issue-orchestrator" / "e2e.db")
        run_id = db.start_run(
            repo_root=str(repo_root),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.finish_run(run_id=run_id, status="failed", duration_seconds=120.0)
        # Override timestamps so we can place agent events deterministically.
        run_started = "2026-01-01T00:00:00Z"
        run_finished = "2026-01-01T00:05:00Z"
        with sqlite3.connect(str(repo_root / ".issue-orchestrator" / "e2e.db")) as raw:
            raw.execute(
                "UPDATE e2e_runs SET started_at = ?, finished_at = ? WHERE id = ?",
                (run_started, run_finished, run_id),
            )

        # E2E run events live in the base-repo timeline under the negative key.
        base_store = SqliteTimelineStore(
            db_path=repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
        )
        run_key = TimelineKey.for_e2e_run(run_id).to_store_key()
        base_store.append(run_key, TimelineRecord(
            event_id="rs", timestamp=run_started,
            event="e2e.run_started", data={},
            source_event="e2e.run_started",
        ))
        base_store.append(run_key, TimelineRecord(
            event_id="ts1", timestamp="2026-01-01T00:00:30Z",
            event="e2e.test_started", data={"nodeid": "tests/e2e/test_a.py::test_one"},
            source_event="e2e.test_started",
        ))
        base_store.append(run_key, TimelineRecord(
            event_id="tc1", timestamp="2026-01-01T00:02:00Z",
            event="e2e.test_completed",
            data={"nodeid": "tests/e2e/test_a.py::test_one", "outcome": "failed"},
            source_event="e2e.test_completed",
        ))
        base_store.append(run_key, TimelineRecord(
            event_id="rf", timestamp=run_finished,
            event="e2e.run_finished", data={"status": "failed"},
            source_event="e2e.run_finished",
        ))

        # Agent activity for two ephemeral issues — written into the
        # WORKTREE timeline (the "no snapshot" fallback path).
        wt_store = SqliteTimelineStore(db_path=wt_state / "timeline.sqlite")
        wt_store.append(
            TimelineKey.for_issue(5677).to_store_key(),
            TimelineRecord(
                event_id="a1", timestamp="2026-01-01T00:00:45Z",
                event="session.started", data={"run_dir": "/tmp/r1"},
                source_event="session.started",
            ),
        )
        wt_store.append(
            TimelineKey.for_issue(5678).to_store_key(),
            TimelineRecord(
                event_id="a2", timestamp="2026-01-01T00:01:30Z",
                event="session.started", data={"run_dir": "/tmp/r2"},
                source_event="session.started",
            ),
        )

        # Hit the live endpoint.
        client = TestClient(control_app)
        response = client.get(
            f"/control/e2e/run/{run_id}/timeline",
            params={"repo_root": str(repo_root)},
        )
        assert response.status_code == 200
        events = response.json()["events"]

        def _nums(evt):
            return sorted(a["issue_number"] for a in evt.get("issue_affordances") or [])

        # Both the test_started and test_completed events must carry both
        # issue numbers — the regression manifests as issue_affordances=[] here.
        test_started = next(e for e in events if e.get("event") == "e2e.test_started")
        test_completed = next(e for e in events if e.get("event") == "e2e.test_completed")
        assert _nums(test_started) == [5677, 5678], (
            f"test_started lost identity: {_nums(test_started)}"
        )
        assert _nums(test_completed) == [5677, 5678], (
            f"test_completed lost identity: {_nums(test_completed)}"
        )
        # Every affordance must carry the run_id so the frontend can
        # route the click to the explicit e2e issue-detail endpoint.
        for evt in (test_started, test_completed):
            for a in evt.get("issue_affordances") or []:
                assert a["run_id"] == run_id


class TestControlIssueDetailEndpoint:
    """Control center serves issue detail from base repo or E2E worktree timeline."""

    @pytest.fixture(autouse=True)
    def _configured_repository(self, tmp_path):
        config_path = (
            tmp_path
            / ".issue-orchestrator"
            / "config"
            / "modes"
            / "default"
            / "default.yaml"
        )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("{}\n", encoding="utf-8")

    def test_serves_issue_detail_from_base_repo(self, tmp_path):
        """Control endpoint reads from base repo timeline.sqlite."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app

        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        store = SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")

        # Issue 42 has session events
        store.append(42, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="session.started",
            data={"run_dir": "/tmp/fake-run", "logical_run": 1, "logical_cycle": 1,
                  "logical_phase": "coding", "timeline_schema_version": 4,
                  "views": ["user", "ops", "debug"]},
            source_event="session.started",
        ))

        client = TestClient(control_app)
        response = client.get(
            "/api/issue-detail/42",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["issue_number"] == 42
        assert "events" in payload
        assert "runs" in payload  # Same shape as dashboard issue detail

    def test_falls_back_to_e2e_worktree_timeline(self, tmp_path):
        """When base repo has no events, control endpoint reads from E2E worktree."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app
        from issue_orchestrator.infra.e2e_worktree import get_e2e_worktree_path

        # Empty base repo timeline
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")

        # E2E worktree timeline has the issue events
        wt_path = get_e2e_worktree_path(tmp_path)
        wt_state = wt_path / ".issue-orchestrator" / "state"
        wt_state.mkdir(parents=True)
        wt_store = SqliteTimelineStore(db_path=wt_state / "timeline.sqlite")
        wt_store.append(99, TimelineRecord(
            event_id="e1", timestamp="2026-01-01T00:00:00Z",
            event="session.completed",
            data={"logical_run": 1, "logical_cycle": 1, "logical_phase": "coding",
                  "timeline_schema_version": 4, "views": ["user", "ops", "debug"]},
            source_event="session.completed",
        ))

        client = TestClient(control_app)
        response = client.get(
            "/api/issue-detail/99",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["issue_number"] == 99
        assert len(payload.get("events", [])) > 0

    def test_returns_404_when_no_events(self, tmp_path):
        """Control endpoint returns 404 when no events anywhere."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.control_api import control_app

        client = TestClient(control_app)
        response = client.get(
            "/api/issue-detail/999",
            params={"repo_root": str(tmp_path)},
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        ("expires_at", "expected_status", "expect_actions"),
        [
            ("2020-01-01T00:00:00+00:00", "expired", False),
            ("2099-01-01T00:00:00+00:00", "retained", True),
        ],
    )
    def test_control_issue_detail_enforces_and_scopes_retention_actions(
        self,
        tmp_path,
        expires_at,
        expected_status,
        expect_actions,
    ):
        from fastapi.testclient import TestClient
        from issue_orchestrator.domain.run_manifest import RunManifest
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
        from issue_orchestrator.entrypoints.control_api import control_app

        run = FileSystemSessionOutput().start_run(
            tmp_path / "worktree",
            "issue-42",
            issue_number=42,
        )
        (run.run_dir / "terminal-recording.jsonl").write_text(
            "{}\n",
            encoding="utf-8",
        )
        manifest = RunManifest.load(run.run_dir)
        manifest.ended_at = "2026-01-01T00:00:00+00:00"
        manifest.retention_expires_at = expires_at
        manifest.evidence_available = True
        manifest.save()
        database = tmp_path / ".issue-orchestrator" / "state" / "timeline.sqlite"
        with SqliteTimelineStore(database) as store:
            store.append(
                42,
                TimelineRecord(
                    event_id="completed-42",
                    timestamp="2026-01-01T00:00:00Z",
                    event="session.completed",
                    source_event="session.completed",
                    data={
                        "run_dir": str(run.run_dir),
                        "logical_run": 1,
                        "logical_cycle": 1,
                        "logical_phase": "coding",
                        "timeline_schema_version": 4,
                        "views": ["user", "ops", "debug"],
                    },
                ),
            )

        response = TestClient(control_app).get(
            "/api/issue-detail/42",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        event = response.json()["events"][-1]
        assert event["evidence"]["status"] == expected_status
        actions = event["actions"]
        assert bool(actions) is expect_actions
        if expect_actions:
            assert all(action["repo_root"] == str(tmp_path) for action in actions)
            assert any(
                action["type"] == "set_timeline_evidence_pin"
                for action in actions
            )

    def test_control_issue_detail_closes_transient_store_on_success(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Issue-detail endpoint closes a short-lived store after a successful read."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.control_api import control_app

        timeline_db = tmp_path / ".issue-orchestrator" / "state" / "timeline.sqlite"
        timeline_db.parent.mkdir(parents=True)
        timeline_db.touch()
        instances = _patch_closing_timeline_store(
            monkeypatch,
            [[
                TimelineRecord(
                    event_id="session-completed",
                    timestamp="2026-01-01T00:00:00Z",
                    event="session.completed",
                    data={
                        "logical_run": 1,
                        "logical_cycle": 1,
                        "logical_phase": "coding",
                        "timeline_schema_version": 4,
                        "views": ["user", "ops", "debug"],
                    },
                    source_event="session.completed",
                )
            ]],
        )

        client = TestClient(control_app)
        response = client.get(
            "/api/issue-detail/42",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 200
        assert [instance.closed for instance in instances] == [True]

    def test_control_issue_detail_closes_transient_stores_on_exception_and_empty(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Issue-detail closes each candidate store when reads raise or return empty."""
        from fastapi.testclient import TestClient
        from issue_orchestrator.entrypoints.control_api import control_app
        from issue_orchestrator.infra.e2e_worktree import get_e2e_worktree_path

        base_db = tmp_path / ".issue-orchestrator" / "state" / "timeline.sqlite"
        worktree_db = (
            get_e2e_worktree_path(tmp_path)
            / ".issue-orchestrator"
            / "state"
            / "timeline.sqlite"
        )
        for db_path in (base_db, worktree_db):
            db_path.parent.mkdir(parents=True)
            db_path.touch()
        instances = _patch_closing_timeline_store(
            monkeypatch,
            [RuntimeError("boom"), []],
        )

        client = TestClient(control_app)
        response = client.get(
            "/api/issue-detail/42",
            params={"repo_root": str(tmp_path)},
        )

        assert response.status_code == 404
        assert [instance.closed for instance in instances] == [True, True]


class TestPruneWorktreeArtifacts:
    """prune_old_runs with e2e_worktree_path cleans worktree-local data."""

    def test_prunes_old_worktree_sessions_and_timeline(self, tmp_path):
        """Old session dirs and timeline events are removed when runs are pruned."""
        import time
        from issue_orchestrator.infra.e2e_db import E2EDB
        from issue_orchestrator.execution.timeline_store import SqliteTimelineStore

        # Set up E2E DB with 3 runs
        db = E2EDB(tmp_path / "e2e.db")
        for i in range(3):
            run_id = db.start_run(
                repo_root=str(tmp_path),
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e"],
            )
            db.finish_run(run_id=run_id, status="passed", duration_seconds=10.0)
            time.sleep(0.05)  # Ensure distinct timestamps

        # Set up worktree with sessions and timeline
        wt = tmp_path / "e2e-worktree"
        wt_state = wt / ".issue-orchestrator" / "state"
        wt_state.mkdir(parents=True)
        wt_sessions = wt / ".issue-orchestrator" / "sessions"

        # Create 3 session dirs with distinct mtimes
        for i in range(3):
            session_dir = wt_sessions / f"session-{i}"
            session_dir.mkdir(parents=True)
            (session_dir / "terminal-recording.jsonl").write_text("data")

        # Backdate old session dirs
        import os
        old_time = time.time() - 86400  # 1 day ago
        for i in range(2):
            session_dir = wt_sessions / f"session-{i}"
            os.utime(session_dir, (old_time + i, old_time + i))

        # Write timeline events
        wt_store = SqliteTimelineStore(db_path=wt_state / "timeline.sqlite")
        wt_store.append(1, TimelineRecord(
            event_id="old", timestamp="2025-01-01T00:00:00Z",
            event="session.started", data={"run_dir": "/fake"},
            source_event="session.started",
        ))
        wt_store.append(2, TimelineRecord(
            event_id="new", timestamp="2099-01-01T00:00:00Z",
            event="session.started", data={"run_dir": "/fake"},
            source_event="session.started",
        ))

        # Prune to keep only 1 run
        db.prune_old_runs(1, e2e_worktree_path=wt)

        # Old session dirs should be gone, newest kept
        remaining_sessions = list(wt_sessions.iterdir())
        assert len(remaining_sessions) == 1
        assert remaining_sessions[0].name == "session-2"

        # Old timeline events should be pruned
        import sqlite3
        conn = sqlite3.connect(wt_state / "timeline.sqlite")
        rows = conn.execute("SELECT event_id FROM timeline_events").fetchall()
        conn.close()
        event_ids = {r[0] for r in rows}
        assert "old" not in event_ids
        assert "new" in event_ids

    def test_no_error_when_worktree_missing(self, tmp_path):
        """Pruning works without error when worktree path doesn't exist."""
        from issue_orchestrator.infra.e2e_db import E2EDB

        db = E2EDB(tmp_path / "e2e.db")
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
        )
        db.finish_run(run_id=run_id, status="passed", duration_seconds=10.0)

        # Should not raise
        db.prune_old_runs(0, e2e_worktree_path=tmp_path / "nonexistent")


class TestCheckE2ECompletion:
    """Tests for _check_e2e_completion SSE broadcasting on worker exit."""

    def _run_check(self, tmp_path, finished_ids, run_status="passed"):
        """Set up mocks and call _check_e2e_completion."""
        from unittest.mock import MagicMock, patch
        from issue_orchestrator.infra.e2e_db import E2EDB

        mock_orch = MagicMock()
        mock_orch.config.repo_root = tmp_path

        mock_runner = MagicMock()
        mock_runner.cleanup_finished.return_value = finished_ids

        if finished_ids:
            # Create a real E2E DB with a finished run
            db_dir = tmp_path / ".issue-orchestrator"
            db_dir.mkdir(parents=True)
            db = E2EDB(db_dir / "e2e.db")
            orch_id = finished_ids[0]
            run_id = db.start_run(
                repo_root=str(tmp_path),
                orchestrator_id=orch_id,
                pytest_args=["tests/e2e"],
            )
            db.finish_run(run_id=run_id, status=run_status, duration_seconds=10.0)

        with patch("issue_orchestrator.infra.orchestrator.get_e2e_runner_manager", return_value=mock_runner):
            from issue_orchestrator.infra.orchestrator import Orchestrator
            Orchestrator._check_e2e_completion(mock_orch)  # noqa: SLF001 - targeted legacy hook test

        return mock_orch

    def test_publishes_completed_event_on_passed_run(self, tmp_path):
        """When E2E worker finishes with passed status, E2E_COMPLETED is published."""
        from issue_orchestrator.events.catalog import EventName

        mock_orch = self._run_check(tmp_path, ["test-orch"], run_status="passed")
        mock_orch.deps.events.publish.assert_called_once()
        call_args = mock_orch.deps.events.publish.call_args[0][0]
        assert call_args.name == EventName.E2E_COMPLETED

    def test_publishes_failed_event_on_failed_run(self, tmp_path):
        """When E2E worker finishes with failed status, E2E_FAILED is published."""
        from issue_orchestrator.events.catalog import EventName

        mock_orch = self._run_check(tmp_path, ["test-orch"], run_status="failed")
        mock_orch.deps.events.publish.assert_called_once()
        call_args = mock_orch.deps.events.publish.call_args[0][0]
        assert call_args.name == EventName.E2E_FAILED

    def test_no_publish_when_no_workers_finished(self, tmp_path):
        """When no E2E workers have finished, nothing is published."""
        mock_orch = self._run_check(tmp_path, [])
        mock_orch.deps.events.publish.assert_not_called()
