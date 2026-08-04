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

It also still owns MUTATION POLICY. Every action carries an optional
``ExpectedState``, and ``ActionApplier`` enforces it as a hard optimistic-
concurrency gate before it writes — but a handler that reaches its owner
directly never crosses that gate, so ``Action.expected`` silently meant nothing
on this dispatch branch. A case file paused behind ``io:needs-reconcile`` could
still have a promotion issue filed against it, receive evidence comments, or be
settled (#6957 round-5 review F15/A6).

The first fix wrapped the four commands whose subjects the registry happened to
spell out by hand — and issue CREATION, which is the largest mutation of the
lot (a remote issue, its labels, a ledger row, and anchor comments), kept
sailing past the gate because nobody added it to that list (#6957 round-6
review F3/A3). A hand-maintained subject table is the defect, not the omission.

So the subject is now part of each command's own contract
(:class:`~.tech_lead_actions.TechLeadMutation`), and :func:`_guarded` is applied
to the COMPLETE mutating set — mechanically, from
``TECH_LEAD_MUTATING_ACTION_TYPES``, with a registry-shape guardrail proving no
tech-lead action type can be added to the table on either side by accident. A
mutating command that names no subject may not carry an ``ExpectedState``
either; that combination is a composition bug and fails closed rather than
writing unguarded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .actions import (
    TECH_LEAD_ISSUE_CREATION_ACTION_TYPES,
    Action,
    ActionResult,
    ActionType,
)
from .tech_lead_actions import (
    NO_RECONCILIATION_SUBJECT,
    reconciliation_subject_for,
)
from .tech_lead_case_files import apply_append_pattern_observation
from .tech_lead_finding_promotion import (
    apply_promote_tech_lead_finding,
    apply_report_promoted_finding_evidence,
    apply_settle_tech_lead_promotion,
)
from .tech_lead_proposals import apply_discard_terminal_tech_lead_proposal_ops

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

ActionHandler = Callable[[Action], ActionResult]
#: ``(action, issue_number) -> None``; raises when the mutation must not proceed.
ExpectedStateGuard = Callable[[Action, int], None]

#: Tech-lead action types that WRITE — to GitHub, to the authority ledger, or to
#: a live session. Every one of them is dispatched through :func:`_guarded`.
#: ``SURFACE_TECH_LEAD_PROPOSAL`` is the only tech-lead type deliberately absent:
#: it publishes a trace event and makes no calls at all (ADR-0031).
TECH_LEAD_MUTATING_ACTION_TYPES: frozenset[ActionType] = (
    TECH_LEAD_ISSUE_CREATION_ACTION_TYPES
    | frozenset(
        {
            ActionType.RESET_RETRY_ISSUE,
            ActionType.KILL_HUNG_SESSION,
            ActionType.DISCARD_TERMINAL_TECH_LEAD_PROPOSAL_OPS,
            ActionType.APPEND_PATTERN_OBSERVATION,
            ActionType.PROMOTE_TECH_LEAD_FINDING,
            ActionType.REPORT_PROMOTED_FINDING_EVIDENCE,
            ActionType.SETTLE_TECH_LEAD_PROMOTION,
        }
    )
)


def _guarded(guard: ExpectedStateGuard, handler: ActionHandler) -> ActionHandler:
    """Run *handler* only after the applier's expected-state gate allows it.

    The gate raises (``ReconciliationRequired``) rather than returning, so a
    paused issue produces zero writes: no target-host call, no repository-host
    call, no authority-store row.

    A command with no subject is allowed through ONLY when it also carries no
    expectations — the two creations that author an anchor issue, and the
    ledger-only discard. A command that states expectations it has nothing to
    check them against is refused before any write: that pairing means the
    subject was dropped somewhere in composition, and running it unguarded is
    exactly the bug this wrapper exists to make impossible.

    This registry-level check is a BACKSTOP, not the primary defence. The
    creation commands now make the invalid state unrepresentable at
    construction (``TechLeadCreationOrigin``, #6957 round-2 review F6/A6),
    because a wrapper that only rejects "expectations without a subject" still
    waves through a follow-up whose subject AND expectations were both dropped —
    it looks exactly like legitimate anchor authoring from here. Commands that
    have no such value object (the ledger-only discard) are still covered here.
    """

    def run(action: Action) -> ActionResult:
        subject = reconciliation_subject_for(action)
        if subject == NO_RECONCILIATION_SUBJECT:
            if action.expected is not None:
                raise ValueError(
                    f"{type(action).__name__} carries an ExpectedState but names"
                    " no reconciliation subject; refusing to mutate unguarded"
                )
            return handler(action)
        guard(action, subject)
        return handler(action)

    return run


def tech_lead_action_handlers(
    *,
    create_tech_lead_issue: ActionHandler,
    surface_proposal: ActionHandler,
    reset_retry: ActionHandler,
    kill_hung_session: ActionHandler,
    require_expected: ExpectedStateGuard,
    repository_host: "RepositoryHost | None",
    authority: "TechLeadAuthorityStore | None",
    promotion_target: "PromotionTargetHost | None",
) -> dict[ActionType, ActionHandler]:
    """Map every tech-lead ActionType to the owner that applies it."""
    handlers: dict[ActionType, ActionHandler] = {
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
        ActionType.APPEND_PATTERN_OBSERVATION: lambda action: (
            apply_append_pattern_observation(
                action, repository_host=repository_host, authority=authority
            )
        ),
        # Finding promotion: file in the routed repo, then close the loop
        # (#6957). All three reconcile against the SOURCE repo's case file —
        # the promoted issue lives elsewhere, outside this reconciliation model.
        ActionType.PROMOTE_TECH_LEAD_FINDING: lambda action: (
            apply_promote_tech_lead_finding(
                action, target=promotion_target, authority=authority
            )
        ),
        ActionType.REPORT_PROMOTED_FINDING_EVIDENCE: lambda action: (
            apply_report_promoted_finding_evidence(
                action, target=promotion_target, authority=authority
            )
        ),
        ActionType.SETTLE_TECH_LEAD_PROMOTION: lambda action: (
            apply_settle_tech_lead_promotion(
                action, repository_host=repository_host, authority=authority
            )
        ),
    }
    unknown = TECH_LEAD_MUTATING_ACTION_TYPES - handlers.keys()
    if unknown:
        raise ValueError(
            "tech-lead mutating action type(s) have no handler:"
            f" {sorted(item.value for item in unknown)}"
        )
    return {
        action_type: (
            _guarded(require_expected, handler)
            if action_type in TECH_LEAD_MUTATING_ACTION_TYPES
            else handler
        )
        for action_type, handler in handlers.items()
    }
