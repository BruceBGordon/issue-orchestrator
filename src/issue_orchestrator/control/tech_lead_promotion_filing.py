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

The owner also settles the one transition the intent's durability creates:
``tech_lead.findings.route`` is ordinary user-editable configuration, so an
operator can re-point an area between ticks while a filing is in flight. A
recovery-only lookup in the intent's own repo decides it — the old issue exists
(it IS the promotion; the old target stays authoritative), is proven absent (the
stale intent is retired and the current route takes over), or cannot be read
(propagate; create nothing). See :meth:`PromotionFilingOwner._reconcile_route_change`.
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
        intent. Every step after the first is idempotent under replay — the
        target's marker lookup returns the already-created issue, and
        ``record_promotion`` is create-once — so the only thing a crash can cost
        is a repeated evidence comment, never a suppressed one.
        """
        pending = self._authority.load_pending_promotion(signature=action.signature)
        if pending is not None and pending.target_repo != action.target_repo:
            resolved = self._reconcile_route_change(
                action, pending, recorded_at=recorded_at
            )
            if resolved is not None:
                return resolved
            pending = None
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
        return self._commit(
            pending,
            issue_number=filed.number,
            url=filed.url,
            recovered=filed.recovered,
            recorded_at=recorded_at,
        )

    def _reconcile_route_change(
        self,
        action: "PromoteTechLeadFindingAction",
        pending: "PendingPromotion",
        *,
        recorded_at: str,
    ) -> FiledPromotion | None:
        """Settle an interrupted filing whose route has since been re-pointed.

        ``tech_lead.findings.route`` is ordinary, user-editable configuration
        and the pending intent is deliberately durable across restarts, so an
        operator re-pointing an area between ticks reaches this — the signature's
        area never has to change (#6957 round-4 review F12). Refusing to act
        stranded the signature forever: every later tick rebuilt the same action
        from the new route, hit the same mismatch, and failed again.

        A RECOVERY-ONLY lookup in the intent's own repo decides it, and only one
        of three things can be true:

        * the old issue EXISTS — it is the promotion. One signature promotes to
          exactly one issue ever, so the old target stays authoritative and the
          new route simply does not apply to a finding already filed. Finalize
          from the intent and return.
        * the old issue is PROVEN ABSENT — that create never happened, so
          nothing is orphaned. Retire the stale intent and return None, letting
          the caller file fresh against the current route.
        * the lookup FAILS — "unknown", never "absent". It propagates, so
          nothing is created and the next tick tries again.
        """
        found = self._target.find_filed_issue(
            repo=pending.target_repo,
            title=pending.title,
            idempotency_marker=pending.idempotency_marker,
        )
        if found is None:
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
            "[tech_lead] Signature %r was re-routed from %s to %s, but its"
            " interrupted filing had already created %s#%d. That issue IS the"
            " promotion — one signature promotes exactly once — so the recorded"
            " target stays %s and the new route does not apply to it",
            action.signature,
            pending.target_repo,
            action.target_repo,
            pending.target_repo,
            found.number,
            pending.target_repo,
        )
        return self._commit(
            pending,
            issue_number=found.number,
            url=found.url,
            recovered=True,
            recorded_at=recorded_at,
        )

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
