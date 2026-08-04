"""YAML parsing for the ``tech_lead`` config section.

Extracted from ``config_sections.py`` for cohesion (the tech_lead section has
grown its own labels/milestone/authority/health-review/stuck-sweep/expedite
sub-parsing). ``config_sections`` re-exports :func:`parse_tech_lead_config` so
the section dispatch table and existing importers are unaffected.
"""

from __future__ import annotations

from .config_models import (
    MilestoneStrategyConfig,
    StuckSweepConfig,
    TechLeadAuthorityConfig,
    TechLeadConfig,
    TechLeadDedupConfig,
    TechLeadFindingsConfig,
    TechLeadHealthReviewConfig,
)


def parse_tech_lead_config(data: dict) -> TechLeadConfig:
    """Parse tech_lead section from YAML data."""
    # Parse lists (support comma-separated strings)
    inherit_labels = data.get("inherit_labels") or []
    if isinstance(inherit_labels, str):
        inherit_labels = [lbl.strip() for lbl in inherit_labels.split(",") if lbl.strip()]

    explicit_labels = data.get("explicit_labels") or []
    if isinstance(explicit_labels, str):
        explicit_labels = [lbl.strip() for lbl in explicit_labels.split(",") if lbl.strip()]

    # Parse milestone_strategy
    ms_data = data.get("milestone_strategy", {})
    milestone_strategy = MilestoneStrategyConfig(
        inherit_from_issues=ms_data.get("inherit_from_issues", "latest"),
        explicit=ms_data.get("explicit"),
    )

    max_concurrent = int(mc) if (mc := data.get("max_concurrent")) is not None else None

    return TechLeadConfig(
        inherit_labels=list(inherit_labels),
        explicit_labels=list(explicit_labels),
        milestone_strategy=milestone_strategy,
        priority=data.get("priority"),
        max_concurrent=max_concurrent,
        max_expedited=int(data.get("max_expedited", 3)),
        authority=TechLeadAuthorityConfig.from_mapping(data.get("authority", {}) or {}),
        dedup=TechLeadDedupConfig.from_mapping(data.get("dedup", {}) or {}),
        health_review=TechLeadHealthReviewConfig.from_mapping(
            data.get("health_review", {}) or {}
        ),
        stuck_sweep=StuckSweepConfig.from_mapping(data.get("stuck_sweep", {}) or {}),
        findings=TechLeadFindingsConfig.from_mapping(data.get("findings", {}) or {}),
    )
