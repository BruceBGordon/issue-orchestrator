"""What a provider outage means for the session it stopped (#6999 F5/F2).

Extracted from :mod:`completion_action_planner`: a provider-caused block is a
different KIND of completion from an agent-reported one - it says nothing about
the issue's substance - and it needs its own collaborators (the availability
policy that owns the circuit assessment, plus the label vocabulary). Keeping it
beside the twelve other completion-status branches buried that distinction and
pushed the planner past its line budget.
"""

from __future__ import annotations

from ..domain.models import Session
from .actions import Action, AddLabelAction, RemoveLabelAction
from .label_manager import LabelManager
from .provider_availability import ProviderAvailabilityPolicy
from .reconciliation import ExpectedState


def provider_blocked_actions(
    session: Session,
    expected: ExpectedState,
    *,
    label_manager: LabelManager,
    provider_availability: ProviderAvailabilityPolicy,
) -> list[Action]:
    """Actions for a session blocked by its provider, not by its work.

    The provider-blocked label and the durable issue-scoped record are two
    halves of one transition, and ``ApplyProviderImpactAction`` is the only
    thing that keeps them together (#5980 F1). Generic blocked handling would
    apply a bare ``AddLabelAction``, after which the impact planner sees the
    label already present and has no transition left to record - the outage
    would vanish from the issue's history (#6999 F5).

    Unlike the generic path this rule does NOT differ by session kind: a review
    or rework session stalled by a dead credential impacts its issue exactly as
    a coding session does, so every kind gets the transition. The claim release
    stays kind-scoped, because holding the issue is a property of the coding
    session, not of the outage.

    No comment is posted. The impact command emits an issue-scoped
    ``provider.issue_blocked`` event that survives the label being shed, which
    is precisely the durable signal #5980 added to replace commenting on every
    affected issue during a fleet-wide outage.

    A rework session additionally gets its ``needs-rework`` trigger back. The
    in-memory queue restore is owned by ``InFlightWorkLedger`` (#6999 F2), but
    the label is the crash-safe half of the same fact: the launcher strips it
    when the session starts, so leaving it off would mean an orchestrator
    restarted during the outage sees a PR that asked for rework and no longer
    says so.
    """
    actions: list[Action] = []
    provider = session.agent_config.provider
    if provider:
        assessment = provider_availability.assess((provider,))
        if assessment.blocked:
            actions.append(
                provider_availability.blocked_transition(
                    session.issue.number,
                    assessment,
                    issue_key=session.issue.key.stable_id(),
                )
            )
    if session.terminal_id.startswith("issue-"):
        actions.append(
            RemoveLabelAction(
                issue_number=session.issue.number,
                label=label_manager.in_progress,
                reason="Session blocked by provider - releasing claim",
                expected=expected,
            )
        )
    if session.terminal_id.startswith("rework-") and session.pr_number:
        actions.append(
            AddLabelAction(
                issue_number=session.pr_number,
                label=label_manager.needs_rework,
                reason="Rework blocked by provider - restoring rework trigger",
                expected=expected,
            )
        )
    return actions
