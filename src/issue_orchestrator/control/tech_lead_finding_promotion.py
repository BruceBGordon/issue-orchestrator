"""Finding promotion: pattern case file -> gated runnable issue (#6957).

``flag_pattern`` case files (#6781) accrue evidence but had no actuation lane;
``tech_lead_shipped_fixes`` has never had a row. This module is the SINGLE
policy owner for the lane that connects them, mirroring
``tech_lead_proposals`` (#6778) piece for piece:

* **Eligibility** — :func:`select_promotable_findings` is pure policy over the
  durable ledgers (zero GitHub reads): a signature is promotable when the lane
  is enabled, it has crossed ``min_evidence`` observations, the tech lead
  classified it ``fix:code``, it has no promotion row yet (promoted, declined,
  or shipped — all three block re-filing), and its ROUTED target repo has cap
  room. Excess eligible signatures simply wait for the next tick; the cap is
  storm backpressure, not a queue.
* **Routing** — the ``tech_lead.findings.route`` table maps an area to the repo
  that owns the fix, with ``self`` meaning the managed repo. Self-routed
  promotions are ordinary issues in the source repo and face its own scope
  gates; promotion never bypasses them.
* **Composition** — :func:`build_promotion_issue_body` renders the case file's
  mechanism into human documentation ONLY. The ledger is the authority; editing
  the promoted issue has zero effect on it (the same tamper boundary op
  proposals and case files carry).
* **Filing boundary** — :func:`apply_promote_tech_lead_finding` is the ONE
  mutating boundary. It files the issue through the ``PromotionTargetHost``
  port, then records the ledger row create-once; a filing that raises leaves
  the ledger untouched so the next tick retries, and a recorded row can never
  file a second issue.
* **Loop closure** — :func:`classify_promotion_outcomes` turns the target-repo
  reads into typed :class:`SettledPromotion` facts (read-only), and
  :func:`apply_settle_tech_lead_promotion` performs the writes: a merged-PR
  close records the ``tech_lead_shipped_fixes`` row, comments ``fixed by
  <target>#N`` on the case file and closes it; a close WITHOUT a merged PR is
  the operator declining, which marks the signature declined forever and
  leaves the case file open to keep accruing evidence.

Promotion FILES issues, full stop. Nothing here approves, merges, labels, or
executes anything in the target repo — the target's own orchestrator runs the
work under all of its existing gates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Sequence

from ..domain.tech_lead_findings import (
    PROMOTION_ROUTE_SELF,
    PROMOTION_STATE_DECLINED,
    PROMOTION_STATE_SHIPPED,
    PatternEvidence,
    PromotableFinding,
    PromotedFinding,
    SettledPromotion,
    promotion_issue_title,
)
from ..domain.tech_lead_session import (
    PROPOSED_TECH_LEAD_LABEL,
    TECH_LEAD_AREA_LABEL_PREFIX,
)
from .actions import (
    Action,
    ActionResult,
    PromoteTechLeadFindingAction,
    SettleTechLeadPromotionAction,
)
from .reconciliation import build_expected_for_mutation
from .tech_lead_issue_policy import tech_lead_follow_up_agent_label

if TYPE_CHECKING:
    from ..domain.models import TechLeadFacts
    from ..infra.config import Config
    from ..ports import RepositoryHost
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

logger = logging.getLogger(__name__)


def resolve_route_target(config: "Config", *, area: str) -> str:
    """The concrete ``owner/repo`` a finding's area routes to.

    ``self`` resolves to the managed repo, so downstream code never has to
    special-case the sentinel. Fails loudly when the orchestrator has no
    configured repo at all — a promotion with nowhere to land is a bug, not a
    silent skip.
    """
    target = config.tech_lead.findings.route_for(area)
    if target != PROMOTION_ROUTE_SELF:
        return target
    if not config.repo:
        raise ValueError(
            "tech_lead.findings routes to 'self' but no repository is configured;"
            " set `repo: owner/name` or route the area to an explicit repo"
        )
    return config.repo


def promotion_issue_labels(config: "Config", *, area: str) -> tuple[str, ...]:
    """Labels for a promoted finding issue.

    Mirrors :func:`~.tech_lead_proposals.proposal_issue_labels` and
    :func:`~.tech_lead_issue_policy.case_file_issue_labels`: the target repo's
    implementation agent label makes the issue schedulable the moment the gate
    comes off, the ``area:*`` tag keeps evidence clusters queryable, and the
    gate label (in ``gated`` mode) blocks pickup and IS the approval affordance
    — removing it is the operator's single action. The gate is
    orchestrator-attached and exempt from the agent-label allowlist here and
    ONLY here.

    The filtering scope label is deliberately NOT applied: a promotion may land
    in a foreign repo whose scope label means something else entirely, and a
    self-routed promotion must face the managed repo's own scope gates rather
    than smuggle itself past them.
    """
    return tuple(
        value
        for value in (
            tech_lead_follow_up_agent_label(config),
            f"{TECH_LEAD_AREA_LABEL_PREFIX}{area}" if area else None,
            PROPOSED_TECH_LEAD_LABEL if config.tech_lead.findings.gated else None,
        )
        if value
    )


def select_promotable_findings(
    config: "Config",
    *,
    evidence: Sequence[PatternEvidence],
    promotions: Sequence[PromotedFinding],
) -> tuple[PromotableFinding, ...]:
    """Signatures eligible to promote right now, capped per target repo.

    Pure policy over the two durable ledgers — no GitHub reads, so an
    ineligible board costs nothing. Selection is deterministic (most evidence
    first, then signature) so a capped storm promotes the best-evidenced
    findings and the same tick replayed picks the same ones.
    """
    findings = config.tech_lead.findings
    if not findings.enabled:
        return ()
    # Every promotion row blocks its signature, whatever its state: promoted =
    # already filed (later observations comment on it), declined = the operator
    # rejected it and it must NEVER be re-filed, shipped = already fixed.
    settled_signatures = {row.signature for row in promotions}
    in_flight: dict[str, int] = {}
    for row in promotions:
        if row.is_open:
            in_flight[row.target_repo] = in_flight.get(row.target_repo, 0) + 1

    candidates = sorted(
        (
            row
            for row in evidence
            if row.signature not in settled_signatures
            and row.is_code_fix
            and row.observation_count >= findings.min_evidence
        ),
        key=lambda row: (-row.observation_count, row.signature),
    )
    selected: list[PromotableFinding] = []
    for row in candidates:
        target = resolve_route_target(config, area=row.area)
        if in_flight.get(target, 0) >= findings.max_open_promoted:
            logger.info(
                "[tech_lead] Promotion of %r deferred: %s already has %d in-flight"
                " promoted issue(s) (tech_lead.findings.max_open_promoted=%d)",
                row.signature,
                target,
                in_flight.get(target, 0),
                findings.max_open_promoted,
            )
            continue
        in_flight[target] = in_flight.get(target, 0) + 1
        selected.append(PromotableFinding(evidence=row, target_repo=target))
    return tuple(selected)


def build_promotion_issue_body(
    finding: PromotableFinding,
    *,
    source_repo: str,
    gated: bool,
) -> str:
    """Human documentation for a promoted finding issue (never authority)."""
    case_file = f"{source_repo}#{finding.evidence.case_file_issue_number}"
    approval = (
        (
            f"**Remove the `{PROPOSED_TECH_LEAD_LABEL}` label** to approve this"
            " work. The issue then becomes an ordinary queue issue in this"
            " repository's own pipeline, under all of its existing gates.\n\n"
            "To decline, close this issue: the signature is recorded as declined"
            " and never re-filed (later observations still accrue on the case"
            " file)."
        )
        if gated
        else (
            "This promotion was filed ungated (`tech_lead.findings.promote:"
            " auto`), so it is already an ordinary queue issue in this"
            " repository's own pipeline.\n\nTo decline, close this issue: the"
            " signature is recorded as declined and never re-filed."
        )
    )
    return f"""## Promoted tech-lead finding (#6957)

A tech lead session in `{source_repo}` diagnosed a recurring pattern and
classified it as fixable by code. It crossed its evidence threshold, so the
orchestrator promoted it here — the repo its area routes to.

| | |
|---|---|
| Signature | `{finding.evidence.signature}` |
| Area | {finding.evidence.area or 'unclassified'} |
| Observations | {finding.evidence.observation_count} |
| Fix class | `fix:{finding.evidence.fix_class}` |
| Evidence ledger | {case_file} |

### Mechanism and evidence

The case file {case_file} is the accumulating evidence ledger: every
observation of this signature lands there as a comment, with the mechanism,
the session that observed it, and its evidence references. Read it before
starting — it is the diagnosis this issue exists to act on.

### How to proceed

{approval}

> This body is documentation only. The promotion is recorded orchestrator-side
> in `{source_repo}` keyed by its pattern signature; editing this issue has no
> effect on that ledger, and promotion never approves, merges, or executes
> anything.
"""


def build_repeat_observation_comment(
    *, signature: str, observation_count: int, case_file_issue_number: int,
    source_repo: str,
) -> str:
    """Comment for a further observation of an already-promoted signature.

    Mirrors :func:`~.tech_lead_proposals.build_duplicate_proposal_comment`: a
    signature promotes exactly once, so new evidence lands on the promoted
    issue instead of filing a second one.
    """
    return (
        "## 🔁 Observed again upstream\n\n"
        f"The pattern `{signature}` was observed again in `{source_repo}`"
        f" (now {observation_count} observations). This promoted issue already"
        " covers it — no second issue is filed.\n\n"
        f"Full evidence ledger: {source_repo}#{case_file_issue_number}"
    )


def plan_finding_promotions(
    config: "Config",
    *,
    promotable: Sequence[PromotableFinding],
) -> list[Action]:
    """Turn eligible findings into typed filing actions (read-free planning)."""
    source_repo = config.repo or ""
    gated = config.tech_lead.findings.gated
    actions: list[Action] = []
    for finding in promotable:
        actions.append(
            PromoteTechLeadFindingAction(
                signature=finding.evidence.signature,
                case_file_issue_number=finding.evidence.case_file_issue_number,
                target_repo=finding.target_repo,
                title=promotion_issue_title(
                    source_repo=source_repo, signature=finding.evidence.signature
                ),
                body=build_promotion_issue_body(
                    finding, source_repo=source_repo, gated=gated
                ),
                labels=promotion_issue_labels(config, area=finding.evidence.area),
                area=finding.evidence.area,
                reason=(
                    f"tech_lead finding {finding.evidence.signature!r} crossed"
                    f" {config.tech_lead.findings.min_evidence} observations:"
                    f" promote to {finding.target_repo} (#6957)"
                ),
                expected=build_expected_for_mutation(),
            )
        )
        logger.info(
            "Planner: promoting tech_lead finding %r (%d observations) to %s",
            finding.evidence.signature,
            finding.evidence.observation_count,
            finding.target_repo,
        )
    return actions


def gather_finding_promotion_facts(
    config: "Config",
    *,
    authority: "TechLeadAuthorityStore | None",
    target: "PromotionTargetHost | None",
) -> tuple[tuple[PromotableFinding, ...], tuple[SettledPromotion, ...]]:
    """Promotion eligibility + loop-closure facts for one tick (READ-ONLY).

    The lane's fact-gathering owner, so the fact gatherer stays a thin seam that
    knows only "ask the promotion owner". Eligibility is pure ledger math with
    ZERO GitHub calls, so a disabled lane or a board with nothing promotable is
    free. Loop closure is the only reader that crosses repos, and it reads at
    most ``max_open_promoted`` issues per target — the cap that bounds the
    lane's work-in-progress bounds its API cost too.
    """
    if authority is None or not config.tech_lead.findings.enabled:
        return (), ()
    promotions = authority.list_promotions()
    promotable = select_promotable_findings(
        config,
        evidence=authority.list_pattern_evidence(),
        promotions=promotions,
    )
    if target is None:
        return promotable, ()
    return promotable, classify_promotion_outcomes(promotions, target=target)


def plan_finding_promotion_actions(
    config: "Config", facts: "TechLeadFacts | None"
) -> list[Action]:
    """Every promotion action for one tick, from the gathered facts.

    The planner delegates here wholesale so the lane has exactly ONE planning
    owner: the fact sets already encode every policy decision (eligibility,
    ``min_evidence``, the ``fix:code`` classification, ledger dedup, the
    per-target cap, and whether a terminal promotion shipped or was declined),
    and this adds none of its own.
    """
    if facts is None:
        return []
    actions = plan_finding_promotions(config, promotable=facts.promotable_findings)
    actions.extend(plan_promotion_settlements(facts.settled_promotions))
    return actions


def classify_promotion_outcomes(
    promotions: Iterable[PromotedFinding],
    *,
    target: "PromotionTargetHost",
) -> tuple[SettledPromotion, ...]:
    """Read each in-flight promotion's target issue and classify terminality.

    READ-ONLY (the fact-gathering contract): the writes happen later in the
    applier off a planned action. An unreadable target — deleted issue, a repo
    that is temporarily unreachable, a transient API failure — yields NO fact,
    so the promotion simply stays in flight and the next tick retries. A
    momentary outage can never be mistaken for a decline.
    """
    settled: list[SettledPromotion] = []
    for promotion in promotions:
        if not promotion.is_open:
            continue
        try:
            outcome = target.read_outcome(
                repo=promotion.target_repo,
                issue_number=promotion.target_issue_number,
            )
        except Exception:
            logger.warning(
                "[tech_lead] Could not read promoted issue %s#%d; leaving the"
                " promotion in flight",
                promotion.target_repo,
                promotion.target_issue_number,
                exc_info=True,
            )
            continue
        if outcome is None or not outcome.closed:
            continue
        settled.append(
            SettledPromotion(
                promotion=promotion,
                shipped=bool(outcome.merged_pr_url),
                merged_pr_url=outcome.merged_pr_url,
            )
        )
    return tuple(settled)


def plan_promotion_settlements(
    settled: Sequence[SettledPromotion],
) -> list[Action]:
    """Turn terminal promotion facts into typed settlement actions."""
    actions: list[Action] = []
    for item in settled:
        promotion = item.promotion
        actions.append(
            SettleTechLeadPromotionAction(
                signature=promotion.signature,
                case_file_issue_number=promotion.case_file_issue_number,
                target_repo=promotion.target_repo,
                target_issue_number=promotion.target_issue_number,
                shipped=item.shipped,
                merged_pr_url=item.merged_pr_url,
                area=promotion.area,
                title=promotion.title,
                reason=(
                    f"promoted finding {promotion.signature!r} closed in"
                    f" {promotion.target_repo}"
                    f"#{promotion.target_issue_number}:"
                    f" {'shipped' if item.shipped else 'declined'} (#6957)"
                ),
                expected=build_expected_for_mutation(),
            )
        )
    return actions


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
    actually exists. The ledger's create-once contract absorbs a crash between
    the two (a re-file would conflict on signature and be surfaced loudly).
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
            repository_host.update_issue_state(
                action.case_file_issue_number, "closed"
            )
        else:
            repository_host.add_comment(
                action.case_file_issue_number, _declined_case_file_comment(action)
            )
        authority.settle_promotion(
            signature=action.signature,
            state=PROMOTION_STATE_SHIPPED if action.shipped else PROMOTION_STATE_DECLINED,
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
