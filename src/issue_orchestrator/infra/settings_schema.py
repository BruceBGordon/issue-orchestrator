"""Data-driven settings schema using Pydantic.

Defines a SettingsSchema model hierarchy where each field carries all its metadata
via Field() + json_schema_extra. This single source of truth drives:
- settings.html (Jinja2 renders from schema)
- GET/POST /api/settings (serialize/validate via Pydantic)
- setup_wizard.py (defaults, labels, hints)
    - docs/user/configuration_reference.md (auto-generated reference)
- doctor checks (path validation, agent references, status summaries)
"""

from __future__ import annotations

import functools
from typing import Any, Literal, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator, model_validator

from ..domain.tech_lead_naming import TECH_LEAD_DISPLAY_NAME
from .config_models import (
    MERGE_QUEUE_PROVIDERS,
    TECH_LEAD_AUTHORITY_MODES,
    TECH_LEAD_MAX_EXPEDITED_LIMIT,
)

_TECH_LEAD_SECTION = f"{TECH_LEAD_DISPLAY_NAME} Review"

from .settings_schema_support import (
    CONFIG_VALUE_TYPE_PATH,
    DOCTOR_CHECK_FIRST_ARG_PATH_EXISTS,  # pyright: ignore[reportUnusedImport] -- re-exported for doctor schema checks
    DOCTOR_CHECK_PATH_EXISTS,
    DOCTOR_CHECK_REFERENCES_AGENT,
    DOCTOR_SEVERITY_ERROR,
    DOCTOR_SEVERITY_WARNING,
    FORM_CONTROL_DICT_ENUM as FORM_CONTROL_DICT_ENUM,
    FORM_CONTROL_ENUM as FORM_CONTROL_ENUM,
    FORM_CONTROL_KINDS as FORM_CONTROL_KINDS,
    FORM_CONTROL_OPTIONAL_INTEGER as FORM_CONTROL_OPTIONAL_INTEGER,
    SettingsSavePlan as SettingsSavePlan,
    UnsupportedSettingsFieldError as UnsupportedSettingsFieldError,
    classify_form_control as classify_form_control,
    SUMMARY_BOOLEAN_FLAG,
    SUMMARY_ENABLED_FLAG,
    SUMMARY_INTERVAL,
    SUMMARY_KEY_VALUE,
    apply_tabs_to_config,
    build_settings_json_schema,
    build_settings_save_plan,
    build_tabs_from_config,
    collect_restart_fields,
    doctor_check_fields,
    field_meta,
    generate_reference_markdown,
    setup_fields,
    summary_fields,
)

if TYPE_CHECKING:
    from .config import Config


# ---------------------------------------------------------------------------
# Sub-models for each settings tab
# ---------------------------------------------------------------------------

class ConcurrencySettings(BaseModel):
    """Settings for the Concurrency tab."""

    max_concurrent_sessions: int = Field(
        3,
        title="Max Concurrent Sessions",
        description="Maximum parallel agent sessions",
        ge=1,
        le=20,
        json_schema_extra={
            "doc_examples": ["1", "3", "5"],
            "doc_notes": "Set based on CPU, RAM, and how many concurrent sessions you can actively review.",
            "section": "Session Limits",
            "config_attr": "max_concurrent_sessions",
            "yaml_path": "execution.concurrency.max_concurrent_sessions",
            "setup": {
                "enabled": True,
                "section": "concurrency",
                "order": 10,
            },
        },
    )
    session_timeout_minutes: int = Field(
        45,
        title="Session Timeout (minutes)",
        description="Kill sessions after this duration",
        ge=5,
        le=180,
        json_schema_extra={
            "doc_examples": ["30", "45", "90"],
            "doc_notes": "Lower values fail faster for stuck sessions; higher values help long builds.",
            "section": "Session Limits",
            "config_attr": "session_timeout_minutes",
            "yaml_path": "execution.concurrency.session_timeout_minutes",
        },
    )
    queue_refresh_seconds: int = Field(
        600,
        title="Queue Refresh Interval (seconds)",
        description="How often to refresh the issue queue from GitHub (0 = manual only)",
        ge=0,
        le=3600,
        json_schema_extra={
            "doc_examples": ["0", "300", "600"],
            "doc_notes": "Use 0 to disable automatic refreshes and refresh manually in the UI.",
            "section": "Queue",
            "config_attr": "queue_refresh_seconds",
            "yaml_path": "ui.queue_refresh_seconds",
        },
    )
    fetch_layer_enabled: bool = Field(
        True,
        title="Fetch-Layer Optimization",
        description="Enable incremental refreshes between periodic full scans",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Disable to force a full GitHub queue scan on every refresh.",
            "section": "Queue",
            "config_attr": "fetch_layer_enabled",
            "yaml_path": "ui.fetch_layer.enabled",
        },
    )
    fetch_layer_network_sync_seconds: int = Field(
        60,
        title="Network Sync Interval (seconds)",
        description="How often to run GitHub network sync cycles (independent of control tick)",
        ge=5,
        le=3600,
        json_schema_extra={
            "doc_examples": ["15", "60", "120"],
            "doc_notes": "Lower values improve freshness; higher values reduce GitHub API calls.",
            "section": "Queue",
            "config_attr": "fetch_layer_network_sync_seconds",
            "yaml_path": "ui.fetch_layer.network_sync_seconds",
        },
    )
    fetch_layer_full_scan_interval_seconds: int = Field(
        1800,
        title="Full Scan Interval (seconds)",
        description="Run a full queue scan at this interval even when incremental mode is enabled",
        ge=60,
        le=86400,
        json_schema_extra={
            "doc_examples": ["600", "1800", "3600"],
            "doc_notes": "Lower values discover new work faster; higher values reduce API usage.",
            "section": "Queue",
            "config_attr": "fetch_layer_full_scan_interval_seconds",
            "yaml_path": "ui.fetch_layer.full_scan_interval_seconds",
        },
    )
    fetch_layer_discovery_limit: int = Field(
        25,
        title="Discovery Limit",
        description="Max issues fetched per incremental discovery pass",
        ge=0,
        le=200,
        json_schema_extra={
            "doc_examples": ["0", "25", "50"],
            "doc_notes": "Set to 0 to disable discovery during incremental refreshes.",
            "section": "Queue",
            "config_attr": "fetch_layer_discovery_limit",
            "yaml_path": "ui.fetch_layer.discovery_limit",
        },
    )
    fetch_layer_max_hot_issues_per_cycle: int = Field(
        40,
        title="Hot Issue Refresh Limit",
        description="Max existing queue issues to refresh by direct issue lookup per cycle",
        ge=0,
        le=500,
        json_schema_extra={
            "doc_examples": ["20", "40", "100"],
            "doc_notes": "Higher values improve freshness but increase API usage.",
            "section": "Queue",
            "config_attr": "fetch_layer_max_hot_issues_per_cycle",
            "yaml_path": "ui.fetch_layer.max_hot_issues_per_cycle",
        },
    )
    fetch_layer_pr_scan_every_n_refreshes: int = Field(
        2,
        title="PR Scan Cadence",
        description="Scan review/rework PRs every N queue refreshes",
        ge=1,
        le=20,
        json_schema_extra={
            "doc_examples": ["1", "2", "3"],
            "doc_notes": "Use 1 for max freshness; increase to reduce PR API calls.",
            "section": "Queue",
            "config_attr": "fetch_layer_pr_scan_every_n_refreshes",
            "yaml_path": "ui.fetch_layer.pr_scan_every_n_refreshes",
        },
    )
    fetch_layer_dependency_scan_every_n_refreshes: int = Field(
        1,
        title="Dependency Scan Cadence",
        description="Recompute dependency blocking every N queue refreshes",
        ge=1,
        le=20,
        json_schema_extra={
            "doc_examples": ["1", "2", "3"],
            "doc_notes": "Use 1 for immediate dependency updates; increase to reduce load.",
            "section": "Queue",
            "config_attr": "fetch_layer_dependency_scan_every_n_refreshes",
            "yaml_path": "ui.fetch_layer.dependency_scan_every_n_refreshes",
        },
    )
    fetch_layer_visibility_aware_enabled: bool = Field(
        False,
        title="Visibility-Aware Refresh",
        description="Prioritize refresh for issues currently visible in the Flow board",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Requires browser visibility hints from the Flow board.",
            "section": "Queue",
            "config_attr": "fetch_layer_visibility_aware_enabled",
            "yaml_path": "ui.fetch_layer.visibility_aware_enabled",
        },
    )
    fetch_layer_selective_sync_planner_enabled: bool = Field(
        False,
        title="Selective Sync Planner",
        description="Enable cross-entity selective sync planning for queue refresh cycles",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Use with telemetry to tune freshness versus API cost.",
            "section": "Queue",
            "config_attr": "fetch_layer_selective_sync_planner_enabled",
            "yaml_path": "ui.fetch_layer.selective_sync_planner_enabled",
        },
    )
    default_priority_tier: int = Field(
        1,
        title="Default Priority Tier",
        description="Default priority tier when none is specified (0-9)",
        ge=0,
        le=9,
        json_schema_extra={
            "doc_examples": ["0", "1", "2"],
            "doc_notes": "Used when issue titles do not include a [P?-nnn] prefix.",
            "section": "Scheduling",
            "config_attr": "scheduling.default_priority_tier",
            "yaml_path": "scheduling.default_priority_tier",
        },
    )


class E2ESettings(BaseModel):
    """Settings for the E2E Runner tab."""

    enabled: bool = Field(
        False,
        title="Enable E2E Test Runner",
        description="Automatically run E2E tests when main branch changes",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Keep disabled on repos without stable E2E tests.",
            "config_attr": "e2e.enabled",
            "yaml_path": "e2e.enabled",
            "summary": {
                "section": "e2e",
                "format": SUMMARY_ENABLED_FLAG,
                "label": "E2E Runner",
            },
        },
    )
    auto_run_interval_minutes: int = Field(
        30,
        title="Auto-run Interval (minutes)",
        description="Min interval between auto runs (0 = disable)",
        ge=0,
        le=1440,
        json_schema_extra={
            "doc_examples": ["0", "30", "60"],
            "doc_notes": "Set to 0 to disable automatic runs and trigger manually.",
            "config_attr": "e2e.auto_run_interval_minutes",
            "yaml_path": "e2e.auto_run_interval_minutes",
            "summary": {
                "section": "e2e",
                "format": SUMMARY_INTERVAL,
                "label": "auto",
                "zero_label": "manual",
                "unit": "m",
            },
        },
    )
    role: Literal["auto", "executor", "reader", "disabled"] = Field(
        "auto",
        title="Role",
        description="Role in multi-orchestrator setup",
        json_schema_extra={
            "doc_examples": ["auto", "executor", "reader", "disabled"],
            "doc_notes": "Use executor on the single machine that should run tests.",
            "config_attr": "e2e.role",
            "yaml_path": "e2e.role",
        },
    )
    occupies_session_slot: bool = Field(
        False,
        title="Count E2E against the session budget",
        description="Treat an E2E run as a first-class worker workload",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": (
                "Off (default) keeps today's parallel behavior: E2E runs "
                "alongside agents. On, an E2E run counts against "
                "max_concurrent_sessions (not the reserved tech lead slot): it "
                "starts only when a worker slot is free, occupies one slot "
                "while running so the planner launches one fewer agent, and a "
                "due suite claims a slot ahead of new issues but behind "
                "in-flight reviews/reworks/validation-retries/tech_lead. Enable on "
                "resource-constrained machines where a second orchestrator "
                "workload would starve live agents."
            ),
            "config_attr": "e2e.occupies_session_slot",
            "yaml_path": "e2e.occupies_session_slot",
        },
    )
    runner_kind: Literal["pytest", "command"] = Field(
        "pytest",
        title="Runner Kind",
        description="Execution adapter used for E2E runs",
        json_schema_extra={
            "doc_examples": ["pytest", "command"],
            "doc_notes": "Use pytest for live test events and retries; use command for arbitrary test runners that emit JUnit XML.",
            "config_attr": "e2e.runner_kind",
            "yaml_path": "e2e.runner_kind",
        },
    )
    pytest_args: str = Field(
        "tests/e2e -v",
        title="Pytest Arguments",
        description="Space-separated pytest arguments used when Runner Kind is pytest",
        json_schema_extra={
            "doc_examples": [
                "tests/e2e -v",
                "tests/e2e -v --junitxml=.issue-orchestrator/e2e-results/pytest-junit.xml",
            ],
            "doc_notes": "Used only when runner_kind=pytest. Add --junitxml and mirror the same path in junit_xml_paths when you want structured Results coverage in the dashboard.",
            "config_attr": "e2e.pytest_args",
            "yaml_path": "e2e.pytest_args",
            "ui_transform": "space_separated_list",
        },
    )
    command: str = Field(
        "",
        title="Command",
        description="Space-separated command used when Runner Kind is command",
        json_schema_extra={
            "doc_examples": ["./scripts/run-e2e-suite.sh", "npm run test:e2e -- --reporter=junit"],
            "doc_notes": "Used when runner_kind=command. The command runs inside the E2E worktree.",
            "config_attr": "e2e.command",
            "yaml_path": "e2e.command",
            "ui_transform": "space_separated_list",
        },
    )
    junit_xml_paths: str = Field(
        "",
        title="JUnit XML Paths",
        description="Relative JUnit XML files or globs to ingest after the run (one per line)",
        json_schema_extra={
            "doc_examples": [
                ".issue-orchestrator/e2e-results/pytest-junit.xml",
                "test-results/junit.xml",
            ],
            "doc_notes": "Leave empty for log-only runs. Missing configured reports fail the run loudly. Use the same path you passed to pytest --junitxml or your external test runner.",
            "config_attr": "e2e.junit_xml_paths",
            "yaml_path": "e2e.junit_xml_paths",
            "ui_transform": "newline_separated_list",
        },
    )
    artifact_paths: str = Field(
        "",
        title="Artifact Paths",
        description="Additional report or artifact files to expose in the UI (one per line)",
        json_schema_extra={
            "doc_examples": [
                "playwright-report/index.html",
                "test-results/**/*.zip",
                "reports/**/*.html",
            ],
            "doc_notes": "Paths are resolved relative to the E2E worktree after the run completes. Use this for native HTML reports, traces, screenshots, and similar debugging artifacts.",
            "config_attr": "e2e.artifact_paths",
            "yaml_path": "e2e.artifact_paths",
            "ui_transform": "newline_separated_list",
        },
    )
    allow_retry_once: bool = Field(
        True,
        title="Retry failed tests once",
        description="Retry failing tests to reduce flakiness",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Applies to runner_kind=pytest. Command runners ignore this and report the original command result.",
            "config_attr": "e2e.allow_retry_once",
            "yaml_path": "e2e.allow_retry_once",
            "summary": {
                "section": "e2e",
                "format": SUMMARY_BOOLEAN_FLAG,
                "label": "retry",
                "true_value": "on",
                "false_value": "off",
            },
        },
    )
    stop_on_first_failure: bool = Field(
        False,
        title="Stop on first failure",
        description="Add -x flag to stop test run on first failure",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Applies to runner_kind=pytest.",
            "config_attr": "e2e.stop_on_first_failure",
            "yaml_path": "e2e.stop_on_first_failure",
        },
    )
    quarantine_file: str = Field(
        "tests/e2e/quarantine.txt",
        title="Quarantine File",
        description="Path to quarantine file for skipping known-flaky tests",
        json_schema_extra={
            "doc_examples": ["tests/e2e/quarantine.txt", "tests/e2e/quarantine-local.txt"],
            "doc_notes": "Doctor verifies the file exists when E2E is enabled.",
            "config_attr": "e2e.quarantine_file",
            "yaml_path": "e2e.quarantine_file",
            "doctor_check": DOCTOR_CHECK_PATH_EXISTS,
            "doctor_check_condition": "e2e.enabled",
            "doctor_severity": DOCTOR_SEVERITY_WARNING,
        },
    )
    auto_quarantine: bool = Field(
        True,
        title="Auto-quarantine failing tests",
        description="Automatically add failing tests to the quarantine list",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Set false to require manual quarantine updates.",
            "config_attr": "e2e.auto_quarantine",
            "yaml_path": "e2e.auto_quarantine",
            "summary": {
                "section": "e2e",
                "format": SUMMARY_BOOLEAN_FLAG,
                "label": "quarantine",
                "true_value": "auto",
                "false_value": "manual",
            },
        },
    )
    auto_create_issues: bool = Field(
        True,
        title="Auto-create failure issues",
        description="Automatically create GitHub issues for failed tests",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Disable if you prefer manual triage of failures.",
            "config_attr": "e2e.auto_create_issues",
            "yaml_path": "e2e.auto_create_issues",
            "summary": {
                "section": "e2e",
                "format": SUMMARY_BOOLEAN_FLAG,
                "label": "issues",
                "true_value": "auto",
                "false_value": "manual",
            },
        },
    )
    issue_agent_label: str = Field(
        "agent:backend",
        title="Failure issue agent label",
        description="Agent label assigned to auto-created failure issues",
        json_schema_extra={
            "doc_examples": ["agent:backend", "agent:tech-lead"],
            "doc_notes": "Must refer to an agent defined in the config.",
            "config_attr": "e2e.issue_agent_label",
            "yaml_path": "e2e.issue_agent_label",
            "doctor_check": DOCTOR_CHECK_REFERENCES_AGENT,
            "doctor_check_condition": "e2e.enabled",
            "doctor_severity": DOCTOR_SEVERITY_WARNING,
        },
    )


class ValidationSettings(BaseModel):
    """Settings for local validation gates."""

    quick_cmd: Optional[str] = Field(
        None,
        title="Quick Validation Command",
        description="Fast command run by coding-done and review exchange loops",
        json_schema_extra={
            "doc_examples": ["./scripts/validate-fast.sh", "make test-fast"],
            "doc_notes": "Keep this fast enough for agent/reviewer back-and-forth. Put repo-specific policy checks such as banned test skips here.",
            "section": "Quick Gate",
            "config_attr": "validation.quick.cmd",
            "yaml_path": "validation.quick.cmd",
        },
    )
    quick_timeout_seconds: int = Field(
        300,
        title="Quick Validation Timeout (seconds)",
        description="Timeout for quick validation",
        ge=1,
        le=7200,
        json_schema_extra={
            "doc_examples": ["120", "300", "600"],
            "doc_notes": "Lower values keep review loops responsive.",
            "section": "Quick Gate",
            "config_attr": "validation.quick.timeout_seconds",
            "yaml_path": "validation.quick.timeout_seconds",
        },
    )
    publish_cmd: Optional[str] = Field(
        None,
        title="Publish Validation Command",
        description="Authoritative command run before push/publish",
        json_schema_extra={
            "doc_examples": ["./scripts/validate-pr.sh", "./scripts/validate-pr-suite.sh"],
            "doc_notes": "This should match the repo's authoritative local PR/pre-push gate. If make validate-pr wraps the cache-aware verify hook, configure a private non-recursive suite command instead.",
            "section": "Publish Gate",
            "config_attr": "validation.publish.cmd",
            "yaml_path": "validation.publish.cmd",
        },
    )
    publish_timeout_seconds: int = Field(
        1800,
        title="Publish Validation Timeout (seconds)",
        description="Timeout for publish validation",
        ge=1,
        le=14400,
        json_schema_extra={
            "doc_examples": ["600", "1800", "3600"],
            "doc_notes": "Allow enough time for the deeper publish gate.",
            "section": "Publish Gate",
            "config_attr": "validation.publish.timeout_seconds",
            "yaml_path": "validation.publish.timeout_seconds",
        },
    )
    publish_dirty_check: Literal["tracked", "unstaged", "all", "off"] = Field(
        "tracked",
        title="Publish Dirty Check",
        description="Dirty-tree policy enforced before push actions",
        json_schema_extra={
            "doc_examples": ["tracked", "unstaged", "all", "off"],
            "doc_notes": "Use tracked for normal agent worktrees. Use off only when another guard owns dirty-tree safety.",
            "section": "Publish Gate",
            "config_attr": "validation.publish.dirty_check",
            "yaml_path": "validation.publish.dirty_check",
        },
    )
    junit_xml_paths: str = Field(
        "",
        title="JUnit XML Paths",
        description="Relative JUnit XML files or globs emitted by validation commands",
        json_schema_extra={
            "doc_examples": ["test-results.xml", "build/test-results/test/*.xml"],
            "doc_notes": "When set, failed validations render a structured test-results view in the dashboard.",
            "section": "Evidence",
            "config_attr": "validation.junit_xml_paths",
            "yaml_path": "validation.junit_xml_paths",
            "ui_transform": "newline_separated_list",
        },
    )


class FilteringSettings(BaseModel):
    """Settings for the Filtering tab."""

    label: Optional[str] = Field(
        None,
        title="Label Filter",
        description="Only process issues with this label (optional)",
        json_schema_extra={
            "doc_examples": ["bot-ready", "needs-triage"],
            "doc_notes": "Use a single label to gate which issues are eligible.",
            "config_attr": "filtering.label",
            "yaml_path": "filtering.label",
        },
    )
    milestones: str = Field(
        "",
        title="Milestones",
        description="Milestones to process (comma-separated string or YAML list)",
        json_schema_extra={
            "doc_examples": ["M1, M2", "[\"M1\", \"M2\"]", ""],
            "doc_notes": "Accepts a comma-separated string or a YAML list. Leave empty to allow all milestones.",
            "config_attr": "filtering.milestones",
            "config_read_method": "filtering.get_milestones",
            "yaml_path": "filtering.milestones",
            "ui_transform": "comma_separated_list",
        },
    )
    exclude_labels: str = Field(
        "",
        title="Exclude Labels",
        description="Labels to exclude (comma-separated string or YAML list)",
        json_schema_extra={
            "doc_examples": ["test-data, skip", "[\"test-data\", \"skip\"]", ""],
            "doc_notes": "Accepts a comma-separated string or a YAML list.",
            "config_attr": "filtering.exclude_labels",
            "yaml_path": "filtering.exclude_labels",
            "ui_transform": "comma_separated_list",
        },
    )
    exclude_label_prefixes: str = Field(
        "",
        title="Exclude Label Prefixes",
        description="Label prefixes to exclude (comma-separated string or YAML list)",
        json_schema_extra={
            "doc_examples": ["io:e2e:", "[\"io:e2e:\", \"tmp:\"]", ""],
            "doc_notes": "Exclude issues that have any label starting with one of these prefixes.",
            "config_attr": "filtering.exclude_label_prefixes",
            "yaml_path": "filtering.exclude_label_prefixes",
            "ui_transform": "comma_separated_list",
        },
    )
    fetch_limit: int = Field(
        100,
        title="Fetch Limit",
        description="Max issues to fetch per API call",
        ge=1,
        le=500,
        json_schema_extra={
            "doc_examples": ["50", "100", "200"],
            "doc_notes": "Lower values reduce API load; higher values reduce pagination.",
            "config_attr": "filtering.fetch_limit",
            "yaml_path": "filtering.fetch_limit",
        },
    )
    max_to_start: int = Field(
        0,
        title="Max to Start",
        description="Stop after starting N issues (0 = unlimited)",
        ge=0,
        le=100,
        json_schema_extra={
            "doc_examples": ["0", "5", "10"],
            "doc_notes": "Useful for dry runs or throttling initial ramp-up.",
            "config_attr": "filtering.max_to_start",
            "yaml_path": "filtering.max_to_start",
        },
    )


class MilestonesSettings(BaseModel):
    """Settings for the Milestones tab."""

    order: str = Field(
        "",
        title="Milestone Order",
        description=(
            "Explicit ordered list of milestone titles. Does not filter; "
            "unlisted milestones are appended using the milestone sort strategy."
        ),
        json_schema_extra={
            "doc_examples": ["M1, M2", ""],
            "doc_notes": "Use to override the default sort order without filtering.",
            "section": "Ordering",
            "config_attr": "milestone_order",
            "yaml_path": "milestones.order",
            "ui_transform": "comma_separated_list",
        },
    )


class ReviewSettings(BaseModel):
    """Settings for the Review tab."""

    enabled: bool = Field(
        False,
        title="Enable Code Review",
        description="Enable automated code review workflow",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "When enabled, a reviewer agent validates work agent PRs.",
            "section": "Code Review Workflow",
            "config_attr": "review_enabled",
            "yaml_path": "review.enabled",
            "summary": {
                "section": "review",
                "format": SUMMARY_ENABLED_FLAG,
                "label": "Code Review",
            },
        },
    )
    default_reviewer: Optional[str] = Field(
        None,
        title="Default Reviewer Agent",
        description="Agent label for code reviews (e.g., agent:reviewer)",
        json_schema_extra={
            "doc_examples": ["agent:reviewer"],
            "doc_notes": "Must match a label defined under agents.",
            "section": "Code Review Workflow",
            "config_attr": "code_review_agent",
            "yaml_path": "review.default",
            "doctor_check": DOCTOR_CHECK_REFERENCES_AGENT,
            "doctor_check_condition": "review_enabled",
            "doctor_severity": DOCTOR_SEVERITY_ERROR,
            "summary": {
                "section": "review",
                "format": SUMMARY_KEY_VALUE,
                "label": "default",
            },
        },
    )
    max_rework_cycles: int = Field(
        5,
        title="Max Rework Cycles",
        description="Max times to re-queue work agent before escalating",
        ge=0,
        le=10,
        json_schema_extra={
            "doc_examples": ["0", "2", "5"],
            "doc_notes": "Set to 0 to disable rework cycles (immediate escalation).",
            "section": "Code Review Workflow",
            "config_attr": "max_rework_cycles",
            "yaml_path": "review.max_rework_cycles",
        },
    )
    max_consecutive_publish_failures: int = Field(
        3,
        title="Max Consecutive Publish Failures",
        description="Escalate to needs-human after this many consecutive push/PR creation failures",
        ge=1,
        le=10,
        json_schema_extra={
            "doc_examples": ["2", "3", "5"],
            "doc_notes": "After N consecutive publish failures for the same issue, escalate to needs-human instead of publish-failed.",
            "section": "Code Review Workflow",
            "config_attr": "max_consecutive_publish_failures",
            "yaml_path": "review.max_consecutive_publish_failures",
        },
    )
    keep_current_approach_label: str = Field(
        "reviewer-keep-current-approach",
        title="Keep Current Approach Label",
        description="Label that tells reviewer to avoid alternative approaches",
        json_schema_extra={
            "doc_examples": ["reviewer-keep-current-approach"],
            "doc_notes": "Applied to issues where stability is preferred over refactors.",
            "section": "Code Review Workflow",
            "config_attr": "review_keep_current_approach_label",
            "yaml_path": "review.keep_current_approach_label",
        },
    )
    retrospective_enabled: bool = Field(
        False,
        title="Enable Retrospective Reviews",
        description="Enable review-first audits for existing implementations",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "When enabled, issues carrying the retrospective trigger label are reviewed before any coder rework is launched.",
            "section": "Retrospective Review Workflow",
            "config_attr": "retrospective_review_enabled",
            "yaml_path": "review.retrospective.enabled",
            "summary": {
                "section": "review",
                "format": SUMMARY_BOOLEAN_FLAG,
                "label": "retrospective",
                "true_value": "enabled",
                "false_value": "disabled",
            },
        },
    )
    retrospective_trigger_label: str = Field(
        "retrospective-review",
        title="Retrospective Trigger Label",
        description="Issue label that queues review of an existing implementation",
        json_schema_extra={
            "doc_examples": ["retrospective-review", "lack-of-review-redo"],
            "doc_notes": "This label is the source of truth for review-first reruns. It may be applied to open or closed issues.",
            "section": "Retrospective Review Workflow",
            "config_attr": "retrospective_review_trigger_label",
            "yaml_path": "review.retrospective.trigger_label",
        },
    )
    retrospective_reviewed_label: str = Field(
        "retrospective-reviewed",
        title="Retrospective Reviewed Label",
        description="Issue label added after retrospective review approval",
        json_schema_extra={
            "doc_examples": ["retrospective-reviewed"],
            "doc_notes": "Added to the issue when the reviewer approves the existing implementation.",
            "section": "Retrospective Review Workflow",
            "config_attr": "retrospective_reviewed_label",
            "yaml_path": "review.retrospective.reviewed_label",
        },
    )
    retrospective_changes_requested_label: str = Field(
        "retrospective-changes-requested",
        title="Retrospective Changes Requested Label",
        description="Issue label added when retrospective review asks for coder rework",
        json_schema_extra={
            "doc_examples": ["retrospective-changes-requested"],
            "doc_notes": "Added before the issue enters the normal coder rework and PR review lifecycle.",
            "section": "Retrospective Review Workflow",
            "config_attr": "retrospective_changes_requested_label",
            "yaml_path": "review.retrospective.changes_requested_label",
        },
    )
    run_audit_min_runtime_minutes: int = Field(
        20,
        title="Auto Run Audit Threshold (minutes)",
        description="Automatically capture a run audit when runtime meets or exceeds this threshold (0 = disable)",
        ge=0,
        le=1440,
        json_schema_extra={
            "doc_examples": ["0", "20", "60"],
            "doc_notes": "Long runs get a persisted audit automatically; set to 0 to keep audits label-driven only.",
            "section": "Code Review Workflow",
            "config_attr": "review_run_audit_min_runtime_minutes",
            "yaml_path": "review.run_audit.min_runtime_minutes",
        },
    )
    run_audit_on_timeout: bool = Field(
        True,
        title="Audit Timed-Out Runs",
        description="Automatically capture a run audit when a session times out",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Keep enabled to preserve diagnostics for timed-out sessions even when they did not exceed the slow-run threshold cleanly.",
            "section": "Code Review Workflow",
            "config_attr": "review_run_audit_on_timeout",
            "yaml_path": "review.run_audit.on_timeout",
        },
    )
    nits_default_policy: Literal["ignore", "surface", "address"] = Field(
        "surface",
        title="Default Nit Policy",
        description="Default policy for reviewer nits before PR creation",
        json_schema_extra={
            "doc_examples": ["surface", "address", "ignore"],
            "doc_notes": (
                "Nits are non-blocking review items. surface records and shows "
                "them without rework; address includes them in the normal coder "
                "rework loop before PR creation; ignore records them only in "
                "review artifacts."
            ),
            "section": "Code Review Workflow",
            "config_attr": "review_nits_default_policy",
            "yaml_path": "review.nits.default_policy",
        },
    )
    nits_by_agent: dict[str, Literal["ignore", "surface", "address"]] = Field(
        default_factory=dict,
        title="Nit Policy By Agent",
        description="Per-coder-agent nit policy overrides",
        json_schema_extra={
            "doc_examples": ['{"agent:frontend": "address"}'],
            "doc_notes": (
                "Keys are coder agent labels. Values override "
                "review.nits.default_policy for work produced by that agent."
            ),
            "section": "Code Review Workflow",
            "config_attr": "review_nits_by_agent",
            "yaml_path": "review.nits.by_agent",
        },
    )
    exchange_mode: Literal["via-draft-pr", "via-mcp", "via-local-loop", "auto"] = Field(
        "via-local-loop",
        title="Review Exchange Mode",
        description="Review exchange mode (via-mcp loop, local loop, or via-draft-pr review)",
        json_schema_extra={
            "doc_examples": ["via-local-loop", "via-draft-pr", "via-mcp", "auto"],
            "doc_notes": "Local loop is the default; use via-draft-pr for GitHub-mediated review cycles.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_mode",
            "yaml_path": "review.exchange.mode",
        },
    )
    exchange_probe_schedule: Literal["startup", "daily", "interval", "manual"] = Field(
        "daily",
        title="Exchange Probe Schedule",
        description="When to run MCP round-trip validation",
        json_schema_extra={
            "doc_examples": ["daily", "startup", "interval", "manual"],
            "doc_notes": "Use manual to disable automatic probes and run on demand.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_probe_schedule",
            "yaml_path": "review.exchange.probe.schedule",
        },
    )
    exchange_probe_interval_days: int = Field(
        1,
        title="Exchange Probe Interval (days)",
        description="Interval for MCP round-trip validation when schedule=interval",
        ge=1,
        le=30,
        json_schema_extra={
            "doc_examples": ["1", "7", "14"],
            "doc_notes": "Used only when schedule=interval.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_probe_interval_days",
            "yaml_path": "review.exchange.probe.interval_days",
        },
    )
    exchange_max_rounds: int = Field(
        10,
        title="Exchange Max Rounds",
        description="Max coder/reviewer rounds before stopping the MCP loop",
        ge=1,
        le=50,
        json_schema_extra={
            "doc_examples": ["5", "10", "20"],
            "doc_notes": "Higher values allow longer back-and-forth reviews.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_max_rounds",
            "yaml_path": "review.exchange.loop.max_rounds",
        },
    )
    exchange_max_no_progress: int = Field(
        2,
        title="Exchange Max No-Progress",
        description="Max rounds where reviewer reports no progress before stopping",
        ge=1,
        le=10,
        json_schema_extra={
            "doc_examples": ["1", "2", "3"],
            "doc_notes": "Limits loops when reviewer is not seeing improvements.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_max_no_progress",
            "yaml_path": "review.exchange.loop.max_no_progress",
        },
    )
    exchange_require_validation: bool = Field(
        True,
        title="Exchange Requires Validation",
        description="Require a validation record before reviewer can approve",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Disable only if you accept reviewer approvals without validation.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_require_validation",
            "yaml_path": "review.exchange.loop.require_validation",
        },
    )
    max_consecutive_review_exchange_failures: int = Field(
        3,
        title="Max Consecutive Review-Exchange Failures",
        description=(
            "Escalate to needs-human after this many consecutive "
            "review-exchange runs ended in reviewer/coder no-completion timeouts."
        ),
        ge=1,
        le=20,
        json_schema_extra={
            "doc_examples": ["2", "3", "5"],
            "doc_notes": (
                "Bounds the runaway loop where a reviewer agent keeps timing "
                "out without writing its verdict file. Each consecutive "
                "no-completion summary on the same coding session counts; "
                "any clean (non-error) summary, scratch-reset boundary, or "
                "different reason resets the count."
            ),
            "section": "Code Review Workflow",
            "config_attr": "max_consecutive_review_exchange_failures",
            "yaml_path": "review.max_consecutive_review_exchange_failures",
        },
    )
    post_publish_checks_pending_timeout_seconds: float = Field(
        1800.0,
        title="Post-Publish Checks-Pending Timeout (seconds)",
        description=(
            "How long the orchestrator waits for required GitHub checks to "
            "finalize after reviewer approval before escalating to "
            "needs-human."
        ),
        ge=60.0,
        le=86400.0,
        json_schema_extra={
            "doc_examples": ["1800", "3600", "5400"],
            "doc_notes": (
                "Governs ONLY the 'waiting on CI' (WAIT_FOR_CHECKS) state: "
                "mergeable_state in {unstable, blocked} with the status-check "
                "rollup reading PENDING/EXPECTED/unknown is treated as 'CI "
                "still running', so the orchestrator waits rather than "
                "triggering rework and escalates a 'checks pending too long' "
                "timeout to needs-human only after this budget elapses. Two "
                "other post-approval states are NOT bounded by this timeout "
                "and escalate immediately. (1) Unreadable checks: when a "
                "decisive PR's status-check rollup cannot be read (the "
                "configured GitHub token is missing the Checks / commit-status "
                "read scope), the orchestrator does not wait — it raises a "
                "separate 'status_rollup_permission_denied' credential/scope "
                "diagnostic right away. Repeated rollup probing and logging "
                "for that case is throttled by the status-rollup permission "
                "backoff, a separate window, not by this pending-checks "
                "timeout. (2) Branch-protection blocks (rollup=SUCCESS but "
                "mergeable_state=blocked) also escalate immediately. Tune this "
                "only to control how long to wait on pending CI."
            ),
            "section": "Code Review Workflow",
            "config_attr": "post_publish_checks_pending_timeout_seconds",
            "yaml_path": "review.post_publish.checks_pending_timeout_seconds",
        },
    )
    tech_lead_agent: Optional[str] = Field(
        None,
        title=f"{TECH_LEAD_DISPLAY_NAME} Review Agent",
        description="Agent for batch reviews (optional)",
        json_schema_extra={
            "doc_examples": ["agent:tech-lead"],
            "doc_notes": "Must match a label defined under agents.",
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead_review_agent",
            "yaml_path": "review.tech_lead_review_agent",
            "doctor_check": DOCTOR_CHECK_REFERENCES_AGENT,
            "doctor_check_condition": "review_enabled",
            "doctor_severity": DOCTOR_SEVERITY_ERROR,
        },
    )
    tech_lead_follow_up_agent: Optional[str] = Field(
        None,
        title=f"{TECH_LEAD_DISPLAY_NAME} Follow-Up Agent",
        description="Worker agent that tech-lead-proposed follow-up issues route to",
        json_schema_extra={
            "doc_examples": ["agent:developer"],
            "doc_notes": (
                "When a tech lead decision proposes a new follow-up issue, the "
                "orchestrator attaches this worker's agent label so normal "
                "discovery picks it up. Must match a worker label under agents. "
                "REQUIRED whenever review.tech_lead_review_agent is set: a "
                "configured tech lead agent makes create_issue proposals reachable, "
                "so leaving this unset fails startup validation instead of "
                "guessing by config order later."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead_follow_up_agent",
            "yaml_path": "review.tech_lead_follow_up_agent",
        },
    )
    tech_lead_threshold: int = Field(
        0,
        title=f"{TECH_LEAD_DISPLAY_NAME} Threshold",
        description="Trigger tech lead after N PRs (0 = manual only)",
        ge=0,
        le=50,
        json_schema_extra={
            "doc_examples": ["0", "5", "10"],
            "doc_notes": "Set to 0 to only trigger tech lead manually.",
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead_review_threshold",
            "yaml_path": "review.tech_lead_review_threshold",
        },
    )
    tech_lead_review_label: Optional[str] = Field(
        None,
        title=f"{TECH_LEAD_DISPLAY_NAME} Review Label",
        description="Label marking PRs that await tech lead review (optional)",
        json_schema_extra={
            "doc_examples": ["needs-tech-lead-review"],
            "doc_notes": "Falls back to code_reviewed_label when not set.",
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead_review_label",
            "yaml_path": "review.tech_lead_review_label",
        },
    )
    tech_lead_reviewed_label: str = Field(
        "tech-lead-reviewed",
        title=f"{TECH_LEAD_DISPLAY_NAME} Reviewed Label",
        description="Label added to manifest PRs after tech lead completes",
        json_schema_extra={
            "doc_examples": ["tech-lead-reviewed"],
            "doc_notes": "Added to every PR in the tech lead manifest on success.",
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead_reviewed_label",
            "yaml_path": "review.tech_lead_reviewed_label",
        },
    )
    tech_lead_failed_label: str = Field(
        "tech-lead-failed",
        title=f"{TECH_LEAD_DISPLAY_NAME} Failed Label",
        description="Label added to manifest PRs when a tech lead session fails",
        json_schema_extra={
            "doc_examples": ["tech-lead-failed"],
            "doc_notes": "Added to every PR in the tech lead manifest on failure.",
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead_failed_label",
            "yaml_path": "review.tech_lead_failed_label",
        },
    )
    tech_lead_review_on_failure: bool = Field(
        True,
        title=f"{TECH_LEAD_DISPLAY_NAME} on Session Failure",
        description="Queue a tech lead investigation when sessions fail",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Disable to only tech lead PR batches, not failures.",
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead_review_on_failure",
            "yaml_path": "review.tech_lead_review_on_failure",
        },
    )
    tech_lead_dedup_enabled: bool = Field(
        True,
        title=f"Enable {TECH_LEAD_DISPLAY_NAME} Follow-Up Dedup",
        description="Check tech-lead follow-up proposals against all open issues",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": (
                "When enabled, the orchestrator maintains a rebuildable local "
                "fingerprint cache of every open GitHub issue. Agent-cited "
                "duplicates are verified against that corpus, and the lexical "
                "backstop gates strong matches instead of filing unchecked "
                "duplicates. Disable only to intentionally bypass this safety gate."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.dedup.enabled",
            "yaml_path": "tech_lead.dedup.enabled",
        },
    )
    tech_lead_dedup_similarity_threshold: float = Field(
        0.72,
        title=f"{TECH_LEAD_DISPLAY_NAME} Dedup Similarity Threshold",
        description="Lexical similarity at or above which a follow-up is gated",
        gt=0.0,
        le=1.0,
        json_schema_extra={
            "doc_examples": ["0.72", "0.8", "0.9"],
            "doc_notes": (
                "Higher values reduce false-positive duplicate gates; lower "
                "values catch looser shared-vocabulary matches. Must be greater "
                "than 0.0 and at most 1.0."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.dedup.similarity_threshold",
            "yaml_path": "tech_lead.dedup.similarity_threshold",
        },
    )
    # Graduated tech lead authority (ADR-0031). Each key is constrained to
    # TECH_LEAD_AUTHORITY_MODES — the same set YAML loading validates against —
    # exposed as an `enum` select plus a POST-time validator (a Literal type
    # is deliberately avoided; see merge_queue.provider for the rationale).
    # escalate_to_human is intentionally absent: it is the non-configurable
    # floor and always executes.
    tech_lead_authority_post_comment: str = Field(
        "execute",
        title=f"{TECH_LEAD_DISPLAY_NAME} Authority: Post Comment",
        description="Execute or surface tech-lead-proposed diagnosis comments",
        json_schema_extra={
            "enum": list(TECH_LEAD_AUTHORITY_MODES),
            "doc_examples": ["execute", "propose"],
            "doc_notes": (
                "execute posts the proposed comment; propose (shadow mode) "
                "surfaces it as would-have-done. Allowed values: execute, propose."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.authority.post_comment",
            "yaml_path": "tech_lead.authority.post_comment",
        },
    )
    tech_lead_authority_create_issue: str = Field(
        "execute",
        title=f"{TECH_LEAD_DISPLAY_NAME} Authority: Create Issue",
        description="Execute or gate tech-lead-proposed follow-up issues",
        json_schema_extra={
            "enum": list(TECH_LEAD_AUTHORITY_MODES),
            "doc_examples": ["execute", "propose"],
            "doc_notes": (
                "execute files the proposed follow-up issue directly; propose "
                "files it as a gated proposal issue carrying the proposed-tech-lead "
                "label, inert until an operator removes that label (per-instance "
                "approval, #6778). Allowed values: execute, propose."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.authority.create_issue",
            "yaml_path": "tech_lead.authority.create_issue",
        },
    )
    tech_lead_authority_flag_pattern: str = Field(
        "execute",
        title=f"{TECH_LEAD_DISPLAY_NAME} Authority: Flag Pattern",
        description="Open/append durable pattern case-file issues for recurring cross-job patterns",
        json_schema_extra={
            "enum": list(TECH_LEAD_AUTHORITY_MODES),
            "doc_examples": ["execute", "propose"],
            "doc_notes": (
                "execute opens a durable pattern case-file issue the first time "
                "a pattern_signature is observed and appends an evidence comment "
                "to that same case file on every repeat observation (one case "
                "file per signature, #6781), so cross-job pattern evidence "
                "accrues for later health reviews to mine; it also emits the "
                "pattern trace event. propose (shadow mode) records only a "
                "would-have-done proposal and opens no case file. Every "
                "flag_pattern action MUST carry a pattern_signature (the "
                "case-file ledger key) or the tech lead decision is rejected. "
                "Allowed values: execute, propose."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.authority.flag_pattern",
            "yaml_path": "tech_lead.authority.flag_pattern",
        },
    )
    tech_lead_authority_reset_retry: str = Field(
        "propose",
        title=f"{TECH_LEAD_DISPLAY_NAME} Authority: Reset & Retry",
        description="Act-level: reset-and-retry an issue from scratch",
        json_schema_extra={
            "enum": list(TECH_LEAD_AUTHORITY_MODES),
            "doc_examples": ["propose", "execute"],
            "doc_notes": (
                "execute runs the reset+retry-from-scratch owner after "
                "re-validating the proposal's preconditions at execution time; "
                "stale proposals downgrade to a surfaced record (#6764). "
                "propose (default) files each proposal as a gated GitHub issue "
                "carrying the proposed-tech-lead label; removing the label is "
                "per-instance approval and triggers the same re-validated "
                "execution (#6778). Allowed values: execute, propose."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.authority.reset_retry",
            "yaml_path": "tech_lead.authority.reset_retry",
        },
    )
    tech_lead_authority_kill_hung_session: str = Field(
        "propose",
        title=f"{TECH_LEAD_DISPLAY_NAME} Authority: Kill Hung Session",
        description="Act-level: terminate a stuck session",
        json_schema_extra={
            "enum": list(TECH_LEAD_AUTHORITY_MODES),
            "doc_examples": ["propose"],
            "doc_notes": (
                "propose (default) files each proposal as a gated GitHub issue "
                "carrying the proposed-tech-lead label; removing the label is "
                "per-instance approval and executes the stored op after "
                "re-validating the target session is still active (#6778). "
                "Direct execute is not wired yet (#6764) and remains a startup "
                "configuration error. Allowed values: execute, propose."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.authority.kill_hung_session",
            "yaml_path": "tech_lead.authority.kill_hung_session",
        },
    )
    tech_lead_health_review_interval_minutes: int = Field(
        0,
        title="Health Review Interval (minutes)",
        description="Create a periodic health-review issue every N minutes (0 = disabled)",
        ge=0,
        json_schema_extra={
            "doc_examples": ["0", "240"],
            "doc_notes": (
                "ADR-0031 §4: when the interval elapses the orchestrator files "
                "a health-review anchor issue for the tech lead agent to walk the "
                "board snapshot. Requires a configured tech lead agent. 0 disables."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.health_review.interval_minutes",
            "yaml_path": "tech_lead.health_review.interval_minutes",
        },
    )
    tech_lead_health_review_storm_threshold: int = Field(
        3,
        title="Health Review Storm Threshold",
        description=(
            "Escalate this many recent blocked/failed issues into one health "
            "review (0 = disabled)"
        ),
        ge=0,
        le=100,
        json_schema_extra={
            "doc_examples": ["0", "3", "5"],
            "doc_notes": (
                "When the threshold is reached inside the configured storm "
                "window, the orchestrator creates one immediate, unscheduled "
                "health review and suppresses individual investigations for "
                "the cohort. The periodic interval remains independent."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.health_review.storm_threshold",
            "yaml_path": "tech_lead.health_review.storm_threshold",
        },
    )
    tech_lead_health_review_storm_window_minutes: int = Field(
        5,
        title="Health Review Storm Window (minutes)",
        description="Time window used to group blocked/failed problem issues",
        ge=1,
        le=1440,
        json_schema_extra={
            "doc_examples": ["1", "5", "15"],
            "doc_notes": (
                "Problems observed within this window count toward the storm "
                "threshold. This window groups reactions; it does not delay "
                "ordinary failure investigations."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.health_review.storm_window_minutes",
            "yaml_path": "tech_lead.health_review.storm_window_minutes",
        },
    )

    tech_lead_stuck_sweep_enabled: bool = Field(
        False,
        title="Enable Tech-Lead Stuck Sweep",
        description="Re-inject terminally-stuck issues into reactive tech lead (0 = disabled)",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": (
                "ADR-0031 (#6823): a bounded, timer-gated backstop that finds "
                "open issues stuck in a terminal blocking state the normal loop "
                "cannot re-discover and re-injects each into the reactive-tech-lead "
                "pipeline as a recovered failure. Requires a configured tech lead "
                "agent and tech_lead_review_on_failure. Off by default."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.stuck_sweep.enabled",
            "yaml_path": "tech_lead.stuck_sweep.enabled",
        },
    )
    tech_lead_stuck_sweep_interval_minutes: int = Field(
        240,
        title="Stuck Sweep Interval (minutes)",
        description="Scan for stuck issues every N minutes (>= 1; disable via 'enabled')",
        ge=1,
        json_schema_extra={
            "doc_examples": ["240", "60", "480"],
            "doc_notes": (
                "How often the tech-lead sweep scans open issues for terminal "
                "stuck state. Only runs when the sweep is enabled. The scan is a "
                "broad exhaustive open-issue read, so this is a reconcile-for-"
                "strays BACKSTOP cadence, not a hot path — the 4h default keeps "
                "that read off the frequent paths; lower it only when faster "
                "recovery of stranded issues is worth the extra scans."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.stuck_sweep.interval_minutes",
            "yaml_path": "tech_lead.stuck_sweep.interval_minutes",
        },
    )
    tech_lead_stuck_sweep_max_recovery_attempts: int = Field(
        3,
        title="Stuck Sweep Max Recovery Attempts",
        description="Re-inject a stuck issue at most this many times before escalating",
        ge=1,
        le=100,
        json_schema_extra={
            "doc_examples": ["1", "3", "5"],
            "doc_notes": (
                "After this many recovery attempts a stuck issue is no longer "
                "re-injected (no infinite loop); it is surfaced as exhausted and "
                "needs human attention. The counter is durable across restarts."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.stuck_sweep.max_recovery_attempts",
            "yaml_path": "tech_lead.stuck_sweep.max_recovery_attempts",
        },
    )
    tech_lead_max_concurrent: Optional[int] = Field(
        None,
        title="Reserved Tech Lead Concurrency",
        description=(
            "Reserved concurrency slots for tech lead sessions (empty = share the "
            "worker budget)"
        ),
        ge=1,
        le=20,
        json_schema_extra={
            "doc_examples": ["1", "2"],
            "doc_notes": (
                "Empty (the default) shares the worker budget "
                "(execution.concurrency.max_concurrent_sessions): tech lead counts "
                "against it and is planned from the shared capacity, exactly as "
                "before. A positive value is a SEPARATE additive tech lead budget: "
                "tech lead sessions run from their own slots and are NOT subtracted "
                "from the worker budget, so the tech lead can run even when "
                "workers are saturated. Total live agents are then bounded at "
                "max_concurrent_sessions + tech_lead.max_concurrent."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.max_concurrent",
            "yaml_path": "tech_lead.max_concurrent",
        },
    )
    tech_lead_max_expedited: int = Field(
        3,
        title="Max Expedited Tech Lead Issues",
        description=(
            "Cap on outstanding tech-lead-expedited issues at the front of the "
            f"worker queue (valid range 0-{TECH_LEAD_MAX_EXPEDITED_LIMIT}; 0 "
            "disables the expedite lane)"
        ),
        ge=0,
        le=TECH_LEAD_MAX_EXPEDITED_LIMIT,
        json_schema_extra={
            "doc_examples": ["0", "3", "5"],
            "doc_notes": (
                "When the tech lead files an urgent create_issue follow-up "
                "(expedite=true), the orchestrator jumps it to the front of the "
                "worker lane via the same priority queue operators use. This caps "
                "how many such issues can be outstanding at once so a noisy tech "
                f"lead cannot starve normal work. Valid range is 0-"
                f"{TECH_LEAD_MAX_EXPEDITED_LIMIT} (values outside it fail "
                "validation at both startup and in the settings form); further "
                "expedite requests past the cap are logged and fall back to "
                "normal priority. 0 disables expediting entirely. Under "
                "'propose' authority an expedited follow-up jumps the lane only "
                "after its proposed-tech-lead gate is removed."
            ),
            "section": _TECH_LEAD_SECTION,
            "config_attr": "tech_lead.max_expedited",
            "yaml_path": "tech_lead.max_expedited",
        },
    )

    @field_validator(
        "tech_lead_authority_post_comment",
        "tech_lead_authority_create_issue",
        "tech_lead_authority_flag_pattern",
        "tech_lead_authority_reset_retry",
        "tech_lead_authority_kill_hung_session",
    )
    @classmethod
    def _validate_tech_lead_authority_mode(cls, value: str) -> str:
        if value not in TECH_LEAD_AUTHORITY_MODES:
            raise ValueError(
                f"tech lead authority mode must be one of"
                f" {list(TECH_LEAD_AUTHORITY_MODES)}, got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _health_review_interval_requires_tech_lead_agent(self) -> "ReviewSettings":
        # Cross-field invariant (#6763/#6776): a positive health-review
        # interval without a configured tech lead agent would be silently
        # disabled at runtime. Reject the pair so the misconfiguration is
        # loud instead of degrading (0 disables; a positive interval needs
        # an agent to walk the board).
        if self.tech_lead_health_review_interval_minutes > 0 and not self.tech_lead_agent:
            raise ValueError(
                "tech_lead.health_review.interval_minutes is "
                f"{self.tech_lead_health_review_interval_minutes} but no tech lead "
                "agent is configured; set review.tech_lead_review_agent, or use 0 "
                "to disable the periodic health review."
            )
        return self

    @model_validator(mode="after")
    def _stuck_sweep_requires_tech_lead_agent(self) -> "ReviewSettings":
        # Cross-field invariant (#6823): the sweep re-injects stuck issues into
        # the reactive-tech-lead pipeline, so an enabled sweep with no tech lead agent
        # is silently inert at runtime. Reject the pair so it fails loudly.
        if self.tech_lead_stuck_sweep_enabled and not self.tech_lead_agent:
            raise ValueError(
                "tech_lead.stuck_sweep.enabled is true but no tech lead agent is "
                "configured; set review.tech_lead_review_agent, or disable the "
                "stuck sweep."
            )
        return self


class GoalPilotSettings(BaseModel):
    """Settings for the Goal Pilot tab."""

    enabled: bool = Field(
        False,
        title="Enable Goal Pilot",
        description="Enable the Goal Pilot AI controller",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Enable only when Goal Pilot prompts are configured and tested.",
            "section": "Goal Pilot",
            "config_attr": "goal_pilot.enabled",
            "yaml_path": "goal_pilot.enabled",
            "summary": {
                "section": "goal_pilot",
                "format": SUMMARY_ENABLED_FLAG,
                "label": "Goal Pilot",
            },
        },
    )
    agent: Optional[str] = Field(
        None,
        title="Goal Pilot Agent",
        description="Agent label to run as Goal Pilot (e.g., agent:goal-pilot)",
        json_schema_extra={
            "doc_examples": ["agent:goal-pilot"],
            "doc_notes": "Must match a label defined under agents.",
            "section": "Goal Pilot",
            "config_attr": "goal_pilot.agent",
            "yaml_path": "goal_pilot.agent",
            "doctor_check": DOCTOR_CHECK_REFERENCES_AGENT,
            "doctor_check_condition": "goal_pilot.enabled",
            "doctor_severity": DOCTOR_SEVERITY_ERROR,
        },
    )
    approval_policy: Literal["journeys_only", "gatekeeper", "batch"] = Field(
        "journeys_only",
        title="Approval Policy",
        description="How Goal Pilot applies repo changes",
        json_schema_extra={
            "doc_examples": ["journeys_only", "gatekeeper", "batch"],
            "doc_notes": "Batch mode bundles changes before approval; gatekeeper requests approval per change.",
            "section": "Goal Pilot",
            "config_attr": "goal_pilot.approval_policy",
            "yaml_path": "goal_pilot.approval_policy",
        },
    )
    approval_batch_size: int = Field(
        10,
        title="Approval Batch Size",
        description="How many changes to bundle before approval (batch mode)",
        ge=1,
        le=200,
        json_schema_extra={
            "doc_examples": ["5", "10", "25"],
            "doc_notes": "Used only when approval_policy=batch.",
            "section": "Goal Pilot",
            "config_attr": "goal_pilot.approval_batch_size",
            "yaml_path": "goal_pilot.approval_batch_size",
        },
    )
    approval_batch_window_minutes: int = Field(
        60,
        title="Approval Batch Window (minutes)",
        description="Max time to wait before asking for approval (batch mode)",
        ge=1,
        le=1440,
        json_schema_extra={
            "doc_examples": ["30", "60", "120"],
            "doc_notes": "Used only when approval_policy=batch.",
            "section": "Goal Pilot",
            "config_attr": "goal_pilot.approval_batch_window_minutes",
            "yaml_path": "goal_pilot.approval_batch_window_minutes",
        },
    )


class MergeQueueSettings(BaseModel):
    """Settings for the Merge Queue tab."""

    enabled: bool = Field(
        False,
        title="Enable Merge Queue",
        description="Enqueue approved PRs into GitHub's native merge queue",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": (
                "When enabled, approved PRs that have cleared the orchestrator "
                "gate are enqueued into the provider's merge queue instead of "
                "being reworked merely for being behind base. Requires a repo "
                "whose branch protection has the merge queue configured."
            ),
            "section": "Merge Queue",
            "config_attr": "merge_queue.enabled",
            "yaml_path": "merge_queue.enabled",
            "summary": {
                "section": "merge_queue",
                "format": SUMMARY_ENABLED_FLAG,
                "label": "Merge Queue",
            },
        },
    )
    # provider is constrained to MERGE_QUEUE_PROVIDERS (the same set YAML loading
    # validates against) so the settings form can neither render nor POST an
    # unsupported provider. A single-value Literal would emit a JSON-schema
    # `const`, which this repo's form-control projection deliberately rejects;
    # we instead expose the allowed set as an `enum` (rendered as a select) and
    # enforce it with a validator at POST time. Both derive from one source.
    provider: str = Field(
        "github",
        title="Merge Queue Provider",
        description="Which merge queue backend to use",
        json_schema_extra={
            "enum": list(MERGE_QUEUE_PROVIDERS),
            "doc_examples": ["github"],
            "doc_notes": (
                "Only GitHub's native merge queue is supported today; the value "
                "is constrained to the allowed set so the settings form rejects "
                "unsupported providers before they reach the running config."
            ),
            "section": "Merge Queue",
            "config_attr": "merge_queue.provider",
            "yaml_path": "merge_queue.provider",
        },
    )
    enqueue_after: Literal["code-reviewed", "tech-lead-reviewed"] = Field(
        "code-reviewed",
        title="Enqueue After Gate",
        description="Orchestrator gate that must pass before a PR is enqueued",
        json_schema_extra={
            "doc_examples": ["code-reviewed", "tech-lead-reviewed"],
            "doc_notes": (
                "Names the approval gate the PR must clear before enqueue. "
                "code-reviewed is the reviewer-approval gate."
            ),
            "section": "Merge Queue",
            "config_attr": "merge_queue.enqueue_after",
            "yaml_path": "merge_queue.enqueue_after",
        },
    )
    failure_action: Literal["rework", "needs_human"] = Field(
        "rework",
        title="Queue Failure Action",
        description="How to route a PR that fails the merge queue",
        json_schema_extra={
            "doc_examples": ["rework", "needs_human"],
            "doc_notes": (
                "rework sends the PR back to a coding agent; needs_human "
                "escalates it for manual attention."
            ),
            "section": "Merge Queue",
            "config_attr": "merge_queue.failure_action",
            "yaml_path": "merge_queue.failure_action",
        },
    )

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        if value not in MERGE_QUEUE_PROVIDERS:
            raise ValueError(
                f"provider must be one of {list(MERGE_QUEUE_PROVIDERS)}, got {value!r}"
            )
        return value


class AdvancedSettings(BaseModel):
    """Settings for the Advanced tab."""

    sqlite_backup_enabled: bool = Field(
        True,
        title="Enable SQLite Backups",
        description="Enable automatic backups of local SQLite state",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Disable only if backups are managed externally.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.enabled",
            "yaml_path": "sqlite_backup.enabled",
        },
    )
    sqlite_backup_cadence_hours: int = Field(
        24,
        title="Backup Cadence (hours)",
        description="Minimum hours between backups",
        ge=1,
        le=168,
        json_schema_extra={
            "doc_examples": ["6", "24", "48"],
            "doc_notes": "Lower values increase backup frequency.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.cadence_hours",
            "yaml_path": "sqlite_backup.cadence_hours",
        },
    )
    sqlite_backup_check_interval_minutes: int = Field(
        60,
        title="Backup Check Interval (minutes)",
        description="How often to check whether backups are due",
        ge=5,
        le=1440,
        json_schema_extra={
            "doc_examples": ["30", "60", "120"],
            "doc_notes": "Checks are lightweight; keep reasonably frequent.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.check_interval_minutes",
            "yaml_path": "sqlite_backup.check_interval_minutes",
        },
    )
    sqlite_backup_retention_daily: int = Field(
        14,
        title="Daily Backup Retention",
        description="Number of daily backups to keep",
        ge=0,
        le=60,
        json_schema_extra={
            "doc_examples": ["7", "14", "30"],
            "doc_notes": "Set to 0 to disable daily backups.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.retention_daily",
            "yaml_path": "sqlite_backup.retention_daily",
        },
    )
    sqlite_backup_retention_weekly: int = Field(
        8,
        title="Weekly Backup Retention",
        description="Number of weekly backups to keep",
        ge=0,
        le=52,
        json_schema_extra={
            "doc_examples": ["4", "8", "12"],
            "doc_notes": "Set to 0 to disable weekly backups.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.retention_weekly",
            "yaml_path": "sqlite_backup.retention_weekly",
        },
    )
    sqlite_backup_enforce_on_startup: bool = Field(
        True,
        title="Enforce Backup on Startup",
        description="If cadence elapsed, force a backup on startup",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Keeps backups current if the process was stopped for a while.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.enforce_on_startup",
            "yaml_path": "sqlite_backup.enforce_on_startup",
        },
    )
    timeline_max_records: int = Field(
        5000,
        title="Timeline Retention (records)",
        description="Max timeline events kept per issue before trimming",
        ge=0,
        json_schema_extra={
            "doc_examples": ["2000", "5000", "10000"],
            "doc_notes": "Set to 0 to disable trimming; higher values keep more history but grow state files faster.",
            "section": "Timeline",
            "config_attr": "timeline.max_records",
            "yaml_path": "timeline.max_records",
        },
    )

    provider_short_retry_max_attempts: int = Field(
        4,
        title="Provider Retry Attempts",
        description="Max attempts for transient provider failures",
        ge=1,
        le=10,
        json_schema_extra={
            "doc_examples": ["2", "4", "6"],
            "doc_notes": "Higher values reduce failures but can prolong degraded runs.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.short_retry.max_attempts",
            "yaml_path": "provider_resilience.short_retry.max_attempts",
        },
    )
    provider_short_retry_initial_backoff_seconds: int = Field(
        5,
        title="Provider Retry Backoff (seconds)",
        description="Initial backoff for transient provider retries",
        ge=1,
        le=300,
        json_schema_extra={
            "doc_examples": ["2", "5", "10"],
            "doc_notes": "Shorter backoffs retry faster but can amplify rate limits.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.short_retry.initial_backoff_seconds",
            "yaml_path": "provider_resilience.short_retry.initial_backoff_seconds",
        },
    )
    provider_short_retry_max_backoff_seconds: int = Field(
        60,
        title="Provider Retry Max Backoff (seconds)",
        description="Maximum backoff for transient provider retries",
        ge=1,
        le=3600,
        json_schema_extra={
            "doc_examples": ["30", "60", "120"],
            "doc_notes": "Caps exponential backoff to avoid excessive waiting.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.short_retry.max_backoff_seconds",
            "yaml_path": "provider_resilience.short_retry.max_backoff_seconds",
        },
    )
    provider_short_retry_jitter: bool = Field(
        True,
        title="Provider Retry Jitter",
        description="Apply full jitter to provider retry backoff",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Keep enabled to avoid synchronized retry storms.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.short_retry.jitter",
            "yaml_path": "provider_resilience.short_retry.jitter",
        },
    )
    provider_circuit_cooldown_seconds: int = Field(
        1800,
        title="Provider Cooldown (seconds)",
        description="Cooldown window before retrying provider after outage",
        ge=60,
        le=86400,
        json_schema_extra={
            "doc_examples": ["600", "1800", "3600"],
            "doc_notes": "Longer cooldowns reduce repeated failures during incidents.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.circuit_breaker.cooldown_seconds",
            "yaml_path": "provider_resilience.circuit_breaker.cooldown_seconds",
        },
    )
    provider_circuit_max_cooldowns: int = Field(
        6,
        title="Provider Max Cooldowns",
        description="Maximum cooldown escalation steps",
        ge=1,
        le=12,
        json_schema_extra={
            "doc_examples": ["3", "6", "8"],
            "doc_notes": "Limits how long we will keep extending cooldowns.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.circuit_breaker.max_cooldowns",
            "yaml_path": "provider_resilience.circuit_breaker.max_cooldowns",
        },
    )
    provider_circuit_label: str = Field(
        "blocked:provider-unavailable",
        title="Provider Blocked Label",
        description="Label applied when provider is unavailable",
        json_schema_extra={
            "doc_examples": ["blocked:provider-unavailable"],
            "doc_notes": "Use a label that is visible and searchable in your workflow.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.circuit_breaker.label",
            "yaml_path": "provider_resilience.circuit_breaker.label",
        },
    )
    session_interactions_enabled: bool = Field(
        False,
        title="Enable Session Interaction Rules",
        description="Allow the orchestrator to auto-respond to trusted prompts in running agent sessions",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Off by default. Enable only if you want runner-managed prompt responses such as Claude's initial trust confirmation.",
            "section": "Interactive Sessions",
            "restart_required": True,
            "config_attr": "session_interactions.enabled",
            "yaml_path": "execution.session_interactions.enabled",
        },
    )

    session_no_output_seconds: int = Field(
        120,
        title="No-Output Threshold (seconds)",
        description="Emit event after this much idle time",
        ge=30,
        le=600,
        json_schema_extra={
            "doc_examples": ["60", "120", "300"],
            "doc_notes": "Lower values surface silent sessions sooner.",
            "section": "Observability",
            "config_attr": "session_no_output_seconds",
            "yaml_path": "observability.session_no_output_seconds",
        },
    )
    stale_escalation_ticks: int = Field(
        0,
        title="Stale Escalation Ticks",
        description="Escalate after K consecutive stale ticks (0 = disabled)",
        ge=0,
        le=20,
        json_schema_extra={
            "doc_examples": ["0", "3", "5"],
            "doc_notes": "Set to 0 to disable automatic escalation.",
            "section": "Observability",
            "config_attr": "stale_escalation_ticks",
            "yaml_path": "observability.stale_escalation_ticks",
        },
    )
    session_output_retention_days: int = Field(
        7,
        title="Session Output Retention (days)",
        description="Retention window in days for session run artifacts",
        ge=0,
        le=365,
        json_schema_extra={
            "doc_examples": ["0", "7", "30"],
            "doc_notes": "Set to 0 to expire immediately; cleanup policy may still defer deletion.",
            "section": "Observability",
            "config_attr": "session_output_retention_days",
            "yaml_path": "observability.session_output_retention_days",
        },
    )
    session_output_retention_tier: Literal["hot", "cold"] = Field(
        "hot",
        title="Session Output Retention Tier",
        description="Retention tier tag recorded in run manifests",
        json_schema_extra={
            "doc_examples": ["hot", "cold"],
            "doc_notes": "Use hot for short-term troubleshooting and cold for longer forensic retention.",
            "section": "Observability",
            "config_attr": "session_output_retention_tier",
            "yaml_path": "observability.session_output_retention_tier",
        },
    )
    web_port: int = Field(
        0,
        title="Web Dashboard Port",
        description="Port for the web dashboard. 0 = auto-assign free port (requires restart)",
        ge=0,
        le=65535,
        json_schema_extra={
            "doc_examples": ["0", "8080", "3000", "9090"],
            "doc_notes": "0 = auto-assign a free port. Use a fixed port for bookmarkable URLs.",
            "section": "Ports",
            "restart_required": True,
            "config_attr": "web_port",
            "yaml_path": "ui.web_port",
            "setup": {
                "enabled": True,
                "section": "ui",
                "order": 10,
                "condition": {"field": "ui_mode", "value": "web"},
            },
        },
    )
    control_api_port: int = Field(
        0,
        title="Control API Port",
        description="0 = auto-assign free port",
        ge=0,
        le=65535,
        json_schema_extra={
            "doc_examples": ["0", "19080", "19081"],
            "doc_notes": "0 = auto-assign a free port. Allows multiple instances to coexist.",
            "section": "Ports",
            "restart_required": True,
            "config_attr": "control_api_port",
            "yaml_path": "ui.control_api_port",
        },
    )
    browser_session_ttl_seconds: int = Field(
        8 * 3600,
        title="Browser Session TTL (seconds)",
        description=(
            "How long a Control Center login is valid before it expires and "
            "the operator must re-enter the admin token. Overridable at "
            "runtime via ISSUE_ORCHESTRATOR_SESSION_TTL_SECONDS."
        ),
        ge=60,
        le=30 * 24 * 3600,
        json_schema_extra={
            "doc_examples": ["3600", "28800", "86400"],
            "doc_notes": (
                "Minimum 60 s. Shorter values reduce the window a stolen "
                "cookie is useful; longer values reduce re-login friction."
            ),
            "section": "Browser Session",
            "restart_required": True,
            "config_attr": "browser_session_ttl_seconds",
            "yaml_path": "ui.browser_session.ttl_seconds",
        },
    )
    browser_session_max: int = Field(
        1024,
        title="Max Concurrent Browser Sessions (deprecated, no-op)",
        description=(
            "Deprecated. Browser sessions are now stateless cookies validated "
            "by HMAC, so there is no in-memory table to cap. The field is "
            "still accepted for back-compat with operator YAML but the value "
            "is ignored at runtime."
        ),
        ge=16,
        le=65536,
        json_schema_extra={
            "doc_examples": ["1024"],
            "doc_notes": (
                "Deprecated and ignored as of the cross-process session "
                "change. Stateless cookies removed the in-memory cap; this "
                "field exists only so existing YAML continues to validate. "
                "Safe to remove from your config."
            ),
            "section": "Browser Session",
            "restart_required": True,
            "config_attr": "browser_session_max",
            "yaml_path": "ui.browser_session.max",
        },
    )
    sse_token_ttl_seconds: int = Field(
        60,
        title="SSE Query-String Token TTL (seconds)",
        description=(
            "How long a /api/sse-token response is valid before the browser "
            "must request a fresh one. Tokens are single-use within their "
            "window. Overridable via ISSUE_ORCHESTRATOR_SSE_TOKEN_TTL_SECONDS."
        ),
        ge=5,
        le=3600,
        json_schema_extra={
            "doc_examples": ["30", "60", "300"],
            "doc_notes": (
                "Shorter is safer — a token in an access log or Referer "
                "header becomes useless faster. The browser re-requests on "
                "every reconnect so operator-visible reconnection latency "
                "is unchanged."
            ),
            "section": "Browser Session",
            "restart_required": True,
            "config_attr": "sse_token_ttl_seconds",
            "yaml_path": "ui.browser_session.sse_token_ttl_seconds",
        },
    )
    ai_systems_allowed: str = Field(
        "",
        title="AI Systems Allowlist",
        description="Additional ai_system values allowed in config (comma-separated)",
        json_schema_extra={
            "doc_examples": ["codex, custom-system", ""],
            "doc_notes": "Use to allow new providers beyond ai_systems.yaml.",
            "section": "AI Systems",
            "config_attr": "ai_systems_allowed",
            "yaml_path": "ai_systems.allowed",
            "ui_transform": "comma_separated_list",
        },
    )
    worktree_base: str = Field(
        "../",
        title="Worktree Base Directory",
        description="Directory where git worktrees are created",
        min_length=1,
        json_schema_extra={
            "doc_examples": ["../", "../worktrees", "/tmp/worktrees"],
            "doc_notes": "Relative paths are resolved from the repo root.",
            "section": "Worktrees",
            "restart_required": True,
            "config_value_type": CONFIG_VALUE_TYPE_PATH,
            "config_attr": "worktree_base",
            "yaml_path": "worktrees.base",
            "setup": {
                "enabled": True,
                "section": "worktrees",
                "order": 10,
            },
        },
    )
    worktree_base_branch_override: str | None = Field(
        None,
        title="Worktree Base Branch Override",
        description="Override the base branch for worktree creation (auto-detect if unset)",
        json_schema_extra={
            "doc_examples": ["main", "master"],
            "doc_notes": "Use when your default branch is not auto-detected correctly.",
            "section": "Worktrees",
            "restart_required": True,
            "config_attr": "worktree_base_branch_override",
            "yaml_path": "worktrees.base_branch_override",
        },
    )
    worktree_seed_ref: str | None = Field(
        None,
        title="Worktree Seed Ref",
        description="Optional local ref used to seed fresh issue worktrees before review/PR creation",
        json_schema_extra={
            "doc_examples": ["HEAD", "main", "fc42d4c"],
            "doc_notes": "Use for local iteration when fresh issue worktrees should inherit a specific local ref.",
            "section": "Worktrees",
            "restart_required": True,
            "config_attr": "worktree_seed_ref",
            "yaml_path": "worktrees.seed_ref",
        },
    )
    worktree_branch_on_recreate: Literal["delete", "create_new_branch"] = Field(
        "delete",
        title="Branch on Recreate",
        description="What to do when recreating a worktree with existing branch",
        json_schema_extra={
            "doc_examples": ["delete", "create_new_branch"],
            "doc_notes": "Use create_new_branch to keep the old branch intact.",
            "section": "Worktrees",
            "restart_required": True,
            "config_attr": "worktree_branch_on_recreate",
            "yaml_path": "worktrees.worktree_branch_on_recreate",
        },
    )
    worktree_setup: str = Field(
        "",
        title="Worktree Setup Commands",
        description="Commands to run in each new worktree after creation (one per line)",
        json_schema_extra={
            "doc_examples": ["npm install", "pip install -e '.[dev]'", "make setup"],
            "doc_notes": "Each command runs in the worktree directory. Leave empty if no setup needed. "
            "The orchestrator's own setup (hooks, coding-done, reviewer-done, Claude settings) is automatic.",
            "section": "Worktrees",
            "restart_required": True,
            "config_attr": "setup_worktree",
            "yaml_path": "worktrees.setup",
            "ui_transform": "newline_separated_list",
            "setup": {
                "enabled": True,
                "section": "worktrees",
                "order": 20,
            },
        },
    )


class HooksSettings(BaseModel):
    """Settings for the Hooks tab."""

    ai_gate_interval_days: int = Field(
        7,
        title="AI Gate Test Interval (days)",
        description="Run AI gate tests every N days (0 = disabled)",
        ge=0,
        le=365,
        json_schema_extra={
            "doc_examples": ["0", "7", "30"],
            "doc_notes": "Set to 0 to disable periodic AI gate tests.",
            "section": "AI Gate",
            "config_attr": "hooks.ai_gate.interval_days",
            "yaml_path": "hooks.ai_gate.interval_days",
            "summary": {
                "section": "hooks",
                "format": SUMMARY_INTERVAL,
                "label": "ai gate",
                "zero_label": "disabled",
                "unit": "d",
            },
        },
    )
    ai_gate_dangerous_allow_failure: bool = Field(
        False,
        title="Allow AI Gate Failure",
        description="If true, warn only on AI gate failure; if false, block orchestrator start",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Keep false in production to enforce hook integrity.",
            "section": "AI Gate",
            "config_attr": "hooks.ai_gate.dangerous_allow_failure",
            "yaml_path": "hooks.ai_gate.dangerous_allow_failure",
            "summary": {
                "section": "hooks",
                "format": SUMMARY_BOOLEAN_FLAG,
                "label": "on failure",
                "true_value": "warn",
                "false_value": "block",
            },
        },
    )


# ---------------------------------------------------------------------------
# Tab definitions (ordered as they appear in the UI)
# ---------------------------------------------------------------------------

TAB_DEFINITIONS: list[dict[str, Any]] = [
    {"key": "concurrency", "label": "Concurrency", "model": ConcurrencySettings},
    {"key": "e2e", "label": "E2E Runner", "model": E2ESettings},
    {"key": "validation", "label": "Validation", "model": ValidationSettings},
    {"key": "filtering", "label": "Filtering", "model": FilteringSettings},
    {"key": "milestones", "label": "Milestones", "model": MilestonesSettings},
    {"key": "review", "label": "Review", "model": ReviewSettings},
    {"key": "merge_queue", "label": "Merge Queue", "model": MergeQueueSettings},
    {"key": "goal_pilot", "label": "Goal Pilot", "model": GoalPilotSettings},
    {"key": "hooks", "label": "Hooks", "model": HooksSettings},
    {"key": "advanced", "label": "Advanced", "model": AdvancedSettings},
]


# ---------------------------------------------------------------------------
# Config <-> Schema bridge
# ---------------------------------------------------------------------------

def from_config(config: Config) -> dict[str, BaseModel]:
    """Build all tab models from a Config object.

    Returns a dict mapping tab key -> Pydantic model instance with current values.
    """
    return build_tabs_from_config(TAB_DEFINITIONS, config)


def apply_to(tabs: dict[str, BaseModel], config: Config) -> bool:
    """Apply all tab models to a Config object.

    Returns True if any field marked restart_required changed.
    """
    return apply_tabs_to_config(TAB_DEFINITIONS, tabs, config)


def build_save_plan(
    snapshot: dict[str, BaseModel], submitted: dict[str, BaseModel]
) -> SettingsSavePlan:
    """Build the field-granular settings-save patch plan.

    Settings-save persistence-policy owner entry point and the persistence
    counterpart to :func:`apply_to`. Compares the submitted tab models against
    the :func:`from_config` snapshot and returns a :class:`SettingsSavePlan`
    carrying only the changed settings-owned ``yaml_path`` entries (reverse UI
    transforms applied), with an explicit :attr:`SettingsSavePlan.is_empty`
    no-op outcome.

    Compose it with
    :func:`~.config_document_patch.save_config_document_patch` via
    ``plan.apply`` so a save patches only edited fields into the parsed on-disk
    YAML -- preserving unrelated operational config (``repo.github`` auth,
    merge queue, hooks, ...) AND unedited settings-owned raw values (a sibling
    ``${SECRET}`` reference is not expanded) -- and skips the file write
    entirely for a no-op. See
    :func:`~.settings_schema_support.build_settings_save_plan` for the full
    rationale.
    """
    return build_settings_save_plan(TAB_DEFINITIONS, snapshot, submitted)


def get_restart_fields() -> set[str]:
    """Return field names that require restart when changed."""
    return collect_restart_fields(TAB_DEFINITIONS)


# ---------------------------------------------------------------------------
# JSON Schema generation (cached for template rendering)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def get_settings_json_schema() -> dict[str, Any]:
    """Generate per-tab JSON schemas for template rendering.

    Returns a dict mapping tab key -> JSON schema dict.
    The schema includes x_extra with section, restart_required, etc.
    """
    return build_settings_json_schema(TAB_DEFINITIONS)


# ---------------------------------------------------------------------------
# Metadata accessor for wizard / docs
# ---------------------------------------------------------------------------

def get_field_meta(tab_key: str, field_name: str) -> dict[str, Any]:
    """Get schema metadata for a specific field.

    Returns dict with 'title', 'description', 'default', and any json_schema_extra.
    """
    return field_meta(TAB_DEFINITIONS, tab_key, field_name)


# ---------------------------------------------------------------------------
# Setup wizard field extraction (data-driven)
# ---------------------------------------------------------------------------

def get_setup_fields(section: str) -> list[dict[str, Any]]:
    """Get schema fields for a wizard section, sorted by order.

    Returns a list of field metadata dicts with keys:
        name, title, description, default, type, order, prompt, condition, tab_key
    """
    return setup_fields(TAB_DEFINITIONS, section)


# ---------------------------------------------------------------------------
# Doctor check field extraction (data-driven)
# ---------------------------------------------------------------------------

def get_doctor_check_fields() -> list[dict[str, Any]]:
    """Get all schema fields that have doctor_check annotations.

    Returns a list of field metadata dicts with keys:
        name, doctor_check, doctor_check_condition, doctor_severity, config_attr,
        title, tab_key, ui_transform
    """
    return doctor_check_fields(TAB_DEFINITIONS)


def get_summary_fields(section: str) -> list[dict[str, Any]]:
    """Get schema fields that contribute to a doctor status summary.

    Returns a list of field metadata dicts with summary format info.
    """
    return summary_fields(TAB_DEFINITIONS, section)


# ---------------------------------------------------------------------------
# Documentation generation
# ---------------------------------------------------------------------------

def generate_config_reference() -> str:
    """Generate markdown configuration reference from schema.

    Returns a markdown string with tables for each tab.
    """
    return generate_reference_markdown(TAB_DEFINITIONS)
