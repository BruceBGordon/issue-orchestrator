"""Truthfulness checks for captured Timeline artifact materialization."""

from __future__ import annotations

import json

from tests.fixtures.timeline_run_artifacts import (
    TimelineFixturePathField,
    rewrite_timeline_fixture_path,
)


def test_start_completion_destination_is_rewritten_without_materialization(
    tmp_path,
) -> None:
    run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "run__coding"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    event_data = {
        "completion_path_absolute": "/captured/completion-agent.json",
    }

    rewritten = rewrite_timeline_fixture_path(
        field=TimelineFixturePathField.COMPLETION_PATH_ABSOLUTE,
        run_dir=run_dir,
        original_value=event_data["completion_path_absolute"],
        event_name="session.started",
        event_data=event_data,
    )

    assert rewritten == run_dir / "completion-agent.json"
    assert not rewritten.exists()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {}


def test_completed_event_materializes_its_completion_artifact(tmp_path) -> None:
    run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "run__coding"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    event_data = {
        "completion_path_absolute": "/captured/completion-record.json",
    }

    rewritten = rewrite_timeline_fixture_path(
        field=TimelineFixturePathField.COMPLETION_PATH_ABSOLUTE,
        run_dir=run_dir,
        original_value=event_data["completion_path_absolute"],
        event_name="session.completed",
        event_data=event_data,
    )

    assert rewritten.read_text(encoding="utf-8") == "{}\n"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["completion_path"] == str(rewritten)
