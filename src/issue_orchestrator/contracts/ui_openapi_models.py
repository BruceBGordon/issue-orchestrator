# This file is generated from docs/api/ui-openapi.json.
# Do not edit by hand. Run: scripts/generate_ui_contracts.py



from __future__ import annotations


import re


from typing import Any, Literal, TypeAlias


from pydantic import BaseModel, ConfigDict, Field, field_validator




TimelineView: TypeAlias = Literal['user', 'ops', 'debug', 'raw']

WorktreeAuditActivityEvidence: TypeAlias = Literal['known', 'unknown']

WorktreeAuditDisposition: TypeAlias = Literal['managed', 'cleanup_candidate', 'retained']

WorktreeAuditKind: TypeAlias = Literal['issue', 'reviewer', 'tech_lead_scratch', 'external']

WorktreeAuditScope: TypeAlias = Literal['configured', 'repo-parent-fallback']

class AgentIdentityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    role: Literal['coder', 'reviewer', 'rework', 'validator', 'e2e_runner', 'orchestrator']

class BlockedCodingAttemptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: AgentIdentityPayload
    blocked_at: str
    commands: list[TimelineCommandPayload]
    diagnostics: list[TimelineDiagnosticPayload]
    issue_number: int
    kind: Literal['blocked_coding_attempt']
    reason: str
    session_recording: SessionRecordingEvidencePayload
    started_at: str | None = None

class BlockedIssuePayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    pass

class BlockedIssuesDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocked_issues: list[BlockedIssuePayload]
    title: str

class CapturedOutputAvailabilityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stderr_available: bool
    stdout_available: bool

class CodingOutputsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pull_request_url: str | None = None
    worktree_path: str | None = None

class CompletedCodingAttemptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: AgentIdentityPayload
    commands: list[TimelineCommandPayload]
    completed_at: str
    completion_record: CompletionRecordEvidencePayload
    issue_number: int
    kind: Literal['completed_coding_attempt']
    outputs: CodingOutputsPayload
    session_recording: SessionRecordingEvidencePayload
    started_at: str
    validation: ValidationOutcomePayload

class CompletionRecordEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['available']
    path: str
    summary: str | None = None

class ConfigDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_text: str
    title: str

class CreateE2EUntriagedIssuesCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['create_e2e_untriaged_issues']
    label: str
    run_id: int = Field(..., ge=1, strict=True)

class CycleArtifactsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    has_review_feedback: bool
    log_url: str | None
    pr_number: int | None
    pr_url: str | None
    review_decision: CycleReviewArtifactPayload | None
    review_report: CycleReviewArtifactPayload | None
    run_dir: str | None

class CycleReviewArtifactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_path: str
    artifact_type: Literal['review_report', 'review_decision']
    label: str
    render_mode: Literal['markdown', 'json']
    run_dir: str

class CycleValidationBadgePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: OpenValidationDetailsCommandPayload | None
    state: Literal['pending', 'not_validated', 'passed', 'failed']

class DashboardDataPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    agents: list[str]
    configMode: str
    configName: str
    e2eLastRun: dict[str, Any] | None = None
    e2eRunning: bool
    githubOwner: str
    githubRepo: str
    paused: bool
    providerCircuit: ProviderCircuitStatusPayload
    queueRefreshSeconds: int
    repo: str
    repoRoot: str
    startupComplete: bool
    validationConfigured: bool

class DashboardIterationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnostics: list[TimelineDiagnosticPayload]
    issue_lifecycles: list[IssueLifecyclePayload]
    kind: Literal['dashboard_current']
    subject: TimelineSubjectPayload

class DashboardTimelineContainerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current: DashboardIterationPayload
    kind: Literal['dashboard']
    subject: TimelineSubjectPayload

class DashboardViewModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_count: int
    active_items: list[IssueItemPayload]
    active_session_count: int
    active_tab: str
    agents: list[str]
    awaiting_merge_count: int
    awaiting_merge_items: list[IssueItemPayload]
    blocked_count: int
    blocked_items: list[IssueItemPayload]
    completed_count: int
    completed_items: list[IssueItemPayload]
    dashboard_data: DashboardDataPayload
    e2e_count: int
    e2e_items: list[IssueItemPayload]
    e2e_page: int
    e2e_status: dict[str, Any]
    e2e_total: int
    e2e_total_pages: int
    flow_columns: list[FlowColumnPayload]
    github_owner: str
    github_repo: str
    history_items: list[IssueItemPayload]
    issues: list[IssueItemPayload]
    paused: bool
    queue_count: int
    queue_items: list[IssueItemPayload]
    queue_page: int
    queue_refresh_seconds: int
    queue_total: int
    queue_total_pages: int
    recent_e2e_runs: RecentE2ERunsPayload
    repo: str
    repo_root: str
    scope_summary: dict[str, Any]
    shutdown_requested: bool
    startup_message: str
    startup_status: str

class DebugDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[DialogSectionPayload]
    title: str

class DialogRowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    value: str
    value_kind: Literal['timestamp'] | None = None

class DialogSectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[DialogRowPayload]
    title: str

class DoctorCheckPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: str | None = None
    name: str | None = None
    status: str | None = None

class DoctorDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checks: list[DoctorCheckPayload]
    overall: str
    title: str

class E2EArtifactDiagnosticPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collected_count: int
    configured_glob_count: int
    state: Literal['collected', 'globs_matched_nothing', 'not_configured']

class E2EFailureDetailsAvailablePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['available']
    longrepr: str

class E2EFailureDetailsMissingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnostics: list[TimelineDiagnosticPayload]
    kind: Literal['missing_evidence']

class E2EIssueAffordancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branch_name: str | None = None
    issue_number: int
    label: str | None = None
    run_id: int

class E2ERunDetailPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[IssueDetailActionPayload]
    artifact_diagnostic: E2EArtifactDiagnosticPayload
    artifacts: list[TestRunArtifactPayload]
    blocked_detail: IssueDetailBlockedDetailPayload | None
    cycles: list[E2ETimelineCyclePayload]
    e2e_run_id: int | None = None
    events: list[E2ETimelineEventPayload]
    issue_affordances: list[E2EIssueAffordancePayload]
    issue_number: int | str
    issue_url: str
    lifecycle: LifecycleTimelineContainerPayload
    phase_toc: list[E2ETimelinePhaseTocItemPayload]
    previous_runs: list[dict[str, Any]]
    previous_runs_count: int
    raw_events_count: int
    reports: list[TestRunArtifactPayload]
    results_by_category: E2ERunResultCategoriesPayload
    results_summary: E2ERunResultsSummaryPayload
    run: E2ERunExecutionPayload
    run_count: int
    runs: list[JourneyRunPayload]
    status_explanation: str
    summary: IssueDetailSummaryPayload
    timeline_steps: list[dict[str, Any]]
    title: str
    view: TimelineView | None = None

class E2ERunExecutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifacts_dir: str | None
    branch: str | None
    command: list[str]
    commit_sha: str | None
    current_test: str | None
    duration_seconds: float | None
    exit_code: int | None
    finished_at: str | None
    id: int
    log_excerpt: list[str]
    log_path: str | None
    orchestrator_id: str
    pytest_args: list[str]
    runner_kind: str
    started_at: str
    status: str
    total_tests: int | None

class E2ERunIterationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnostics: list[TimelineDiagnosticPayload]
    e2e_run: E2ERunLifecyclePayload
    kind: Literal['e2e_run']
    subject: TimelineSubjectPayload

class E2ERunLifecyclePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed_at: str | None = None
    diagnostics: list[TimelineDiagnosticPayload]
    linked_issue_lifecycles: list[IssueLifecyclePayload]
    run_id: int
    started_at: str
    tests: list[E2ETestExecutionPayload]

class E2ERunResultCategoriesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixed: list[TestCaseResultPayload]
    flaky: list[TestCaseResultPayload]
    has_issue: list[TestCaseResultPayload]
    passed: list[TestCaseResultPayload]
    quarantined: list[TestCaseResultPayload]
    skipped: list[TestCaseResultPayload]
    untriaged: list[TestCaseResultPayload]

class E2ERunResultCountsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    errored: int = Field(..., ge=0, strict=True)
    failed: int = Field(..., ge=0, strict=True)
    passed: int = Field(..., ge=0, strict=True)
    quarantined: int = Field(..., ge=0, strict=True)
    skipped: int = Field(..., ge=0, strict=True)
    total: int = Field(..., ge=0, strict=True)

class E2ERunResultsSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixed: int
    flaky: int
    has_issue: int
    passed: int
    quarantined: int
    skipped: int
    total: int
    untriaged: int

class E2ERunTimelinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cycles: list[E2ETimelineCyclePayload]
    events: list[E2ETimelineEventPayload]
    issue_affordances: list[E2EIssueAffordancePayload]
    lifecycle: LifecycleTimelineContainerPayload
    phase_toc: list[E2ETimelinePhaseTocItemPayload]

class E2ESuiteTimelineContainerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['e2e_suite']
    runs: list[E2ERunIterationPayload]
    subject: TimelineSubjectPayload

class E2ETestOutputPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodeid: str
    source_path: str
    system_err: str | None
    system_out: str | None

class E2ETimelineArtifactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    render_mode: str | None = None
    type: str
    value: str

class E2ETimelineCyclePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cycle: int
    end: str | None
    events: list[E2ETimelineEventPayload]
    phases: list[str]
    start: str | None
    status: str
    summary: str

class E2ETimelineEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    added: list[str] | None = None
    agent: str | None = None
    artifacts: list[E2ETimelineArtifactPayload]
    attempt_index: int | None = None
    coder_response_text: str | None = None
    coder_response_type: str | None = None
    detail: str | None
    duration_seconds: float | None = None
    event: str
    event_id: str
    event_intent: str
    issue_affordances: list[E2EIssueAffordancePayload] | None = None
    issue_number: int
    level: str
    logical_cycle: int | None = None
    logical_phase: str | None = None
    logical_run: int | None = None
    longrepr: str | None = None
    narrative: str | None = None
    nodeid: str | None = None
    outcome: str | None = None
    parent_key: str
    phase: str
    removed: list[str] | None = None
    response_type: str | None = None
    review_oriented: bool
    reviewer_agent: str | None = None
    reviewer_response_text: str | None = None
    reviewer_response_type: str | None = None
    rework_cycle: int | None = None
    role: str | None = None
    round_index: int | None = None
    rounds: int | None = None
    run_dir: str | None
    run_id: str | None
    source_event: str | None = None
    status: str
    step: str
    summary: str | None
    task: str | None = None
    timeline_schema_version: int | None = None
    timestamp: str
    unsupported_schema: bool
    views: list[str] | None = None

class E2ETimelinePhaseTocItemPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    phase: str

class ExpandE2ERunCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['expand_e2e_run']
    label: str
    run_id: int = Field(..., ge=1, strict=True)

class FailedCodingAttemptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: AgentIdentityPayload | None = None
    commands: list[TimelineCommandPayload]
    diagnostics: list[TimelineDiagnosticPayload]
    failed_at: str
    issue_number: int
    kind: Literal['failed_coding_attempt']
    reason: str
    session_recording: SessionRecordingEvidencePayload
    started_at: str | None = None

class FailedE2ETestExecutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[TimelineCommandPayload]
    completed_at: str
    duration_seconds: float | None = None
    failure: E2EFailureEvidencePayload
    kind: Literal['failed_e2e_test']
    linked_issues: list[LinkedIssueLifecyclePayload]
    nodeid: str
    started_at: str

class FlowColumnPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    count: int
    expandable: bool | None = None
    hidden_count: int = Field(..., ge=0, strict=True)
    id: str
    items: list[IssueItemPayload]
    session_scoped: bool | None = None
    title: str

class InfoDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[DialogRowPayload]
    title: str

class IssueCyclePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str | None
    artifacts: CycleArtifactsPayload | None
    coder: CodingAttemptPayload
    cycle_in_run: int | None
    cycle_label: str | None
    cycle_number: int
    diagnostics: list[TimelineDiagnosticPayload]
    expanded: bool | None
    iteration: int | None
    lifecycle: int | None
    outcome: OutcomeBadgePayload
    phase_groups: list[JourneyPhaseGroupPayload]
    reset_from_scratch: bool | None
    retry_count: int | None
    review: ReviewStagePayload
    reviewer_agent: str | None
    run_id: str | None
    session_run_ids: list[str]
    steps: list[JourneyStepPayload]
    time_label: str | None
    timestamp: str | None
    validation: CycleValidationBadgePayload | None

class IssueDetailActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    run_dir: str | None = None
    url: str | None = None

class IssueDetailBlockedDetailPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_summary: str
    labels: list[str]
    reason: str
    rework_info: str | None

class IssueDetailPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[IssueDetailActionPayload]
    blocked_detail: IssueDetailBlockedDetailPayload | None
    cycles: list[dict[str, Any]]
    e2e_run_id: int | None = None
    events: list[dict[str, Any]]
    issue_number: int
    issue_url: str
    lifecycle: LifecycleTimelineContainerPayload | None = None
    phase_toc: list[dict[str, Any]]
    previous_runs: list[dict[str, Any]]
    previous_runs_count: int
    raw_events_count: int
    run_count: int
    runs: list[JourneyRunPayload]
    stack_dependency: StackDependencyGateViewPayload | None = None
    status_explanation: str
    summary: IssueDetailSummaryPayload
    timeline_steps: list[dict[str, Any]]
    title: str
    view: TimelineView | None = None

class IssueDetailSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_count: int
    last_event: str
    run_diagnostic: IssueDetailValidationDiagnosticPayload | None = None
    status: str
    timeline_diagnostic: IssueDetailTimelineDiagnosticPayload | None = None

class IssueDetailTimelineDiagnosticPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dropped_missing_semantics: int
    expected_timeline_store: str
    expected_timeline_store_exists: bool
    resolved_run_dir: str | None
    signals: list[str]
    state: str

class IssueDetailValidationDiagnosticPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str
    exit_code: int
    failed_tests: list[str]
    failed_tests_preview: list[str]
    junit_cases: list[TestCaseResultPayload]
    reason: str
    run_dir: str
    session_name: str | None
    state: str
    suite: str
    validation_record_path: str | None
    validation_stderr: str | None
    validation_stdout: str | None

class IssueItemPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    action: str | None = None
    action_hint: str | None = None
    issue_number: int | str | None = None
    issue_url: str | None = None
    open_run_command: OpenE2ERunCommandPayload | None = None
    provider_badge: ProviderBadgeViewPayload | None = None
    provider_signal: str | None = None
    runtime_label: str | None = None
    show_stale_badge: bool
    stack_chip: StackChipViewPayload | None = None
    stack_dependency: StackDependencyGateViewPayload | None = None
    stack_signal: str | None = None
    status: str | None = None
    title: str | None = None
    url: str | None = None

class IssueLifecyclePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cycles: list[IssueCyclePayload]
    diagnostics: list[TimelineDiagnosticPayload]
    issue_number: int
    title: str

class IssueNumbersRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issues: list[int]

class IssueRowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    html: str
    issue_number: int | str | None = None

class IssueRowsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_tab: str
    count: int
    rows: list[IssueRowPayload]

class JUnitCasePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    display_name: str
    duration_seconds: float | None = None
    extras: list[ValidationExtraPayload]
    failure_details: str | None = None
    outcome: Literal['passed', 'failed', 'error', 'skipped']
    suite_name: str | None = None
    system_err: str | None = None
    system_out: str | None = None

class JourneyPhaseGroupPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Literal['coding', 'review', 'rework', 'orchestrator']
    label: str
    steps: list[JourneyStepPayload]

class JourneyRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cycles: list[IssueCyclePayload]
    expanded: bool
    outcome: OutcomeBadgePayload
    reset_from_scratch: bool
    run_id: str | None
    run_key: str
    run_label: str
    run_number: int
    session_run_ids: list[str]
    time_label: str
    timestamp: str

class JourneyStepPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[dict[str, Any]]
    day: str
    detail: str | None = None
    event: str
    in_round_progress: bool | None = None
    narrative: str
    status: str
    time_label: str
    timestamp: str

class LinkedIssueLifecyclePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: OpenIssueTimelineCommandPayload
    issue_number: int
    relationship: Literal['exercises', 'discovered', 'failed_with', 'validates']

class MissingCodingEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[TimelineCommandPayload]
    diagnostics: list[TimelineDiagnosticPayload]
    expected_state: Literal['completed', 'running', 'blocked', 'failed']
    issue_number: int
    kind: Literal['missing_coding_evidence']
    missing: list[MissingEvidencePayload]
    observed_at: str

class MissingE2ETestEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[TimelineCommandPayload]
    diagnostics: list[TimelineDiagnosticPayload]
    kind: Literal['missing_e2e_test_evidence']
    missing: list[MissingEvidencePayload]
    nodeid: str
    observed_at: str

class MissingEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence: str
    expected_ref: str | None = None
    kind: Literal['missing_evidence']
    reason: str

class MissingReviewEvidencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[TimelineCommandPayload]
    diagnostics: list[TimelineDiagnosticPayload]
    expected_state: Literal['approved', 'changes_requested', 'running']
    kind: Literal['missing_review_evidence']
    missing: list[MissingEvidencePayload]
    observed_at: str

class OpenCompletionRecordCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['open_completion_record']
    label: str
    path: str

class OpenE2ERunCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expand_run_details: bool | None = None
    kind: Literal['open_e2e_run']
    label: str
    run_id: int = Field(..., ge=1, strict=True)

class OpenInlineAgentAttemptsCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_number: int = Field(..., ge=1, strict=True)
    kind: Literal['open_inline_agent_attempts']
    label: str

class OpenIssueTimelineCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    e2e_run_id: int | None = None
    issue_number: int
    kind: Literal['open_issue_timeline']
    label: str
    scope_kind: Literal['dashboard', 'e2e_run']

class OpenReviewArtifactCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_path: str
    artifact_type: Literal['review_report', 'review_decision']
    issue_number: int
    kind: Literal['open_review_artifact']
    label: str
    render_mode: Literal['markdown', 'json']
    run_dir: str

class OpenReviewFeedbackCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_ref: str | None = None
    issue_number: int
    kind: Literal['open_review_feedback']
    label: str

class OpenSessionRecordingCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_number: int
    kind: Literal['open_session_recording']
    label: str
    round_index: int | None = None
    run_dir: str
    session_role: str | None = None

class OpenValidationDetailsCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_number: int
    kind: Literal['open_validation_details']
    label: str
    run_dir: str

class OutcomeBadgePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    tone: Literal['passed', 'failed', 'error', 'in_progress', 'neutral']

class PassedE2ETestExecutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[TimelineCommandPayload]
    completed_at: str
    duration_seconds: float | None = None
    kind: Literal['passed_e2e_test']
    linked_issues: list[LinkedIssueLifecyclePayload]
    nodeid: str
    started_at: str

class PhaseDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_number: int
    phase: dict[str, Any] | None
    phases: list[dict[str, Any]]
    title: str

class ProviderBadgeViewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label_text: str
    title: str
    tone: str

class ProviderCircuitEntryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consecutive_outages: int
    cooldown_remaining_label: str | None
    is_open: bool
    last_error_summary: str | None
    next_retry_at: str | None
    provider: str
    status_label: str

class ProviderCircuitStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    any_open: bool
    entries: list[ProviderCircuitEntryPayload]
    next_retry_at: str | None
    open_count: int
    open_providers: list[str]
    status_unavailable: bool
    summary_text: str

class PublishFailedCodingAttemptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: AgentIdentityPayload
    commands: list[TimelineCommandPayload]
    completed_at: str
    completion_record: CompletionRecordEvidencePayload
    diagnostics: list[TimelineDiagnosticPayload]
    issue_number: int
    kind: Literal['publish_failed_coding_attempt']
    outputs: CodingOutputsPayload
    publish_failed_at: str
    reason: str
    session_recording: SessionRecordingEvidencePayload
    started_at: str
    validation: ValidationOutcomePayload

class RecentE2ERunSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branch: str | None = None
    command_summary: str
    commit_sha: str | None = None
    duration_seconds: float | None = None
    expand_command: ExpandE2ERunCommandPayload
    finished_at: str | None = None
    note: str | None = None
    outcome: OutcomeBadgePayload
    results: E2ERunResultCountsPayload
    run_id: int = Field(..., ge=1, strict=True)
    runner_kind: str
    started_at: str

class RecentE2ERunsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runs: list[RecentE2ERunSummaryPayload]

class RepositorySetupCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_name: str | None = Field(default=None, min_length=1)
    configure_internal_reviewer: bool
    configure_reviewer: bool
    configure_tech_lead: bool
    create_labels: bool | None = None
    create_prompts: bool | None = None
    effort: Literal['low', 'medium', 'high', 'xhigh', 'max']
    github_authorization: RepositorySetupGitHubAuthorizationPayload
    internal_review_instructions: str = Field(..., min_length=1)
    internal_review_max_rounds: int = Field(..., ge=1, le=50, strict=True)
    model: Literal['haiku', 'sonnet', 'opus']
    replace_existing: bool | None = None
    repo_name: str = Field(..., min_length=1)
    repo_root: str = Field(..., min_length=1)
    reviewer_effort: Literal['low', 'medium', 'high', 'xhigh', 'max']
    reviewer_model: Literal['haiku', 'sonnet', 'opus']
    tech_lead_effort: Literal['low', 'medium', 'high', 'xhigh', 'max']
    tech_lead_model: Literal['haiku', 'sonnet', 'opus']
    tech_lead_review_threshold: int = Field(..., ge=0, le=50, strict=True)
    validation_publish_command: str = Field(..., min_length=1)
    validation_quick_command: str = Field(..., min_length=1)
    worker_agent_label: str
    worktree_base: str | None = Field(default=None, min_length=1)

    @field_validator('config_name')
    @classmethod
    def _validate_config_name_pattern(cls, value: Any) -> Any:
        if value is not None and re.search('^[^/\\\\]+(?:\\.yaml)?$', value) is None:
            raise ValueError("config_name must match '^[^/\\\\\\\\]+(?:\\\\.yaml)?$'")
        return value

    @field_validator('worker_agent_label')
    @classmethod
    def _validate_worker_agent_label_pattern(cls, value: Any) -> Any:
        if value is not None and re.search('^agent:(?!(?:reviewer|tech-lead)$).+', value) is None:
            raise ValueError("worker_agent_label must match '^agent:(?!(?:reviewer|tech-lead)$).+'")
        return value

class RepositorySetupConflictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_path: str
    detail: str
    error: Literal['replace_confirmation_required']

class RepositorySetupDetectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_labels: list[str]
    config_path: str | None
    existing_config: dict[str, Any] | None
    github_authorization: RepositorySetupGitHubAuthorizationDetectionPayload
    github_labels: list[str]
    prompt_candidates: list[str]
    repo: str | None
    repo_root: str = Field(..., min_length=1)
    validation_defaults: RepositorySetupValidationDefaultsPayload
    worktree_base_default: str = Field(..., min_length=1)
    worktree_base_resolved: str = Field(..., min_length=1)

class RepositorySetupFailurePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applied_files: list[str]
    created_labels: list[str]
    detail: str
    error: Literal['repository_setup_failed']
    stage: Literal['authorization', 'planning', 'files', 'labels']

class RepositorySetupFilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal['create', 'overwrite']
    agent: str | None = None
    path: str
    size: int | None = Field(default=None, ge=0, strict=True)
    type: Literal['prompt'] | None = None

class RepositorySetupGitHubAuthorizationDetectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization: RepositorySetupGitHubAuthorizationPayload
    configuration_error: str | None = Field(default=None, min_length=1)
    configured_kind: Literal['detected', 'personal', 'github_app', 'invalid']
    inline_token_migration_required: bool

class RepositorySetupGitHubAuthorizationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_url: str = Field(..., min_length=1)
    app_client_id: str | None = Field(default=None, min_length=1)
    app_id: str | None = Field(default=None, min_length=1)
    app_installation_id: str | None = Field(default=None, min_length=1)
    app_private_key_env: str | None = Field(default=None, min_length=1)
    app_private_key_path: str | None = Field(default=None, min_length=1)
    http_timeout_seconds: float = Field(..., gt=0)
    keyring_service: str | None = Field(default=None, min_length=1)
    keyring_username: str | None = Field(default=None, min_length=1)
    kind: Literal['detected', 'personal', 'github_app']
    token_env: str | None = Field(default=None, min_length=1)

class RepositorySetupGitHubTokenPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_url: str = Field(..., min_length=1)
    http_timeout_seconds: float = Field(..., gt=0)
    repo_name: str = Field(..., min_length=1)
    repo_root: str = Field(..., min_length=1)
    token: str = Field(..., min_length=1)

class RepositorySetupGitHubVerificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auth_kind: Literal['personal', 'github_app']
    authorization: RepositorySetupGitHubAuthorizationPayload
    authorship_notice: str = Field(..., min_length=1)
    identity: str = Field(..., min_length=1)
    repository: str = Field(..., min_length=1)
    required_permissions: list[str]
    source: str = Field(..., min_length=1)
    verification_note: str = Field(..., min_length=1)
    verified: Literal[True]

class RepositorySetupGitHubVerifyRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization: RepositorySetupGitHubAuthorizationPayload
    repo_name: str = Field(..., min_length=1)
    repo_root: str = Field(..., min_length=1)

class RepositorySetupPrerequisiteCheckPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: str = Field(..., min_length=1)
    name: str | None = Field(default=None, min_length=1)
    ok: bool

class RepositorySetupPrerequisitesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_checks: list[RepositorySetupPrerequisiteCheckPayload]
    all_ok: bool
    checks: dict[str, RepositorySetupPrerequisiteCheckPayload]

class RepositorySetupPreviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[RepositorySetupFilePayload]
    github_authorization: RepositorySetupGitHubVerificationPayload
    worktree_base: str = Field(..., min_length=1)
    yaml: str

class RepositorySetupResultPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_path: str
    created_files: list[str]
    created_labels: list[str]
    status: Literal['saved']

class RepositorySetupValidationDefaultsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publish_command: str | None
    quick_command: str | None
    source: str = Field(..., min_length=1)

class RetrospectiveReviewDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    agent_label: str | None
    eligible: bool
    issue: int = Field(..., ge=1, strict=True)
    labels: list[str]
    prior_pr_number: int | None = Field(..., ge=1)
    prior_pr_url: str | None
    reason: str
    state: str | None
    title: str | None
    trigger_label: str | None

class RetrospectiveReviewExecutePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failed: list[RetrospectiveReviewFailurePayload]
    queued: list[RetrospectiveReviewQueuedPayload]
    refresh_triggered: bool
    skipped: list[RetrospectiveReviewDecisionPayload]
    trigger_label: str
    workflow: Literal['retrospective_review']

class RetrospectiveReviewFailurePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: str
    issue: int = Field(..., ge=1, strict=True)

class RetrospectiveReviewPreflightPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[RetrospectiveReviewDecisionPayload]
    eligible: list[int]
    skipped: list[int]
    trigger_label: str
    workflow: Literal['retrospective_review']

class RetrospectiveReviewQueuedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    agent_label: str | None
    eligible: bool
    issue: int = Field(..., ge=1, strict=True)
    labels: list[str]
    prior_pr_number: int | None = Field(..., ge=1)
    prior_pr_url: str | None
    queued: bool
    reason: str
    state: str | None
    title: str | None
    trigger_label: str | None

class ReviewApprovedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[TimelineCommandPayload]
    completed_at: str
    kind: Literal['review_approved']
    reviewer: AgentIdentityPayload
    session_recording: SessionRecordingEvidencePayload
    started_at: str
    transcript: ReviewTranscriptEvidencePayload

class ReviewChangesRequestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[TimelineCommandPayload]
    completed_at: str
    feedback_summary: str
    kind: Literal['review_changes_requested']
    reviewer: AgentIdentityPayload
    session_recording: SessionRecordingEvidencePayload
    started_at: str

class ReviewFailedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[TimelineCommandPayload]
    diagnostics: list[TimelineDiagnosticPayload]
    failed_at: str
    kind: Literal['review_failed']
    reason: str
    reviewer: AgentIdentityPayload | None = None
    session_recording: SessionRecordingEvidencePayload
    started_at: str | None = None

class ReviewNotReachedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['review_not_reached']
    reason: Literal['coding_in_progress', 'coding_failed', 'publish_failed', 'validation_failed', 'not_required']

class ReviewRunningPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[TimelineCommandPayload]
    kind: Literal['review_running']
    reviewer: AgentIdentityPayload
    session_recording: SessionRecordingEvidencePayload
    started_at: str

class ReviewSkippedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['review_skipped']
    reason: str

class ReviewTranscriptAvailablePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['available']

class ReviewTranscriptUnavailablePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnostics: list[TimelineDiagnosticPayload]
    kind: Literal['unavailable']
    reason: str

class RunningCodingAttemptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: AgentIdentityPayload
    commands: list[TimelineCommandPayload]
    issue_number: int
    kind: Literal['running_coding_attempt']
    session_recording: SessionRecordingEvidencePayload
    started_at: str

class RunningE2ETestExecutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[TimelineCommandPayload]
    kind: Literal['running_e2e_test']
    linked_issues: list[LinkedIssueLifecyclePayload]
    nodeid: str
    started_at: str

class SessionDiagnosticsActionPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    group: Literal['validation_artifacts', 'session_evidence', 'diagnostics'] | None = None
    issue_number: int | None = None
    label: str
    path: str | None = None
    type: str

class SessionDiagnosticsAnalysisPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: str | None = None
    headline: str
    suggestions: list[str] | None = None

class SessionDiagnosticsDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[SessionDiagnosticsActionPayload]
    analysis: SessionDiagnosticsAnalysisPayload | None = None
    follow_up_issues: list[SessionDiagnosticsFollowUpIssuePayload] | None = None
    rows: list[DialogRowPayload]
    title: str

class SessionDiagnosticsFollowUpIssuePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocking: bool
    evidence: str | None = None
    reason: str
    suggested_labels: list[str] | None = None
    title: str

class SessionRecordingAvailablePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: OpenSessionRecordingCommandPayload
    kind: Literal['available']
    recording_path: str
    run_dir: str

class SessionRecordingUnavailablePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnostics: list[TimelineDiagnosticPayload]
    kind: Literal['unavailable']
    reason: str

class ShowEventDetailsCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_ref: str
    kind: Literal['show_event_details']
    label: str

class StackChipViewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode_label: str
    status_text: str
    title: str
    tone: str

class StackDependencyGatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate: str
    open: bool
    reason_codes: list[str]
    reasons: list[str]

class StackDependencyGateViewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_freshness: str
    blocked_gates: list[str]
    blocked_reason_codes: list[str]
    gates: list[StackDependencyGatePayload]
    has_stack_edges: bool
    issue_number: int
    mode: str
    predecessors: list[StackDependencyPredecessorPayload]
    stack_base_branch: str | None
    stale: bool
    stale_reason_codes: list[str]
    successors: list[StackDependencySuccessorPayload]

class StackDependencyPredecessorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str
    problem: str | None
    ref: str
    state: str

class StackDependencySuccessorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_number: int
    mode: str
    ref: str

class SwitchE2ETimelineViewCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['switch_e2e_timeline_view']
    label: str
    run_id: int = Field(..., ge=1, strict=True)
    view: TimelineView

class TechLeadGlobalHealthReviewScopePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['global_health_review']

class TechLeadIssueScopePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_number: int = Field(..., ge=1, strict=True)
    kind: Literal['issue']

class TechLeadRunAdmissionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    admitted: bool
    behind_global_barrier: bool
    detail: str
    issue_number: int | None = Field(..., ge=1)
    outcome: Literal['queued', 'already_queued', 'already_running', 'paused', 'not_running', 'not_configured', 'not_eligible', 'claim_conflict', 'failed']
    reason: str
    run_key: str
    scope_kind: Literal['global_health_review', 'global_batch_review', 'issue']

class TechLeadRunRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: TechLeadRunScopePayload

class TestCaseHistoryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: str
    run_id: int

class TestCaseIssueLinkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int
    resolution: str | None
    status: str

class TestCaseResultPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    captured_output: CapturedOutputAvailabilityPayload
    case_id: str
    category: str
    display_name: str | None
    duration_seconds: float | None
    existing_issue: TestCaseIssueLinkPayload | None
    failure_summary: str | None
    flip_rate: float
    flip_rate_percent: float
    history: list[TestCaseHistoryPayload]
    is_likely_flaky: bool
    is_quarantined: bool
    label: str
    longrepr: str | None
    nodeid: str
    outcome: str
    result_category: str
    result_source: str
    retry_outcome: str | None
    suite_name: str | None
    updated_at: str

class TestRunArtifactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    label: str
    path: str

class TimelineDiagnosticPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    evidence_ref: str | None = None
    message: str
    severity: Literal['info', 'warning', 'error']

class TimelineSubjectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal['dashboard', 'issue', 'e2e_suite', 'e2e_run']
    label: str
    outcome: str | None = None
    status: str | None = None

class ValidationEvidenceMissingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnostics: list[TimelineDiagnosticPayload]
    expected_record_path: str | None = None
    kind: Literal['missing_evidence']

class ValidationExtraPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str
    payload: dict[str, Any]

class ValidationFailedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str
    details_command: OpenValidationDetailsCommandPayload
    failure_summary: str
    kind: Literal['failed']
    record_path: str

class ValidationFailureActionSectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[SessionDiagnosticsActionPayload]
    title: str

class ValidationFailureDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_sections: list[ValidationFailureActionSectionPayload]
    command: str
    ended_at: str
    exit_code: int | None
    failed_tests: list[str]
    junit_cases: list[JUnitCasePayload]
    reason: str
    started_at: str
    status: Literal['passed', 'failed']
    stderr_excerpt: list[str]
    stdout_excerpt: list[str]
    suite: str
    summary_rows: list[DialogRowPayload]
    title: str

class ValidationNotRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal['not_run']
    reason: Literal['coding_in_progress', 'validation_disabled', 'not_required']

class ValidationPassedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str
    details_command: OpenValidationDetailsCommandPayload
    kind: Literal['passed']
    record_path: str

class ViewModelSnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_tab: str
    count: int
    rows: list[IssueRowPayload]
    view_model: DashboardViewModelPayload

class WorktreeAuditEntryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disposition: WorktreeAuditDisposition
    kind: WorktreeAuditKind
    name: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)

class WorktreeAuditRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo_root: str = Field(..., min_length=1)

class WorktreeAuditResponsePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    activity_evidence: WorktreeAuditActivityEvidence
    audit_unavailable: bool
    cleanup_candidates: list[WorktreeAuditEntryPayload]
    issue_cleanup_enabled: bool | None
    message: str = Field(..., min_length=1)
    note: str | None
    scope: WorktreeAuditScope
    stale_worktrees: list[WorktreeAuditEntryPayload]
    worktrees: list[WorktreeAuditEntryPayload]

CodingAttemptPayload: TypeAlias = RunningCodingAttemptPayload | CompletedCodingAttemptPayload | PublishFailedCodingAttemptPayload | BlockedCodingAttemptPayload | FailedCodingAttemptPayload | MissingCodingEvidencePayload

E2EFailureEvidencePayload: TypeAlias = E2EFailureDetailsAvailablePayload | E2EFailureDetailsMissingPayload

E2ETestExecutionPayload: TypeAlias = PassedE2ETestExecutionPayload | FailedE2ETestExecutionPayload | RunningE2ETestExecutionPayload | MissingE2ETestEvidencePayload

LifecycleTimelineContainerPayload: TypeAlias = DashboardTimelineContainerPayload | E2ESuiteTimelineContainerPayload

ReviewStagePayload: TypeAlias = ReviewNotReachedPayload | ReviewSkippedPayload | ReviewRunningPayload | ReviewApprovedPayload | ReviewChangesRequestedPayload | ReviewFailedPayload | MissingReviewEvidencePayload

ReviewTranscriptEvidencePayload: TypeAlias = ReviewTranscriptAvailablePayload | ReviewTranscriptUnavailablePayload

SessionRecordingEvidencePayload: TypeAlias = SessionRecordingAvailablePayload | SessionRecordingUnavailablePayload

TechLeadRunScopePayload: TypeAlias = TechLeadGlobalHealthReviewScopePayload | TechLeadIssueScopePayload

TimelineCommandPayload: TypeAlias = ShowEventDetailsCommandPayload | OpenCompletionRecordCommandPayload | OpenValidationDetailsCommandPayload | OpenSessionRecordingCommandPayload | OpenReviewFeedbackCommandPayload | OpenReviewArtifactCommandPayload | OpenIssueTimelineCommandPayload | OpenE2ERunCommandPayload | ExpandE2ERunCommandPayload | SwitchE2ETimelineViewCommandPayload | CreateE2EUntriagedIssuesCommandPayload | OpenInlineAgentAttemptsCommandPayload

ValidationOutcomePayload: TypeAlias = ValidationPassedPayload | ValidationFailedPayload | ValidationNotRunPayload | ValidationEvidenceMissingPayload
