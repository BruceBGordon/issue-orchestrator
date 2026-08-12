export interface SupervisorStatus {
  state: string;
  pid?: number;
  port?: number;
  started_at?: string;
  recovered?: boolean;
  error?: string;
  instance_id?: string;
}

export interface Snapshot {
  status: StatusPayload;
  info: InfoPayload;
  blocked: BlockedPayload;
  stale: StalePayload;
  dependency_problems: DependencyProblemsPayload;
  excluded: ExcludedPayload;
  history: HistoryPayload;
}

export interface StatusPayload {
  paused: boolean;
  shutdown_requested: boolean;
  active_sessions: ActiveSession[];
  max_sessions: number;
  completed_today: number[];
  queue: IssueSummary[];
  pending_reviews: PendingReview[];
  tick_id: number | null;
  last_tick_time: number | null;
  e2e_role: string | null;
}

export interface InfoPayload {
  repo: string | null;
  repo_root: string | null;
  ui_mode: string | null;
  terminal_backend: string | null;
  client_capabilities?: ClientCapabilities;
  commit_sha: string | null;
  commit_short: string | null;
  max_sessions: number;
  active_sessions: number;
  completed_today: number;
}

export interface ClientCapabilities {
  focus_session: boolean;
  open_path: boolean;
  reveal_worktree: boolean;
  local_server_paths_only: boolean;
  host_platform: string;
}

export interface IssueSummary {
  number: number;
  title: string;
  labels?: string[];
  priority?: string | null;
  agent_type?: string | null;
  issue_url?: string | null;
  flow_stage?: string | null;
  blocked_summary?: string | null;
}

export interface ActiveSession {
  issue_number: number;
  title: string;
  runtime_minutes: number;
  agent_type: string;
  status: string;
  branch: string;
}

export interface PendingReview {
  issue_number: number;
  pr_number: number;
  pr_url: string;
  branch_name: string;
}

export interface BlockedPayload {
  blocked: IssueSummary[];
  count: number;
}

export interface StalePayload {
  stale: IssueSummary[];
  count: number;
}

export interface DependencyProblemsPayload {
  problems: Record<string, unknown>;
}

export interface ExcludedPayload {
  excluded: IssueSummary[];
  count: number;
}

export interface HistoryPayload {
  history: HistoryEntry[];
  count: number;
}

export interface HistoryEntry {
  issue_number: number;
  title: string;
  agent_type: string;
  status: string;
  runtime_minutes: number;
  pr_url?: string | null;
  status_reason?: string | null;
  worktree_path?: string | null;
}

export interface McpStatusPayload {
  supervisor: SupervisorStatus;
  status?: StatusPayload;
  info?: InfoPayload;
}

export interface McpError {
  message: string;
  type?: string;
}

/**
 * Mirrors `LaunchStatus` in `src/issue_orchestrator/infra/launcher.py`. The
 * server rejects any status outside this set rather than reporting it as a
 * success, so the union is closed on the wire too.
 */
export type LaunchStatus =
  | "ok"
  | "doctor_warning"
  | "already_running"
  | "doctor_error"
  | "launch_error"
  | "configuration_conflict";

export interface LaunchResult {
  status: LaunchStatus;
  launched: boolean;
  doctor?: DoctorReport;
  error?: string;
  supervisor?: Record<string, unknown>;
}

export interface StartResponse {
  supervisor?: SupervisorStatus;
  /**
   * Present when this call ran the launcher. Operator detail only — do NOT
   * decide success from `launch.status`. The server normalises every failure
   * (thrown or returned) onto the top-level `error` below; reading the nested
   * status here would duplicate that mapping on the client.
   */
  launch?: LaunchResult;
  error?: McpError;
  ui_hint?: UiHint;
}

export interface UiHint {
  kind: string;
  url?: string;
}

export interface DoctorCheck {
  name: string;
  status: string;
  detail: string;
}

export interface DoctorReport {
  overall: string;
  checks: DoctorCheck[];
}
