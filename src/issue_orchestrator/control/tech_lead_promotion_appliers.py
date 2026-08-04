"""The finding-promotion lane's mutating boundaries (#6957).

Split from ``tech_lead_finding_promotion`` along the line the lane already
draws: that module is PURE policy over the durable ledgers (eligibility,
routing, composition, planning — no IO at all), while everything here writes.
Three boundaries, each with an ordering that a crash must survive:

* :func:`apply_promote_tech_lead_finding` — file the issue in its routed repo,
  then record the ledger row create-once.
* :func:`apply_report_promoted_finding_evidence` — comment later evidence onto
  the one promoted issue, then advance its high-water mark.
* :func:`apply_settle_tech_lead_promotion` — close the loop in the SOURCE repo
  (shipped-fix memory, case-file comment/close, terminal ledger state).

Re-exported from ``tech_lead_finding_promotion`` so callers keep one import
site.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.tech_lead_findings import (
    PROMOTION_STATE_DECLINED,
    PROMOTION_STATE_SHIPPED,
    PromotedFinding,
)
from .actions import (
    Action,
    ActionResult,
    PromoteTechLeadFindingAction,
    ReportPromotedFindingEvidenceAction,
    SettleTechLeadPromotionAction,
)

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_promote_tech_lead_finding(
    action: Action,
    *,
    target: "PromotionTargetHost | None",
    authority: "TechLeadAuthorityStore | None",
    now_iso: str | None = None,
) -> ActionResult:
    """File a promoted finding issue and record its ledger row create-once.

    Order matters: file FIRST, record SECOND. A filing that raises leaves the
    ledger untouched so the next tick retries cleanly; a recorded row makes the
    signature permanently ineligible, so it must only exist for an issue that
    actually exists. The target's deterministic marker lookup absorbs a crash
    between the two by returning the already-created remote issue on retry.
    """
    assert isinstance(action, PromoteTechLeadFindingAction)
    if target is None or authority is None:
        return ActionResult.fail(
            action,
            "tech_lead finding promotion requires a PromotionTargetHost and the"
            " TechLeadAuthorityStore wired into this applier",
        )
    existing = authority.load_promotion(signature=action.signature)
    if existing is not None:
        # Belt-and-braces against a stale plan: the ledger is the authority on
        # at-most-once, not the planning snapshot it was derived from.
        return ActionResult.ok(
            action,
            issue_number=existing.target_issue_number,
            deduplicated=True,
        )
    try:
        filed = target.file_issue(
            repo=action.target_repo,
            title=action.title,
            body=action.body,
            labels=list(action.labels),
            idempotency_marker=action.idempotency_marker,
        )
    except Exception as exc:
        logger.exception(
            "Failed to file promoted tech_lead finding %r in %s",
            action.signature,
            action.target_repo,
        )
        return ActionResult.fail(action, str(exc))
    authority.record_promotion(
        promotion=PromotedFinding(
            signature=action.signature,
            case_file_issue_number=action.case_file_issue_number,
            target_repo=action.target_repo,
            target_issue_number=filed.number,
            area=action.area,
            title=action.title,
            recorded_at=now_iso or _utc_now_iso(),
            reported_observations=action.observation_count,
        )
    )
    logger.info(
        "[tech_lead] Promoted finding %r -> %s#%d (case file #%d)",
        action.signature,
        action.target_repo,
        filed.number,
        action.case_file_issue_number,
    )
    return ActionResult.ok(
        action,
        issue_number=filed.number,
        target_repo=action.target_repo,
        url=filed.url,
    )


def apply_report_promoted_finding_evidence(
    action: Action,
    *,
    target: "PromotionTargetHost | None",
    authority: "TechLeadAuthorityStore | None",
) -> ActionResult:
    """Comment later evidence on the one promoted issue, then mark it reported.

    The write order fails toward a duplicate comment after a crash, never toward
    silently losing evidence: only a successful target comment advances the
    durable high-water mark. The apply-time ledger read also makes a stale plan
    harmless after another tick already reported or settled the promotion.
    """
    assert isinstance(action, ReportPromotedFindingEvidenceAction)
    if target is None or authority is None:
        return ActionResult.fail(
            action,
            "reporting promoted finding evidence requires a PromotionTargetHost"
            " and the TechLeadAuthorityStore wired into this applier",
        )
    promotion = authority.load_promotion(signature=action.signature)
    if promotion is None:
        return ActionResult.fail(
            action,
            f"no promotion is recorded for signature {action.signature!r}",
        )
    if (
        promotion.target_repo != action.target_repo
        or promotion.target_issue_number != action.target_issue_number
    ):
        return ActionResult.fail(
            action,
            f"promotion target changed for signature {action.signature!r}:"
            f" ledger has {promotion.target_repo}#{promotion.target_issue_number},"
            f" action has {action.target_repo}#{action.target_issue_number}",
        )
    if not promotion.is_open or (
        promotion.reported_observations >= action.observation_count
    ):
        return ActionResult.ok(
            action,
            issue_number=promotion.target_issue_number,
            deduplicated=True,
        )
    try:
        target.add_comment(
            repo=action.target_repo,
            issue_number=action.target_issue_number,
            body=action.comment,
        )
        authority.note_promotion_reported(
            signature=action.signature,
            observations=action.observation_count,
        )
    except Exception as exc:
        logger.exception(
            "Failed to report later evidence for promoted finding %r",
            action.signature,
        )
        return ActionResult.fail(action, str(exc))
    return ActionResult.ok(
        action,
        issue_number=action.target_issue_number,
        observation_count=action.observation_count,
    )


def _shipped_case_file_comment(action: SettleTechLeadPromotionAction) -> str:
    return (
        "## ✅ Fixed upstream\n\n"
        f"The promoted issue {action.target_repo}#{action.target_issue_number}"
        " closed with a merged pull request, so this pattern is fixed by"
        f" {action.target_repo}#{action.target_issue_number}"
        f"{f' ({action.merged_pr_url})' if action.merged_pr_url else ''}.\n\n"
        "Closing this case file; the shipped fix is recorded in the tech lead's"
        " durable operational memory.\n\nIf this pattern recurs, later"
        " observations still land here as evidence — but the signature is NOT"
        " promoted again. A recurrence after a shipped fix is the signal to"
        " step back and fix the design, not to file another point patch."
    )


def _declined_case_file_comment(action: SettleTechLeadPromotionAction) -> str:
    return (
        "## 🚫 Promotion declined\n\n"
        f"The promoted issue {action.target_repo}#{action.target_issue_number}"
        " was closed without a merged fix, which is an operator decline. This"
        " signature will never be promoted again.\n\n"
        "This case file stays OPEN and keeps accruing observations — the"
        " evidence is still worth having even when the fix is not being taken."
    )


def apply_settle_tech_lead_promotion(
    action: Action,
    *,
    repository_host: "RepositoryHost | None",
    authority: "TechLeadAuthorityStore | None",
) -> ActionResult:
    """Close the loop for a terminal promotion (the ONE settlement boundary).

    All writes here are IN the source repo (case-file comment/close, shipped-fix
    memory, ledger state) — only the READ that produced this fact crossed repos.

    Ordering makes a crash mid-settlement self-healing: the durable ledger state
    is written LAST, so an interrupted settlement is re-planned next tick and the
    comment/close/record steps are individually idempotent (``record_shipped_fix``
    is create-once, closing a closed issue is a no-op, and a duplicate comment is
    cosmetic — far cheaper than losing the ``tech_lead_shipped_fixes`` row this
    whole lane exists to produce).
    """
    assert isinstance(action, SettleTechLeadPromotionAction)
    if repository_host is None or authority is None:
        return ActionResult.fail(
            action,
            "tech_lead promotion settlement requires repository_host and the"
            " TechLeadAuthorityStore wired into this applier",
        )
    try:
        if action.shipped:
            authority.record_shipped_fix(
                issue_number=action.case_file_issue_number,
                title=action.title or action.signature,
                pr_url=action.merged_pr_url,
                area=action.area,
            )
            repository_host.add_comment(
                action.case_file_issue_number, _shipped_case_file_comment(action)
            )
            repository_host.update_issue_state(action.case_file_issue_number, "closed")
        else:
            repository_host.add_comment(
                action.case_file_issue_number, _declined_case_file_comment(action)
            )
        authority.settle_promotion(
            signature=action.signature,
            state=PROMOTION_STATE_SHIPPED
            if action.shipped
            else PROMOTION_STATE_DECLINED,
            shipped_pr_url=action.merged_pr_url,
        )
    except Exception as exc:
        logger.exception(
            "Failed to settle promoted tech_lead finding %r", action.signature
        )
        return ActionResult.fail(action, str(exc))
    return ActionResult.ok(
        action,
        issue_number=action.case_file_issue_number,
        shipped=action.shipped,
    )
