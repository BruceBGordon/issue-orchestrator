"""Tech Lead session completion planning (ADR-0031).

Single home for what happens when a tech_lead session ends: launch-authority
verification, assignment-driven label policy, and decision-artifact
processing. Extracted from ``completion_action_planner`` so the tech_lead owner
boundary (``tech_lead_session_policy`` / ``TechLeadLaunchAuthority`` on the
launch side, this module on the completion side) lives in one cohesive seam.

Policy summary:

* The ONLY trusted launch scope is the orchestrator-owned
  :class:`TechLeadLaunchAuthority` recorded at launch (outside the
  agent-writable worktree). The worktree copies (tech-lead-assignment.json,
  manifest.json) are the agent's reading material; a missing authority
  record, or worktree copies that no longer match it, is a critical failure
  (#6761 re-review finding 1) — never a fail-safe success.
* Only batch-review sessions label PRs (the authority manifest set they were
  launched to audit); failure investigations and health reviews never touch
  manifest labels (#6768 B4 / ADR-0031 §4).
* Every COMPLETED tech_lead session (any flavor) must produce a valid
  decision artifact pair — a missing/invalid pair is a contract violation.
  The authoritative classification runs in the completion processing path's
  PRE-ACTION policy phase (``tech_lead_decision_processing_error``, called by
  ``completion_processor`` before any requested push/PR/comment executes —
  #6769 finding 1) so a rejected completion produces zero GitHub effects and
  the session's history outcome is FAILED, not a quiet success; the action
  planner re-reads the same validation for its planning effects (#6761
  finding 3).
* Decision proposals may only target the session's immutable launch scope:
  a failure investigation targets its focus issue only; a batch review
  targets manifest PRs plus the anchor tracking issue
  (``create_issue``/``flag_pattern`` are scope-free — that is where a health
  review's board-wide findings land) — out-of-scope targets are contract
  violations (#6761 re-review finding 2). A failure investigation must
  additionally publish its diagnosis: >=1 ``post_comment`` targeting the
  focus issue (#6761 finding 2).
* A health review's scope SPLITS by tier (#6780): ``post_comment`` /
  ``escalate_to_human`` stay anchor-scoped (the report's home, ADR-0031 §4),
  while act-level ``reset_retry`` / ``kill_hung_session`` target ONLY the
  immutable ``problem_cohort`` the review was launched owning
  (``allowed_act_level_targets``). A periodic review owns no cohort and so
  owns no act-level targets at all. The cohort comes from the launch grant
  recorded in ``TechLeadLaunchAuthority``, never from the board snapshot's
  ``recent_failures`` — that list is deliberately broader context, and
  reading authority out of it let a review act on issues it did not own.
* Health reviews close their anchor issue on success: the anchor is a
  walk-the-floor log entry, closed when the review lands. A rejected or
  missing pair leaves the anchor open for operator visibility; a
  FAILED/TIMED_OUT health session closes it through the terminal-failure
  path (like batch, no manifest labels) so the next interval re-fires
  instead of deduping against a dead anchor.
* ``create_issue`` proposals may not carry protected workflow labels
  (``tech_lead_issue_policy`` owns the protected set, case-insensitively) —
  contract violation (#6761 finding 4).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..domain.models import Session
from ..domain.board_snapshot import BOARD_SNAPSHOT_FILENAME, BoardSnapshot
from ..domain.tech_lead_artifacts import ACT_LEVEL_TECH_LEAD_ACTIONS
from ..domain.tech_lead_manifest import TechLeadManifest
from ..domain.tech_lead_session import TechLeadLaunchAuthority, TechLeadSessionFlavor
from .actions import (
    Action,
    AddCommentAction,
    AddLabelAction,
    CloseIssueAction,
    RemoveLabelAction,
)
from .completion_types import (
    ERROR_PREFIX_TECH_LEAD_AUTHORITY,
    ERROR_PREFIX_TECH_LEAD_DECISION,
)
from .label_manager import LabelManager
from .publish_recovery import is_publish_failure
from .proposal_dedup_gate import DuplicateTargetGrant
from .tech_lead_decision_actions import (
    plan_tech_lead_decision_actions,
    plan_tech_lead_rejection_action,
)
from .tech_lead_decision_loader import (
    TechLeadArtifactLoadResult,
    TechLeadDecisionLoadFailure,
    load_tech_lead_artifact_pair_for_run,
)
from .tech_lead_case_files import build_pattern_ledger
from .tech_lead_issue_policy import protected_tech_lead_label_violations
from .tech_lead_proposals import build_op_ledger
from .tech_lead_session_policy import is_tech_lead_session, read_tech_lead_assignment

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import TechLeadDecision
    from ..infra.config import Config
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .open_issue_corpus import OpenIssueCorpusManager
    from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)

# Comment/routing proposals whose target_number must fall inside the general
# launch scope (which, for a batch review, includes the audited manifest PRs).
# create_issue / flag_pattern carry no target and are scope-free. Act-level
# proposals (reset_retry / kill_hung_session) are validated separately against
# the STRICTER issue-only scope — see ``allowed_act_level_targets`` (#6764 rr F1).
_TARGET_SCOPED_ACTION_TYPES = frozenset(("post_comment", "escalate_to_human"))


def read_tech_lead_manifest(run_dir: Path) -> TechLeadManifest | None:
    """Read the agent-visible batch PR manifest copy for a session run.

    UNTRUSTED: this is the worktree copy, used only to detect divergence
    from the launch authority (tamper evidence). Completion effects never
    key off it. Fail-safe: a missing run manifest, key, or manifest file
    yields None (with a warning where content is present but unreadable).
    """
    run_manifest_path = run_dir / "manifest.json"
    if not run_manifest_path.exists():
        return None
    try:
        run_manifest = json.loads(run_manifest_path.read_text())
    except Exception as exc:
        logger.warning(
            "[tech_lead] Failed to read run manifest %s: %s",
            run_manifest_path,
            exc,
            exc_info=True,
        )
        return None
    tech_lead_manifest_path = run_manifest.get("tech_lead_manifest")
    if not tech_lead_manifest_path:
        return None
    manifest_path = Path(tech_lead_manifest_path)
    if not manifest_path.exists():
        logger.warning(
            "[tech_lead] Manifest path in run manifest doesn't exist: %s",
            manifest_path,
        )
        return None
    try:
        return TechLeadManifest.read(manifest_path)
    except Exception as exc:
        logger.warning(
            "[tech_lead] Failed to read manifest from %s: %s",
            manifest_path,
            exc,
            exc_info=True,
        )
        return None


def _health_snapshot_scope_error(
    run_dir: Path, authority: TechLeadLaunchAuthority
) -> str | None:
    """Return snapshot/cohort tamper detail for a health-review authority."""
    snapshot_path = run_dir / "tech-lead-data" / BOARD_SNAPSHOT_FILENAME
    if not snapshot_path.exists():
        return "worktree board-snapshot.json is missing (deleted after launch)"
    try:
        worktree_snapshot = BoardSnapshot.read(snapshot_path)
    except Exception as exc:
        return f"worktree board-snapshot.json is malformed: {exc}"
    worktree_problems = worktree_snapshot.problem_issue_numbers()
    authority_problems = frozenset(authority.problem_issue_numbers)
    if worktree_problems != authority_problems:
        return (
            f"worktree board-snapshot problem set {sorted(worktree_problems)}"
            " does not match the launch authority cohort "
            f"{sorted(authority_problems)}"
        )
    return None


def resolve_tech_lead_launch_authority(
    tech_lead_authority: "TechLeadAuthorityStore",
    *,
    run_dir: Path,
    run_id: str,
    session_name: str,
) -> tuple[TechLeadLaunchAuthority | None, str | None]:
    """Load the orchestrator-owned launch authority and verify worktree copies.

    Returns ``(authority, error_detail)``. ``error_detail`` is None only when
    the authority record exists AND the agent-visible worktree copies still
    mirror it. A missing record, a deleted/malformed/flipped assignment copy,
    or a manifest whose PR set diverges from the recorded set is tamper
    evidence (#6761 re-review finding 1) — callers must fail the session.
    """
    authority = tech_lead_authority.load(run_id=run_id, session_name=session_name)
    if authority is None:
        return None, (
            "no orchestrator launch-authority record for run"
            f" {run_id}/{session_name}; the worktree tech_lead inputs cannot"
            " be trusted"
        )
    try:
        assignment = read_tech_lead_assignment(run_dir)
    except ValueError as exc:
        return authority, f"worktree tech-lead-assignment.json is malformed: {exc}"
    if assignment is None:
        return authority, (
            "worktree tech-lead-assignment.json is missing (deleted after launch)"
        )
    if not authority.matches_assignment(assignment):
        return authority, (
            "worktree tech-lead-assignment.json"
            f" (flavor={assignment.flavor.value},"
            f" focus={assignment.focus_issue_number}) does not match the"
            f" launch authority (flavor={authority.flavor.value},"
            f" focus={authority.focus_issue_number})"
        )
    if authority.flavor is TechLeadSessionFlavor.BATCH_REVIEW:
        manifest = read_tech_lead_manifest(run_dir)
        worktree_prs = frozenset(pr.number for pr in manifest.prs) if manifest else frozenset()
        if worktree_prs != frozenset(authority.manifest_pr_numbers):
            return authority, (
                f"worktree manifest PR set {sorted(worktree_prs)} does not"
                " match the launch authority set"
                f" {sorted(authority.manifest_pr_numbers)}"
            )
    if authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        if error := _health_snapshot_scope_error(run_dir, authority):
            return authority, error
    return authority, None


def _launch_scope_description(
    authority: TechLeadLaunchAuthority, allowed: frozenset[int]
) -> str:
    """Human-readable launch scope for out-of-scope violation messages."""
    if authority.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        return f"the originating issue #{authority.focus_issue_number}"
    if authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        return (
            f"the health-review anchor issue #{authority.anchor_issue_number}"
            " (board-wide comments/escalations belong on the anchor; act-level"
            " proposals instead use the cohort this review owns, published as"
            " problem_cohort in board-snapshot.json)"
        )
    return (
        "the audited manifest PRs and the tracking issue"
        f" ({', '.join(f'#{n}' for n in sorted(allowed))})"
    )


def _act_level_scope_description(authority: TechLeadLaunchAuthority) -> str:
    """Human-readable ISSUE-only scope for an out-of-scope act-level violation."""
    if authority.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        return f"the originating work issue #{authority.focus_issue_number}"
    if authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        cohort = ", ".join(f"#{n}" for n in authority.problem_issue_numbers)
        return (
            "the health review's immutable problem cohort, published as"
            " problem_cohort in board-snapshot.json"
            f" ({cohort or 'empty — a periodic review owns no act-level target'})"
        )
    return (
        "no work issue is in scope for an act-level reset/kill from this"
        " session — that intent applies only to a failure investigation's"
        " focus issue; batch manifest entries are PRs and tech_lead anchors are"
        " bookkeeping issues, so route board findings through the scope-free"
        " create_issue/flag_pattern proposals instead"
    )


def _target_scope_violation(
    decision: "TechLeadDecision", authority: TechLeadLaunchAuthority
) -> str | None:
    """Out-of-scope target detail for any targeted proposal, or None.

    Two scopes (#6764 re-review F1): comment/routing proposals may target the
    general launch scope (manifest PRs included for a batch), while act-level
    reset/kill proposals are held to the STRICTER issue-only scope so a
    manifest PR number never reaches the issue reset owner as an ``issue_number``.
    """
    allowed = authority.allowed_targets()
    act_allowed = authority.allowed_act_level_targets()
    for action in decision.proposed_actions:
        if action.action_type in ACT_LEVEL_TECH_LEAD_ACTIONS:
            if action.target_number not in act_allowed:
                return (
                    f"proposed action {action.id} ({action.action_type}) targets"
                    f" #{action.target_number}, outside this session's launch"
                    f" scope for an act-level reset/kill:"
                    f" {_act_level_scope_description(authority)}"
                )
            continue
        if action.action_type not in _TARGET_SCOPED_ACTION_TYPES:
            continue
        if action.target_number not in allowed:
            return (
                f"proposed action {action.id} ({action.action_type}) targets"
                f" #{action.target_number}, outside this session's launch"
                f" scope: {_launch_scope_description(authority, allowed)}"
            )
    return None


def validate_decision_for_authority(
    decision: "TechLeadDecision",
    authority: TechLeadLaunchAuthority,
    *,
    config: "Config",
    labels: LabelManager,
) -> str | None:
    """Authority/policy validation beyond the structural artifact contract.

    Returns a human-readable contract-violation detail, or None when valid.
    Enforced here (the completion owner) against the immutable launch
    authority, never against the agent-writable worktree copies:

    * Every targeted comment/routing proposal must stay inside the launch
      scope — a failure investigation may only address its focus issue; a
      batch review may only address manifest PRs and the anchor tracking
      issue (#6761 re-review finding 2).
    * An ACT-LEVEL proposal (reset_retry/kill_hung_session) is held to the
      STRICTER issue-only scope (``allowed_act_level_targets``): its target is
      handed to the issue reset owner as an ``issue_number``, so a batch
      manifest PR number — or a tech_lead bookkeeping anchor — is a confused
      deputy that would reset the wrong entity (#6764 re-review F1).
    * Failure investigations must publish their diagnosis to the originating
      issue — >=1 ``post_comment`` targeting the focus issue (#6761 F2).
    * ``create_issue`` proposals may not carry protected workflow labels
      (#6761 F4). Checked at mapping time so the domain contract stays
      config-free.
    """
    target_violation = _target_scope_violation(decision, authority)
    if target_violation is not None:
        return target_violation
    if authority.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        focus = authority.focus_issue_number
        has_focus_comment = any(
            action.action_type == "post_comment" and action.target_number == focus
            for action in decision.proposed_actions
        )
        if not has_focus_comment:
            return (
                "failure investigation decision must propose at least one"
                f" post_comment targeting the originating issue #{focus}"
                " (the diagnosis has no channel otherwise)"
            )
    for action in decision.proposed_actions:
        if action.action_type != "create_issue":
            continue
        violations = protected_tech_lead_label_violations(
            action.labels, config=config, labels=labels
        )
        if violations:
            return (
                f"proposed action {action.id} (create_issue) carries protected"
                f" workflow labels: {', '.join(violations)}; agent-proposed"
                " labels may not touch orchestrator label truth"
            )
    return None


def load_validated_tech_lead_pair(
    run_dir: Path,
    authority: TechLeadLaunchAuthority,
    *,
    config: "Config",
    labels: LabelManager,
) -> TechLeadArtifactLoadResult:
    """Load the artifact pair and apply authority/policy validation.

    The ONE read both completion seams use: the processing path (authoritative
    outcome, finding 3) and the action planner (planning effects). Never raises.
    """
    result = load_tech_lead_artifact_pair_for_run(run_dir)
    if not result.ok or result.decision is None:
        return result
    detail = validate_decision_for_authority(
        result.decision, authority, config=config, labels=labels
    )
    if detail is not None:
        logger.error("Tech Lead decision contract violation in %s: %s", run_dir, detail)
        return TechLeadArtifactLoadResult(
            failure=TechLeadDecisionLoadFailure.CONTRACT_VIOLATION,
            detail=detail,
        )
    return result


def tech_lead_decision_processing_error(
    config: "Config",
    *,
    tech_lead_authority: "TechLeadAuthorityStore",
    run_dir: Path,
    run_id: str,
    session_name: str,
) -> str | None:
    """Authoritative scope + pair validation for a COMPLETED tech_lead session.

    Called from the completion processing path's PRE-ACTION policy phase —
    before the completion record is preserved and before ANY requested action
    executes (#6769 finding 1). A missing/tampered launch authority (#6761
    re-review F1) or a missing/rejected artifact pair (#6761 F3) returns a
    tagged processing error; the processor rejects the completion outright
    (zero push/PR/comment calls) and ``critical_processing_errors``
    classifies the error critical so history records FAILED and the failure
    labeling path fires.
    """
    authority, tamper = resolve_tech_lead_launch_authority(
        tech_lead_authority, run_dir=run_dir, run_id=run_id, session_name=session_name
    )
    if authority is None:
        return f"{ERROR_PREFIX_TECH_LEAD_AUTHORITY}: missing_authority: {tamper}"
    if tamper is not None:
        return f"{ERROR_PREFIX_TECH_LEAD_AUTHORITY}: scope_tampered: {tamper}"
    result = load_validated_tech_lead_pair(
        run_dir, authority, config=config, labels=LabelManager(config)
    )
    if result.ok:
        return None
    failure = result.failure.value if result.failure else "unknown"
    return f"{ERROR_PREFIX_TECH_LEAD_DECISION}: {failure}: {result.detail}"


_TECH_LEAD_ERROR_PREFIXES = (ERROR_PREFIX_TECH_LEAD_DECISION, ERROR_PREFIX_TECH_LEAD_AUTHORITY)


def has_tech_lead_decision_errors(processing_errors: list[str] | None) -> bool:
    """True when processing errors include a rejected pair or tampered scope."""
    return any(
        error.startswith(_TECH_LEAD_ERROR_PREFIXES)
        for error in processing_errors or ()
    )


def _split_tech_lead_decision_error(processing_errors: list[str]) -> tuple[str, str]:
    """Parse (failure, detail) back out of the recorded processing error."""
    for error in processing_errors:
        for prefix in _TECH_LEAD_ERROR_PREFIXES:
            if not error.startswith(prefix):
                continue
            remainder = error[len(prefix):].lstrip(": ")
            failure, sep, detail = remainder.partition(": ")
            return (failure or "unknown", detail if sep else "")
    return ("unknown", "")


def _resolve_launch_authority_for_session(
    tech_lead_authority: "TechLeadAuthorityStore", session: Session
) -> tuple[TechLeadLaunchAuthority | None, str | None]:
    return resolve_tech_lead_launch_authority(
        tech_lead_authority,
        run_dir=session.run_dir,
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    )


def discard_tech_lead_authority_after_completion(
    config: "Config",
    tech_lead_authority: "TechLeadAuthorityStore",
    session: Session,
    *,
    processing_errors: list[str] | None,
) -> None:
    """Retention owner (#6769 F3): drop the run's authority row at the end.

    Called from completion finalization for every terminal status. The row
    is keyed by run identity, so a relaunch (new run) records a fresh row at
    launch, and a completed/failed/rejected run leaves nothing behind. Runs
    AFTER completion actions are planned — every authority read happens
    during planning.

    Exception: a publish-stage failure (push/create_pr/publish_blocked)
    records Retry-Publish locators, and the retry re-enters
    ``CompletionProcessor.process`` for this same run — which re-validates
    the launch authority. The row is retained then;
    ``PublishRecoveryService`` discards it at the retry's own terminal
    (success finalization or issue abandonment).

    A storm anchor's cohort row (#6780) is discarded on the same terminal.
    It is keyed by the ANCHOR issue rather than run identity, because it
    outlives any single run: it is recorded at anchor creation, before a run
    exists, and rehydrates the pending review after a restart. Its readers
    intersect it with live pending/active tech_lead work, so dropping it here is
    what releases the cohort's held run artifacts for cleanup.
    """
    if not is_tech_lead_session(config.tech_lead_review_agent, session.issue.agent_type):
        return
    if is_publish_failure(processing_errors):
        return
    tech_lead_authority.discard(
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    )
    tech_lead_authority.discard_storm_cohort(anchor_issue_number=session.issue.number)


def _manifest_label_actions(
    config: "Config",
    authority: TechLeadLaunchAuthority,
    expected: "ExpectedState",
    *,
    success: bool,
) -> list[Action]:
    """Label the AUTHORITY manifest PRs tech-lead-reviewed/-failed.

    The PR set comes exclusively from the launch authority record — a
    tampered worktree manifest with substituted PR numbers never receives
    labels (#6761 re-review finding 1).
    """
    if not authority.manifest_pr_numbers:
        return []
    if success:
        tech_lead_label = config.tech_lead_reviewed_label or "tech-lead-reviewed"
        reason = "Tech Lead completed successfully"
    else:
        tech_lead_label = config.tech_lead_failed_label or "tech-lead-failed"
        reason = "Tech Lead session failed"
    logger.info(
        "[tech_lead] Adding '%s' label to %d PRs",
        tech_lead_label,
        len(authority.manifest_pr_numbers),
    )
    return [
        AddLabelAction(
            issue_number=pr_number,
            label=tech_lead_label,
            reason=reason,
            expected=expected,
        )
        for pr_number in authority.manifest_pr_numbers
    ]


def generate_tech_lead_completion_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    completed_ok: bool,
    labels: LabelManager,
    tech_lead_authority: "TechLeadAuthorityStore",
    open_issue_corpus: "OpenIssueCorpusManager",
    active_session_run_id: "Callable[[int], str | None]",
) -> list[Action]:
    """Plan all completion effects for a tech_lead session (see module docstring).

    Pure planning — no GitHub reads. ``tech_lead.milestone_strategy.explicit``
    travels as intent on :class:`CreateTechLeadIssueAction` and is resolved at
    the create-issue execution boundary (#6769 finding 4), so a shadow-mode
    ``create_issue`` proposal plans zero API calls.
    """
    actions: list[Action] = []

    if not is_tech_lead_session(config.tech_lead_review_agent, session.issue.agent_type):
        return actions

    authority, tamper = _resolve_launch_authority_for_session(
        tech_lead_authority, session
    )
    if authority is None or tamper is not None:
        # Belt-and-braces: the processing path classifies this critical
        # BEFORE status recording, so completions normally take the failure
        # routing instead. Never plan success effects from untrusted scope.
        failure = "missing_authority" if authority is None else "scope_tampered"
        detail = tamper or "no launch authority recorded"
        logger.error(
            "[tech_lead] Launch authority rejected for issue #%d (%s): %s",
            session.issue.number,
            failure,
            detail,
        )
        actions.append(
            plan_tech_lead_rejection_action(
                anchor_issue_number=session.issue.number,
                failure=failure,
                detail=detail,
            )
        )
        return actions

    load_result = (
        load_validated_tech_lead_pair(
            session.run_dir, authority, config=config, labels=labels
        )
        if completed_ok
        else None
    )
    succeeded = load_result is not None and load_result.ok

    if authority.flavor is TechLeadSessionFlavor.BATCH_REVIEW:
        actions.extend(
            _manifest_label_actions(config, authority, expected, success=succeeded)
        )

    if load_result is None:
        return actions
    if load_result.decision is not None:
        # The op ledger (one open gated proposal per (op, target), #6778)
        # and the pattern ledger (one case file per signature, #6781) come
        # from the same injected authority store that owns launch scope:
        # both reads are local, so planning needs no GitHub call.
        actions.extend(
            plan_tech_lead_decision_actions(
                load_result.decision,
                config,
                labels,
                anchor_issue=session.issue,
                expected=expected,
                op_ledger=build_op_ledger(tech_lead_authority.list_ops()),
                pattern_ledger=build_pattern_ledger(
                    tech_lead_authority.list_pattern_evidence()
                ),
                source_run_id=session.run_assets.run_id,
                source_session_name=session.run_assets.session_name,
                observed_at=session.run_assets.started_at,
                active_session_run_id=active_session_run_id,
                dedup_corpus=open_issue_corpus.load(),
                dedup_grant=DuplicateTargetGrant.of(authority.allowed_targets()),
            )
        )
    else:
        # Belt-and-braces: the processing path (finding 3) should already have
        # classified this session FAILED before the planner sees it; still
        # surface the rejection when a rejected pair reaches this seam.
        failure = load_result.failure.value if load_result.failure else "unknown"
        logger.warning(
            "[tech_lead] Decision artifact rejected for issue #%d (%s): %s",
            session.issue.number,
            failure,
            load_result.detail,
        )
        actions.append(
            plan_tech_lead_rejection_action(
                anchor_issue_number=session.issue.number,
                failure=failure,
                detail=load_result.detail,
            )
        )

    if succeeded and authority.flavor is TechLeadSessionFlavor.BATCH_REVIEW:
        # Terminal transition (#6768 round 4): the open+agent-labeled tracking
        # issue is what startup recovery requeues and what
        # _find_existing_tech_lead_anchor_issues treats as the active batch.
        # Ordered last so a mid-apply crash leaves the batch open and
        # re-auditable. No comment: tech_lead prompts promise the orchestrator
        # posts none here.
        actions.append(
            CloseIssueAction(
                issue_number=session.issue.number,
                reason="Batch tech_lead review completed - closing tracking issue",
                expected=expected,
            )
        )
    if succeeded and authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        # The anchor issue is a walk-the-floor log entry (ADR-0031 §4): a
        # landed review closes it (same terminal ordering rationale as batch;
        # no manifest labels exist for this flavor). Rejected/missing pairs
        # take the rejection surface instead and leave the anchor open.
        actions.append(
            CloseIssueAction(
                issue_number=session.issue.number,
                reason="Health review completed with a valid decision pair"
                " - closing anchor issue",
                expected=expected,
            )
        )
    return actions

def generate_tech_lead_decision_failure_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    processing_errors: list[str],
    labels: LabelManager,
    tech_lead_authority: "TechLeadAuthorityStore",
) -> list[Action]:
    """Completion effects when a COMPLETED tech_lead session was rejected.

    The completion processing path recorded a tech_lead authority/decision
    error (findings 1/3); history is FAILED via the critical-error seam.
    This plans the label/comment effects for every flavor:

    * batch review — the AUTHORITY manifest PRs get the tech-lead-failed label;
    * both flavors — the rejection is surfaced as an event AND durably on the
      session's own issue (blocked-failed label + explanatory comment, the
      operator-facing escalation surface — #6761 finding 6), and the
      in-progress claim is released. The batch tracking issue stays open for
      re-audit.
    """
    failure, detail = _split_tech_lead_decision_error(processing_errors)
    actions: list[Action] = []
    authority, _tamper = _resolve_launch_authority_for_session(
        tech_lead_authority, session
    )
    if (
        authority is not None
        and authority.flavor is TechLeadSessionFlavor.BATCH_REVIEW
    ):
        actions.extend(
            _manifest_label_actions(config, authority, expected, success=False)
        )
    actions.append(
        plan_tech_lead_rejection_action(
            anchor_issue_number=session.issue.number,
            failure=failure,
            detail=detail,
        )
    )
    detail_text = detail or "no detail recorded"
    actions.extend(
        (
            AddLabelAction(
                issue_number=session.issue.number,
                label=labels.blocked_failed,
                reason=f"Tech Lead completion rejected ({failure})",
                expected=expected,
            ),
            AddCommentAction(
                number=session.issue.number,
                comment=(
                    "## ❌ Tech Lead completion rejected\n\n"
                    "The tech_lead session completed, but its output was"
                    f" rejected (`{failure}`):\n\n"
                    f"> {detail_text}\n\n"
                    f"- Session: `{session.terminal_id}`\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n\n"
                    f"The session is recorded as failed and `{labels.blocked_failed}`"
                    " was added. Remove the label to allow reprocessing."
                ),
                reason="Durable operator record of the rejected tech_lead completion",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=session.issue.number,
                label=labels.in_progress,
                reason="Tech Lead completion rejected - releasing claim",
                expected=expected,
            ),
        )
    )
    return actions


def generate_tech_lead_failure_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    tech_lead_authority: "TechLeadAuthorityStore",
) -> list[Action]:
    """Batch/health FAILED/TIMED_OUT terminal effects (#6768 round 5, ADR-0031 §4).

    Batch: the AUTHORITY manifest PRs get the operator-visible tech-lead-failed
    label and the tracking issue closes after the generic failure diagnosis
    and the PR labels: an open failed tracker would be requeued at restart
    with an empty manifest (its PRs are now candidate-filtered as
    tech-lead-failed), looping forever. Health reviews close their anchor the
    same way — an open dead anchor would both be requeued at restart and
    dedupe the next interval's trigger — but have no manifest to label.
    Failure investigations produce nothing here — their anchor is the
    original failed work issue. A session without a launch authority record
    produces nothing (the session already failed; closing or labeling from
    untrusted worktree copies would hand the agent authority).
    """
    if not is_tech_lead_session(config.tech_lead_review_agent, session.issue.agent_type):
        return []
    authority, _tamper = _resolve_launch_authority_for_session(
        tech_lead_authority, session
    )
    if authority is None:
        logger.warning(
            "[tech_lead] No launch authority for session %s; "
            "skipping terminal tech_lead effects",
            session.terminal_id,
        )
        return []
    if authority.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION:
        return []
    if authority.flavor is TechLeadSessionFlavor.HEALTH_REVIEW:
        return [
            CloseIssueAction(
                issue_number=session.issue.number,
                reason="Health review session failed - closing anchor issue "
                "(the next interval re-fires a fresh review)",
                expected=expected,
            )
        ]
    actions = _manifest_label_actions(config, authority, expected, success=False)
    actions.append(
        CloseIssueAction(
            issue_number=session.issue.number,
            reason="Batch tech_lead review failed - closing tracking issue "
            "(manifest PRs carry tech-lead-failed)",
            expected=expected,
        )
    )
    return actions
