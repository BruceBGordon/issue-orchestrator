"""Review-workflow projections from runtime config to public mappings."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


def runtime_exchange_dict(config: "Config") -> dict[str, object]:
    """Project the complete exchange configuration for runtime events."""
    return {
        "mode": config.review_exchange_mode,
        "probe": {
            "schedule": config.review_exchange_probe_schedule,
            "interval_days": config.review_exchange_probe_interval_days,
        },
        "loop": {
            "max_rounds": config.review_exchange_max_rounds,
            "max_no_progress": config.review_exchange_max_no_progress,
            "require_validation": config.review_exchange_require_validation,
        },
    }


def runtime_run_audit_dict(config: "Config") -> dict[str, object]:
    """Project the complete run-audit configuration for runtime events."""
    return {
        "min_runtime_minutes": config.review_run_audit_min_runtime_minutes,
        "on_timeout": config.review_run_audit_on_timeout,
    }


def internal_review_dict(config: "Config") -> dict[str, object]:
    """Project the complete internal-review configuration."""
    return {
        "enabled": config.internal_review_enabled,
        "max_rounds": config.internal_review_max_rounds,
        "instructions": config.internal_review_instructions,
    }


def serialized_internal_review_dict(config: "Config") -> dict[str, object] | None:
    """Return internal-review YAML only when it differs from defaults."""
    if (
        not config.internal_review_enabled
        and config.internal_review_max_rounds == 5
        and config.internal_review_instructions == ".io/internal-review.md"
    ):
        return None
    return internal_review_dict(config)
