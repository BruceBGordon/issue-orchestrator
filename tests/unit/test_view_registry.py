"""Tests for the timeline view registry and fan-out logic."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from issue_orchestrator.events.catalog import EventName
from issue_orchestrator.events.view_registry import (
    VIEW_REGISTRY,
    VIEWS,
    ViewEvent,
    fan_out,
)
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.ports.timeline_store import TimelineRecord
from issue_orchestrator.view_models.issue_detail import (
    _filter_events_by_view,
    build_issue_detail_view_model,
)


class TestViewEvent:
    def test_visible_in(self):
        ve = ViewEvent("test.event", frozenset({"user", "ops"}))
        assert ve.visible_in("user")
        assert ve.visible_in("ops")
        assert not ve.visible_in("debug")

    def test_with_narrative(self):
        ve = ViewEvent("agent.started", frozenset({"user"}), "Agent started", "coding")
        assert ve.narrative == "Agent started"
        assert ve.phase == "coding"


class TestFanOut:
    def test_registered_event_returns_specs(self):
        specs = fan_out("session.started")
        assert len(specs) >= 1
        assert any(s.name == "agent.coding_started" for s in specs)

    def test_unregistered_event_returns_debug_only(self):
        specs = fan_out("unknown.internal.event")
        assert len(specs) == 1
        assert specs[0].name == "unknown.internal.event"
        assert specs[0].views == frozenset({"debug"})

    def test_debug_shorthand_fills_name(self):
        specs = fan_out("claim.renewed")
        assert len(specs) == 1
        assert specs[0].name == "claim.renewed"
        assert specs[0].views == frozenset({"debug"})

    def test_user_events_include_all_views(self):
        specs = fan_out("review.approved")
        assert len(specs) == 1
        s = specs[0]
        assert "user" in s.views
        assert "ops" in s.views
        assert "debug" in s.views

    def test_review_round_events_visible_to_user(self):
        specs = fan_out("review_exchange.round_started")
        assert len(specs) == 1
        s = specs[0]
        assert "user" in s.views
        assert "ops" in s.views
        assert "debug" in s.views

    def test_review_exchange_lifecycle_events_visible_to_user(self):
        started = fan_out("review_exchange.started")
        completed = fan_out("review_exchange.completed")
        failed = fan_out("review_exchange.failed")

        assert len(started) == 1
        assert len(completed) == 1
        assert len(failed) == 1

        for spec in (started[0], completed[0], failed[0]):
            assert "user" in spec.views
            assert "ops" in spec.views
            assert "debug" in spec.views

    def test_publish_failed_visible_to_user(self):
        specs = fan_out("publish.failed")

        assert len(specs) == 1
        spec = specs[0]
        assert spec.name == "publish.failed"
        assert spec.narrative == "Publish failed"
        assert spec.phase == "orchestrator"
        assert "user" in spec.views
        assert "ops" in spec.views
        assert "debug" in spec.views

    def test_invalid_completion_record_is_ops_only_to_avoid_duplicate_user_rows(self):
        specs = fan_out("session.invalid_completion_record")

        assert len(specs) == 1
        spec = specs[0]
        assert spec.name == "agent.invalid_completion_record"
        assert spec.narrative == "Completion record rejected"
        assert spec.phase == "orchestrator"
        assert "user" not in spec.views
        assert "ops" in spec.views
        assert "debug" in spec.views


class TestFilterEventsByView:
    def test_user_view_filters_debug_events(self):
        events = [
            {"event": "agent.coding_started", "views": ["user", "ops", "debug"]},
            {"event": "claim.renewed", "views": ["debug"]},
            {"event": "review.approved", "views": ["user", "ops", "debug"]},
        ]
        result = _filter_events_by_view(events, "user")
        assert len(result) == 2
        assert result[0]["event"] == "agent.coding_started"
        assert result[1]["event"] == "review.approved"

    def test_debug_view_includes_everything(self):
        events = [
            {"event": "agent.coding_started", "views": ["user", "ops", "debug"]},
            {"event": "claim.renewed", "views": ["debug"]},
            {"event": "review.approved", "views": ["user", "ops", "debug"]},
        ]
        result = _filter_events_by_view(events, "debug")
        assert len(result) == 3

    def test_raw_view_includes_unregistered_view_tags(self):
        events = [
            {"event": "agent.coding_started", "views": ["user", "ops", "debug"]},
            {"event": "claim.renewed", "views": ["debug"]},
            {"event": "third.party.detail", "views": ["plugin-only"]},
        ]
        result = _filter_events_by_view(events, "raw")
        assert result == events

    def test_ops_view_excludes_debug_only(self):
        events = [
            {"event": "agent.coding_started", "views": ["user", "ops", "debug"]},
            {"event": "claim.renewed", "views": ["debug"]},
            {"event": "review_exchange.round_started", "views": ["user", "ops", "debug"]},
        ]
        result = _filter_events_by_view(events, "ops")
        assert len(result) == 2

    def test_legacy_events_without_views_included_everywhere(self):
        events = [{"event": "session.started"}]  # no views tag
        assert len(_filter_events_by_view(events, "user")) == 1
        assert len(_filter_events_by_view(events, "debug")) == 1

    def test_legacy_debug_only_publish_failed_is_promoted(self):
        events = [
            {
                "event": "publish.failed",
                "source_event": "publish.failed",
                "views": ["debug"],
            }
        ]

        assert len(_filter_events_by_view(events, "user")) == 1
        assert len(_filter_events_by_view(events, "ops")) == 1


class TestWriterFanOut:
    """Test that DefaultTimelineWriter fans out events through the registry."""

    def _make_store(self):
        store = MagicMock()
        store.read.return_value = []
        return store

    def _make_event(self, name: str, run_dir: str = "", issue_number: int = 42) -> TraceEvent:
        data: dict = {
            "issue_number": issue_number,
            "task": "code",
        }
        if run_dir:
            data["run_dir"] = run_dir
        return TraceEvent(name, data)

    def _make_run_dir(self, tmp_path):
        run_dir = tmp_path / "sessions" / "run1"
        run_dir.mkdir(parents=True)
        (run_dir / "ui-session.log").write_text("log")
        return str(run_dir)

    def test_session_started_fans_out_to_user_event(self, tmp_path):
        store = self._make_store()
        writer = DefaultTimelineWriter(store)
        run_dir = self._make_run_dir(tmp_path)
        writer.record(self._make_event("session.started", run_dir=run_dir))

        assert store.append.call_count >= 1
        records = [call.args[1] for call in store.append.call_args_list]

        names = [r.event for r in records]
        assert "agent.coding_started" in names

        for r in records:
            assert r.source_event == "session.started"

        user_record = next(r for r in records if r.event == "agent.coding_started")
        assert "user" in user_record.data["views"]
        assert user_record.data["narrative"] == "Coding agent started"

    def test_debug_only_event_produces_one_record(self):
        store = self._make_store()
        writer = DefaultTimelineWriter(store)
        writer.record(self._make_event("claim.renewed"))

        assert store.append.call_count == 1
        record = store.append.call_args[0][1]
        assert record.event == "claim.renewed"
        assert record.source_event == "claim.renewed"
        assert record.data["views"] == ["debug"]

    def test_enrichment_uses_source_event_from_previous(self, tmp_path):
        """Verify enrichment reads source_event, not event, from previous record."""
        store = self._make_store()
        prev = TimelineRecord(
            event_id="prev-1",
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            event="agent.coding_started",
            data={"logical_run": 1, "logical_cycle": 1, "_logical_restart_pending": False},
            source_event="session.started",
        )
        store.read.return_value = [prev]
        writer = DefaultTimelineWriter(store)

        writer.record(
            self._make_event(
                "session.failed",
                run_dir=str(tmp_path / "deleted-run"),
            )
        )
        assert store.append.called

    def test_phase_override_from_registry(self, tmp_path):
        store = self._make_store()
        writer = DefaultTimelineWriter(store)
        # session.completed requires completion_path_absolute
        completion = tmp_path / "completion.json"
        completion.write_text("{}")
        event = TraceEvent(
            "session.completed",
            {
                "issue_number": 42,
                "task": "code",
                "run_dir": str(tmp_path / "deleted-run"),
                "completion_path_absolute": str(completion),
            },
        )
        writer.record(event)

        records = [call.args[1] for call in store.append.call_args_list]
        user_record = next(r for r in records if r.event == "agent.completed")
        assert user_record.data["logical_phase"] == "orchestrator"


class TestViewModelViewFiltering:
    """Test build_issue_detail_view_model produces different output per view."""

    def _make_events(self):
        """Simulate events as they appear after fan-out (stored in timeline DB)."""
        base = {
            "logical_run": 1,
            "logical_cycle": 1,
            "_logical_restart_pending": False,
            "event_intent": "coding",
            "review_oriented": False,
        }
        return [
            {
                **base,
                "event": "agent.coding_started",
                "logical_phase": "coding",
                "views": ["user", "ops", "debug"],
                "narrative": "Coding agent started",
                "timestamp": "2026-03-07T10:00:00Z",
            },
            {
                **base,
                "event": "claim.renewed",
                "logical_phase": "system",
                "views": ["debug"],
                "timestamp": "2026-03-07T10:01:00Z",
            },
            {
                **base,
                "event": "agent.coding_completed",
                "logical_phase": "coding",
                "views": ["user", "ops", "debug"],
                "narrative": "Agent finished coding",
                "timestamp": "2026-03-07T10:05:00Z",
            },
            {
                **base,
                "event": "review.started",
                "logical_phase": "review",
                "event_intent": "review",
                "review_oriented": True,
                "views": ["user", "ops", "debug"],
                "narrative": "Code review started",
                "timestamp": "2026-03-07T10:06:00Z",
            },
            {
                **base,
                "event": "review_exchange.round_started",
                "logical_phase": "review",
                "event_intent": "review",
                "review_oriented": True,
                "views": ["user", "ops", "debug"],
                "narrative": "Review round started",
                "timestamp": "2026-03-07T10:07:00Z",
            },
            {
                **base,
                "event": "review_exchange.round_completed",
                "logical_phase": "review",
                "event_intent": "review",
                "review_oriented": True,
                "views": ["user", "ops", "debug"],
                "narrative": "Review round completed",
                "timestamp": "2026-03-07T10:08:00Z",
            },
            {
                **base,
                "event": "review.approved",
                "logical_phase": "review",
                "event_intent": "review",
                "review_oriented": True,
                "views": ["user", "ops", "debug"],
                "narrative": "Review approved",
                "timestamp": "2026-03-07T10:09:00Z",
            },
            {
                **base,
                "event": "validation.passed",
                "logical_phase": "orchestrator",
                "event_intent": "orchestrator",
                "views": ["user", "ops", "debug"],
                "narrative": "Validation passed",
                "timestamp": "2026-03-07T10:10:00Z",
            },
            {
                **base,
                "event": "pr.created",
                "logical_phase": "orchestrator",
                "event_intent": "orchestrator",
                "views": ["user", "ops", "debug"],
                "narrative": "PR created",
                "timestamp": "2026-03-07T10:11:00Z",
            },
        ]

    def test_user_view_shows_clean_story(self):
        events = self._make_events()
        result = build_issue_detail_view_model(
            issue_number=42,
            title="Test",
            issue_url="https://github.com/test/42",
            events=events,
            phase_toc=[],
            cycles=[],
            view="user",
        )
        # User view should not include claim.renewed and should collapse completed review mechanics.
        step_events = [s["event"] for s in result["timeline_steps"]]
        assert "claim.renewed" not in step_events
        assert "review_exchange.round_started" not in step_events
        assert "review.started" not in step_events
        assert "review_exchange.started" not in step_events
        assert "review_exchange.round_completed" not in step_events
        assert "agent.coding_started" in step_events
        assert "review.approved" in step_events
        assert "pr.created" in step_events

    def test_debug_view_shows_everything(self):
        events = self._make_events()
        result = build_issue_detail_view_model(
            issue_number=42,
            title="Test",
            issue_url="https://github.com/test/42",
            events=events,
            phase_toc=[],
            cycles=[],
            view="debug",
        )
        step_events = [s["event"] for s in result["timeline_steps"]]
        assert "claim.renewed" in step_events
        assert "review_exchange.round_started" in step_events
        assert "agent.coding_started" in step_events

    def test_ops_view_includes_exchange_rounds(self):
        events = self._make_events()
        result = build_issue_detail_view_model(
            issue_number=42,
            title="Test",
            issue_url="https://github.com/test/42",
            events=events,
            phase_toc=[],
            cycles=[],
            view="ops",
        )
        step_events = [s["event"] for s in result["timeline_steps"]]
        assert "claim.renewed" not in step_events
        assert "review_exchange.round_started" in step_events

    def test_user_view_phase_groups_are_clean(self):
        """User view should produce Coding -> Review -> Orchestrator phases."""
        events = self._make_events()
        result = build_issue_detail_view_model(
            issue_number=42,
            title="Test",
            issue_url="https://github.com/test/42",
            events=events,
            phase_toc=[],
            cycles=[],
            view="user",
        )
        runs = result["runs"]
        assert len(runs) >= 1
        last_run = runs[-1]
        cycles = last_run["cycles"]
        assert len(cycles) >= 1
        phase_labels = [
            pg["label"]
            for c in cycles
            for pg in c.get("phase_groups", [])
        ]
        # Should be exactly: Coding, Review, Orchestrator — no fragments
        assert phase_labels == ["Coding", "Review", "Orchestrator"]

    def test_view_field_in_payload(self):
        events = self._make_events()
        result = build_issue_detail_view_model(
            issue_number=42,
            title="Test",
            issue_url="https://github.com/test/42",
            events=events,
            phase_toc=[],
            cycles=[],
            view="ops",
        )
        assert result["view"] == "ops"

    def test_raw_view_payload_uses_raw_event_source(self):
        semantic_events = self._make_events()
        raw_events = [
            {
                "event": "trace.noisy_internal",
                "timestamp": "2026-03-07T10:09:00Z",
                "views": ["plugin-only"],
            },
            *semantic_events,
        ]
        result = build_issue_detail_view_model(
            issue_number=42,
            title="Test",
            issue_url="https://github.com/test/42",
            events=semantic_events,
            phase_toc=[],
            cycles=[],
            view="raw",
            raw_events=raw_events,
        )
        assert result["events"] == raw_events
        assert result["raw_events_count"] == len(raw_events)

    def test_narrative_from_registry_used_in_steps(self):
        events = self._make_events()
        result = build_issue_detail_view_model(
            issue_number=42,
            title="Test",
            issue_url="https://github.com/test/42",
            events=events,
            phase_toc=[],
            cycles=[],
            view="user",
        )
        narratives = [s["narrative"] for s in result["timeline_steps"]]
        assert "Coding agent started" in narratives
        assert "Review approved" in narratives
        assert "PR created" in narratives


class TestRegistryCompleteness:
    """Verify the registry covers key events."""

    def test_all_views_are_valid(self):
        for internal_name, specs in VIEW_REGISTRY.items():
            for spec in specs:
                assert spec.views.issubset(VIEWS), (
                    f"{internal_name} -> {spec.name} has invalid views: {spec.views - VIEWS}"
                )

    def test_user_events_have_narratives(self):
        """All user-visible events should have a narrative."""
        for internal_name, specs in VIEW_REGISTRY.items():
            for spec in specs:
                if "user" in spec.views:
                    assert spec.narrative, (
                        f"{internal_name} -> {spec.name} is user-visible but has no narrative"
                    )

    def test_key_user_events_registered(self):
        """Critical user-facing moments are in the registry."""
        expected_user_events = {
            "agent.coding_started",
            "agent.coding_completed",
            "review.started",
            "review.approved",
            "review.changes_requested",
            "validation.passed",
            "pr.created",
            "agent.completed",
            "agent.failed",
        }
        actual_user_events = set()
        for specs in VIEW_REGISTRY.values():
            for spec in specs:
                if "user" in spec.views:
                    actual_user_events.add(spec.name)
        missing = expected_user_events - actual_user_events
        assert not missing, f"Missing user events: {missing}"
