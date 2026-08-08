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
    from ..control.action_applier import ActionApplier
    from ..control.governed_label_set import LabelWriter
    from ..control.label_manager import LabelManager
    from ..ports import EventSink
    from ..ports.repository_host import RepositoryHost


def require_repository_host(
    github: "RepositoryHost | None",
) -> "RepositoryHost":
    """The repository host, or the operator-facing error explaining its absence.

    Hoisted out of :func:`_validate_required_deps` so the same message can be
    raised at the first point that genuinely needs a host, rather than letting
    a downstream ``None`` surface as an AttributeError. The shared-block owner
    is that point: it reads live labels and writes the governed label, so it
    cannot be built without one (#6999 F2 round 4).
    """
    if github is not None:
        return github
    raise ValueError(
        "Could not determine GitHub repository.\n\n"
        "Either:\n"
        "  1. Set 'repo.name' in your config file:\n"
        "       repo:\n"
        "         name: owner/repo-name\n\n"
        "  2. Or ensure you're running from a git repo with a GitHub remote:\n"
        "       git remote get-url origin\n"
        "       # Should show: https://github.com/owner/repo.git"
    )



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
    action_applier: "ActionApplier",
    # The RAW label writer. The owner is the one holder that may write the
    # governed label, so it deliberately does NOT receive the guarded
    # capability every other writer gets (#6999 F2 round 4).
    label_writer: "LabelWriter",
    label_manager: "LabelManager",
    events: "EventSink",
) -> PendingWorkWiring:
    """Assemble the ledger, its quarantine owner, and the shared-block reader."""
    claims = SqlitePendingWorkClaimStore.for_repo(repo_root)
    needs_human_block = NeedsHumanBlock(
        needs_human_label=label_manager.needs_human,
        labels=label_writer,
        tech_lead_marker=label_manager.tech_lead_needs_human,
        read_labels=repository_host.get_issue_labels_fresh,
        quarantined_issue_numbers=claims.quarantined_issue_numbers,
        causes=claims,
    )
    # The applier is the single seam every label mutation passes through, so it
    # is where an acquisition records its cause and a removal withdraws one
    # (#6999 F2 round 2). Bound here, post-construction, because the applier is
    # also what the quarantine owner applies THROUGH - and because the binding
    # is a correctness contract, not a convenience: the applier must write into
    # the very store this block reads, or a recorded cause is invisible to the
    # remover it exists to stop.
    action_applier.needs_human_block = needs_human_block
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


__all__ = [
    "PendingWorkWiring",
    "build_pending_work_wiring",
    "require_repository_host",
]
