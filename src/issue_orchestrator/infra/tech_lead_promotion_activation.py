"""When is the finding-promotion lane active, and is it ready? (#6957)

Four boundaries need this answer and had each encoded their own version of it,
which is how a configuration could pass validation AND doctor and then raise on
a normal tick (#6957 round-2 review F9/A4):

* configuration validation — "is this config startable?"
* doctor — "which route targets must be proven writable before startup?"
* fact gathering — "should this tick read the ledgers and poll targets at all?"
* route resolution — "which repo and scheduling labels does this area map to?"

This module is the single owner. Everything else consumes
:func:`promotion_lane_readiness` and never re-derives the predicate.

**Activation.** The lane is ACTIVE only when the master tech-lead workflow is
enabled, the promotion mode is not ``off``, and a repository is configured.
The master predicate includes the configured-agent dependency and preserves the
legacy agent-presence rule when ``tech_lead.enabled`` is omitted. Turning the
master switch off stops the whole ADR-0031 machinery — including cross-repo
loop-closure reads. Durable promotion rows are never discarded, so re-enabling
resumes settlement exactly where it stopped.

**Readiness.** An ACTIVE lane's remaining dependencies are startup errors, not
tick-time exceptions: any route that resolves through the managed repo's own
worker agent requires ``review.tech_lead_follow_up_agent``, and every foreign
target must be proven writable before startup.

The module is deliberately a pure function of ``Config`` — no ports, no GitHub —
so validation, doctor, and the control plane can all consume it without either
layer importing the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


@dataclass(frozen=True)
class PromotionLaneReadiness:
    """Whether the promotion lane runs, and what it still needs to run safely."""

    active: bool
    # Why the lane is inert, for logs/doctor. Empty when it is active.
    inactive_reason: str = ""
    # Startup configuration errors. Non-empty only for an ACTIVE lane: an
    # inactive lane's missing dependencies are irrelevant, so switching the lane
    # off is always a way out of a misconfiguration.
    problems: tuple[str, ...] = ()
    # NOTE: what doctor must PROVE about each foreign target is deliberately not
    # here. A repo-name list is a weaker statement than the filing command makes
    # (it also provisions labels), and having a second owner state it is what let
    # doctor approve a route whose first promotion would fail (#6957 round-6
    # review F2/A1). ``promotion_filing_contracts`` — built on the one route
    # resolver — owns the probe list, and consults this readiness for activation.

    @property
    def ready(self) -> bool:
        """True when the lane may plan and execute promotion work."""
        return self.active and not self.problems


def promotion_lane_readiness(config: "Config") -> PromotionLaneReadiness:
    """The one activation/readiness decision for the finding-promotion lane."""
    findings = config.tech_lead.findings
    if not config.tech_lead_enabled:
        inactive_reason = (
            "tech_lead.enabled is false"
            if config.tech_lead.enabled is False
            else "no tech lead agent is configured (review.tech_lead_review_agent)"
        )
        return PromotionLaneReadiness(
            active=False,
            inactive_reason=inactive_reason,
        )
    if not findings.enabled:
        return PromotionLaneReadiness(
            active=False,
            inactive_reason="tech_lead.findings.promote is 'off'",
        )
    if not config.repo:
        return PromotionLaneReadiness(
            active=False,
            inactive_reason="no repository is configured (`repo: owner/name`)",
        )

    problems: list[str] = []
    # A route resolves through the managed repo's own worker agent whenever it
    # is `self`, or foreign without its own agent_label. Leaving that unset
    # raises inside route resolution on the tick a pattern finally crosses its
    # threshold — a latent failure, so it is a startup error instead.
    inherits_source_agent = sorted(
        area
        for area, target in findings.route.items()
        if target.is_self or target.agent_label is None
    )
    if inherits_source_agent and not config.tech_lead_follow_up_agent:
        problems.append(
            "review.tech_lead_follow_up_agent is required by"
            f" tech_lead.findings.route[{', '.join(repr(a) for a in inherits_source_agent)}]:"
            " those routes carry this repo's worker agent label onto the promoted"
            " issue, and leaving it unset fails at promotion time. Set it to a"
            f" worker agent in `agents` (available: {list(config.agents)}), give"
            " each foreign route its own agent_label, or set"
            " tech_lead.findings.promote: off"
        )
    return PromotionLaneReadiness(active=True, problems=tuple(problems))
