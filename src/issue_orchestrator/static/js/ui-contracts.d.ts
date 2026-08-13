// This file is generated from docs/api/ui-openapi.json.
// Do not edit by hand. Run: scripts/generate_ui_contracts.py



export type TimelineView = "user" | "ops" | "debug" | "raw";

export type WorktreeAuditActivityEvidence = "known" | "unknown";

export type WorktreeAuditDisposition = "managed" | "cleanup_candidate" | "retained";

export type WorktreeAuditKind = "issue" | "reviewer" | "tech_lead_scratch" | "external";

export type WorktreeAuditScope = "configured" | "repo-parent-fallback";

export interface AgentIdentityPayload {
  name: string;
  role: "coder" | "reviewer" | "rework" | "validator" | "e2e_runner" | "orchestrator";
}

export interface BlockedCodingAttemptPayload {
  agent: AgentIdentityPayload;
  blocked_at: string;
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  issue_number: number;
  kind: "blocked_coding_attempt";
  reason: string;
  session_recording: SessionRecordingEvidencePayload;
  started_at?: string | null;
}

export interface BlockedIssuePayload {
  [key: string]: any;
}

export interface BlockedIssuesDialogPayload {
  blocked_issues: BlockedIssuePayload[];
  title: string;
}

export interface CapturedOutputAvailabilityPayload {
  stderr_available: boolean;
  stdout_available: boolean;
}

export interface CodingOutputsPayload {
  pull_request_url?: string | null;
  worktree_path?: string | null;
}

export interface CompletedCodingAttemptPayload {
  agent: AgentIdentityPayload;
  commands: TimelineCommandPayload[];
  completed_at: string;
  completion_record: CompletionRecordEvidencePayload;
  issue_number: number;
  kind: "completed_coding_attempt";
  outputs: CodingOutputsPayload;
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
  validation: ValidationOutcomePayload;
}

export interface CompletionRecordEvidencePayload {
  kind: "available";
  path: string;
  summary?: string | null;
}

export interface ConfigDialogPayload {
  config_text: string;
  title: string;
}

export interface CreateE2EUntriagedIssuesCommandPayload {
  kind: "create_e2e_untriaged_issues";
  label: string;
  run_id: number;
}

export interface CycleArtifactsPayload {
  has_review_feedback: boolean;
  log_url: string | null;
  pr_number: number | null;
  pr_url: string | null;
  review_decision: CycleReviewArtifactPayload | null;
  review_report: CycleReviewArtifactPayload | null;
  run_dir: string | null;
}

export interface CycleReviewArtifactPayload {
  artifact_path: string;
  artifact_type: "review_report" | "review_decision";
  label: string;
  render_mode: "markdown" | "json";
  run_dir: string;
}

export interface CycleValidationBadgePayload {
  command: OpenValidationDetailsCommandPayload | null;
  state: "pending" | "not_validated" | "passed" | "failed";
}

export interface DashboardDataPayload {
  agents: string[];
  configMode: string;
  configName: string;
  e2eLastRun?: Record<string, any> | null;
  e2eRunning: boolean;
  githubOwner: string;
  githubRepo: string;
  paused: boolean;
  providerCircuit: ProviderCircuitStatusPayload;
  queueRefreshSeconds: number;
  repo: string;
  repoRoot: string;
  startupComplete: boolean;
  validationConfigured: boolean;
  [key: string]: any;
}

export interface DashboardIterationPayload {
  diagnostics: TimelineDiagnosticPayload[];
  issue_lifecycles: IssueLifecyclePayload[];
  kind: "dashboard_current";
  subject: TimelineSubjectPayload;
}

export interface DashboardTimelineContainerPayload {
  current: DashboardIterationPayload;
  kind: "dashboard";
  subject: TimelineSubjectPayload;
}

export interface DashboardViewModelPayload {
  active_count: number;
  active_items: IssueItemPayload[];
  active_session_count: number;
  active_tab: string;
  agents: string[];
  awaiting_merge_count: number;
  awaiting_merge_items: IssueItemPayload[];
  blocked_count: number;
  blocked_items: IssueItemPayload[];
  completed_count: number;
  completed_items: IssueItemPayload[];
  dashboard_data: DashboardDataPayload;
  e2e_count: number;
  e2e_items: IssueItemPayload[];
  e2e_page: number;
  e2e_status: Record<string, any>;
  e2e_total: number;
  e2e_total_pages: number;
  flow_columns: FlowColumnPayload[];
  github_owner: string;
  github_repo: string;
  history_items: IssueItemPayload[];
  issues: IssueItemPayload[];
  paused: boolean;
  queue_count: number;
  queue_items: IssueItemPayload[];
  queue_page: number;
  queue_refresh_seconds: number;
  queue_total: number;
  queue_total_pages: number;
  recent_e2e_runs: RecentE2ERunsPayload;
  repo: string;
  repo_root: string;
  scope_summary: Record<string, any>;
  shutdown_requested: boolean;
  startup_message: string;
  startup_status: string;
}

export interface DebugDialogPayload {
  sections: DialogSectionPayload[];
  title: string;
}

export interface DialogRowPayload {
  label: string;
  value: string;
  value_kind?: "timestamp";
}

export interface DialogSectionPayload {
  rows: DialogRowPayload[];
  title: string;
}

export interface DoctorCheckPayload {
  detail?: string | null;
  name?: string | null;
  status?: string | null;
}

export interface DoctorDialogPayload {
  checks: DoctorCheckPayload[];
  overall: string;
  title: string;
}

export interface E2EArtifactDiagnosticPayload {
  collected_count: number;
  configured_glob_count: number;
  state: "collected" | "globs_matched_nothing" | "not_configured";
}

export interface E2EFailureDetailsAvailablePayload {
  kind: "available";
  longrepr: string;
}

export interface E2EFailureDetailsMissingPayload {
  diagnostics: TimelineDiagnosticPayload[];
  kind: "missing_evidence";
}

export interface E2EIssueAffordancePayload {
  branch_name?: string;
  issue_number: number;
  label?: string;
  run_id: number;
}

export interface E2ERunDetailPayload {
  actions: IssueDetailActionPayload[];
  artifact_diagnostic: E2EArtifactDiagnosticPayload;
  artifacts: TestRunArtifactPayload[];
  blocked_detail: IssueDetailBlockedDetailPayload | null;
  cycles: E2ETimelineCyclePayload[];
  e2e_run_id?: number | null;
  events: E2ETimelineEventPayload[];
  issue_affordances: E2EIssueAffordancePayload[];
  issue_number: number | string;
  issue_url: string;
  lifecycle: LifecycleTimelineContainerPayload;
  phase_toc: E2ETimelinePhaseTocItemPayload[];
  previous_runs: Record<string, any>[];
  previous_runs_count: number;
  raw_events_count: number;
  reports: TestRunArtifactPayload[];
  results_by_category: E2ERunResultCategoriesPayload;
  results_summary: E2ERunResultsSummaryPayload;
  run: E2ERunExecutionPayload;
  run_count: number;
  runs: JourneyRunPayload[];
  status_explanation: string;
  summary: IssueDetailSummaryPayload;
  timeline_steps: Record<string, any>[];
  title: string;
  view?: TimelineView;
}

export interface E2ERunExecutionPayload {
  artifacts_dir: string | null;
  branch: string | null;
  command: string[];
  commit_sha: string | null;
  current_test: string | null;
  duration_seconds: number | null;
  exit_code: number | null;
  finished_at: string | null;
  id: number;
  log_excerpt: string[];
  log_path: string | null;
  orchestrator_id: string;
  pytest_args: string[];
  runner_kind: string;
  started_at: string;
  status: string;
  total_tests: number | null;
}

export interface E2ERunIterationPayload {
  diagnostics: TimelineDiagnosticPayload[];
  e2e_run: E2ERunLifecyclePayload;
  kind: "e2e_run";
  subject: TimelineSubjectPayload;
}

export interface E2ERunLifecyclePayload {
  completed_at?: string | null;
  diagnostics: TimelineDiagnosticPayload[];
  linked_issue_lifecycles: IssueLifecyclePayload[];
  run_id: number;
  started_at: string;
  tests: E2ETestExecutionPayload[];
}

export interface E2ERunResultCategoriesPayload {
  fixed: TestCaseResultPayload[];
  flaky: TestCaseResultPayload[];
  has_issue: TestCaseResultPayload[];
  passed: TestCaseResultPayload[];
  quarantined: TestCaseResultPayload[];
  skipped: TestCaseResultPayload[];
  untriaged: TestCaseResultPayload[];
}

export interface E2ERunResultCountsPayload {
  errored: number;
  failed: number;
  passed: number;
  quarantined: number;
  skipped: number;
  total: number;
}

export interface E2ERunResultsSummaryPayload {
  fixed: number;
  flaky: number;
  has_issue: number;
  passed: number;
  quarantined: number;
  skipped: number;
  total: number;
  untriaged: number;
}

export interface E2ERunTimelinePayload {
  cycles: E2ETimelineCyclePayload[];
  events: E2ETimelineEventPayload[];
  issue_affordances: E2EIssueAffordancePayload[];
  lifecycle: LifecycleTimelineContainerPayload;
  phase_toc: E2ETimelinePhaseTocItemPayload[];
}

export interface E2ESuiteTimelineContainerPayload {
  kind: "e2e_suite";
  runs: E2ERunIterationPayload[];
  subject: TimelineSubjectPayload;
}

export interface E2ETestOutputPayload {
  nodeid: string;
  source_path: string;
  system_err: string | null;
  system_out: string | null;
}

export interface E2ETimelineArtifactPayload {
  label: string;
  render_mode?: string | null;
  type: string;
  value: string;
}

export interface E2ETimelineCyclePayload {
  cycle: number;
  end: string | null;
  events: E2ETimelineEventPayload[];
  phases: string[];
  start: string | null;
  status: string;
  summary: string;
}

export interface E2ETimelineEventPayload {
  added?: string[];
  agent?: string;
  artifacts: E2ETimelineArtifactPayload[];
  attempt_index?: number;
  coder_response_text?: string;
  coder_response_type?: string;
  detail: string | null;
  duration_seconds?: number;
  event: string;
  event_id: string;
  event_intent: string;
  issue_affordances?: E2EIssueAffordancePayload[];
  issue_number: number;
  level: string;
  logical_cycle?: number;
  logical_phase?: string;
  logical_run?: number;
  longrepr?: string;
  narrative?: string;
  nodeid?: string;
  outcome?: string;
  parent_key: string;
  phase: string;
  removed?: string[];
  response_type?: string;
  review_oriented: boolean;
  reviewer_agent?: string;
  reviewer_response_text?: string;
  reviewer_response_type?: string;
  rework_cycle?: number;
  role?: string;
  round_index?: number;
  rounds?: number;
  run_dir: string | null;
  run_id: string | null;
  source_event?: string;
  status: string;
  step: string;
  summary: string | null;
  task?: string;
  timeline_schema_version?: number;
  timestamp: string;
  unsupported_schema: boolean;
  views?: string[];
}

export interface E2ETimelinePhaseTocItemPayload {
  label: string;
  phase: string;
}

export interface ExpandE2ERunCommandPayload {
  kind: "expand_e2e_run";
  label: string;
  run_id: number;
}

export interface FailedCodingAttemptPayload {
  agent?: AgentIdentityPayload | null;
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  failed_at: string;
  issue_number: number;
  kind: "failed_coding_attempt";
  reason: string;
  session_recording: SessionRecordingEvidencePayload;
  started_at?: string | null;
}

export interface FailedE2ETestExecutionPayload {
  commands: TimelineCommandPayload[];
  completed_at: string;
  duration_seconds?: number | null;
  failure: E2EFailureEvidencePayload;
  kind: "failed_e2e_test";
  linked_issues: LinkedIssueLifecyclePayload[];
  nodeid: string;
  started_at: string;
}

export interface FlowColumnPayload {
  count: number;
  expandable?: boolean;
  hidden_count: number;
  id: string;
  items: IssueItemPayload[];
  session_scoped?: boolean;
  title: string;
  [key: string]: any;
}

export interface InfoDialogPayload {
  rows: DialogRowPayload[];
  title: string;
}

export interface IssueCyclePayload {
  agent: string | null;
  artifacts: CycleArtifactsPayload | null;
  coder: CodingAttemptPayload;
  cycle_in_run: number | null;
  cycle_label: string | null;
  cycle_number: number;
  diagnostics: TimelineDiagnosticPayload[];
  expanded: boolean | null;
  iteration: number | null;
  lifecycle: number | null;
  outcome: OutcomeBadgePayload;
  phase_groups: JourneyPhaseGroupPayload[];
  reset_from_scratch: boolean | null;
  retry_count: number | null;
  review: ReviewStagePayload;
  reviewer_agent: string | null;
  run_id: string | null;
  session_run_ids: string[];
  steps: JourneyStepPayload[];
  time_label: string | null;
  timestamp: string | null;
  validation: CycleValidationBadgePayload | null;
}

export interface IssueDetailActionPayload {
  id: string;
  label: string;
  run_dir?: string | null;
  url?: string | null;
}

export interface IssueDetailBlockedDetailPayload {
  event_summary: string;
  labels: string[];
  reason: string;
  rework_info: string | null;
}

export interface IssueDetailPayload {
  actions: IssueDetailActionPayload[];
  blocked_detail: IssueDetailBlockedDetailPayload | null;
  cycles: Record<string, any>[];
  e2e_run_id?: number | null;
  events: Record<string, any>[];
  issue_number: number;
  issue_url: string;
  lifecycle?: LifecycleTimelineContainerPayload | null;
  phase_toc: Record<string, any>[];
  previous_runs: Record<string, any>[];
  previous_runs_count: number;
  raw_events_count: number;
  run_count: number;
  runs: JourneyRunPayload[];
  stack_dependency?: StackDependencyGateViewPayload | null;
  status_explanation: string;
  summary: IssueDetailSummaryPayload;
  timeline_steps: Record<string, any>[];
  title: string;
  view?: TimelineView;
}

export interface IssueDetailSummaryPayload {
  event_count: number;
  last_event: string;
  run_diagnostic?: IssueDetailValidationDiagnosticPayload | null;
  status: string;
  timeline_diagnostic?: IssueDetailTimelineDiagnosticPayload | null;
}

export interface IssueDetailTimelineDiagnosticPayload {
  dropped_missing_semantics: number;
  expected_timeline_store: string;
  expected_timeline_store_exists: boolean;
  resolved_run_dir: string | null;
  signals: string[];
  state: string;
}

export interface IssueDetailValidationDiagnosticPayload {
  command: string;
  exit_code: number;
  failed_tests: string[];
  failed_tests_preview: string[];
  junit_cases: TestCaseResultPayload[];
  reason: string;
  run_dir: string;
  session_name: string | null;
  state: string;
  suite: string;
  validation_record_path: string | null;
  validation_stderr: string | null;
  validation_stdout: string | null;
}

export interface IssueItemPayload {
  action?: string | null;
  action_hint?: string | null;
  issue_number?: number | string | null;
  issue_url?: string | null;
  open_run_command?: OpenE2ERunCommandPayload | null;
  provider_badge?: ProviderBadgeViewPayload | null;
  provider_signal?: string | null;
  runtime_label?: string | null;
  show_stale_badge: boolean;
  stack_chip?: StackChipViewPayload | null;
  stack_dependency?: StackDependencyGateViewPayload | null;
  stack_signal?: string | null;
  status?: string | null;
  title?: string | null;
  url?: string | null;
  [key: string]: any;
}

export interface IssueLifecyclePayload {
  cycles: IssueCyclePayload[];
  diagnostics: TimelineDiagnosticPayload[];
  issue_number: number;
  title: string;
}

export interface IssueNumbersRequestPayload {
  issues: number[];
}

export interface IssueRowPayload {
  html: string;
  issue_number?: number | string | null;
}

export interface IssueRowsPayload {
  active_tab: string;
  count: number;
  rows: IssueRowPayload[];
}

export interface JUnitCasePayload {
  case_id: string;
  display_name: string;
  duration_seconds?: number | null;
  extras: ValidationExtraPayload[];
  failure_details?: string | null;
  outcome: "passed" | "failed" | "error" | "skipped";
  suite_name?: string | null;
  system_err?: string | null;
  system_out?: string | null;
}

export interface JourneyPhaseGroupPayload {
  key: "coding" | "review" | "rework" | "orchestrator";
  label: string;
  steps: JourneyStepPayload[];
}

export interface JourneyRunPayload {
  cycles: IssueCyclePayload[];
  expanded: boolean;
  outcome: OutcomeBadgePayload;
  reset_from_scratch: boolean;
  run_id: string | null;
  run_key: string;
  run_label: string;
  run_number: number;
  session_run_ids: string[];
  time_label: string;
  timestamp: string;
}

export interface JourneyStepPayload {
  actions: Record<string, any>[];
  day: string;
  detail?: string | null;
  event: string;
  in_round_progress?: boolean;
  narrative: string;
  status: string;
  time_label: string;
  timestamp: string;
}

export interface LinkedIssueLifecyclePayload {
  command: OpenIssueTimelineCommandPayload;
  issue_number: number;
  relationship: "exercises" | "discovered" | "failed_with" | "validates";
}

export interface MissingCodingEvidencePayload {
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  expected_state: "completed" | "running" | "blocked" | "failed";
  issue_number: number;
  kind: "missing_coding_evidence";
  missing: MissingEvidencePayload[];
  observed_at: string;
}

export interface MissingE2ETestEvidencePayload {
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  kind: "missing_e2e_test_evidence";
  missing: MissingEvidencePayload[];
  nodeid: string;
  observed_at: string;
}

export interface MissingEvidencePayload {
  evidence: string;
  expected_ref?: string | null;
  kind: "missing_evidence";
  reason: string;
}

export interface MissingReviewEvidencePayload {
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  expected_state: "approved" | "changes_requested" | "running";
  kind: "missing_review_evidence";
  missing: MissingEvidencePayload[];
  observed_at: string;
}

export interface OpenCompletionRecordCommandPayload {
  kind: "open_completion_record";
  label: string;
  path: string;
}

export interface OpenE2ERunCommandPayload {
  expand_run_details?: boolean;
  kind: "open_e2e_run";
  label: string;
  run_id: number;
}

export interface OpenInlineAgentAttemptsCommandPayload {
  issue_number: number;
  kind: "open_inline_agent_attempts";
  label: string;
}

export interface OpenIssueTimelineCommandPayload {
  e2e_run_id?: number | null;
  issue_number: number;
  kind: "open_issue_timeline";
  label: string;
  scope_kind: "dashboard" | "e2e_run";
}

export interface OpenReviewArtifactCommandPayload {
  artifact_path: string;
  artifact_type: "review_report" | "review_decision";
  issue_number: number;
  kind: "open_review_artifact";
  label: string;
  render_mode: "markdown" | "json";
  run_dir: string;
}

export interface OpenReviewFeedbackCommandPayload {
  event_ref?: string | null;
  issue_number: number;
  kind: "open_review_feedback";
  label: string;
}

export interface OpenSessionRecordingCommandPayload {
  issue_number: number;
  kind: "open_session_recording";
  label: string;
  round_index?: number | null;
  run_dir: string;
  session_role?: string | null;
}

export interface OpenValidationDetailsCommandPayload {
  issue_number: number;
  kind: "open_validation_details";
  label: string;
  run_dir: string;
}

export interface OutcomeBadgePayload {
  label: string;
  tone: "passed" | "failed" | "error" | "in_progress" | "neutral";
}

export interface PassedE2ETestExecutionPayload {
  commands: TimelineCommandPayload[];
  completed_at: string;
  duration_seconds?: number | null;
  kind: "passed_e2e_test";
  linked_issues: LinkedIssueLifecyclePayload[];
  nodeid: string;
  started_at: string;
}

export interface PhaseDialogPayload {
  issue_number: number;
  phase: Record<string, any> | null;
  phases: Record<string, any>[];
  title: string;
}

export interface ProviderBadgeViewPayload {
  label_text: string;
  title: string;
  tone: string;
}

export interface ProviderCircuitEntryPayload {
  consecutive_outages: number;
  cooldown_remaining_label: string | null;
  is_open: boolean;
  last_error_summary: string | null;
  next_retry_at: string | null;
  provider: string;
  status_label: string;
}

export interface ProviderCircuitStatusPayload {
  any_open: boolean;
  entries: ProviderCircuitEntryPayload[];
  next_retry_at: string | null;
  open_count: number;
  open_providers: string[];
  status_unavailable: boolean;
  summary_text: string;
}

export interface PublishFailedCodingAttemptPayload {
  agent: AgentIdentityPayload;
  commands: TimelineCommandPayload[];
  completed_at: string;
  completion_record: CompletionRecordEvidencePayload;
  diagnostics: TimelineDiagnosticPayload[];
  issue_number: number;
  kind: "publish_failed_coding_attempt";
  outputs: CodingOutputsPayload;
  publish_failed_at: string;
  reason: string;
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
  validation: ValidationOutcomePayload;
}

export interface RecentE2ERunSummaryPayload {
  branch?: string | null;
  command_summary: string;
  commit_sha?: string | null;
  duration_seconds?: number | null;
  expand_command: ExpandE2ERunCommandPayload;
  finished_at?: string | null;
  note?: string | null;
  outcome: OutcomeBadgePayload;
  results: E2ERunResultCountsPayload;
  run_id: number;
  runner_kind: string;
  started_at: string;
}

export interface RecentE2ERunsPayload {
  runs: RecentE2ERunSummaryPayload[];
}

export interface RepositorySetupCommandPayload {
  config_name?: string;
  configure_internal_reviewer: boolean;
  configure_reviewer: boolean;
  configure_tech_lead: boolean;
  create_labels?: boolean;
  create_prompts?: boolean;
  effort: "low" | "medium" | "high" | "xhigh" | "max";
  github_authorization: RepositorySetupGitHubAuthorizationPayload;
  internal_review_instructions: string;
  internal_review_max_rounds: number;
  model: "haiku" | "sonnet" | "opus";
  replace_existing?: boolean;
  repo_name: string;
  repo_root: string;
  reviewer_effort: "low" | "medium" | "high" | "xhigh" | "max";
  reviewer_model: "haiku" | "sonnet" | "opus";
  tech_lead_effort: "low" | "medium" | "high" | "xhigh" | "max";
  tech_lead_model: "haiku" | "sonnet" | "opus";
  tech_lead_review_threshold: number;
  validation_publish_command: string;
  validation_quick_command: string;
  worker_agent_label: string;
  worktree_base?: string;
}

export interface RepositorySetupConflictPayload {
  config_path: string;
  detail: string;
  error: "replace_confirmation_required";
}

export interface RepositorySetupDetectionPayload {
  agent_labels: string[];
  config_path: string | null;
  existing_config: Record<string, any> | null;
  github_authorization: RepositorySetupGitHubAuthorizationDetectionPayload;
  github_labels: string[];
  prompt_candidates: string[];
  repo: string | null;
  repo_root: string;
  validation_defaults: RepositorySetupValidationDefaultsPayload;
  worktree_base_default: string;
  worktree_base_resolved: string;
}

export interface RepositorySetupFailurePayload {
  applied_files: string[];
  created_labels: string[];
  detail: string;
  error: "repository_setup_failed";
  stage: "authorization" | "planning" | "files" | "labels";
}

export interface RepositorySetupFilePayload {
  action: "create" | "overwrite";
  agent?: string;
  path: string;
  size?: number;
  type?: "prompt";
}

export interface RepositorySetupGitHubAuthorizationDetectionPayload {
  authorization: RepositorySetupGitHubAuthorizationPayload;
  configuration_error?: string;
  configured_kind: "detected" | "personal" | "github_app" | "invalid";
  inline_token_migration_required: boolean;
}

export interface RepositorySetupGitHubAuthorizationPayload {
  api_url: string;
  app_client_id?: string;
  app_id?: string;
  app_installation_id?: string;
  app_private_key_env?: string;
  app_private_key_path?: string;
  http_timeout_seconds: number;
  keyring_service?: string;
  keyring_username?: string;
  kind: "detected" | "personal" | "github_app";
  token_env?: string;
}

export interface RepositorySetupGitHubTokenPayload {
  api_url: string;
  http_timeout_seconds: number;
  repo_name: string;
  repo_root: string;
  token: string;
}

export interface RepositorySetupGitHubVerificationPayload {
  auth_kind: "personal" | "github_app";
  authorization: RepositorySetupGitHubAuthorizationPayload;
  authorship_notice: string;
  identity: string;
  repository: string;
  required_permissions: string[];
  source: string;
  verification_note: string;
  verified: true;
}

export interface RepositorySetupGitHubVerifyRequestPayload {
  authorization: RepositorySetupGitHubAuthorizationPayload;
  repo_name: string;
  repo_root: string;
}

export interface RepositorySetupPrerequisiteCheckPayload {
  detail: string;
  name?: string;
  ok: boolean;
}

export interface RepositorySetupPrerequisitesPayload {
  agent_checks: RepositorySetupPrerequisiteCheckPayload[];
  all_ok: boolean;
  checks: Record<string, RepositorySetupPrerequisiteCheckPayload>;
}

export interface RepositorySetupPreviewPayload {
  files: RepositorySetupFilePayload[];
  github_authorization: RepositorySetupGitHubVerificationPayload;
  worktree_base: string;
  yaml: string;
}

export interface RepositorySetupResultPayload {
  config_path: string;
  created_files: string[];
  created_labels: string[];
  status: "saved";
}

export interface RepositorySetupValidationDefaultsPayload {
  publish_command: string | null;
  quick_command: string | null;
  source: string;
}

export interface RetrospectiveReviewDecisionPayload {
  action: string;
  agent_label: string | null;
  eligible: boolean;
  issue: number;
  labels: string[];
  prior_pr_number: number | null;
  prior_pr_url: string | null;
  reason: string;
  state: string | null;
  title: string | null;
  trigger_label: string | null;
}

export interface RetrospectiveReviewExecutePayload {
  failed: RetrospectiveReviewFailurePayload[];
  queued: RetrospectiveReviewQueuedPayload[];
  refresh_triggered: boolean;
  skipped: RetrospectiveReviewDecisionPayload[];
  trigger_label: string;
  workflow: "retrospective_review";
}

export interface RetrospectiveReviewFailurePayload {
  error: string;
  issue: number;
}

export interface RetrospectiveReviewPreflightPayload {
  decisions: RetrospectiveReviewDecisionPayload[];
  eligible: number[];
  skipped: number[];
  trigger_label: string;
  workflow: "retrospective_review";
}

export interface RetrospectiveReviewQueuedPayload {
  action: string;
  agent_label: string | null;
  eligible: boolean;
  issue: number;
  labels: string[];
  prior_pr_number: number | null;
  prior_pr_url: string | null;
  queued: boolean;
  reason: string;
  state: string | null;
  title: string | null;
  trigger_label: string | null;
}

export interface ReviewApprovedPayload {
  commands: TimelineCommandPayload[];
  completed_at: string;
  kind: "review_approved";
  reviewer: AgentIdentityPayload;
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
  transcript: ReviewTranscriptEvidencePayload;
}

export interface ReviewChangesRequestedPayload {
  commands: TimelineCommandPayload[];
  completed_at: string;
  feedback_summary: string;
  kind: "review_changes_requested";
  reviewer: AgentIdentityPayload;
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
}

export interface ReviewFailedPayload {
  commands: TimelineCommandPayload[];
  diagnostics: TimelineDiagnosticPayload[];
  failed_at: string;
  kind: "review_failed";
  reason: string;
  reviewer?: AgentIdentityPayload | null;
  session_recording: SessionRecordingEvidencePayload;
  started_at?: string | null;
}

export interface ReviewNotReachedPayload {
  kind: "review_not_reached";
  reason: "coding_in_progress" | "coding_failed" | "publish_failed" | "validation_failed" | "not_required";
}

export interface ReviewRunningPayload {
  commands: TimelineCommandPayload[];
  kind: "review_running";
  reviewer: AgentIdentityPayload;
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
}

export interface ReviewSkippedPayload {
  kind: "review_skipped";
  reason: string;
}

export interface ReviewTranscriptAvailablePayload {
  kind: "available";
}

export interface ReviewTranscriptUnavailablePayload {
  diagnostics: TimelineDiagnosticPayload[];
  kind: "unavailable";
  reason: string;
}

export interface RunningCodingAttemptPayload {
  agent: AgentIdentityPayload;
  commands: TimelineCommandPayload[];
  issue_number: number;
  kind: "running_coding_attempt";
  session_recording: SessionRecordingEvidencePayload;
  started_at: string;
}

export interface RunningE2ETestExecutionPayload {
  commands: TimelineCommandPayload[];
  kind: "running_e2e_test";
  linked_issues: LinkedIssueLifecyclePayload[];
  nodeid: string;
  started_at: string;
}

export interface SessionDiagnosticsActionPayload {
  group?: "validation_artifacts" | "session_evidence" | "diagnostics" | null;
  issue_number?: number | null;
  label: string;
  path?: string | null;
  type: string;
  [key: string]: any;
}

export interface SessionDiagnosticsAnalysisPayload {
  detail?: string | null;
  headline: string;
  suggestions?: string[];
}

export interface SessionDiagnosticsDialogPayload {
  actions: SessionDiagnosticsActionPayload[];
  analysis?: SessionDiagnosticsAnalysisPayload | null;
  follow_up_issues?: SessionDiagnosticsFollowUpIssuePayload[];
  rows: DialogRowPayload[];
  title: string;
}

export interface SessionDiagnosticsFollowUpIssuePayload {
  blocking: boolean;
  evidence?: string | null;
  reason: string;
  suggested_labels?: string[];
  title: string;
}

export interface SessionRecordingAvailablePayload {
  command: OpenSessionRecordingCommandPayload;
  kind: "available";
  recording_path: string;
  run_dir: string;
}

export interface SessionRecordingUnavailablePayload {
  diagnostics: TimelineDiagnosticPayload[];
  kind: "unavailable";
  reason: string;
}

export interface ShowEventDetailsCommandPayload {
  event_ref: string;
  kind: "show_event_details";
  label: string;
}

export interface StackChipViewPayload {
  mode_label: string;
  status_text: string;
  title: string;
  tone: string;
}

export interface StackDependencyGatePayload {
  gate: string;
  open: boolean;
  reason_codes: string[];
  reasons: string[];
}

export interface StackDependencyGateViewPayload {
  approval_freshness: string;
  blocked_gates: string[];
  blocked_reason_codes: string[];
  gates: StackDependencyGatePayload[];
  has_stack_edges: boolean;
  issue_number: number;
  mode: string;
  predecessors: StackDependencyPredecessorPayload[];
  stack_base_branch: string | null;
  stale: boolean;
  stale_reason_codes: string[];
  successors: StackDependencySuccessorPayload[];
}

export interface StackDependencyPredecessorPayload {
  mode: string;
  problem: string | null;
  ref: string;
  state: string;
}

export interface StackDependencySuccessorPayload {
  issue_number: number;
  mode: string;
  ref: string;
}

export interface SwitchE2ETimelineViewCommandPayload {
  kind: "switch_e2e_timeline_view";
  label: string;
  run_id: number;
  view: TimelineView;
}

export interface TechLeadGlobalHealthReviewScopePayload {
  kind: "global_health_review";
}

export interface TechLeadIssueScopePayload {
  issue_number: number;
  kind: "issue";
}

export interface TechLeadRunAdmissionPayload {
  admitted: boolean;
  behind_global_barrier: boolean;
  detail: string;
  issue_number: number | null;
  outcome: "queued" | "already_queued" | "already_running" | "paused" | "not_running" | "not_configured" | "not_eligible" | "claim_conflict" | "failed";
  reason: string;
  run_key: string;
  scope_kind: "global_health_review" | "global_batch_review" | "issue";
}

export interface TechLeadRunRequestPayload {
  scope: TechLeadRunScopePayload;
}

export interface TestCaseHistoryPayload {
  outcome: string;
  run_id: number;
}

export interface TestCaseIssueLinkPayload {
  number: number;
  resolution: string | null;
  status: string;
}

export interface TestCaseResultPayload {
  captured_output: CapturedOutputAvailabilityPayload;
  case_id: string;
  category: string;
  display_name: string | null;
  duration_seconds: number | null;
  existing_issue: TestCaseIssueLinkPayload | null;
  failure_summary: string | null;
  flip_rate: number;
  flip_rate_percent: number;
  history: TestCaseHistoryPayload[];
  is_likely_flaky: boolean;
  is_quarantined: boolean;
  label: string;
  longrepr: string | null;
  nodeid: string;
  outcome: string;
  result_category: string;
  result_source: string;
  retry_outcome: string | null;
  suite_name: string | null;
  updated_at: string;
}

export interface TestRunArtifactPayload {
  kind: string;
  label: string;
  path: string;
}

export interface TimelineDiagnosticPayload {
  code: string;
  evidence_ref?: string | null;
  message: string;
  severity: "info" | "warning" | "error";
}

export interface TimelineSubjectPayload {
  id: string;
  kind: "dashboard" | "issue" | "e2e_suite" | "e2e_run";
  label: string;
  outcome?: string | null;
  status?: string | null;
}

export interface ValidationEvidenceMissingPayload {
  diagnostics: TimelineDiagnosticPayload[];
  expected_record_path?: string | null;
  kind: "missing_evidence";
}

export interface ValidationExtraPayload {
  namespace: string;
  payload: Record<string, any>;
}

export interface ValidationFailedPayload {
  command: string;
  details_command: OpenValidationDetailsCommandPayload;
  failure_summary: string;
  kind: "failed";
  record_path: string;
}

export interface ValidationFailureActionSectionPayload {
  actions: SessionDiagnosticsActionPayload[];
  title: string;
}

export interface ValidationFailureDialogPayload {
  action_sections: ValidationFailureActionSectionPayload[];
  command: string;
  ended_at: string;
  exit_code: number | null;
  failed_tests: string[];
  junit_cases: JUnitCasePayload[];
  reason: string;
  started_at: string;
  status: "passed" | "failed";
  stderr_excerpt: string[];
  stdout_excerpt: string[];
  suite: string;
  summary_rows: DialogRowPayload[];
  title: string;
}

export interface ValidationNotRunPayload {
  kind: "not_run";
  reason: "coding_in_progress" | "validation_disabled" | "not_required";
}

export interface ValidationPassedPayload {
  command: string;
  details_command: OpenValidationDetailsCommandPayload;
  kind: "passed";
  record_path: string;
}

export interface ViewModelSnapshotPayload {
  active_tab: string;
  count: number;
  rows: IssueRowPayload[];
  view_model: DashboardViewModelPayload;
}

export interface WorktreeAuditEntryPayload {
  disposition: WorktreeAuditDisposition;
  kind: WorktreeAuditKind;
  name: string;
  path: string;
  reason: string;
}

export interface WorktreeAuditRequestPayload {
  repo_root: string;
}

export interface WorktreeAuditResponsePayload {
  activity_evidence: WorktreeAuditActivityEvidence;
  audit_unavailable: boolean;
  cleanup_candidates: WorktreeAuditEntryPayload[];
  issue_cleanup_enabled: boolean | null;
  message: string;
  note: string | null;
  scope: WorktreeAuditScope;
  stale_worktrees: WorktreeAuditEntryPayload[];
  worktrees: WorktreeAuditEntryPayload[];
}

export type CodingAttemptPayload = RunningCodingAttemptPayload | CompletedCodingAttemptPayload | PublishFailedCodingAttemptPayload | BlockedCodingAttemptPayload | FailedCodingAttemptPayload | MissingCodingEvidencePayload;

export type E2EFailureEvidencePayload = E2EFailureDetailsAvailablePayload | E2EFailureDetailsMissingPayload;

export type E2ETestExecutionPayload = PassedE2ETestExecutionPayload | FailedE2ETestExecutionPayload | RunningE2ETestExecutionPayload | MissingE2ETestEvidencePayload;

export type LifecycleTimelineContainerPayload = DashboardTimelineContainerPayload | E2ESuiteTimelineContainerPayload;

export type ReviewStagePayload = ReviewNotReachedPayload | ReviewSkippedPayload | ReviewRunningPayload | ReviewApprovedPayload | ReviewChangesRequestedPayload | ReviewFailedPayload | MissingReviewEvidencePayload;

export type ReviewTranscriptEvidencePayload = ReviewTranscriptAvailablePayload | ReviewTranscriptUnavailablePayload;

export type SessionRecordingEvidencePayload = SessionRecordingAvailablePayload | SessionRecordingUnavailablePayload;

export type TechLeadRunScopePayload = TechLeadGlobalHealthReviewScopePayload | TechLeadIssueScopePayload;

export type TimelineCommandPayload = ShowEventDetailsCommandPayload | OpenCompletionRecordCommandPayload | OpenValidationDetailsCommandPayload | OpenSessionRecordingCommandPayload | OpenReviewFeedbackCommandPayload | OpenReviewArtifactCommandPayload | OpenIssueTimelineCommandPayload | OpenE2ERunCommandPayload | ExpandE2ERunCommandPayload | SwitchE2ETimelineViewCommandPayload | CreateE2EUntriagedIssuesCommandPayload | OpenInlineAgentAttemptsCommandPayload;

export type ValidationOutcomePayload = ValidationPassedPayload | ValidationFailedPayload | ValidationNotRunPayload | ValidationEvidenceMissingPayload;
