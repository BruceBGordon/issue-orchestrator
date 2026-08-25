"""Execution-time owner for tech_lead ``kill_hung_session`` ops.

The action may come directly from ``execute`` authority or from an approved
gated proposal (#6778). It is a stale-checkable fact recorded against the board
the proposing session observed. The injected termination owner re-validates
and mutates that same typed generation in one boundary:

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
generation-aware production boundary. It conditionally stops the exact
terminal/run pair and tears down its issue-scoped hidden owners, WITHOUT the
reset (no PR superseding, label/history clearing, or relaunch). Production
wiring lives in ``entrypoints/tech_lead_reset_retry_wiring.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..events import EventName
from ..domain.session_key import TaskKind
from ..domain.tech_lead_session import TechLeadSessionGeneration
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
    stale_reason: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


# The production boundary: (exact observed generation, reason) -> outcome.
RunKillFn = Callable[[TechLeadSessionGeneration, str], KillSessionRunOutcome]


def kill_hung_session_stale_reason(
    *,
    issue_number: int,
    target_session_id: str,
    target_terminal_id: str,
    target_session_type: str,
) -> str | None:
    """Why an action has no complete trusted generation, or ``None``."""
    if not target_session_id or not target_terminal_id or not target_session_type:
        return (
            f"the proposal recorded no complete session generation for issue"
            f" #{issue_number}; refusing to kill an unverified runtime"
        )
    try:
        task_kind = TaskKind(target_session_type)
    except ValueError:
        return (
            f"the proposal recorded unsupported session type"
            f" {target_session_type!r} for issue #{issue_number}"
        )
    if task_kind not in {TaskKind.CODE, TaskKind.REWORK}:
        return (
            f"the proposal targeted non-killable {task_kind.value!r} work"
            f" for issue #{issue_number}"
        )
    return None


@dataclass
class TechLeadKillSessionExecutor:
    """Applies :class:`KillHungSessionAction` with execution-time re-validation.

    ``run_kill`` owns the live revalidation and exact termination atomically.
    This executor validates the stored identity envelope and owns only the
    downgrade/execute/surface policy.
    """

    events: EventSink
    run_kill: RunKillFn

    def apply(self, action: KillHungSessionAction) -> ActionResult:
        stale = kill_hung_session_stale_reason(
            issue_number=action.issue_number,
            target_session_id=action.target_session_id,
            target_terminal_id=action.target_terminal_id,
            target_session_type=action.target_session_type,
        )
        if stale is not None:
            return self._downgrade(action, stale)
        target = TechLeadSessionGeneration(
            issue_number=action.issue_number,
            task_kind=TaskKind(action.target_session_type),
            terminal_id=action.target_terminal_id,
            run_id=action.target_session_id,
        )
        authority_source = (
            f"approved proposal #{action.proposal_issue_number}"
            if action.proposal_issue_number
            else f"direct authority on anchor #{action.anchor_issue_number}"
        )
        outcome = self.run_kill(
            target,
            f"tech_lead kill_hung_session {action.proposal_id} ({authority_source})",
        )
        if outcome.stale_reason is not None:
            return self._downgrade(action, outcome.stale_reason)
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
