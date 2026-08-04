"""Tech-lead action dataclasses (ADR-0031 / #6778 / #6781 / #6957).

Split out of ``actions`` for cohesion and its line budget as the tech-lead
surface grew: gated proposal issues, pattern case files, act-level ops, and the
finding-promotion lane. The split is ONE-WAY and the dependency ROOT is
``action_base``: this module imports :class:`~.action_base.Action` and
:class:`~.action_base.ActionType` from there directly — never from ``actions``,
which imports THIS module and would therefore be only partially initialized
(#6957 review F7). ``actions`` re-exports every name here, so importers are
unaffected. ``tests/unit/control/test_action_module_boundaries.py`` pins the
direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..domain.models import DiscoveredFailure
from ..domain.tech_lead_milestone import TechLeadMilestoneIntent
from ..domain.tech_lead_session import TechLeadSessionFlavor
from .action_base import Action, ActionType

if TYPE_CHECKING:
    from ..domain.tech_lead_findings import PatternObservation
    from ..domain.tech_lead_session import StoredTechLeadOp


# These actions deliberately share one apply-time owner: all create a
# tech-lead-authored issue, while proposal and case-file variants additionally
# finalize their respective authority-ledger record.
TECH_LEAD_ISSUE_CREATION_ACTION_TYPES: frozenset[ActionType] = frozenset(
    {
        ActionType.CREATE_TECH_LEAD_ISSUE,
        ActionType.CREATE_TECH_LEAD_PROPOSAL_ISSUE,
        ActionType.CREATE_TECH_LEAD_CASE_FILE_ISSUE,
    }
)

#: A command whose reconciliation subject is "no managed-repo issue at all".
#: Only two tech-lead mutations legitimately have none: creating the ANCHOR
#: issue itself (there is nothing yet to reconcile against) and discarding
#: terminal ledger rows (a purely orchestrator-side write). Anything else with
#: this subject is a composition bug, and the dispatch guard fails it closed.
NO_RECONCILIATION_SUBJECT = 0


@runtime_checkable
class TechLeadMutation(Protocol):
    """A tech-lead command that MUTATES, and names what it reconciles against.

    Every mutating tech-lead command crosses the applier's optimistic-concurrency
    gate before it writes, and that gate needs an issue to read labels from. The
    dispatch table used to supply those subjects by hand for the four commands
    someone remembered, which is exactly why issue creation kept slipping past
    it: a case file or proposal could still be filed against a source anchor
    paused behind ``io:needs-reconcile`` (#6957 round-6 review F3/A3).

    So the SUBJECT is part of each command's own contract, not a lookup table
    the registry has to keep in sync. A command that mutates and does not
    implement this protocol cannot be dispatched.
    """

    def reconciliation_subject(self) -> int:
        """The managed-repo issue whose current labels gate this mutation.

        :data:`NO_RECONCILIATION_SUBJECT` when the command genuinely has none.
        """
        ...


def reconciliation_subject_for(action: Action) -> int:
    """The managed-repo issue *action*'s mutation is checked against.

    Fails loudly for a mutating tech-lead command that never declared one:
    silently skipping the gate is the failure mode this replaces.
    """
    if not isinstance(action, TechLeadMutation):
        raise TypeError(
            f"{type(action).__name__} is dispatched as a mutating tech-lead"
            " command but does not implement TechLeadMutation; every mutating"
            " command must name the issue its reconciliation is checked against"
        )
    return action.reconciliation_subject()


@dataclass(frozen=True)
class CreateTechLeadIssueAction(Action):
    """Create a tech_lead review issue when PR threshold is met.

    The Planner produces this when tech_lead_facts.pr_count >= threshold.
    The orchestrator applies it by creating the GitHub issue. Both creation
    paths — the planner's batch tracking issue and decision-driven follow-up
    issues — share this one action, so the applier is the single milestone
    resolution boundary.
    """

    title: str = ""
    body: str = ""
    labels: tuple[str, ...] = field(default_factory=tuple)
    pr_count: int = 0
    milestone: TechLeadMilestoneIntent = field(default_factory=TechLeadMilestoneIntent)
    # Non-empty only for an immediate problem-storm health review. Preserves
    # the exact discovery facts across create -> durable ledger -> pending
    # queue -> launch, so the cohort the anchor is authorized over is the one
    # that was actually discovered. The board snapshot's failure list is
    # deliberately broader board context and is never the authority (#6780).
    storm_problems: tuple[DiscoveredFailure, ...] = ()
    # The lifecycle variant this anchor is authored as. The owner that decides
    # to create the anchor (health-review trigger vs batch planning) states it
    # here, so the applier reports the decision instead of re-deriving it from
    # marker labels at the creation boundary (#6780).
    flavor: TechLeadSessionFlavor = TechLeadSessionFlavor.BATCH_REVIEW
    # The board fingerprint the health-review trigger fired on, carried to the
    # post-creation stamp so "reviewed" records what justified the review, not a
    # recompute against a board that by then holds this anchor. "" (batch, or no
    # facts) means never-reviewed: fails toward reviewing (ADR-0031 §4, #6793).
    health_review_fingerprint: str = ""
    # Expedite-lane intent (#6870): set for a decision-driven create_issue the
    # tech lead marked urgent. The applier's create boundary reads it (with the
    # gate presence) to front-queue the new issue via the expedite owner.
    expedite: bool = False
    # The tech-lead session anchor this creation was decided from, and therefore
    # the issue its reconciliation gate reads (#6957 round-6 review F3/A3). 0 for
    # the two planner-side creations that AUTHOR an anchor (batch review, health
    # review): there is no prior issue to reconcile against, and they carry no
    # ``expected`` either — the dispatch guard enforces that pairing.
    anchor_issue_number: int = 0
    action_type: ActionType = field(default=ActionType.CREATE_TECH_LEAD_ISSUE, init=False)

    def reconciliation_subject(self) -> int:
        """The anchor issue whose pause label gates this creation."""
        return self.anchor_issue_number


@dataclass(frozen=True)
class CreateTechLeadProposalIssueAction(CreateTechLeadIssueAction):
    """Create a GATED act-level tech_lead proposal issue (#6778, ADR-0031 §2).

    A ``CreateTechLeadIssueAction`` that additionally carries the typed
    :class:`StoredTechLeadOp`. The applier creates the issue AND records the op
    create-once in the orchestrator-owned authority store, keyed by the new
    issue number, then links the proposal from the session's anchor issue.
    The issue body is human documentation only — execution consumes the
    stored op, never the body (tamper boundary).
    """

    op: "StoredTechLeadOp" = field(kw_only=True)
    action_type: ActionType = field(
        default=ActionType.CREATE_TECH_LEAD_PROPOSAL_ISSUE, init=False
    )

    def __post_init__(self) -> None:
        from ..domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL

        # Self-validating type: an ungated proposal issue would be
        # schedulable before any approval. (Baseline note: this branch is an
        # accepted control_policy_branch_sites entry — the invariant is
        # inherently about the gate label, not scattered policy.)
        if PROPOSED_TECH_LEAD_LABEL not in self.labels:
            raise ValueError(
                "CreateTechLeadProposalIssueAction must carry the"
                f" {PROPOSED_TECH_LEAD_LABEL!r} gate label"
            )
        if self.anchor_issue_number <= 0:
            raise ValueError(
                "CreateTechLeadProposalIssueAction requires a positive"
                " anchor_issue_number"
            )


@dataclass(frozen=True)
class CreateTechLeadCaseFileIssueAction(CreateTechLeadIssueAction):
    """Create a pattern CASE-FILE issue for a flag_pattern proposal (#6781).

    A ``CreateTechLeadIssueAction`` that additionally carries the pattern
    signature (the durable ledger key) and optional area. The applier
    creates the issue AND records the (signature -> issue) ledger row
    create-once in the orchestrator-owned authority store; later
    flag_pattern proposals with the same signature comment evidence onto
    the recorded issue instead of filing a second one. The issue body is
    human documentation only — dedup consults the ledger, never the body
    (tamper boundary).
    """

    pattern_signature: str = ""
    area: str | None = None
    # Every observation of this signature the creating decision carried, in
    # order. ``observations[0]`` is the one the issue BODY records; its comment
    # form is what the apply-time reconcile path posts when the ledger already
    # holds the signature. Each carries its own identity, so replaying this
    # action after a partial write re-posts at most a duplicate comment and can
    # never advance the durable evidence count twice (#6957 review F1).
    observations: tuple["PatternObservation", ...] = ()
    # Deterministic remote provenance key, present in ``body``. The case file is
    # created on GitHub before its ledger row is written; this is what lets the
    # applier's case-file owner RECOVER the already-created issue after a crash
    # in that window instead of filing a second one for the same signature
    # (#6957 round-2 review F10).
    idempotency_marker: str = ""
    # The tech lead's promotion classification for this signature (#6957):
    # "code", "human", or "" for unclassified. Recorded on the ledger row at
    # creation so promotion eligibility never has to parse the issue body.
    fix_class: str = ""
    # Original flag_pattern diagnosis/recommended fix. This is persisted with
    # the pattern facts so a later cross-repo promotion is self-contained.
    diagnosis: str = ""
    action_type: ActionType = field(
        default=ActionType.CREATE_TECH_LEAD_CASE_FILE_ISSUE, init=False
    )

    def __post_init__(self) -> None:
        from ..domain.tech_lead_session import require_case_file_observation_label

        # Self-validating type: an empty signature could never accrue
        # evidence. The observation-label invariant is delegated to its
        # domain owner (an unlabeled case file would be schedulable work).
        if not self.pattern_signature.strip():
            raise ValueError(
                "CreateTechLeadCaseFileIssueAction requires a non-empty"
                " pattern_signature (the ledger key)"
            )
        if not self.observations:
            raise ValueError(
                "CreateTechLeadCaseFileIssueAction requires at least one"
                " identified observation (the one its body records)"
            )
        identities = [item.observation_id for item in self.observations]
        if len(set(identities)) != len(identities):
            raise ValueError(
                "CreateTechLeadCaseFileIssueAction observations must have"
                f" distinct identities, got {identities}"
            )
        if self.anchor_issue_number <= 0:
            raise ValueError(
                "CreateTechLeadCaseFileIssueAction requires the positive"
                " anchor_issue_number it was decided from; without it the"
                " creation has no reconciliation subject and could file a case"
                " file against an anchor paused behind io:needs-reconcile"
            )
        if not self.idempotency_marker.strip():
            raise ValueError(
                "CreateTechLeadCaseFileIssueAction requires a non-empty"
                " idempotency_marker so an interrupted creation is recoverable"
            )
        if self.idempotency_marker not in self.body:
            raise ValueError(
                "CreateTechLeadCaseFileIssueAction idempotency_marker must"
                " appear in the issue body; a marker the created issue does not"
                " carry cannot recover it after a crash"
            )
        require_case_file_observation_label(self.labels)

    @property
    def body_observation(self) -> "PatternObservation":
        """The observation the case-file issue BODY records."""
        return self.observations[0]

    @property
    def additional_observations(self) -> tuple["PatternObservation", ...]:
        """Observations from the same decision that append as comments."""
        return self.observations[1:]


@dataclass(frozen=True)
class SurfaceTechLeadProposalAction(Action):
    """Surface a tech_lead decision proposal without executing it (ADR-0031).

    Emitted for propose-mode (shadow) authority, ``flag_pattern`` records,
    and rejected decision artifacts. The applier only publishes a trace
    event (``TECH_LEAD_ACTION_PROPOSED``, or ``TECH_LEAD_DECISION_REJECTED`` when
    ``mode == "rejected"``) — it makes NO GitHub calls.

    ``mode`` values:
    - ``"shadow"`` — propose-mode authority: recorded as would-have-done.
    - ``"pattern"`` — a ``flag_pattern`` proposal (its execution IS the record).
    - ``"rejected"`` — the decision artifact pair failed validation;
      ``proposal_type`` is ``"decision"`` and ``body_preview`` carries the
      failure detail.
    """

    issue_number: int = 0  # The tech_lead session's anchor issue
    action_id: str = ""
    proposal_type: str = ""
    target_number: int = 0  # 0 = no target
    target_is_pr: bool = False
    title: str = ""
    body_preview: str = ""  # Capped at 500 chars by the construction site
    finding_ids: tuple[str, ...] = ()
    mode: str = ""  # "shadow" | "pattern" | "rejected"
    action_type: ActionType = field(
        default=ActionType.SURFACE_TECH_LEAD_PROPOSAL, init=False
    )


@dataclass(frozen=True)
class ResetRetryIssueAction(Action):
    """Execute a tech_lead ``reset_retry`` proposal via the reset owner (#6764).

    Planned by ``plan_tech_lead_decision_actions`` ONLY when
    ``tech_lead.authority.reset_retry`` is ``execute``. Proposals are
    stale-checkable facts, not commands (ADR-0031 §2): the applier's owner
    re-validates the recorded preconditions against current state at
    execution time and downgrades to a surfaced proposal
    (``TECH_LEAD_ACTION_PROPOSED``, ``mode="stale_downgrade"``) when the board
    has moved — no mutations are posted on the downgrade path.

    ``anchor_issue_number`` is the tech_lead session's anchor issue — the event
    surface a downgrade is reported against, mirroring
    :class:`SurfaceTechLeadProposalAction`. For failure investigations and
    health reviews the immutable launch scope forces
    ``issue_number == anchor_issue_number``.
    """

    issue_number: int = 0  # The issue to scratch-reset (the proposal's target)
    rationale: str = ""  # The agent's recorded rationale (proposal body)
    proposal_id: str = ""  # The decision artifact action id (A<n>)
    finding_ids: tuple[str, ...] = ()
    anchor_issue_number: int = 0
    # Set (>0) when this execution consumes an APPROVED gated proposal's
    # stored op (#6778): the applier then finalizes the proposal issue
    # (outcome comment + close + discard_op). 0 = direct execute-authority.
    proposal_issue_number: int = 0
    action_type: ActionType = field(default=ActionType.RESET_RETRY_ISSUE, init=False)

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("ResetRetryIssueAction requires a positive issue_number")
        if not self.proposal_id:
            raise ValueError("ResetRetryIssueAction requires the proposal id")

    def reconciliation_subject(self) -> int:
        """The issue this reset mutates."""
        return self.issue_number


@dataclass(frozen=True)
class KillHungSessionAction(Action):
    """Execute an APPROVED ``kill_hung_session`` proposal op (#6778).

    Planned ONLY from an approved gated proposal's :class:`StoredTechLeadOp`
    (there is no direct execute-authority tier yet — startup rejects
    ``tech_lead.authority.kill_hung_session: execute``). The applier's owner
    (``tech_lead_kill_session``) re-validates that the target issue still has an
    active session and applies the issue-runtime termination boundary — the
    same ``terminate_issue_runtime`` the reset owner uses, WITHOUT the reset.
    Stale proposals downgrade with no mutations, mirroring ``reset_retry``.
    """

    issue_number: int = 0  # The issue whose runtime is terminated (op target)
    rationale: str = ""  # The agent's recorded rationale (stored op)
    proposal_id: str = ""  # The decision artifact action id (A<n>)
    finding_ids: tuple[str, ...] = ()
    anchor_issue_number: int = 0  # Event surface: the proposal issue
    proposal_issue_number: int = 0  # The gated proposal issue to finalize
    # The active session run id the proposal bound its consent to (#6779 R1).
    # The applier's kill owner refuses to terminate unless the target issue's
    # LIVE session still matches this id, so a replacement session started
    # before approval is never killed.
    target_session_id: str = ""
    action_type: ActionType = field(default=ActionType.KILL_HUNG_SESSION, init=False)

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError("KillHungSessionAction requires a positive issue_number")
        if not self.proposal_id:
            raise ValueError("KillHungSessionAction requires the proposal id")
        if self.proposal_issue_number <= 0:
            raise ValueError(
                "KillHungSessionAction requires the gated proposal issue number"
                " (there is no direct execute tier for kill_hung_session)"
            )

    def reconciliation_subject(self) -> int:
        """The issue whose runtime this termination mutates."""
        return self.issue_number


@dataclass(frozen=True)
class DiscardTerminalTechLeadProposalOpsAction(Action):
    """Confirm-and-discard terminal gated-proposal ledger rows (#6779 R7/R10).

    Emitted by the planner from a read-only fact (``candidate_issue_numbers``):
    ledger op rows whose proposal issue was ABSENT from the exhaustive open
    scan. Absence alone is not proof of terminality — an exhaustive-scan
    truncation (a later-page API failure, or a >2000-issue repo) can drop a
    still-open proposal from the scan. So the applier's owner CONFIRMS each
    candidate with a fresh targeted issue read before discarding: a deleted or
    closed issue is terminal and its op is discarded; a still-open issue was a
    pagination gap and its live op is preserved. This keeps fact gathering
    read-only while routing the (formerly scattered) discard mutation through
    one invariant-enforcing boundary.
    """

    candidate_issue_numbers: tuple[int, ...] = ()
    action_type: ActionType = field(
        default=ActionType.DISCARD_TERMINAL_TECH_LEAD_PROPOSAL_OPS, init=False
    )

    def reconciliation_subject(self) -> int:
        """None: this writes only orchestrator-owned ledger rows.

        Its candidates are issues that are already gone or closed, so there is
        no live managed-repo issue whose labels could gate it.
        """
        return NO_RECONCILIATION_SUBJECT


@dataclass(frozen=True)
class AppendPatternObservationAction(Action):
    """Append a REPEAT observation to an existing pattern case file (#6781/#6957).

    Replaces the bare comment the repeat-observation path used to plan. The
    comment alone left the observation count derivable only from GitHub comment
    cadence — which humans also write to — so promotion eligibility would have
    been forgeable by commenting on a case file. This action makes the applier
    do both halves under one owner: post the evidence comment AND increment the
    orchestrator-owned durable observation count, which is the only thing
    ``min_evidence`` ever reads.

    ``fix_class``/``area`` upgrade a row the first observation left
    unclassified; empty values preserve what is recorded, and a CONFLICTING
    non-empty value is rejected by the store rather than silently reclassifying
    the signature (#6957 review F3).

    ``observation`` carries the identity that makes the increment create-once:
    replaying a completed action after a crash re-posts at most a duplicate
    comment and can never advance the count twice (review F1).
    """

    issue_number: int = 0  # The case-file issue
    pattern_signature: str = ""
    observation: "PatternObservation | None" = None
    fix_class: str = ""
    area: str = ""
    action_type: ActionType = field(
        default=ActionType.APPEND_PATTERN_OBSERVATION, init=False
    )

    def __post_init__(self) -> None:
        if self.issue_number <= 0:
            raise ValueError(
                "AppendPatternObservationAction requires the case-file issue number"
            )
        if not self.pattern_signature.strip():
            raise ValueError(
                "AppendPatternObservationAction requires the pattern signature"
                " (the ledger key whose observation count it increments)"
            )
        if self.observation is None:
            raise ValueError(
                "AppendPatternObservationAction requires the identified"
                " observation it appends (identity + evidence comment)"
            )

    def reconciliation_subject(self) -> int:
        """The case file this comments on and counts against."""
        return self.issue_number

    @property
    def comment(self) -> str:
        """The evidence comment posted onto the case file."""
        assert self.observation is not None  # enforced by __post_init__
        return self.observation.comment


@dataclass(frozen=True)
class PromoteTechLeadFindingAction(Action):
    """File a promotable pattern finding as a gated issue in its routed repo.

    The finding-promotion lane's ONE filing action (#6957). The target repo is
    frequently NOT the managed repo, so the applier routes it through the
    ``PromotionTargetHost`` port rather than ``RepositoryHost``. The applier
    files first and records the (signature -> promotion) ledger row second. A
    deterministic ``idempotency_marker`` lets the target recover the remote
    issue if the process dies between those writes.

    ``gated`` is the approval mode this filing was PLANNED under, and the
    self-validation below makes it agree with ``labels``. Carrying the mode was
    the missing half: the command's claim to make ungated filing "an explicit
    typed decision" was empty while the only evidence of the decision was a
    label's presence, so the applier could not tell a deliberate
    ``promote: auto`` filing from a gated one whose gate label a composition bug
    dropped — and a dropped gate is an immediately schedulable issue nobody
    approved (#6957 round-6 review A2).
    """

    signature: str = ""
    case_file_issue_number: int = 0
    target_repo: str = ""
    title: str = ""
    body: str = ""
    labels: tuple[str, ...] = ()
    area: str = ""
    observation_count: int = 0
    idempotency_marker: str = ""
    # tech_lead.findings.promote == "gated" at planning time. Deliberately NOT
    # defaulted to the safe-looking True: the default must be the one that fails
    # closed, and a `gated` command is invalid unless the gate label is really
    # present, so a caller that forgets the field gets a loud auto/gate mismatch
    # rather than a silently gated-looking filing.
    gated: bool = False
    action_type: ActionType = field(
        default=ActionType.PROMOTE_TECH_LEAD_FINDING, init=False
    )

    def reconciliation_subject(self) -> int:
        """The SOURCE case file this promotion is filed on behalf of.

        The promoted issue lives in another repository, which this
        reconciliation model does not span.
        """
        return self.case_file_issue_number

    def __post_init__(self) -> None:
        from ..domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL

        if not self.signature.strip():
            raise ValueError(
                "PromoteTechLeadFindingAction requires the pattern signature"
                " (the promotion ledger key)"
            )
        if self.case_file_issue_number <= 0:
            raise ValueError(
                "PromoteTechLeadFindingAction requires the source case-file issue"
                " number (the evidence ledger it promotes)"
            )
        if "/" not in self.target_repo:
            raise ValueError(
                "PromoteTechLeadFindingAction requires a concrete owner/repo"
                f" target, got {self.target_repo!r}"
            )
        if not self.title.strip() or not self.body.strip():
            raise ValueError("PromoteTechLeadFindingAction requires a title and body")
        if self.observation_count <= 0:
            raise ValueError(
                "PromoteTechLeadFindingAction requires the observation count"
                " already represented in the promoted issue body"
            )
        if not self.idempotency_marker.strip() or (
            self.idempotency_marker not in self.body
        ):
            raise ValueError(
                "PromoteTechLeadFindingAction requires its idempotency marker"
                " in the promoted issue body"
            )
        # Self-validating type, both directions. A gated command missing the
        # gate would file schedulable work nobody approved; an auto command
        # CARRYING the gate would file work nobody can start without noticing
        # a label the operator was never told about.
        has_gate = PROPOSED_TECH_LEAD_LABEL in self.labels
        if self.gated and not has_gate:
            raise ValueError(
                "PromoteTechLeadFindingAction planned as gated must carry the"
                f" {PROPOSED_TECH_LEAD_LABEL!r} label; filing it without the gate"
                " creates immediately schedulable work nobody approved"
            )
        if not self.gated and has_gate:
            raise ValueError(
                "PromoteTechLeadFindingAction planned as ungated"
                " (tech_lead.findings.promote: auto) must NOT carry the"
                f" {PROPOSED_TECH_LEAD_LABEL!r} label"
            )


@dataclass(frozen=True)
class ReportPromotedFindingEvidenceAction(Action):
    """Report NEW evidence onto an already-promoted finding issue (#6957).

    The dedup mirror of the promotion filing: one promoted issue per signature,
    ever, so later observations are reported onto it instead of filing a second
    one. The applier comments through the ``PromotionTargetHost`` (the target
    repo is frequently not the managed one) and then advances the promotion's
    reported-observation high-water mark, so the same evidence is never
    reported twice.
    """

    signature: str = ""
    case_file_issue_number: int = 0
    target_repo: str = ""
    target_issue_number: int = 0
    comment: str = ""
    observation_count: int = 0
    action_type: ActionType = field(
        default=ActionType.REPORT_PROMOTED_FINDING_EVIDENCE, init=False
    )

    def __post_init__(self) -> None:
        if not self.signature.strip():
            raise ValueError(
                "ReportPromotedFindingEvidenceAction requires the pattern signature"
            )
        if self.case_file_issue_number <= 0:
            raise ValueError(
                "ReportPromotedFindingEvidenceAction requires the source case-file"
                " issue number"
            )
        if "/" not in self.target_repo:
            raise ValueError(
                "ReportPromotedFindingEvidenceAction requires a concrete owner/repo"
                f" target, got {self.target_repo!r}"
            )
        if self.target_issue_number <= 0:
            raise ValueError(
                "ReportPromotedFindingEvidenceAction requires the promoted issue number"
            )
        if not self.comment.strip():
            raise ValueError(
                "ReportPromotedFindingEvidenceAction requires the evidence comment"
            )
        if self.observation_count <= 0:
            raise ValueError(
                "ReportPromotedFindingEvidenceAction requires the observation count"
                " it advances the promotion's high-water mark to"
            )

    def reconciliation_subject(self) -> int:
        """The SOURCE case file whose evidence this reports."""
        return self.case_file_issue_number


@dataclass(frozen=True)
class SettleTechLeadPromotionAction(Action):
    """Close the loop on a promoted finding that went terminal (#6957).

    ``shipped`` distinguishes the two terminal outcomes the applier must handle
    differently: closed by a MERGED PR (record the shipped fix, comment and
    close the case file) versus closed without one, which is the operator
    declining (mark the signature declined forever, leave the case file open to
    keep accruing evidence). Every write this action performs lands in the
    SOURCE repo — only the read that produced the fact crossed repos.
    """

    signature: str = ""
    case_file_issue_number: int = 0
    target_repo: str = ""
    target_issue_number: int = 0
    shipped: bool = False
    merged_pr_url: str = ""
    area: str = ""
    title: str = ""
    action_type: ActionType = field(
        default=ActionType.SETTLE_TECH_LEAD_PROMOTION, init=False
    )

    def __post_init__(self) -> None:
        if not self.signature.strip():
            raise ValueError(
                "SettleTechLeadPromotionAction requires the pattern signature"
            )
        if self.case_file_issue_number <= 0:
            raise ValueError(
                "SettleTechLeadPromotionAction requires the case-file issue number"
            )
        if self.shipped and not self.merged_pr_url.strip():
            raise ValueError(
                "SettleTechLeadPromotionAction marked shipped requires the merged"
                " PR url — that url IS the shipped-fix evidence"
            )

    def reconciliation_subject(self) -> int:
        """The SOURCE case file every write of this settlement lands on."""
        return self.case_file_issue_number
