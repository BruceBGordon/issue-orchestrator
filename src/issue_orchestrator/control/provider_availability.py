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
from typing import Iterable

from typing import TYPE_CHECKING

from ..ports.issue import Issue
from ..infra.config import Config
from .actions import Action
from .provider_impact import ApplyProviderImpactAction, ProviderImpactTransition
from .provider_resilience import ProviderResilienceManager
from .reconciliation import build_expected_for_mutation

if TYPE_CHECKING:
    from .label_manager import LabelManager
    from .planner_types import OrchestratorSnapshot, PlanContext, SkippedItem

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderAvailabilityPolicy:
    config: Config
    provider_resilience: ProviderResilienceManager
    label_manager: "LabelManager | None" = None

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

    def is_open(self, provider: str | None) -> bool:
        if not provider:
            return False
        return self.provider_resilience.is_open(provider)

    def any_open(self, providers: Iterable[str]) -> bool:
        return any(self.is_open(provider) for provider in providers)

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

    def blocked_transition(
        self,
        issue_number: int,
        providers: Iterable[str],
        *,
        issue_key: str = "",
    ) -> ApplyProviderImpactAction:
        ordered = tuple(sorted(providers))
        next_retry_at, cooldown = self._soonest_retry(ordered)
        return ApplyProviderImpactAction(
            issue_number=issue_number,
            transition=ProviderImpactTransition.BLOCKED,
            label=self.blocked_label(),
            providers=ordered,
            next_retry_at=next_retry_at,
            cooldown_remaining_seconds=cooldown,
            issue_key=issue_key,
            reason=f"provider unavailable: {', '.join(ordered)}",
            expected=build_expected_for_mutation(),
        )

    def cleared_transition(
        self,
        issue_number: int,
        providers: Iterable[str],
        *,
        issue_key: str = "",
    ) -> ApplyProviderImpactAction:
        ordered = tuple(sorted(providers))
        return ApplyProviderImpactAction(
            issue_number=issue_number,
            transition=ProviderImpactTransition.CLEARED,
            label=self.blocked_label(),
            providers=ordered,
            issue_key=issue_key,
            reason=f"provider available: {', '.join(ordered)}",
            expected=build_expected_for_mutation(),
        )

    def _soonest_retry(self, providers: Iterable[str]) -> tuple[str | None, int | None]:
        """When the soonest still-open circuit next allows a retry.

        ``(None, None)`` when every named circuit is merely recovering (cooldown
        elapsed, no successful call yet) — there is no retry window to advertise.
        """
        statuses = [
            status
            for status in (self.provider_resilience.status(p) for p in providers)
            if status is not None and status.is_open
        ]
        if not statuses:
            return None, None
        soonest = min(statuses, key=lambda s: s.cooldown_remaining_seconds)
        open_until = soonest.open_until
        return (
            open_until.isoformat() if open_until is not None else None,
            soonest.cooldown_remaining_seconds,
        )

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
        actions.append(self.blocked_transition(issue_number, (provider,)))
        plan_context.record_add(issue_number, self.blocked_label())

    def plan_provider_impact(
        self,
        snapshot: "OrchestratorSnapshot",
        plan_context: "PlanContext",
    ) -> list[Action]:
        """Plan provider-impact transitions for every in-scope issue."""
        actions: list[Action] = []
        label = self.blocked_label()
        providers_by_issue = self.providers_for_snapshot(snapshot)
        for issue in snapshot.issues:
            providers = providers_by_issue.get(issue.number, set())
            if not providers:
                continue
            any_open = self.any_open(providers)
            issue_labels = plan_context.issue_labels(issue.number)
            planned_labels = plan_context.planned_adds(issue.number)
            issue_key = issue.key.stable_id()
            if any_open and self.should_add_blocked_label(issue_labels, planned_labels):
                actions.append(
                    self.blocked_transition(issue.number, providers, issue_key=issue_key)
                )
                plan_context.record_add(issue.number, label)
            if (
                not any_open
                and self.should_remove_blocked_label(issue_labels, planned_labels)
                and plan_context.should_remove_label(issue.number, label)
            ):
                actions.append(
                    self.cleared_transition(issue.number, providers, issue_key=issue_key)
                )
                plan_context.record_remove(issue.number, label)
        return actions
