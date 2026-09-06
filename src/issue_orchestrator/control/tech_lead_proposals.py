"""Gated tech_lead proposal issues (#6778, amends ADR-0031 §2).

Consequential tech_lead proposals become **gated GitHub issues** carrying
:data:`~..domain.tech_lead_session.PROPOSED_TECH_LEAD_LABEL`. Removing the label is
per-instance operator approval. This module is the single policy owner for
the whole gated lifecycle:

* **Composition** — :func:`build_tech_lead_proposal_issue_action` turns an
  act-level decision proposal under ``propose`` authority into a
  :class:`CreateTechLeadProposalIssueAction` carrying the typed
  :class:`StoredTechLeadOp`. The issue body is human documentation ONLY.
* **Creation boundary** —
  :func:`tech_lead_issue_creation.apply_create_tech_lead_issue` is the shared
  create-issue executor. Proposal creations record the op create-once in the
  orchestrator-owned authority store and link the issue from the tech_lead
  session's anchor. Execution later consumes only the stored op, so editing
  the issue body after creation has zero effect (the tamper boundary).
* **Ledger dedup** — one open proposal per (op, target):
  :func:`build_op_ledger` projects the store's rows; a duplicate proposal
  plans an :class:`AddCommentAction` on the existing proposal issue instead
  of filing a second one (:func:`build_duplicate_proposal_comment`).
* **Approval backlog** — :func:`observe_gated_tech_lead_proposals` answers
  "what is waiting on the operator?" from LABEL truth over the open issues a
  tick already observed, because only act-level proposals leave a ledger row
  (#7014). It is the visibility counterpart to reconciliation below.
* **Reconciliation** — :func:`reconcile_tech_lead_proposals` is the lifecycle
  owner that partitions the fact gatherer's EXHAUSTIVE open-issue scan (#6779
  R2/R4) against the durable ledger in one pass: a gate-labeled issue is an
  open proposal; an op-backed issue WITHOUT the gate label was approved; a
  ledger row whose issue is absent from the scan is only a CANDIDATE for
  terminal cleanup (#6779 R7) — the scan can be truncated, so absence alone
  never proves terminality. Reconciliation stays READ-ONLY: it classifies but
  does not mutate the ledger. Anchor classification runs on the remainder so a
  proposal issue can never be mistaken for a batch/health anchor.
* **Terminal cleanup** — :func:`apply_discard_terminal_tech_lead_proposal_ops` is
  the single mutating boundary the applier invokes on a
  :class:`DiscardTerminalTechLeadProposalOpsAction` the planner emitted from the
  absent-candidate fact. It CONFIRMS each candidate with a fresh targeted read
  before discarding, so a paginated scan gap can never delete a live op.
* **Approval planning** — :func:`plan_approved_tech_lead_op_executions` turns
  approved ops into the typed execution actions (``reset_retry`` reuses the
  #6777 executor + stale policy verbatim; ``kill_hung_session`` uses its own
  executor in ``tech_lead_kill_session``).
* **Terminal handling** — :func:`finalize_tech_lead_op_execution` posts the
  outcome comment on the proposal issue, closes it, and discards the op for
  executed AND stale outcomes (stale = "preconditions no longer hold": the
  executor posted no mutations). A loud executor failure leaves the op in
  place so the next tick retries. ``discard_op`` after terminal handling
  plus create-once recording makes ops execute at most once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence, TypeVar

from ..domain.tech_lead_session import (
    PROPOSED_TECH_LEAD_LABEL,
    ApprovedTechLeadOp,
    GatedTechLeadProposal,
    StoredTechLeadOp,
    TechLeadCreationOrigin,
    TechLeadSessionGeneration,
    is_proposed_tech_lead_gate,
)
from .actions import (
    Action,
    ActionResult,
    CreateTechLeadProposalIssueAction,
    DiscardTerminalTechLeadProposalOpsAction,
    KillHungSessionAction,
    ResetRetryIssueAction,
)
from .reconciliation import build_expected_for_mutation
from .tech_lead_reset_retry import STALE_DOWNGRADE_MODE

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import ProposedTechLeadAction
    from ..infra.config import Config
    from ..ports import RepositoryHost
    from ..ports.issue import Issue
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)

# The two act-level op actions the consent-gated execution owner handles;
# mirrors the applier's constrained TypeVar so the thin dispatch preserves the
# concrete action type through the consent gate into finalize.
_TechLeadOpAction = TypeVar(
    "_TechLeadOpAction", ResetRetryIssueAction, KillHungSessionAction
)

# Exhaustive open tech-lead-agent scan bound (#6779 R4). Both the per-tick fact
# gatherer and startup recovery page the COMPLETE open set so a backlog of
# gated proposals can never push an older approved op or a batch/health anchor
# past a small window. The value is a runaway backstop, not an expected size:
# the GitHub adapter pages until a short page, capped here so an unbounded
# scan fails loud rather than looping. Realistic open tech-lead-agent issue
# counts (≤2 anchors + a handful of proposals) are orders of magnitude below.
TECH_LEAD_PROPOSAL_SCAN_LIMIT = 2000

# Human-facing verbs per op type, used in proposal issue titles/bodies.
# Titles must never contain "Batch Review"/"Tech Lead Review" (the historical
# batch-anchor title heuristic), and classification additionally excludes
# gate-labeled/op-backed issues before that heuristic runs.
_OP_TITLES: dict[str, str] = {
    "reset_retry": "reset & retry issue #{target} from scratch",
    "kill_hung_session": "kill hung session for issue #{target}",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def proposal_issue_labels(config: "Config") -> tuple[str, ...]:
    """Labels for a gated act-level proposal issue.

    The tech lead agent label keeps the proposal inside the fact gatherer's ONE
    anchor scan; the filtering label keeps it inside the active scope (the
    anchor classifier ignores out-of-scope issues); the gate label blocks
    pickup and is the approval affordance. Orchestrator-attached: the gate
    label is exempt here and ONLY here — the agent-label allowlist rejects it.
    """
    return tuple(
        value
        for value in (
            config.tech_lead_review_agent,
            config.filtering.label,
            PROPOSED_TECH_LEAD_LABEL,
        )
        if value
    )


def build_stored_tech_lead_op(
    proposed: "ProposedTechLeadAction",
    *,
    source_run_id: str,
    source_session_name: str,
    target_session: TechLeadSessionGeneration | None = None,
    now_iso: str | None = None,
) -> StoredTechLeadOp:
    """The orchestrator-side executable payload for an act-level proposal.

    ``target_session`` binds a ``kill_hung_session`` op to the exact trusted
    generation observed at tech-lead launch (#6779 R1); it stays absent for
    ``reset_retry`` (label/no-session stale-checked).
    ``proposed.finding_ids`` are persisted so execution correlates to the
    findings the approver saw (#6779 R6).
    """
    assert proposed.target_number is not None  # enforced by validate()
    return StoredTechLeadOp(
        op_type=proposed.action_type,
        target_issue_number=proposed.target_number,
        rationale=proposed.body or "",
        source_run_id=source_run_id,
        source_session_name=source_session_name,
        source_action_id=proposed.id,
        created_at=now_iso or _utc_now_iso(),
        target_session_id=target_session.run_id if target_session else "",
        target_terminal_id=target_session.terminal_id if target_session else "",
        target_session_type=(target_session.task_kind.value if target_session else ""),
        finding_ids=tuple(proposed.finding_ids),
    )


def _proposal_issue_body(
    op: StoredTechLeadOp, *, anchor_issue_number: int, finding_ids: Sequence[str]
) -> str:
    findings = ", ".join(finding_ids) or "none"
    # kill_hung_session binds approval to one live session generation (#6779
    # R1): show the run id the operator is consenting to terminate so an
    # execution that no-ops on a replacement is auditable against this body.
    session_row = (
        f"| Target session | `{op.target_session_type}` terminal"
        f" `{op.target_terminal_id}`, run `{op.target_session_id}`"
        " (approval kills only this generation) |\n"
        if op.op_type == "kill_hung_session"
        else ""
    )
    return f"""## Gated tech_lead proposal (ADR-0031 §2)

A tech_lead session proposed an act-level operation. It is **inert** until a
human approves it.

| | |
|---|---|
| Operation | `{op.op_type}` |
| Target | #{op.target_issue_number} |
{session_row}| Proposed by | session `{op.source_session_name}` (run `{op.source_run_id}`, action {op.source_action_id}) |
| Anchor issue | #{anchor_issue_number} |
| Findings | {findings} |

### Rationale

{op.rationale}

### How to approve

**Remove the `{PROPOSED_TECH_LEAD_LABEL}` label.** The orchestrator re-validates
the operation's preconditions against current state and executes it exactly
once, then closes this issue with the outcome. If the preconditions no longer
hold, it comments and closes without acting.

To reject, close this issue.

> This body is documentation only. The executable payload was recorded
> orchestrator-side when this issue was created; editing this issue has no
> effect on what runs.
"""


def build_tech_lead_proposal_issue_action(
    proposed: "ProposedTechLeadAction",
    *,
    config: "Config",
    anchor_issue_number: int,
    source_run_id: str,
    source_session_name: str,
    expected: "ExpectedState",
    target_session: TechLeadSessionGeneration | None = None,
    now_iso: str | None = None,
) -> CreateTechLeadProposalIssueAction:
    """Compose the gated proposal issue creation for an act-level proposal.

    ``target_session`` is the trusted target generation observed at launch,
    bound onto the stored op so kill approval consents to that exact runtime.
    """
    op = build_stored_tech_lead_op(
        proposed,
        source_run_id=source_run_id,
        source_session_name=source_session_name,
        target_session=target_session,
        now_iso=now_iso,
    )
    title_detail = _OP_TITLES[op.op_type].format(target=op.target_issue_number)
    return CreateTechLeadProposalIssueAction(
        title=f"Tech Lead proposal: {title_detail}",
        body=_proposal_issue_body(
            op,
            anchor_issue_number=anchor_issue_number,
            finding_ids=proposed.finding_ids,
        ),
        labels=proposal_issue_labels(config),
        pr_count=0,
        op=op,
        origin=TechLeadCreationOrigin.derived_from_anchor(anchor_issue_number),
        reason=(
            f"tech_lead decision action {proposed.id}: gated {op.op_type} proposal"
            f" for issue #{op.target_issue_number} (#6778)"
        ),
        expected=expected,
    )


def build_op_ledger(
    ops: Iterable[tuple[int, StoredTechLeadOp]],
) -> dict[tuple[str, int], int]:
    """Project store rows to a (op_type, target) -> proposal-issue map.

    The store row lifetime IS the "open proposal" window: rows are created
    with the proposal issue and discarded at terminal handling, so this
    ledger enforces one open proposal per (op, target) without a GitHub read.
    """
    return {
        (op.op_type, op.target_issue_number): issue_number for issue_number, op in ops
    }


def build_duplicate_proposal_comment(
    proposed: "ProposedTechLeadAction", *, anchor_issue_number: int
) -> str:
    """Comment for a re-proposal of an already-open (op, target) proposal."""
    return (
        "## 🔁 Proposed again by tech_lead\n\n"
        f"A tech_lead session (anchor #{anchor_issue_number}, action"
        f" {proposed.id}) proposed `{proposed.action_type}` for"
        f" #{proposed.target_number} again. This open proposal already covers"
        f" it — remove the `{PROPOSED_TECH_LEAD_LABEL}` label to approve.\n\n"
        f"### Latest rationale\n\n{proposed.body or ''}"
    )


@dataclass(frozen=True)
class ReconciledTechLeadProposals:
    """Live partition of the EXHAUSTIVE open tech-lead-agent scan vs the ledger.

    The single lifecycle-owner view every caller reads instead of re-deriving
    proposal state from an open-only scan (#6779 R2). A live proposal (gated or
    approved) always carries the tech-lead-agent label and is open, so its
    presence in the scan is authoritative: gate-labeled -> open proposal,
    op-backed-without-gate -> approved.

    Absence is NOT authoritative, though (#6779 R7): the exhaustive scan can be
    truncated by a later-page API failure or a >2000-issue repo, dropping a
    still-open proposal from the result. So ``absent_op_issue_numbers`` are
    only CANDIDATES for terminal cleanup — the discard owner
    (:func:`apply_discard_terminal_tech_lead_proposal_ops`) confirms each with a
    fresh targeted read before deleting its ledger row.
    """

    anchor_candidate_issues: list["Issue"]  # -> batch/health anchor classifier
    approved: tuple[ApprovedTechLeadOp, ...]  # gate removed -> execute
    # Ledger rows whose proposal issue was absent from the exhaustive scan:
    # candidates for cleanup, confirmed terminal (deleted/closed) before discard.
    absent_op_issue_numbers: tuple[int, ...]


def _issue_carries_gate(issue: "Issue") -> bool:
    """True iff *issue* carries the owned proposal gate, case-insensitively.

    The ONE gate predicate shared by reconciliation classification and the
    apply-time consent re-check (#6779 R15/R16), delegating the case fold to
    :func:`is_proposed_tech_lead_gate`. GitHub folds label names, so a repo whose
    canonical spelling is ``Proposed-Tech-Lead`` still gates: classification and
    blocking cannot diverge on case.
    """
    return any(is_proposed_tech_lead_gate(name) for name in issue.labels)


def reconcile_tech_lead_proposals(
    issues: Sequence["Issue"],
    *,
    ops: Mapping[int, StoredTechLeadOp],
) -> ReconciledTechLeadProposals:
    """Classify the exhaustive open scan against the durable ledger.

    One pass reconciles every proposal transition so callers cannot mistake a
    stale row for a live proposal:

    * gate-labeled open issues are open proposals — inert, nothing to execute;
    * op-backed open issues WITHOUT the gate label were approved (the operator
      removed it) — returned for the planner to execute;
    * ledger rows whose proposal issue is absent from the scan are only
      CANDIDATES for terminal cleanup (#6779 R7): most were closed manually or
      leaked by a finalize that crashed before ``discard_op``, but a truncated
      scan (a later-page API failure, or a repo with more open issues than the
      scan cap) can also drop a still-open proposal. Reconciliation is
      read-only, so it returns the candidate numbers without deleting anything;
      the confirm-and-discard owner re-reads each before cleanup;
    * everything else flows on to the batch/health anchor classifier.
    """
    open_numbers = {issue.number for issue in issues}
    remaining: list["Issue"] = []
    approved: list[ApprovedTechLeadOp] = []
    for issue in issues:
        if _issue_carries_gate(issue):
            continue  # open proposal (or foreign gate-labeled issue): inert
        op = ops.get(issue.number)
        if op is not None:
            approved.append(
                ApprovedTechLeadOp(proposal_issue_number=issue.number, op=op)
            )
            continue
        remaining.append(issue)
    absent = tuple(sorted(number for number in ops if number not in open_numbers))
    return ReconciledTechLeadProposals(
        anchor_candidate_issues=remaining,
        approved=tuple(approved),
        absent_op_issue_numbers=absent,
    )


def observe_approval_backlog(
    repository_host: "RepositoryHost",
    config: "Config",
    *partial: Sequence["Issue"],
) -> tuple[GatedTechLeadProposal, ...]:
    """The backlog as the board should publish it: complete, and this tick's.

    Composes the two halves so no caller has to remember to do both. The sets
    a tick already holds go in first (free, and the freshest evidence about
    the issues they cover), and the authoritative gate-label query goes last
    so it decides the ones only it can see.

    Complete on purpose, because the board is written straight from the
    result. The alternative — publishing a partial observation and retaining
    what it missed — trades erasing a pending approval for advertising one
    the operator already approved, which is the failure ``_build_view``'s
    docstring warns about and #7014's own symptom.
    """
    return observe_gated_tech_lead_proposals(
        *partial, discover_open_gated_proposals(repository_host, config)
    )


def discover_open_gated_proposals(
    repository_host: "RepositoryHost", config: "Config"
) -> list["Issue"]:
    """AUTHORITATIVE observation of the approval backlog, in its own scope.

    The backlog is defined by a LABEL, so the only complete observation of it
    is a query for that label. Everything the tick already holds is a query
    for something else that merely overlaps:

    - the worker board is narrowed by configured agents, milestones, exclusion
      filters and a fetch limit — it fetches runnable work, not approvals;
    - the anchor scan queries the TECH-LEAD agent label, while a promoted
      finding carries the TARGET'S worker agent label so it is
      "DISCOVERABLE the moment the gate comes off"
      (:func:`~.tech_lead_finding_promotion.promotion_issue_labels`) — and is
      therefore structurally invisible to an agent-scoped scan.

    Joining those two does not produce a complete set; it produces two
    incomplete ones. This costs one labelled query on ticks that already do
    tech-lead work, and it is what lets the board be written straight from
    the facts: a complete observation needs no retention, and retention is
    what would let an approved proposal linger (the warning in
    ``_build_view``'s own docstring).

    ``exhaustive`` for the same reason the anchor scan is (#6779 R17): a
    dropped page must RAISE rather than return a silently partial set a caller
    would read as "fewer approvals pending".
    """
    from .health_review_trigger import _scoped_issues

    issues = repository_host.list_issues(
        labels=[
            value
            for value in (PROPOSED_TECH_LEAD_LABEL, config.filtering.label)
            if value
        ],
        state="open",
        limit=TECH_LEAD_PROPOSAL_SCAN_LIMIT,
        exhaustive=True,
    )
    return _scoped_issues(issues, config.filtering.label)


def observe_gated_tech_lead_proposals(
    *observed: Sequence["Issue"],
) -> tuple[GatedTechLeadProposal, ...]:
    """The approval backlog as LABEL truth: every open gate-labeled issue (#7014).

    The lifecycle owner's answer to "what is waiting on the operator?", and the
    counterpart to :func:`reconcile_tech_lead_proposals`, which classifies the
    same gate against the op ledger. Reconciliation exists to decide what to
    EXECUTE, so it can afford to look only at ledger-backed issues; visibility
    cannot. Act-level op proposals are the only gated issues that leave a
    ``tech_lead_proposal_ops`` row — promoted findings (#6957) and plain
    follow-up issues carry the same gate with no row at all — so a projection
    built from the ledger renders "no open proposals" while a backlog of gated
    issues waits on GitHub. That is what #7014 hid: 20 gated issues, empty
    ledger, and an operator board that said ``None.``

    Callers pass whatever open-issue sets the tick ALREADY holds (the runnable
    board fetch, the exhaustive anchor scan); the union is deduplicated by
    issue number and ordered by it, so the projection is deterministic and
    costs zero GitHub calls. Only OPEN issues qualify: a closed issue is
    rejected or already handled, never awaiting approval.

    Pass EVERY set the tick holds, not just the worker board. That board is
    narrowed by configured agents, milestones, exclusion filters and a fetch
    limit, so it is a runnable-work fetch rather than the approval scope — a
    gated issue outside it is still waiting on an operator. Feeding only the
    board is how this projection could report nothing on a tick that had
    already fetched a pending approval through the anchor scan.
    """
    # One issue can appear in several observed sets; the LAST observation of it
    # wins, so a set gathered later in the tick refreshes an earlier snapshot.
    # Resolve the LATEST observation of each issue FIRST, then judge approval
    # state. Filtering first meant a later observation that does NOT await
    # approval was discarded instead of superseding the earlier one, so an
    # issue approved or closed between two of this tick's fetches stayed
    # advertised as pending despite fresher evidence in the same tick. "Last
    # observation wins" has to include observing that it is no longer waiting.
    latest: dict[int, "Issue"] = {
        issue.number: issue for issues in observed for issue in issues
    }
    backlog: dict[int, GatedTechLeadProposal] = {
        number: _gated_proposal_summary(issue)
        for number, issue in latest.items()
        if _awaits_approval(issue)
    }
    return tuple(backlog[number] for number in sorted(backlog))


def _gated_proposal_summary(issue: "Issue") -> GatedTechLeadProposal:
    """Project one observed issue onto the backlog fact."""
    return GatedTechLeadProposal(
        issue_number=issue.number,
        title=issue.title,
        created_at=issue.created_at or "",
    )


def _awaits_approval(issue: "Issue") -> bool:
    """True iff *issue* is open and still carries the operator-approval gate."""
    return issue.state == "open" and _issue_carries_gate(issue)


def _proposal_issue_is_open(tracker: "RepositoryHost", issue_number: int) -> bool:
    """Fresh targeted read: is this proposal issue confirmably still open?

    The exhaustive open scan can be truncated — a later-page API failure, or a
    repo with more open issues than :data:`TECH_LEAD_PROPOSAL_SCAN_LIMIT` — so a
    ledger row's absence from it is only a candidate for cleanup (#6779 R7).
    This re-reads the ONE issue directly: ``open`` means the scan had a gap and
    the op is live; ``closed`` or absent (deleted) means the proposal is
    genuinely terminal. A transient read error raises out of ``get_issue_state``
    and aborts the whole discard action, so a momentary API failure never
    deletes a live op.
    """
    return tracker.get_issue_state(issue_number) == "open"


def apply_discard_terminal_tech_lead_proposal_ops(
    action: Action,
    *,
    tracker: "RepositoryHost | None",
    authority: "TechLeadAuthorityStore | None",
) -> ActionResult:
    """Confirm-and-discard terminal gated-proposal ledger rows (#6779 R7/R10).

    The single mutating boundary for proposal-op cleanup, invoked by the
    applier off the read-only fact path. :func:`reconcile_tech_lead_proposals`
    only CLASSIFIES which ledger rows were absent from the exhaustive scan;
    the planner surfaces those numbers as a
    :class:`DiscardTerminalTechLeadProposalOpsAction`; this owner CONFIRMS each
    candidate with a fresh targeted read before discarding.

    A still-open candidate is a scan gap and its op is PRESERVED (never
    deleted); a closed or deleted candidate is genuinely terminal and its op
    is discarded. Discards are idempotent (``discard_op`` no-ops on an absent
    row), so a candidate confirmed terminal but re-emitted next tick self-heals.
    """
    assert isinstance(action, DiscardTerminalTechLeadProposalOpsAction)
    if tracker is None or authority is None:
        return ActionResult.fail(
            action,
            "terminal tech_lead proposal cleanup requires repository_host and the"
            " TechLeadAuthorityStore wired into this applier",
        )
    discarded: list[int] = []
    preserved: list[int] = []
    for issue_number in action.candidate_issue_numbers:
        if _proposal_issue_is_open(tracker, issue_number):
            preserved.append(issue_number)
            logger.info(
                "[tech_lead] Proposal #%d absent from the open scan but still open:"
                " preserving its ledger op (scan gap, #6779 R7)",
                issue_number,
            )
            continue
        authority.discard_op(issue_number=issue_number)
        discarded.append(issue_number)
        logger.info(
            "[tech_lead] Confirmed terminal proposal #%d: discarded its leaked"
            " ledger op (#6779 R7/R10)",
            issue_number,
        )
    return ActionResult.ok(
        action,
        discarded_op_count=len(discarded),
        preserved_op_count=len(preserved),
    )


def plan_approved_tech_lead_op_executions(
    approved: Sequence[ApprovedTechLeadOp],
) -> list[Action]:
    """Turn approved stored ops into their typed execution actions.

    The proposal issue is the surface the operator approved on, so it is
    also the event/downgrade anchor for the execution. Precondition
    re-validation (#6777's stale policy for ``reset_retry``; the
    active-session policy for ``kill_hung_session``) happens in the
    executors at apply time — planning stays read-free.
    """
    actions: list[Action] = []
    for item in approved:
        op = item.op
        reason = (
            f"approved tech_lead proposal #{item.proposal_issue_number}:"
            f" execute {op.op_type} for issue"
            f" #{op.target_issue_number} (#6778)"
        )
        # Both executors carry the approved findings so TECH_LEAD_ACTION_EXECUTED
        # correlates back to what the approver saw (#6779 R6). kill also carries
        # the session generation it consented to terminate (#6779 R1); any other
        # act-level op is reset_retry (StoredTechLeadOp validated op_type).
        if op.op_type == "kill_hung_session":
            actions.append(
                KillHungSessionAction(
                    issue_number=op.target_issue_number,
                    rationale=op.rationale,
                    proposal_id=op.source_action_id,
                    finding_ids=op.finding_ids,
                    anchor_issue_number=item.proposal_issue_number,
                    proposal_issue_number=item.proposal_issue_number,
                    target_session_id=op.target_session_id,
                    target_terminal_id=op.target_terminal_id,
                    target_session_type=op.target_session_type,
                    reason=reason,
                    expected=build_expected_for_mutation(),
                )
            )
        else:
            actions.append(
                ResetRetryIssueAction(
                    issue_number=op.target_issue_number,
                    rationale=op.rationale,
                    proposal_id=op.source_action_id,
                    finding_ids=op.finding_ids,
                    anchor_issue_number=item.proposal_issue_number,
                    proposal_issue_number=item.proposal_issue_number,
                    reason=reason,
                    expected=build_expected_for_mutation(),
                )
            )
        logger.info(
            "Planner: approved tech_lead proposal #%d -> %s for issue #%d",
            item.proposal_issue_number,
            op.op_type,
            op.target_issue_number,
        )
    return actions


def _terminal_outcome_comment(
    result: ActionResult, action: Action, op_type: str, target: int
) -> str | None:
    """The proposal-issue terminal comment, or None for non-terminal results."""
    if result.success:
        return (
            "## ✅ Approved tech_lead operation executed\n\n"
            f"`{op_type}` for #{target} was executed after re-validating its"
            " preconditions. Closing this proposal."
        )
    if result.details.get("mode") == STALE_DOWNGRADE_MODE:
        stale = result.details.get("skip_reason", "preconditions no longer hold")
        return (
            "## ⏸️ Preconditions no longer hold\n\n"
            f"`{op_type}` for #{target} was approved, but re-validation found"
            f" the recorded preconditions stale: {stale}\n\n"
            "No changes were made. Closing this proposal."
        )
    return None


def finalize_tech_lead_op_execution(
    result: ActionResult,
    action: "ResetRetryIssueAction | KillHungSessionAction",
    *,
    repository_host: "RepositoryHost | None",
    ops: "TechLeadAuthorityStore | None",
) -> ActionResult:
    """Terminal handling for a proposal-linked op execution (once-only owner).

    Executed and stale outcomes both terminate the proposal: outcome comment,
    close, ``discard_op`` — in that order, so a crash mid-finalize leaves the
    issue open and the next tick retries finalization (the reset executor's
    own stale policy makes a re-run of an already-applied reset downgrade
    instead of double-executing). Executor FAILURES are not terminal: the op
    row stays and the next tick retries the execution loudly.

    Direct execute-authority actions (``proposal_issue_number == 0``) pass
    through untouched.
    """
    proposal_issue = getattr(action, "proposal_issue_number", 0)
    if not proposal_issue:
        return result
    op_type = (
        "reset_retry"
        if isinstance(action, ResetRetryIssueAction)
        else "kill_hung_session"
    )
    comment = _terminal_outcome_comment(result, action, op_type, action.issue_number)
    if comment is None:
        return result  # loud failure: keep the op, retry next tick
    if repository_host is None or ops is None:
        return ActionResult.fail(
            action,
            "tech_lead proposal finalization requires repository_host and the"
            " TechLeadAuthorityStore wired into this applier",
        )
    try:
        repository_host.add_comment(proposal_issue, comment)
        repository_host.update_issue_state(proposal_issue, "closed")
        ops.discard_op(issue_number=proposal_issue)
    except Exception as e:
        logger.exception(
            "Failed to finalize tech_lead proposal #%d after %s",
            proposal_issue,
            op_type,
        )
        return ActionResult.fail(
            action,
            f"op outcome reached but proposal #{proposal_issue} finalization"
            f" failed: {e}",
            proposal_issue_number=proposal_issue,
        )
    logger.info(
        "[tech_lead] Proposal #%d finalized (%s, success=%s)",
        proposal_issue,
        op_type,
        result.success,
    )
    return result


def _approval_confirmed(repository_host: "RepositoryHost", proposal_issue: int) -> bool:
    """Fresh read: True iff the proposal still openly holds operator approval.

    Approval STILL STANDS only when a fresh read shows the proposal issue open
    AND no longer gated (case-insensitive via the one shared predicate, #6779
    R15/R16); a re-added gate, a closed issue, or a deleted issue each withdraw
    it. Fail-safe: a read that raises is UNCONFIRMED (never approval), so the
    caller preserves the op inert rather than act on unverifiable consent.
    """
    try:
        issue = repository_host.get_issue(proposal_issue)
    except Exception:
        logger.exception(
            "[tech_lead] Fresh consent read for proposal #%d failed; treating"
            " approval as unconfirmed and preserving the op (#6779 R16)",
            proposal_issue,
        )
        return False
    if issue is None:
        return False  # proposal deleted -> gone, not approved
    if issue.state != "open":
        return False  # proposal closed -> rejected/terminal
    return not _issue_carries_gate(issue)  # re-gated -> approval withdrawn


def _withheld_for_withdrawn_approval(
    action: "_TechLeadOpAction",
    repository_host: "RepositoryHost | None",
) -> ActionResult | None:
    """None when approval still stands, else the inert result to return.

    The consent gate the lifecycle owner runs immediately before a target
    mutation. Direct execute-authority actions (``proposal_issue_number == 0``)
    carry no per-instance gate and pass straight through (None). Otherwise a
    fresh read decides: still approved -> None (proceed); withdrawn or
    unconfirmable -> a non-terminal failure that PRESERVES the op (executor not
    run, proposal not finalized) so the next tick re-reads it as an inert
    proposal.
    """
    proposal_issue = getattr(action, "proposal_issue_number", 0)
    if not proposal_issue:
        return None
    if repository_host is None:
        return ActionResult.fail(
            action,
            "approved tech_lead op consent re-check requires repository_host"
            " wired into this applier",
        )
    if _approval_confirmed(repository_host, proposal_issue):
        return None
    logger.info(
        "[tech_lead] Proposal #%d no longer confirms operator approval before"
        " apply (re-gated, closed, or unreadable): preserving its op inert"
        " (#6779 R16)",
        proposal_issue,
    )
    return ActionResult.fail(
        action,
        f"proposal #{proposal_issue} no longer confirms operator approval;"
        " op preserved inert",
        proposal_issue_number=proposal_issue,
    )


def execute_approved_tech_lead_op(
    action: "_TechLeadOpAction",
    apply_fn: "Callable[[_TechLeadOpAction], ActionResult]",
    *,
    repository_host: "RepositoryHost | None",
    ops: "TechLeadAuthorityStore | None",
) -> ActionResult:
    """Consent-gated execution boundary for an approved gated-proposal op.

    The proposal lifecycle owner the applier dispatches an approved act-level op
    to (the applier stays a thin dispatch). Immediately before the target
    mutation it re-confirms per-instance approval with a FRESH read
    (:func:`_withheld_for_withdrawn_approval`), then runs the executor and
    finalizes. Consent is re-checked HERE, not snapshotted at plan time: an
    operator who removes the gate, lets the scan plan the op, then re-adds the
    gate before apply has the op preserved inert rather than executed and closed
    (#6779 R16, the undoable-until-executed property). Read failures fail safe.
    """
    inert = _withheld_for_withdrawn_approval(action, repository_host)
    if inert is not None:
        return inert
    return finalize_tech_lead_op_execution(
        apply_fn(action), action, repository_host=repository_host, ops=ops
    )
