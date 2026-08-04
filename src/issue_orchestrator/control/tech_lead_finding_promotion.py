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
* **Routing** — the ``tech_lead.findings.route`` table maps an area to the
  target that owns the fix, and :func:`resolve_promotion_route` turns an entry
  into a :class:`PromotionRoute`: the repo AND its scheduling contract. That
  contract is what makes a promotion RUNNABLE — the managed repo's discovery
  queries ``filtering.label`` alongside the agent label, so a self-routed
  promotion inherits both and removing the gate really is the operator's whole
  approval. Self-routed promotions are otherwise ordinary issues in the source
  repo and face its own gates; promotion never bypasses them.
* **Composition** — :func:`build_promotion_issue_body` renders the case file's
  mechanism into human documentation ONLY. The ledger is the authority; editing
  the promoted issue has zero effect on it (the same tamper boundary op
  proposals and case files carry).
* **Filing boundary** — :func:`apply_promote_tech_lead_finding` is the ONE
  mutating boundary. It files the issue through the ``PromotionTargetHost``
  port, then records the ledger row create-once; a filing that raises leaves
  the ledger untouched so the next tick retries, and a recorded row can never
  file a second issue.
* **Loop closure** — :class:`PromotionReadBudget` decides WHICH in-flight
  promotions are polled this tick (at most ``max_open_promoted`` per target,
  rotating), :func:`classify_promotion_outcomes` turns those target-repo reads
  into typed :class:`SettledPromotion` facts (read-only), and
  :func:`apply_settle_tech_lead_promotion` performs the writes: a close whose
  CLOSING pull request merged records the ``tech_lead_shipped_fixes`` row,
  comments ``fixed by <target>#N`` on the case file and closes it; any other
  close is the operator declining, which marks the signature declined forever
  and leaves the case file open to keep accruing evidence.

Promotion FILES issues, full stop. Nothing here approves, merges, labels, or
executes anything in the target repo — the target's own orchestrator runs the
work under all of its existing gates.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable, Sequence

from ..domain.tech_lead_findings import (
    PatternEvidence,
    PromotionRoute,
    PromotionUpdate,
    PromotableFinding,
    PromotedFinding,
    SettledPromotion,
    promotion_issue_marker,
    promotion_issue_title,
)
from ..domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL
from .actions import (
    Action,
    PromoteTechLeadFindingAction,
    ReportPromotedFindingEvidenceAction,
    SettleTechLeadPromotionAction,
)
from ..infra.tech_lead_promotion_activation import promotion_lane_readiness
from .reconciliation import build_expected_for_mutation
from .tech_lead_issue_policy import tech_lead_follow_up_agent_label
# The lane's cross-tick read budget lives in its own module (it is the only
# MUTABLE state here); re-exported so callers keep one import site.
from .tech_lead_promotion_read_budget import (
    PromotionReadBudget as PromotionReadBudget,
)
# The lane's three MUTATING boundaries live next door (this module is pure
# policy); re-exported so callers keep one import site.
from .tech_lead_promotion_appliers import (
    apply_promote_tech_lead_finding as apply_promote_tech_lead_finding,
    apply_report_promoted_finding_evidence as apply_report_promoted_finding_evidence,
    apply_settle_tech_lead_promotion as apply_settle_tech_lead_promotion,
)

if TYPE_CHECKING:
    from ..domain.models import TechLeadFacts
    from ..infra.config import Config
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

logger = logging.getLogger(__name__)


def resolve_promotion_route(config: "Config", *, area: str) -> PromotionRoute:
    """The full queue contract of the repo a finding's area routes to.

    The ONE owner of route resolution (#6957 review F2). A promoted issue is
    only runnable when it carries its target's scheduling labels, so this
    resolves the repo AND those labels together — callers never assemble a
    label set from config internals themselves.

    * ``self`` resolves to the managed repo and INHERITS its own scheduling
      contract: ``filtering.label`` (the scope label normal discovery queries
      alongside the agent label) and ``review.tech_lead_follow_up_agent``. Under
      ``auto`` such an issue is immediately discoverable; under ``gated``
      removing the gate is genuinely the operator's whole approval.
    * A foreign target declares its own contract in the route entry. Undeclared
      values fall back to "no scope label" and the source's follow-up agent
      label — the source repo's scope label is meaningless there, and applying
      it would tag a foreign issue with an unrelated filter.

    Fails loudly when a ``self`` route has no configured repo at all: a
    promotion with nowhere to land is a bug, not a silent skip.
    """
    target = config.tech_lead.findings.route_for(area)
    if not target.is_self:
        return PromotionRoute(
            target_repo=target.repo,
            agent_label=(
                target.agent_label
                if target.agent_label is not None
                else tech_lead_follow_up_agent_label(config)
            ),
            scope_label=target.scope_label or "",
        )
    if not config.repo:
        raise ValueError(
            "tech_lead.findings routes to 'self' but no repository is configured;"
            " set `repo: owner/name` or route the area to an explicit repo"
        )
    return PromotionRoute(
        target_repo=config.repo,
        agent_label=tech_lead_follow_up_agent_label(config),
        scope_label=config.filtering.label or "",
        is_self=True,
    )


def promotion_issue_labels(config: "Config", *, area: str) -> tuple[str, ...]:
    """Labels for a promoted finding issue, from its resolved route.

    Mirrors :func:`~.tech_lead_proposals.proposal_issue_labels` and
    :func:`~.tech_lead_issue_policy.case_file_issue_labels`: the target's worker
    agent label plus its scope label make the issue DISCOVERABLE the moment the
    gate comes off, the ``area:*`` tag keeps evidence clusters queryable, and
    the gate label (in ``gated`` mode) is the only blocking one — removing it is
    the operator's single action. The gate is orchestrator-attached and exempt
    from the agent-label allowlist here and ONLY here.
    """
    return resolve_promotion_route(config, area=area).issue_labels(
        area=area,
        gate_label=(
            PROPOSED_TECH_LEAD_LABEL if config.tech_lead.findings.gated else ""
        ),
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
            target_key = row.target_repo.casefold()
            in_flight[target_key] = in_flight.get(target_key, 0) + 1

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
        target = resolve_promotion_route(config, area=row.area).target_repo
        target_key = target.casefold()
        if in_flight.get(target_key, 0) >= findings.max_open_promoted:
            logger.info(
                "[tech_lead] Promotion of %r deferred: %s already has %d in-flight"
                " promoted issue(s) (tech_lead.findings.max_open_promoted=%d)",
                row.signature,
                target,
                in_flight.get(target_key, 0),
                findings.max_open_promoted,
            )
            continue
        in_flight[target_key] = in_flight.get(target_key, 0) + 1
        selected.append(PromotableFinding(evidence=row, target_repo=target))
    return tuple(selected)


def select_promotion_updates(
    *,
    evidence: Sequence[PatternEvidence],
    promotions: Sequence[PromotedFinding],
    settling_signatures: frozenset[str] = frozenset(),
) -> tuple[PromotionUpdate, ...]:
    """Select in-flight promotions that have unreported later evidence.

    The durable watermark makes this a pure, restart-safe ledger join. Initial
    evidence is seeded when the promotion is recorded, so only a strictly newer
    observation count produces an update. A target found terminal on this tick
    is excluded so settlement never races a comment onto an already-closed issue.
    """
    evidence_by_signature = {row.signature: row for row in evidence}
    updates: list[PromotionUpdate] = []
    for promotion in promotions:
        if not promotion.is_open or promotion.signature in settling_signatures:
            continue
        row = evidence_by_signature.get(promotion.signature)
        if row is None or row.observation_count <= promotion.reported_observations:
            continue
        updates.append(
            PromotionUpdate(
                promotion=promotion,
                observation_count=row.observation_count,
            )
        )
    return tuple(updates)


def build_promotion_issue_body(
    finding: PromotableFinding,
    *,
    source_repo: str,
    gated: bool,
) -> str:
    """Human documentation for a promoted finding issue (never authority)."""
    case_file = f"{source_repo}#{finding.evidence.case_file_issue_number}"
    marker = promotion_issue_marker(
        source_repo=source_repo,
        signature=finding.evidence.signature,
    )
    diagnosis = finding.evidence.diagnosis.strip() or (
        f"See the evidence ledger at {case_file} for the original diagnosis"
        " and suggested fix (this is a legacy case-file row recorded before"
        " promotion narratives were persisted)."
    )
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
| Area | {finding.evidence.area or "unclassified"} |
| Observations | {finding.evidence.observation_count} |
| Fix class | `fix:{finding.evidence.fix_class}` |
| Evidence ledger | {case_file} |

### Diagnosis and suggested fix

{diagnosis}

### Evidence

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

{marker}
"""


def build_repeat_observation_comment(
    *,
    signature: str,
    observation_count: int,
    case_file_issue_number: int,
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
        marker = promotion_issue_marker(
            source_repo=source_repo,
            signature=finding.evidence.signature,
        )
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
                observation_count=finding.evidence.observation_count,
                idempotency_marker=marker,
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
    read_budget: PromotionReadBudget,
) -> tuple[
    tuple[PromotableFinding, ...],
    tuple[PromotionUpdate, ...],
    tuple[SettledPromotion, ...],
]:
    """Promotion eligibility + loop-closure facts for one tick (READ-ONLY).

    The lane's fact-gathering owner, so the fact gatherer stays a thin seam that
    knows only "ask the promotion owner". Eligibility is pure ledger math with
    ZERO GitHub calls, so an inactive lane or a board with nothing promotable is
    free. Loop closure is the only reader that crosses repos, and
    ``read_budget`` enforces the ADR's promise that it reads at most
    ``max_open_promoted`` issues per target per tick — the cap that bounds the
    lane's work-in-progress bounds its API cost too, whatever the durable ledger
    accumulated before the cap was lowered.

    Whether the lane runs at all is :func:`promotion_lane_readiness`'s decision,
    the same one configuration validation and doctor consume — so this can never
    promote against a configuration those two considered inert (#6957 round-2
    review F9). An unready lane is a hard no-op: zero reads, zero writes, and
    the reason is logged once per tick rather than raised mid-plan.
    """
    if authority is None:
        return (), (), ()
    readiness = promotion_lane_readiness(config)
    if not readiness.active:
        return (), (), ()
    if readiness.problems:
        # Startup validation rejects this configuration, so reaching here means
        # the orchestrator is running with validation bypassed. Refuse to plan
        # rather than raise inside route resolution mid-tick.
        logger.error(
            "[tech_lead] Finding promotion is configured but not ready; skipping"
            " the lane this tick: %s",
            "; ".join(readiness.problems),
        )
        return (), (), ()
    evidence = authority.list_pattern_evidence()
    promotions = authority.list_promotions()
    promotable = select_promotable_findings(
        config,
        evidence=evidence,
        promotions=promotions,
    )
    if target is None:
        return promotable, (), ()
    settled = classify_promotion_outcomes(
        read_budget.select(
            promotions,
            per_target_limit=config.tech_lead.findings.max_open_promoted,
        ),
        target=target,
    )
    updates = select_promotion_updates(
        evidence=evidence,
        promotions=promotions,
        settling_signatures=frozenset(item.promotion.signature for item in settled),
    )
    return promotable, updates, settled


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
    actions.extend(plan_promotion_updates(config, updates=facts.promotion_updates))
    actions.extend(plan_promotion_settlements(facts.settled_promotions))
    return actions


def plan_promotion_updates(
    config: "Config", *, updates: Sequence[PromotionUpdate]
) -> list[Action]:
    """Turn unreported evidence facts into typed target-comment actions."""
    actions: list[Action] = []
    source_repo = config.repo or ""
    for update in updates:
        promotion = update.promotion
        actions.append(
            ReportPromotedFindingEvidenceAction(
                signature=promotion.signature,
                case_file_issue_number=promotion.case_file_issue_number,
                target_repo=promotion.target_repo,
                target_issue_number=promotion.target_issue_number,
                observation_count=update.observation_count,
                comment=build_repeat_observation_comment(
                    signature=promotion.signature,
                    observation_count=update.observation_count,
                    case_file_issue_number=promotion.case_file_issue_number,
                    source_repo=source_repo,
                ),
                reason=(
                    f"tech_lead finding {promotion.signature!r} accrued"
                    f" {update.new_observations} new observation(s): report on"
                    f" {promotion.target_repo}#{promotion.target_issue_number}"
                    " (#6957)"
                ),
                expected=build_expected_for_mutation(),
            )
        )
    return actions


def classify_promotion_outcomes(
    promotions: Iterable[PromotedFinding],
    *,
    target: "PromotionTargetHost",
) -> tuple[SettledPromotion, ...]:
    """Read each supplied promotion's target issue and classify terminality.

    Callers pass the read budget's selection, not the whole ledger: the
    per-target cap on in-flight promotions is also the per-tick cap on these
    cross-repo reads (:class:`PromotionReadBudget`).

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
