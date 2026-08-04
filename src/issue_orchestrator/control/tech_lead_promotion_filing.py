"""The bounded owner of a promotion's filing transaction (#6957).

Filing a promoted finding spans two writes in a fixed order — create the issue
in the routed repo, then record the ledger row — and the crash window between
them carries a fact that only the FIRST write knows: how much evidence the
issue's body documents.

``PromotedFinding.reported_observations`` is a high-water mark. Later evidence
is deduplicated against it: ``select_promotion_updates`` emits a comment only
for observation counts strictly above it. Seeding it from the retrying action
therefore recorded evidence the target was never told about. Concretely, before
this owner existed:

1. a count-2 promotion is created remotely; its ledger write fails;
2. the case file accrues a third observation;
3. the next tick's count-3 action recovers the same remote issue (the marker
   lookup finds it) and records ``reported_observations=3``;
4. the count-3 evidence comment is never emitted — for good. The promoted issue
   claims two observations forever while the ledger claims three.

The adapter cannot fix this on its own: ``PromotionTargetHost.file_issue``
returns an issue number, not the payload that issue was created from (#6957
round-3 review F11/A5). So this owner writes a durable
:class:`PendingPromotion` BEFORE the remote create, carrying the watermark the
body will actually document, and finalizes from THAT — never from the action in
hand. Evidence accrued after it then travels the ordinary
comment-first/advance-watermark path.

Failure direction, deliberately: if the create never happened, the retry files
a body carrying the CURRENT count while the ledger still records the intent's
older watermark, so one evidence comment is repeated. A duplicate comment is
cosmetic; a suppressed one is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.tech_lead_findings import PendingPromotion, PromotedFinding

if TYPE_CHECKING:
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .actions import PromoteTechLeadFindingAction

logger = logging.getLogger(__name__)


class PromotionTargetChangedError(RuntimeError):
    """An in-flight filing targets a different repo than the action in hand.

    A signature's area — and therefore its route — is immutable once recorded,
    so this should be unreachable. If it happens, the already-filed issue lives
    somewhere the current action does not know about, and filing again would
    create a second promotion for one signature. Stop instead.
    """


@dataclass(frozen=True)
class FiledPromotion:
    """The outcome of one filing transaction."""

    issue_number: int
    url: str
    #: True when an in-flight intent was finalized rather than filed fresh.
    recovered: bool


class PromotionFilingOwner:
    """Owns pre-create intent, remote create/recovery, and ledger finalization."""

    def __init__(
        self,
        *,
        authority: "TechLeadAuthorityStore",
        target: "PromotionTargetHost",
    ) -> None:
        self._authority = authority
        self._target = target

    def file(
        self, action: "PromoteTechLeadFindingAction", *, recorded_at: str
    ) -> FiledPromotion:
        """File (or recover) the promotion issue and commit its ledger row.

        Order: intent, then remote create, then ledger row, then retire the
        intent. Every step after the first is idempotent under replay — the
        target's marker lookup returns the already-created issue, and
        ``record_promotion`` is create-once — so the only thing a crash can cost
        is a repeated evidence comment, never a suppressed one.
        """
        pending = self._authority.load_pending_promotion(signature=action.signature)
        if pending is None:
            pending = PendingPromotion(
                signature=action.signature,
                case_file_issue_number=action.case_file_issue_number,
                target_repo=action.target_repo,
                title=action.title,
                idempotency_marker=action.idempotency_marker,
                area=action.area,
                body_observations=action.observation_count,
            )
            self._authority.record_pending_promotion(pending=pending)
        elif pending.target_repo != action.target_repo:
            raise PromotionTargetChangedError(
                f"signature {action.signature!r} has an in-flight promotion to"
                f" {pending.target_repo} but this action targets"
                f" {action.target_repo}; refusing to file a second issue"
            )
        else:
            logger.warning(
                "[tech_lead] Resuming an interrupted promotion filing for"
                " signature %r in %s; its body documents %d observation(s), so"
                " the ledger watermark is set from that, not from this action's"
                " %d",
                action.signature,
                pending.target_repo,
                pending.body_observations,
                action.observation_count,
            )

        filed = self._target.file_issue(
            repo=pending.target_repo,
            title=action.title,
            body=action.body,
            labels=list(action.labels),
            idempotency_marker=action.idempotency_marker,
        )
        self._authority.record_promotion(
            promotion=PromotedFinding(
                signature=action.signature,
                case_file_issue_number=pending.case_file_issue_number,
                target_repo=pending.target_repo,
                target_issue_number=filed.number,
                area=pending.area,
                title=pending.title,
                recorded_at=recorded_at,
                # The watermark the FILED BODY documents — never the retrying
                # action's live count, which the target has not been told.
                reported_observations=pending.body_observations,
            )
        )
        self._authority.discard_pending_promotion(signature=action.signature)
        return FiledPromotion(
            issue_number=filed.number,
            url=filed.url,
            recovered=pending.body_observations != action.observation_count,
        )

    def forget_pending(self, signature: str) -> None:
        """Retire an intent left behind by a crash after the ledger row landed."""
        self._authority.discard_pending_promotion(signature=signature)
