"""One planning owner for everything the tech-lead durable ledgers drive.

Three tech-lead lanes are ledger-driven rather than scan-driven, and all three
were being assembled inline in the planner's main loop:

* **Approved gated proposals** (#6778) — the operator removed the
  ``proposed-tech-lead`` label, so the fact scan classified the stored op as
  approved; the appliers re-validate preconditions and finalize the proposal.
* **Terminal-op cleanup candidates** (#6779 R7/R10) — fact gathering only
  CLASSIFIED ledger rows absent from the exhaustive scan (it stays read-only),
  so the applier must re-read each proposal issue before discarding: absence
  from a possibly-truncated scan must never delete a live op.
* **Finding promotion and settlement** (#6957) — eligible pattern case files
  become gated runnable issues in the repo their area routes to, and promotions
  the target repo has already settled close the loop.

They share one precondition (a ``TechLeadFacts`` snapshot the gatherer produced
read-only) and one contract (translate typed facts to actions, decide nothing),
so they share one owner. The planner asks this module once instead of carrying
three inline blocks that each re-check ``tech_lead_facts``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .actions import Action, DiscardTerminalTechLeadProposalOpsAction
from .tech_lead_finding_promotion import plan_finding_promotion_actions
from .tech_lead_proposals import plan_approved_tech_lead_op_executions

if TYPE_CHECKING:
    from ..domain.models import TechLeadFacts
    from ..infra.config import Config


def plan_tech_lead_ledger_actions(
    config: "Config", facts: "TechLeadFacts | None"
) -> list[Action]:
    """Every ledger-driven tech-lead action for one tick.

    Read-free: each fact set already encodes its lane's policy decisions, so
    this only translates them. No facts (a tick where nothing tech-lead was
    armed) plans nothing.
    """
    if facts is None:
        return []
    actions: list[Action] = list(
        plan_approved_tech_lead_op_executions(facts.approved_tech_lead_ops)
    )
    if facts.absent_proposal_op_candidates:
        actions.append(
            DiscardTerminalTechLeadProposalOpsAction(
                candidate_issue_numbers=facts.absent_proposal_op_candidates
            )
        )
    actions.extend(plan_finding_promotion_actions(config, facts))
    return actions
