"""Pattern case-file issues: the durable flag_pattern ledger (#6781).

``flag_pattern`` used to produce an event and a report line — observed
patterns evaporated unless someone read that session's report. Under execute
authority it now creates or appends to a **case-file issue** keyed by pattern
signature, so the tech-lead model gets an accumulating problem ledger the
operator can read on GitHub. This module is the single policy owner for the
case-file lifecycle, mirroring ``tech_lead_proposals`` (#6778) piece for piece:

* **Composition** — :func:`build_case_file_issue_action` turns the first
  observation of a signature into a :class:`CreateTechLeadCaseFileIssueAction`.
  The issue body is human documentation ONLY: dedup consults the ledger, so
  editing the issue after creation has zero effect (the tamper boundary).
* **Creation boundary** — the applier's single create-issue executor
  (``tech_lead_issue_creation.apply_create_tech_lead_issue``) records the
  ``(signature -> issue)`` ledger row create-once when it creates the issue.
* **Ledger dedup** — one case file per signature:
  :func:`build_pattern_ledger` projects the store's rows; a repeat
  observation plans an :class:`AddCommentAction` carrying the new evidence
  (:func:`build_case_file_evidence_comment`) instead of a second issue.
* **Classification** — :func:`split_tech_lead_case_file_issues` partitions the
  fact gatherer's ONE open-issue anchor scan (no extra GitHub call):
  observation-labeled issues become :class:`TechLeadCaseFileSummary` facts for
  the board snapshot and can never be mistaken for batch/health anchors.
  Startup recovery uses the same split so a case file is never requeued as
  an anchor.
* **Per-decision planning** — :class:`PatternCaseFilePlanner` owns the whole
  create-vs-append-vs-coalesce decision for ONE tech-lead decision, plus the
  classification preflight that must run before any of them. That state
  (signature -> merged classification, signature -> the creation already
  planned this decision) exists only to serve case files, so it belongs here
  rather than mixed into the general decision planner's fields.
* **No terminal handling** — observations are not ops; there is nothing to
  execute or discard. Graduation is native: a firmed-up pattern gets a
  linked root-cause work issue, evidence trail intact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

from ..domain.tech_lead_findings import (
    PatternEvidence,
    PatternObservation,
    case_file_issue_marker,
    pattern_observation_id,
    pattern_observation_marker,
    reconcile_pattern_classification,
)
from ..domain.tech_lead_session import (
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadCaseFileSummary,
    is_tech_lead_observation_label,
    tech_lead_area_from_labels,
)
from .actions import (
    Action,
    ActionResult,
    AppendPatternObservationAction,
    CreateTechLeadCaseFileIssueAction,
)
from .tech_lead_case_file_owner import PatternCaseFileOwner
from .tech_lead_issue_policy import case_file_issue_labels

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import ProposedTechLeadAction, TechLeadFinding
    from ..infra.config import Config
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)

CASE_FILE_TITLE_PREFIX = "Pattern case file: "


def build_pattern_ledger(
    evidence: Iterable[PatternEvidence],
) -> dict[str, PatternEvidence]:
    """Project the store's pattern rows to a signature -> evidence map.

    Rows are created with the case-file issue and never discarded — the
    case file IS the accumulating artifact — so this ledger enforces one
    case file per signature without a GitHub read.

    It carries the FULL durable row, not just the issue number: planning has to
    preflight a new observation's ``fix_class``/``area`` against what is already
    recorded, and it cannot do that from a number alone. Without it a
    conflicting classification was only discovered at apply time, AFTER the
    evidence comment had already been published (#6957 round-2 review F3).
    """
    return {row.signature: row for row in evidence}


def _evidence_lines(
    proposed: "ProposedTechLeadAction",
    findings: Mapping[str, "TechLeadFinding"],
) -> list[str]:
    """The observation's evidence block: linked findings + their refs."""
    lines: list[str] = []
    for finding_id in proposed.finding_ids:
        finding = findings.get(finding_id)
        if finding is None:
            continue
        lines.append(
            f"- **{finding_id}** ({finding.classification}): {finding.title}"
        )
        lines.extend(f"  - evidence: {ref}" for ref in finding.evidence)
    return lines


def _observation_body(
    proposed: "ProposedTechLeadAction",
    *,
    anchor_issue_number: int,
    findings: Mapping[str, "TechLeadFinding"],
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
) -> str:
    """One observation's record — shared by the issue body and comments."""
    lines = [
        "| | |",
        "|---|---|",
        f"| Signature | `{proposed.pattern_signature}` |",
        f"| Area | {proposed.area or 'unclassified'} |",
        f"| Fix class | {f'`fix:{proposed.fix_class}`' if proposed.fix_class else 'unclassified'} |",
        f"| Observed at | {observed_at} |",
        (
            f"| Observed by | session `{source_session_name}`"
            f" (run `{source_run_id}`, action {proposed.id}) |"
        ),
        f"| Anchor issue | #{anchor_issue_number} |",
        "",
        "### Observation",
        "",
        proposed.body or "",
    ]
    evidence = _evidence_lines(proposed, findings)
    if evidence:
        lines.extend(["", "### Evidence", "", *evidence])
    return "\n".join(lines)


def build_case_file_issue_action(
    proposed: "ProposedTechLeadAction",
    *,
    config: "Config",
    anchor_issue_number: int,
    findings: Mapping[str, "TechLeadFinding"],
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
    expected: "ExpectedState",
) -> CreateTechLeadCaseFileIssueAction:
    """Compose the case-file creation for a signature's FIRST observation."""
    assert proposed.pattern_signature is not None  # enforced by validate()
    # Deterministic remote provenance key. The case file is created on GitHub
    # BEFORE its ledger row is written, so a process that dies in between would
    # otherwise file a second case file for one signature on retry, splitting
    # the evidence promotion reads (#6957 round-2 review F10). The applier's
    # case-file owner recovers the existing issue by this marker instead.
    marker = case_file_issue_marker(proposed.pattern_signature)
    body = (
        f"## Pattern case file (#6781)\n\n"
        "A tech_lead session flagged a recurring cross-job pattern. This issue"
        " is its durable evidence ledger: every later observation of the"
        " same signature lands here as a comment, and comment cadence is"
        " the severity signal health reviews read from the board snapshot."
        "\n\n"
        + _observation_body(
            proposed,
            anchor_issue_number=anchor_issue_number,
            findings=findings,
            source_run_id=source_run_id,
            source_session_name=source_session_name,
            observed_at=observed_at,
        )
        + f"\n\n> This is an orchestrator-owned observation ledger, keyed"
        f" orchestrator-side by its pattern signature when this issue was"
        f" created; editing this issue has no effect on that ledger. It is"
        f" never picked up as agent work (`{TECH_LEAD_OBSERVATION_LABEL}`)."
        " Graduation: link a root-cause work issue (or relabel into"
        " actionable work) when the pattern firms up."
        f"\n\n{marker}"
    )
    return CreateTechLeadCaseFileIssueAction(
        title=f"{CASE_FILE_TITLE_PREFIX}{proposed.pattern_signature}",
        body=body,
        labels=case_file_issue_labels(config, area=proposed.area),
        pr_count=0,
        pattern_signature=proposed.pattern_signature,
        # Retained, not just rendered into the body: it is the issue this
        # creation's reconciliation gate reads before any write (#6957 F3/A3).
        anchor_issue_number=anchor_issue_number,
        area=proposed.area,
        fix_class=proposed.fix_class or "",
        diagnosis=proposed.body or "",
        idempotency_marker=marker,
        observations=(
            build_pattern_observation(
                proposed,
                anchor_issue_number=anchor_issue_number,
                findings=findings,
                source_run_id=source_run_id,
                source_session_name=source_session_name,
                observed_at=observed_at,
            ),
        ),
        reason=(
            f"tech_lead decision action {proposed.id}: open pattern case file"
            f" for signature {proposed.pattern_signature!r} (#6781)"
        ),
        expected=expected,
    )


def build_pattern_observation(
    proposed: "ProposedTechLeadAction",
    *,
    anchor_issue_number: int,
    findings: Mapping[str, "TechLeadFinding"],
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
) -> PatternObservation:
    """One identified observation: its stable identity + its evidence comment.

    The identity is what makes the durable count create-once (#6957 review F1),
    and it is embedded in the comment so a duplicate posted by a crash-retry is
    recognizable as the SAME observation rather than fresh evidence.
    """
    observation_id = pattern_observation_id(
        source_run_id=source_run_id,
        source_session_name=source_session_name,
        action_id=proposed.id,
    )
    return PatternObservation(
        observation_id=observation_id,
        comment=build_case_file_evidence_comment(
            proposed,
            anchor_issue_number=anchor_issue_number,
            findings=findings,
            source_run_id=source_run_id,
            source_session_name=source_session_name,
            observed_at=observed_at,
            observation_id=observation_id,
        ),
    )


def build_case_file_evidence_comment(
    proposed: "ProposedTechLeadAction",
    *,
    anchor_issue_number: int,
    findings: Mapping[str, "TechLeadFinding"],
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
    observation_id: str,
) -> str:
    """The evidence comment for a REPEAT observation of a known signature."""
    return (
        "## 📌 Pattern observed again\n\n"
        + _observation_body(
            proposed,
            anchor_issue_number=anchor_issue_number,
            findings=findings,
            source_run_id=source_run_id,
            source_session_name=source_session_name,
            observed_at=observed_at,
        )
        + f"\n\n{pattern_observation_marker(observation_id)}"
    )


def build_append_observation_action(
    proposed: "ProposedTechLeadAction",
    *,
    case_file_issue_number: int,
    anchor_issue_number: int,
    findings: Mapping[str, "TechLeadFinding"],
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
    expected: "ExpectedState",
    fix_class: str,
    area: str,
) -> AppendPatternObservationAction:
    """Plan a REPEAT observation of a known signature (comment + count).

    The count is the promotion lane's ``min_evidence`` input (#6957), so the
    comment and the increment must be one action with one owner — a bare
    comment would leave the count derivable only from GitHub comment cadence,
    which humans also write to.

    ``fix_class``/``area`` are the values the PLANNER already reconciled against
    the durable row (and against earlier observations in the same decision), not
    this proposal's raw claim: a conflict has to reject the decision before any
    action exists, so what reaches the store here can only be an upgrade or a
    no-op (#6957 round-2 review F3).
    """
    assert proposed.pattern_signature is not None  # enforced by validate()
    return AppendPatternObservationAction(
        issue_number=case_file_issue_number,
        pattern_signature=proposed.pattern_signature,
        observation=build_pattern_observation(
            proposed,
            anchor_issue_number=anchor_issue_number,
            findings=findings,
            source_run_id=source_run_id,
            source_session_name=source_session_name,
            observed_at=observed_at,
        ),
        fix_class=fix_class,
        area=area,
        reason=(
            f"tech_lead decision action {proposed.id}: pattern"
            f" {proposed.pattern_signature!r} observed again; appending evidence"
            f" to case file #{case_file_issue_number} (#6781)"
        ),
        expected=expected,
    )


@dataclass
class PatternCaseFilePlanner:
    """Plans the DURABLE half of one executed ``flag_pattern`` decision.

    Extracted from the general decision planner, whose fields it was the only
    consumer of: the merged-classification map and the "already planned this
    decision" index are case-file bookkeeping, and keeping them beside the
    create/append/coalesce rules puts the whole per-decision case-file policy
    under one owner (#6957 round-6 final abstraction pass).

    It appends into the decision's shared ``actions`` list because coalescing
    REPLACES a creation already planned in it — the position matters, so the
    list is the collaboration, not a return value.
    """

    config: "Config"
    actions: list["Action"]
    anchor_issue_number: int
    # signature -> its FULL durable row: planning preflights a new observation's
    # classification against it, which a bare issue number cannot support.
    pattern_ledger: Mapping[str, PatternEvidence]
    findings: Mapping[str, "TechLeadFinding"]
    source_run_id: str
    source_session_name: str
    observed_at: str
    expected: "ExpectedState"
    # signature -> the (fix_class, area) every observation seen so far in THIS
    # decision reconciled to, seeded from the durable row. Two observations that
    # disagree conflict with each other, not just with what is recorded
    # (#6957 round-2 review F3).
    _classification: dict[str, tuple[str, str]] = field(default_factory=dict)
    # signature -> index in ``actions`` of the creation this decision planned.
    _planned: dict[str, int] = field(default_factory=dict)

    def plan(self, proposed: "ProposedTechLeadAction") -> None:
        """Create, append to, or coalesce into this signature's case file."""
        signature = proposed.pattern_signature
        assert signature is not None  # enforced by validate()
        # Preflight FIRST: a classification conflict must reject the decision
        # before this produces any mutating action (#6957 R2 F3).
        fix_class, area = self.classification_for(signature, proposed)
        existing = self.pattern_ledger.get(signature)
        if existing is not None:
            # Comment AND durable count under one owner (#6957): the count is
            # what promotion's min_evidence reads, so it can never be left to
            # GitHub comment cadence. The action carries the MERGED
            # classification, so the store's own reconcile is an upgrade or a
            # no-op — never a conflict discovered mid-write.
            self.actions.append(
                build_append_observation_action(
                    proposed,
                    case_file_issue_number=existing.case_file_issue_number,
                    anchor_issue_number=self.anchor_issue_number,
                    findings=self.findings,
                    source_run_id=self.source_run_id,
                    source_session_name=self.source_session_name,
                    observed_at=self.observed_at,
                    expected=self.expected,
                    fix_class=fix_class,
                    area=area,
                )
            )
            return
        planned_index = self._planned.get(signature)
        if planned_index is not None:
            self._coalesce(planned_index, proposed, fix_class=fix_class, area=area)
            return
        self._planned[signature] = len(self.actions)
        self.actions.append(
            build_case_file_issue_action(
                proposed,
                config=self.config,
                anchor_issue_number=self.anchor_issue_number,
                findings=self.findings,
                source_run_id=self.source_run_id,
                source_session_name=self.source_session_name,
                observed_at=self.observed_at,
                expected=self.expected,
            )
        )

    def classification_for(
        self, signature: str, proposed: "ProposedTechLeadAction"
    ) -> tuple[str, str]:
        """The signature's merged ``(fix_class, area)``, or raise on conflict.

        The classification PREFLIGHT (#6957 round-2 review F3). It reconciles
        this observation against everything already known about the signature —
        the DURABLE row first, then whatever earlier observations in this same
        decision merged into — using the one rule the store enforces.

        Running it before any action is produced is what makes a conflict
        externally invisible: the raise unwinds into the whole-decision
        rejection in ``plan_tech_lead_decision_actions``, so no evidence
        comment, surface action, or sibling mutation is ever applied.
        Reconciling only at apply time published the conflicting comment first
        and left the durable row disagreeing with it.
        """
        merged = self._classification.get(signature)
        if merged is None:
            recorded = self.pattern_ledger.get(signature)
            merged = (
                (recorded.fix_class, recorded.area) if recorded is not None else ("", "")
            )
        fix_class = reconcile_pattern_classification(
            field="fix_class",
            signature=signature,
            existing=merged[0],
            incoming=proposed.fix_class or "",
        )
        area = reconcile_pattern_classification(
            field="area",
            signature=signature,
            existing=merged[1],
            incoming=proposed.area or "",
        )
        self._classification[signature] = (fix_class, area)
        return fix_class, area

    def _coalesce(
        self,
        planned_index: int,
        proposed: "ProposedTechLeadAction",
        *,
        fix_class: str,
        area: str,
    ) -> None:
        """Fold a second first-seen observation into the pending creation.

        One case file per signature, so a second observation of a signature this
        decision is already creating rides the SAME action as an extra
        identified observation, carrying the classification the preflight
        already merged. Retaining only the first action's values silently lost
        an ``unclassified -> code`` upgrade (and an area that decides routing)
        whenever both observations arrived in one decision (#6957 review F3).
        """
        creation = self.actions[planned_index]
        assert isinstance(creation, CreateTechLeadCaseFileIssueAction)
        self.actions[planned_index] = replace(
            creation,
            observations=(
                *creation.observations,
                build_pattern_observation(
                    proposed,
                    anchor_issue_number=self.anchor_issue_number,
                    findings=self.findings,
                    source_run_id=self.source_run_id,
                    source_session_name=self.source_session_name,
                    observed_at=self.observed_at,
                ),
            ),
            fix_class=fix_class,
            area=area or None,
            # An upgraded area changes the case file's ``area:*`` tag, so the
            # labels are recomposed by their policy owner rather than left
            # describing the first observation only.
            labels=case_file_issue_labels(self.config, area=area or None),
        )


def apply_append_pattern_observation(
    action: "Action",
    *,
    repository_host: "RepositoryHost | None",
    authority: "TechLeadAuthorityStore | None",
) -> "ActionResult":
    """Post a repeat observation and count it create-once (#6781/#6957).

    Delegates the comment/count ordering to :class:`PatternCaseFileOwner`, the
    same owner the creation boundary uses, so the two paths cannot drift on
    "already recorded means do nothing; otherwise comment, then count".

    Classification conflicts never reach here: the planner preflights every
    observation against the durable row and rejects the whole decision instead
    (#6957 round-2 review F3). The store still enforces the rule as a last line
    of defence, which fails this action rather than reclassifying a signature.

    Like every other tech-lead applier boundary (case-file creation, proposal
    finalization), this writes without the applier's reconciliation guard: the
    target is an orchestrator-owned case file, never a claimed coding issue, so
    there is no concurrent owner whose state could have moved underneath it.
    """
    assert isinstance(action, AppendPatternObservationAction)
    if repository_host is None or authority is None:
        return ActionResult.fail(
            action,
            "pattern observation append requires repository_host and the"
            " TechLeadAuthorityStore wired into this applier",
        )
    assert action.observation is not None  # enforced by the action's __post_init__
    try:
        outcome = PatternCaseFileOwner(
            authority=authority,
            repository_host=repository_host,
            add_comment=repository_host.add_comment,
        ).append_observations(
            signature=action.pattern_signature,
            issue_number=action.issue_number,
            observations=(action.observation,),
            fix_class=action.fix_class,
            area=action.area,
        )
    except Exception as exc:
        logger.exception(
            "Failed to append pattern observation for signature %r",
            action.pattern_signature,
        )
        return ActionResult.fail(action, str(exc))
    if outcome.deduplicated:
        return ActionResult.ok(
            action, issue_number=action.issue_number, deduplicated=True
        )
    return ActionResult.ok(action, issue_number=action.issue_number)


def build_case_file_summary(issue: "Issue") -> TechLeadCaseFileSummary:
    """Project one observation-labeled scan issue onto the board facts.

    ``comment_count``/``updated_at`` ride the SAME list-issues payload the
    anchor scan already fetched (GitHub API discipline: zero extra calls).
    """
    return TechLeadCaseFileSummary(
        issue_number=issue.number,
        title=issue.title,
        comment_count=issue.comment_count,
        updated_at=issue.updated_at or "",
        area=tech_lead_area_from_labels(issue.labels),
    )


def split_tech_lead_case_file_issues(
    issues: Sequence["Issue"],
) -> tuple[list["Issue"], tuple[TechLeadCaseFileSummary, ...]]:
    """Partition the anchor scan into (non-case-file issues, case files).

    One pass over the fact gatherer's existing open-issue scan, run AFTER
    the gated-proposal split and BEFORE anchor classification — mirroring
    proposals, an observation-labeled issue can never be mistaken for a
    batch/health anchor, and startup recovery never requeues one.
    """
    remaining: list["Issue"] = []
    case_files: list[TechLeadCaseFileSummary] = []
    for issue in issues:
        if any(is_tech_lead_observation_label(label) for label in issue.labels):
            case_files.append(build_case_file_summary(issue))
            continue
        remaining.append(issue)
    return remaining, tuple(case_files)


def case_file_area_counts(
    case_files: Sequence[TechLeadCaseFileSummary],
) -> tuple[tuple[str, int], ...]:
    """Open case files grouped by area (#6781 amendment trailing-window fact).

    Sorted by count (desc) then area name so the projection is
    deterministic; the empty area groups as "unclassified".
    """
    counts: dict[str, int] = {}
    for case_file in case_files:
        area = case_file.area or "unclassified"
        counts[area] = counts.get(area, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
