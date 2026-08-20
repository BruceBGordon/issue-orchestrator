"""Execution-time owner for tech_lead ``kill_hung_session`` ops.

The action may come directly from ``execute`` authority or from an approved
gated proposal (#6778). It is a stale-checkable fact recorded against the board
the proposing session observed, so the executor re-validates immediately
before acting:

1. the target issue must STILL have an active session — the entire point of
   the op is terminating live-but-stuck work, so a session that already
   exited (completed, crashed, was reset) makes the proposal stale.

On a stale precondition the op DOWNGRADES exactly like ``reset_retry``:
``publish_proposal_surfaced`` emits ``TECH_LEAD_ACTION_PROPOSED`` with
``mode="stale_downgrade"`` and no mutations are posted (the applier's
finalizer then closes the proposal issue with a "preconditions no longer
hold" comment). On success a ``TECH_LEAD_ACTION_EXECUTED`` event records the
termination boundary effects. Kill-owner failures fail the action loudly.

The termination itself is NOT reimplemented here: ``run_kill`` is the
injected production boundary — ``terminate_issue_runtime`` via
``Orchestrator.terminate_issue_runtime_for_issue``, the same issue-terminal
boundary the reset owner applies, WITHOUT the reset (no PR superseding, no
label/history clearing, no relaunch). Production wiring lives in
``entrypoints/tech_lead_reset_retry_wiring.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, make_trace_event
from .actions import ActionResult, KillHungSessionAction
from .tech_lead_reset_retry import (
    STALE_DOWNGRADE_MODE,
    publish_proposal_surfaced,
)

logger = logging.getLogger(__name__)

# Cap applied to rationale previews in surfaced events, matching the other
# proposal surfaces (tech_lead_decision_actions / tech_lead_reset_retry).
_RATIONALE_PREVIEW_CHARS = 500


@dataclass(frozen=True)
class KillSessionRunOutcome:
    """Typed result of one termination-owner invocation (injected boundary)."""

    success: bool
    error: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


# The production boundary: (issue_number, reason) -> outcome.
RunKillFn = Callable[[int, str], KillSessionRunOutcome]


def kill_hung_session_stale_reason(
    *,
    issue_number: int,
    active_session_id: str | None,
    approved_session_id: str,
) -> str | None:
    """Why the op's preconditions no longer hold, or None when valid.

    Approval is bound to the exact session generation the proposal diagnosed
    (#6779 R1): ``approved_session_id`` is the target's active session run id
    captured when the proposal was filed, and ``active_session_id`` is the
    run id of the target's live session right now (``None`` when none runs).
    The op is stale unless they still match — otherwise the diagnosed session
    exited and a replacement started, which the approver never consented to
    terminating.
    """
    if active_session_id is None:
        return (
            f"issue #{issue_number} has no active session; the session the"
            " proposal diagnosed as hung is already gone"
        )
    if not approved_session_id:
        return (
            f"the proposal recorded no session identity for issue"
            f" #{issue_number}; refusing to kill an unverified session"
        )
    if active_session_id != approved_session_id:
        return (
            f"issue #{issue_number}'s live session (run {active_session_id})"
            f" is not the one approved (run {approved_session_id}); a"
            " replacement session started before approval"
        )
    return None


@dataclass
class TechLeadKillSessionExecutor:
    """Applies :class:`KillHungSessionAction` with execution-time re-validation.

    All collaborators are injected: ``active_session_run_id`` reads the run id
    of the target issue's live session (``None`` when none runs) and
    ``run_kill`` is the reused issue-runtime termination boundary. The
    executor owns only the validate/downgrade/execute/surface policy.
    """

    events: EventSink
    active_session_run_id: Callable[[int], str | None]
    run_kill: RunKillFn

    def apply(self, action: KillHungSessionAction) -> ActionResult:
        stale = kill_hung_session_stale_reason(
            issue_number=action.issue_number,
            active_session_id=self.active_session_run_id(action.issue_number),
            approved_session_id=action.target_session_id,
        )
        if stale is not None:
            return self._downgrade(action, stale)
        authority_source = (
            f"approved proposal #{action.proposal_issue_number}"
            if action.proposal_issue_number
            else f"direct authority on anchor #{action.anchor_issue_number}"
        )
        outcome = self.run_kill(
            action.issue_number,
            f"tech_lead kill_hung_session {action.proposal_id} ({authority_source})",
        )
        if not outcome.success:
            logger.error(
                issue_log(
                    action.issue_number,
                    "Tech Lead kill_hung_session %s FAILED in the termination"
                    " owner: %s",
                ),
                action.proposal_id,
                outcome.error,
            )
            return ActionResult.fail(
                action,
                f"termination owner failed for issue #{action.issue_number}"
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
                    "proposal_type": "kill_hung_session",
                    "target_number": action.issue_number,
                    "finding_ids": list(action.finding_ids),
                    "boundary": dict(outcome.details),
                },
            )
        )
        logger.info(
            issue_log(
                action.issue_number,
                "Tech Lead kill_hung_session %s executed via the termination owner",
            ),
            action.proposal_id,
        )
        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            proposal_id=action.proposal_id,
        )

    def _downgrade(self, action: KillHungSessionAction, stale: str) -> ActionResult:
        """Stale precondition: surface as would-have-done, post no mutations."""
        logger.warning(
            issue_log(
                action.issue_number,
                "Tech Lead kill_hung_session %s downgraded to surfaced proposal: %s",
            ),
            action.proposal_id,
            stale,
        )
        publish_proposal_surfaced(
            self.events,
            issue_number=action.anchor_issue_number,
            action_id=action.proposal_id,
            proposal_type="kill_hung_session",
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
