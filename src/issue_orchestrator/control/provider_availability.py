"""Provider availability policy (shared owner for planner and launcher).

Owns every "is this issue affected by a provider outage, and what should the
orchestrator do about it" decision: which providers an issue depends on,
whether their circuits are open, and the typed provider-impact transition that
carries the blocked-label mutation *and* its durable issue-scoped record.

Call sites ask this owner for actions; they never assemble the label mutation
themselves, so the label and the history record cannot drift apart (#5980 F1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from typing import TYPE_CHECKING

from ..ports.issue import Issue
from ..ports.provider_readiness import (
    NO_PROVIDER_READINESS_PROBE,
    ProviderReadiness,
    ProviderReadinessProbe,
)
from ..infra.config import Config
from .actions import Action
from .provider_impact import (
    ApplyProviderImpactAction,
    ProviderImpactAssessment,
    ProviderImpactTransition,
)
from .provider_resilience import ProviderResilienceManager
from .reconciliation import build_expected_for_mutation

if TYPE_CHECKING:
    from .label_manager import LabelManager
    from .planner_types import OrchestratorSnapshot, PlanContext, SkippedItem

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProviderLaunchOutcome:
    """The one typed answer to "may I launch against this provider?" (#6999 A1).

    Carries both halves of the question so no consumer has to re-ask either:
    what the provider's own credential probe said, and whether the circuit
    owner is holding launches back. Planning and launch control consume the
    same value, which is why they cannot drift apart on eligibility.
    """

    provider: str
    readiness: ProviderReadiness
    circuit_open: bool

    @property
    def may_launch(self) -> bool:
        """Whether a session may be spawned for this provider right now."""
        return self.readiness.launchable and not self.circuit_open

    @property
    def blocked_by_credentials(self) -> bool:
        """Whether the provider itself refused — only a human can clear this."""
        return not self.readiness.launchable

    @classmethod
    def no_provider(cls) -> "ProviderLaunchOutcome":
        """The outcome for work that names no provider: nothing to gate on."""
        return cls(
            provider="",
            readiness=ProviderReadiness.unknown("", "no provider configured"),
            circuit_open=False,
        )


@dataclass(frozen=True)
class ProviderAvailabilityPolicy:
    config: Config
    provider_resilience: ProviderResilienceManager
    label_manager: "LabelManager | None" = None
    readiness_probe: ProviderReadinessProbe = NO_PROVIDER_READINESS_PROBE

    def blocked_label(self) -> str:
        if self.label_manager is not None:
            return self.label_manager.provider_unavailable
        # Deprecated fallback — callers should provide label_manager
        from .label_manager import LabelManager
        return LabelManager(self.config).provider_unavailable

    def provider_for_agent_label(self, agent_label: str | None) -> str | None:
        if not agent_label:
            return None
        agent_config = self.config.agents.get(agent_label)
        if not agent_config:
            return None
        return agent_config.provider

    def provider_for_issue(self, issue: Issue) -> str | None:
        return self.provider_for_agent_label(issue.agent_type)

    def providers_for_snapshot(self, snapshot: OrchestratorSnapshot) -> dict[int, set[str]]:
        providers_by_issue: dict[int, set[str]] = {}

        for issue in snapshot.issues:
            provider = self.provider_for_issue(issue)
            if provider:
                providers_by_issue.setdefault(issue.number, set()).add(provider)

        for review in snapshot.pending_reviews:
            reviewer_label = self.config.get_reviewer_for_agent(review.agent_label) if review.agent_label else self.config.code_review_agent
            provider = self.provider_for_agent_label(reviewer_label)
            if provider:
                providers_by_issue.setdefault(review.issue_number, set()).add(provider)

        for rework in snapshot.pending_reworks:
            issue_num = rework.resolve_issue_number()
            if issue_num is None:
                continue
            provider = self.provider_for_agent_label(rework.agent_type)
            if provider:
                providers_by_issue.setdefault(issue_num, set()).add(provider)

        tech_lead_provider = self.provider_for_agent_label(self.config.tech_lead_review_agent)
        if tech_lead_provider:
            for tech_lead in snapshot.pending_tech_lead:
                providers_by_issue.setdefault(tech_lead.issue_number, set()).add(tech_lead_provider)

        return providers_by_issue

    def circuit_is_open(self, provider: str | None) -> bool:
        """Raw circuit read — "does the resilience owner still hold this?".

        Deliberately NOT a launch gate: it takes no readiness sample, so on its
        own it can never observe a human re-authenticating and would keep the
        fleet parked for the whole auth cooldown (#6999 F1). Launch paths call
        :meth:`assess_launch` instead (via the per-tick sampler). This remains for
        the readers that genuinely ask about circuit *ownership* rather than
        eligibility, such as the tech-lead stuck sweep.

        Also deliberately NOT used to decide what a provider-impact record says:
        anything that ends up in an issue's history goes through :meth:`assess`,
        so the record cannot describe a different instant or a wider set of
        providers than the decision that produced it (#5980 F4/A2).
        """
        if not provider:
            return False
        return self.provider_resilience.is_open(provider)

    # ------------------------------------------------------------------
    # Bounded provider launch assessment (#6999 A1)
    #
    # ONE answer to "may I launch against this provider right now?". Both
    # halves of the question — the credential sample and the circuit — are
    # resolved here, in that order, and the sample's circuit consequence is
    # recorded exactly once. Callers are the per-tick sampler (whose result
    # planning reads as a fact) and the launch-time gate. No call site reads
    # the raw circuit to make a launch decision, so an open auth circuit can
    # never suppress the very probe that would retire it.
    # ------------------------------------------------------------------

    def assess_launch(
        self, provider: str | None, *, now: datetime | None = None
    ) -> ProviderLaunchOutcome:
        """Take one readiness sample, feed the circuit, and read the result.

        Order matters and is the whole fix: the probe runs *before* the circuit
        is consulted. While an auth circuit is open no session runs, so a gate
        that short-circuited on the open circuit could never observe the human
        re-authenticating (#6999 F1). The circuit is then read *after* the
        sample is recorded, so this one outcome reflects the sample it was
        derived from.

        Both directions are reported to
        :class:`~.provider_resilience.ProviderResilienceManager` here rather
        than at the call sites, so the circuit is the only thing that decides
        how many failures are tolerated, how long launches stay paused, and when
        an outage is over. Control receives only the typed outcome — never a
        banner or exit code.
        """
        if not provider:
            return ProviderLaunchOutcome.no_provider()
        readiness = self.readiness_probe.check_launch_readiness(provider)
        if readiness.human_fixable:
            self.provider_resilience.record_auth_failure(
                provider,
                error_summary=readiness.detail or "provider is not authenticated",
                sample_id=readiness.sample_id,
                now=now,
            )
        elif readiness.authenticated:
            self.provider_resilience.clear_auth_failures(provider, now=now)
        return ProviderLaunchOutcome(
            provider=provider,
            readiness=readiness,
            circuit_open=self.provider_resilience.is_open(provider, now),
        )

    def should_add_blocked_label(self, issue_labels: Iterable[str], planned_labels: set[str]) -> bool:
        label = self.blocked_label()
        return label not in issue_labels and label not in planned_labels

    def should_remove_blocked_label(self, issue_labels: Iterable[str], planned_labels: set[str]) -> bool:
        label = self.blocked_label()
        return label in issue_labels and label not in planned_labels

    # ------------------------------------------------------------------
    # Provider-impact transitions (#5980)
    #
    # The only supported way to move an issue's provider-blocked label. The
    # returned command carries the durable issue-scoped record with it, so a
    # caller cannot apply the label and forget the history.
    # ------------------------------------------------------------------

    def assess(
        self,
        providers: Iterable[str],
        *,
        now: datetime | None = None,
    ) -> ProviderImpactAssessment:
        """Read every named circuit once, at one instant.

        The single source of provider truth for a transition: the label
        decision (``assessment.blocked``), which providers are actually to
        blame, the retry window, and the history wording all come from this one
        read, so they cannot describe different moments or different providers
        (#5980 F4/A2). ``now`` is resolved once here and passed to every circuit
        read — never re-derived per provider.
        """
        assessed_at = now or _now()
        return ProviderImpactAssessment.from_statuses(
            assessed_at,
            (
                (provider, self.provider_resilience.status(provider, assessed_at))
                for provider in sorted(providers)
            ),
        )

    def blocked_transition(
        self,
        issue_number: int,
        assessment: ProviderImpactAssessment,
        *,
        issue_key: str = "",
    ) -> ApplyProviderImpactAction:
        """Command for "this issue is blocked by an open provider circuit".

        Only the actually-open providers reach the record; the action rejects an
        assessment with no open circuit rather than recording an empty outage.
        """
        return ApplyProviderImpactAction(
            issue_number=issue_number,
            transition=ProviderImpactTransition.BLOCKED,
            label=self.blocked_label(),
            assessment=assessment,
            issue_key=issue_key,
            reason=f"provider unavailable: {', '.join(assessment.open_providers)}",
            expected=build_expected_for_mutation(),
        )

    def cleared_transition(
        self,
        issue_number: int,
        assessment: ProviderImpactAssessment,
        *,
        issue_key: str = "",
    ) -> ApplyProviderImpactAction:
        """Command for "no circuit is open; release this issue".

        The assessment decides whether this is a confirmed-healthy release or a
        mere cooldown expiry, so the history never claims recovery the circuit
        owner has not observed.
        """
        action = ApplyProviderImpactAction(
            issue_number=issue_number,
            transition=ProviderImpactTransition.CLEARED,
            label=self.blocked_label(),
            assessment=assessment,
            issue_key=issue_key,
            reason=(
                f"provider {assessment.release_kind.value}: "
                f"{', '.join(assessment.assessed_providers)}"
            ),
            expected=build_expected_for_mutation(),
        )
        return action

    # ------------------------------------------------------------------
    # Planning (moved out of Planner: provider policy has one owner)
    # ------------------------------------------------------------------

    def record_provider_skip(
        self,
        *,
        issue_number: int,
        item_type: str,
        item_number: int,
        provider: str,
        actions: list[Action],
        skipped: "list[SkippedItem]",
        plan_context: "PlanContext",
        now: datetime | None = None,
    ) -> None:
        """Record a launch skipped because ``provider``'s circuit is open."""
        from .planner_types import SkippedItem

        skipped.append(SkippedItem(
            item_type=item_type,
            number=item_number,
            reason=f"provider unavailable: {provider}",
        ))
        logger.info(
            "[issue #%s] Skipped: reason=provider_unavailable provider=%s",
            issue_number,
            provider,
        )
        issue_labels = plan_context.issue_labels(issue_number)
        planned_labels = plan_context.planned_adds(issue_number)
        if not self.should_add_blocked_label(issue_labels, planned_labels):
            return
        assessment = self.assess((provider,), now=now)
        if not assessment.blocked:
            # The cooldown elapsed between the skip decision and this read; the
            # item stays skipped for this tick, but blocking the issue (and
            # recording an outage) would no longer be true.
            return
        actions.append(self.blocked_transition(issue_number, assessment))
        plan_context.record_add(issue_number, self.blocked_label())

    def plan_provider_impact(
        self,
        snapshot: "OrchestratorSnapshot",
        plan_context: "PlanContext",
        now: datetime | None = None,
    ) -> list[Action]:
        """Plan provider-impact transitions for every in-scope issue.

        ``now`` fixes the instant every circuit in this pass is read at, so a
        single planning cycle cannot mix reads from different moments.
        """
        actions: list[Action] = []
        label = self.blocked_label()
        assessed_at = now or _now()
        providers_by_issue = self.providers_for_snapshot(snapshot)
        for issue in snapshot.issues:
            providers = providers_by_issue.get(issue.number, set())
            if not providers:
                continue
            assessment = self.assess(providers, now=assessed_at)
            issue_labels = plan_context.issue_labels(issue.number)
            planned_labels = plan_context.planned_adds(issue.number)
            issue_key = issue.key.stable_id()
            if assessment.blocked and self.should_add_blocked_label(
                issue_labels, planned_labels
            ):
                actions.append(
                    self.blocked_transition(issue.number, assessment, issue_key=issue_key)
                )
                plan_context.record_add(issue.number, label)
            if (
                not assessment.blocked
                and self.should_remove_blocked_label(issue_labels, planned_labels)
                and plan_context.should_remove_label(issue.number, label)
            ):
                actions.append(
                    self.cleared_transition(issue.number, assessment, issue_key=issue_key)
                )
                plan_context.record_remove(issue.number, label)
        return actions
