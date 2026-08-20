"""Execution-time owner for tech_lead ``reset_retry`` proposals (ADR-0031 §2, #6764).

Authority made ``reset_retry`` plannable; this module makes executing it
safe. A proposal is a stale-checkable fact recorded against a board
snapshot, not a command: by the time the tech_lead session completes, the
board may have moved. The executor therefore re-validates the proposal's
preconditions against CURRENT state immediately before invoking the reset
owner:

1. the target issue can still be read and is still open;
2. no runtime the reset boundary would terminate is active for the issue
   — a visible issue/rework session, the persistent coder/reviewer pair, a
   supervised review-exchange job, or a pending publish retry. The reset
   force-terminates ALL of these, which an operator clicking "Reset & Retry"
   consents to, but an agent proposal written before that work started must
   never kill unobserved live work (the freshness check reads the same owner
   set the teardown mutates — see ``has_active_reset_retry_runtime``);
3. the issue still carries at least one blocking-class label
   (:meth:`LabelManager.get_blocking` — the same classification the retry
   entry points clear via ``labels_to_remove_for_retry``). If nothing
   blocking remains, the diagnosed failure has already been recovered and
   the proposal is stale.

On a stale precondition the proposal DOWNGRADES (ADR-0031 §2): the
surfaced-proposal event (``TECH_LEAD_ACTION_PROPOSED`` with
``mode="stale_downgrade"``) is emitted and no mutations are posted. On
success a ``TECH_LEAD_ACTION_EXECUTED`` event records the boundary effects.
Reset-owner failures fail the action loudly — never a silent success.

This module is also the single authoritative outcome boundary for
completion terminalization: :class:`RequiredActLevelOutcome` /
:func:`evaluate_required_act_level_outcome` fold the applied results into
one verdict ("did the mandated act-level action commit?"), and
:func:`effective_terminal_status` turns that verdict into the ONE terminal
status the whole post-apply completion phase consumes — so the observer,
failure discovery, retry gating, cleanup reason, operator surface, and
history all agree, never split between the agent's reported status and this
verdict (#6764 re-review F2). :func:`finalize_required_act_level_history`
keeps the persisted history row consistent with that status, and
:func:`build_required_act_level_failure_actions` routes any failed mandated
act-level mutation to a durable needs-human label + comment on its target so
the FAILED terminal is not merely in-memory. A failed mandated action can
therefore never be recorded as a clean success.

The reset itself is NOT reimplemented here: ``run_reset`` is the injected
production boundary — the same ``reset_and_retry_issue`` pipeline the
dashboard's ``/api/reset-retry`` endpoint uses (runtime termination, PR
superseding, branch deletion, label/history/timeline clearing,
pending-label relaunch marking, and queue re-insertion). Production wiring
lives in ``entrypoints/tech_lead_reset_retry_wiring.py``.

This module also owns the surfaced-proposal event payload
(:func:`publish_proposal_surfaced`) so the shadow/pattern/rejected surface
handler and the stale-downgrade path cannot drift apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from ..domain.models import SessionStatus
from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, make_trace_event
from .actions import (
    Action,
    ActionResult,
    ActionResultType,
    AddCommentAction,
    AddLabelAction,
    KillHungSessionAction,
    ResetRetryIssueAction,
    SurfaceTechLeadProposalAction,
)
from .needs_human_block import NeedsHumanCause

if TYPE_CHECKING:
    from ..domain.models import SessionHistoryEntry
    from ..ports.issue import Issue
    from .action_applier import ActionApplier
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

# Surfaced-proposal mode for an execute-authority proposal whose recorded
# preconditions no longer held at execution time (ADR-0031 §2).
STALE_DOWNGRADE_MODE = "stale_downgrade"

# Cap applied to rationale previews in surfaced events, matching
# tech_lead_decision_actions._BODY_PREVIEW_CHARS for planned surfaces.
_RATIONALE_PREVIEW_CHARS = 500


def publish_proposal_surfaced(
    events: EventSink,
    *,
    issue_number: int,
    action_id: str,
    proposal_type: str,
    target_number: int,
    target_is_pr: bool,
    title: str,
    body_preview: str,
    finding_ids: Sequence[str],
    mode: str,
    stale_reason: str | None = None,
) -> None:
    """Publish the surfaced-proposal trace event (single payload owner).

    Shadow/pattern/stale-downgrade surfaces emit ``TECH_LEAD_ACTION_PROPOSED``;
    rejected decision artifacts (``mode == "rejected"``) emit
    ``TECH_LEAD_DECISION_REJECTED``. No GitHub calls — surfacing is the whole
    effect.
    """
    payload: dict[str, Any] = {
        "issue_number": issue_number,
        "action_id": action_id,
        "proposal_type": proposal_type,
        "target_number": target_number,
        "target_is_pr": target_is_pr,
        "title": title,
        "body_preview": body_preview,
        "finding_ids": list(finding_ids),
        "mode": mode,
    }
    candidate = stale_reason
    if _is_present(candidate):
        payload["stale_reason"] = candidate
    event_name = (
        EventName.TECH_LEAD_DECISION_REJECTED
        if mode == "rejected"
        else EventName.TECH_LEAD_ACTION_PROPOSED
    )
    events.publish(make_trace_event(event_name, payload))
    logger.info(
        issue_log(
            issue_number, "Tech Lead proposal surfaced: mode=%s type=%s action_id=%s"
        ),
        mode,
        proposal_type,
        action_id,
    )


def _is_present(value: object | None) -> bool:
    """Return whether an optional payload value is present."""
    return value is not None


def apply_surface_tech_lead_proposal(
    action: SurfaceTechLeadProposalAction, events: EventSink
) -> ActionResult:
    """Apply a :class:`SurfaceTechLeadProposalAction` (event-only, ADR-0031)."""
    publish_proposal_surfaced(
        events,
        issue_number=action.issue_number,
        action_id=action.action_id,
        proposal_type=action.proposal_type,
        target_number=action.target_number,
        target_is_pr=action.target_is_pr,
        title=action.title,
        body_preview=action.body_preview,
        finding_ids=action.finding_ids,
        mode=action.mode,
    )
    return ActionResult.ok(
        action,
        issue_number=action.issue_number,
        action_id=action.action_id,
        proposal_type=action.proposal_type,
        mode=action.mode,
    )


@dataclass(frozen=True)
class ResetRetryRunOutcome:
    """Typed result of one reset-owner invocation (the injected boundary)."""

    success: bool
    error: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


# The production boundary: (issue_number, current_labels) -> outcome. The
# labels are the fresh read the executor already made for re-validation,
# passed through so the reset owner does not re-fetch them (GitHub API
# discipline).
RunResetFn = Callable[[int, Sequence[str]], ResetRetryRunOutcome]


def reset_retry_stale_reason(
    *,
    issue: "Issue | None",
    active_runtime: bool,
    label_manager: "LabelManager",
) -> str | None:
    """Why the proposal's preconditions no longer hold, or None when valid.

    Pure policy — see the module docstring for the precondition rationale.
    ``active_runtime`` is the aggregate signal from every runtime owner the
    reset boundary would terminate (visible session, persistent review-exchange
    pair/job, or pending publish retry), not just a visible session.
    """
    if issue is None:
        return "target issue could not be read from the repository host"
    if issue.state != "open":
        return f"target issue #{issue.number} is {issue.state}, not open"
    if active_runtime:
        return (
            f"issue #{issue.number} has active runtime (session, review-exchange"
            " pair/job, or publish retry); resetting would terminate live work"
            " the proposal did not observe"
        )
    if not label_manager.get_blocking(issue.labels):
        return (
            f"issue #{issue.number} no longer carries a blocking-class"
            " label; the diagnosed failure appears already recovered"
        )
    return None


@dataclass
class TechLeadResetRetryExecutor:
    """Applies :class:`ResetRetryIssueAction` with execution-time re-validation.

    All collaborators are injected: reads come from the composition root's
    closures over live orchestrator state, and ``run_reset`` is the reused
    dashboard reset pipeline. The executor owns only the
    validate/downgrade/execute/surface policy.
    """

    events: EventSink
    label_manager: "LabelManager"
    read_issue: Callable[[int], "Issue | None"]
    has_active_issue_runtime: Callable[[int], bool]
    run_reset: RunResetFn

    def apply(self, action: ResetRetryIssueAction) -> ActionResult:
        issue = self.read_issue(action.issue_number)
        stale = reset_retry_stale_reason(
            issue=issue,
            active_runtime=self.has_active_issue_runtime(action.issue_number),
            label_manager=self.label_manager,
        )
        if stale is not None:
            return self._downgrade(action, stale)
        assert issue is not None  # stale check rejects None
        outcome = self.run_reset(action.issue_number, list(issue.labels))
        if not outcome.success:
            logger.error(
                issue_log(
                    action.issue_number,
                    "Tech Lead reset_retry %s FAILED in the reset owner: %s",
                ),
                action.proposal_id,
                outcome.error,
            )
            return ActionResult.fail(
                action,
                f"reset owner failed for issue #{action.issue_number}"
                f" (proposal {action.proposal_id}): {outcome.error}",
                issue_number=action.issue_number,
                proposal_id=action.proposal_id,
            )
        self.events.publish(
            make_trace_event(
                EventName.TECH_LEAD_ACTION_EXECUTED,
                {
                    "issue_number": action.anchor_issue_number,
                    "action_id": action.proposal_id,
                    "proposal_type": "reset_retry",
                    "target_number": action.issue_number,
                    "finding_ids": list(action.finding_ids),
                    "boundary": dict(outcome.details),
                },
            )
        )
        logger.info(
            issue_log(
                action.issue_number,
                "Tech Lead reset_retry %s executed via the reset owner",
            ),
            action.proposal_id,
        )
        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            proposal_id=action.proposal_id,
        )

    def _downgrade(self, action: ResetRetryIssueAction, stale: str) -> ActionResult:
        """Stale precondition: surface as would-have-done, post no mutations."""
        logger.warning(
            issue_log(
                action.issue_number,
                "Tech Lead reset_retry %s downgraded to surfaced proposal: %s",
            ),
            action.proposal_id,
            stale,
        )
        publish_proposal_surfaced(
            self.events,
            issue_number=action.anchor_issue_number,
            action_id=action.proposal_id,
            proposal_type="reset_retry",
            target_number=action.issue_number,
            target_is_pr=False,
            title="",
            body_preview=action.rationale[:_RATIONALE_PREVIEW_CHARS],
            finding_ids=action.finding_ids,
            mode=STALE_DOWNGRADE_MODE,
            stale_reason=stale,
        )
        return ActionResult.skip(
            action,
            f"stale precondition: {stale}",
            mode=STALE_DOWNGRADE_MODE,
            issue_number=action.issue_number,
            proposal_id=action.proposal_id,
        )


def preserve_reset_retry_eligibility(
    applied: Sequence[ActionResult],
    *,
    make_retryable: Callable[[int], object],
) -> list[int]:
    """Keep reset issues retryable after the completion's own history append.

    The completion pipeline appends the completing session's history entry
    AFTER its actions are applied. For a failure investigation, that entry
    is keyed on the very issue a successful ``reset_retry`` just made
    retryable — and both the planner's eligibility loop and
    ``QueueCache.evaluate_issue`` treat a history entry as "already ran, do
    not relaunch". Without this pass, the reset's relaunch would be
    silently re-blocked by the reset's own tech_lead session. Callers invoke
    this after the append with :meth:`RetryHistoryState.make_retryable` —
    the owner of those gates — and get back the issue numbers re-cleared.
    """
    cleared: list[int] = []
    for result in applied:
        candidate = result.action
        matched = isinstance(candidate, ResetRetryIssueAction) and result.success
        if not matched:
            continue
        make_retryable(candidate.issue_number)
        cleared.append(candidate.issue_number)
    return cleared


@dataclass(frozen=True)
class RequiredActLevelOutcome:
    """Did every decision-mandated act-level tech_lead action commit? (ADR-0031 §2).

    The single authoritative boundary the completion path consumes to decide
    terminalization. A planned :class:`ResetRetryIssueAction` is a
    decision-MANDATED act-level mutation: the tech_lead decision required it, so a
    completion is authoritative-success only if it committed. This owner folds
    the applied results into that one verdict so the executor and the
    completion handler cannot drift on what "committed" means.

    ``committed`` is true when no required act-level action FAILED. A stale
    downgrade (``ActionResultType.SKIPPED``) counts as committed: the board
    moved and the reset owner correctly surfaced instead of mutating — a
    non-failure outcome. Only a hard FAILURE (the reset owner itself failed)
    blocks success terminalization.
    """

    committed: bool
    failures: tuple[str, ...] = ()
    failed_actions: tuple[Action, ...] = ()

    @property
    def failed(self) -> bool:
        return not self.committed

    def failure_summary(self) -> str:
        return "; ".join(self.failures) or "act-level owner did not commit"


def is_required_act_level_action(action: Action) -> bool:
    """True for a decision-MANDATED act-level action (ADR-0031 §2).

    THE single source of "which actions carry mandated authority", shared by the
    apply-time GATE (:func:`partition_required_act_level_actions`, which withholds
    success-only effects) and the terminal VERDICT
    (:func:`evaluate_required_act_level_outcome`), so authority and the effects it
    gates classify the same actions and cannot drift (#6779 R13). A
    Both wired act-level mutations are mandatory completion gates.
    """
    return isinstance(action, (ResetRetryIssueAction, KillHungSessionAction))


def partition_required_act_level_actions(
    actions: Sequence[Action],
) -> tuple[list[Action], list[Action]]:
    """Split completion actions into (mandated act-level, success-only remainder).

    Relative order within each partition is preserved. The mandated partition is
    the authority gate applied first; the remainder holds the success-only effects
    (labels/comments/close) that must NOT commit unless the gate commits (#6779).
    """
    mandated = [action for action in actions if is_required_act_level_action(action)]
    remainder = [
        action for action in actions if not is_required_act_level_action(action)
    ]
    return mandated, remainder


def evaluate_required_act_level_outcome(
    applied: Sequence[ActionResult],
) -> RequiredActLevelOutcome:
    """Fold applied results into the required-act-level commit verdict.

    Pure over the apply results — the single seam that classifies a mandated
    act-level failure, shared by the completion terminalization path so a
    failed reset can never be recorded as a clean success (#6764 re-review F2).
    """
    failed_results = tuple(
        result
        for result in applied
        if is_required_act_level_action(result.action)
        and result.result_type is ActionResultType.FAILURE
    )
    failures = tuple(
        result.error or "act-level owner failed" for result in failed_results
    )
    return RequiredActLevelOutcome(
        committed=not failures,
        failures=failures,
        failed_actions=tuple(result.action for result in failed_results),
    )


def apply_completion_actions_gated(
    action_applier: "ActionApplier",
    actions: Sequence[Action],
    *,
    issue_number: int,
) -> tuple[list[ActionResult], BaseException | None]:
    """Apply completion actions so the mandated act-level outcome GATES the rest.

    THE authority-with-effects owner (ADR-0031 §2, #6779 R13 root cause): the
    decision-mandated ``ResetRetryIssueAction`` is applied FIRST as the gate, and
    its success-only siblings (completion labels/comments/close) apply ONLY when it
    commits — so a failing mandated reset can never leave a success-only effect
    committed, no matter where it sat in the planned list. A raised apply past the
    runtime-kill boundary (Reconciliation/Claim/adapter, #6777) withholds the
    remainder too and is returned so the caller can finalize the ONE terminal
    outcome, then re-raise. With no mandated action the whole list applies in one
    pass — behavior for ordinary completions is unchanged.
    """
    mandated, remainder = partition_required_act_level_actions(actions)
    applied, error = _apply_completion_action_batch(
        action_applier, mandated or list(actions), issue_number
    )
    if (
        not mandated
        or error is not None
        or evaluate_required_act_level_outcome(applied).failed
    ):
        if mandated and remainder:
            logger.warning(
                issue_log(
                    issue_number,
                    "Mandated act-level action did not commit; "
                    "withholding %d success-only completion effect(s)",
                ),
                len(remainder),
            )
        return applied, error
    remainder_applied, error = _apply_completion_action_batch(
        action_applier, remainder, issue_number
    )
    return applied + remainder_applied, error


def _apply_completion_action_batch(
    action_applier: "ActionApplier",
    actions: Sequence[Action],
    issue_number: int,
) -> tuple[list[ActionResult], BaseException | None]:
    """Apply one batch of actions, capturing a raise past the runtime-kill boundary.

    Terminal finalization must run on EVERY apply outcome (#6777): a propagated
    ``ReconciliationRequired`` / ``ClaimLostError`` / adapter fault is CAPTURED and
    returned rather than aborting before finalization.
    """
    if not actions:
        return [], None
    logger.info(
        issue_log(issue_number, "Applying %d completion action(s): %s"),
        len(actions),
        [type(action).__name__ for action in actions],
    )
    try:
        # `or []` tolerates test doubles whose apply_all returns None.
        return list(action_applier.apply_all(list(actions)) or []), None
    except Exception as exc:
        logger.warning(
            issue_log(
                issue_number,
                "Completion-action apply raised; finalizing "
                "terminal FAILED before re-raising: %s",
            ),
            exc,
        )
        return [], exc


def required_act_level_outcome_after_apply(
    applied: Sequence[ActionResult],
    apply_error: BaseException | None,
) -> RequiredActLevelOutcome:
    """The required-act-level verdict once completion actions have been applied.

    On a normal return this folds the real applied results
    (:func:`evaluate_required_act_level_outcome`). When ``apply_all`` RAISED past
    the runtime-kill boundary — ``ReconciliationRequired`` / ``ClaimLostError`` /
    any adapter fault — the mandated act-level action cannot be confirmed
    committed, so the verdict is a hard failure through the SAME machinery
    (#6777). :func:`effective_terminal_status` therefore terminalizes the whole
    completion FAILED (never a false COMPLETED) with no parallel status path, and
    the caller re-raises ``apply_error`` only AFTER finalization has committed.
    """
    if apply_error is not None:
        return RequiredActLevelOutcome(
            committed=False,
            failures=(f"completion action apply raised before commit: {apply_error}",),
        )
    return evaluate_required_act_level_outcome(applied)


def finalize_required_act_level_history(
    history_entry: "SessionHistoryEntry",
    outcome: RequiredActLevelOutcome,
) -> "SessionHistoryEntry":
    """Terminal history status for a completion carrying required act-level work.

    The authoritative outcome boundary (ADR-0031 §2, #6764 re-review F2): a
    decision-mandated act-level action that FAILED at apply time makes the
    WHOLE completion a failure — never a partial success. The reset either
    committed or the session's terminal record is FAILED, so the agent's
    "completed" intent can never mask an un-run reset (orchestrator-authoritative,
    fail-loud). A committed or stale-downgraded outcome returns the caller's
    entry unchanged — success terminalization proceeds as before.
    """
    if outcome.committed:
        return history_entry
    return replace(
        history_entry,
        status="failed",
        status_reason=(
            "required act-level tech_lead action did not commit: "
            + outcome.failure_summary()
        ),
    )


def effective_terminal_status(
    status: SessionStatus, outcome: RequiredActLevelOutcome
) -> SessionStatus:
    """The single terminal status the WHOLE post-apply completion phase consumes.

    Terminal-status policy lives HERE, co-located with the required-act-level
    outcome boundary, so the completion path cannot split it between the agent's
    reported ``status`` and this outcome object (#6764 re-review F2, the final
    abstraction point). A decision-mandated act-level action that FAILED at apply
    time makes the effective terminal status :attr:`SessionStatus.FAILED`
    regardless of the agent's "completed" intent — every downstream consumer
    (observer, failure discovery, retry gating, cleanup reason, operator surface,
    and history) then routes the completion as the failure it is. A committed or
    stale-downgraded outcome preserves the agent-reported status unchanged, so
    ordinary completions and genuine failures behave exactly as before.
    """
    if outcome.failed:
        return SessionStatus.FAILED
    return status


def build_required_act_level_failure_actions(
    *,
    issue_number: int,
    needs_human_label: str,
    outcome: RequiredActLevelOutcome,
    session_id: str,
    runtime_minutes: float,
) -> list[Action]:
    """Durable, crash-safe operator surface for a failed mandated act-level action.

    A failed mandated act-level mutation terminalizes the completion as FAILED
    (:func:`effective_terminal_status`), but that terminal record is in-memory
    only — a crash between it and the next tick would lose the signal. This
    routes the failure to GitHub through the SAME label/comment action owners the
    rest of completion uses (no parallel mechanism): the needs-human blocking
    label plus an explanatory comment, mirroring the invalid-completion-record
    surface ("the orchestrator could not safely apply the agent's requested
    outcome"). Returns an EMPTY list when the outcome committed (or
    stale-downgraded), so the caller applies nothing on the success path and the
    genuine-failure path (whose surface the completion handler already planned).
    """
    if outcome.committed:
        return []

    grouped: dict[int, list[tuple[str, str, str]]] = {}
    failures = outcome.failures or ("act-level owner did not commit",)
    for index, error in enumerate(failures):
        action = (
            outcome.failed_actions[index]
            if index < len(outcome.failed_actions)
            else None
        )
        target_issue, action_name, target_description = _failure_surface_identity(
            action, fallback_issue_number=issue_number
        )
        grouped.setdefault(target_issue, []).append(
            (action_name, target_description, error)
        )

    actions: list[Action] = []
    for target_issue, target_failures in grouped.items():
        details = "\n".join(
            f"- Action: `{action_name}`\n"
            f"- Target: {target_description}\n"
            f"- Failure: {error}"
            for action_name, target_description, error in target_failures
        )
        comment = (
            "**Required Tech Lead Action Did Not Complete**\n\n"
            "The tech_lead decision mandated an act-level mutation, but its "
            "execution owner failed at apply time. The orchestrator recorded "
            "the tech_lead session as FAILED instead of accepting its completed "
            "intent, so the target is not silently left partially mutated.\n\n"
            f"{details}\n"
            f"- Tech lead session: `{session_id}`\n"
            f"- Runtime: {runtime_minutes:.1f} minutes\n\n"
            f"Issue #{target_issue} has been marked as `{needs_human_label}` "
            "because the orchestrator could not safely apply the required "
            "action. Inspect the failure above, then remove the label after "
            "correcting or re-running that action."
        )
        actions.extend(
            (
                AddLabelAction(
                    issue_number=target_issue,
                    label=needs_human_label,
                    reason=(
                        "mandated tech_lead act-level action did not commit; "
                        "routing target to needs-human"
                    ),
                    needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
                ),
                AddCommentAction(
                    number=target_issue,
                    comment=comment,
                    reason=(
                        "notify target operator that a mandated tech_lead action "
                        "failed at apply time"
                    ),
                ),
            )
        )
    return actions


def _failure_surface_identity(
    action: Action | None, *, fallback_issue_number: int
) -> tuple[int, str, str]:
    """Return the truthful action name and mutation target for operator routing."""
    if isinstance(action, KillHungSessionAction):
        target = (
            f"issue #{action.issue_number}, {action.target_session_type} terminal "
            f"`{action.target_terminal_id}`, run `{action.target_session_id}`"
        )
        return action.issue_number, "kill_hung_session", target
    if isinstance(action, ResetRetryIssueAction):
        return (
            action.issue_number,
            "reset_retry",
            f"issue #{action.issue_number}",
        )
    return (
        fallback_issue_number,
        "tech_lead_act_level",
        f"issue #{fallback_issue_number}",
    )
