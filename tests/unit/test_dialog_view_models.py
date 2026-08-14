import pytest

from issue_orchestrator.view_models.dialogs import (
    _build_validation_failure_action_sections,
    build_blocked_issues_dialog,
    build_config_dialog,
    build_debug_dialog,
    build_doctor_dialog,
    build_info_dialog,
    build_phase_dialog,
    build_session_diagnostics_dialog,
    build_validation_failure_dialog,
)


def _rows_to_map(rows):
    return {row["label"]: row["value"] for row in rows}


def test_build_info_dialog_defaults():
    dialog = build_info_dialog({})

    assert dialog["title"] == "About Issue Orchestrator"
    rows = _rows_to_map(dialog["rows"])
    assert rows["Version"] == "dev"
    assert rows["Commit"] == "unknown"
    assert rows["Max Sessions"] == "-"
    assert rows["Active Sessions"] == "0"


def test_build_info_dialog_values():
    dialog = build_info_dialog(
        {
            "version": "1.2.3",
            "repo": "repo",
            "ui_mode": "web",
            "terminal_backend": "tmux",
            "commit_short": "abcd123",
            "max_sessions": 3,
            "active_sessions": 2,
            "completed_today": 5,
        }
    )

    rows = _rows_to_map(dialog["rows"])
    assert rows["Version"] == "1.2.3"
    assert rows["Repository"] == "repo"
    assert rows["UI Mode"] == "web"
    assert rows["Terminal"] == "tmux"
    assert rows["Commit"] == "abcd123"
    assert rows["Max Sessions"] == "3"
    assert rows["Active Sessions"] == "2"
    assert rows["Completed Today"] == "5"


def test_build_config_dialog():
    dialog = build_config_dialog("config text")

    assert dialog == {
        "title": "Configuration",
        "config_text": "config text",
    }


def test_build_debug_dialog_sections():
    dialog = build_debug_dialog(
        {
            "startup_options": {
                "ui_mode": "web",
                "web_port": 8080,
                "test_mode": True,
                "filtering": {
                    "label": "bug",
                    "milestone": "v1",
                },
                "max_sessions": 4,
            },
            "paused": False,
            "priority_queue": ["M1-1", "M1-2"],
            "config_path": "/config/path",
            "repo_root": "/repo/root",
            "agents": {"default": {"timeout": 15}},
        }
    )

    assert dialog["title"] == "Debug Info"
    sections = {section["title"]: section for section in dialog["sections"]}

    startup_rows = _rows_to_map(sections["Startup Options"]["rows"])
    assert startup_rows["UI Mode"] == "web"
    assert startup_rows["Web Port"] == "8080"
    assert startup_rows["Test Mode"] == "yes"
    assert startup_rows["Filter Label"] == "bug"
    assert startup_rows["Filter Milestone"] == "v1"
    assert startup_rows["Max Sessions"] == "4"

    state_rows = _rows_to_map(sections["State"]["rows"])
    assert state_rows["Paused"] == "False"
    assert state_rows["Priority Queue"] == "M1-1, M1-2"

    path_rows = _rows_to_map(sections["Paths"]["rows"])
    assert path_rows["Config Path"] == "/config/path"
    assert path_rows["Repo Root"] == "/repo/root"

    agent_rows = _rows_to_map(sections["Agent Types"]["rows"])
    assert agent_rows["default"] == "timeout: 15m"


def test_build_debug_dialog_defaults():
    dialog = build_debug_dialog({})

    sections = {section["title"]: section for section in dialog["sections"]}
    startup_rows = _rows_to_map(sections["Startup Options"]["rows"])
    assert startup_rows["Filter Label"] == "none"
    assert startup_rows["Filter Milestone"] == "none"

    state_rows = _rows_to_map(sections["State"]["rows"])
    assert state_rows["Priority Queue"] == "empty"


def test_build_doctor_dialog():
    dialog = build_doctor_dialog(
        {
            "overall": "warning",
            "checks": [
                {"name": "git", "status": "ok", "detail": "good"},
                {"name": "gh", "status": "error", "detail": "missing"},
            ],
        }
    )

    assert dialog["title"] == "Doctor"
    assert dialog["overall"] == "warning"
    assert dialog["checks"] == [
        {"name": "git", "status": "ok", "detail": "good"},
        {"name": "gh", "status": "error", "detail": "missing"},
    ]


def test_build_session_diagnostics_dialog_actions():
    dialog = build_session_diagnostics_dialog(
        42,
        {
            "manifest": {
                "session_name": "sess-1",
                "started_at": "2024-01-01",
                "run_id": "run-1",
                "backend": "tmux",
                "agent_label": "codex",
                "claude_session_id": "cl-1",
                "worktree": "/wt",
                "diagnostic_path": "diag/diagnostic.json",
                "run_audit_path": "diag/run-audit.json",
                "claude_log_path": "/logs/claude.log",
                "claude_log_dir": "/logs",
                "orchestrator_log": "/logs/orch.log",
                "validation_record_path": "validate.json",
                "validation_stdout": "validation-output.log",
                "validation_stderr": "validation-stderr.log",
                "validation_status": "failed",
                "validation_reason": "Missing packages/vscode/node_modules",
                "follow_up_issues": [
                    {
                        "title": "Open follow-up for env-sensitive test isolation",
                        "reason": "A failing test was discovered but was unrelated to the assigned issue.",
                        "suggested_labels": ["bug", "tests"],
                        "blocking": False,
                    }
                ],
            },
            "session_identity": {
                "task": "code",
                "branch": "4057-scratch",
                "provider": "claude-code",
                "model": "sonnet",
                "permission_mode": "bypassPermissions",
                "timeout_minutes": 60,
                "extra_provider_args": {"verbose": "true"},
                "claude_args": "",
                "claude_prompt_mode": "arg",
            },
            "analysis": {
                "headline": "Validation failed: Missing packages/vscode/node_modules",
                "detail": "Install the worktree dependencies before running make validate.",
                "suggestions": ["Run make worktree-setup"],
            },
            "run_dir": "/run/dir",
            "session_name": "fallback",
        },
    )

    rows = _rows_to_map(dialog["rows"])
    assert rows["Session"] == "sess-1"
    assert rows["Worktree"] == "/wt"
    assert rows["Provider"] == "claude-code"
    assert rows["Model"] == "sonnet"
    assert rows["Permission Mode"] == "bypassPermissions"
    assert rows["Timeout"] == "60m"
    assert rows["Provider Args"] == "verbose=true"
    assert rows["Prompt Mode"] == "arg"
    assert rows["Validation Status"] == "failed"
    assert rows["Validation Reason"] == "Missing packages/vscode/node_modules"
    assert dialog["analysis"]["headline"] == "Validation failed: Missing packages/vscode/node_modules"
    assert dialog["follow_up_issues"] == [
        {
            "title": "Open follow-up for env-sensitive test isolation",
            "reason": "A failing test was discovered but was unrelated to the assigned issue.",
            "suggested_labels": ["bug", "tests"],
            "blocking": False,
        }
    ]

    action_types = [action["type"] for action in dialog["actions"]]
    assert "open_path" in action_types
    assert "open_agent_log" in action_types
    assert "copy_agent_log" in action_types
    assert "view_claude_log" in action_types
    assert "open_orchestrator_log" in action_types
    agent_log_action = next(action for action in dialog["actions"] if action["type"] == "open_agent_log")
    claude_action = next(action for action in dialog["actions"] if action["type"] == "view_claude_log")
    orchestrator_action = next(action for action in dialog["actions"] if action["type"] == "open_orchestrator_log")
    assert agent_log_action["run_dir"] == "/run/dir"
    assert claude_action["run_dir"] == "/run/dir"
    assert orchestrator_action["run_dir"] == "/run/dir"

    paths = {action.get("path") for action in dialog["actions"] if "path" in action}
    assert "/run/dir" in paths
    assert "/run/dir/session-identity.json" in paths
    assert "/logs/claude.log" in paths
    assert "/logs" in paths
    assert "/logs/orch.log" in paths
    assert "/wt/diag/diagnostic.json" in paths
    assert "/wt/diag/run-audit.json" in paths
    assert "/wt/validate.json" in paths
    assert "/wt/validation-output.log" in paths
    assert "/wt/validation-stderr.log" in paths


@pytest.mark.parametrize(
    ("status", "available", "expected_text", "expect_actions"),
    [
        ("active", True, "Active — retention starts when the run ends", True),
        ("retained", True, "Retained", True),
        ("pinned", True, "Pinned — retained until unpinned", True),
        (
            "expired",
            False,
            "Expired — artifacts are no longer available",
            False,
        ),
        (
            "missing",
            False,
            "Unavailable — retained artifacts could not be found",
            False,
        ),
    ],
)
def test_session_diagnostics_uses_owner_evidence_state(
    status: str,
    available: bool,
    expected_text: str,
    expect_actions: bool,
) -> None:
    expires_at = "2026-08-20T12:00:00+00:00" if status == "retained" else None
    dialog = build_session_diagnostics_dialog(
        42,
        {
            "run_dir": "/archive/42/run-1",
            "manifest": None if status == "missing" else {"run_id": "run-1"},
            "timeline_evidence": {
                "run_dir": "/archive/42/run-1",
                "status": status,
                "label": status.title(),
                "available": available,
                "pinned": status == "pinned",
                "archived": status != "active",
                "expires_at": expires_at,
                "help_text": "Evidence lifecycle help.",
                "unpin_expires_immediately": False,
            },
        },
    )

    rows = _rows_to_map(dialog["rows"])
    assert rows["Timeline Evidence"] == expected_text
    if status == "retained":
        assert rows["Evidence Expires"] == expires_at
    assert bool(dialog["actions"]) is expect_actions


def test_build_session_diagnostics_dialog_passed_outcome_has_no_reason_row():
    """Closes the bug-1 narrative on the read side: a passed validation
    must produce a "Validation Status: passed" row but NO "Validation
    Reason" row, even if the on-disk manifest carries a stale failure
    reason from a pre-#6302 writer.

    The reader migration in #6306 routes the dialog through
    ``RunManifest.validation_outcome``, which surfaces ``ValidationPassed``
    for the inconsistent triple — and ``ValidationPassed`` has no
    ``reason`` field, so a Reason row is unrepresentable on the success
    path."""
    dialog = build_session_diagnostics_dialog(
        99,
        {
            "manifest": {
                "session_name": "sess-passed",
                "worktree": "/wt",
                # Inconsistent triple from a pre-#6302 writer:
                # status says passed but a stale reason is still on disk.
                "validation_passed": True,
                "validation_status": "passed",
                "validation_reason": "Validation failed for a949871f (exit_code=1)",
            },
            "run_dir": "/run/dir",
            "session_name": "fallback",
        },
    )

    rows = _rows_to_map(dialog["rows"])
    assert rows["Validation Status"] == "passed"
    # The stale reason MUST NOT surface on a passed outcome — that's
    # the exact contradiction the user screenshot captured before the
    # fix landed.
    assert "Validation Reason" not in rows


def test_build_session_diagnostics_dialog_no_validation_rows_when_outcome_unset():
    """Backwards-compat: a manifest with no validation fields produces
    neither a Validation Status nor a Validation Reason row. Old
    behavior preserved."""
    dialog = build_session_diagnostics_dialog(
        100,
        {
            "manifest": {
                "session_name": "sess-fresh",
                "worktree": "/wt",
            },
            "run_dir": "/run/dir",
            "session_name": "fallback",
        },
    )

    rows = _rows_to_map(dialog["rows"])
    assert "Validation Status" not in rows
    assert "Validation Reason" not in rows


def test_build_session_diagnostics_dialog_fallbacks_without_worktree():
    dialog = build_session_diagnostics_dialog(
        7,
        {
            "manifest": {
                "session_name": "",
                "validation_record_path": "validate.json",
            },
            "run_dir": "/run/fallback",
            "session_name": "fallback-session",
        },
    )

    rows = _rows_to_map(dialog["rows"])
    assert rows["Session"] == "fallback-session"
    assert rows["Worktree"] == "-"
    agent_log_action = next(action for action in dialog["actions"] if action["type"] == "open_agent_log")
    orchestrator_action = next(action for action in dialog["actions"] if action["type"] == "open_orchestrator_log")
    assert agent_log_action["run_dir"] == "/run/fallback"
    assert orchestrator_action["run_dir"] == "/run/fallback"
    assert all(action["type"] != "view_claude_log" for action in dialog["actions"])

    paths = {action.get("path") for action in dialog["actions"] if "path" in action}
    assert "/run/fallback" in paths
    # No worktree means relative validation path cannot be resolved/opened.
    assert "validate.json" not in paths


def test_build_session_diagnostics_dialog_keeps_absolute_validation_path():
    dialog = build_session_diagnostics_dialog(
        9,
        {
            "manifest": {
                "session_name": "sess-abs",
                "worktree": "/wt",
                "validation_record_path": "/wt/.issue-orchestrator/sessions/r1/validation-record.json",
            },
            "run_dir": "/run/r1",
        },
    )

    paths = {action.get("path") for action in dialog["actions"] if "path" in action}
    assert "/wt/.issue-orchestrator/sessions/r1/validation-record.json" in paths
    assert "/wt//wt/.issue-orchestrator/sessions/r1/validation-record.json" not in paths


def test_build_session_diagnostics_dialog_keeps_absolute_validation_output_path():
    dialog = build_session_diagnostics_dialog(
        10,
        {
            "manifest": {
                "session_name": "sess-abs-out",
                "worktree": "/wt",
                "validation_stdout": "/wt/.issue-orchestrator/sessions/r1/validation-output.log",
            },
            "run_dir": "/run/r1",
        },
    )

    paths = {action.get("path") for action in dialog["actions"] if "path" in action}
    assert "/wt/.issue-orchestrator/sessions/r1/validation-output.log" in paths


def test_build_validation_failure_dialog_includes_failed_tests_and_artifacts():
    dialog = build_validation_failure_dialog(
        12,
        {
            "manifest": {
                "session_name": "sess-validate",
                "worktree": "/wt",
                "validation_record_path": "/wt/.issue-orchestrator/sessions/r1/validation-record.json",
                "validation_stdout": "/wt/.issue-orchestrator/sessions/r1/validation-stdout.log",
                "validation_stderr": "/wt/.issue-orchestrator/sessions/r1/validation-stderr.log",
            },
            "run_dir": "/run/r1",
            "validation_failure": {
                "status": "failed",
                "reason": "Validation failed for deadbeef (exit_code=2)",
                "suite": "publish_gate",
                "command": "make validate",
                "exit_code": 2,
                "started_at": "2026-03-22T04:53:14Z",
                "ended_at": "2026-03-22T04:53:58Z",
                "failed_tests": ["tests/unit/test_web.py::test_one"],
                "stdout_excerpt": ["FAILED tests/unit/test_web.py::test_one"],
                "stderr_excerpt": ["make: *** [validate] Error 2"],
            },
        },
    )

    assert dialog["title"] == "Validation Failure #12"
    assert dialog["status"] == "failed"
    assert dialog["reason"] == "Validation failed for deadbeef (exit_code=2)"
    assert dialog["failed_tests"] == ["tests/unit/test_web.py::test_one"]
    assert dialog["stdout_excerpt"] == ["FAILED tests/unit/test_web.py::test_one"]
    assert dialog["stderr_excerpt"] == ["make: *** [validate] Error 2"]
    assert dialog["summary_rows"] == [
        {"label": "Outcome", "value": "Failed"},
        {"label": "Reason", "value": "Validation failed for deadbeef (exit_code=2)"},
        {"label": "Suite", "value": "publish_gate"},
        {"label": "Command", "value": "make validate"},
        {"label": "Exit Code", "value": "2"},
        {"label": "Started", "value": "2026-03-22T04:53:14Z", "value_kind": "timestamp"},
        {"label": "Ended", "value": "2026-03-22T04:53:58Z", "value_kind": "timestamp"},
        {"label": "Failing Tests", "value": "1"},
    ]
    assert dialog["action_sections"] == [
        {
            "title": "Validation Artifacts",
            "actions": [
                {
                    "type": "open_path",
                    "label": "Open Validation Record",
                    "path": "/wt/.issue-orchestrator/sessions/r1/validation-record.json",
                    "group": "validation_artifacts",
                },
                {
                    "type": "open_path",
                    "label": "Open Validation Output",
                    "path": "/wt/.issue-orchestrator/sessions/r1/validation-stdout.log",
                    "group": "validation_artifacts",
                },
                {
                    "type": "open_path",
                    "label": "Open Validation Stderr",
                    "path": "/wt/.issue-orchestrator/sessions/r1/validation-stderr.log",
                    "group": "validation_artifacts",
                },
            ],
        },
        {
            "title": "Session Evidence",
            "actions": [
                {
                    "type": "open_agent_log",
                    "label": "View Session Recording",
                    "issue_number": 12,
                    "run_dir": "/run/r1",
                    "group": "session_evidence",
                },
                {
                    "type": "copy_agent_log",
                    "label": "Copy Session Recording",
                    "issue_number": 12,
                    "run_dir": "/run/r1",
                    "group": "session_evidence",
                },
                {
                    "type": "open_orchestrator_log",
                    "label": "Open Orchestrator Log",
                    "issue_number": 12,
                    "run_dir": "/run/r1",
                    "group": "session_evidence",
                },
            ],
        },
        {
            "title": "Diagnostics",
            "actions": [
                {
                    "type": "open_path",
                    "label": "Open Session Dir",
                    "path": "/run/r1",
                    "group": "diagnostics",
                },
                {
                    "type": "open_path",
                    "label": "Open Session Settings",
                    "path": "/run/r1/session-identity.json",
                    "group": "diagnostics",
                },
                {
                    "type": "open_session_diagnostics",
                    "label": "Full Diagnostics",
                    "issue_number": 12,
                    "run_dir": "/run/r1",
                    "group": "diagnostics",
                },
            ],
        },
    ]
    assert "actions" not in dialog


def test_build_validation_failure_dialog_renders_passed_run() -> None:
    dialog = build_validation_failure_dialog(
        14,
        {
            "manifest": {
                "session_name": "sess-validate",
                "worktree": "/wt",
                "validation_record_path": "/wt/.issue-orchestrator/sessions/r3/validation-record.json",
                "validation_stdout": "/wt/.issue-orchestrator/sessions/r3/validation-stdout.log",
                "validation_stderr": "/wt/.issue-orchestrator/sessions/r3/validation-stderr.log",
            },
            "run_dir": "/run/r3",
            "validation_failure": {
                "status": "passed",
                "reason": "Validation passed",
                "suite": "publish_gate",
                "command": "make validate",
                "exit_code": 0,
                "started_at": "2026-05-07T12:00:00Z",
                "ended_at": "2026-05-07T12:04:30Z",
                "failed_tests": [],
                "stdout_excerpt": ["============= 142 passed in 41.21s ============="],
                "stderr_excerpt": [],
            },
        },
    )

    assert dialog["title"] == "Validation Passed #14"
    assert dialog["status"] == "passed"
    assert dialog["reason"] == "Validation passed"
    assert dialog["failed_tests"] == []
    assert {"label": "Outcome", "value": "Passed"} in dialog["summary_rows"]
    assert {"label": "Failing Tests", "value": "0"} in dialog["summary_rows"]


def test_build_validation_failure_dialog_keeps_missing_exit_code_visible() -> None:
    dialog = build_validation_failure_dialog(
        13,
        {
            "manifest": {
                "session_name": "sess-validate",
            },
            "run_dir": "/run/r2",
            "validation_failure": {
                "reason": "Validation failed without an exit code",
                "suite": "publish_gate",
                "command": "make validate",
                "started_at": "2026-03-22T04:53:14Z",
                "ended_at": "2026-03-22T04:53:58Z",
                "failed_tests": [],
                "stdout_excerpt": [],
                "stderr_excerpt": [],
            },
        },
    )

    assert dialog["exit_code"] is None
    assert {"label": "Exit Code", "value": "-"} in dialog["summary_rows"]


def test_build_validation_failure_action_sections_rejects_unknown_group() -> None:
    with pytest.raises(ValueError, match="Unknown validation failure action group"):
        _build_validation_failure_action_sections(
            [
                {
                    "type": "open_path",
                    "label": "Open Validation Record",
                    "path": "/tmp/validation-record.json",
                    "group": "sesion_evidence",
                }
            ]
        )


def test_build_session_diagnostics_dialog_drops_malformed_analysis():
    dialog = build_session_diagnostics_dialog(
        11,
        {
            "manifest": {
                "session_name": "sess-bad-analysis",
            },
            "run_dir": "/run/r1",
            "analysis": {
                "detail": "Missing the required headline should drop this payload.",
                "unexpected": True,
            },
        },
    )

    assert dialog["analysis"] is None


def test_build_blocked_issues_dialog():
    dialog = build_blocked_issues_dialog({"blocked_issues": ["M1-1"]})

    assert dialog == {
        "title": "Blocked Issues",
        "blocked_issues": ["M1-1"],
    }


def test_build_phase_dialog_in_progress():
    dialog = build_phase_dialog(
        {
            "phases": [
                {"name": "coding-1", "display_name": "Coding 1"},
                {"name": "review-1", "display_name": "Review 1"},
                {"name": "coding-2", "display_name": "Coding 2"},
            ]
        },
        issue_number=7,
        phase_key="in_progress",
    )

    assert dialog["title"] == "Coding 2"
    assert dialog["phase"]["name"] == "coding-2"


def test_build_phase_dialog_review_and_default():
    phases_payload = {
        "phases": [
            {"name": "coding-1", "display_name": "Coding 1"},
            {"name": "review-1", "display_name": "Review 1"},
        ]
    }

    review_dialog = build_phase_dialog(phases_payload, issue_number=9, phase_key="review")
    assert review_dialog["title"] == "Review 1"
    assert review_dialog["phase"]["name"] == "review-1"

    default_dialog = build_phase_dialog(phases_payload, issue_number=9, phase_key=None)
    assert default_dialog["phase"]["name"] == "review-1"


def test_build_phase_dialog_specific_match():
    dialog = build_phase_dialog(
        {"phases": [{"name": "tech_lead", "display_name": "Tech Lead"}]},
        issue_number=1,
        phase_key="tech_lead",
    )

    assert dialog["title"] == "Tech Lead"
    assert dialog["phase"]["name"] == "tech_lead"
