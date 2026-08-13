"""Configuration sub-model dataclasses."""

from dataclasses import dataclass, field
from typing import Optional

# Tech-lead sub-models live in their own module for cohesion and line budget
# (mirroring the ``config_sections_tech_lead`` parsing split). Re-exported here
# so every existing ``from .config_models import TechLeadConfig`` keeps working.
from .config_models_tech_lead import (
    TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS as TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS,
    TECH_LEAD_AUTHORITY_MODES as TECH_LEAD_AUTHORITY_MODES,
    TECH_LEAD_MAX_EXPEDITED_LIMIT as TECH_LEAD_MAX_EXPEDITED_LIMIT,
    MilestoneStrategyConfig as MilestoneStrategyConfig,
    PromotionRouteTarget as PromotionRouteTarget,
    StuckSweepConfig as StuckSweepConfig,
    TechLeadActivationOwner as TechLeadActivationOwner,
    TechLeadAuthorityConfig as TechLeadAuthorityConfig,
    TechLeadConfig as TechLeadConfig,
    TechLeadDedupConfig as TechLeadDedupConfig,
    TechLeadFindingsConfig as TechLeadFindingsConfig,
    TechLeadHealthReviewConfig as TechLeadHealthReviewConfig,
)


@dataclass
class CleanupWithTechLead:
    """Cleanup settings when tech_lead review is enabled."""

    close_ai_session_tabs: bool = True
    remove_worktrees: bool = True


@dataclass
class CleanupWithoutTechLead:
    """Cleanup settings when tech_lead review is NOT enabled."""

    wait_for_code_review: bool = True  # True = after code review, False = on completion
    close_ai_session_tabs: bool = True
    remove_worktrees: bool = True


@dataclass
class CleanupConfig:
    """Cleanup configuration - when to close tabs and remove worktrees."""

    with_tech_lead: CleanupWithTechLead = field(default_factory=CleanupWithTechLead)
    without_tech_lead: CleanupWithoutTechLead = field(default_factory=CleanupWithoutTechLead)


@dataclass
class DangerousConfig:
    """Dangerous configuration options that bypass safety guardrails.

    These options should only be used for testing or in environments
    where you understand the risks.
    """

    allow_unsupported_agents: bool = False  # Allow agents without hook support


@dataclass
class AiGateConfig:
    """AI gate configuration for periodic hook enforcement testing.

    Exercises AI-level hooks/execpolicy to verify blocking works.
    """

    interval_days: int = 7  # Run AI gate test every N days (0 = disabled)
    dangerous_allow_failure: bool = False  # If True, warn only; if False, block on failure


@dataclass
class HooksConfig:
    """Hook management configuration."""

    ai_gate: AiGateConfig = field(default_factory=AiGateConfig)


@dataclass
class CoverageGuardrailConfig:
    """Per-file coverage guardrail for files touched in a change."""

    enabled: bool = False
    min_percent: Optional[float] = None
    apply_to: str = "changed"  # "changed" or "all"
    scope: list[str] = field(default_factory=list)
    coverage_type: str = "line"  # "line" or "branch"
    exclude: list[str] = field(default_factory=list)


@dataclass
class ValidationCommandConfig:
    """A single configured validation command."""

    cmd: Optional[str] = None
    timeout_seconds: int = 300


@dataclass
class PublishValidationConfig(ValidationCommandConfig):
    """Authoritative validation used before publishing code."""

    timeout_seconds: int = 1800
    dirty_check: str = "tracked"  # "tracked" | "unstaged" | "all" | "off"


@dataclass
class ValidationConfig:
    """Validation configuration split by lifecycle cost.

    ``quick`` runs while the coding agent still owns the session and during
    coder/reviewer exchanges. ``publish`` is the deeper pre-push/pre-publish
    gate.
    """

    quick: ValidationCommandConfig = field(default_factory=ValidationCommandConfig)
    publish: PublishValidationConfig = field(default_factory=PublishValidationConfig)
    coverage_guardrail: CoverageGuardrailConfig = field(default_factory=CoverageGuardrailConfig)
    # JUnit XML output paths (relative to worktree, glob-supported) emitted by
    # the validation command. When set, the dashboard renders a structured
    # test-results view for failing validations instead of just a list of
    # test names. Empty by default — keeps current behavior unchanged.
    junit_xml_paths: tuple[str, ...] = ()


@dataclass
class RetryConfig:
    """Validation retry configuration.

    When an agent completes but validation fails, the orchestrator can
    retry with error context injected into the prompt.
    """

    max_validation_retries: int = 3  # Max times to retry after validation failure
    validation_error_file: str = "validation-errors.txt"  # Filename in session output dir
    # Default retry prompt template path (relative to repo root).
    # Agents can override this with their own retry_prompt_template.
    # If None, uses built-in default template.
    retry_prompt_template: Optional[str] = None
    interrupted_sessions: "InterruptedSessionRetryConfig" = field(
        default_factory=lambda: InterruptedSessionRetryConfig()
    )


@dataclass
class InterruptedSessionRetryConfig:
    """Auto-retry settings for sessions that exit without completion."""

    enabled: bool = True
    retry_coding: bool = True
    retry_review: bool = True
    coding_guard_label: str = "io:auto-retried-interrupted-coding"
    review_guard_label: str = "io:auto-retried-interrupted-review"


@dataclass
class ProviderShortRetryConfig:
    """Short retry settings for provider resilience."""

    max_attempts: int = 4
    initial_backoff_seconds: int = 5
    max_backoff_seconds: int = 60
    jitter: bool = True


@dataclass
class ProviderCircuitBreakerConfig:
    """Circuit breaker settings for provider resilience."""

    cooldown_seconds: int = 1800
    max_cooldowns: int = 6
    label: str = "blocked:provider-unavailable"
    # Auth failures trip on their own threshold. The default is 1 because the
    # credential probe reads local state and is deterministic: one confirmed
    # "not logged in" is evidence, not a blip. Raise it only if a provider's
    # probe proves flaky.
    auth_failure_threshold: int = 1
    # An expired login is human-fixable, so the auth cooldown is long: retrying
    # on the transient cooldown would just re-burn the fleet until a human
    # notices. Recovery does not wait it out, and does not need a session to
    # succeed either — while the circuit is open nothing launches, so the
    # credential probe itself is what observes the human re-authenticating and
    # clears the circuit before the next launch.
    auth_cooldown_seconds: int = 21600


@dataclass
class ProviderResilienceConfig:
    """Provider resilience configuration."""

    short_retry: ProviderShortRetryConfig = field(default_factory=ProviderShortRetryConfig)
    circuit_breaker: ProviderCircuitBreakerConfig = field(default_factory=ProviderCircuitBreakerConfig)


@dataclass
class DefaultAgentConfig:
    """Default agent configuration inherited by all agents.

    When agents don't specify their own provider/model/args, they inherit
    from default_agent. If an agent has no provider and no default is set,
    config validation fails fast.

    Example YAML:
        default_agent:
          provider: claude-code
          model: sonnet
          provider_args:
            permission_mode: bypassPermissions
    """

    provider: Optional[str] = None  # "claude-code", "codex", etc.
    model: Optional[str] = None  # Model to use (sonnet, gpt-5.3-codex, etc.)
    provider_args: dict = field(default_factory=dict)  # Provider-specific args


@dataclass
class IsolationConfig:
    """Agent isolation configuration."""

    mode: str = "standard"  # "standard" or "hardened"


@dataclass
class SessionInteractionsConfig:
    """Guardrails for orchestrator-driven session interactions."""

    enabled: bool = False


@dataclass
class FilteringConfig:
    """Issue filtering configuration.

    Controls which issues the orchestrator will process.
    """

    label: Optional[str] = None  # Only process issues with this label
    milestone: Optional[str] = None  # Only process issues in this milestone
    milestones: list[str] = field(default_factory=list)  # Process issues in any of these milestones
    issue: Optional[int] = None  # Only process this specific issue number
    exclude_labels: list[str] = field(default_factory=list)  # Exclude issues with any of these labels
    exclude_label_prefixes: list[str] = field(default_factory=list)  # Exclude labels matching these prefixes
    fetch_limit: int = 100  # Max issues to fetch per API call
    max_to_start: int = 0  # Stop after starting this many issues (0 = unlimited)

    def get_milestones(self) -> list[str]:
        """Return list of milestone filters (supports both single and list)."""
        if self.milestones:
            return list(self.milestones)
        if self.milestone:
            return [self.milestone]
        return []



@dataclass
class SchedulingConfig:
    """Scheduling configuration."""

    default_priority_tier: int = 1  # P1 (medium) when no [P?-nnn] prefix


@dataclass(frozen=True)
class E2EExecutionSpec:
    """Normalized execution spec for a single E2E run."""

    runner_kind: str
    pytest_args: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    junit_xml_paths: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()
    allow_retry_once: bool = True
    stop_on_first_failure: bool = False

    @property
    def canonical_command(self) -> tuple[str, ...]:
        if self.runner_kind == "pytest":
            return ("pytest", *self.pytest_args)
        return self.command

    @property
    def display_target(self) -> str:
        if self.runner_kind == "pytest":
            return self.pytest_args[0] if self.pytest_args else "pytest"
        return self.command[0] if self.command else "command"


@dataclass
class E2EConfig:
    """E2E async test runner settings.

    Controls local async E2E test execution with results persisted to SQLite.

    Role determines whether this orchestrator runs E2E tests:
    - "auto": Determine role automatically (default)
    - "executor": Run tests and publish results
    - "reader": Only display results (don't run tests)
    - "disabled": E2E functionality completely off

    In "auto" mode:
    - orchestrator-1 (or single instance) is executor
    - Other instances become readers

    For multi-machine setups, use explicit role with env var:
        role: ${E2E_ROLE}  # Set E2E_ROLE=executor on designated machine
    """

    enabled: bool = False  # Whether E2E runner is active
    role: str = "auto"  # auto | executor | reader | disabled
    auto_run_interval_minutes: int = 30  # Min interval between auto runs (0 = disable auto)
    # Make an E2E run a first-class workload in the concurrency budget. None of
    # today's parallel behavior changes while this is False (the default): E2E
    # triggers alongside agents. When True, an E2E run counts against the WORKER
    # budget (``max_concurrent_sessions``) — NOT the reserved tech_lead slot: it
    # starts only when a worker slot is free, and while it runs it occupies one
    # slot so the planner launches one fewer agent (they interleave over time).
    # A due suite claims a slot AHEAD of new issues but BEHIND in-flight
    # completion work (reviews/reworks/validation-retries/tech_lead), which are
    # never preempted. Leave off unless the machine is resource-constrained
    # enough that a second full orchestrator workload starves live agents.
    occupies_session_slot: bool = False
    runner_kind: str = "pytest"  # pytest | command
    pytest_args: list[str] = field(default_factory=lambda: ["tests/e2e", "-v"])
    command: list[str] = field(default_factory=list)  # Generic command when runner_kind=command
    junit_xml_paths: list[str] = field(default_factory=list)  # Relative file paths or globs
    artifact_paths: list[str] = field(default_factory=list)  # Additional artifact paths or globs
    allow_retry_once: bool = True  # Retry failing tests once to reduce flakiness
    quarantine_file: str = "tests/e2e/quarantine.txt"  # Path to quarantine list
    survive_restart: bool = True  # Let worker finish if orchestrator restarts
    stop_on_first_failure: bool = False  # If True, stop on first test failure (-x flag)
    auto_quarantine: bool = True  # Auto-add failing tests to quarantine list
    auto_create_issues: bool = True  # Auto-create GitHub issues for failures
    issue_agent_label: str = "agent:backend"  # Agent label for failure issues
    flake_threshold: int = 20  # Flip rate percentage (0-100) to flag test as flaky
    flake_window_runs: int = 10  # Number of recent runs to check for flakiness
    run_retention_count: int = 50  # Max runs to keep; older runs are pruned on completion

    def execution_spec(self) -> E2EExecutionSpec:
        """Return the normalized execution spec for a run."""
        if self.runner_kind == "pytest":
            return E2EExecutionSpec(
                runner_kind="pytest",
                pytest_args=tuple(self.pytest_args),
                junit_xml_paths=tuple(self.junit_xml_paths),
                artifact_paths=tuple(self.artifact_paths),
                allow_retry_once=self.allow_retry_once,
                stop_on_first_failure=self.stop_on_first_failure,
            )
        if self.runner_kind == "command":
            if not self.command:
                raise ValueError("e2e.command must be configured when runner_kind=command")
            return E2EExecutionSpec(
                runner_kind="command",
                command=tuple(self.command),
                junit_xml_paths=tuple(self.junit_xml_paths),
                artifact_paths=tuple(self.artifact_paths),
                allow_retry_once=self.allow_retry_once,
                stop_on_first_failure=self.stop_on_first_failure,
            )
        raise ValueError(f"Unsupported e2e.runner_kind: {self.runner_kind}")


@dataclass
class SqliteBackupConfig:
    """SQLite backup configuration."""

    enabled: bool = True
    cadence_hours: int = 24  # Minimum hours between backups
    check_interval_minutes: int = 60  # How often to check for due backups
    retention_daily: int = 14  # Number of daily backups to keep
    retention_weekly: int = 8  # Number of weekly backups to keep
    enforce_on_startup: bool = True  # Force backup on startup if cadence elapsed


@dataclass
class TimelineConfig:
    """Timeline retention configuration."""

    max_records: int = 5000


@dataclass
class ClaimsConfig:
    """Claims/lease configuration for multi-orchestrator coordination.

    When enabled, orchestrators coordinate through the configured repository
    host to ensure only one orchestrator works on each issue at a time.

    Single-orchestrator deployments should leave this disabled (the default).
    """

    enabled: bool = False  # Whether claims system is active
    claimant_id: Optional[str] = None  # Unique ID for this orchestrator instance

    # Lease timing (seconds)
    lease_seconds: int = 900  # 15 min default lease duration
    renew_before_expiry_seconds: int = 300  # Renew when 5 min remaining

    # Convergence protocol settings
    convergence_timeout_seconds: float = 5.0  # Max time to wait for convergence
    convergence_poll_min_ms: int = 250  # Min poll interval (randomized)
    convergence_poll_max_ms: int = 500  # Max poll interval (randomized)


@dataclass
class GoalPilotConfig:
    """Configuration for Goal Pilot AI."""

    enabled: bool = False
    agent: Optional[str] = None
    approval_policy: str = "journeys_only"  # journeys_only | gatekeeper | batch
    approval_batch_size: int = 10
    approval_batch_window_minutes: int = 60


# Allowed values for the merge_queue section, validated at parse time so a typo
# fails loud at config load rather than silently picking a wrong policy branch.
MERGE_QUEUE_PROVIDERS = ("github",)
MERGE_QUEUE_GATES = ("code-reviewed", "tech-lead-reviewed")
MERGE_QUEUE_FAILURE_ACTIONS = ("rework", "needs_human")


@dataclass
class MergeQueueConfig:
    """Optional per-repository GitHub Merge Queue integration.

    When ``enabled``, approved PRs that have cleared the orchestrator gate are
    enqueued into the provider's native merge queue instead of being merged or
    reworked for being behind base. GitHub remains the merge authority; the
    orchestrator owns eligibility, enqueue decisions, and failure routing.
    """

    enabled: bool = False
    provider: str = "github"  # see MERGE_QUEUE_PROVIDERS
    enqueue_after: str = "code-reviewed"  # orchestrator gate; see MERGE_QUEUE_GATES
    failure_action: str = "rework"  # rework | needs_human; see MERGE_QUEUE_FAILURE_ACTIONS
