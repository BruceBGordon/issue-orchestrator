"""Configuration sub-model dataclasses."""

from dataclasses import dataclass, field
from typing import Optional

from ..domain.tech_lead_artifacts import UNWIRED_ACT_LEVEL_TECH_LEAD_ACTIONS


@dataclass
class CleanupWithTechLead:
    """Cleanup settings when tech_lead review is enabled."""

    close_ai_session_tabs: bool = True
    remove_worktrees: bool = False


@dataclass
class CleanupWithoutTechLead:
    """Cleanup settings when tech_lead review is NOT enabled."""

    wait_for_code_review: bool = True  # True = after code review, False = on completion
    close_ai_session_tabs: bool = True
    remove_worktrees: bool = False


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
class MilestoneStrategyConfig:
    """Milestone assignment strategy for tech_lead issues."""

    inherit_from_issues: Optional[str] = "latest"  # "earliest" | "latest" | None
    explicit: Optional[str] = None  # Explicit milestone name


TECH_LEAD_AUTHORITY_MODES = ("execute", "propose")

# Action types whose authority mode is configurable. escalate_to_human is
# deliberately absent: it is the non-configurable floor and always executes.
TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS = (
    "post_comment",
    "create_issue",
    "flag_pattern",
    "reset_retry",
    "kill_hung_session",
)


@dataclass
class TechLeadAuthorityConfig:
    """Per-action-type authority modes for tech_lead decision proposals (ADR-0031).

    ``execute`` — the orchestrator performs the proposed action directly.
    ``propose`` — for ``post_comment``/``flag_pattern``: shadow mode (the
    proposal is surfaced as would-have-done). For ``create_issue`` and
    act-level actions: a GATED ISSUE (#6778) — the proposal is created as a
    GitHub issue carrying ``proposed-tech-lead``; removing that label is
    per-instance operator approval. Per-instance approval and config-level
    trust coexist.

    ``escalate_to_human`` is intentionally not a field: it is the
    non-configurable floor and always executes. Act-level actions
    (``reset_retry``, ``kill_hung_session``) default to ``propose``.
    ``reset_retry: execute`` is honored — it is wired to the
    reset+retry-from-scratch owner with execution-time re-validation
    (#6764, first slice). ``kill_hung_session: execute`` remains a startup
    error: its DIRECT tier is not wired yet — it ships as gated proposal
    issues (#6778) — see ``Config.validate``.
    """

    post_comment: str = "execute"
    create_issue: str = "execute"
    flag_pattern: str = "execute"
    reset_retry: str = "propose"
    kill_hung_session: str = "propose"

    @classmethod
    def from_mapping(cls, data: dict) -> "TechLeadAuthorityConfig":
        """Parse the ``tech_lead.authority`` YAML section, validating modes."""
        defaults = cls()
        values: dict[str, str] = {}
        for key in TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS:
            value = data.get(key, getattr(defaults, key))
            if value not in TECH_LEAD_AUTHORITY_MODES:
                raise ValueError(
                    f"tech_lead.authority.{key} must be one of"
                    f" {list(TECH_LEAD_AUTHORITY_MODES)}, got {value!r}"
                )
            values[key] = value
        return cls(**values)

    def mode_for(self, action_type: str) -> str:
        """Return the authority mode for a proposed tech_lead action type.

        ``escalate_to_human`` ALWAYS returns ``execute`` — routing to a
        human is the fail-safe floor and cannot be configured away.
        Unknown action types raise: authority for an unrecognized action
        must never be silently guessed.
        """
        if action_type == "escalate_to_human":
            return "execute"
        if action_type not in TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS:
            raise ValueError(f"unknown tech_lead action type: {action_type!r}")
        return getattr(self, action_type)

    def to_event_dict(self) -> dict:
        """All five graduated-authority modes, for config event payloads."""
        return {
            key: getattr(self, key) for key in TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS
        }

    def startup_errors(self) -> list[str]:
        """Startup configuration errors for this authority block (ADR-0031).

        ``execute`` on an act-level action whose DIRECT executor is not
        wired yet must be a startup configuration error, never a silent
        no-op (#6764). ``reset_retry`` is wired and no longer rejected; the
        unwired set lives in ``UNWIRED_ACT_LEVEL_TECH_LEAD_ACTIONS``. The
        rejection is deliberate even though ``kill_hung_session`` ships as
        GATED PROPOSAL ISSUES under ``propose`` (#6778): the gated tier is
        the point — per-instance approval, not config-level trust.
        """
        errors: list[str] = []
        for key in TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS:
            mode = getattr(self, key)
            if mode not in TECH_LEAD_AUTHORITY_MODES:
                errors.append(
                    f"tech_lead.authority.{key} must be one of"
                    f" {list(TECH_LEAD_AUTHORITY_MODES)}, got {mode!r}"
                )
        for key in sorted(UNWIRED_ACT_LEVEL_TECH_LEAD_ACTIONS):
            if getattr(self, key) == "execute":
                errors.append(
                    f"tech_lead.authority.{key}: direct 'execute' is not wired"
                    " yet (#6764); use 'propose' — proposals surface as"
                    " gated issues awaiting per-instance approval (#6778)"
                )
        return errors


@dataclass
class TechLeadDedupConfig:
    """Trusted open-issue deduplication settings for ``create_issue`` proposals."""

    enabled: bool = True
    similarity_threshold: float = 0.72

    @classmethod
    def from_mapping(cls, data: dict) -> "TechLeadDedupConfig":
        return cls(
            enabled=bool(data.get("enabled", True)),
            similarity_threshold=float(data.get("similarity_threshold", 0.72)),
        )

    def startup_errors(self) -> list[str]:
        if not 0.0 < self.similarity_threshold <= 1.0:
            return [
                "tech_lead.dedup.similarity_threshold must be > 0.0 and <= 1.0, "
                f"got {self.similarity_threshold}"
            ]
        return []


@dataclass
class TechLeadHealthReviewConfig:
    """Periodic and problem-storm health-review trigger settings (ADR-0031).

    ``interval_minutes`` drives the planner-side trigger: every N minutes
    the orchestrator creates a health-review anchor issue for the tech_lead
    agent to walk the board snapshot. 0 (the default) disables the trigger.

    ``storm_threshold`` is the number of recent blocked/failed problem issues
    that replaces per-issue investigations with one unscheduled health review;
    0 disables storm escalation. ``storm_window_minutes`` defines "recent".
    """

    interval_minutes: int = 0
    storm_threshold: int = 3
    storm_window_minutes: int = 5

    @classmethod
    def from_mapping(cls, data: dict) -> "TechLeadHealthReviewConfig":
        """Parse the ``tech_lead.health_review`` YAML sub-dict."""
        return cls(
            interval_minutes=int(data.get("interval_minutes", 0)),
            storm_threshold=int(data.get("storm_threshold", 3)),
            storm_window_minutes=int(data.get("storm_window_minutes", 5)),
        )

    def startup_errors(self) -> list[str]:
        """Startup configuration errors for the health-review block.

        The documented disable value is exactly 0; a negative interval is a
        misconfiguration that must fail startup loudly, never be silently
        treated as disabled (#6763 finding 8).
        """
        errors: list[str] = []
        if self.interval_minutes < 0:
            errors.append(
                "tech_lead.health_review.interval_minutes must be >= 0 "
                f"(0 disables the trigger), got {self.interval_minutes}"
            )
        if self.storm_threshold < 0:
            errors.append(
                "tech_lead.health_review.storm_threshold must be >= 0 "
                f"(0 disables storm escalation), got {self.storm_threshold}"
            )
        if self.storm_window_minutes <= 0:
            errors.append(
                "tech_lead.health_review.storm_window_minutes must be > 0, got "
                f"{self.storm_window_minutes}"
            )
        return errors


@dataclass
class StuckSweepConfig:
    """Tech-lead attention sweep trigger settings (ADR-0031, #6823).

    A bounded, timer-gated backstop that re-injects open issues stuck in a
    terminal blocking state (that the normal loop cannot re-discover) into the
    reactive-tech-lead pipeline. ``interval_minutes`` is the cadence;
    ``max_recovery_attempts`` bounds re-injection per issue before the sweep
    surfaces it as exhausted (needs human attention) instead of looping.
    ``enabled`` is False (off) by default. The default cadence is 4h: the sweep
    is a reconcile-for-strays BACKSTOP, not a hot path, and its scan is a broad
    exhaustive open-issue read — a slow interval keeps that off the frequent
    paths (stranded issues are quiescent, so a few hours of recovery latency is
    fine; lower it when faster reclamation is worth the extra scans).
    """

    enabled: bool = False
    interval_minutes: int = 240
    max_recovery_attempts: int = 3

    @classmethod
    def from_mapping(cls, data: dict) -> "StuckSweepConfig":
        """Parse the ``tech_lead.stuck_sweep`` YAML sub-dict."""
        return cls(
            enabled=bool(data.get("enabled", False)),
            interval_minutes=int(data.get("interval_minutes", 240)),
            max_recovery_attempts=int(data.get("max_recovery_attempts", 3)),
        )

    def startup_errors(self) -> list[str]:
        """Own-block invariants; the enabled-requires-tech-lead-agent cross-field
        check lives in the review validator (it reads other config sections)."""
        errors: list[str] = []
        if self.interval_minutes < 1:
            errors.append(
                "tech_lead.stuck_sweep.interval_minutes must be >= 1 — a zero (or "
                "negative) interval makes stuck_sweep_due true every tick, i.e. an "
                "unthrottled GitHub scan on every loop, which #6823 forbids; a "
                "cadence of 0 is meaningless, so set enabled: false to turn the "
                f"sweep off instead. Got {self.interval_minutes}"
            )
        if self.max_recovery_attempts < 1:
            errors.append(
                "tech_lead.stuck_sweep.max_recovery_attempts must be >= 1 "
                f"(bounds re-injection before escalation), got "
                f"{self.max_recovery_attempts}"
            )
        return errors


# Upper bound on the expedite-lane cap (#6870). The single source of truth for
# BOTH the runtime config validation (TechLeadConfig.startup_errors) and the
# settings-form schema (settings_schema le=...), so the two layers can never
# accept/reject a value inconsistently.
TECH_LEAD_MAX_EXPEDITED_LIMIT = 20


@dataclass
class TechLeadConfig:
    """Tech Lead issue configuration.

    Controls how labels and milestones are assigned to orchestrator-created
    tech_lead issues, which tech_lead decision proposals the orchestrator
    executes versus surfaces (ADR-0031), and the periodic health-review
    trigger (ADR-0031 §4).
    """

    # Labels to inherit from source issues (if any source issue has the label)
    inherit_labels: list[str] = field(default_factory=list)

    # Labels always applied to tech_lead issues
    explicit_labels: list[str] = field(default_factory=list)

    # Milestone assignment strategy
    milestone_strategy: MilestoneStrategyConfig = field(default_factory=MilestoneStrategyConfig)

    # Optional explicit priority label
    priority: Optional[str] = None

    # Reserved concurrency for tech_lead sessions. None (the default) = tech_lead
    # shares the worker budget (``max_concurrent_sessions``): tech_lead counts
    # against it and is planned from the shared capacity, exactly as before.
    # An int = a SEPARATE additive tech_lead budget: tech_lead sessions run from
    # their own ``tech_lead.max_concurrent`` slots and are NOT subtracted from
    # the worker ``max_concurrent_sessions``, so the tech lead can run even
    # when the worker budget is saturated. Total live agents are then bounded
    # at ``max_concurrent_sessions + tech_lead.max_concurrent``.
    max_concurrent: Optional[int] = None

    # Expedite lane cap (#6870). Bounds how many OUTSTANDING tech-lead-expedited
    # issues can sit at the front of the worker queue at once, so a noisy tech
    # lead cannot starve normal work. The default is small; 0 disables the lane
    # entirely (an expedite request then falls back to normal priority).
    max_expedited: int = 3

    # Per-action-type graduated authority for tech_lead decision proposals
    authority: TechLeadAuthorityConfig = field(default_factory=TechLeadAuthorityConfig)

    # Trusted open-issue corpus and lexical backstop for create_issue proposals
    dedup: TechLeadDedupConfig = field(default_factory=TechLeadDedupConfig)

    # Periodic health-review trigger (ADR-0031 §4)
    health_review: TechLeadHealthReviewConfig = field(default_factory=TechLeadHealthReviewConfig)

    # Tech-lead attention sweep for stuck issues (ADR-0031, #6823)
    stuck_sweep: StuckSweepConfig = field(default_factory=StuckSweepConfig)

    def to_event_dict(self) -> dict:
        """Serialized ``tech_lead`` section for config event payloads."""
        return {
            "inherit_labels": list(self.inherit_labels),
            "explicit_labels": list(self.explicit_labels),
            "milestone_strategy": {
                "inherit_from_issues": self.milestone_strategy.inherit_from_issues,
                "explicit": self.milestone_strategy.explicit,
            },
            "priority": self.priority,
            "max_concurrent": self.max_concurrent,
            "max_expedited": self.max_expedited,
            "authority": self.authority.to_event_dict(),
            "dedup": {
                "enabled": self.dedup.enabled,
                "similarity_threshold": self.dedup.similarity_threshold,
            },
            "health_review": {
                "interval_minutes": self.health_review.interval_minutes,
                "storm_threshold": self.health_review.storm_threshold,
                "storm_window_minutes": self.health_review.storm_window_minutes,
            },
            "stuck_sweep": {
                "enabled": self.stuck_sweep.enabled,
                "interval_minutes": self.stuck_sweep.interval_minutes,
                "max_recovery_attempts": self.stuck_sweep.max_recovery_attempts,
            },
        }

    def startup_errors(self) -> list[str]:
        """Own-block invariants for the ``tech_lead`` section (#6870).

        The documented disable value is exactly 0; a value outside
        ``0..TECH_LEAD_MAX_EXPEDITED_LIMIT`` is a misconfiguration that must fail
        startup loudly, never be silently treated as disabled (mirrors the
        health-review/stuck-sweep blocks). The upper bound is shared verbatim
        with the settings-form schema so both layers agree on the ceiling.
        """
        errors: list[str] = []
        if not 0 <= self.max_expedited <= TECH_LEAD_MAX_EXPEDITED_LIMIT:
            errors.append(
                "tech_lead.max_expedited must be between 0 and "
                f"{TECH_LEAD_MAX_EXPEDITED_LIMIT} (0 disables the expedite lane), "
                f"got {self.max_expedited}"
            )
        errors.extend(self.dedup.startup_errors())
        return errors


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
