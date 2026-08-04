"""Apply-time dispatch table for every tech-lead action type.

The tech-lead surface now spans issue creation (#6761/#6778/#6781), act-level
op execution (#6764/#6778), ledger cleanup (#6779), and the finding-promotion
lane (#6957). Each has its own extracted apply-time owner, and the applier's
job for all of them is identical: hand the action to that owner with the ports
it needs. Assembling that mapping here keeps the applier's dispatch table a
single entry, and keeps "which owner runs which tech-lead action" in one place
instead of scattered across the table.

The applier still owns the four handlers that need its own collaborators
(sessions, events, expedite lane); they are passed in rather than reached for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .actions import (
    TECH_LEAD_ISSUE_CREATION_ACTION_TYPES,
    Action,
    ActionResult,
    ActionType,
)
from .tech_lead_case_files import apply_append_pattern_observation
from .tech_lead_finding_promotion import (
    apply_promote_tech_lead_finding,
    apply_settle_tech_lead_promotion,
)
from .tech_lead_proposals import apply_discard_terminal_tech_lead_proposal_ops

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

ActionHandler = Callable[[Action], ActionResult]


def tech_lead_action_handlers(
    *,
    create_tech_lead_issue: ActionHandler,
    surface_proposal: ActionHandler,
    reset_retry: ActionHandler,
    kill_hung_session: ActionHandler,
    repository_host: "RepositoryHost | None",
    authority: "TechLeadAuthorityStore | None",
    promotion_target: "PromotionTargetHost | None",
) -> dict[ActionType, ActionHandler]:
    """Map every tech-lead ActionType to the owner that applies it."""
    return {
        # All tech-lead-authored issues share one apply-time creation owner.
        **dict.fromkeys(TECH_LEAD_ISSUE_CREATION_ACTION_TYPES, create_tech_lead_issue),
        # Decision proposals: event-only surfacing, no GitHub calls (ADR-0031).
        ActionType.SURFACE_TECH_LEAD_PROPOSAL: surface_proposal,
        # Act-level execution via the reset (#6764) / termination (#6778) owners.
        ActionType.RESET_RETRY_ISSUE: reset_retry,
        ActionType.KILL_HUNG_SESSION: kill_hung_session,
        # Confirm-and-discard terminal gated-proposal ledger rows (#6779 R7/R10).
        ActionType.DISCARD_TERMINAL_TECH_LEAD_PROPOSAL_OPS: (
            lambda action: apply_discard_terminal_tech_lead_proposal_ops(
                action, tracker=repository_host, authority=authority
            )
        ),
        # Repeat pattern observation: evidence comment + durable count (#6957).
        ActionType.APPEND_PATTERN_OBSERVATION: (
            lambda action: apply_append_pattern_observation(
                action, repository_host=repository_host, authority=authority
            )
        ),
        # Finding promotion: file in the routed repo, then close the loop (#6957).
        ActionType.PROMOTE_TECH_LEAD_FINDING: (
            lambda action: apply_promote_tech_lead_finding(
                action, target=promotion_target, authority=authority
            )
        ),
        ActionType.SETTLE_TECH_LEAD_PROMOTION: (
            lambda action: apply_settle_tech_lead_promotion(
                action, repository_host=repository_host, authority=authority
            )
        ),
    }
