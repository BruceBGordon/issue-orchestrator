"""Config section parsing and application helpers."""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.models import AgentConfig, CommentHeadings
from .config_models import (
    AiGateConfig,
    ClaimsConfig,
    CleanupConfig,
    CleanupWithTechLead,
    CleanupWithoutTechLead,
    CoverageGuardrailConfig,
    DangerousConfig,
    DefaultAgentConfig,
    E2EConfig,
    FilteringConfig,
    GoalPilotConfig,
    HooksConfig,
    InterruptedSessionRetryConfig,
    IsolationConfig,
    MERGE_QUEUE_FAILURE_ACTIONS,
    MERGE_QUEUE_GATES,
    MERGE_QUEUE_PROVIDERS,
    MergeQueueConfig,
    ProviderCircuitBreakerConfig,
    ProviderResilienceConfig,
    ProviderShortRetryConfig,
    RetryConfig,
    SchedulingConfig,
    SessionInteractionsConfig,
    SqliteBackupConfig,
    TimelineConfig,
    PublishValidationConfig,
    ValidationCommandConfig,
    ValidationConfig,
)
# tech_lead section parsing lives in its own cohesion module; re-exported here so
# the section dispatch table and existing importers keep working.
from .config_sections_tech_lead import parse_tech_lead_config
from .config_paths import get_section, resolve_relative_path, resolve_worktree_base
from .config_value_rules import normalize_optional_mapping
from . import github_config as _github_config

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)

# Valid per-agent config fields (worktree_base and repo_root removed - now top-level only)
ALLOWED_AGENT_FIELDS = {
    'prompt', 'provider', 'model', 'timeout_minutes',
    'permission_mode', 'skip_review', 'reviewer', 'command', 'sandbox',
    'meta_agent', 'initial_prompt', 'ai_system', 'provider_args', 'retry_prompt_template',
}

_TOP_LEVEL_SECTION_KEYS = (
    "agents", "labels", "review", "cleanup", "worktrees", "execution",
    "validation", "provider_resilience", "ui", "observability", "timeline", "security", "filtering",
    "tech_lead", "scheduling", "e2e", "goal_pilot", "milestones", "state", "claims", "hooks",
    "ai_systems", "retry",
    "sqlite_backup",
    "merge_queue",
)

# Derive ALLOWED_TOP_LEVEL_FIELDS from _TOP_LEVEL_SECTION_KEYS — single source of truth.
# "repo" and "default_agent" are parsed separately but are valid top-level keys.
ALLOWED_TOP_LEVEL_FIELDS = frozenset(_TOP_LEVEL_SECTION_KEYS) | {"repo", "default_agent"}

_SUPPORTED_VALIDATION_KEYS = frozenset(
    {
        "coverage_guardrail",
        "junit_xml_paths",
        "publish",
        "quick",
    }
)


def parse_default_agent_config(data: dict) -> DefaultAgentConfig | None:
    """Parse the optional default_agent section from YAML data."""
    if not data:
        return None
    return DefaultAgentConfig(
        provider=data.get("provider"),
        model=data.get("model"),
        provider_args=data.get("provider_args", {}),
    )


def parse_e2e_config(data: dict) -> E2EConfig:
    """Parse e2e section from YAML data."""
    pytest_args = data.get("pytest_args") or ["tests/e2e", "-v"]
    if isinstance(pytest_args, str):
        # Support space-separated string
        pytest_args = pytest_args.split()

    command = data.get("command") or []
    if isinstance(command, str):
        command = command.split()

    junit_xml_paths = data.get("junit_xml_paths") or []
    if isinstance(junit_xml_paths, str):
        junit_xml_paths = [
            line.strip() for line in junit_xml_paths.splitlines() if line.strip()
        ]

    artifact_paths = data.get("artifact_paths") or []
    if isinstance(artifact_paths, str):
        artifact_paths = [
            line.strip() for line in artifact_paths.splitlines() if line.strip()
        ]

    # Validate role
    role = data.get("role", "auto")
    if role not in ("auto", "executor", "reader", "disabled"):
        role = "auto"

    runner_kind = data.get("runner_kind", "pytest")
    if runner_kind not in ("pytest", "command"):
        raise ValueError(
            f"e2e.runner_kind must be 'pytest' or 'command', got {runner_kind!r}"
        )

    return E2EConfig(
        enabled=data.get("enabled", False),
        role=role,
        auto_run_interval_minutes=data.get("auto_run_interval_minutes", 30),
        occupies_session_slot=data.get("occupies_session_slot", False),
        runner_kind=runner_kind,
        pytest_args=list(pytest_args),
        command=list(command),
        junit_xml_paths=list(junit_xml_paths),
        artifact_paths=list(artifact_paths),
        allow_retry_once=data.get("allow_retry_once", True),
        quarantine_file=data.get("quarantine_file", "tests/e2e/quarantine.txt"),
        survive_restart=data.get("survive_restart", True),
        stop_on_first_failure=data.get("stop_on_first_failure", False),
        auto_quarantine=data.get("auto_quarantine", True),
        auto_create_issues=data.get("auto_create_issues", True),
        issue_agent_label=data.get("issue_agent_label", "agent:backend"),
        flake_threshold=data.get("flake_threshold", 20),
        flake_window_runs=data.get("flake_window_runs", 10),
        run_retention_count=data.get("run_retention_count", 50),
    )


def parse_sqlite_backup_config(data: dict) -> SqliteBackupConfig:
    """Parse sqlite_backup section from YAML data."""
    return SqliteBackupConfig(
        enabled=data.get("enabled", True),
        cadence_hours=data.get("cadence_hours", 24),
        check_interval_minutes=data.get("check_interval_minutes", 60),
        retention_daily=data.get("retention_daily", 14),
        retention_weekly=data.get("retention_weekly", 8),
        enforce_on_startup=data.get("enforce_on_startup", True),
    )


def parse_timeline_config(data: dict) -> TimelineConfig:
    """Parse timeline section from YAML data."""
    return TimelineConfig(
        max_records=data.get("max_records", 5000),
    )


def parse_claims_config(data: dict) -> ClaimsConfig:
    """Parse claims section from YAML data."""
    return ClaimsConfig(
        enabled=data.get("enabled", False),
        claimant_id=data.get("claimant_id"),
        lease_seconds=data.get("lease_seconds", 900),
        renew_before_expiry_seconds=data.get("renew_before_expiry_seconds", 300),
        convergence_timeout_seconds=data.get("convergence_timeout_seconds", 5.0),
        convergence_poll_min_ms=data.get("convergence_poll_min_ms", 250),
        convergence_poll_max_ms=data.get("convergence_poll_max_ms", 500),
    )


def parse_goal_pilot_config(data: dict) -> GoalPilotConfig:
    """Parse goal_pilot section from YAML data."""
    return GoalPilotConfig(
        enabled=data.get("enabled", False),
        agent=data.get("agent"),
        approval_policy=data.get("approval_policy", "journeys_only"),
        approval_batch_size=data.get("approval_batch_size", 10),
        approval_batch_window_minutes=data.get("approval_batch_window_minutes", 60),
    )


def _validate_choice(value: str, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise ValueError(
            f"merge_queue.{field} must be one of {list(allowed)}, got {value!r}"
        )
    return value


def parse_merge_queue_config(data: dict) -> MergeQueueConfig:
    """Parse and validate the merge_queue section from YAML data."""
    return MergeQueueConfig(
        enabled=data.get("enabled", False),
        provider=_validate_choice(
            data.get("provider", "github"), MERGE_QUEUE_PROVIDERS, "provider"
        ),
        enqueue_after=_validate_choice(
            data.get("enqueue_after", "code-reviewed"), MERGE_QUEUE_GATES, "enqueue_after"
        ),
        failure_action=_validate_choice(
            data.get("failure_action", "rework"),
            MERGE_QUEUE_FAILURE_ACTIONS,
            "failure_action",
        ),
    )


def parse_hooks_config(data: dict) -> HooksConfig:
    """Parse hooks section from YAML data."""
    ai_gate_data = data.get("ai_gate", {})
    ai_gate = AiGateConfig(
        interval_days=ai_gate_data.get("interval_days", 7),
        dangerous_allow_failure=ai_gate_data.get("dangerous_allow_failure", False),
    )
    return HooksConfig(ai_gate=ai_gate)


def parse_scheduling_config(data: dict) -> SchedulingConfig:
    """Parse scheduling section from YAML data."""
    return SchedulingConfig(
        default_priority_tier=int(data.get("default_priority_tier", 1)),
    )


def parse_filtering_config(data: dict) -> FilteringConfig:
    """Parse filtering section from YAML data."""
    # Parse milestones (list or comma-separated string)
    raw_milestones = data.get("milestones") or []
    if isinstance(raw_milestones, str):
        raw_milestones = [m.strip() for m in raw_milestones.split(",") if m.strip()]
    if not isinstance(raw_milestones, list):
        raise ValueError("filtering.milestones must be a list or comma-separated string")
    milestones = [str(m).strip() for m in raw_milestones if str(m).strip()]

    # Parse exclude_labels (list or comma-separated string)
    raw_exclude = data.get("exclude_labels") or []
    if isinstance(raw_exclude, str):
        raw_exclude = [lbl.strip() for lbl in raw_exclude.split(",") if lbl.strip()]
    if not isinstance(raw_exclude, list):
        raise ValueError("filtering.exclude_labels must be a list or comma-separated string")
    exclude_labels = [str(lbl).strip() for lbl in raw_exclude if str(lbl).strip()]

    raw_exclude_prefixes = data.get("exclude_label_prefixes") or []
    if isinstance(raw_exclude_prefixes, str):
        raw_exclude_prefixes = [
            prefix.strip()
            for prefix in raw_exclude_prefixes.split(",")
            if prefix.strip()
        ]
    if not isinstance(raw_exclude_prefixes, list):
        raise ValueError(
            "filtering.exclude_label_prefixes must be a list or comma-separated string"
        )
    exclude_label_prefixes = [
        str(prefix).strip() for prefix in raw_exclude_prefixes if str(prefix).strip()
    ]

    return FilteringConfig(
        label=data.get("label"),
        milestone=data.get("milestone"),
        milestones=milestones,
        issue=data.get("issue"),
        exclude_labels=exclude_labels,
        exclude_label_prefixes=exclude_label_prefixes,
        fetch_limit=data.get("fetch_limit", 100),
        max_to_start=data.get("max_to_start", 0),
    )


def parse_provider_resilience_config(data: dict) -> ProviderResilienceConfig:
    """Parse provider resilience section from YAML data."""
    short_data = data.get("short_retry", {}) or {}
    circuit_data = data.get("circuit_breaker", {}) or {}

    short_retry = ProviderShortRetryConfig(
        max_attempts=int(short_data.get("max_attempts", 4)),
        initial_backoff_seconds=int(short_data.get("initial_backoff_seconds", 5)),
        max_backoff_seconds=int(short_data.get("max_backoff_seconds", 60)),
        jitter=bool(short_data.get("jitter", True)),
    )
    circuit_breaker = ProviderCircuitBreakerConfig(
        cooldown_seconds=int(circuit_data.get("cooldown_seconds", 1800)),
        max_cooldowns=int(circuit_data.get("max_cooldowns", 6)),
        label=str(circuit_data.get("label", "blocked:provider-unavailable")),
        auth_failure_threshold=int(circuit_data.get("auth_failure_threshold", 1)),
        auth_cooldown_seconds=int(circuit_data.get("auth_cooldown_seconds", 21600)),
    )
    return ProviderResilienceConfig(
        short_retry=short_retry,
        circuit_breaker=circuit_breaker,
    )


def parse_milestone_order(value: object) -> list[str]:
    """Parse milestones.order from YAML (list or comma-separated string)."""
    raw = value or []
    if isinstance(raw, str):
        raw = [m.strip() for m in raw.split(",") if m.strip()]
    if not isinstance(raw, list):
        raise ValueError("milestones.order must be a list or comma-separated string")
    return [str(m).strip() for m in raw if str(m).strip()]


# Optional complex config sections: each entry maps a YAML section name (which
# is also the matching ``Config`` attribute it populates) to its parser. Driving
# these from a table keeps the apply step flat as sections are added, instead of
# growing one more ``if`` branch each time.
_OPTIONAL_SECTION_PARSERS: dict[str, Callable[[dict], object]] = {
    "tech_lead": parse_tech_lead_config,
    "scheduling": parse_scheduling_config,
    "e2e": parse_e2e_config,
    "timeline": parse_timeline_config,
    "sqlite_backup": parse_sqlite_backup_config,
    "goal_pilot": parse_goal_pilot_config,
    "merge_queue": parse_merge_queue_config,
    "claims": parse_claims_config,
    "hooks": parse_hooks_config,
    "provider_resilience": parse_provider_resilience_config,
}


def apply_optional_sections(config: "Config", sections: dict) -> None:
    """Apply optional complex config sections.

    Each section name doubles as the ``Config`` attribute it populates; an empty
    section is skipped so the attribute keeps its default.
    """
    for name, parse in _OPTIONAL_SECTION_PARSERS.items():
        raw = sections[name]
        if raw:
            setattr(config, name, parse(raw))


def load_repo_section(config: "Config", repo_section: dict, github_section: dict) -> None:
    """Load repo and GitHub settings into config."""
    config.repo = repo_section.get("name")
    _github_config.apply_github_section(config, github_section)


def load_github_write_verify(config: "Config", github_section: dict) -> None:
    """Load GitHub write verification and rate limit settings."""
    write_verify = github_section.get("write_verify", {})
    config.gh_write_verify_timeout_seconds = write_verify.get("timeout_seconds", 20)
    config.gh_write_verify_initial_delay_ms = write_verify.get("initial_delay_ms", 250)
    config.gh_write_verify_max_delay_ms = write_verify.get("max_delay_ms", 2000)
    config.gh_write_verify_backoff = write_verify.get("backoff", 1.5)
    config.gh_write_verify_jitter_ms = write_verify.get("jitter_ms", 0)

    rate_limit = github_section.get("rate_limit", {})
    config.gh_rate_limit_startup = rate_limit.get("startup", True)
    config.gh_rate_limit_every_calls = rate_limit.get("every_calls", 500)
    config.gh_rate_limit_warn_fraction = rate_limit.get("warn_fraction", 0.1)
    config.gh_rate_limit_warn_remaining = rate_limit.get("warn_remaining", 100)

    audit = github_section.get("audit", {})
    config.gh_audit_enabled = audit.get("enabled", False)
    config.gh_audit_events = audit.get("events", False)
    config.gh_audit_file = audit.get("file")


def load_labels_section(config: "Config", labels_section: dict) -> None:
    """Load label configuration."""
    config.label_in_progress = labels_section.get("in_progress", "in-progress")
    config.label_blocked = labels_section.get("blocked", "blocked")
    config.label_needs_human = labels_section.get("needs_human", "needs-human")
    config.label_needs_rework = labels_section.get("needs_rework", "needs-rework")
    config.label_validation_failed = labels_section.get("validation_failed", "validation-failed")
    config.label_prefix = labels_section.get("prefix")


def load_review_section(config: "Config", review_section: dict) -> None:
    """Load review workflow configuration."""
    config.review_enabled = review_section.get("enabled", False)
    config.code_review_agent = review_section.get("default")
    config.code_review_label = review_section.get("code_review_label", "needs-code-review")
    config.code_reviewed_label = review_section.get("code_reviewed_label", "code-reviewed")
    config.tech_lead_review_agent = review_section.get("tech_lead_review_agent")
    config.tech_lead_follow_up_agent = review_section.get("tech_lead_follow_up_agent")
    config.tech_lead_review_label = review_section.get("tech_lead_review_label")
    config.tech_lead_reviewed_label = review_section.get("tech_lead_reviewed_label", "tech-lead-reviewed")
    config.tech_lead_failed_label = review_section.get("tech_lead_failed_label", "tech-lead-failed")
    config.tech_lead_review_threshold = review_section.get("tech_lead_review_threshold", 0)
    config.tech_lead_review_on_failure = review_section.get("tech_lead_review_on_failure", True)
    config.max_rework_cycles = review_section.get("max_rework_cycles", 5)
    config.max_consecutive_publish_failures = review_section.get(
        "max_consecutive_publish_failures", 3
    )
    config.max_consecutive_review_exchange_failures = review_section.get(
        "max_consecutive_review_exchange_failures", 3
    )
    post_publish_section = review_section.get("post_publish", {})
    if isinstance(post_publish_section, dict):
        config.post_publish_checks_pending_timeout_seconds = float(
            post_publish_section.get("checks_pending_timeout_seconds", 1800.0)
        )
    config.reviewer_feedback_cache_minutes = review_section.get(
        "reviewer_feedback_cache_minutes", 5
    )
    config.review_keep_current_approach_label = review_section.get(
        "keep_current_approach_label",
        "reviewer-keep-current-approach",
    )
    retrospective_section = review_section.get("retrospective", {})
    if isinstance(retrospective_section, dict):
        config.retrospective_review_enabled = retrospective_section.get("enabled", False)
        config.retrospective_review_trigger_label = retrospective_section.get(
            "trigger_label",
            "retrospective-review",
        )
        config.retrospective_reviewed_label = retrospective_section.get(
            "reviewed_label",
            "retrospective-reviewed",
        )
        config.retrospective_changes_requested_label = retrospective_section.get(
            "changes_requested_label",
            "retrospective-changes-requested",
        )
    run_audit_section = review_section.get("run_audit", {})
    if isinstance(run_audit_section, dict):
        config.review_run_audit_min_runtime_minutes = run_audit_section.get(
            "min_runtime_minutes",
            20,
        )
        config.review_run_audit_on_timeout = run_audit_section.get(
            "on_timeout",
            True,
        )
    internal_section = get_section(
        review_section,
        "internal",
        config.config_path or Path("<config>"),
    )
    config.internal_review_enabled = internal_section.get("enabled", False)
    config.internal_review_max_rounds = internal_section.get("max_rounds", 5)
    internal_instructions = internal_section.get(
        "instructions",
        ".io/internal-review.md",
    )
    config.internal_review_instructions = (
        internal_instructions.strip()
        if isinstance(internal_instructions, str)
        else internal_instructions
    )
    nits_section = review_section.get("nits", {})
    if isinstance(nits_section, dict):
        config.review_nits_default_policy = nits_section.get(
            "default_policy",
            "surface",
        )
        config.review_nits_by_agent = normalize_optional_mapping(nits_section.get("by_agent", {}))
    exchange_section = review_section.get("exchange", {})
    config.review_exchange_mode = exchange_section.get("mode", "via-local-loop")
    probe_section = exchange_section.get("probe", {})
    if isinstance(probe_section, dict):
        config.review_exchange_probe_schedule = probe_section.get("schedule", "daily")
        config.review_exchange_probe_interval_days = probe_section.get("interval_days", 1)
    loop_section = exchange_section.get("loop", {})
    if isinstance(loop_section, dict):
        config.review_exchange_max_rounds = loop_section.get("max_rounds", 10)
        config.review_exchange_max_no_progress = loop_section.get("max_no_progress", 2)
        config.review_exchange_require_validation = loop_section.get(
            "require_validation", True
        )
    # agent_pair removed; coder/reviewer derived at runtime


def load_cleanup_section(config: "Config", cleanup_section: dict) -> None:
    """Load cleanup configuration."""
    if cleanup_section:
        with_tech_lead_data = cleanup_section.get("with_tech_lead", {})
        without_tech_lead_data = cleanup_section.get("without_tech_lead", {})

        config.cleanup = CleanupConfig(
            with_tech_lead=CleanupWithTechLead(
                close_ai_session_tabs=with_tech_lead_data.get("close_ai_session_tabs", True),
                remove_worktrees=with_tech_lead_data.get("remove_worktrees", True),
            ),
            without_tech_lead=CleanupWithoutTechLead(
                wait_for_code_review=without_tech_lead_data.get("wait_for_code_review", True),
                close_ai_session_tabs=without_tech_lead_data.get("close_ai_session_tabs", True),
                remove_worktrees=without_tech_lead_data.get("remove_worktrees", True),
            ),
        )


def load_worktrees_section(
    config: "Config",
    worktrees_section: dict,
    repo_root: Path,
    config_path: Path,
) -> None:
    """Load worktree configuration."""
    worktree_base_raw = worktrees_section.get("base")
    config.worktree_base = resolve_worktree_base(worktree_base_raw, repo_root)

    base_branch_override_raw = worktrees_section.get("base_branch_override")
    if base_branch_override_raw is None:
        config.worktree_base_branch_override = None
    else:
        base_branch_override = str(base_branch_override_raw).strip()
        config.worktree_base_branch_override = base_branch_override or None

    seed_ref_raw = worktrees_section.get("seed_ref")
    if seed_ref_raw is None:
        config.worktree_seed_ref = None
    else:
        seed_ref = str(seed_ref_raw).strip()
        config.worktree_seed_ref = seed_ref or None

    # Validate worktree_base is usable
    try:
        config.worktree_base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            f"worktrees.base '{config.worktree_base}' cannot be created: {exc}. "
            "Specify an absolute path in your config under worktrees.base"
        ) from exc

    if "setup" in worktrees_section:
        config.setup_worktree = worktrees_section.get("setup", [])
    config.reuse_push_preflight = worktrees_section.get("reuse_push_preflight", True)
    config.allow_no_verify_dry_run_preflight = worktrees_section.get(
        "allow_no_verify_dry_run_preflight", True
    )
    config.worktree_branch_on_recreate = worktrees_section.get(
        "worktree_branch_on_recreate", "delete"
    )

    remediation_section = get_section(worktrees_section, "remediation", config_path)
    config.worktree_remediation_pr_collision = remediation_section.get(
        "pr_collision", "new_branch"
    )
    config.worktree_remediation_push_rebase_retry = remediation_section.get(
        "push_rebase_retry", True
    )


def load_execution_section(config: "Config", execution_section: dict, config_path: Path) -> None:
    """Load execution and terminal configuration."""
    # Concurrency
    concurrency = get_section(execution_section, "concurrency", config_path)
    config.max_concurrent_sessions = concurrency.get("max_concurrent_sessions", 3)
    config.session_timeout_minutes = concurrency.get("session_timeout_minutes", 45)

    # Terminal adapter
    config.terminal_adapter = execution_section.get("terminal_adapter")
    session_interactions = get_section(execution_section, "session_interactions", config_path)
    config.session_interactions = SessionInteractionsConfig(
        enabled=bool(session_interactions.get("enabled", False))
    )

    # Isolation
    isolation_data = execution_section.get("isolation", {})
    if isolation_data:
        config.isolation = IsolationConfig(mode=isolation_data.get("mode", "standard"))


def load_ui_section(config: "Config", ui_section: dict) -> None:
    """Load UI configuration."""
    def _choice(raw: object, allowed: set[str], default: str) -> str:
        value = str(raw or default).strip().lower()
        return value if value in allowed else default

    def _flow_refresh_defaults_for_mode(
        freshness_mode: str,
        api_budget: str,
    ) -> tuple[bool, int, int]:
        # High-level presets: these define the default lazy-refresh behavior.
        preset = {
            "aggressive": (True, 180, 30),
            "balanced": (True, 900, 120),
            "economy": (True, 3600, 300),
        }[freshness_mode]
        enabled, stale_seconds, cooldown_seconds = preset
        budget_multiplier = {
            "high": 0.6,
            "medium": 1.0,
            "low": 1.7,
        }[api_budget]
        stale_seconds = max(60, int(stale_seconds * budget_multiplier))
        cooldown_seconds = max(0, int(cooldown_seconds * budget_multiplier))
        return enabled, stale_seconds, cooldown_seconds

    config.ui_mode = ui_section.get("mode", "web")
    config.web_port = ui_section.get("web_port", 0)
    config.control_api_port = ui_section.get("control_api_port", 0)
    config.queue_refresh_seconds = ui_section.get("queue_refresh_seconds", 600)
    browser_session_section = ui_section.get("browser_session", {}) or {}
    config.browser_session_ttl_seconds = int(
        browser_session_section.get("ttl_seconds", config.browser_session_ttl_seconds)
    )
    config.browser_session_max = int(
        browser_session_section.get("max", config.browser_session_max)
    )
    config.sse_token_ttl_seconds = int(
        browser_session_section.get(
            "sse_token_ttl_seconds", config.sse_token_ttl_seconds
        )
    )
    fetch_layer = ui_section.get("fetch_layer", {})
    config.fetch_layer_enabled = fetch_layer.get("enabled", True)
    config.fetch_layer_network_sync_seconds = fetch_layer.get("network_sync_seconds", 60)
    config.fetch_layer_full_scan_interval_seconds = fetch_layer.get(
        "full_scan_interval_seconds", 1800
    )
    config.fetch_layer_discovery_limit = fetch_layer.get("discovery_limit", 25)
    config.fetch_layer_max_hot_issues_per_cycle = fetch_layer.get("max_hot_issues_per_cycle", 40)
    config.fetch_layer_pr_scan_every_n_refreshes = fetch_layer.get("pr_scan_every_n_refreshes", 2)
    config.fetch_layer_dependency_scan_every_n_refreshes = fetch_layer.get(
        "dependency_scan_every_n_refreshes", 1
    )
    config.fetch_layer_visibility_aware_enabled = fetch_layer.get("visibility_aware_enabled", False)
    config.fetch_layer_selective_sync_planner_enabled = fetch_layer.get(
        "selective_sync_planner_enabled", False
    )
    config.instances = ui_section.get("instances", 1)
    flow_refresh_section = ui_section.get("flow_refresh", {}) or {}
    config.flow_freshness_mode = _choice(
        flow_refresh_section.get("freshness_mode", "balanced"),
        {"aggressive", "balanced", "economy"},
        "balanced",
    )
    config.flow_api_budget = _choice(
        flow_refresh_section.get("api_budget", "medium"),
        {"low", "medium", "high"},
        "medium",
    )
    config.flow_attention_priority = _choice(
        flow_refresh_section.get("attention_priority", "strict"),
        {"strict", "normal"},
        "strict",
    )
    default_enabled, default_stale, default_cooldown = _flow_refresh_defaults_for_mode(
        config.flow_freshness_mode,
        config.flow_api_budget,
    )
    config.flow_refresh_enabled = flow_refresh_section.get("enabled", default_enabled)
    config.flow_refresh_stale_seconds = flow_refresh_section.get("stale_seconds", default_stale)
    config.flow_refresh_cooldown_seconds = flow_refresh_section.get(
        "cooldown_seconds", default_cooldown
    )


def load_observability_section(config: "Config", observability_section: dict) -> None:
    """Load observability configuration."""
    config.session_no_output_seconds = observability_section.get("session_no_output_seconds", 120)
    config.session_no_output_tail_lines = observability_section.get("session_no_output_tail_lines", 50)
    config.session_no_output_max_bytes = observability_section.get("session_no_output_max_bytes", 10000)
    config.session_no_output_repeat_seconds = observability_section.get(
        "session_no_output_repeat_seconds", 120
    )
    config.session_output_retention_runs = observability_section.get("session_output_retention_runs", 7)
    config.session_output_retention_days = observability_section.get("session_output_retention_days", 7)
    tier = str(observability_section.get("session_output_retention_tier", "hot")).strip().lower()
    if tier not in {"hot", "cold"}:
        raise ValueError(
            "observability.session_output_retention_tier must be 'hot' or 'cold'"
        )
    config.session_output_retention_tier = tier
    config.stale_escalation_ticks = observability_section.get("stale_escalation_ticks", 0)
    config.tick_stall_threshold_seconds = int(
        observability_section.get("tick_stall_threshold_seconds", 60)
    )

    # Comment headings
    headings_data = observability_section.get("comment_headings", {})
    if headings_data:
        config.comment_headings = CommentHeadings(
            implementation=headings_data.get("implementation", "## Implementation"),
            problems=headings_data.get("problems", "## Problems Encountered"),
            pr_link=headings_data.get("pr_link", "## Pull Request"),
            blocked=headings_data.get("blocked", "## Blocked"),
            needs_human=headings_data.get("needs_human", "## Needs Human Input"),
        )


def load_security_section(config: "Config", security_section: dict, repo_root: Path) -> None:
    """Load security configuration."""
    config.enforce_hooks = security_section.get("enforce_hooks", True)
    if security_section.get("pre_push_hook"):
        config.pre_push_hook = resolve_relative_path(security_section["pre_push_hook"], repo_root)

    dangerous_data = security_section.get("dangerous", {})
    if dangerous_data:
        config.dangerous = DangerousConfig(
            allow_unsupported_agents=dangerous_data.get("allow_unsupported_agents", False),
        )


def load_validation_section(config: "Config", validation_section: dict) -> None:
    """Load validation configuration."""
    if validation_section:
        _reject_unsupported_validation_keys(validation_section)
        coverage_data = validation_section.get("coverage_guardrail", {}) or {}
        junit_paths_raw = validation_section.get("junit_xml_paths", []) or []
        quick_data = validation_section.get("quick", {}) or {}
        publish_data = validation_section.get("publish", {}) or {}
        config.validation = ValidationConfig(
            quick=ValidationCommandConfig(
                cmd=quick_data.get("cmd"),
                timeout_seconds=quick_data.get("timeout_seconds", 300),
            ),
            publish=PublishValidationConfig(
                cmd=publish_data.get("cmd"),
                timeout_seconds=publish_data.get("timeout_seconds", 1800),
                dirty_check=publish_data.get("dirty_check", "tracked"),
            ),
            coverage_guardrail=CoverageGuardrailConfig(
                enabled=coverage_data.get("enabled", False),
                min_percent=coverage_data.get("min_percent"),
                apply_to=coverage_data.get("apply_to", "changed"),
                scope=coverage_data.get("scope", []) or [],
                coverage_type=coverage_data.get("coverage_type", "line"),
                exclude=coverage_data.get("exclude", []) or [],
            ),
            junit_xml_paths=tuple(str(p) for p in junit_paths_raw if p),
        )
        _warn_partial_validation_commands(config.validation)


def _reject_unsupported_validation_keys(validation_section: dict) -> None:
    """Reject unknown validation keys before they can be ignored."""
    unsupported_keys = sorted(set(validation_section) - _SUPPORTED_VALIDATION_KEYS)
    if not unsupported_keys:
        return
    unsupported = ", ".join(f"validation.{key}" for key in unsupported_keys)
    supported = ", ".join(
        f"validation.{key}" for key in sorted(_SUPPORTED_VALIDATION_KEYS)
    )
    raise ValueError(
        f"Unsupported validation configuration key(s): {unsupported}. "
        f"Supported keys: {supported}."
    )


def _warn_partial_validation_commands(validation: ValidationConfig) -> None:
    """Surface validation sections that intentionally omit one lifecycle gate."""
    has_quick = bool(validation.quick.cmd)
    has_publish = bool(validation.publish.cmd)
    if has_quick and has_publish:
        return
    if has_quick:
        logger.warning(
            "validation.quick.cmd is configured but validation.publish.cmd is not; "
            "pre-push validation will not run."
        )
        return
    if has_publish:
        logger.warning(
            "validation.publish.cmd is configured but validation.quick.cmd is not; "
            "agent/reviewer quick validation will not run."
        )
        return
    logger.warning(
        "validation is configured without validation.quick.cmd or "
        "validation.publish.cmd; command validation is disabled."
    )


def load_retry_section(config: "Config", retry_data: dict) -> None:
    """Load retry configuration."""
    if retry_data:
        interrupted_data = retry_data.get("interrupted_sessions", {}) or {}
        config.retry = RetryConfig(
            max_validation_retries=retry_data.get("max_validation_retries", 3),
            validation_error_file=retry_data.get("validation_error_file", "validation-errors.txt"),
            retry_prompt_template=retry_data.get("retry_prompt_template"),
            interrupted_sessions=InterruptedSessionRetryConfig(
                enabled=interrupted_data.get("enabled", True),
                retry_coding=interrupted_data.get("retry_coding", True),
                retry_review=interrupted_data.get("retry_review", True),
                coding_guard_label=interrupted_data.get(
                    "coding_guard_label", "io:auto-retried-interrupted-coding"
                ),
                review_guard_label=interrupted_data.get(
                    "review_guard_label", "io:auto-retried-interrupted-review"
                ),
            ),
        )


def parse_ai_systems_allowed(value: object) -> list[str]:
    """Normalize ai_systems.allowed values from config."""
    if not value:
        return []
    if isinstance(value, str):
        return [entry.strip() for entry in value.split(",") if entry.strip()]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return []


def _implicit_model_for_provider(provider: str | None) -> str:
    """Return the legacy implicit model for agents without an explicit model.

    Historical configs without a provider implicitly meant Claude Code, so they
    continue to receive the Claude default. Explicit non-Claude providers must
    stay blank so their CLIs can select their own default model.
    """
    if provider in (None, "claude-code"):
        return "sonnet"
    return ""


def load_agents_section(
    config: "Config",
    agents_section: dict,
    repo_root: Path,
) -> None:
    """Load agent configurations."""
    for label, agent_data in agents_section.items():
        prompt_relative = agent_data["prompt"]
        prompt_path = resolve_relative_path(prompt_relative, repo_root)

        # Inherit provider from default_agent if not specified
        provider = agent_data.get("provider")
        if provider is None and config.default_agent:
            provider = config.default_agent.provider

        # Inherit model from default_agent if not specified
        model = agent_data.get("model")
        if model is None and config.default_agent and config.default_agent.model:
            model = config.default_agent.model
        if model is None:
            model = _implicit_model_for_provider(provider)

        # Merge provider_args
        provider_args = {}
        if config.default_agent and config.default_agent.provider_args:
            provider_args.update(config.default_agent.provider_args)
        if agent_data.get("provider_args"):
            provider_args.update(agent_data["provider_args"])

        if "permission_mode" in agent_data:
            raise ValueError(
                f"agents.{label}.permission_mode is not supported; "
                f"set agents.{label}.provider_args.permission_mode instead"
            )
        agent_kwargs = {
            "prompt_path": prompt_path,
            "prompt_relative": prompt_relative,
            "provider": provider,
            "model": model,
            "timeout_minutes": agent_data.get("timeout_minutes", 45),
            "provider_args": provider_args,
            "skip_review": agent_data.get("skip_review", False),
            "reviewer": agent_data.get("reviewer"),
            "meta_agent": agent_data.get("meta_agent"),
            "ai_system": agent_data.get("ai_system"),
            "retry_prompt_template": agent_data.get("retry_prompt_template"),
            "sandbox": agent_data.get("sandbox", False),  # ADR-0034 opt-in (per-agent)
        }
        if "command" in agent_data:
            agent_kwargs["command"] = agent_data["command"]
        if "initial_prompt" in agent_data:
            agent_kwargs["initial_prompt"] = agent_data["initial_prompt"]
        config.agents[label] = AgentConfig(**agent_kwargs)


def extract_config_sections(data: dict, config_path: Path) -> dict:
    """Extract all sections from config data."""
    repo_section = get_section(data, "repo", config_path)
    sections = {
        key: get_section(data, key, config_path)
        for key in _TOP_LEVEL_SECTION_KEYS
    }
    sections["repo"] = repo_section
    sections["github"] = get_section(repo_section, "github", config_path)
    return sections
