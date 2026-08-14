"""Dialog view models for the web UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args

from ..domain.artifact_contracts import (
    ValidationFailed,
    ValidationOutcome,
    ValidationPassed,
    ValidationRetry,
)
from .session_diagnostics import (
    SessionDiagnosticAnalysis,
    SessionDiagnosticFollowUpIssue,
    SessionDiagnosticsContext,
    present_session_evidence,
)

@dataclass(frozen=True)
class DialogRow:
    label: str
    value: str
    value_kind: Literal["timestamp"] | None = None

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "value": self.value} | ({"value_kind": self.value_kind} if self.value_kind else {})


@dataclass(frozen=True)
class DialogSection:
    title: str
    rows: list[DialogRow]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "rows": [row.to_dict() for row in self.rows],
        }


def _outcome_reason(outcome: ValidationOutcome | None) -> str | None:
    """Project failure/retry outcomes down to their diagnostic reason."""
    if isinstance(outcome, (ValidationFailed, ValidationRetry)):
        return outcome.reason
    return None


def _build_session_diagnostics_rows(ctx: SessionDiagnosticsContext) -> list[DialogRow]:
    evidence = present_session_evidence(ctx)
    rows = [
        DialogRow("Session", ctx.session_name or "-"),
        DialogRow("Started", ctx.started_at or "-", value_kind="timestamp"),
        DialogRow("Run ID", ctx.run_id or "-"),
        DialogRow("Backend", ctx.backend or "-"),
        DialogRow("Agent", ctx.agent_label or "-"),
        DialogRow("Task", ctx.task or "-"),
        DialogRow("Branch", ctx.branch or "-"),
        DialogRow("Provider", ctx.provider or "-"),
        DialogRow("Model", ctx.model or "-"),
        DialogRow("Permission Mode", ctx.permission_mode or "-"),
        DialogRow("Timeout", f"{ctx.timeout_minutes}m" if ctx.timeout_minutes else "-"),
        DialogRow("Provider Args", ctx.extra_provider_args or "-"),
        DialogRow("Launch Args", ctx.claude_args or "-"),
        DialogRow("Prompt Mode", ctx.claude_prompt_mode or "-"),
        DialogRow("Claude Session", ctx.claude_session_id or "-"),
        DialogRow("Retention Tier", ctx.retention_tier or "-"),
    ]
    rows.extend(
        DialogRow(row.label, row.value, value_kind=row.value_kind)
        for row in evidence.rows
    )
    rows.append(DialogRow("Worktree", ctx.worktree or "-"))
    # Project the typed outcome into Status + Reason rows. The union
    # guarantees: passed has no reason field at all (so the stale-
    # reason-on-success bug surfaces here as an absent Reason row,
    # not a contradiction); failed/retry carry a non-empty reason
    # by construction.
    outcome = ctx.validation_outcome
    if isinstance(outcome, ValidationPassed):
        rows.append(DialogRow("Validation Status", "passed"))
    elif isinstance(outcome, ValidationFailed):
        rows.append(DialogRow("Validation Status", "failed"))
        rows.append(DialogRow("Validation Reason", outcome.reason))
    elif isinstance(outcome, ValidationRetry):
        rows.append(DialogRow("Validation Status", "retry"))
        rows.append(DialogRow("Validation Reason", outcome.reason))
    return rows


def _build_session_diagnostics_actions(ctx: SessionDiagnosticsContext) -> list[dict[str, Any]]:
    evidence = present_session_evidence(ctx)
    if not evidence.artifacts_available:
        return []
    actions: list[dict[str, Any]] = []
    _append_open_path(actions, "Open Session Dir", ctx.run_dir, group="diagnostics")
    _append_open_path(actions, "Open Session Settings", ctx.session_settings_path, group="diagnostics")
    _append_run_scoped_action(
        actions,
        ctx,
        action_type="open_agent_log",
        label="View Session Recording",
        group="session_evidence",
    )
    _append_run_scoped_action(
        actions,
        ctx,
        action_type="copy_agent_log",
        label="Copy Session Recording",
        group="session_evidence",
    )
    if ctx.claude_log_path:
        _append_run_scoped_action(
            actions,
            ctx,
            action_type="view_claude_log",
            label="View Claude Log",
            group="session_evidence",
        )
        _append_open_path(actions, "Open Claude Log File", ctx.claude_log_path, group="session_evidence")
    _append_open_path(actions, "Open Claude Log Dir", ctx.claude_log_dir, group="session_evidence")
    if evidence.show_orchestrator_log:
        _append_run_scoped_action(
            actions,
            ctx,
            action_type="open_orchestrator_log",
            label="Open Orchestrator Log",
            group="session_evidence",
        )
    if evidence.show_full_orchestrator_log:
        _append_open_path(
            actions,
            "Open Full Log",
            ctx.orchestrator_log,
            group="session_evidence",
        )
    _append_open_path(actions, "Open Diagnostic", ctx.diagnostic_path, group="diagnostics")
    _append_open_path(actions, "Open Run Audit", ctx.run_audit_path, group="diagnostics")
    _append_open_path(actions, "Open Validation Record", ctx.validation_path, group="validation_artifacts")
    _append_open_path(actions, "Open Validation Output", ctx.validation_output_path, group="validation_artifacts")
    _append_open_path(actions, "Open Validation Stderr", ctx.validation_stderr_path, group="validation_artifacts")
    return actions


SessionActionGroup = Literal["validation_artifacts", "session_evidence", "diagnostics"]

_SESSION_DIAGNOSTIC_SECTION_TITLES: tuple[tuple[SessionActionGroup, str], ...] = (
    ("validation_artifacts", "Validation Artifacts"),
    ("session_evidence", "Session Evidence"),
    ("diagnostics", "Diagnostics"),
)
_SESSION_DIAGNOSTIC_ACTION_GROUPS: frozenset[str] = frozenset(get_args(SessionActionGroup))


def _append_open_path(
    actions: list[dict[str, Any]],
    label: str,
    path: str,
    *,
    group: SessionActionGroup,
) -> None:
    if not path:
        return
    payload: dict[str, Any] = {
        "type": "open_path",
        "label": label,
        "path": path,
        "group": _validated_session_action_group(group),
    }
    actions.append(payload)


def _append_run_scoped_action(
    actions: list[dict[str, Any]],
    ctx: SessionDiagnosticsContext,
    *,
    action_type: str,
    label: str,
    group: SessionActionGroup,
) -> None:
    if not ctx.run_dir:
        return
    payload: dict[str, Any] = {
        "type": action_type,
        "label": label,
        "issue_number": ctx.issue_number,
        "run_dir": ctx.run_dir,
        "group": _validated_session_action_group(group),
    }
    actions.append(payload)


def _validated_session_action_group(group: str) -> str:
    if group not in _SESSION_DIAGNOSTIC_ACTION_GROUPS:
        allowed = ", ".join(sorted(_SESSION_DIAGNOSTIC_ACTION_GROUPS))
        raise ValueError(f"Unknown session diagnostics action group {group!r}; expected one of: {allowed}")
    return group


def build_info_dialog(info: dict[str, Any]) -> dict[str, Any]:
    rows = [
        DialogRow("Version", info.get("version") or "dev"),
        DialogRow("Repository", info.get("repo") or ""),
        DialogRow("UI Mode", info.get("ui_mode") or ""),
        DialogRow("Terminal", info.get("terminal_backend") or ""),
        DialogRow("Commit", info.get("commit_short") or "unknown"),
        DialogRow("Max Sessions", str(info.get("max_sessions") or "-")),
        DialogRow("Active Sessions", str(info.get("active_sessions") or 0)),
        DialogRow("Completed Today", str(info.get("completed_today") or 0)),
    ]
    return {
        "title": "About Issue Orchestrator",
        "rows": [row.to_dict() for row in rows],
    }


def build_config_dialog(config_text: str) -> dict[str, Any]:
    return {
        "title": "Configuration",
        "config_text": config_text,
    }


def build_debug_dialog(debug_data: dict[str, Any]) -> dict[str, Any]:
    startup = debug_data.get("startup_options", {})
    filtering = startup.get("filtering", {})
    sections = [
        DialogSection(
            "Startup Options",
            [
                DialogRow("UI Mode", str(startup.get("ui_mode") or "-")),
                DialogRow("Web Port", str(startup.get("web_port") or "-")),
                DialogRow("Test Mode", "yes" if startup.get("test_mode") else "no"),
                DialogRow("Filter Label", str(filtering.get("label") or "none")),
                DialogRow("Filter Milestone", str(filtering.get("milestone") or "none")),
                DialogRow("Max Sessions", str(startup.get("max_sessions") or "-")),
            ],
        ),
        DialogSection(
            "State",
            [
                DialogRow("Paused", str(debug_data.get("paused"))),
                DialogRow(
                    "Priority Queue",
                    ", ".join(map(str, debug_data.get("priority_queue") or [])) or "empty",
                ),
            ],
        ),
        DialogSection(
            "Paths",
            [
                DialogRow("Config Path", str(debug_data.get("config_path") or "")),
                DialogRow("Repo Root", str(debug_data.get("repo_root") or "")),
            ],
        ),
    ]

    agents = debug_data.get("agents", {})
    if agents:
        sections.append(
            DialogSection(
                "Agent Types",
                [
                    DialogRow(name, f"timeout: {cfg.get('timeout')}m")
                    for name, cfg in agents.items()
                ],
            )
        )

    return {
        "title": "Debug Info",
        "sections": [section.to_dict() for section in sections],
    }


def build_doctor_dialog(doctor_data: dict[str, Any]) -> dict[str, Any]:
    checks = doctor_data.get("checks", [])
    return {
        "title": "Doctor",
        "overall": doctor_data.get("overall", "unknown"),
        "checks": [
            {
                "name": check.get("name"),
                "status": check.get("status"),
                "detail": check.get("detail"),
            }
            for check in checks
        ],
    }


def build_session_diagnostics_dialog(
    issue_number: int,
    manifest_payload: dict[str, Any],
) -> dict[str, Any]:
    ctx = SessionDiagnosticsContext.from_payload(issue_number, manifest_payload)
    analysis = SessionDiagnosticAnalysis.from_payload(manifest_payload.get("analysis"))
    follow_up_payload = (manifest_payload.get("manifest") or {}).get("follow_up_issues")
    follow_up_issues = [
        issue.to_dict()
        for item in follow_up_payload
        if (issue := SessionDiagnosticFollowUpIssue.from_payload(item)) is not None
    ] if isinstance(follow_up_payload, list) else []
    rows = _build_session_diagnostics_rows(ctx)
    actions = _build_session_diagnostics_actions(ctx)

    return {
        "title": f"Session Diagnostics #{issue_number}",
        "rows": [row.to_dict() for row in rows],
        "actions": actions,
        "analysis": analysis.to_dict() if analysis else None,
        "follow_up_issues": follow_up_issues,
    }


def build_validation_failure_dialog(
    issue_number: int,
    manifest_payload: dict[str, Any],
) -> dict[str, Any]:
    ctx = SessionDiagnosticsContext.from_payload(issue_number, manifest_payload)
    validation = manifest_payload.get("validation_failure") or {}
    raw_status = str(validation.get("status") or "")
    status = raw_status if raw_status in ("passed", "failed") else "failed"
    default_reason = "Validation passed" if status == "passed" else "Validation failed"
    failed_tests = [
        str(item)
        for item in validation.get("failed_tests", [])
        if isinstance(item, str) and item.strip()
    ]
    stdout_excerpt = [
        str(item)
        for item in validation.get("stdout_excerpt", [])
        if isinstance(item, str)
    ]
    stderr_excerpt = [
        str(item)
        for item in validation.get("stderr_excerpt", [])
        if isinstance(item, str)
    ]
    junit_cases = [
        _normalize_junit_case_for_dialog(item)
        for item in validation.get("junit_cases", [])
        if isinstance(item, dict) and item.get("case_id")
    ]
    actions = _build_session_diagnostics_actions(ctx)
    _append_run_scoped_action(
        actions,
        ctx,
        action_type="open_session_diagnostics",
        label="Full Diagnostics",
        group="diagnostics",
    )
    summary_rows = _build_validation_failure_summary_rows(
        validation, failed_tests, status,
    )
    action_sections = _build_validation_failure_action_sections(actions)

    title_outcome = "Passed" if status == "passed" else "Failure"
    return {
        "title": f"Validation {title_outcome} #{issue_number}",
        "status": status,
        "reason": str(
            validation.get("reason")
            or _outcome_reason(ctx.validation_outcome)
            or default_reason
        ),
        "suite": str(validation.get("suite") or ""),
        "command": str(validation.get("command") or ""),
        "exit_code": _optional_int(validation.get("exit_code")),
        "started_at": str(validation.get("started_at") or ""),
        "ended_at": str(validation.get("ended_at") or ""),
        "failed_tests": failed_tests,
        "stdout_excerpt": stdout_excerpt,
        "stderr_excerpt": stderr_excerpt,
        "junit_cases": junit_cases,
        "summary_rows": [row.to_dict() for row in summary_rows],
        "action_sections": action_sections,
    }


def _normalize_junit_case_for_dialog(case: dict[str, Any]) -> dict[str, Any]:
    """Carry ``extras`` through to the dialog payload.

    ``case.extras`` is the Phase-0 plugin slot (issue #6310 follow-up).
    Each extra is ``{namespace, payload}``.  The dialog renderer iterates
    these and delegates to plugin renderers registered for the
    namespace; unknown namespaces are silently skipped.  Cases produced
    by generic JUnit parsers carry an empty list — the slot exists for
    type stability, not because there's anything to render.
    """
    extras_raw = case.get("extras")
    extras: list[dict[str, Any]] = []
    if isinstance(extras_raw, list):
        for entry in extras_raw:
            if not isinstance(entry, dict):
                continue
            namespace = entry.get("namespace")
            payload = entry.get("payload")
            if not isinstance(namespace, str) or not namespace:
                continue
            if not isinstance(payload, dict):
                continue
            extras.append({"namespace": namespace, "payload": payload})
    return {**case, "extras": extras}


def _build_validation_failure_summary_rows(
    validation: dict[str, Any],
    failed_tests: list[str],
    status: str,
) -> list[DialogRow]:
    exit_code = _optional_int(validation.get("exit_code"))
    exit_code_display = str(exit_code) if exit_code is not None else "-"
    default_reason = "Validation passed" if status == "passed" else "Validation failed"
    return [
        DialogRow("Outcome", "Passed" if status == "passed" else "Failed"),
        DialogRow("Reason", str(validation.get("reason") or default_reason)),
        DialogRow("Suite", str(validation.get("suite") or "-")),
        DialogRow("Command", str(validation.get("command") or "-")),
        DialogRow("Exit Code", exit_code_display),
        DialogRow("Started", str(validation.get("started_at") or "-"), value_kind="timestamp"),
        DialogRow("Ended", str(validation.get("ended_at") or "-"), value_kind="timestamp"),
        DialogRow(
            "Failing Tests",
            str(len(failed_tests)) if failed_tests else "0",
        ),
    ]


def _build_validation_failure_action_sections(
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped_actions: dict[str, list[dict[str, Any]]] = {
        group: [] for group, _title in _SESSION_DIAGNOSTIC_SECTION_TITLES
    }

    for action in actions:
        group = action.get("group")
        if not isinstance(group, str):
            raise ValueError(f"Validation failure action {action.get('label')!r} is missing a group")
        if group not in grouped_actions:
            allowed = ", ".join(sorted(grouped_actions))
            raise ValueError(f"Unknown validation failure action group {group!r}; expected one of: {allowed}")
        grouped_actions[group].append(action)

    sections: list[dict[str, Any]] = []
    for group, title in _SESSION_DIAGNOSTIC_SECTION_TITLES:
        if grouped_actions[group]:
            sections.append({"title": title, "actions": grouped_actions[group]})
    return sections


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def build_blocked_issues_dialog(blocked_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "Blocked Issues",
        "blocked_issues": blocked_payload.get("blocked_issues", []),
    }


def _find_last_phase_with_prefix(phases: list[dict[str, Any]], prefix: str) -> dict[str, Any] | None:
    for phase in reversed(phases):
        if phase.get("name", "").startswith(prefix):
            return phase
    return None


def _select_phase(phases: list[dict[str, Any]], phase_key: str | None) -> dict[str, Any] | None:
    if phase_key in ("in_progress", "rework"):
        return _find_last_phase_with_prefix(phases, "coding-")
    if phase_key in ("review", "tech_lead"):
        return _find_last_phase_with_prefix(phases, "review-")
    if phase_key:
        for phase in phases:
            if phase.get("name") == phase_key:
                return phase
    return None


def build_phase_dialog(phases_payload: dict[str, Any], issue_number: int, phase_key: str | None) -> dict[str, Any]:
    phases = phases_payload.get("phases", [])
    current = _select_phase(phases, phase_key)

    if current is None and phases:
        current = phases[-1]

    return {
        "title": current.get("display_name") if current else "Phase Details",
        "issue_number": issue_number,
        "phase": current,
        "phases": phases,
    }
