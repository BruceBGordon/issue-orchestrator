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


def _required_mapping(data: dict, key: str) -> dict:
    """The ``tech_lead.<key>`` sub-dict, rejecting every non-mapping by SHAPE.

    ``data.get(key, {}) or {}`` accepts any falsy value and silently replaces it
    with the block's defaults. For ``findings`` that is not a cosmetic sloppiness:
    the defaults are ``promote: gated`` with ``route.default: self``, so
    ``findings: false`` — an operator plainly trying to turn the lane OFF — would
    have quietly ENABLED issue creation (#6957 round-5 review F14). It is the
    same truthiness bug this branch already fixed one level down, at
    ``tech_lead.findings.route``.

    Omission and an explicit ``null`` are the only accepted ways to say "use the
    defaults"; anything else that is not a mapping is a loud configuration error.
    """
    value = data.get(key, None)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            f"tech_lead.{key} must be a mapping, got {type(value).__name__}"
            f" ({value!r}); remove the key or set it to null to accept the"
            f" defaults — note that the tech_lead.findings defaults ENABLE the"
            f" promotion lane, so use `promote: off` to disable it"
        )
    return value


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
        findings=TechLeadFindingsConfig.from_mapping(
            _required_mapping(data, "findings")
        ),
    )
