"""Configuration loading and management."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from ..domain.issue_filter import IssueLabelFilter

from ..domain.models import AgentConfig, CommentHeadings
from .config_models import (
    AiGateConfig as AiGateConfig,
    ClaimsConfig,
    CleanupConfig,
    CleanupWithTechLead as CleanupWithTechLead,
    CleanupWithoutTechLead as CleanupWithoutTechLead,
    CoverageGuardrailConfig,
    DangerousConfig,
    DefaultAgentConfig,
    E2EConfig,
    FilteringConfig,
    GoalPilotConfig,
    HooksConfig,
    InterruptedSessionRetryConfig as InterruptedSessionRetryConfig,
    IsolationConfig,
    MergeQueueConfig,
    MilestoneStrategyConfig as MilestoneStrategyConfig,
    ProviderCircuitBreakerConfig as ProviderCircuitBreakerConfig,
    ProviderResilienceConfig,
    ProviderShortRetryConfig as ProviderShortRetryConfig,
    RetryConfig,
    SchedulingConfig,
    SessionInteractionsConfig,
    SqliteBackupConfig,
    TimelineConfig,
    TechLeadActivationOwner,
    TechLeadConfig,
    ValidationConfig,
)
from .config_identity import ConfigLaunchIdentity, RuntimeConfigReferenceOwner
from .config_paths import (
    CONFIG_DIR as CONFIG_DIR,
    DEFAULT_CONFIG_NAME as DEFAULT_CONFIG_NAME,
    ConfigEnvVarError as ConfigEnvVarError,
    ConfigSectionError as ConfigSectionError,
    config_exists as config_exists,
    expand_env_vars as _expand_env_vars,
    find_config_file as find_config_file,
    get_config_dir as get_config_dir,
    get_config_path as get_config_path,
    list_configs as list_configs,
    list_modes as list_modes,
    repo_root_from_config_path as repo_root_from_config_path,
    resolve_relative_path as resolve_relative_path,
    selection_from_config_path as selection_from_config_path,
)
from . import github_config as _github_config
from .config_sections import (
    ALLOWED_AGENT_FIELDS as ALLOWED_AGENT_FIELDS,
    ALLOWED_TOP_LEVEL_FIELDS as ALLOWED_TOP_LEVEL_FIELDS,
    _TOP_LEVEL_SECTION_KEYS as _TOP_LEVEL_SECTION_KEYS,
    apply_optional_sections,
    extract_config_sections,
    load_agents_section,
    load_cleanup_section,
    load_execution_section,
    load_github_write_verify,
    load_labels_section,
    load_observability_section,
    load_repo_section,
    load_review_section,
    load_retry_section,
    load_security_section,
    load_ui_section,
    load_validation_section,
    load_worktrees_section,
    parse_ai_systems_allowed,
    parse_default_agent_config,
    parse_filtering_config,
    parse_milestone_order,
)
from .config_value_rules import resolve_tech_lead_watch_label
from .config_review_projection import (
    internal_review_dict,
    runtime_exchange_dict,
    runtime_run_audit_dict,
    serialized_internal_review_dict,
)
from .validation_config_loader import (
    load_validation_config as load_validation_config,
    load_validation_config_from_file as load_validation_config_from_file,
    load_runtime_validation_config as load_runtime_validation_config,
)


def _put_if_truthy(target: dict, key: str, value: object) -> None:
    """Insert ``key`` only when truthy (to_dict omits defaults)."""
    if value:
        target[key] = value


@dataclass
class Config(ConfigLaunchIdentity, RuntimeConfigReferenceOwner, TechLeadActivationOwner):
    """Orchestrator configuration."""

    # Agent configurations keyed by label (e.g., "agent:web")
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    # Default agent configuration inherited by agents without explicit provider
    default_agent: Optional[DefaultAgentConfig] = None

    # Concurrency settings
    max_concurrent_sessions: int = 3
    session_timeout_minutes: int = 45

    # Label names (customizable)
    label_in_progress: str = "in-progress"
    label_blocked: str = "blocked"
    label_needs_human: str = "needs-human"
    label_needs_rework: str = "needs-rework"  # Applied to PR when reviewer requests changes
    label_validation_failed: str = "validation-failed"  # Applied when validation fails
    label_prefix: Optional[str] = None  # Optional prefix for all labels (e.g., "bot")

    # Paths
    state_file: Path = Path(".issue-orchestrator/state.json")
    repo_root: Path = field(default_factory=Path.cwd)  # Root of the git repository
    repo_root_from_yaml: bool = False  # Internal: YAML explicitly set repo_root
    worktree_base: Path = Path(".issue-orchestrator/worktrees")  # Base directory for worktrees
    worktree_base_branch_override: Optional[str] = None  # Override base branch for worktree creation
    worktree_seed_ref: Optional[str] = None  # Optional local ref to seed fresh issue worktrees
    worktree_branch_on_recreate: str = "delete"  # delete or create_new_branch

    # AI systems allowlist (merged with built-in ai_systems.yaml)
    ai_systems_allowed: list[str] = field(default_factory=list)

    # GitHub settings
    repo: Optional[str] = None  # owner/repo, or None to auto-detect
    github_token: Optional[str] = None  # Explicit GitHub token (prefer env)
    github_token_env: Optional[str] = None  # Env var name for token (overrides defaults)
    github_keyring_service: Optional[str] = None  # Optional repo-specific keyring service name
    github_keyring_username: Optional[str] = None  # Optional repo-specific keyring username/account
    github_app_client_id: Optional[str] = None  # GitHub App Client ID for installation auth
    github_app_id: Optional[str] = None  # GitHub App ID fallback for JWT issuer
    github_app_installation_id: Optional[str] = None  # GitHub App installation ID
    github_app_private_key_path: Optional[str] = None  # PEM path for GitHub App private key
    github_app_private_key_env: Optional[str] = None  # Env var containing GitHub App private key
    github_api_url: str = "https://api.github.com"
    github_http_timeout_seconds: float = 20.0
    github_cache_ttl_seconds: int = 300  # Cache TTL for GitHub adapter responses
    github_required_scopes: list[str] = field(default_factory=list)
    github_allowed_scopes: list[str] = field(default_factory=list)

    # Issue filtering
    filtering: FilteringConfig = field(default_factory=FilteringConfig)

    # E2E test configuration
    e2e_pr_labels: list[str] = field(default_factory=list)  # Labels to apply to PRs created during e2e tests

    # Comment headings for structured worker comments
    comment_headings: CommentHeadings = field(default_factory=CommentHeadings)

    # Logging
    log_retention_days: int = 7  # Days to keep rotated log files
    session_output_retention_runs: int = 7  # Runs to keep per worktree
    session_output_retention_days: int = 7  # Minimum days to retain terminal Timeline evidence
    session_output_retention_tier: str = "hot"  # Retention tier tag stored in run manifest

    # UI mode: "web" (default, browser dashboard)
    ui_mode: str = "web"
    web_port: int = 0  # Port for web dashboard (0 = auto-assign free port)
    control_api_port: int = 0  # Port for control API (0 = auto-assign free port)
    queue_refresh_seconds: int = 600  # How often web UI refetches queue from GitHub (0 = manual only)
    # Browser-session knobs for the Control Center login flow (security
    # #5987 F3). Overridable at runtime by
    # ISSUE_ORCHESTRATOR_SESSION_TTL_SECONDS /
    # ISSUE_ORCHESTRATOR_SSE_TOKEN_TTL_SECONDS /
    # ISSUE_ORCHESTRATOR_MAX_SESSIONS env vars; see ``browser_session``.
    browser_session_ttl_seconds: int = 8 * 3600
    browser_session_max: int = 1024
    sse_token_ttl_seconds: int = 60
    # Fetch-layer optimization for queue refreshes
    fetch_layer_enabled: bool = True
    fetch_layer_network_sync_seconds: int = 60
    fetch_layer_full_scan_interval_seconds: int = 1800
    fetch_layer_discovery_limit: int = 25
    fetch_layer_max_hot_issues_per_cycle: int = 40
    fetch_layer_pr_scan_every_n_refreshes: int = 2
    fetch_layer_dependency_scan_every_n_refreshes: int = 1
    fetch_layer_visibility_aware_enabled: bool = False
    fetch_layer_selective_sync_planner_enabled: bool = False
    # Flow UI lazy refresh policy (visible stale cards only)
    flow_refresh_enabled: bool = True
    flow_refresh_stale_seconds: int = 900
    flow_refresh_cooldown_seconds: int = 120
    flow_freshness_mode: str = "balanced"  # aggressive | balanced | economy
    flow_api_budget: str = "medium"  # low | medium | high
    flow_attention_priority: str = "strict"  # strict | normal

    # Dashboard shows "Orchestrator tick stalled" when the main loop has not
    # completed a tick in this many seconds. Busy orchestrators with a heavy
    # process_active_sessions sweep may legitimately take >60s; operators
    # should tune to their local tick budget. Keep a generous floor so a
    # single slow tick doesn't false-positive the banner.
    tick_stall_threshold_seconds: int = 60

    # Multi-instance support (for multi-orchestrator coordination)
    instances: int = 1  # Number of orchestrator instances to spawn (CC manages this)
    session_no_output_seconds: int = 120  # Emit session_no_output after this many seconds idle
    session_no_output_tail_lines: int = 50  # Max tail lines to include in session_no_output
    session_no_output_max_bytes: int = 10000  # Max bytes of tail content
    session_no_output_repeat_seconds: int = 120  # Minimum gap between session_no_output events

    # Session detection - be lenient to avoid false terminations
    # These protect against session detection failures during startup
    session_grace_period_seconds: int = 120  # Don't terminate sessions younger than this
    session_log_activity_seconds: int = 120  # If log modified within this window, session is alive

    gh_write_verify_timeout_seconds: int = 20
    gh_write_verify_initial_delay_ms: int = 250
    gh_write_verify_max_delay_ms: int = 2000
    gh_write_verify_backoff: float = 1.5
    gh_write_verify_jitter_ms: int = 0
    gh_rate_limit_startup: bool = True  # Log GH rate limits at startup
    gh_rate_limit_every_calls: int = 500  # Check GH rate limits every N calls (0 = disabled)
    gh_rate_limit_warn_fraction: float = 0.1  # Warn when remaining below fraction of limit
    gh_rate_limit_warn_remaining: int = 100  # Warn when remaining below this count
    gh_audit_enabled: bool = False  # Enable GH audit reporting
    gh_audit_events: bool = False  # Emit GH audit events to event stream
    gh_audit_file: Optional[str] = None  # Path for GH audit report (supports {pid})

    # Terminal adapter (optional - overrides ui_mode if set)
    # Can be "subprocess" or a full class path for custom adapters
    terminal_adapter: Optional[str] = None
    session_interactions: SessionInteractionsConfig = field(
        default_factory=SessionInteractionsConfig
    )

    # Milestone sorting strategy - built-in: "milestone_number", "due_date", "pattern", "name"
    # Or provide a custom class path like "mymodule.MyStrategy"
    milestone_sort: str = "milestone_number"
    # Config passed to strategy via **kwargs (e.g., pattern="M(\\d+)" for PatternStrategy)
    milestone_sort_config: dict = field(default_factory=dict)
    # Optional explicit order for milestones (titles). Unlisted milestones follow milestone_sort.
    milestone_order: list[str] = field(default_factory=list)
    # Foundation milestone - dependencies must be same milestone OR in foundation
    foundation_milestone: str = "M0"

    # Cleanup configuration - when to close AI session tabs and remove worktrees
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)

    # Enforcement options
    enforce_hooks: bool = True  # Install pre-push hooks (runs project validation + orchestrator checks)
    pre_push_hook: Optional[Path] = None  # Custom pre-push hook path (uses bundled if None)

    # Client-repo-specific setup commands (run after worktree creation).
    # No default — users must configure these for their repo (e.g., npm install,
    # pip install -e '.[dev]'). The orchestrator's own setup (hooks, claude
    # settings, coding-done/reviewer-done availability) is handled automatically.
    setup_worktree: list[str] = field(default_factory=list)
    # Preflight a dry-run push when reusing worktrees to catch stale refs early.
    reuse_push_preflight: bool = True
    # Allow git push --dry-run --no-verify for reuse preflight (default on).
    allow_no_verify_dry_run_preflight: bool = True
    # Remediation strategies for publish-time failures.
    worktree_remediation_pr_collision: str = "new_branch"  # fail | reuse_open | new_branch
    worktree_remediation_push_rebase_retry: bool = True

    # Code review workflow (optional) - per-PR review after agent creates PR
    review_enabled: bool = False  # Explicit toggle for code review
    code_review_agent: Optional[str] = None  # Agent that reviews PRs (e.g., "agent:reviewer")
    code_review_label: Optional[str] = None  # Label on PRs needing review (e.g., "needs-code-review")
    code_reviewed_label: Optional[str] = None  # Label after review passes (e.g., "code-reviewed")

    # Tech Lead/batch review workflow (optional) - pattern review across multiple PRs
    tech_lead_review_agent: Optional[str] = None  # Agent that does batch reviews (e.g., "agent:tech-lead")
    tech_lead_review_label: Optional[str] = None  # Label for PRs awaiting tech_lead review (uses code_reviewed_label if not set)
    tech_lead_reviewed_label: str = "tech-lead-reviewed"  # Label after tech_lead review (matches load_review_section default)
    tech_lead_failed_label: str = "tech-lead-failed"  # Label when tech_lead fails (matches load_review_section default)
    tech_lead_review_threshold: int = 0  # Trigger tech_lead review after N PRs (0 = manual only)
    tech_lead_review_on_failure: bool = True  # Trigger tech_lead to investigate when sessions fail
    tech_lead_follow_up_agent: Optional[str] = None

    @property
    def tech_lead_watch_label(self) -> str:
        """The single owned label that selects PRs for tech_lead batch review.

        Fact gathering (threshold trigger), manifest building (session
        inputs), and prompt generation must all use this one derivation so
        the PR set that trips the threshold is exactly the set the tech_lead
        session audits and labels.
        """
        return resolve_tech_lead_watch_label(
            self.tech_lead_review_label, self.code_reviewed_label
        )

    # Rework cycle limit (when reviewer requests changes)
    max_rework_cycles: int = 5  # Max times to re-queue work agent before escalating to needs-human

    # Publish failure limit (push/PR creation fails after agent completes)
    max_consecutive_publish_failures: int = 3  # Escalate to needs-human after N consecutive publish failures

    # Reviewer feedback cache: write feedback locally on review completion and use it
    # for rework sessions within this time window (avoids GitHub eventual consistency issues)
    # -1 = disabled, 0+ = minutes to trust local file over GitHub API
    reviewer_feedback_cache_minutes: int = 5  # Default: 5 minutes
    # Label to tell reviewer to keep the current approach
    review_keep_current_approach_label: str = "reviewer-keep-current-approach"
    review_run_audit_min_runtime_minutes: int = 20  # 0 disables automatic run audits
    review_run_audit_on_timeout: bool = True
    retrospective_review_enabled: bool = False
    retrospective_review_trigger_label: str = "retrospective-review"
    retrospective_reviewed_label: str = "retrospective-reviewed"
    retrospective_changes_requested_label: str = "retrospective-changes-requested"

    # Fast coder-owned review loop that runs before any successful coder handoff.
    internal_review_enabled: bool = False
    internal_review_max_rounds: int = 5
    internal_review_instructions: str = ".io/internal-review.md"

    # Review exchange mode (via-mcp, via-local-loop, or via-draft-pr review)
    review_exchange_mode: str = "via-local-loop"
    review_exchange_probe_schedule: str = "daily"  # startup, daily, interval, manual
    review_exchange_probe_interval_days: int = 1
    review_exchange_max_rounds: int = 10
    review_exchange_max_no_progress: int = 2
    review_exchange_require_validation: bool = True
    review_nits_default_policy: str = "surface"
    review_nits_by_agent: dict[str, str] = field(default_factory=dict)
    # Cap on consecutive review-exchange failures with reason
    # ``*_no_completion`` (reviewer or coder timed out without writing
    # a verdict). After N failures in a row on the same coding session,
    # escalate to needs-human instead of relaunching forever. Mirrors
    # ``max_consecutive_publish_failures`` for publish loops.
    max_consecutive_review_exchange_failures: int = 3

    # Post-publish: when a reviewer-approved PR sits in
    # mergeable_state ∈ {unstable, blocked} with the status-check
    # rollup not yet conclusive (PENDING/EXPECTED/unknown), the
    # orchestrator waits up to this long for required checks to
    # finalize before escalating to needs-human. 30 minutes by
    # default — tune higher for repos with long CI.
    post_publish_checks_pending_timeout_seconds: float = 1800.0

    # Dangerous options (use with caution)
    dangerous: DangerousConfig = field(default_factory=DangerousConfig)

    # Validation configuration - quick agent feedback plus deeper publish gate
    validation: ValidationConfig = field(default_factory=ValidationConfig)

    # Retry configuration - validation retry with error injection
    retry: RetryConfig = field(default_factory=RetryConfig)

    # Provider resilience configuration (retries + circuit breaker)
    provider_resilience: ProviderResilienceConfig = field(default_factory=ProviderResilienceConfig)

    # Isolation configuration - how agents are sandboxed
    isolation: IsolationConfig = field(default_factory=IsolationConfig)

    # Tech Lead issue configuration - label/milestone inheritance
    tech_lead: TechLeadConfig = field(default_factory=TechLeadConfig)

    # Scheduling configuration
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)

    # E2E async test runner configuration
    e2e: E2EConfig = field(default_factory=E2EConfig)

    # Goal Pilot AI configuration
    goal_pilot: GoalPilotConfig = field(default_factory=GoalPilotConfig)
    # Optional GitHub Merge Queue integration (disabled by default)
    merge_queue: MergeQueueConfig = field(default_factory=MergeQueueConfig)
    # SQLite backup configuration
    sqlite_backup: SqliteBackupConfig = field(default_factory=SqliteBackupConfig)
    # Timeline retention configuration
    timeline: TimelineConfig = field(default_factory=TimelineConfig)

    # Claims/lease configuration for multi-orchestrator coordination
    claims: ClaimsConfig = field(default_factory=ClaimsConfig)

    # Hooks configuration - AI gate tests for agent hooks/execpolicy
    hooks: HooksConfig = field(default_factory=HooksConfig)

    # Stale in-progress escalation threshold (0 = disabled)
    # If an issue has stale in-progress for K consecutive ticks, emit escalation event
    stale_escalation_ticks: int = 0

    # Path to the config file (set during load)
    config_path: Optional[Path] = None

    # Raw YAML data for unknown field validation (set during load)
    raw_data: dict = field(default_factory=dict)
    raw_agents: dict = field(default_factory=dict)

    @property
    def orchestrator_id(self) -> str:
        """Unique identifier for this orchestrator instance.

        Uses the directory name of repo_root as the identifier.
        This must be consistent across all components (orchestrator, web UI, control API)
        to ensure E2E test status and other cross-component data is correctly matched.
        """
        return self.repo_root.name

    def prefixed_label(self, label: str) -> str:
        """Return label with prefix if configured.

        Args:
            label: The base label name

        Returns:
            The label with prefix if configured, otherwise the original label
        """
        if self.label_prefix:
            return f"{self.label_prefix}:{label}"
        return label

    def get_label_in_progress(self) -> str:
        """Get the in-progress label with prefix if configured."""
        return self.prefixed_label(self.label_in_progress)

    def get_label_blocked(self) -> str:
        """Get the blocked label with prefix if configured."""
        return self.prefixed_label(self.label_blocked)

    def get_label_provider_unavailable(self) -> str:
        """Get the provider-unavailable blocked label with prefix if configured."""
        return self.prefixed_label(self.provider_resilience.circuit_breaker.label)

    def get_label_needs_human(self) -> str:
        """Get the needs-human label with prefix if configured."""
        return self.prefixed_label(self.label_needs_human)

    def get_label_needs_rework(self) -> str:
        """Get the needs-rework label with prefix if configured."""
        return self.prefixed_label(self.label_needs_rework)

    def get_label_validation_failed(self) -> str:
        """Get the validation-failed label with prefix if configured."""
        return self.prefixed_label(self.label_validation_failed)

    def is_validation_enabled(self) -> bool:
        """Check if any validation command is configured."""
        return bool(self.validation.quick.cmd or self.validation.publish.cmd)

    def get_filter_milestones(self) -> list[str]:
        """Return a list of milestone filters."""
        return self.filtering.get_milestones()

    def get_issue_filter(self) -> "IssueLabelFilter":
        """Get the issue label filter configured for this config.

        Returns an IssueLabelFilter instance that can filter issues based on
        exact-label and label-prefix exclusion configuration.
        """
        from ..domain.issue_filter import IssueLabelFilter
        return IssueLabelFilter.from_config(
            exclude_labels=self.filtering.exclude_labels,
            exclude_label_prefixes=self.filtering.exclude_label_prefixes,
        )

    def github_auth_kwargs(self) -> dict[str, str | None]:
        """Return repo-scoped GitHub auth settings keyed for auth helpers."""
        return _github_config.github_auth_kwargs(self)

    def github_app_auth_configured(self) -> bool:
        """Return whether any GitHub App auth field is configured."""
        return _github_config.github_app_auth_configured(self)

    def get_reviewer_for_agent(self, agent_label: str) -> Optional[str]:
        """Get the effective reviewer for an agent.

        Returns the per-agent reviewer if set, otherwise the default reviewer
        (code_review_agent). Returns None if no reviewer is configured.
        """
        if agent_label not in self.agents:
            return self.code_review_agent
        agent = self.agents[agent_label]
        # Per-agent reviewer takes precedence over default
        return agent.reviewer or self.code_review_agent

    def _serialization_exchange_dict(self) -> dict:
        exchange_dict: dict = {}
        if self.review_exchange_mode != "via-local-loop":
            exchange_dict["mode"] = self.review_exchange_mode
        if self.review_exchange_probe_schedule != "daily" or self.review_exchange_probe_interval_days != 1:
            exchange_dict["probe"] = {
                "schedule": self.review_exchange_probe_schedule,
                "interval_days": self.review_exchange_probe_interval_days,
            }
        if (
            self.review_exchange_max_rounds != 10
            or self.review_exchange_max_no_progress != 2
            or self.review_exchange_require_validation is not True
        ):
            exchange_dict["loop"] = {
                "max_rounds": self.review_exchange_max_rounds,
                "max_no_progress": self.review_exchange_max_no_progress,
                "require_validation": self.review_exchange_require_validation,
            }
        return exchange_dict

    def get_label_review_keep_current_approach(self) -> str:
        """Get the reviewer keep-current-approach label with prefix if configured."""
        return self.prefixed_label(self.review_keep_current_approach_label)

    def to_event_dict(self) -> dict:
        """Convert config to a dict for event emission.

        Returns a serializable dict with the merged configuration
        (YAML + command line overrides) for debugging.
        """
        result = {
            "launch_selection": self.launch_identity_dict(),
            "repo": {
                "name": self.repo,
                "root": str(self.repo_root),
                "github": {
                    **_github_config.github_auth_event_fields(self),
                    "write_verify": {
                        "timeout_seconds": self.gh_write_verify_timeout_seconds,
                        "initial_delay_ms": self.gh_write_verify_initial_delay_ms,
                        "max_delay_ms": self.gh_write_verify_max_delay_ms,
                        "backoff": self.gh_write_verify_backoff,
                        "jitter_ms": self.gh_write_verify_jitter_ms,
                    },
                    "rate_limit": {
                        "startup": self.gh_rate_limit_startup,
                        "every_calls": self.gh_rate_limit_every_calls,
                        "warn_fraction": self.gh_rate_limit_warn_fraction,
                        "warn_remaining": self.gh_rate_limit_warn_remaining,
                    },
                    "audit": {
                        "enabled": self.gh_audit_enabled,
                        "events": self.gh_audit_events,
                        "file": self.gh_audit_file,
                    },
                },
            },
            "config": {
                "path": str(self.config_path) if self.config_path else None,
            },
            "state": {
                "file": str(self.state_file),
            },
            "worktrees": {
                "base": str(self.worktree_base),
                "base_branch_override": self.worktree_base_branch_override,
                "seed_ref": self.worktree_seed_ref,
                "setup": list(self.setup_worktree),
                "reuse_push_preflight": self.reuse_push_preflight,
                "allow_no_verify_dry_run_preflight": self.allow_no_verify_dry_run_preflight,
                "worktree_branch_on_recreate": self.worktree_branch_on_recreate,
                "remediation": {
                    "pr_collision": self.worktree_remediation_pr_collision,
                    "push_rebase_retry": self.worktree_remediation_push_rebase_retry,
                },
            },
            "execution": {
                "concurrency": {
                    "max_concurrent_sessions": self.max_concurrent_sessions,
                    "session_timeout_minutes": self.session_timeout_minutes,
                },
                "terminal_adapter": self.terminal_adapter,
                "isolation": {
                    "mode": self.isolation.mode,
                },
            },
            "ui": {
                "mode": self.ui_mode,
                "web_port": self.web_port,
                "control_api_port": self.control_api_port,
                "queue_refresh_seconds": self.queue_refresh_seconds,
                "fetch_layer": {
                    "enabled": self.fetch_layer_enabled,
                    "network_sync_seconds": self.fetch_layer_network_sync_seconds,
                    "full_scan_interval_seconds": self.fetch_layer_full_scan_interval_seconds,
                    "discovery_limit": self.fetch_layer_discovery_limit,
                    "max_hot_issues_per_cycle": self.fetch_layer_max_hot_issues_per_cycle,
                    "pr_scan_every_n_refreshes": self.fetch_layer_pr_scan_every_n_refreshes,
                    "dependency_scan_every_n_refreshes": self.fetch_layer_dependency_scan_every_n_refreshes,
                    "visibility_aware_enabled": self.fetch_layer_visibility_aware_enabled,
                    "selective_sync_planner_enabled": self.fetch_layer_selective_sync_planner_enabled,
                },
                "instances": self.instances,
                "flow_refresh": {
                    "enabled": self.flow_refresh_enabled,
                    "stale_seconds": self.flow_refresh_stale_seconds,
                    "cooldown_seconds": self.flow_refresh_cooldown_seconds,
                    "freshness_mode": self.flow_freshness_mode,
                    "api_budget": self.flow_api_budget,
                    "attention_priority": self.flow_attention_priority,
                },
            },
            "observability": {
                "session_no_output_seconds": self.session_no_output_seconds,
                "session_no_output_tail_lines": self.session_no_output_tail_lines,
                "session_no_output_max_bytes": self.session_no_output_max_bytes,
                "session_no_output_repeat_seconds": self.session_no_output_repeat_seconds,
                "session_output_retention_runs": self.session_output_retention_runs,
                "session_output_retention_days": self.session_output_retention_days,
                "session_output_retention_tier": self.session_output_retention_tier,
                "stale_escalation_ticks": self.stale_escalation_ticks,
                "comment_headings": {
                    "implementation": self.comment_headings.implementation,
                    "problems": self.comment_headings.problems,
                    "pr_link": self.comment_headings.pr_link,
                    "blocked": self.comment_headings.blocked,
                    "needs_human": self.comment_headings.needs_human,
                },
            },
            "security": {
                "enforce_hooks": self.enforce_hooks,
                "pre_push_hook": str(self.pre_push_hook) if self.pre_push_hook else None,
                "dangerous": {
                    "allow_unsupported_agents": self.dangerous.allow_unsupported_agents,
                },
            },
            "labels": {
                "in_progress": self.get_label_in_progress(),
                "blocked": self.get_label_blocked(),
                "needs_human": self.get_label_needs_human(),
                "needs_rework": self.get_label_needs_rework(),
                "validation_failed": self.get_label_validation_failed(),
                "prefix": self.label_prefix,
            },
            "validation": {
                "enabled": self.is_validation_enabled(),
                "quick": {
                    "cmd": self.validation.quick.cmd,
                    "timeout_seconds": self.validation.quick.timeout_seconds,
                },
                "publish": {
                    "cmd": self.validation.publish.cmd,
                    "timeout_seconds": self.validation.publish.timeout_seconds,
                    "dirty_check": self.validation.publish.dirty_check,
                },
                "coverage_guardrail": {
                    "enabled": self.validation.coverage_guardrail.enabled,
                    "min_percent": self.validation.coverage_guardrail.min_percent,
                    "apply_to": self.validation.coverage_guardrail.apply_to,
                    "scope": self.validation.coverage_guardrail.scope,
                    "coverage_type": self.validation.coverage_guardrail.coverage_type,
                    "exclude": self.validation.coverage_guardrail.exclude,
                },
                "junit_xml_paths": list(self.validation.junit_xml_paths),
            },
            "review": {
                "enabled": self.review_enabled,
                "default": self.code_review_agent,
                "code_review_label": self.code_review_label,
                "code_reviewed_label": self.code_reviewed_label,
                "run_audit": runtime_run_audit_dict(self),
                "retrospective": {
                    "enabled": self.retrospective_review_enabled,
                    "trigger_label": self.retrospective_review_trigger_label,
                    "reviewed_label": self.retrospective_reviewed_label,
                    "changes_requested_label": self.retrospective_changes_requested_label,
                },
                "internal": internal_review_dict(self),
                "exchange": runtime_exchange_dict(self),
                "nits": {
                    "default_policy": self.review_nits_default_policy,
                    "by_agent": dict(self.review_nits_by_agent),
                },
                "tech_lead_review": {
                    "agent": self.tech_lead_review_agent,
                    "label": self.tech_lead_review_label,
                    "reviewed_label": self.tech_lead_reviewed_label,
                    "threshold": self.tech_lead_review_threshold,
                    "on_failure": self.tech_lead_review_on_failure,
                },
                "max_rework_cycles": self.max_rework_cycles,
                "max_consecutive_publish_failures": self.max_consecutive_publish_failures,
                "max_consecutive_review_exchange_failures": self.max_consecutive_review_exchange_failures,
                "reviewer_feedback_cache_minutes": self.reviewer_feedback_cache_minutes,
            },
            "cleanup": {
                "with_tech_lead": {
                    "close_ai_session_tabs": self.cleanup.with_tech_lead.close_ai_session_tabs,
                    "remove_worktrees": self.cleanup.with_tech_lead.remove_worktrees,
                },
                "without_tech_lead": {
                    "wait_for_code_review": self.cleanup.without_tech_lead.wait_for_code_review,
                    "close_ai_session_tabs": self.cleanup.without_tech_lead.close_ai_session_tabs,
                    "remove_worktrees": self.cleanup.without_tech_lead.remove_worktrees,
                },
            },
            "milestones": {
                "sort": self.milestone_sort,
                "sort_config": self.milestone_sort_config,
                "order": list(self.milestone_order),
                "foundation": self.foundation_milestone,
            },
            "filtering": {
                "label": self.filtering.label,
                "milestone": self.filtering.milestone,
                "milestones": list(self.filtering.milestones),
                "issue": self.filtering.issue,
                "exclude_labels": list(self.filtering.exclude_labels),
                "exclude_label_prefixes": list(self.filtering.exclude_label_prefixes),
                "fetch_limit": self.filtering.fetch_limit,
                "max_to_start": self.filtering.max_to_start,
            },
            "tech_lead": self.tech_lead.to_event_dict(enabled=self.tech_lead_enabled),
            "scheduling": {
                "default_priority_tier": self.scheduling.default_priority_tier,
            },
            "e2e": {
                "pr_labels": self.e2e_pr_labels,
                "enabled": self.e2e.enabled,
                "auto_run_interval_minutes": self.e2e.auto_run_interval_minutes,
                "runner_kind": self.e2e.runner_kind,
                "pytest_args": list(self.e2e.pytest_args),
                "command": list(self.e2e.command),
                "junit_xml_paths": list(self.e2e.junit_xml_paths),
                "artifact_paths": list(self.e2e.artifact_paths),
                "allow_retry_once": self.e2e.allow_retry_once,
                "quarantine_file": self.e2e.quarantine_file,
                "survive_restart": self.e2e.survive_restart,
                "auto_quarantine": self.e2e.auto_quarantine,
                "auto_create_issues": self.e2e.auto_create_issues,
                "issue_agent_label": self.e2e.issue_agent_label,
            },
            "sqlite_backup": {
                "enabled": self.sqlite_backup.enabled,
                "cadence_hours": self.sqlite_backup.cadence_hours,
                "check_interval_minutes": self.sqlite_backup.check_interval_minutes,
                "retention_daily": self.sqlite_backup.retention_daily,
                "retention_weekly": self.sqlite_backup.retention_weekly,
                "enforce_on_startup": self.sqlite_backup.enforce_on_startup,
            },
            "claims": {
                "enabled": self.claims.enabled,
                "claimant_id": self.claims.claimant_id,
                "lease_seconds": self.claims.lease_seconds,
                "renew_before_expiry_seconds": self.claims.renew_before_expiry_seconds,
                "convergence_timeout_seconds": self.claims.convergence_timeout_seconds,
                "convergence_poll_min_ms": self.claims.convergence_poll_min_ms,
                "convergence_poll_max_ms": self.claims.convergence_poll_max_ms,
            },
            "hooks": {
                    "ai_gate": {
                        "interval_days": self.hooks.ai_gate.interval_days,
                        "dangerous_allow_failure": self.hooks.ai_gate.dangerous_allow_failure,
                    },
            },
            "merge_queue": {
                "enabled": self.merge_queue.enabled,
                "provider": self.merge_queue.provider,
                "enqueue_after": self.merge_queue.enqueue_after,
                "failure_action": self.merge_queue.failure_action,
            },
            "agents": {
                label: {
                    "prompt_path": str(cfg.prompt_path),
                    "model": cfg.model,
                    "timeout_minutes": cfg.timeout_minutes,
                    "meta_agent": cfg.meta_agent,
                }
                for label, cfg in self.agents.items()
            },
        }
        if self.session_interactions.enabled:
            result["execution"]["session_interactions"] = {"enabled": True}
        return result

    def to_dict(self) -> dict:  # noqa: C901, PLR0912 - serialization method handles many config fields
        """Convert config to a dict suitable for YAML serialization.

        Returns a dict that can be saved back to a YAML config file.
        This preserves the original YAML structure as closely as possible.
        """
        from .config_serialization import (
            filtering_section,
            goal_pilot_section,
            merge_queue_section,
            observability_section,
            worktrees_section,
        )

        # Build agents section
        agents_dict = {}
        for label, agent in self.agents.items():
            agent_dict: dict = {
                "prompt": agent.prompt_relative,
            }
            if agent.provider:
                agent_dict["provider"] = agent.provider
            if agent.model and agent.model != "sonnet":
                agent_dict["model"] = agent.model
            if agent.timeout_minutes != 45:
                agent_dict["timeout_minutes"] = agent.timeout_minutes
            if agent.provider_args:
                agent_dict["provider_args"] = dict(agent.provider_args)
            if agent.skip_review:
                agent_dict["skip_review"] = agent.skip_review
            if agent.sandbox:
                agent_dict["sandbox"] = agent.sandbox
            if agent.reviewer:
                agent_dict["reviewer"] = agent.reviewer
            if agent.meta_agent:
                agent_dict["meta_agent"] = agent.meta_agent
            if agent.ai_system:
                agent_dict["ai_system"] = agent.ai_system
            if agent.retry_prompt_template:
                agent_dict["retry_prompt_template"] = agent.retry_prompt_template
            agents_dict[label] = agent_dict

        result: dict = {
            "repo": {
                "name": self.repo,
            },
            "agents": agents_dict,
        }

        # Add default_agent if set
        if self.default_agent:
            default_agent_dict: dict = {}
            if self.default_agent.provider:
                default_agent_dict["provider"] = self.default_agent.provider
            if self.default_agent.model:
                default_agent_dict["model"] = self.default_agent.model
            if self.default_agent.provider_args:
                default_agent_dict["provider_args"] = dict(self.default_agent.provider_args)
            if default_agent_dict:
                result["default_agent"] = default_agent_dict

        # Labels section
        labels_dict: dict = {}
        if self.label_in_progress != "in-progress":
            labels_dict["in_progress"] = self.label_in_progress
        if self.label_blocked != "blocked":
            labels_dict["blocked"] = self.label_blocked
        if self.label_needs_human != "needs-human":
            labels_dict["needs_human"] = self.label_needs_human
        if self.label_needs_rework != "needs-rework":
            labels_dict["needs_rework"] = self.label_needs_rework
        if self.label_validation_failed != "validation-failed":
            labels_dict["validation_failed"] = self.label_validation_failed
        if self.label_prefix:
            labels_dict["prefix"] = self.label_prefix
        if labels_dict:
            result["labels"] = labels_dict

        # Execution section
        execution_dict: dict = {
            "concurrency": {
                "max_concurrent_sessions": self.max_concurrent_sessions,
                "session_timeout_minutes": self.session_timeout_minutes,
            },
        }
        if self.terminal_adapter:
            execution_dict["terminal_adapter"] = self.terminal_adapter
        if self.session_interactions.enabled:
            execution_dict["session_interactions"] = {
                "enabled": True,
            }
        if self.isolation.mode != "standard":
            execution_dict["isolation"] = {"mode": self.isolation.mode}
        result["execution"] = execution_dict

        # Provider resilience section
        provider_dict: dict = {}
        short_dict: dict = {}
        short = self.provider_resilience.short_retry
        if short.max_attempts != 4:
            short_dict["max_attempts"] = short.max_attempts
        if short.initial_backoff_seconds != 5:
            short_dict["initial_backoff_seconds"] = short.initial_backoff_seconds
        if short.max_backoff_seconds != 60:
            short_dict["max_backoff_seconds"] = short.max_backoff_seconds
        if short.jitter is not True:
            short_dict["jitter"] = short.jitter
        if short_dict:
            provider_dict["short_retry"] = short_dict

        circuit = self.provider_resilience.circuit_breaker
        circuit_dict: dict = {}
        if circuit.cooldown_seconds != 1800:
            circuit_dict["cooldown_seconds"] = circuit.cooldown_seconds
        if circuit.max_cooldowns != 6:
            circuit_dict["max_cooldowns"] = circuit.max_cooldowns
        if circuit.label != "blocked:provider-unavailable":
            circuit_dict["label"] = circuit.label
        if circuit.auth_failure_threshold != 1:
            circuit_dict["auth_failure_threshold"] = circuit.auth_failure_threshold
        if circuit.auth_cooldown_seconds != 21600:
            circuit_dict["auth_cooldown_seconds"] = circuit.auth_cooldown_seconds
        if circuit_dict:
            provider_dict["circuit_breaker"] = circuit_dict

        if provider_dict:
            result["provider_resilience"] = provider_dict

        # UI section
        ui_dict: dict = {}
        if self.ui_mode != "web":
            ui_dict["mode"] = self.ui_mode
        if self.web_port != 0:
            ui_dict["web_port"] = self.web_port
        if self.control_api_port != 0:
            ui_dict["control_api_port"] = self.control_api_port
        if self.queue_refresh_seconds != 600:
            ui_dict["queue_refresh_seconds"] = self.queue_refresh_seconds
        if (
            not self.fetch_layer_enabled
            or self.fetch_layer_network_sync_seconds != 60
            or self.fetch_layer_full_scan_interval_seconds != 1800
            or self.fetch_layer_discovery_limit != 25
            or self.fetch_layer_max_hot_issues_per_cycle != 40
            or self.fetch_layer_pr_scan_every_n_refreshes != 2
            or self.fetch_layer_dependency_scan_every_n_refreshes != 1
            or self.fetch_layer_visibility_aware_enabled
            or self.fetch_layer_selective_sync_planner_enabled
        ):
            ui_dict["fetch_layer"] = {
                "enabled": self.fetch_layer_enabled,
                "network_sync_seconds": self.fetch_layer_network_sync_seconds,
                "full_scan_interval_seconds": self.fetch_layer_full_scan_interval_seconds,
                "discovery_limit": self.fetch_layer_discovery_limit,
                "max_hot_issues_per_cycle": self.fetch_layer_max_hot_issues_per_cycle,
                "pr_scan_every_n_refreshes": self.fetch_layer_pr_scan_every_n_refreshes,
                "dependency_scan_every_n_refreshes": self.fetch_layer_dependency_scan_every_n_refreshes,
                "visibility_aware_enabled": self.fetch_layer_visibility_aware_enabled,
                "selective_sync_planner_enabled": self.fetch_layer_selective_sync_planner_enabled,
            }
        if self.instances != 1:
            ui_dict["instances"] = self.instances
        flow_refresh_dict: dict = {}
        if self.flow_refresh_enabled is not True:
            flow_refresh_dict["enabled"] = self.flow_refresh_enabled
        if self.flow_freshness_mode != "balanced":
            flow_refresh_dict["freshness_mode"] = self.flow_freshness_mode
        if self.flow_api_budget != "medium":
            flow_refresh_dict["api_budget"] = self.flow_api_budget
        if self.flow_attention_priority != "strict":
            flow_refresh_dict["attention_priority"] = self.flow_attention_priority
        if self.flow_refresh_stale_seconds != 900:
            flow_refresh_dict["stale_seconds"] = self.flow_refresh_stale_seconds
        if self.flow_refresh_cooldown_seconds != 120:
            flow_refresh_dict["cooldown_seconds"] = self.flow_refresh_cooldown_seconds
        if flow_refresh_dict:
            ui_dict["flow_refresh"] = flow_refresh_dict
        if ui_dict:
            result["ui"] = ui_dict

        # Observability section
        if observability_dict := observability_section(self):
            result["observability"] = observability_dict

        # Filtering section
        if filtering_dict := filtering_section(self):
            result["filtering"] = filtering_dict

        # Scheduling section
        if self.scheduling.default_priority_tier != 1:
            result["scheduling"] = {
                "default_priority_tier": self.scheduling.default_priority_tier,
            }

        # Review section
        review_dict: dict = {}
        if self.review_enabled:
            review_dict["enabled"] = True
        if self.code_review_agent:
            review_dict["default"] = self.code_review_agent
        if self.code_review_label:
            review_dict["code_review_label"] = self.code_review_label
        if self.code_reviewed_label:
            review_dict["code_reviewed_label"] = self.code_reviewed_label
        _put_if_truthy(review_dict, "tech_lead_review_agent", self.tech_lead_review_agent)
        _put_if_truthy(review_dict, "tech_lead_follow_up_agent", self.tech_lead_follow_up_agent)
        if self.tech_lead_review_label:
            review_dict["tech_lead_review_label"] = self.tech_lead_review_label
        if self.tech_lead_reviewed_label and self.tech_lead_reviewed_label != "tech-lead-reviewed":
            review_dict["tech_lead_reviewed_label"] = self.tech_lead_reviewed_label
        if self.tech_lead_review_threshold != 0:
            review_dict["tech_lead_review_threshold"] = self.tech_lead_review_threshold
        if self.max_rework_cycles != 5:
            review_dict["max_rework_cycles"] = self.max_rework_cycles
        if self.max_consecutive_publish_failures != 3:
            review_dict["max_consecutive_publish_failures"] = self.max_consecutive_publish_failures
        if self.max_consecutive_review_exchange_failures != 3:
            review_dict["max_consecutive_review_exchange_failures"] = self.max_consecutive_review_exchange_failures
        if self.review_keep_current_approach_label != "reviewer-keep-current-approach":
            review_dict["keep_current_approach_label"] = self.review_keep_current_approach_label
        if self.review_run_audit_min_runtime_minutes != 20:
            review_dict.setdefault("run_audit", {})["min_runtime_minutes"] = self.review_run_audit_min_runtime_minutes
        if self.review_run_audit_on_timeout is not True:
            review_dict.setdefault("run_audit", {})["on_timeout"] = self.review_run_audit_on_timeout
        if (
            self.retrospective_review_enabled
            or self.retrospective_review_trigger_label != "retrospective-review"
            or self.retrospective_reviewed_label != "retrospective-reviewed"
            or self.retrospective_changes_requested_label != "retrospective-changes-requested"
        ):
            review_dict["retrospective"] = {
                "enabled": self.retrospective_review_enabled,
                "trigger_label": self.retrospective_review_trigger_label,
                "reviewed_label": self.retrospective_reviewed_label,
                "changes_requested_label": self.retrospective_changes_requested_label,
            }
        if internal_review := serialized_internal_review_dict(self):
            review_dict["internal"] = internal_review
        if self.review_nits_default_policy != "surface" or self.review_nits_by_agent:
            nits_dict: dict[str, object] = {
                "default_policy": self.review_nits_default_policy,
            }
            if self.review_nits_by_agent:
                nits_dict["by_agent"] = dict(self.review_nits_by_agent)
            review_dict["nits"] = nits_dict
        exchange_dict = self._serialization_exchange_dict()
        if exchange_dict:
            review_dict["exchange"] = exchange_dict
        if review_dict:
            result["review"] = review_dict

        # Goal Pilot section
        if goal_pilot_dict := goal_pilot_section(self):
            result["goal_pilot"] = goal_pilot_dict

        # Merge queue section
        if merge_queue_dict := merge_queue_section(self):
            result["merge_queue"] = merge_queue_dict

        # Worktrees section
        if worktrees_dict := worktrees_section(self):
            result["worktrees"] = worktrees_dict

        # E2E section
        e2e_dict: dict = {}
        if self.e2e.enabled:
            e2e_dict["enabled"] = True
        if self.e2e.role != "auto":
            e2e_dict["role"] = self.e2e.role
        if self.e2e.auto_run_interval_minutes != 30:
            e2e_dict["auto_run_interval_minutes"] = self.e2e.auto_run_interval_minutes
        if self.e2e.occupies_session_slot:
            e2e_dict["occupies_session_slot"] = True
        if self.e2e.runner_kind != "pytest":
            e2e_dict["runner_kind"] = self.e2e.runner_kind
        if self.e2e.pytest_args != ["tests/e2e", "-v"]:
            e2e_dict["pytest_args"] = list(self.e2e.pytest_args)
        if self.e2e.command:
            e2e_dict["command"] = list(self.e2e.command)
        if self.e2e.junit_xml_paths:
            e2e_dict["junit_xml_paths"] = list(self.e2e.junit_xml_paths)
        if self.e2e.artifact_paths:
            e2e_dict["artifact_paths"] = list(self.e2e.artifact_paths)
        if not self.e2e.allow_retry_once:
            e2e_dict["allow_retry_once"] = False
        if self.e2e.quarantine_file != "tests/e2e/quarantine.txt":
            e2e_dict["quarantine_file"] = self.e2e.quarantine_file
        if self.e2e.stop_on_first_failure:
            e2e_dict["stop_on_first_failure"] = True
        if not self.e2e.auto_quarantine:
            e2e_dict["auto_quarantine"] = False
        if not self.e2e.auto_create_issues:
            e2e_dict["auto_create_issues"] = False
        if self.e2e.issue_agent_label != "agent:backend":
            e2e_dict["issue_agent_label"] = self.e2e.issue_agent_label
        if e2e_dict:
            result["e2e"] = e2e_dict

        # SQLite backup section
        if self.sqlite_backup != SqliteBackupConfig():
            result["sqlite_backup"] = {
                "enabled": self.sqlite_backup.enabled,
                "cadence_hours": self.sqlite_backup.cadence_hours,
                "check_interval_minutes": self.sqlite_backup.check_interval_minutes,
                "retention_daily": self.sqlite_backup.retention_daily,
                "retention_weekly": self.sqlite_backup.retention_weekly,
                "enforce_on_startup": self.sqlite_backup.enforce_on_startup,
            }

        # Timeline section
        if self.timeline != TimelineConfig():
            result["timeline"] = {
                "max_records": self.timeline.max_records,
            }

        # Validation section
        validation_dict: dict = {}
        quick_dict: dict = {}
        if self.validation.quick.cmd:
            quick_dict["cmd"] = self.validation.quick.cmd
            if self.validation.quick.timeout_seconds != 300:
                quick_dict["timeout_seconds"] = self.validation.quick.timeout_seconds
        if quick_dict:
            validation_dict["quick"] = quick_dict
        publish_dict: dict = {}
        if self.validation.publish.cmd:
            publish_dict["cmd"] = self.validation.publish.cmd
            if self.validation.publish.timeout_seconds != 1800:
                publish_dict["timeout_seconds"] = self.validation.publish.timeout_seconds
        if self.validation.publish.dirty_check != "tracked":
            publish_dict["dirty_check"] = self.validation.publish.dirty_check
        if publish_dict:
            validation_dict["publish"] = publish_dict
        if self.validation.coverage_guardrail != CoverageGuardrailConfig():
            validation_dict["coverage_guardrail"] = {
                "enabled": self.validation.coverage_guardrail.enabled,
                "min_percent": self.validation.coverage_guardrail.min_percent,
                "apply_to": self.validation.coverage_guardrail.apply_to,
                "scope": list(self.validation.coverage_guardrail.scope),
                "coverage_type": self.validation.coverage_guardrail.coverage_type,
                "exclude": list(self.validation.coverage_guardrail.exclude),
            }
        if self.validation.junit_xml_paths:
            validation_dict["junit_xml_paths"] = list(self.validation.junit_xml_paths)
        if validation_dict:
            result["validation"] = validation_dict

        # Retry section
        retry_dict: dict = {}
        if self.retry.max_validation_retries != 3:
            retry_dict["max_validation_retries"] = self.retry.max_validation_retries
        if self.retry.validation_error_file != "validation-errors.txt":
            retry_dict["validation_error_file"] = self.retry.validation_error_file
        if self.retry.retry_prompt_template:
            retry_dict["retry_prompt_template"] = self.retry.retry_prompt_template

        interrupted_dict: dict = {}
        interrupted = self.retry.interrupted_sessions
        if interrupted.enabled is not True:
            interrupted_dict["enabled"] = interrupted.enabled
        if interrupted.retry_coding is not True:
            interrupted_dict["retry_coding"] = interrupted.retry_coding
        if interrupted.retry_review is not True:
            interrupted_dict["retry_review"] = interrupted.retry_review
        if interrupted.coding_guard_label != "io:auto-retried-interrupted-coding":
            interrupted_dict["coding_guard_label"] = interrupted.coding_guard_label
        if interrupted.review_guard_label != "io:auto-retried-interrupted-review":
            interrupted_dict["review_guard_label"] = interrupted.review_guard_label
        if interrupted_dict:
            retry_dict["interrupted_sessions"] = interrupted_dict
        if retry_dict:
            result["retry"] = retry_dict

        # Security section
        security_dict: dict = {}
        if not self.enforce_hooks:
            security_dict["enforce_hooks"] = False
        if self.dangerous.allow_unsupported_agents:
            security_dict["dangerous"] = {"allow_unsupported_agents": True}
        if security_dict:
            result["security"] = security_dict

        # Hooks section (only include if non-default)
        hooks_dict: dict = {}
        if self.hooks.ai_gate.interval_days != 7:
            hooks_dict.setdefault("ai_gate", {})["interval_days"] = self.hooks.ai_gate.interval_days
        if self.hooks.ai_gate.dangerous_allow_failure:
            hooks_dict.setdefault("ai_gate", {})["dangerous_allow_failure"] = True
        if hooks_dict:
            result["hooks"] = hooks_dict
        result.update(self.explicit_tech_lead_section())
        return result

    def save(self, path: Optional[Path] = None) -> Path:
        """Save configuration to a YAML file.

        Args:
            path: Path to save to. If None, uses self.config_path.

        Returns:
            The path the config was saved to.

        Raises:
            ValueError: If no path is specified and config_path is not set.
        """
        save_path = path or self.config_path
        if save_path is None:
            raise ValueError("No path specified and config_path is not set")

        config_dict = self.to_dict()

        # Add header comment
        header = (
            "# Issue Orchestrator Configuration\n"
            "# Generated/modified by settings page\n"
            "# See docs/user/configuration.md for full documentation\n\n"
        )

        with open(save_path, "w") as f:
            f.write(header)
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        return save_path

    @classmethod
    def load(cls, config_path: Path, overrides: Optional[list[str]] = None) -> "Config":
        """Load configuration from YAML file."""
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path) as f:
            data = yaml.safe_load(f)
        if data is None:
            data = {}
        _apply_yaml_overrides(data, overrides or [])

        # Expand ${VAR} environment variable references
        data = _expand_env_vars(data)

        config = cls()
        config.config_path = config_path.resolve()
        config.launch_selection = selection_from_config_path(config.config_path)

        # Extract all sections with validation
        sections = extract_config_sections(data, config_path)
        repo_root = repo_root_from_config_path(config_path)

        # Set repo root
        config.repo_root = repo_root
        if sections["repo"].get("root"):
            config.repo_root = resolve_relative_path(sections["repo"]["root"], repo_root)
            config.repo_root_from_yaml = True

        # Store raw data for unknown field validation
        config.raw_data = data
        config.raw_agents = sections["agents"]
        if sections["state"].get("file"):
            config.state_file = resolve_relative_path(sections["state"]["file"], repo_root)

        # Parse default_agent section
        default_agent_section = data.get("default_agent", {})
        if default_agent_section:
            config.default_agent = parse_default_agent_config(default_agent_section)

        # Load all sections using helper functions
        load_worktrees_section(config, sections["worktrees"], repo_root, config_path)
        load_agents_section(config, sections["agents"], repo_root)
        load_execution_section(config, sections["execution"], config_path)
        load_labels_section(config, sections["labels"])
        load_repo_section(config, sections["repo"], sections["github"])
        load_github_write_verify(config, sections["github"])
        load_ui_section(config, sections["ui"])
        load_observability_section(config, sections["observability"])
        load_security_section(config, sections["security"], repo_root)
        load_review_section(config, sections["review"])
        load_cleanup_section(config, sections["cleanup"])
        load_validation_section(config, sections["validation"])
        load_retry_section(config, sections["retry"])

        # Simple direct assignments
        config.e2e_pr_labels = sections["e2e"].get("pr_labels", [])
        config.filtering = parse_filtering_config(sections["filtering"])
        config.milestone_sort = sections["milestones"].get("sort", "milestone_number")
        config.milestone_sort_config = sections["milestones"].get("sort_config", {})
        config.milestone_order = parse_milestone_order(sections["milestones"].get("order", []))
        config.foundation_milestone = sections["milestones"].get("foundation", "M0")
        config.ai_systems_allowed = parse_ai_systems_allowed(
            sections["ai_systems"].get("allowed", [])
        )
        # Parse complex optional configs
        apply_optional_sections(config, sections)
        config.refresh_config_fingerprint()
        return config

    @classmethod
    def find_and_load(
        cls,
        start_path: Optional[Path] = None,
        config_name: str = DEFAULT_CONFIG_NAME,
        mode: str = "default",
        overrides: Optional[list[str]] = None,
    ) -> "Config":
        """Find config file in current or parent directories and load it.

        Args:
            start_path: Starting path for search (defaults to cwd)
            config_name: Name of config file to load (default: default.yaml)
            overrides: CLI overrides in path=value format
        """
        config_file = find_config_file(start_path, config_name, mode)
        if not config_file:
            raise FileNotFoundError(
                f"No config found in {CONFIG_DIR}/ directory. "
                "Run the setup wizard to create a configuration."
            )

        config = cls.load(config_file, overrides=overrides)
        # Set repo_root using centralized helper if not already set from YAML
        if not config.repo_root_from_yaml:
            config.repo_root = repo_root_from_config_path(config_file)
        return config

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors.

        Call this at startup to catch configuration problems early.
        Returns empty list if valid, list of error messages otherwise.

        Uses modular validators from the validators subpackage for cleaner
        separation of concerns and easier testing.
        """
        from .validators import (
            AgentValidator,
            GoalPilotValidator,
            MilestonesValidator,
            IsolationValidator,
            ReviewWorkflowValidator,
            TemplateValidator,
            UnknownFieldsValidator,
            WorktreeValidator,
        )

        errors: list[str] = []

        # Run all validators
        errors.extend(WorktreeValidator().validate(self))
        errors.extend(AgentValidator().validate(self))
        errors.extend(ReviewWorkflowValidator().validate(self))
        errors.extend(GoalPilotValidator().validate(self))
        errors.extend(MilestonesValidator().validate(self))
        errors.extend(IsolationValidator().validate(self))
        errors.extend(TemplateValidator().validate(self))
        errors.extend(UnknownFieldsValidator().validate(self))

        if not (0 <= self.scheduling.default_priority_tier <= 9):
            errors.append("scheduling.default_priority_tier must be between 0 and 9")

        if self.tech_lead.priority is not None and not re.fullmatch(r"P\d", self.tech_lead.priority.strip()):
            errors.append("tech_lead.priority must be a tier like 'P0'..'P9'")
        if self.tech_lead.max_concurrent is not None and self.tech_lead.max_concurrent < 1:
            errors.append(
                "tech_lead.max_concurrent must be >= 1 when set (a reserved tech_lead "
                "budget of at least one slot); omit it to share the worker "
                f"budget, got {self.tech_lead.max_concurrent}"
            )
        # Same ranges the settings schema advertises. Raw YAML bypasses that
        # schema entirely, and the circuit owner no longer clamps: an
        # out-of-range threshold or a zero/negative cooldown would silently
        # produce an already-expired auth deadline and stop protecting the
        # fleet, which is the burn this whole feature exists to prevent
        # (#6999 F7).
        circuit = self.provider_resilience.circuit_breaker
        if not (1 <= circuit.auth_failure_threshold <= 10):
            errors.append(
                "provider_resilience.circuit_breaker.auth_failure_threshold must be "
                f"between 1 and 10, got {circuit.auth_failure_threshold}"
            )
        if not (60 <= circuit.auth_cooldown_seconds <= 604800):
            errors.append(
                "provider_resilience.circuit_breaker.auth_cooldown_seconds must be "
                f"between 60 and 604800, got {circuit.auth_cooldown_seconds}"
            )
        if self.validation.publish.dirty_check not in {"tracked", "unstaged", "all", "off"}:
            errors.append(
                "validation.publish.dirty_check must be one of: tracked, unstaged, all, off"
            )
        if (
            self.review_enabled
            and self.review_exchange_require_validation
            and not self.validation.quick.cmd
        ):
            errors.append(
                "validation.quick.cmd must be configured when review.exchange.loop.require_validation is true"
            )

        return errors

    def validate_unknown_fields(self) -> list[tuple[str, str]]:
        """Check for unknown fields in the raw YAML data.

        Returns list of (field_path, level) tuples where:
        - field_path is like "repo.root" or "agents.agent:web.some_field"
        - level is the nearest section that owns the path
        """
        from .validators.unknown_fields import UnknownFieldsValidator

        return UnknownFieldsValidator().find_unknown_fields(self)

    def validate_template_variables(self) -> list[tuple[str, str, set[str]]]:
        """Check for invalid template variables in initial_prompt and command.

        Returns list of (agent_label, field_name, invalid_vars) tuples.
        """
        import re

        # Valid variables for initial_prompt (before command rendering)
        VALID_INITIAL_PROMPT_VARS = {
            "issue_number",
            "issue_title",
            "prompt",
            "worktree",
            "model",
            "permission_mode",
            "claude_args",
            "pr_number",  # Only valid for review agents, but we allow it here
        }

        # Valid variables for command (after initial_prompt is rendered)
        # system_prompt includes completion command instructions, built by get_command()
        VALID_COMMAND_VARS = VALID_INITIAL_PROMPT_VARS | {"initial_prompt", "system_prompt"}

        # Regex to find {variable_name} patterns (excluding {{ escaped braces }})
        VAR_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

        invalid = []

        for label, agent in self.agents.items():
            # Check initial_prompt
            found_vars = set(VAR_PATTERN.findall(agent.initial_prompt))
            bad_vars = found_vars - VALID_INITIAL_PROMPT_VARS
            if bad_vars:
                invalid.append((label, "initial_prompt", bad_vars))

            # Check command
            found_vars = set(VAR_PATTERN.findall(agent.command))
            bad_vars = found_vars - VALID_COMMAND_VARS
            if bad_vars:
                invalid.append((label, "command", bad_vars))

        return invalid

    def validate_or_raise(self) -> None:
        """Validate configuration, raising ValueError if invalid."""
        errors = self.validate()
        if errors:
            raise ValueError(
                "Configuration errors:\n  - " + "\n  - ".join(errors)
            )

def _apply_yaml_overrides(data: dict, overrides: list[str]) -> None:
    """Apply CLI overrides (path=value) to YAML data in-place."""
    for override in overrides:
        if "=" not in override:
            raise ValueError(f"Invalid override (expected key=value): {override}")
        path, raw_value = override.split("=", 1)
        if not path:
            raise ValueError(f"Invalid override (empty key): {override}")
        try:
            value = yaml.safe_load(raw_value)
        except Exception:
            value = raw_value
        cursor = data
        keys = path.split(".")
        for key in keys[:-1]:
            if key not in cursor or not isinstance(cursor[key], dict):
                cursor[key] = {}
            cursor = cursor[key]
        cursor[keys[-1]] = value
