"""Timeline action web route tests split from test_web."""

# ruff: noqa: F403,F405,SLF001

from tests.unit import test_web as _support
from tests.unit.route_helpers import iter_route_paths
from tests.unit.test_web import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestTimelineActionWiring:
    """Validate every timeline action type is handled end-to-end.

    The pipeline is:
        backend emits action types → JS runTimelineEventAction dispatches
        → JS handler calls API endpoint → endpoint exists in FastAPI app.

    This test prevents wiring drift: if a new action type is emitted by
    the backend but never handled by the frontend, or if a handler
    references an endpoint that doesn't exist, the test fails.
    """

    # Complete registry: action type → API route pattern (or None for client-only)
    _ACTION_ENDPOINT_MAP: dict[str, str | None] = {
        "open_path": "/api/host/open-path",
        "open_url": None,  # client-side window.open, no HTTP call
        "open_review_feedback": None,  # in-app modal from existing issue detail payload
        "open_validation_failure": "/api/dialog/validation-failure/{issue_number}",
        "open_agent_log": "/api/session/terminal-recording/{issue_number}",
        "open_review_transcript": "/api/session/review-transcript/{issue_number}",
        "copy_agent_log": None,  # client-side fetch+clipboard from existing local log endpoint
        "view_claude_log": "/api/session/claude-log/{issue_number}",
        "open_orchestrator_log": "/api/session/orchestrator-log/{issue_number}",
        "open_session_diagnostics": "/api/dialog/session-diagnostics/{issue_number}",
        "show_event_details": None,  # client-side modal for row payload inspection
    }
    _REQUIRED_FIELDS_BY_ACTION: dict[str, tuple[str, ...]] = {
        "open_validation_failure": ("issue_number", "run_dir"),
        "open_agent_log": ("issue_number", "run_dir"),
        "open_review_transcript": ("issue_number", "run_dir"),
        "copy_agent_log": ("issue_number", "run_dir"),
        "view_claude_log": ("issue_number", "run_dir"),
        "open_orchestrator_log": ("issue_number", "run_dir"),
        "open_session_diagnostics": ("issue_number", "run_dir"),
    }

    def _write_review_phase_recording(
        self,
        run_dir: Path,
        *,
        round_index: int,
        role: str,
    ) -> None:
        recording = (
            run_dir
            / "review-exchange"
            / f"round-{round_index:03d}"
            / role
            / "terminal-recording.jsonl"
        )
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_text(
            '{"event_type":"output","offset_ms":0,"data_b64":"cmV2aWV3Cg==","schema_version":1}\n',
            encoding="utf-8",
        )

    def _collect_app_route_patterns(self) -> set[str]:
        """Extract all registered route patterns from the FastAPI app."""
        return set(iter_route_paths(app))

    def test_all_emitted_action_types_are_registered(self, tmp_path: Path) -> None:
        """Every action type produced by _timeline_event_actions must be
        in _ACTION_ENDPOINT_MAP so we know it has a JS handler."""
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-action-registry"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})
        run_dir = str(run.run_dir)

        # Generate actions for representative events to collect all possible types
        representative_events = [
            {"event": "session.started", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
            {"event": "review.comment_added", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
            {"event": "session.completed", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
            {"event": "session.failed", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
            {"event": "agent.invalid_completion_record", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
            {"event": "validation.failed", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
            {
                "event": "session.completed",
                "issue_number": 1,
                "run_dir": run_dir,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                "artifacts": [
                    {"type": "pull_request", "label": "PR", "value": "https://example.com/pr/1"},
                    {"type": "worktree", "label": "Worktree", "value": str(worktree)},
                ],
            },
        ]

        all_types: set[str] = set()
        for evt in representative_events:
            actions = _timeline_event_actions(evt, 1)
            for action in actions:
                all_types.add(action["type"])

        unhandled = all_types - set(self._ACTION_ENDPOINT_MAP)
        assert not unhandled, (
            f"Action types emitted by backend but missing from wiring registry: {unhandled}. "
            f"Add them to TestTimelineActionWiring._ACTION_ENDPOINT_MAP."
        )
        for evt in representative_events:
            actions = _timeline_event_actions(evt, 1)
            for action in actions:
                action_type = str(action.get("type") or "")
                required_fields = self._REQUIRED_FIELDS_BY_ACTION.get(action_type, ())
                missing_fields = [
                    field for field in required_fields
                    if field not in action or action.get(field) in (None, "")
                ]
                assert not missing_fields, (
                    f"Action type '{action_type}' missing required field(s) {missing_fields}: {action}"
                )

    def test_all_action_endpoints_exist_in_app(self) -> None:
        """Every action type that calls an API must have a matching route."""
        patterns = self._collect_app_route_patterns()

        for action_type, endpoint in self._ACTION_ENDPOINT_MAP.items():
            if endpoint is None:
                continue  # client-only action, no HTTP
            assert endpoint in patterns, (
                f"Action type '{action_type}' expects endpoint '{endpoint}' "
                f"but no matching route found in the FastAPI app."
            )

    def test_issue_detail_run_steps_carry_actions(self, tmp_path: Path) -> None:
        """Run cycle steps must pass through event actions for ⋯ menus."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        mock_orch.state.cached_queue_issues = [create_issue(123, "Wire Test")]
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-issue-detail-actions"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        stream = TimelineStream(
            issue_number=123,
            events=[
                TimelineEvent(
                    event_id="e1",
                    timestamp="2026-02-06T00:00:00Z",
                    event="session.started",
                    issue_number=123,
                    phase="in_progress",
                    step="started",
                    status="started",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    artifacts=[],
                    run_dir=str(run.run_dir),
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                    logical_run=1,
                    logical_cycle=1,
                    logical_phase="coding",
                ),
            ],
        )
        mock_orch.deps.timeline_reader.read.return_value = stream

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()
            assert payload["lifecycle"]["kind"] == "dashboard"
            lifecycle_issue = payload["lifecycle"]["current"]["issue_lifecycles"][0]
            assert lifecycle_issue["issue_number"] == 123

            # Run cycles must exist and carry actions on steps
            runs = payload.get("runs", [])
            assert len(runs) > 0, "Expected at least one run"
            cycles = runs[0].get("cycles", [])
            assert len(cycles) > 0, "Expected at least one cycle"
            steps = cycles[0].get("steps", [])
            assert len(steps) > 0, "Expected at least one step in cycle"

            step_actions = steps[0].get("actions", [])
            assert len(step_actions) > 0, (
                "Journey cycle steps must include actions for ⋯ menu rendering"
            )
            step_action_types = {a["type"] for a in step_actions}
            # Every step should have at least the default diagnostics actions
            assert "open_agent_log" in step_action_types
            assert "open_session_diagnostics" in step_action_types
        finally:
            set_orchestrator(None)

    def test_issue_detail_review_approved_uses_phase_scoped_reviewer_recording(self) -> None:
        mock_orch = create_mock_orchestrator()
        mock_orch.state.cached_queue_issues = [create_issue(4057, "Review Phase Recording")]

        run_dir = Path(_ensure_test_run_dir(4057))
        phase_recording = (
            run_dir
            / "review-exchange"
            / "round-002"
            / "reviewer"
            / "terminal-recording.jsonl"
        )
        phase_recording.parent.mkdir(parents=True, exist_ok=True)
        phase_recording.write_text(
            '{"event_type":"resize","rows":40,"cols":120,"offset_ms":0,"schema_version":1}\n',
            encoding="utf-8",
        )

        stream = TimelineStream(
            issue_number=4057,
            events=[
                TimelineEvent(
                    event_id="approved-1",
                    timestamp="2026-03-22T13:50:04.655598+00:00",
                    event="review.approved",
                    issue_number=4057,
                    phase="reviewing",
                    step="approved",
                    status="completed",
                    level="phase",
                    summary="Looks good now.",
                    parent_key="issue:4057",
                    artifacts=[],
                    run_dir=str(run_dir),
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    review_oriented=True,
                    event_intent="review",
                    logical_run=2,
                    logical_cycle=2,
                    logical_phase="review",
                    narrative="Review approved after 2 rounds",
                    rounds=2,
                ),
            ],
        )
        mock_orch.deps.timeline_reader.read.return_value = stream

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/4057")
            assert response.status_code == 200
            payload = response.json()
            steps = payload["runs"][0]["cycles"][0]["steps"]
            approved_step = next(step for step in steps if step["event"] == "review.approved")
            review_action = next(
                action for action in approved_step["actions"] if action["type"] == "open_agent_log"
            )
            assert review_action["round_index"] == 2
            assert review_action["session_role"] == "reviewer"
        finally:
            set_orchestrator(None)

    def test_timeline_action_wiring_rejects_unsupported_event_versions(self) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions

        with pytest.raises(RuntimeError, match="unsupported schema version"):
            _timeline_event_actions(
                {
                    "event": "session.started",
                    "issue_number": 4057,
                    "timeline_schema_version": 1,
                    "run_dir": "/tmp/wt/.issue-orchestrator/sessions/20260216-000000Z__coding-1",
                },
                4057,
            )

    def test_timeline_artifact_types_produce_viewable_actions(self, tmp_path: Path) -> None:
        """All known timeline artifact types should map to a usable UI action."""
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-artifact-actions"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-4057", issue_number=4057)
        run_dir = str(run.run_dir)
        (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        completion_path = run.run_dir / "completion.json"
        completion_path.write_text('{"status":"completed"}\n', encoding="utf-8")
        validation_path = run.run_dir / "validation.json"
        validation_path.write_text('{"ok":true}\n', encoding="utf-8")
        diagnostic_path = run.run_dir / "invalid-completion-1.json"
        diagnostic_path.write_text('{"kind":"invalid-completion-record"}\n', encoding="utf-8")
        session_output.update_manifest(
            run.run_dir,
            {
                "completion_path": str(completion_path),
                "validation_record_path": str(validation_path),
                "diagnostic_path": str(diagnostic_path),
            },
        )

        event = {
            "event": "review.comment_added",
            "issue_number": 4057,
            "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            "run_dir": run_dir,
            "artifacts": [
                {"type": "pull_request", "label": "PR", "value": "https://github.com/org/repo/pull/4124"},
                {"type": "review_comment", "label": "Review Comment", "value": "https://github.com/org/repo/pull/4124#discussion_r1"},
                {"type": "completion_record", "label": "Completion", "value": str(completion_path)},
                {"type": "diagnostic", "label": "Invalid Completion Diagnostic", "value": str(diagnostic_path)},
                {"type": "worktree", "label": "Worktree", "value": str(worktree)},
                {"type": "validation", "label": "Validation", "value": str(validation_path)},
                {"type": "run_dir", "label": "Run Dir", "value": run_dir},
            ],
        }
        actions = _timeline_event_actions(event, 4057)
        assert actions, "Expected at least one action from timeline event artifacts"

        open_url_labels = {
            action["label"]
            for action in actions
            if action.get("type") == "open_url"
        }
        open_paths = {
            action["path"]
            for action in actions
            if action.get("type") == "open_path"
        }
        run_scoped = {
            action["type"]
            for action in actions
            if action.get("run_dir") == run_dir
        }
        assert "Open PR ↗" in open_url_labels
        assert "Open Review Comment ↗" in open_url_labels
        assert str(completion_path) in open_paths
        assert str(diagnostic_path) in open_paths
        assert str(worktree) in open_paths
        assert str(validation_path) in open_paths
        assert run_dir in open_paths
        assert "open_agent_log" in run_scoped
        assert "view_claude_log" in run_scoped
        assert "open_orchestrator_log" in run_scoped

    def test_available_run_with_missing_local_artifact_fails_fast(
        self, tmp_path: Path
    ) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        worktree = tmp_path / "wt-missing-local-artifact"
        worktree.mkdir()
        run = FileSystemSessionOutput().start_run(
            worktree, "issue-4057", issue_number=4057
        )
        missing_completion = worktree / ".issue-orchestrator" / "completion.json"

        with pytest.raises(
            RuntimeError, match="timeline event references missing local artifact"
        ):
            _timeline_event_actions(
                {
                    "event": "issue.unblocked",
                    "issue_number": 4057,
                    "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                    "run_dir": str(run.run_dir),
                    "artifacts": [
                        {
                            "type": "completion_record",
                            "label": "Completion",
                            "value": str(missing_completion),
                        }
                    ],
                },
                4057,
            )

    def test_agent_log_action_label_matches_event_context(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-agent-log-labels"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
        review_transcript = run.run_dir / "review-exchange" / "transcript.log"
        review_transcript.parent.mkdir(parents=True, exist_ok=True)
        review_transcript.write_text("review exchange status=ok\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(
            run.run_dir,
            {
                "claude_log_path": str(claude_log),
                "review_exchange_transcript_path": str(review_transcript),
            },
        )
        run_dir = str(run.run_dir)

        review_actions = _timeline_event_actions({"event": "review.approved", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION}, 1)
        coding_actions = _timeline_event_actions({"event": "session.started", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION}, 1)
        rework_actions = _timeline_event_actions({"event": "rework.started", "issue_number": 1, "run_dir": run_dir, "timeline_schema_version": TIMELINE_SCHEMA_VERSION}, 1)
        fallback_actions = _timeline_event_actions({"event": "issue.unblocked", "issue_number": 1, "timeline_schema_version": TIMELINE_SCHEMA_VERSION}, 1)

        def _label(actions: list[dict[str, Any]], action_type: str) -> str:
            return next(action["label"] for action in actions if action.get("type") == action_type)

        assert _label(review_actions, "open_agent_log") == "View Reviewer Session Recording"
        assert _label(review_actions, "open_review_transcript") == "View Review Transcript"
        assert _label(coding_actions, "open_agent_log") == "View Coding Session Recording"
        assert _label(rework_actions, "open_agent_log") == "View Rework Session Recording"
        assert all(action.get("type") != "open_agent_log" for action in fallback_actions)

    def test_review_events_offer_session_recording_and_review_transcript_when_present(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-review-transcript-actions"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "review-1", issue_number=1)
        transcript = run.run_dir / "review-exchange" / "transcript.log"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("review exchange status=ok\n", encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"review_exchange_transcript_path": str(transcript)})

        actions = _timeline_event_actions(
            {
                "event": "review.approved",
                "issue_number": 1,
                "run_dir": str(run.run_dir),
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        action_types = {action.get("type") for action in actions}
        assert "open_review_transcript" in action_types
        assert "open_agent_log" in action_types

    def test_review_exchange_aggregate_rows_do_not_open_run_summary_as_session_recording(
        self,
        tmp_path: Path,
    ) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
        from issue_orchestrator.ports.event_sink import (
            make_review_exchange_completed_event,
        )

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-review-exchange-aggregate"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "review-aggregate", issue_number=4057)
        (run.run_dir / "terminal-recording.jsonl").write_text(
            "review-exchange status=ok reason=reviewer_ok\n",
            encoding="utf-8",
        )
        transcript = run.run_dir / "review-exchange" / "transcript.log"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("round 1 reviewer ok\n", encoding="utf-8")
        session_output.update_manifest(
            run.run_dir,
            {"review_exchange_transcript_path": str(transcript)},
        )

        completed_event = make_review_exchange_completed_event(
            {
                "issue_number": 4057,
                "run_dir": str(run.run_dir),
                "session_name": "review-aggregate",
                "rounds": 1,
                "status": "ok",
                "reason": "reviewer_ok",
            }
        )
        completed_actions = _timeline_event_actions(
            {
                "event": completed_event.name,
                **completed_event.data,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            4057,
        )
        completed_types = {action.get("type") for action in completed_actions}
        assert "open_review_transcript" in completed_types
        assert "open_agent_log" not in completed_types
        transcript_action = next(
            action
            for action in completed_actions
            if action.get("type") == "open_review_transcript"
        )
        assert transcript_action["issue_number"] == 4057
        assert transcript_action["run_dir"] == str(run.run_dir)

        started_actions = _timeline_event_actions(
            {
                "event": "review.started",
                "issue_number": 4057,
                "run_dir": str(run.run_dir),
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                "review_exchange_mode": "via-local-loop",
            },
            4057,
        )
        assert all(action.get("type") != "open_agent_log" for action in started_actions)

    def test_review_events_keep_agent_log_when_optional_transcript_is_missing(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-review-transcript-optional"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "review-1", issue_number=1)

        actions = _timeline_event_actions(
            {
                "event": "review.approved",
                "issue_number": 1,
                "run_dir": str(run.run_dir),
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        action_types = {action.get("type") for action in actions}
        assert "open_review_transcript" not in action_types
        assert "open_agent_log" in action_types

    def test_validation_failed_events_offer_validation_details(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-validation-actions"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "coding-1", issue_number=1)

        actions = _timeline_event_actions(
            {
                "event": "validation.failed",
                "issue_number": 1,
                "run_dir": str(run.run_dir),
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )

        validation_action = next(
            action for action in actions if action.get("type") == "open_validation_failure"
        )
        assert validation_action["label"] == "Validation Details"
        assert validation_action["run_dir"] == str(run.run_dir)

    def test_validation_passed_events_offer_validation_details(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-validation-passed-actions"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "coding-1", issue_number=1)

        for event_name in ("validation.passed", "session.validation_passed"):
            actions = _timeline_event_actions(
                {
                    "event": event_name,
                    "issue_number": 1,
                    "run_dir": str(run.run_dir),
                    "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                },
                1,
            )
            validation_action = next(
                action for action in actions if action.get("type") == "open_validation_failure"
            )
            assert validation_action["label"] == "Validation Details", event_name

    def test_in_flight_validation_events_do_not_offer_validation_details(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-validation-inflight-actions"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "coding-1", issue_number=1)

        # `retry`, `started`, `completed` are in-flight signals — there's no
        # finalized validation-record.json yet, so the dialog has nothing to
        # render. The orchestrator-log action remains so users can still trace
        # what happened.
        for event_name in (
            "validation.retry",
            "validation.started",
            "validation.completed",
        ):
            actions = _timeline_event_actions(
                {
                    "event": event_name,
                    "issue_number": 1,
                    "run_dir": str(run.run_dir),
                    "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                },
                1,
            )
            action_types = {action.get("type") for action in actions}
            assert "open_validation_failure" not in action_types, event_name
            assert "open_orchestrator_log" in action_types, event_name

    def test_review_transcript_actions_bind_round_and_role_context(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-review-transcript-context"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "review-1", issue_number=4057)
        exchange_dir = run.run_dir / "review-exchange"
        exchange_dir.mkdir(parents=True, exist_ok=True)
        transcript = exchange_dir / "transcript.log"
        transcript.touch(exist_ok=True)
        session_output.update_manifest(
            run.run_dir,
            {"review_exchange_transcript_path": str(transcript)},
        )
        transcript.write_text(
            "[2026-03-20T04:39:32Z] round=2 role=reviewer section=prompt\nPrompt\n",
            encoding="utf-8",
        )
        self._write_review_phase_recording(run.run_dir, round_index=2, role="reviewer")
        self._write_review_phase_recording(run.run_dir, round_index=2, role="coder")

        review_round_actions = _timeline_event_actions(
            {
                "event": "review_exchange.round_completed",
                "issue_number": 4057,
                "run_dir": str(run.run_dir),
                "round_index": 2,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            4057,
        )
        review_action = next(
            action for action in review_round_actions if action.get("type") == "open_review_transcript"
        )
        assert review_action["round_index"] == 2
        assert review_action["transcript_role"] == "reviewer"

        rework_actions = _timeline_event_actions(
            {
                "event": "review.rework_started",
                "issue_number": 4057,
                "run_dir": str(run.run_dir),
                "round_index": 2,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            4057,
        )
        rework_action = next(
            action for action in rework_actions if action.get("type") == "open_review_transcript"
        )
        assert rework_action["round_index"] == 2
        assert rework_action["transcript_role"] == "coder"

        approved_actions = _timeline_event_actions(
            {
                "event": "review.approved",
                "issue_number": 4057,
                "run_dir": str(run.run_dir),
                "rounds": 2,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            4057,
        )
        approved_action = next(
            action for action in approved_actions if action.get("type") == "open_review_transcript"
        )
        assert approved_action["round_index"] == 2
        assert approved_action["transcript_role"] == "reviewer"

    def test_review_session_recording_actions_bind_round_and_role_context(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-review-recording-context"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "review-1", issue_number=4057)
        reviewer_recording = run.run_dir / "review-exchange" / "round-002" / "reviewer" / "terminal-recording.jsonl"
        reviewer_recording.parent.mkdir(parents=True, exist_ok=True)
        reviewer_recording.write_text(
            '{"event_type":"output","offset_ms":0,"data_b64":"aGVsbG8K","schema_version":1}\n',
            encoding="utf-8",
        )
        coder_recording = run.run_dir / "review-exchange" / "round-002" / "coder" / "terminal-recording.jsonl"
        coder_recording.parent.mkdir(parents=True, exist_ok=True)
        coder_recording.write_text(
            '{"event_type":"output","offset_ms":0,"data_b64":"Y29kZXIK","schema_version":1}\n',
            encoding="utf-8",
        )

        review_round_actions = _timeline_event_actions(
            {
                "event": "review_exchange.round_completed",
                "issue_number": 4057,
                "run_dir": str(run.run_dir),
                "round_index": 2,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            4057,
        )
        review_action = next(
            action for action in review_round_actions if action.get("type") == "open_agent_log"
        )
        assert review_action["round_index"] == 2
        assert review_action["session_role"] == "reviewer"

        rework_actions = _timeline_event_actions(
            {
                "event": "review.rework_started",
                "issue_number": 4057,
                "run_dir": str(run.run_dir),
                "round_index": 2,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            4057,
        )
        rework_action = next(
            action for action in rework_actions if action.get("type") == "open_agent_log"
        )
        assert rework_action["round_index"] == 2
        assert rework_action["session_role"] == "coder"

        approved_actions = _timeline_event_actions(
            {
                "event": "review.approved",
                "issue_number": 4057,
                "run_dir": str(run.run_dir),
                "rounds": 2,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            4057,
        )
        approved_action = next(
            action for action in approved_actions if action.get("type") == "open_agent_log"
        )
        assert approved_action["round_index"] == 2
        assert approved_action["session_role"] == "reviewer"

    def test_review_feedback_actions_bind_to_specific_timeline_entry(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-review-feedback-actions"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-4057", issue_number=4057)
        (run.run_dir / "ui-session.log").write_text("review output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})
        run_dir = str(run.run_dir)
        self._write_review_phase_recording(run.run_dir, round_index=2, role="reviewer")

        round_completed_actions = _timeline_event_actions(
            {
                "event": "review_exchange.round_completed",
                "issue_number": 4057,
                "run_dir": run_dir,
                "timestamp": "2026-03-20T04:40:42Z",
                "round_index": 2,
                "reviewer_response_text": "Looks good.",
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            4057,
        )
        feedback_action = next(
            action for action in round_completed_actions if action.get("type") == "open_review_feedback"
        )
        assert feedback_action["feedback_event"] == "review_exchange.round_completed"
        assert feedback_action["event_timestamp"] == "2026-03-20T04:40:42Z"
        assert feedback_action["round_index"] == 2

        round_started_actions = _timeline_event_actions(
            {
                "event": "review_exchange.round_started",
                "issue_number": 4057,
                "run_dir": run_dir,
                "timestamp": "2026-03-20T04:39:32Z",
                "round_index": 2,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            4057,
        )
        assert all(action.get("type") != "open_review_feedback" for action in round_started_actions)

    def test_run_scoped_timeline_actions_require_run_dir(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        with pytest.raises(RuntimeError, match="missing required run_dir"):
            _timeline_event_actions(
                {"event": "session.started", "issue_number": 1, "timeline_schema_version": TIMELINE_SCHEMA_VERSION},
                1,
            )

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-run-dir-required"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        actions_with_run_dir = _timeline_event_actions(
            {
                "event": "session.started",
                "issue_number": 1,
                "run_dir": str(run.run_dir),
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        types_with_run_dir = {action.get("type") for action in actions_with_run_dir}
        assert "open_agent_log" in types_with_run_dir
        assert "view_claude_log" in types_with_run_dir

    def test_run_scoped_start_events_allow_session_log_even_when_sparse(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        run_dir = str(run.run_dir)

        # Start events should expose agent log when a populated run-scoped log exists.
        provider_stdout = run.run_dir / "provider-runner" / "stdout.log"
        provider_stdout.parent.mkdir(parents=True, exist_ok=True)
        provider_stdout.write_text("provider output\n", encoding="utf-8")
        sparse_actions = _timeline_event_actions(
            {
                "event": "session.started",
                "issue_number": 1,
                "run_dir": run_dir,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        sparse_types = {action.get("type") for action in sparse_actions}
        assert "open_agent_log" in sparse_types
        assert "view_claude_log" not in sparse_types

        # Add usable agent log + claude log manifest binding.
        (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        actions_present = _timeline_event_actions(
            {
                "event": "session.started",
                "issue_number": 1,
                "run_dir": run_dir,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        present_types = {action.get("type") for action in actions_present}
        assert "open_agent_log" in present_types
        assert "view_claude_log" in present_types

    def test_run_scoped_start_events_keep_session_log_action_when_log_files_empty(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-empty-agent-log"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        run_dir = str(run.run_dir)

        # Keep run-scoped agent log candidates present but empty.
        (run.run_dir / "ui-session.log").write_text("", encoding="utf-8")
        provider_stdout = run.run_dir / "provider-runner" / "stdout.log"
        provider_stdout.parent.mkdir(parents=True, exist_ok=True)
        provider_stdout.write_text("", encoding="utf-8")

        # Populate claude log so only agent-log behavior is under test.
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        actions = _timeline_event_actions(
            {
                "event": "session.started",
                "issue_number": 1,
                "run_dir": run_dir,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        action_types = {action.get("type") for action in actions}
        assert "open_agent_log" in action_types
        assert "show_actions_error" not in action_types

    def test_run_scoped_non_start_events_keep_session_log_action_when_log_empty(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-empty-agent-log-nonstart"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        run_dir = str(run.run_dir)

        (run.run_dir / "ui-session.log").write_text("", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        actions = _timeline_event_actions(
            {
                "event": "review.comment_added",
                "issue_number": 1,
                "run_dir": run_dir,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                "event_intent": "review",
                "review_oriented": True,
                "logical_run": 1,
                "logical_cycle": 1,
                "logical_phase": "review",
            },
            1,
        )
        action_types = {action.get("type") for action in actions}
        assert "open_agent_log" in action_types

    def test_run_scoped_actions_keep_agent_log_when_claude_log_missing(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-claude-missing"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
        # Intentionally do not attach claude_log_path in manifest.

        actions = _timeline_event_actions(
            {
                "event": "session.started",
                "issue_number": 1,
                "run_dir": str(run.run_dir),
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        action_types = {action.get("type") for action in actions}
        assert "open_agent_log" in action_types
        assert "open_session_diagnostics" in action_types
        assert "view_claude_log" not in action_types
        assert "show_actions_error" not in action_types

    def test_review_oriented_non_session_event_without_run_dir_keeps_non_run_actions(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        actions_without_run_dir = _timeline_event_actions(
            {
                "event": "review.comment_added",
                "issue_number": 1,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                "event_intent": "review",
                "review_oriented": True,
                "logical_run": 1,
                "logical_cycle": 1,
                "logical_phase": "review",
            },
            1,
        )
        non_run_scoped_types = [action.get("type") for action in actions_without_run_dir]
        assert non_run_scoped_types == [
            "open_review_feedback",
        ]

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-run-warning"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        actions_with_run_dir = _timeline_event_actions(
            {
                "event": "review.comment_added",
                "issue_number": 1,
                "run_dir": str(run.run_dir),
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        run_scoped_types = {action.get("type") for action in actions_with_run_dir}
        assert "open_agent_log" in run_scoped_types
        assert "view_claude_log" in run_scoped_types

    @pytest.mark.parametrize(
        "event_name",
        (
            "review_exchange.round_started",
            "review_exchange.round_completed",
            "review_exchange.role_prompted",
            "review_exchange.role_feedback",
            "review_exchange.role_timeout",
            "review.rework_started",
            "review.rework_completed",
        ),
    )
    def test_review_phase_log_events_without_run_dir_fail_fast(self, event_name: str) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions

        with pytest.raises(RuntimeError, match="timeline event missing required run_dir"):
            _timeline_event_actions(
                {
                    "event": event_name,
                    "issue_number": 1,
                    "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                    "round_index": 1,
                    "logical_run": 1,
                    "logical_cycle": 1,
                    "logical_phase": "review",
                },
                1,
            )

    def test_review_phase_log_event_with_missing_round_recording_fails_fast(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-missing-round-recording"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "review-1", issue_number=1)
        (run.run_dir / "ui-session.log").write_text("review output\n", encoding="utf-8")

        with pytest.raises(
            RuntimeError, match="required Timeline phase recording is missing"
        ):
            _timeline_event_actions(
                {
                    "event": "review_exchange.round_completed",
                    "issue_number": 1,
                    "run_dir": str(run.run_dir),
                    "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                    "round_index": 2,
                    "logical_run": 1,
                    "logical_cycle": 1,
                    "logical_phase": "review",
                },
                1,
            )

    def test_review_exchange_aggregate_row_does_not_open_run_level_recording(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-review-aggregate-row"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "review-exchange-1", issue_number=1)
        (run.run_dir / "terminal-recording.jsonl").write_text(
            '{"event_type":"output","offset_ms":0,"data_b64":"c3RhdHVzPW9rCg==","schema_version":1}\n',
            encoding="utf-8",
        )

        actions = _timeline_event_actions(
            {
                "event": "review_exchange.completed",
                "issue_number": 1,
                "run_dir": str(run.run_dir),
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                "status": "completed",
                "logical_run": 1,
                "logical_cycle": 1,
                "logical_phase": "review",
            },
            1,
        )

        assert [a for a in actions if a.get("type") == "open_agent_log"] == []

    def test_decorate_timeline_events_does_not_hide_missing_required_run_dir(self) -> None:
        from issue_orchestrator.entrypoints.web import _decorate_timeline_events

        events = [
            {
                "event": "session.started",
                "issue_number": 4057,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                "artifacts": [
                    {"type": "worktree", "label": "Worktree", "value": "/tmp/wt-4057"},
                ],
            }
        ]

        with pytest.raises(RuntimeError, match="missing required run_dir"):
            _decorate_timeline_events(events, 4057)


class TestPerRoundLogActions:
    """Pin replacement of stale per-round path guessing with run-scoped sessions."""

    def test_review_exchange_rows_emit_run_scoped_replacement_actions(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-review-exchange-replacement-actions"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "review-1", issue_number=1)
        for role in ("reviewer", "coder"):
            recording = (
                run.run_dir
                / "review-exchange"
                / "round-001"
                / role
                / "terminal-recording.jsonl"
            )
            recording.parent.mkdir(parents=True, exist_ok=True)
            recording.write_text(
                '{"event_type":"output","offset_ms":0,"data_b64":"cmV2aWV3Cg==","schema_version":1}\n',
                encoding="utf-8",
            )

        cases = (
            ("review_exchange.round_started", {"round_index": 1}, "reviewer"),
            ("review_exchange.round_completed", {"round_index": 1}, "reviewer"),
            ("review.rework_completed", {"round_index": 1}, "coder"),
            ("review_exchange.role_prompted", {"round_index": 1, "role": "reviewer"}, "reviewer"),
        )
        for event_name, fields, expected_role in cases:
            actions = _timeline_event_actions(
                {
                    "event": event_name,
                    "issue_number": 1,
                    "run_dir": str(run.run_dir),
                    "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                    **fields,
                },
                1,
            )
            assert [
                action for action in actions
                if action.get("type") == "open_path"
                and str(action.get("path", "")).endswith("/agent-output.log")
            ] == [], event_name
            session_action = next(
                action for action in actions if action.get("type") == "open_agent_log"
            )
            assert session_action["run_dir"] == str(run.run_dir)
            assert session_action["round_index"] == 1
            assert session_action["session_role"] == expected_role

    def test_required_round_event_rejects_missing_run_dir(self) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions

        with pytest.raises(RuntimeError, match="missing required run_dir"):
            _timeline_event_actions(
                {
                    "event": "review_exchange.round_started",
                    "issue_number": 1,
                    "round_index": 1,
                    "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                },
                1,
            )

    def test_missing_run_keeps_durable_actions_only(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions

        deleted_run = tmp_path / "deleted-run"
        actions = _timeline_event_actions(
            {
                "event": "review_exchange.round_completed",
                "issue_number": 1,
                "run_dir": str(deleted_run),
                "round_index": 1,
                "reviewer_response_text": "Approved",
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                "artifacts": [
                    {
                        "type": "pull_request",
                        "label": "PR",
                        "value": "https://example.com/pull/1",
                    },
                    {
                        "type": "review_report",
                        "label": "Review report",
                        "value": str(deleted_run / "review.md"),
                    },
                ],
            },
            1,
        )

        assert {action["type"] for action in actions} == {
            "open_review_feedback",
            "open_url",
        }


    def test_local_artifact_type_cannot_disguise_url_as_durable(self, tmp_path: Path) -> None:
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run = FileSystemSessionOutput().start_run(
            worktree,
            "issue-1",
            issue_number=1,
        )

        with pytest.raises(RuntimeError, match="local artifact path is not absolute"):
            _timeline_event_actions(
                {
                    "event": "issue.unblocked",
                    "issue_number": 1,
                    "run_dir": str(run.run_dir),
                    "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
                    "artifacts": [
                        {
                            "type": "completion_record",
                            "label": "Completion",
                            "value": "https://example.com/not-a-completion-record",
                        }
                    ],
                },
                1,
            )


class TestIsAgentScopedEvent:
    """Test _is_agent_scoped_event gating of session log buttons."""

    def test_orchestrator_only_events_return_false(self) -> None:
        from issue_orchestrator.entrypoints.web import _is_agent_scoped_event

        for event_name in [
            "validation.passed", "validation.failed", "pr.created",
            "agent.completed", "issue.completed", "issue.blocked",
        ]:
            assert not _is_agent_scoped_event(
                {"event_intent": "orchestrator"}, event_name
            ), f"{event_name} should not be agent-scoped"

    def test_coding_intent_returns_true(self) -> None:
        from issue_orchestrator.entrypoints.web import _is_agent_scoped_event

        assert _is_agent_scoped_event(
            {"event_intent": "coding"}, "agent.coding_started"
        )

    def test_review_intent_returns_true(self) -> None:
        from issue_orchestrator.entrypoints.web import _is_agent_scoped_event

        assert _is_agent_scoped_event(
            {"event_intent": "review"}, "review.started"
        )

    def test_rework_intent_returns_true(self) -> None:
        from issue_orchestrator.entrypoints.web import _is_agent_scoped_event

        assert _is_agent_scoped_event(
            {"event_intent": "rework"}, "rework.started"
        )

    def test_review_event_name_without_intent_returns_true(self) -> None:
        """Events without event_intent fall back to name pattern matching."""
        from issue_orchestrator.entrypoints.web import _is_agent_scoped_event

        assert _is_agent_scoped_event({}, "review.comment_added")

    def test_review_exchange_event_without_intent_returns_true(self) -> None:
        from issue_orchestrator.entrypoints.web import _is_agent_scoped_event

        assert _is_agent_scoped_event({}, "review_exchange.round_started")

    def test_rework_event_without_intent_returns_true(self) -> None:
        from issue_orchestrator.entrypoints.web import _is_agent_scoped_event

        assert _is_agent_scoped_event({}, "rework.started")

    def test_agent_prefixed_event_without_intent_returns_true(self) -> None:
        from issue_orchestrator.entrypoints.web import _is_agent_scoped_event

        assert _is_agent_scoped_event({}, "agent.coding_started")

    def test_unknown_event_without_intent_returns_false(self) -> None:
        from issue_orchestrator.entrypoints.web import _is_agent_scoped_event

        assert not _is_agent_scoped_event({}, "some.unknown.event")

    def test_orchestrator_event_with_agent_intent_still_blocked(self) -> None:
        """_ORCHESTRATOR_ONLY_EVENTS takes precedence over event_intent."""
        from issue_orchestrator.entrypoints.web import _is_agent_scoped_event

        assert not _is_agent_scoped_event(
            {"event_intent": "coding"}, "validation.passed"
        )

    def test_gating_excludes_session_log_for_orchestrator_events(self, tmp_path: Path) -> None:
        """Integration: orchestrator events with run_dir should NOT get agent log buttons."""
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-orch"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        (run.run_dir / "ui-session.log").write_text("log\n", encoding="utf-8")
        run_dir = str(run.run_dir)

        actions = _timeline_event_actions(
            {
                "event": "validation.passed",
                "event_intent": "orchestrator",
                "issue_number": 1,
                "run_dir": run_dir,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        action_types = {a["type"] for a in actions}
        assert "open_orchestrator_log" in action_types
        assert "open_agent_log" not in action_types
        assert "view_claude_log" not in action_types

    def test_gating_includes_session_log_for_agent_events(self, tmp_path: Path) -> None:
        """Integration: agent events with run_dir SHOULD get agent log buttons."""
        from issue_orchestrator.entrypoints.web import _timeline_event_actions
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-agent"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-1", issue_number=1)
        (run.run_dir / "ui-session.log").write_text("log\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})
        run_dir = str(run.run_dir)

        actions = _timeline_event_actions(
            {
                "event": "review.started",
                "event_intent": "review",
                "issue_number": 1,
                "run_dir": run_dir,
                "timeline_schema_version": TIMELINE_SCHEMA_VERSION,
            },
            1,
        )
        action_types = {a["type"] for a in actions}
        assert "open_agent_log" in action_types
        assert "view_claude_log" in action_types
