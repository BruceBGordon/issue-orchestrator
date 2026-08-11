"""Allowed YAML config shape for unknown-field validation."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from functools import cache
from typing import Any, get_type_hints

from ..domain.models import CommentHeadings
from .config_models import (
    ClaimsConfig,
    CleanupConfig,
    DangerousConfig,
    DefaultAgentConfig,
    E2EConfig,
    FilteringConfig,
    GoalPilotConfig,
    HooksConfig,
    IsolationConfig,
    MergeQueueConfig,
    ProviderResilienceConfig,
    RetryConfig,
    SchedulingConfig,
    SessionInteractionsConfig,
    SqliteBackupConfig,
    TimelineConfig,
    TechLeadConfig,
    ValidationConfig,
)
from .config_sections import ALLOWED_AGENT_FIELDS, ALLOWED_TOP_LEVEL_FIELDS


@dataclass(frozen=True)
class _Leaf:
    """Scalar/list config value; child keys are not inspected."""


@dataclass(frozen=True)
class _OpenMap:
    """Mapping whose child keys are intentionally provider/user-defined."""


@dataclass(frozen=True)
class DynamicMap:
    """Mapping whose keys are arbitrary names and values share one schema."""

    value_schema: "ConfigShape"


ConfigShape = dict[str, "ConfigShape"] | DynamicMap | _Leaf | _OpenMap

LEAF = _Leaf()
OPEN_MAP = _OpenMap()

__all__ = [
    "ConfigShape",
    "DynamicMap",
    "LEAF",
    "OPEN_MAP",
    "allowed_config_shape",
    "dataclass_config_shape",
]


def _leaf_keys(*keys: str) -> dict[str, ConfigShape]:
    return {key: LEAF for key in keys}


def _merge(
    base: dict[str, ConfigShape],
    extra: dict[str, ConfigShape],
) -> dict[str, ConfigShape]:
    merged = dict(base)
    merged.update(extra)
    return merged


def dataclass_config_shape(model: type[Any]) -> dict[str, ConfigShape]:
    """Derive an allowed YAML shape from a config dataclass."""
    if not is_dataclass(model):
        raise TypeError(f"{model!r} is not a dataclass")
    type_hints = get_type_hints(model)
    shape: dict[str, ConfigShape] = {}
    for field in fields(model):
        field_type = type_hints.get(field.name)
        if isinstance(field_type, type) and is_dataclass(field_type):
            shape[field.name] = dataclass_config_shape(field_type)
        else:
            shape[field.name] = LEAF
    return shape


def _agent_shape() -> dict[str, ConfigShape]:
    shape = _leaf_keys(*ALLOWED_AGENT_FIELDS)
    shape["provider_args"] = OPEN_MAP
    return shape


def _default_agent_shape() -> dict[str, ConfigShape]:
    return _merge(
        dataclass_config_shape(DefaultAgentConfig),
        {"provider_args": OPEN_MAP},
    )


@cache
def allowed_config_shape() -> dict[str, ConfigShape]:
    """Return the recursive allowed YAML config shape.

    Most nested sections are derived from the existing config dataclasses so a
    new dataclass field automatically becomes valid YAML. Sections whose YAML
    layout is not represented by one dataclass are declared here at their
    composition boundary.
    """
    shape: dict[str, ConfigShape] = {
        "agents": DynamicMap(_agent_shape()),
        "ai_systems": _leaf_keys("allowed"),
        "claims": dataclass_config_shape(ClaimsConfig),
        "cleanup": dataclass_config_shape(CleanupConfig),
        "default_agent": _default_agent_shape(),
        "e2e": _merge(dataclass_config_shape(E2EConfig), {"pr_labels": LEAF}),
        "filtering": dataclass_config_shape(FilteringConfig),
        "goal_pilot": dataclass_config_shape(GoalPilotConfig),
        "hooks": dataclass_config_shape(HooksConfig),
        "merge_queue": dataclass_config_shape(MergeQueueConfig),
        "labels": _leaf_keys(
            "in_progress",
            "blocked",
            "needs_human",
            "needs_rework",
            "validation_failed",
            "prefix",
        ),
        "milestones": {
            "sort": LEAF,
            "sort_config": OPEN_MAP,
            "order": LEAF,
            "foundation": LEAF,
        },
        "provider_resilience": dataclass_config_shape(ProviderResilienceConfig),
        "retry": dataclass_config_shape(RetryConfig),
        "scheduling": dataclass_config_shape(SchedulingConfig),
        "security": {
            "enforce_hooks": LEAF,
            "pre_push_hook": LEAF,
            "dangerous": dataclass_config_shape(DangerousConfig),
        },
        "sqlite_backup": dataclass_config_shape(SqliteBackupConfig),
        "state": _leaf_keys("file"),
        "timeline": dataclass_config_shape(TimelineConfig),
        "tech_lead": dataclass_config_shape(TechLeadConfig),
        "validation": dataclass_config_shape(ValidationConfig),
    }
    shape["execution"] = {
        "concurrency": _leaf_keys(
            "max_concurrent_sessions",
            "session_timeout_minutes",
        ),
        "terminal_adapter": LEAF,
        "session_interactions": dataclass_config_shape(SessionInteractionsConfig),
        "isolation": dataclass_config_shape(IsolationConfig),
    }
    shape["observability"] = {
        "session_no_output_seconds": LEAF,
        "session_no_output_tail_lines": LEAF,
        "session_no_output_max_bytes": LEAF,
        "session_no_output_repeat_seconds": LEAF,
        "session_output_retention_runs": LEAF,
        "session_output_retention_days": LEAF,
        "session_output_retention_tier": LEAF,
        "stale_escalation_ticks": LEAF,
        "tick_stall_threshold_seconds": LEAF,
        "comment_headings": dataclass_config_shape(CommentHeadings),
    }
    shape["repo"] = {
        "name": LEAF,
        "root": LEAF,
        "github": {
            "token": LEAF,
            "token_env": LEAF,
            "keyring_service": LEAF,
            "keyring_username": LEAF,
            "app": _leaf_keys(
                "client_id",
                "app_id",
                "installation_id",
                "private_key_path",
                "private_key_env",
            ),
            "api_url": LEAF,
            "http_timeout_seconds": LEAF,
            "cache_ttl_seconds": LEAF,
            "required_scopes": LEAF,
            "allowed_scopes": LEAF,
            "write_verify": _leaf_keys(
                "timeout_seconds",
                "initial_delay_ms",
                "max_delay_ms",
                "backoff",
                "jitter_ms",
            ),
            "rate_limit": _leaf_keys(
                "startup",
                "every_calls",
                "warn_fraction",
                "warn_remaining",
            ),
            "audit": _leaf_keys("enabled", "events", "file"),
        },
    }
    shape["review"] = {
        "enabled": LEAF,
        "default": LEAF,
        "code_review_label": LEAF,
        "code_reviewed_label": LEAF,
        "tech_lead_review_agent": LEAF,
        "tech_lead_follow_up_agent": LEAF,
        "tech_lead_review_label": LEAF,
        "tech_lead_reviewed_label": LEAF,
        "tech_lead_failed_label": LEAF,
        "tech_lead_review_threshold": LEAF,
        "tech_lead_review_on_failure": LEAF,
        "max_rework_cycles": LEAF,
        "max_consecutive_publish_failures": LEAF,
        "max_consecutive_review_exchange_failures": LEAF,
        "reviewer_feedback_cache_minutes": LEAF,
        "keep_current_approach_label": LEAF,
        "internal": _leaf_keys("enabled", "max_rounds", "instructions"),
        "retrospective": _leaf_keys(
            "enabled",
            "trigger_label",
            "reviewed_label",
            "changes_requested_label",
        ),
        "run_audit": _leaf_keys("min_runtime_minutes", "on_timeout"),
        "nits": _leaf_keys("default_policy", "by_agent"),
        "exchange": {
            "mode": LEAF,
            "probe": _leaf_keys("schedule", "interval_days"),
            "loop": _leaf_keys(
                "max_rounds",
                "max_no_progress",
                "require_validation",
            ),
        },
    }
    shape["ui"] = {
        "mode": LEAF,
        "web_port": LEAF,
        "control_api_port": LEAF,
        "queue_refresh_seconds": LEAF,
        "fetch_layer": _leaf_keys(
            "enabled",
            "network_sync_seconds",
            "full_scan_interval_seconds",
            "discovery_limit",
            "max_hot_issues_per_cycle",
            "pr_scan_every_n_refreshes",
            "dependency_scan_every_n_refreshes",
            "visibility_aware_enabled",
            "selective_sync_planner_enabled",
        ),
        "instances": LEAF,
        "flow_refresh": _leaf_keys(
            "enabled",
            "stale_seconds",
            "cooldown_seconds",
            "freshness_mode",
            "api_budget",
            "attention_priority",
        ),
        "browser_session": _leaf_keys(
            "ttl_seconds",
            "max",
            "sse_token_ttl_seconds",
        ),
    }
    shape["worktrees"] = {
        "base": LEAF,
        "base_branch_override": LEAF,
        "seed_ref": LEAF,
        "setup": LEAF,
        "reuse_push_preflight": LEAF,
        "allow_no_verify_dry_run_preflight": LEAF,
        "worktree_branch_on_recreate": LEAF,
        "remediation": _leaf_keys("pr_collision", "push_rebase_retry"),
    }

    missing = ALLOWED_TOP_LEVEL_FIELDS - shape.keys()
    if missing:
        raise RuntimeError(
            "Allowed config shape missing top-level sections: "
            + ", ".join(sorted(missing))
        )
    return shape
