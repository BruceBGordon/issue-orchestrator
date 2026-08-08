"""Composition of the pending-work ledger and everything that reads it (#6999).

Three collaborators that only make sense together: the orchestrator-owned claim
ledger, the quarantine owner that escalates rows it cannot read, and the
shared-``needs-human``-block reader both that owner and the tech-lead lifecycle
consult before touching the label they share.

They are assembled here rather than at each composition root because the wiring
is a correctness contract, not a convenience: the block reader must be given the
SAME claim store the quarantine owner writes to, or it reports "no quarantine
holds this issue" about the very rows that do (#6999 F4). Two roots assembling
that by hand is two chances to get it wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..control.claim_quarantine import ClaimQuarantineOwner, build_claim_quarantine_owner
from ..control.needs_human_block import NeedsHumanBlock
from ..execution.pending_work_claim_store import SqlitePendingWorkClaimStore

if TYPE_CHECKING:
    from ..control.actions import SupportsApplyAction
    from ..control.label_manager import LabelManager
    from ..ports import EventSink
    from ..ports.repository_host import RepositoryHost


@dataclass(frozen=True, slots=True)
class PendingWorkWiring:
    """The pending-work ledger and the two owners that read it."""

    claims: SqlitePendingWorkClaimStore
    quarantine: ClaimQuarantineOwner
    needs_human_block: NeedsHumanBlock


def build_pending_work_wiring(
    *,
    repo_root: Path,
    repository_host: "RepositoryHost",
    action_applier: "SupportsApplyAction",
    label_manager: "LabelManager",
    events: "EventSink",
) -> PendingWorkWiring:
    """Assemble the ledger, its quarantine owner, and the shared-block reader."""
    claims = SqlitePendingWorkClaimStore.for_repo(repo_root)
    needs_human_block = NeedsHumanBlock(
        tech_lead_marker=label_manager.tech_lead_needs_human,
        read_labels=repository_host.get_issue_labels_fresh,
        quarantined_issue_numbers=claims.quarantined_issue_numbers,
    )
    return PendingWorkWiring(
        claims=claims,
        quarantine=build_claim_quarantine_owner(
            store=claims,
            action_applier=action_applier,
            label_manager=label_manager,
            events=events,
            needs_human_block=needs_human_block,
        ),
        needs_human_block=needs_human_block,
    )


__all__ = ["PendingWorkWiring", "build_pending_work_wiring"]
