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

Finding an intent already in the store therefore means exactly one thing: a
previous attempt stopped somewhere inside that sequence. Which side of the
remote create it stopped on is the only question that matters, and it is settled
BEFORE anything is created, by the recovery-only lookup in the intent's own repo
— the issue exists (it IS the promotion), is proven absent (that create never
happened), or cannot be read (propagate; create nothing). Asking it with a
bounded, title-scoped search would answer "absent" for an issue that had merely
aged out or been retitled, and file a second issue for a signature that already
has one, so this path requires the AUTHORITATIVE lookup (#6957 round-5 F13).

``tech_lead.findings.route`` is ordinary user-editable configuration, so an
operator can also re-point an area between ticks while a filing is in flight.
That does not change the question above — it only changes what a proven absence
permits: with the route unchanged the intent stands and is filed under, and with
it re-pointed the stale intent is retired so the current route can take over. A
found issue outranks both: one signature promotes exactly once.
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


@dataclass(frozen=True)
class FiledPromotion:
    """The outcome of one filing transaction."""

    issue_number: int
    url: str
    #: True when the remote issue already existed and was recovered, False when
    #: this call created it. Reported by the target port, not inferred.
    recovered: bool
    #: The repo the promotion actually landed in. Differs from the action's when
    #: an interrupted filing in a previously-configured route was recovered.
    target_repo: str


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
        intent. An intent already in the store means a previous attempt was
        interrupted inside that sequence, so it is settled against its own repo
        FIRST — no create may precede that answer, and only the authoritative
        lookup's "absent" is an answer at all (#6957 round-5 review F13).
        """
        pending = self._authority.load_pending_promotion(signature=action.signature)
        if pending is not None:
            settled = self._settle_interrupted_filing(
                action, pending, recorded_at=recorded_at
            )
            if settled is not None:
                return settled
            pending = self._intent_after_proven_absence(action, pending)
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

        filed = self._target.file_issue(
            repo=pending.target_repo,
            title=action.title,
            body=action.body,
            labels=list(action.labels),
            idempotency_marker=action.idempotency_marker,
        )
        return self._commit(
            pending,
            issue_number=filed.number,
            url=filed.url,
            recovered=filed.recovered,
            recorded_at=recorded_at,
        )

    def _settle_interrupted_filing(
        self,
        action: "PromoteTechLeadFindingAction",
        pending: "PendingPromotion",
        *,
        recorded_at: str,
    ) -> FiledPromotion | None:
        """Did the interrupted attempt's remote create happen? Settle it if so.

        Exactly one of three things can be true, and the lookup is chosen so
        that all three are distinguishable:

        * the issue EXISTS — it IS the promotion, whatever the route says now.
          One signature promotes to exactly one issue ever, so finalize the
          ledger from the intent and return it.
        * it is PROVEN ABSENT — that create never happened, nothing is orphaned.
          Return None so the caller files, under the intent the next step
          selects.
        * the lookup FAILS — "unknown", never "absent", so it propagates and
          nothing is created. The next tick asks again.

        The third outcome is why this uses the AUTHORITATIVE lookup: a bounded,
        title-scoped search collapses "unknown" into "absent" for an issue that
        aged out of the recent window or was retitled, and filing on that
        answer creates a second issue for a signature that already has one
        (#6957 round-5 review F13).
        """
        found = self._target.find_filed_issue(
            repo=pending.target_repo,
            title=pending.title,
            idempotency_marker=pending.idempotency_marker,
        )
        if found is None:
            return None
        if pending.target_repo != action.target_repo:
            logger.warning(
                "[tech_lead] Signature %r was re-routed from %s to %s, but its"
                " interrupted filing had already created %s#%d. That issue IS"
                " the promotion — one signature promotes exactly once — so the"
                " recorded target stays %s and the new route does not apply",
                action.signature,
                pending.target_repo,
                action.target_repo,
                pending.target_repo,
                found.number,
                pending.target_repo,
            )
        else:
            logger.warning(
                "[tech_lead] An interrupted filing for signature %r had already"
                " created %s#%d; recovering it instead of creating a second"
                " issue, with the ledger watermark set from the %d"
                " observation(s) its body documents, not this action's %d",
                action.signature,
                pending.target_repo,
                found.number,
                pending.body_observations,
                action.observation_count,
            )
        return self._commit(
            pending,
            issue_number=found.number,
            url=found.url,
            recovered=True,
            recorded_at=recorded_at,
        )

    def _intent_after_proven_absence(
        self,
        action: "PromoteTechLeadFindingAction",
        pending: "PendingPromotion",
    ) -> "PendingPromotion | None":
        """The intent to file under, once the old create is known not to exist.

        The intent is deliberately durable across restarts while
        ``tech_lead.findings.route`` is ordinary user-editable configuration, so
        an operator can re-point an area between ticks (#6957 round-4 review
        F12). With nothing filed, that re-point is free to take effect: retire
        the stale intent and return None so a fresh one is recorded against the
        current route. Refusing instead stranded the signature forever — every
        later tick rebuilt the same action and hit the same mismatch.

        With the route unchanged there is nothing stale about it, so the intent
        stands and its older watermark keeps ruling the ledger row.
        """
        if pending.target_repo != action.target_repo:
            logger.warning(
                "[tech_lead] Signature %r was re-routed from %s to %s while a"
                " filing was in flight, and no issue carries its marker in %s —"
                " that create never happened. Discarding the stale intent and"
                " filing against the current route",
                action.signature,
                pending.target_repo,
                action.target_repo,
                pending.target_repo,
            )
            self._authority.discard_pending_promotion(signature=action.signature)
            return None
        logger.warning(
            "[tech_lead] Resuming an interrupted promotion filing for signature"
            " %r in %s; its create is proven not to have happened, so the body"
            " filed now documents %d observation(s) while the ledger keeps the"
            " intent's %d — at worst one evidence comment is repeated, which is"
            " the direction this fails in deliberately",
            action.signature,
            pending.target_repo,
            action.observation_count,
            pending.body_observations,
        )
        return pending

    def _commit(
        self,
        pending: "PendingPromotion",
        *,
        issue_number: int,
        url: str,
        recovered: bool,
        recorded_at: str,
    ) -> FiledPromotion:
        """Record the ledger row from the INTENT and retire it."""
        self._authority.record_promotion(
            promotion=PromotedFinding(
                signature=pending.signature,
                case_file_issue_number=pending.case_file_issue_number,
                target_repo=pending.target_repo,
                target_issue_number=issue_number,
                area=pending.area,
                title=pending.title,
                recorded_at=recorded_at,
                # The watermark the FILED BODY documents — never the retrying
                # action's live count, which the target has not been told.
                reported_observations=pending.body_observations,
            )
        )
        self._authority.discard_pending_promotion(signature=pending.signature)
        return FiledPromotion(
            issue_number=issue_number,
            url=url,
            recovered=recovered,
            target_repo=pending.target_repo,
        )

    def forget_pending(self, signature: str) -> None:
        """Retire an intent left behind by a crash after the ledger row landed."""
        self._authority.discard_pending_promotion(signature=signature)
