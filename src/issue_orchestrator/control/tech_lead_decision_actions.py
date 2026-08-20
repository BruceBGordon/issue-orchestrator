"""Map a validated tech_lead decision to orchestrator actions (ADR-0031).

Pure policy: no IO, no events. The tech lead agent proposed actions as intent;
this module applies the configured graduated authority per action type and
emits the orchestrator's action vocabulary:

- ``execute`` authority -> the concrete action (comment, issue, escalation).
- ``propose`` authority -> a shadow :class:`SurfaceTechLeadProposalAction` for
  ``post_comment``/``escalate_to_human``/``flag_pattern`` (the
  immediate/report tier).
- ``create_issue`` with ``propose`` authority -> the issue is CREATED, gated
  with ``proposed-tech-lead`` (#6778): removing the label flows it into normal
  scheduling. The gate label is orchestrator-attached by the
  ``tech_lead_issue_policy`` owner and rejected by the agent-label allowlist.
- ``flag_pattern`` with ``execute`` authority -> surfaced with
  ``mode="pattern"`` PLUS the durable case-file ledger (#6781): a signature
  absent from the pattern ledger plans a
  :class:`~.actions.CreateTechLeadCaseFileIssueAction` (the applier records
  the signature -> issue row create-once); a known signature plans an
  evidence ``AddCommentAction`` on the existing case file. Under
  ``propose`` authority it stays a shadow record like any other proposal.
- Act-level proposals with ``execute`` authority -> typed
  :class:`ResetRetryIssueAction` / :class:`KillHungSessionAction` commands;
  each applier owner re-validates the proposal's preconditions at execution
  time and downgrades stale proposals to a surfaced record (#6764, ADR-0031
  §2).
- Act-level proposals under ``propose`` ->
  GATED PROPOSAL ISSUES (#6778): a
  :class:`~.actions.CreateTechLeadProposalIssueAction` whose applier creates
  the issue AND records the executable :class:`StoredTechLeadOp` create-once.
  Removing the gate label is per-instance approval; the fact gatherer's
  label scan then triggers execution of the STORED op. Dedup is
  ledger-based: one open proposal per (op, target) — a re-proposal plans an
  ``AddCommentAction`` on the existing proposal issue instead.

Shadow proposals additionally plan ONE durable would-have-done digest
comment on the tech_lead session's anchor issue. The trace event alone is not
an operator surface: ADR-0031 §2 requires shadow records to be visible "in
the report, as a structured event, and on the escalation surface", and the
escalation surface in this codebase is the crash-safe GitHub label/comment
channel that the dashboard projects (#6761 finding 6). Gated proposals do
NOT appear in the digest — their surface is the proposal issue itself,
linked from the anchor by the creation applier.

Decision-driven ``create_issue`` proposals are composed by the
``tech_lead_issue_policy`` owner: configured tech_lead labels/priority/milestone
strategy apply exactly as they do to the planner's batch tracking issue, and
agent labels have already passed the protected-label contract check
(``tech_lead_completion``).

Escalation note: tech_lead escalation deliberately does NOT reuse
``EscalateToHumanAction``. That action's applier terminates the target
issue's runtime ("escalation kills issue automation, full stop"), which
would let the always-execute ``escalate_to_human`` floor reach the same
effect as ``kill_hung_session`` while bypassing that action's explicit
authority dial and exact-session-generation revalidation. Tech Lead escalation
is strictly a routing
surface: needs-human label + explanatory comment on the target; nothing
is stopped and no other labels are touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Mapping

from ..domain.tech_lead_artifacts import (
    ProposedTechLeadAction,
    TechLeadDecision,
)
from ..domain.tech_lead_findings import PatternClassificationConflictError
from ..domain.tech_lead_session import (
    TechLeadCreationOrigin,
    TechLeadSessionGeneration,
)
from ..ports.issue import Issue
from .actions import (
    Action,
    AddCommentAction,
    AddLabelAction,
    CreateTechLeadIssueAction,
    KillHungSessionAction,
    ResetRetryIssueAction,
    SurfaceTechLeadProposalAction,
)
from .label_manager import LabelManager
from .proposal_dedup import similarity
from .proposal_dedup_gate import (
    CommentExisting,
    DedupAuthority,
    DuplicateTargetGrant,
    OpenIssueCorpus,
    ProposalIntent,
    classify_proposal,
)
from .tech_lead_gate_notes import (
    batch_duplicate_note,
    compose_gate_note,
    outcome_gate_note,
)
from .tech_lead_case_files import PatternCaseFilePlanner
from .tech_lead_issue_policy import (
    apply_tech_lead_priority_prefix,
    decision_issue_labels,
    tech_lead_follow_up_agent_label,
    tech_lead_issue_milestone_intent,
)
from .tech_lead_proposals import (
    build_duplicate_proposal_comment,
    build_tech_lead_proposal_issue_action,
)
from .needs_human_block import NeedsHumanCause

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import TechLeadFinding
    from ..domain.tech_lead_findings import PatternEvidence
    from ..infra.config import Config
    from .reconciliation import ExpectedState

# Cap applied to SurfaceTechLeadProposalAction.body_preview at construction.
_BODY_PREVIEW_CHARS = 500

# Operator-facing gate-note rendering (outcome → prose, batch note, composition)
# lives in tech_lead_gate_notes so the planner stays focused on action mapping.


def _provenance_footer(action: ProposedTechLeadAction) -> str:
    finding_ids = ", ".join(action.finding_ids) or "none"
    return (
        f"\n\n---\n*Proposed by tech_lead session (action {action.id};"
        f" findings: {finding_ids}) — ADR-0031.*"
    )


def _surface(
    action: ProposedTechLeadAction,
    *,
    anchor_issue_number: int,
    mode: str,
) -> SurfaceTechLeadProposalAction:
    return SurfaceTechLeadProposalAction(
        issue_number=anchor_issue_number,
        action_id=action.id,
        proposal_type=action.action_type,
        target_number=action.target_number or 0,
        target_is_pr=action.target_is_pr,
        title=action.title or "",
        body_preview=(action.body or "")[:_BODY_PREVIEW_CHARS],
        finding_ids=action.finding_ids,
        mode=mode,
        reason=f"tech_lead proposal {action.id} ({action.action_type}) surfaced as {mode}",
    )


def _concrete_actions(
    action: ProposedTechLeadAction,
    *,
    config: "Config",
    labels: LabelManager,
    anchor_issue: Issue,
    expected: "ExpectedState",
    needs_human_label: str,
    gate_reason: str | None = None,
) -> list[Action]:
    body = (action.body or "") + _provenance_footer(action)
    if action.action_type == "post_comment":
        assert action.target_number is not None  # enforced by validate()
        return [
            AddCommentAction(
                number=action.target_number,
                comment=body,
                is_pr=action.target_is_pr,
                reason=f"tech_lead decision action {action.id}: post diagnosis comment",
                expected=expected,
            )
        ]
    if action.action_type == "create_issue":
        # Config policy (tech_lead: labels/priority/milestone strategy) is the
        # single tech_lead_issue_policy owner, shared with the planner's batch
        # tracking issue; agent labels passed the protected-set contract
        # check at decision validation time (#6761 finding 4). The milestone
        # travels as INTENT — name resolution happens in the applier at
        # creation time, so planning makes zero GitHub reads (#6769 F4).
        anchor_milestones = (
            [(anchor_issue.milestone_number, anchor_issue.milestone or "")]
            if anchor_issue.milestone_number is not None
            else []
        )
        # A gate reason both gates the issue and explains itself in the operator-
        # facing body — never a bare boolean whose meaning callers must guess.
        gated = gate_reason is not None
        gated_body = f"{body}\n\n---\n> {gate_reason}" if gated else body
        return [
            CreateTechLeadIssueAction(
                title=apply_tech_lead_priority_prefix(config, action.title or ""),
                body=gated_body,
                labels=decision_issue_labels(
                    config,
                    anchor_labels=anchor_issue.labels,
                    agent_labels=action.labels,
                    labels=labels,
                    # Orchestrator-owned routing label so removing the gate
                    # alone lands a schedulable issue (#6779 R5); attached for
                    # execute-authority create_issue too — both need an agent.
                    destination_agent=tech_lead_follow_up_agent_label(config),
                    gate=gated,
                    area=action.area,
                ),
                pr_count=0,
                # DECIDED by a session working this anchor — so the anchor's
                # pause label gates the creation, and the ExpectedState below is
                # what the gate checks (#6957 F3/A3, R2 F6/A6).
                origin=TechLeadCreationOrigin.derived_from_anchor(anchor_issue.number),
                milestone=tech_lead_issue_milestone_intent(config, anchor_milestones),
                # Expedite intent (#6870) rides the action so the applier's
                # create boundary can front-queue the new issue. It composes
                # with the gate: gated (propose) creations defer expediting to
                # gate removal, ungated (execute) creations expedite at once.
                expedite=action.expedite,
                reason=(
                    f"tech_lead decision action {action.id}: create follow-up"
                    f" issue{' (gated)' if gated else ''}"
                ),
                expected=expected,
            )
        ]
    if action.action_type == "escalate_to_human":
        assert action.target_number is not None  # enforced by validate()
        # Routing surface only — see the module docstring for why this must
        # not reuse EscalateToHumanAction (runtime termination).
        return [
            AddLabelAction(
                issue_number=action.target_number,
                label=needs_human_label,
                reason=f"tech_lead decision action {action.id}: escalate to human",
                needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
                expected=expected,
            ),
            AddCommentAction(
                number=action.target_number,
                comment="## ⚠️ Tech Lead escalation — human attention needed\n\n" + body,
                is_pr=action.target_is_pr,
                reason=f"tech_lead decision action {action.id}: escalation comment",
                expected=expected,
            ),
        ]
    raise ValueError(
        f"no concrete executor for tech_lead action type {action.action_type!r}"
    )


def plan_tech_lead_rejection_action(
    *, anchor_issue_number: int, failure: str, detail: str
) -> SurfaceTechLeadProposalAction:
    """Surface a rejected decision artifact pair (``mode="rejected"``)."""
    return SurfaceTechLeadProposalAction(
        issue_number=anchor_issue_number,
        proposal_type="decision",
        body_preview=detail[:_BODY_PREVIEW_CHARS],
        mode="rejected",
        reason=f"tech_lead decision rejected ({failure})",
    )


def plan_tech_lead_decision_actions(
    decision: TechLeadDecision,
    config: "Config",
    labels: LabelManager,
    *,
    anchor_issue: Issue,
    expected: "ExpectedState",
    op_ledger: Mapping[tuple[str, int], int],
    pattern_ledger: Mapping[str, "PatternEvidence"],
    source_run_id: str,
    source_session_name: str,
    observed_at: str,
    observed_session_generation: Callable[[int], TechLeadSessionGeneration | None],
    dedup_corpus: OpenIssueCorpus,
    dedup_grant: DuplicateTargetGrant,
) -> list[Action]:
    """Plan orchestrator actions for a validated tech_lead decision.

    ``op_ledger`` maps (op_type, target_issue_number) of every currently
    recorded gated-proposal op to its proposal issue number (the authority
    store's rows, projected by ``tech_lead_proposals.build_op_ledger``): one
    open proposal per (op, target) — re-proposals comment on the existing
    issue. ``pattern_ledger`` maps every recorded flag_pattern signature to
    its case-file issue number (``tech_lead_case_files.build_pattern_ledger``,
    #6781): one case file per signature — repeat observations comment
    evidence onto the existing issue. ``source_run_id``/
    ``source_session_name`` are the proposing session's identity, recorded
    on each :class:`StoredTechLeadOp` and in each case-file observation.
    ``observed_session_generation`` resolves only the trusted worker generation
    retained in launch authority from the immutable board input. It never reads
    live state at completion, so a replacement cannot be rebound silently.
    """
    planner = _DecisionActionPlanner(
        config=config,
        labels=labels,
        anchor_issue=anchor_issue,
        expected=expected,
        op_ledger=op_ledger,
        pattern_ledger=pattern_ledger,
        findings={finding.id: finding for finding in decision.findings},
        source_run_id=source_run_id,
        source_session_name=source_session_name,
        observed_at=observed_at,
        observed_session_generation=observed_session_generation,
        dedup_corpus=dedup_corpus,
        dedup_grant=dedup_grant,
    )
    try:
        for proposed in decision.proposed_actions:
            planner.plan(proposed)
    except PatternClassificationConflictError as exc:
        # One decision claimed two different fix classes (or areas) for one
        # pattern signature. Classification decides whether a finding is
        # promotable at all and which repo it routes to, so the orchestrator
        # refuses to pick a winner: the whole decision is rejected as a contract
        # violation and nothing partially planned is applied (#6957 review F3).
        return [
            plan_tech_lead_rejection_action(
                anchor_issue_number=anchor_issue.number,
                failure="pattern_classification_conflict",
                detail=str(exc),
            )
        ]
    if planner.shadow:
        planner.actions.append(
            _shadow_digest_comment(
                planner.shadow,
                anchor_issue_number=anchor_issue.number,
                expected=expected,
            )
        )
    return planner.actions


@dataclass
class _DecisionActionPlanner:
    """Per-decision planning state: one authority dispatch per proposal."""

    config: "Config"
    labels: LabelManager
    anchor_issue: Issue
    expected: "ExpectedState"
    op_ledger: Mapping[tuple[str, int], int]
    # signature -> its FULL durable row: planning preflights a new
    # observation's classification against it, which a bare issue number
    # cannot support (#6957 round-2 review F3).
    pattern_ledger: Mapping[str, "PatternEvidence"]
    findings: Mapping[str, "TechLeadFinding"]
    source_run_id: str
    source_session_name: str
    observed_at: str
    observed_session_generation: Callable[[int], TechLeadSessionGeneration | None]
    # Trusted dedup facts (#6878), REQUIRED — never a silent empty default, which
    # would disable the safety mechanism invisibly. The corpus carries an explicit
    # Ready/Unavailable state; the grant is the launch-authority-derived set a
    # dedup redirect may target.
    dedup_corpus: OpenIssueCorpus
    dedup_grant: DuplicateTargetGrant
    actions: list[Action] = field(default_factory=list)
    shadow: list[SurfaceTechLeadProposalAction] = field(default_factory=list)
    _planned_ops: set[tuple[str, int]] = field(default_factory=set)
    # (action_id, title, body) of EVERY create_issue intent this decision has
    # processed — whether it filed a new issue or routed onto an existing one.
    # The persisted-corpus gate cannot see them (they have no issue number yet),
    # so identical sibling proposals in one decision would each take an
    # independent primary action (two files, or two comments on the same issue)
    # under execute — intra-decision dedup closes that for every create intent.
    _seen_create_intents: list[tuple[str, str, str]] = field(default_factory=list)
    # The case-file half of an executed flag_pattern, with its own per-decision
    # bookkeeping. Built in __post_init__ over the SAME action list, because
    # coalescing replaces a creation already planned in it.
    _case_files: PatternCaseFilePlanner = field(init=False)

    def __post_init__(self) -> None:
        self._case_files = PatternCaseFilePlanner(
            config=self.config,
            actions=self.actions,
            anchor_issue_number=self._anchor_number,
            pattern_ledger=self.pattern_ledger,
            findings=self.findings,
            source_run_id=self.source_run_id,
            source_session_name=self.source_session_name,
            observed_at=self.observed_at,
            expected=self.expected,
        )

    @property
    def _anchor_number(self) -> int:
        return self.anchor_issue.number

    def plan(self, proposed: ProposedTechLeadAction) -> None:
        if proposed.action_type == "flag_pattern":
            self._plan_flag_pattern(proposed)
        elif proposed.is_act_level:
            self._plan_act_level(proposed)
        else:
            self._plan_decision_tier(proposed)

    def _surface_shadow(self, proposed: ProposedTechLeadAction) -> None:
        surfaced = _surface(
            proposed, anchor_issue_number=self._anchor_number, mode="shadow"
        )
        self.shadow.append(surfaced)
        self.actions.append(surfaced)

    def _plan_flag_pattern(self, proposed: ProposedTechLeadAction) -> None:
        # Authority-aware (#6761 finding 5): execute records the pattern —
        # the trace event (mode="pattern") plus the durable case-file
        # ledger (#6781). Propose stays a shadow record (unchanged).
        if self.config.tech_lead.authority.mode_for("flag_pattern") != "execute":
            self._surface_shadow(proposed)
            return
        self.actions.append(
            _surface(
                proposed,
                anchor_issue_number=self._anchor_number,
                mode="pattern",
            )
        )
        self._case_files.plan(proposed)

    def _plan_gated_op(self, proposed: ProposedTechLeadAction) -> None:
        """Gated proposal issue for an act-level intent (#6778)."""
        assert proposed.target_number is not None  # enforced by validate()
        key = (proposed.action_type, proposed.target_number)
        existing = self.op_ledger.get(key)
        if existing is not None:
            self.actions.append(
                AddCommentAction(
                    number=existing,
                    comment=build_duplicate_proposal_comment(
                        proposed, anchor_issue_number=self._anchor_number
                    ),
                    is_pr=False,
                    reason=(
                        f"tech_lead decision action {proposed.id}: duplicate"
                        f" {proposed.action_type} proposal for"
                        f" #{proposed.target_number}; commenting on open"
                        f" proposal #{existing} (#6778)"
                    ),
                    expected=self.expected,
                )
            )
            return
        if key in self._planned_ops:
            # Two identical act-level proposals inside ONE decision: the
            # first creation covers both; a second issue would break the
            # one-open-proposal-per-(op, target) ledger invariant.
            return
        self._planned_ops.add(key)
        # kill_hung_session binds approval to the target's live session
        # generation (#6779 R1); reset_retry carries no generation binding.
        target_session = (
            self.observed_session_generation(proposed.target_number)
            if proposed.action_type == "kill_hung_session"
            else None
        )
        self.actions.append(
            build_tech_lead_proposal_issue_action(
                proposed,
                config=self.config,
                anchor_issue_number=self._anchor_number,
                source_run_id=self.source_run_id,
                source_session_name=self.source_session_name,
                expected=self.expected,
                target_session=target_session,
            )
        )

    def _plan_act_level(self, proposed: ProposedTechLeadAction) -> None:
        # Execute authority plans typed commands whose owners revalidate their
        # operation-specific preconditions at apply time. Propose authority
        # remains the per-instance gated issue path (#6778).
        if self.config.tech_lead.authority.mode_for(proposed.action_type) != "execute":
            self._plan_gated_op(proposed)
            return

        assert proposed.target_number is not None  # enforced by validate()
        if proposed.action_type == "reset_retry":
            self.actions.append(
                ResetRetryIssueAction(
                    issue_number=proposed.target_number,
                    rationale=proposed.body or "",
                    proposal_id=proposed.id,
                    finding_ids=proposed.finding_ids,
                    anchor_issue_number=self._anchor_number,
                    reason=(
                        f"tech_lead decision action {proposed.id}:"
                        " reset and retry from scratch"
                    ),
                    expected=self.expected,
                )
            )
            return
        assert proposed.action_type == "kill_hung_session"
        target_session = self.observed_session_generation(proposed.target_number)
        self.actions.append(
            KillHungSessionAction(
                issue_number=proposed.target_number,
                rationale=proposed.body or "",
                proposal_id=proposed.id,
                finding_ids=proposed.finding_ids,
                anchor_issue_number=self._anchor_number,
                target_session_id=target_session.run_id if target_session else "",
                target_terminal_id=(
                    target_session.terminal_id if target_session else ""
                ),
                target_session_type=(
                    target_session.task_kind.value if target_session else ""
                ),
                reason=(
                    f"tech_lead decision action {proposed.id}: terminate hung session"
                ),
                expected=self.expected,
            )
        )

    def _dedup_authority(self) -> DedupAuthority:
        authority = self.config.tech_lead.authority
        return DedupAuthority(
            create_issue_execute=authority.mode_for("create_issue") == "execute",
            post_comment_execute=authority.mode_for("post_comment") == "execute",
        )

    def _dedup_comment_action(
        self, proposed: ProposedTechLeadAction, existing: int
    ) -> Action:
        """Route a confirmed duplicate's observation (its title AND body) onto the
        existing issue instead of filing a new one."""
        heading = f"**{proposed.title}**\n\n" if proposed.title else ""
        note = heading + (proposed.body or "") + _provenance_footer(proposed)
        return AddCommentAction(
            number=existing,
            comment=(
                "## 🔁 Tech Lead: deduplicated follow-up\n\n"
                "A proposed new issue was recognized as a duplicate of this one,"
                " so its observation is routed here instead of filing a"
                f" duplicate.\n\n{note}"
            ),
            is_pr=False,
            reason=(f"tech_lead decision action {proposed.id}: dedup onto #{existing}"),
            expected=self.expected,
        )

    def _concrete_decision(
        self, proposed: ProposedTechLeadAction, *, gate_reason: str | None
    ) -> list[Action]:
        return _concrete_actions(
            proposed,
            config=self.config,
            labels=self.labels,
            anchor_issue=self.anchor_issue,
            expected=self.expected,
            needs_human_label=self.labels.needs_human,
            gate_reason=gate_reason,
        )

    def _plan_create_issue(
        self, proposed: ProposedTechLeadAction, *, execute: bool
    ) -> None:
        # The dedup gate OWNS the decision; the planner only translates its typed
        # outcome into actions. It never comments on an unverified/ungranted
        # target, never bypasses post_comment authority, and never loses a gate's
        # candidate/score/reason (#6878 B1-B3/A1).
        outcome = classify_proposal(
            ProposalIntent(
                proposed.title or "", proposed.body or "", proposed.duplicate_of
            ),
            self.dedup_corpus,
            self.dedup_grant,
            self._dedup_authority(),
            threshold=self.config.tech_lead.dedup.similarity_threshold,
        )
        # Intra-decision dedup owns the batch boundary. The persisted corpus
        # cannot see sibling create_issue proposals this decision has already
        # processed (they have no issue number yet), so EVERY create_issue
        # intent — file-new AND comment-onto-existing — is checked and recorded
        # here. A repeated proposal never takes a second primary action
        # (duplicate file OR duplicate comment); it is gated, and its gate note
        # COMPOSES the sibling reason with whatever candidate/score/reason its
        # own typed outcome carried, so no gate evidence is lost (#6883 review).
        sibling = self._batch_duplicate_of(proposed)  # earlier siblings only
        self._seen_create_intents.append(
            (proposed.id, proposed.title or "", proposed.body or "")
        )
        if sibling is not None:
            self.actions.extend(
                self._concrete_decision(
                    proposed,
                    gate_reason=compose_gate_note(
                        batch_duplicate_note(sibling),
                        outcome_gate_note(outcome, execute=execute),
                    ),
                )
            )
            return
        if isinstance(outcome, CommentExisting):
            # First occurrence of this content: route the observation onto the
            # verified, granted existing issue.
            self.actions.append(
                self._dedup_comment_action(proposed, outcome.issue_number)
            )
            return
        self.actions.extend(
            self._concrete_decision(
                proposed, gate_reason=outcome_gate_note(outcome, execute=execute)
            )
        )

    def _batch_duplicate_of(self, proposed: ProposedTechLeadAction) -> str | None:
        """Action id of an earlier create_issue intent this decision already
        processed whose content matches ``proposed`` (else None) — regardless of
        whether that earlier sibling filed a new issue or routed onto an existing
        one. Uses the same lexical threshold as the corpus backstop and the same
        ``dedup.enabled`` toggle, so intra-decision dedup and against-the-backlog
        dedup agree and turn off together."""
        dedup = self.config.tech_lead.dedup
        if not dedup.enabled:
            return None
        for action_id, title, body in self._seen_create_intents:
            if (
                similarity(proposed.title or "", proposed.body or "", title, body)
                >= dedup.similarity_threshold
            ):
                return action_id
        return None

    def _plan_decision_tier(self, proposed: ProposedTechLeadAction) -> None:
        # Execute authority -> the concrete action(s). Propose-authority
        # create_issue -> the issue is CREATED, gated with proposed-tech-lead
        # (#6778): per-instance approval is removing the label, after which the
        # issue flows into normal scheduling. Everything else propose -> shadow
        # record. create_issue additionally routes through the dedup gate.
        execute = (
            self.config.tech_lead.authority.mode_for(proposed.action_type) == "execute"
        )
        if not execute and proposed.action_type != "create_issue":
            self._surface_shadow(proposed)
            return
        if proposed.action_type == "create_issue":
            self._plan_create_issue(proposed, execute=execute)
            return
        self.actions.extend(self._concrete_decision(proposed, gate_reason=None))


def _shadow_digest_comment(
    shadow: list[SurfaceTechLeadProposalAction],
    *,
    anchor_issue_number: int,
    expected: "ExpectedState",
) -> AddCommentAction:
    """Durable would-have-done record for shadow proposals (#6761 finding 6).

    Trace events are ephemeral; the operator-facing escalation surface is the
    crash-safe GitHub comment/label channel. One digest comment per completion
    keeps the record bounded while listing every proposal the configured
    authority did not execute.
    """
    lines = [
        "## 🔍 Tech Lead proposals recorded, not executed (shadow mode)",
        "",
        "The tech_lead decision proposed the following actions. Configured"
        " authority is `propose` for them, so the orchestrator recorded"
        " them as *would-have-done* instead of executing (ADR-0031):",
        "",
    ]
    for item in shadow:
        target = f"#{item.target_number}" if item.target_number else "n/a"
        title = f" — {item.title}" if item.title else ""
        lines.append(
            f"- **{item.action_id}** `{item.proposal_type}` (target: {target}){title}"
        )
        if item.body_preview:
            lines.append(f"  > {item.body_preview}")
        if item.finding_ids:
            lines.append(f"  findings: {', '.join(item.finding_ids)}")
    # Only immediate/report-tier types reach the shadow digest (#6778):
    # create_issue proposals become gated issues, and act-level proposals
    # become gated proposal issues — the anchor gets a per-proposal link
    # comment from the creation applier instead of a digest entry. Every
    # remaining shadow type is a real, flip-able authority knob.
    knob_types = sorted({item.proposal_type for item in shadow})
    lines.append("")
    knobs = ", ".join(f"`tech_lead.authority.{name}`" for name in knob_types)
    lines.append(
        f"*Flip {knobs} to `execute` to let the orchestrator perform these next time.*"
    )
    return AddCommentAction(
        number=anchor_issue_number,
        comment="\n".join(lines),
        is_pr=False,
        reason="tech_lead decision: durable shadow-proposal record (would-have-done)",
        expected=expected,
    )
